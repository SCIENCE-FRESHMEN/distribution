"""将 BOM 表转换为仓库运行时 SKU 配置。

输出的 ``sku_types``、``sku_pairs``、``sku_solo`` 和产线映射可被核心库存、
入库分配器及出库配对判断共同读取。
"""

import pandas as pd
import json
import os

from config_loader import load_jsonc

class SKUConfigBuilder:
    """从 BOM 行构建双层配对梁与单层独立梁配置。"""
    def __init__(self, bom_df):
        """初始化对象的运行状态和依赖对象，使实例可被调用方继续使用。


        Returns:
            None: 通过实例状态、队列或外部副作用完成处理。
        """
        self.bom_df = bom_df

        # 输出结果
        self.sku_types = []
        self.sku_pairs = {}
        self.sku_solo = {}
        self.sku_to_production_line = {}

    # ========= 新增：从文件加载 =========
    @classmethod
    def from_file(cls, filepath):
        """从 Excel 或 CSV 文件导入 BOM 数据
        必须包含列：层数、主梁零件号、副梁零件号

        """
        if not os.path.exists(filepath):
            raise FileNotFoundError(f"文件不存在: {filepath}")

        if filepath.endswith(".xlsx"):
            df = pd.read_excel(filepath)
        elif filepath.endswith(".csv"):
            df = pd.read_csv(filepath)
        else:
            raise ValueError("仅支持 Excel (.xlsx) 或 CSV (.csv) 文件")

        # 检查列名
        required_cols = ["层数", "主梁零件号", "副梁零件号"]
        for col in required_cols:
            if col not in df.columns:
                raise ValueError(f"缺少必要列: {col}")

        return cls(df)

    # ========= 主逻辑：构建配置信息 =========
    def build(self):
        """遍历 BOM，建立双向配对索引和可用于库存初始化的 SKU 集合。

        Args:
            None: 无显式业务参数；使用实例状态或模块配置。

        """
        # 先填充缺失值为字符串 '9'
        df = self.bom_df.fillna("9")

        for _, row in df.iterrows():
            layer_type, main_part, sub_part = row["层数"], str(row["主梁零件号"]), str(row["副梁零件号"])

            # ============ 双层梁 ============
            if layer_type == "双层梁":
                if sub_part == "9":
                    # 无副梁 → 独立SKU（单独占货位）
                    self.sku_solo[main_part] = True
                    if main_part not in self.sku_types and main_part != "9":
                        self.sku_types.append(main_part)
                    continue

                # 主梁 ↔ 副梁（跳过9）
                if main_part != "9" and sub_part != "9":
                    self.sku_pairs[main_part] = sub_part
                    self.sku_pairs[sub_part] = main_part

                    for part in [main_part, sub_part]:
                        if part not in self.sku_types and part != "9":
                            self.sku_types.append(part)

            # ============ 单层梁 ============
            elif layer_type == "单层梁":
                if main_part != "9":
                    self.sku_pairs[main_part] = main_part
                    if main_part not in self.sku_types:
                        self.sku_types.append(main_part)

        # ============ 自动分配产线 ============
        self.sku_to_production_line = {
            sku: ["1", "2", "3"] for sku in self.sku_types
        }

        return self


    # ========= 可视化输出 =========
    def show(self):
        """执行 `show` 对应的模块处理步骤，并返回该步骤产生的结果。

        输入：无显式业务输入；依赖实例字段或模块配置。

        Args:
            None: 无显式业务参数；使用实例状态或模块配置。

        Returns:
            None: 通过实例状态、队列或外部副作用完成处理。
        """
        print("=== SKU 类型列表 ===")
        print(self.sku_types, "\n")

        print("=== SKU 配对关系 ===")
        for k, v in self.sku_pairs.items():
            print(f"{k} ↔ {v}")
        print()

        print("=== 无配对 SKU ===")
        for k in self.sku_solo.keys():
            print(k)
        print()

        print("=== SKU → 产线 ===")
        for k, v in self.sku_to_production_line.items():
            print(f"{k}: line {v}")

    # ========= 导出为 Python dict =========
    def to_dict(self):
        """执行 `to_dict` 对应的模块处理步骤，并返回该步骤产生的结果。

        输入：无显式业务输入；依赖实例字段或模块配置。

        Args:
            None: 无显式业务参数；使用实例状态或模块配置。

        """
        return {
            "sku_types": self.sku_types,
            "sku_pairs": self.sku_pairs,
            "sku_solo": self.sku_solo,
            "sku_to_production_line": self.sku_to_production_line
        }

    # ========= 导出为 JSON =========
    def to_json(self, filepath=None, indent=2):
        """执行 `to_json` 对应的模块处理步骤，并返回该步骤产生的结果。

        """
        data = self.to_dict()
        if filepath:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=indent)
            print(f"已保存 JSON 文件：{filepath}")
        return json.dumps(data, ensure_ascii=False, indent=indent)

    @staticmethod
    def load_json(filepath):
        """加载 SKU/BOM 配置，兼容 JSON 与带注释的 JSONC 文件。

        Args:
            filepath: SKU 配置文件路径；文件包含 SKU 列表、配对关系、单梁标记及产线映射。

        Returns:
            包含 ``sku_types``、``sku_pairs``、``sku_solo`` 和
            ``sku_to_production_line`` 的配置字典。

        Raises:
            FileNotFoundError: 指定路径不存在时抛出。
            json.JSONDecodeError: 去除 JSONC 注释后的内容不是有效 JSON 时抛出。
        """
        if not os.path.exists(filepath):
            raise FileNotFoundError(f"配置文件不存在: {filepath}")

        # 统一使用项目的 JSONC 读取器，使人工维护的 BOM 文件可保留字段说明注释。
        return load_jsonc(filepath)



if __name__ == "__main__":
    # 从 Excel / CSV 导入
    config = SKUConfigBuilder.from_file("simulation/data/bom.xlsx").build()
    # 导出 JSON 配置
    config.to_json("simulation/data/sku_config.json")
