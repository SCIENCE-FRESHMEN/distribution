import pandas as pd
import json
import os
from pathlib import Path
import datetime
import pytz

class ProductionPlanBuilder:
    def __init__(self, plan_df, sku_config):
        """
        :param plan_df: 生产计划 DataFrame
        :param sku_config: SKU 配置（来自 SKUConfigBuilder.load_json）
        """
        self.plan_df = plan_df
        self.sku_config = sku_config
        self.production_plan = {1: [], 2: [], 3: []}
        # 额外属性（与production_plan同结构），key=字段名
        self.production_plan_attrs = {"version": {1: [], 2: [], 3: []}}
        # 与生产计划同结构的创建时间列表（秒）
        self.creation_times = {1: [], 2: [], 3: []}

    @staticmethod
    def _to_seconds(val):
        """将 Excel/字符串时间转换为秒，失败返回 0"""
        try:
            if isinstance(val, pd.Timestamp):
                # 如果时间戳没有时区信息，假设为中国标准时间(CST, UTC+8)
                if val.tz is None:
                    local_tz = pytz.timezone('Asia/Shanghai')  # 中国标准时间
                    localized_dt = local_tz.localize(val)
                    return localized_dt.timestamp()
                else:
                    return val.timestamp()
            if isinstance(val, (int, float)):
                # 假设数值型时间戳是秒单位的时间戳
                return float(val)
            
            # 将输入解析为datetime对象
            dt = pd.to_datetime(val)
            if dt.tz is None:
                # 假设解析出的时间是本地时间（中国标准时间）
                local_tz = pytz.timezone('Asia/Shanghai')
                localized_dt = local_tz.localize(dt)
                return localized_dt.timestamp()
            else:
                return dt.timestamp()
        except Exception:
            try:
                return float(val)
            except Exception:
                return 0.0

    # ========= 从文件导入SKU 配置 =========
    @classmethod
    def from_files(cls, plan_path, sku_config_path):
        """
        从 Excel/CSV 文件和 SKU 配置文件构建对象
        """
        if not os.path.exists(plan_path):
            raise FileNotFoundError(f"计划文件不存在: {plan_path}")
        if not os.path.exists(sku_config_path):
            raise FileNotFoundError(f"SKU配置文件不存在: {sku_config_path}")

        if plan_path.endswith(".xlsx"):
            plan_df = pd.read_excel(plan_path)
        elif plan_path.endswith(".csv"):
            plan_df = pd.read_csv(plan_path)
        else:
            raise ValueError("仅支持 Excel (.xlsx) 或 CSV (.csv) 文件")

        with open(sku_config_path, "r", encoding="utf-8") as f:
            sku_config = json.load(f)

        return cls(plan_df, sku_config)

    # ========= 判定函数 =========
    def _is_single_layer_sku(self, sku):
        """判断是否为可合并的单层梁 SKU"""
        sku_pairs = self.sku_config.get("sku_pairs", {})
        sku_solo = set(self.sku_config.get("sku_solo", []))
        if not sku or sku in ("", "9", "nan", "NaN", "None"):
            return False
        return sku in sku_pairs and sku_pairs.get(sku) == sku and sku not in sku_solo

    # ========= 构建逻辑 =========
    def build(self, filter_solo_sku=False):
        df = self.plan_df.copy()
        sku_pairs = self.sku_config.get("sku_pairs", {})
        sku_solo = set(self.sku_config.get("sku_solo", []))

        line_map = {"装配一线": 1, "装配二线": 2, "装配三线": 3}
        df["计划类型"] = df["计划类型"].astype(str).str.strip()
        df["车架号"] = df["车架号"].astype(str).str.strip()
        
        # 识别创建时间列（生产计划中的创建时间）
        creation_col = None
        for cand in ["完成时间", "creation_time"]:
            if cand in df.columns:
                creation_col = cand
                break
        # 识别生产属性列
        prod_attr_col = []
        for cand in ["生产属性", "production_attr"]:
            if cand in df.columns:
                prod_attr_col = cand
                break
        # 遍历每个产线
        for plan_type, group in df.groupby("计划类型", sort=False):
            line = line_map.get(plan_type)
            if not line:
                continue

            entries = []
            for _, row in group.iterrows():
                def norm(x): return str(x).strip()
                def is_empty_or_9(x): return x in ("", "9", "nan", "NaN", "None")
                def norm_or_none(x):
                    v = norm(x)
                    return None if is_empty_or_9(v) else v

                left_main = norm(row.get("零件号（左主）", ""))
                left_main_ver = norm_or_none(row.get("版本号（左主）", ""))
                left_sub = norm(row.get("零件号（左副）", ""))
                left_sub_ver = norm_or_none(row.get("版本号（左副）", ""))
                right_main = norm(row.get("零件号（右主）", row.get("零件号（右主", "")))
                right_main_ver = norm_or_none(row.get("版本号（右主）", row.get("版本号（右主", "")))
                right_sub = norm(row.get("零件号（右副）", ""))
                right_sub_ver = norm_or_none(row.get("版本号（右副）", ""))
                prod_attr_val = norm_or_none(row.get(prod_attr_col, "")) if prod_attr_col else None
                car_frame = norm(row.get("车架号", ""))
                creation_time = row.get(creation_col) if creation_col else None

                left = [left_main] if is_empty_or_9(left_sub) else [left_main, left_sub]
                right = [right_main] if is_empty_or_9(right_sub) else [right_main, right_sub]
                left_versions = [left_main_ver] if is_empty_or_9(left_sub) else [left_main_ver, left_sub_ver]
                right_versions = [right_main_ver] if is_empty_or_9(right_sub) else [right_main_ver, right_sub_ver]
                left_prod_attrs = [prod_attr_val] if is_empty_or_9(left_sub) else [prod_attr_val, prod_attr_val]
                right_prod_attrs = [prod_attr_val] if is_empty_or_9(right_sub) else [prod_attr_val, prod_attr_val]
                entries.append({
                    "car_frame": car_frame,
                    "left": left,
                    "right": right,
                    "left_versions": left_versions,
                    "right_versions": right_versions,
                    "left_prod_attrs": left_prod_attrs,
                    "right_prod_attrs": right_prod_attrs,
                    "creation_time": self._to_seconds(creation_time) if creation_col else None,
                })

            # 顺序扫描并合并
            i = 0
            n = len(entries)
            while i < n:
                cur = entries[i]
                left, right, car_frame = cur["left"], cur["right"], cur["car_frame"]
                left_versions, right_versions = cur["left_versions"], cur["right_versions"]
                left_prod_attrs, right_prod_attrs = cur["left_prod_attrs"], cur["right_prod_attrs"]
                ctime = cur["creation_time"]

                left_is_single = (len(left) == 1 and self._is_single_layer_sku(left[0]))
                right_is_single = (len(right) == 1 and self._is_single_layer_sku(right[0]))
                current_both_single = left_is_single and right_is_single

                if current_both_single and (i + 1) < n:
                    nxt = entries[i + 1]
                    nxt_left, nxt_right, nxt_car_frame = nxt["left"], nxt["right"], nxt["car_frame"]
                    nxt_left_versions, nxt_right_versions = nxt["left_versions"], nxt["right_versions"]
                    nxt_left_prod_attrs, nxt_right_prod_attrs = nxt["left_prod_attrs"], nxt["right_prod_attrs"]
                    nxt_ctime = nxt["creation_time"]

                    nxt_left_is_single = (len(nxt_left) == 1 and self._is_single_layer_sku(nxt_left[0]))
                    nxt_right_is_single = (len(nxt_right) == 1 and self._is_single_layer_sku(nxt_right[0]))
                    next_both_single = nxt_left_is_single and nxt_right_is_single

                    if next_both_single and (nxt_car_frame == car_frame):
                        combined = [[left[0], nxt_left[0]], [right[0], nxt_right[0]]]
                        self.production_plan[line].append(combined)
                        combined_versions = [[left_versions[0], nxt_left_versions[0]], [right_versions[0], nxt_right_versions[0]]]
                        self.production_plan_attrs["version"][line].append(combined_versions)
                        combined_prod_attrs = [[left_prod_attrs[0], nxt_left_prod_attrs[0]], [right_prod_attrs[0], nxt_right_prod_attrs[0]]]
                        self.production_plan_attrs.setdefault("生产属性", {1: [], 2: [], 3: []})
                        self.production_plan_attrs["生产属性"][line].append(combined_prod_attrs)                        
                        self.creation_times[line].append(ctime if ctime is not None else nxt_ctime)
                        i += 2
                        continue

                # 其他情况直接输出单条
                self.production_plan[line].append([left, right])
                self.production_plan_attrs["version"][line].append([left_versions, right_versions])
                self.production_plan_attrs.setdefault("生产属性", {1: [], 2: [], 3: []})
                self.production_plan_attrs["生产属性"][line].append([left_prod_attrs, right_prod_attrs])
                self.creation_times[line].append(ctime)
                i += 1

        return self

    # ========= 导出为 JSON =========
    def to_json(self, filepath=None, indent=4):
        data = {
            "production_plan": self.production_plan,
            "production_plan_attrs": self.production_plan_attrs,
            "creation_times": self.creation_times
        }
        if filepath:
            Path(filepath).parent.mkdir(parents=True, exist_ok=True)
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=indent)
            print(f"已生成生产计划 JSON：{filepath}")
        return json.dumps(data, ensure_ascii=False, indent=indent)

    # ========= 可视化输出 =========
    def show(self):
        for line, plans in self.production_plan.items():
            print(f"\n=== 产线 {line} ===")
            for i, p in enumerate(plans, 1):
                print(f"第{i}组: {p}")

    # =========  导入方法 =========
    @classmethod
    def load_json(cls, filepath):
        """
        从 JSON 文件加载已生成的生产计划
        :param filepath: JSON 文件路径
        :return: ProductionPlanBuilder 实例
        """
        if not os.path.exists(filepath):
            raise FileNotFoundError(f"生产计划文件不存在: {filepath}")

        with open(filepath, "r", encoding="utf-8") as f:
            production_plan = json.load(f)

        # 兼容老格式（仅生产计划）和新格式（包含 creation_times/attrs）
        if isinstance(production_plan, dict) and "production_plan" in production_plan:
            creation_times = production_plan.get("creation_times", {1: [], 2: [], 3: []})
            production_plan_attrs = production_plan.get("production_plan_attrs")
            production_plan_versions = production_plan.get("production_plan_versions")
            production_plan = production_plan["production_plan"]
        else:
            creation_times = {1: [], 2: [], 3: []}
            production_plan_attrs = None
            production_plan_versions = None

        production_plan = {int(k): v for k, v in production_plan.items()}
        creation_times = {int(k): v for k, v in creation_times.items()}
        # 统一为 attrs 结构；老版本的 production_plan_versions 转为 attrs["version"]
        if production_plan_attrs is None:
            production_plan_attrs = {}
            if production_plan_versions is not None:
                production_plan_attrs["version"] = production_plan_versions
        production_plan_attrs = {k: {int(kk): vv for kk, vv in v.items()} for k, v in (production_plan_attrs or {}).items()}
        
        instance = cls(plan_df=None, sku_config=None)
        instance.production_plan = production_plan
        instance.creation_times = creation_times
        instance.production_plan_attrs = production_plan_attrs

        print(f"已加载生产计划 JSON：{filepath}")
        return instance


