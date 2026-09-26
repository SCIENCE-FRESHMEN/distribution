"""维护车身立体库的货位主数据、库存索引、禁用位和入出库写入。"""

import copy
import random
import time
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Union
from .position import InventoryPosition


# ==========================================================================
# 主类：库存、货位和索引管理
# ==========================================================================
class InventoryManager:
    """车身库库存权威对象，同步维护坐标索引、SKU 索引和巷道库存汇总。"""

    def __init__(self, num_aisles: int = 3, num_rows: int = 2, num_columns: int = 11,
                 num_levels: int = 5, total_positions: int = 330, max_beams: int = 330,
                 sku_types: Optional[List[str]] = None, initial_inventory_ratio: float = 0.3,
                 use_double_layer: bool = True,
                 sku_pairs: Optional[Dict[str, str]] = None,
                 sku_solo: Optional[List[str]] = None,
                 disabled_positions: Optional[List[Union[str, Dict[str, int]]]] = None,
                 aisle_dimensions: Optional[Dict[Any, Dict[str, int]]] = None):
        """
        Args:
            num_aisles: 巷道数量
            num_rows: 行数
            num_columns: 列数
            num_levels: 层数
            total_positions: 总货位数
            max_beams: 最大梁数
            sku_types: SKU类型列表
            initial_inventory_ratio: 初始库存比例
            use_double_layer: 是否使用双层货位
            sku_pairs: SKU配对关系字典
            sku_solo: 单独梁SKU列表
            disabled_positions: 禁用的货位列表
            aisle_dimensions: 按巷道覆盖仓位维度，格式:
                {
                  "1": {"rows": 2, "columns": 17, "levels": 5},
                  "4": {"rows": 2, "columns": 11, "levels": 5}
                }
        """
        self.num_aisles = num_aisles
        self.num_rows = num_rows
        self.num_columns = num_columns
        self.num_levels = num_levels
        self.total_positions = total_positions
        self.max_beams = max_beams
        self.initial_inventory_ratio = initial_inventory_ratio
        self.use_double_layer = use_double_layer
        self.disabled_position_ids = self._normalize_disabled_positions(disabled_positions)
        self.aisle_dimensions = self._normalize_aisle_dimensions(aisle_dimensions)

        # SKU配置：新数据结构下允许为空，SKU按实际入库动态出现
        self.sku_types = sku_types or []
        self.sku_pairs = sku_pairs or {}  # SKU配对关系
        self.sku_solo = set(sku_solo or [])  # 单独梁SKU集合
        self.aisles = list(range(1, num_aisles + 1))

        # 货位管理
        self.inventory_positions: List[InventoryPosition] = []
        self.position_map: Dict[str, InventoryPosition] = {}
        self.sku_position_index: Dict[str, List[InventoryPosition]] = {}

        # 简化库存（用于快速查询）
        self.current_inventory: Dict[int, Dict[str, int]] = {}
        # 需要额外跟踪日志的 SKU 列表
        self.sku_watchlist: set = set()

    def set_sku_watchlist(self, skus: List[str]):
        """设置需要额外输出日志的 SKU 列表"""
        self.sku_watchlist = set(skus or [])

    def resolve_inbound_time(self, supplied_time: Optional[Any] = None) -> float:
        """将外部入库时间归一为 Unix 秒时间戳。

        Args:
            supplied_time: 上游传入的 ``inboundTime`` 或 ``arrivalTime``；支持数值型 Unix 秒
                和 ISO 8601 字符串。未传或格式错误时使用当前系统时间。

        Returns:
            float: 可用于 FIFO 排序的入库时间戳。数值越小表示越早入库。
        """
        if isinstance(supplied_time, (int, float)) and not isinstance(supplied_time, bool):
            timestamp = float(supplied_time)
            if timestamp >= 0:
                return timestamp
        if isinstance(supplied_time, str) and supplied_time.strip():
            try:
                normalized = supplied_time.strip().replace("Z", "+00:00")
                return datetime.fromisoformat(normalized).timestamp()
            except ValueError:
                pass
        return time.time()

    def initialize(self, log_initialization: bool = True):
        """建立货位、索引和可选的仿真初始库存。

        Args:
            log_initialization: 是否输出初始化库存和货位统计；API 启动时可关闭，
                避免将空结构初始化误认为真实库存初始化。

        Returns:
            None: 重建 ``inventory_positions``、``position_map``、SKU 索引和库存汇总。
        """
        mode_str = "Double Shelf Warehouse" if self.use_double_layer else "Single Shelf Warehouse"
        if log_initialization:
            print(f"[INIT] {mode_str}...")

        self.inventory_positions = []
        self.position_map = {}
        self.sku_position_index = {sku: [] for sku in self.sku_types}

        #
        # 配置了巷道尺寸时，按实际容量限制货位总数。
        capacity = 0
        for aisle in range(1, self.num_aisles + 1):
            dims = self._get_aisle_dimensions(aisle)
            capacity += dims["rows"] * dims["columns"] * dims["levels"]
        effective_total_positions = min(self.total_positions, capacity)
        self.total_positions = effective_total_positions

        positions_created = 0
        for aisle in range(1, self.num_aisles + 1):
            dims = self._get_aisle_dimensions(aisle)
            for row in range(1, dims["rows"] + 1):
                for column in range(1, dims["columns"] + 1):
                    for level in range(1, dims["levels"] + 1):
                        if positions_created >= effective_total_positions:
                            break

                        if self.use_double_layer:
                            #
                            position = self._create_double_layer_position(aisle, row, column, level)
                        else:
                            #
                            position = self._create_single_layer_position(aisle, row, column, level)

                        self.inventory_positions.append(position)
                        self.position_map[position.get_position_id()] = position

                        # SKU 字段。
                        if not position.is_empty():
                            for sku in position.get_available_skus():
                                if sku and sku not in self.sku_position_index:
                                    self.sku_position_index[sku] = []
                                if position not in self.sku_position_index[sku]:
                                    self.sku_position_index[sku].append(position)

                        positions_created += 1
                        if positions_created >= effective_total_positions:
                            break
                    if positions_created >= effective_total_positions:
                        break
                if positions_created >= effective_total_positions:
                    break
            if positions_created >= effective_total_positions:
                break

        #
        self._update_simplified_inventory()

    def initialize_from_inbound_tasks(self, inbound_records, aisles, inbound_position_allocator=None, inbound_aisle_allocator=None, initial_inventory_count=250):
        """通过读取出入库任务记录来进行库存初始化，位置随机分配"""
        print(f"[INFO] 通过前{initial_inventory_count}组入库任务初始化库存...")

        # 获取前N组入库任务
        inbound_tasks = inbound_records[:initial_inventory_count] if len(inbound_records) >= initial_inventory_count else inbound_records

        for record in inbound_tasks:
            try:
                sku_list = record.get('skus', []) if isinstance(record, dict) else record

                # Keep side info; accept sku entries as str or dict with features.
                skus = []
                for idx_slot, sku_entry in enumerate(sku_list):
                    side = 'A' if idx_slot == 0 else 'B'
                    sku_id = sku_entry.get('skuId') if isinstance(sku_entry, dict) else sku_entry
                    sku_features = sku_entry.get('features') if isinstance(sku_entry, dict) else None
                    skus.append({'skuId': sku_id, 'quantity': 1, 'side': side, 'features': sku_features})
                non_null_skus = [s for s in skus if s['skuId'] is not None]

                # 初始化库存时只需模拟任务的 SKU 与巷道字段；不构造完整 TaskData，
                # 避免把自动初始化记录误加入运行队列或生产计划。
                task_info: Any = SimpleNamespace(skus=skus, assigned_aisle=None)

                aisle = None
                if inbound_aisle_allocator is not None:
                    try:
                        aisle = inbound_aisle_allocator.allocate(task_info, self.inventory_positions)
                    except Exception as e:
                        print(f"[ERROR] 使用巷道分配器分配巷道失败: {e}")

                # 如果没有设置巷道分配器或分配失败，则使用随机分配
                if aisle is None:
                    aisle = random.choice(aisles)
                # 确保aisle是有效的
                elif aisle not in aisles:
                    print(f"[WARN] 巷道分配器返回了无效巷道: {aisle}，使用随机分配")
                    aisle = random.choice(aisles)

                # 更新task_info中的aisle信息
                task_info.assigned_aisle = aisle

                # 使用货位分配器分配位置
                if inbound_position_allocator is not None:
                    positions = inbound_position_allocator.allocate(self.inventory_positions, task_info)
                    if len(positions) == 1 and len(non_null_skus) == 2:
                        positions = [positions[0], positions[0]]
                else:
                    positions = []
                    valid_positions = [p for p in self.inventory_positions if p.aisle == aisle and p.is_empty()]
                    if len(non_null_skus) == 1 and valid_positions:
                        positions = [random.choice(valid_positions)]
                    elif len(non_null_skus) == 2:
                        double_layer_positions = [p for p in valid_positions if p.is_double_layer and p.is_empty()]
                        if double_layer_positions:
                            pos = random.choice(double_layer_positions)
                            positions = [pos, pos]

                # 未找到可用位置时跳过本条初始化记录，不能对空 positions 做索引。
                if not positions:
                    print(f"[WARN] 初始化入库任务未找到货位，跳过: {skus}")
                    continue

                for idx, sku in enumerate(skus):
                    sku_id = sku['skuId']
                    sku_features = sku.get('features') if isinstance(sku, dict) else None
                    position = positions[min(idx, len(positions) - 1)]
                    try:
                        layer = None
                        if position.is_double_layer:
                            if len(non_null_skus) > 1:
                                if len(positions) == 2:
                                    # 双梁情况：有两个位置
                                    if positions[0] == positions[1]:
                                        # 同一个位置：根据行号决定上下层
                                        # row=1: sku1放上层，sku2放下层
                                        # row=2: sku1放下层，sku2放上层
                                        if position.row == 1:
                                            layer = 'upper' if idx == 0 else 'lower'
                                        elif position.row == 2:
                                            layer = 'lower' if idx == 0 else 'upper'
                                    else:
                                        if position.upper_sku and not position.lower_sku:
                                            layer = 'lower'
                                        elif (not position.upper_sku) and (not position.lower_sku):
                                            layer = 'upper'
                                        else:
                                            layer = 'upper'
                                else:
                                    layer = 'upper' if idx == 0 else 'lower'
                            else:
                                if position.upper_sku in (None, ''):
                                    layer = 'upper'
                                elif position.lower_sku in (None, ''):
                                    layer = 'lower'
                                else:
                                    layer = 'upper'

                        # 使用库存管理器的接口写入库存
                        self.add_inventory(
                            sku=sku_id,
                            quantity=1,
                            position=position,
                            layer=layer,
                            features=sku_features,
                            # 随机初始化库存没有真实到货时刻，统一视为仿真开始前库存。
                            inbound_time=0.0,
                        )
                    except Exception as e:
                        print(f"[ERROR] 添加库存失败: {position.get_position_id()} {sku_id}, 错误: {str(e)}")
                        continue

            except Exception as e:
                print(f"[ERROR] 处理入库任务 {sku} 时发生异常: {e}")
                continue

        # 更新简化库存
        self._update_simplified_inventory()
        print(f"[INFO] 基于前 {initial_inventory_count} 组入库任务的库存初始化完成")

    # ==========================================================================
    # 辅助函数：货位构建、配置归一化和索引维护
    # ==========================================================================
    def _create_single_layer_position(self, aisle: int, row: int, column: int, level: int) -> InventoryPosition:
        """创建single layer 货位相关逻辑。

        Args:
            self: 当前对象实例。
            aisle: 目标巷道编号。
            row: 用于本函数处理的 `row` 参数。
            column: 用于本函数处理的 `column` 参数。
            level: 用于本函数处理的 `level` 参数。

        Returns:
            InventoryPosition: 处理后的结果。
        """
        """"""
        disabled = self._is_disabled(aisle, row, column, level)
        #
        if self.sku_types and (not disabled) and random.random() < self.initial_inventory_ratio:
            sku = random.choice(self.sku_types)
            quantity = 1
        else:
            sku = ""
            quantity = 0

        return InventoryPosition(
            aisle=aisle,
            row=row,
            column=column,
            level=level,
            sku=sku,
            quantity=quantity,
            is_double_layer=False,
            disabled=disabled,
            # 初始随机库存没有外部到货时刻，统一视为仿真开始前库存。
            features={"_inbound_time": 0.0} if quantity > 0 else None,
        )

    def _create_double_layer_position(self, aisle: int, row: int, column: int, level: int) -> InventoryPosition:
        """创建双层货位"""
        disabled = self._is_disabled(aisle, row, column, level)
        position = InventoryPosition(
            aisle=aisle,
            row=row,
            column=column,
            level=level,
            is_double_layer=True,
            disabled=disabled
        )
        if disabled:
            return position

        # 根据初始库存比例决定是否初始化库存
        rand_val = random.random()

        if rand_val < self.initial_inventory_ratio:
            # 上层放置随机SKU
            position.upper_sku = random.choice(self.sku_types)
            position.upper_quantity = 1

            # 判断是否需要放置下层SKU
            # 如果是solo类型SKU，则不放置下层
            # 如果在sku_pairs中，则放置其配对的SKU作为下层
            if (position.upper_sku not in self.sku_solo and
                random.random() < 0.8):  # 80%概率放置下层
                # 检查是否在配对关系中
                if position.upper_sku in self.sku_pairs:
                    # 放置配对的SKU作为下层
                    position.lower_sku = self.sku_pairs[position.upper_sku]
                    position.lower_quantity = 1
        elif rand_val < self.initial_inventory_ratio * 1.5:
            # 只放置上层SKU
            position.upper_sku = random.choice(self.sku_types)
            position.upper_quantity = 1

        return position

    def _format_position_id(self, aisle: int, row: int, column: int, level: int) -> str:
        """执行 format 货位 id 对应的业务处理。

        Args:
            self: 当前对象实例。
            aisle: 目标巷道编号。
            row: 用于本函数处理的 `row` 参数。
            column: 用于本函数处理的 `column` 参数。
            level: 用于本函数处理的 `level` 参数。

        Returns:
            str: 处理后的结果。
        """
        return f"{aisle:01d}-{row:01d}-{column:02d}-{level:02d}"

    def _is_disabled(self, aisle: int, row: int, column: int, level: int) -> bool:
        """判断disabled相关逻辑。

        Args:
            self: 当前对象实例。
            aisle: 目标巷道编号。
            row: 用于本函数处理的 `row` 参数。
            column: 用于本函数处理的 `column` 参数。
            level: 用于本函数处理的 `level` 参数。

        Returns:
            bool: 判断结果。
        """
        return self._format_position_id(aisle, row, column, level) in self.disabled_position_ids

    def _normalize_disabled_positions(self, disabled_positions: Optional[List[Union[str, Dict[str, int]]]]) -> set:
        """标准化disabled positions相关逻辑。

        Args:
            self: 当前对象实例。
            disabled_positions: 用于本函数处理的 `disabled_positions` 参数。

        Returns:
            set: 处理后的结果。
        """
        ids = set()
        for item in disabled_positions or []:
            if isinstance(item, str):
                parts = item.split("-")
                if len(parts) == 4 and all(p.isdigit() for p in parts):
                    aisle, row, column, level = (int(p) for p in parts)
                    ids.add(self._format_position_id(aisle, row, column, level))
            elif isinstance(item, dict):
                try:
                    aisle = int(item.get("aisle", 0))
                    row = int(item.get("row", 0))
                    column = int(item.get("column", 0))
                    level = int(item.get("level", 0))
                    if aisle and row and column and level:
                        ids.add(self._format_position_id(aisle, row, column, level))
                except Exception:
                    continue
        return ids

    def _normalize_aisle_dimensions(self, aisle_dimensions: Optional[Dict[Union[int, str], Dict[str, int]]]) -> Dict[int, Dict[str, int]]:
        """标准化巷道 dimensions相关逻辑。

        Args:
            self: 当前对象实例。
            aisle_dimensions: 用于本函数处理的 `aisle_dimensions` 参数。

        Returns:
            Dict[int, Dict[str, int]]: 处理后的结果。
        """
        result: Dict[int, Dict[str, int]] = {}
        if not aisle_dimensions:
            return result
        for aisle_raw, dims in aisle_dimensions.items():
            try:
                aisle_id = int(aisle_raw)
            except Exception:
                continue
            if not isinstance(dims, dict):
                continue
            rows = int(dims.get("rows", self.num_rows))
            columns = int(dims.get("columns", self.num_columns))
            levels = int(dims.get("levels", self.num_levels))
            if rows <= 0 or columns <= 0 or levels <= 0:
                continue
            result[aisle_id] = {"rows": rows, "columns": columns, "levels": levels}
        return result

    def _get_aisle_dimensions(self, aisle: int) -> Dict[str, int]:
        """获取巷道 dimensions相关逻辑。

        Args:
            self: 当前对象实例。
            aisle: 目标巷道编号。

        Returns:
            Dict[str, int]: 处理后的结果。
        """
        dims = self.aisle_dimensions.get(aisle, {})
        return {
            "rows": int(dims.get("rows", self.num_rows)),
            "columns": int(dims.get("columns", self.num_columns)),
            "levels": int(dims.get("levels", self.num_levels)),
        }

    def _update_simplified_inventory(self):
        """更新simplified 库存相关逻辑。

        Args:
            self: 当前对象实例。

        Returns:
            None: 通过修改对象或外部状态完成处理。
        """
        """"""
        self.current_inventory = {aisle: {} for aisle in self.aisles}

        for position in self.inventory_positions:
            if position.is_double_layer:
                #
                if position.upper_quantity > 0 and position.upper_sku:
                    self.current_inventory[position.aisle].setdefault(position.upper_sku, 0)
                    self.current_inventory[position.aisle][position.upper_sku] += position.upper_quantity
                if position.lower_quantity > 0 and position.lower_sku:
                    self.current_inventory[position.aisle].setdefault(position.lower_sku, 0)
                    self.current_inventory[position.aisle][position.lower_sku] += position.lower_quantity
            else:
                #
                if not position.is_empty():
                    self.current_inventory[position.aisle].setdefault(position.sku, 0)
                    self.current_inventory[position.aisle][position.sku] += position.quantity

    # ==========================================================================
    # 主函数：库存查询、写入、扣减和快照输出
    # ==========================================================================
    def get_empty_positions(self, aisle: Optional[int] = None) -> List[InventoryPosition]:
        """

        Args:
            aisle: None

        Returns:

        """
        if aisle is None:
            return [p for p in self.inventory_positions if p.is_empty()]
        else:
            return [p for p in self.inventory_positions if p.aisle == aisle and p.is_empty()]


    def get_sku_positions(self, sku: str, aisle: Optional[int] = None,
                         only_available: bool = True) -> List[InventoryPosition]:
        """获取包含指定SKU的货位列表

        Args:
            sku: SKU类型
            aisle: 巷道号，None表示所有巷道
            only_available: 是否只返回有库存的货位

        Returns:
            包含指定SKU的货位列表
        """
        positions = self.sku_position_index.get(sku, [])

        if only_available:
            # 只返回有库存的货位
            filtered = []
            for p in positions:
                if p.is_double_layer:
                    if (p.upper_sku == sku and p.upper_quantity > 0) or \
                       (p.lower_sku == sku and p.lower_quantity > 0):
                        filtered.append(p)
                else:
                    if p.quantity > 0:
                        filtered.append(p)
            positions = filtered

        if aisle is not None:
            positions = [p for p in positions if p.aisle == aisle]

        return positions

    def get_positions_by_features(self, features: dict, feature_keys: List[str],
                                  only_available: bool = True) -> List[InventoryPosition]:
        """获取positions by features相关逻辑。

        Args:
            self: 当前对象实例。
            features: 用于本函数处理的 `features` 参数。
            feature_keys: 用于本函数处理的 `feature_keys` 参数。
            only_available: 用于本函数处理的 `only_available` 参数。

        Returns:
            List[InventoryPosition]: 处理后的结果。
        """
        if not features or not feature_keys:
            return []
        positions = []
        for pos in self.inventory_positions:
            if pos.is_double_layer:
                if pos.upper_quantity > 0:
                    if self._features_match(pos.upper_features, features, feature_keys):
                        positions.append(pos)
                if pos.lower_quantity > 0:
                    if self._features_match(pos.lower_features, features, feature_keys):
                        positions.append(pos)
            else:
                if pos.quantity > 0 and self._features_match(pos.features, features, feature_keys):
                    positions.append(pos)
        if only_available:
            positions = [p for p in positions if not p.reserved and not p.disabled]
        return positions

    @staticmethod
    def _normalize_color(value: Optional[str]) -> Optional[str]:
        """标准化color相关逻辑。

        Args:
            value: 待处理的单个值。

        Returns:
            Optional[str]: 处理后的结果。
        """
        if value is None:
            return None
        aliases = {
            "\u7ea2\u8272": "\u7ea2\u8272",
            "\u7f8e\u7ea2\u8272": "\u7ea2\u8272",
            "\u7ea2": "\u7ea2\u8272",
            "\u7ec6\u7ea2\u8272": "\u7ea2\u8272",
            "\u767d\u8272": "\u767d\u8272",
            "\u767d": "\u767d\u8272",
            "\u9ed1\u8272": "\u9ed1\u8272",
            "\u9ed1": "\u9ed1\u8272",
            "\u7eda\u8272": "\u7ea2\u8272",
            "\u94c1\u767d\u8272": "\u767d\u8272",
            "\u69df\u9ed1\u8272": "\u9ed1\u8272",
        }
        return aliases.get(value, value)

    @classmethod
    def _normalize_features(cls, features: Optional[dict]) -> Optional[dict]:
        """标准化features相关逻辑。

        Args:
            cls: 当前类对象。
            features: 用于本函数处理的 `features` 参数。

        Returns:
            Optional[dict]: 处理后的结果。
        """
        if not features:
            return features
        normalized = dict(features)
        if "color" in normalized:
            normalized["color"] = cls._normalize_color(normalized.get("color"))
        return normalized

    @classmethod
    def _features_match(cls, stored: Optional[dict], target: dict, keys: List[str]) -> bool:
        """执行 features 匹配 对应的业务处理。

        Args:
            cls: 当前类对象。
            stored: 用于本函数处理的 `stored` 参数。
            target: 用于本函数处理的 `target` 参数。
            keys: 用于本函数处理的 `keys` 参数。

        Returns:
            bool: 处理后的结果。
        """
        if not stored:
            return False
        stored_norm = cls._normalize_features(stored)
        target_norm = cls._normalize_features(target)
        if stored_norm is None or target_norm is None:
            return False
        for k in keys:
            if k not in target_norm:
                return False
            if stored_norm.get(k) != target_norm.get(k):
                return False
        return True


    def get_sku_total_quantity(self, sku: Optional[str]) -> int:
        """返回当前仓库中某个SKU的总数量（包含上下层）"""
        if not sku:
            return 0
        total = 0
        for pos in self.sku_position_index.get(sku, []):
            if pos.is_double_layer:
                if pos.upper_sku == sku:
                    total += pos.upper_quantity
                if pos.lower_sku == sku:
                    total += pos.lower_quantity
            else:
                if pos.sku == sku:
                    total += pos.quantity
        return total

    def log_sku_snapshot(self, sku: str):
        """打印指定SKU的总量及分布，便于快速追踪"""
        total = self.get_sku_total_quantity(sku)
        parts = []
        for pos in self.get_sku_positions(sku, aisle=None, only_available=False):
            if pos.is_double_layer:
                if pos.upper_sku == sku and pos.upper_quantity > 0:
                    parts.append(f"{pos.get_position_id()}:upper x{pos.upper_quantity}")
                if pos.lower_sku == sku and pos.lower_quantity > 0:
                    parts.append(f"{pos.get_position_id()}:lower x{pos.lower_quantity}")
            else:
                if pos.sku == sku and pos.quantity > 0:
                    parts.append(f"{pos.get_position_id()} x{pos.quantity}")
        distribution = ", ".join(parts) if parts else "无在库位置"
        print(f"[INFO][SKU {sku}] 总量 {total}，分布: {distribution}")

    def add_inventory(
        self,
        position: InventoryPosition,
        sku: str,
        quantity: int = 1,
        layer: Optional[str] = None,
        features: Optional[dict] = None,
        in_line: Optional[object] = None,
        out_line: Optional[object] = None,
        inbound_time: Optional[Any] = None,
    ):
        """
        在指定货位增加库存

        Args:
            position: 货位对象
            sku: SKU类型
            quantity: 数量
            layer: 层位 ('upper' 或 'lower' 或 None)
            features: 与库存绑定的属性字典；函数会将 FIFO 时间写入其中的
                ``_inbound_time``，使时间随具体 SKU 而非货位保存。
            in_line: 入库口标识
            out_line: 关联出库口标识
            inbound_time: 可选实际入库时间，支持 Unix 秒或 ISO 8601 字符串；未传时
                使用当前时间，并写入 ``features["_inbound_time"]``。
        """
        # 通过 position_map 获取实际的位置对象
        position_id = position.get_position_id()
        actual_position = self.position_map.get(position_id)

        # 跳过无效 sku
        if sku is None:
            return

        if actual_position is None:
            raise ValueError(f"位置 {position_id} 不存在于 position_map 中")

        features_copy = dict(features) if isinstance(features, dict) else {}
        # FIFO 元数据属于具体 SKU。恢复已扣减库存时保留原时间；正常入库或显式
        # 提供时间时覆盖为本次解析结果，避免同一货位换 SKU 后沿用旧库存年龄。
        if inbound_time is not None or "_inbound_time" not in features_copy:
            features_copy["_inbound_time"] = self.resolve_inbound_time(inbound_time)

        if actual_position.is_double_layer:
            # 双层货位
            if layer == 'upper':
                if actual_position.upper_quantity > 0:
                    raise ValueError(f"位置 {actual_position.get_position_id()} 的上层已经有货物")
                actual_position.upper_sku = sku
                actual_position.upper_quantity = quantity
                if features_copy is not None:
                    actual_position.upper_features = features_copy
                    if "color" not in (actual_position.upper_features or {}):
                        print(f"[ERROR] Inbound features missing color at {position_id}")
                actual_position.upper_in_line = in_line
                actual_position.upper_out_line = out_line
            elif layer == 'lower':
                if actual_position.lower_quantity > 0:
                    raise ValueError(f"位置 {actual_position.get_position_id()} 的下层已经有货物")
                actual_position.lower_sku = sku
                actual_position.lower_quantity = quantity
                if features_copy is not None:
                    actual_position.lower_features = features_copy
                    if "color" not in (actual_position.lower_features or {}):
                        print(f"[ERROR] Inbound features missing color at {position_id}")
                actual_position.lower_in_line = in_line
                actual_position.lower_out_line = out_line
            else:
                # 自动分配到空的层
                if actual_position.upper_quantity == 0:
                    actual_position.upper_sku = sku
                    actual_position.upper_quantity = quantity
                    if features_copy is not None:
                        actual_position.upper_features = features_copy
                        if "color" not in (actual_position.upper_features or {}):
                            print(f"[ERROR] Inbound features missing color at {position_id}")
                    actual_position.upper_in_line = in_line
                    actual_position.upper_out_line = out_line
                elif actual_position.lower_quantity == 0:
                    actual_position.lower_sku = sku
                    actual_position.lower_quantity = quantity
                    if features_copy is not None:
                        actual_position.lower_features = features_copy
                        if "color" not in (actual_position.lower_features or {}):
                            print(f"[ERROR] Inbound features missing color at {position_id}")
                    actual_position.lower_in_line = in_line
                    actual_position.lower_out_line = out_line
                else:
                    raise ValueError(f"位置 {actual_position.get_position_id()} 的上下层都已有货物")
        else:
            # 单层货位
            if not actual_position.is_empty():
                raise ValueError(f"位置 {actual_position.get_position_id()} 已有货物")
            actual_position.sku = sku
            actual_position.quantity = quantity
            if features_copy is not None:
                actual_position.features = features_copy
                if "color" not in (actual_position.features or {}):
                    print(f"[ERROR] Inbound features missing color at {position_id}")
            actual_position.in_line = in_line
            actual_position.out_line = out_line

        # 更新SKU索引
        if sku not in self.sku_position_index:
            self.sku_position_index[sku] = []
        if actual_position not in self.sku_position_index[sku]:
            self.sku_position_index[sku].append(actual_position)

        total_sku_qty = self.get_sku_total_quantity(sku)
        print(f"[INFO] 位置 {position_id} 增加 {sku} {quantity} {layer} 库存，总计 {total_sku_qty}")
        if sku in self.sku_watchlist:
            self.log_sku_snapshot(sku)

        self._update_simplified_inventory()

    def remove_inventory(self, position: InventoryPosition, sku: Optional[str] = None, quantity: int = 1):
        """
        从指定货位移除库存

        Args:
            position: 货位对象
            sku: SKU类型（对于双层货位必须指定）
            quantity: 数量
        """
        # 通过 position_map 获取实际的位置对象
        position_id = position.get_position_id()
        actual_position = self.position_map.get(position_id)

        if actual_position is None:
            raise ValueError(f"位置 {position_id} 不存在于 position_map 中")

        if actual_position.is_double_layer:
            # 双层货位需要指定SKU
            if sku is None:
                raise ValueError("双层货位需要指定SKU")

            # 检查并减少相应层的库存（优先从下层扣减，避免上下层同SKU时总是取上层）
            if actual_position.lower_sku == sku and actual_position.lower_quantity >= quantity:
                actual_position.lower_quantity -= quantity
                # 如果该层库存为0，清空SKU信息
                if actual_position.lower_quantity == 0:
                    actual_position.lower_sku = None
                    actual_position.lower_features = None
                    actual_position.lower_in_line = None
                    actual_position.lower_out_line = None
            elif actual_position.upper_sku == sku and actual_position.upper_quantity >= quantity:
                actual_position.upper_quantity -= quantity
                # 如果该层库存为0，清空SKU信息
                if actual_position.upper_quantity == 0:
                    actual_position.upper_sku = None
                    actual_position.upper_features = None
                    actual_position.upper_in_line = None
                    actual_position.upper_out_line = None
            else:
                raise ValueError(f"位置 {actual_position.get_position_id()} 没有足够的 {sku} 库存")

            # 检查该货位是否还包含此SKU，如果不包含则从索引中移除
            if actual_position.upper_sku != sku and actual_position.lower_sku != sku:
                if sku in self.sku_position_index and actual_position in self.sku_position_index[sku]:
                    self.sku_position_index[sku].remove(actual_position)
        else:
            # 单层货位
            if actual_position.quantity < quantity:
                raise ValueError(f"位置 {actual_position.get_position_id()} 没有足够的库存")

            actual_position.quantity -= quantity

            # 如果库存为0，清空SKU信息并从索引中移除
            if actual_position.quantity == 0:
                removed_sku = actual_position.sku
                actual_position.sku = ""
                actual_position.features = None
                actual_position.in_line = None
                actual_position.out_line = None
                if removed_sku in self.sku_position_index and actual_position in self.sku_position_index[removed_sku]:
                    self.sku_position_index[removed_sku].remove(actual_position)

        self._update_simplified_inventory()

    def get_inventory_snapshot(self) -> Dict:
        """获取库存快照"""
        sku_distribution = {}
        for aisle in self.aisles:
            for sku, qty in self.current_inventory[aisle].items():
                sku_distribution[sku] = sku_distribution.get(sku, 0) + qty
        return {
            'current_inventory': copy.deepcopy(self.current_inventory),
            'total_occupied': sum(1 for p in self.inventory_positions if not p.is_empty()),
            'total_beams': sum(p.quantity for p in self.inventory_positions),
            'sku_distribution': sku_distribution,
        }

    def print_distribution(self):
        """打印库存分布情况"""
        snapshot = self.get_inventory_snapshot()

        print(f"[INFO] 库存分布情况:")
        print(f"  总占用货位: {snapshot['total_occupied']}/{self.total_positions} "
              f"({snapshot['total_occupied']/self.total_positions*100:.1f}%)")
        print(f"  总梁数: {snapshot['total_beams']}/{self.max_beams} "
              f"({snapshot['total_beams']/self.max_beams*100:.1f}%)")

        for sku, total_qty in snapshot['sku_distribution'].items():
            if total_qty > 0:
                aisle_dist = {}
                for aisle in self.aisles:
                    qty = self.current_inventory[aisle].get(sku, 0)
                    if qty > 0:
                        aisle_dist[aisle] = qty

                dist_str = ", ".join([f"{a}:{q}" for a, q in aisle_dist.items()])
                print(f"  {sku}: {total_qty} ({dist_str})")
