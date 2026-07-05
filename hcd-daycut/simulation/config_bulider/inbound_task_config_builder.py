import pandas as pd
import json
import pytz
import re
from pathlib import Path

class InboundConfigBuilder:
    def __init__(self, source_path: str):
        self.source_path = source_path
        self.inbound_records = []
        # 可选：表格含真实到达时间（列名如“到达时间”或 arrival_time），会一并保存
        self.has_arrival_time = False

    @staticmethod
    def _extract_sku(raw_value):
        """提取 SKU（去掉 / 后面的二维码部分）"""
        if not isinstance(raw_value, str):
            return None
        return raw_value.split('/')[0].strip()

    @staticmethod
    def _to_seconds(val):
        """Excel 时间/字符串时间戳转秒（转换为UTC时间戳），失败则返回 0"""
        try:
            if isinstance(val, pd.Timestamp):
                if val.tz is None:
                    # 假设 naive Timestamp 是本地时间（中国标准时间）
                    local_tz = pytz.timezone('Asia/Shanghai')
                    localized_dt = local_tz.localize(val)
                    return localized_dt.timestamp()  # 自动转换为 UTC 时间戳
                else:
                    return val.timestamp()
            if isinstance(val, (int, float)):
                # 假设传入的数值型时间戳是 UTC 时间戳（单位：秒）
                return float(val)
            
            # 解析字符串或其他格式的时间
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

    def build(self):
        df = pd.read_excel(self.source_path) if self.source_path.endswith(('.xlsx', '.xls')) else pd.read_csv(self.source_path)

        # 识别到达时间列（allocation中的到达时间）
        arrival_col = None
        for cand in ["到达时间", "arrival_time"]:
            if cand in df.columns:
                arrival_col = cand
                break
        self.has_arrival_time = arrival_col is not None

        # 识别入库线路列（可选）
        line_col = None
        for cand in ["入库层", "in_line"]:
            if cand in df.columns:
                line_col = cand
                break

        for _, row in df.iterrows():
            sku_a = self._extract_sku(row.get('纵梁A', ''))
            sku_b = self._extract_sku(row.get('纵梁B', ''))
            arrival_time = row.get(arrival_col) if arrival_col else None
            in_line = int(row.get(line_col)) if line_col and pd.notna(row.get(line_col)) else 1

            if self.has_arrival_time:
                rec = {
                    "arrival_time": self._to_seconds(arrival_time) if pd.notna(arrival_time) else 0.0,
                    "skus": [],
                    "version": [],
                    "生产属性": [],
                    "in_line": in_line,
                }
                rec["skus"].append(sku_a if sku_a else None)
                rec["skus"].append(sku_b if sku_b else None)
                rec["version"].append("00" if sku_a else None)
                rec["version"].append("00" if sku_b else None)
                rec["生产属性"].append("D" if sku_a else None)
                rec["生产属性"].append("D" if sku_b else None)
                self.inbound_records.append(rec)
            else:
                skus_pair = [sku_a if sku_a else None, sku_b if sku_b else None]
                if line_col:
                    self.inbound_records.append({
                        "skus": skus_pair,
                        "version": ["00" if sku_a else None, "00" if sku_b else None],
                        "生产属性": ["D" if sku_a else None, "D" if sku_b else None],
                        "in_line": in_line
                    })
                else:
                    self.inbound_records.append({
                        "skus": skus_pair,
                        "version": ["00" if sku_a else None, "00" if sku_b else None],
                        "生产属性": ["D" if sku_a else None, "D" if sku_b else None],
                    })
        return self

    def to_dict(self):
        return {"inbound_records": self.inbound_records}

    def save_json(self, save_path: str = "simulation/data/inbound_config.json"):
        Path(save_path).parent.mkdir(parents=True, exist_ok=True)
        with open(save_path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=4)
        print(f"Inbound config saved to: {save_path}")

    @staticmethod
    def load_json(path: str):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        builder = InboundConfigBuilder(source_path="")
        builder.inbound_records = data["inbound_records"]
        return builder

# ========= compare版本 =========
if __name__ == "__main__":
    builder = InboundConfigBuilder("simulation/data/aisle_allocation.xlsx").build()
    builder.save_json("simulation/data/inbound_task_config.json")
# ========= daily版本 =========
# if __name__ == "__main__":
#
#     # 定义源数据目录和目标输出目录
#     data_dir = Path("simulation/data/daily")
#     output_dir = data_dir
#
#     # 确保输出目录存在
#     output_dir.mkdir(parents=True, exist_ok=True)
#
#     # 扫描所有入库数据文件
#     for file_path in data_dir.glob("aisle_allocation_*.xlsx"):
#         print(f"Processing: {file_path}")
#         try:
#             builder = InboundConfigBuilder(str(file_path)).build()
#             # 构造输出路径：将日期移到末尾
#             base_name = file_path.stem  # e.g., aisle_allocation_20251012
#             date_part = re.search(r'(\d{8})', base_name)
#             if date_part:
#                 date_str = date_part.group(1)
#                 non_date_part = base_name.replace(date_str, "").strip("_")
#                 json_output_path = output_dir / f"{non_date_part}_config_{date_str}.json"
#             else:
#                 json_output_path = output_dir / f"{base_name}_config.json"
#             builder.save_json(str(json_output_path))
#         except Exception as e:
#             print(f"Error processing {file_path}: {e}")