# ========= 可直接运行（compare版本） =========
if __name__ == "__main__":
    builder = ProductionPlanBuilder.from_files(
        plan_path="simulation/data/production_plan.xlsx",
        sku_config_path="simulation/data/sku_config.json"
    ).build()

    builder.to_json("simulation/data/production_plan_config.json")

# ========= 可直接运行（daily版本） =========
# if __name__ == "__main__":

#     # 定义源数据目录和目标输出目录
#     data_dir = Path("simulation/data/daily")
#     output_dir = data_dir  # 输出到同一目录下

#     # 确保输出目录存在
#     output_dir.mkdir(parents=True, exist_ok=True)

#     # SKU 配置文件路径（假设固定）
#     sku_config_path = "simulation/data/sku_config.json"

#     # 扫描所有生产计划文件
#     for file_path in data_dir.glob("production_plan_*.xlsx"):
#         print(f"Processing: {file_path}")
#         try:
#             builder = ProductionPlanBuilder.from_files(
#                 plan_path=str(file_path),
#                 sku_config_path=sku_config_path
#             ).build()
#             # 构造输出路径：将 .xlsx 替换为 _config.json，并将日期移到末尾
#             base_name = file_path.stem  # e.g., production_plan_20251012
#             date_part = re.search(r'(\d{8})', base_name)
#             if date_part:
#                 date_str = date_part.group(1)
#                 # 提取非日期部分
#                 non_date_part = base_name.replace(date_str, "").strip("_")
#                 # 新文件名：non_date_part_config_date_str.json
#                 json_output_path = output_dir / f"{non_date_part}_config_{date_str}.json"
#             else:
#                 json_output_path = output_dir / f"{base_name}_config.json"
#             builder.to_json(json_output_path)
#         except Exception as e:
#             print(f"Error processing {file_path}: {e}")
