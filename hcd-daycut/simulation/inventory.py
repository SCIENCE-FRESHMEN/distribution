"""

"""

import random
import copy
from typing import List, Dict, Optional, Union, Any
from .position import InventoryPosition


class InventoryManager:
    """"""
    
    def __init__(self, num_aisles: int = 5, num_rows: int = 2, num_columns: int = 3,
                 num_levels: int = 18, total_positions: int = 1000, max_beams: int = 980,
                 sku_types: List[str] = None, initial_inventory_ratio: float = 0.3,
                 use_double_layer: bool = True,
                 sku_pairs: dict = None,
                 sku_solo: list = None,
                 disabled_positions: Optional[List[Union[str, Dict[str, int]]]] = None,
                 match_fields: Optional[List[str]] = None):
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
        self.match_fields = list(match_fields or [])
        
        # SKU配置
        self.sku_types = sku_types or ['A1', 'A2', 'B1', 'B2', 'C1', 'C2']
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
    
    def initialize(self):
        """初始化仓库"""
        mode_str = "Double Shelf Warehouse" if self.use_double_layer else "Single Shelf Warehouse"
        print(f"[INIT] {mode_str}...")

        # 若启用属性匹配，随机初始化会缺少属性，导致出库匹配失败
        if self.match_fields and self.initial_inventory_ratio > 0:
            print("[WARN] match_fields 已启用，禁用随机初始库存以避免属性缺失")
            self.initial_inventory_ratio = 0.0
        
        self.inventory_positions = []
        self.position_map = {}
        self.sku_position_index = {sku: [] for sku in self.sku_types}
        
        # 
        positions_created = 0
        for aisle in range(1, self.num_aisles + 1):
            for row in range(1, self.num_rows + 1):
                for column in range(1, self.num_columns + 1):
                    for level in range(1, self.num_levels + 1):
                        if positions_created >= self.total_positions:
                            break
                        
                        if self.use_double_layer:
                            # 
                            position = self._create_double_layer_position(aisle, row, column, level)
                        else:
                            # 
                            position = self._create_single_layer_position(aisle, row, column, level)
                        
                        self.inventory_positions.append(position)
                        self.position_map[position.get_position_id()] = position
                        
                        # SKU
                        if not position.is_empty():
                            for sku in position.get_available_skus():
                                if sku and sku not in self.sku_position_index:
                                    self.sku_position_index[sku] = []
                                if position not in self.sku_position_index[sku]:
                                    self.sku_position_index[sku].append(position)
                        
                        positions_created += 1
                        if positions_created >= self.total_positions:
                            break
                    if positions_created >= self.total_positions:
                        break
                if positions_created >= self.total_positions:
                    break
            if positions_created >= self.total_positions:
                break
        
        # 
        self._update_simplified_inventory()

    def _extract_sku_attrs(self, record: dict, idx: int) -> Dict[str, Any]:
        attrs: Dict[str, Any] = {}
        if not record or not self.match_fields:
            return attrs
        for field in self.match_fields:
            values = record.get(field)
            if isinstance(values, list) and idx < len(values):
                attrs[field] = values[idx]
        return attrs
    
    def initialize_from_inbound_tasks(self, inbound_records, aisles, inbound_position_allocator=None, inbound_aisle_allocator=None, initial_inventory_count=250):
        """通过读取出入库任务记录来进行库存初始化，位置随机分配"""
        print(f"[INFO] 通过前{initial_inventory_count}组入库任务初始化库存...")
        
        # 获取前N组入库任务
        inbound_tasks = inbound_records[:initial_inventory_count] if len(inbound_records) >= initial_inventory_count else inbound_records

        for record in inbound_tasks:
            try:
                sku_list = record.get('skus', []) if isinstance(record, dict) else record

                # 保留槽位(side)信息，兼容 [None, sku] / [sku, None]
                skus = []
                for idx_slot, sku in enumerate(sku_list):
                    side = 'A' if idx_slot == 0 else 'B'
                    sku_entry = {'skuId': sku, 'quantity': 1, 'side': side}
                    sku_entry.update(self._extract_sku_attrs(record, idx_slot))
                    skus.append(sku_entry)
                non_null_skus = [s for s in skus if s['skuId'] is not None]

                task_info = type('TaskData', (), {'skus': skus, 'assigned_aisle': None})()

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

                for idx, sku in enumerate(skus):
                    sku_id = sku['skuId']
                    sku_attrs = {k: sku.get(k) for k in self.match_fields} if self.match_fields else {}
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
                        self.add_inventory(sku=sku_id, quantity=1, position=position, layer=layer, attrs=sku_attrs)
                    except Exception as e:
                        print(f"[ERROR] 添加库存失败: {position.get_position_id()} {sku_id}, 错误: {str(e)}")
                        continue

            except Exception as e:
                print(f"[ERROR] 处理入库任务 {sku} 时发生异常: {e}")
                continue
        
        # 更新简化库存
        self._update_simplified_inventory()
        print(f"[INFO] 基于前 {initial_inventory_count} 组入库任务的库存初始化完成")
    
    def _create_single_layer_position(self, aisle: int, row: int, column: int, level: int) -> InventoryPosition:
        """"""
        disabled = self._is_disabled(aisle, row, column, level)
        # 
        if (not disabled) and random.random() < self.initial_inventory_ratio:
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
            disabled=disabled
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
        return f"{aisle:01d}-{row:01d}-{column:02d}-{level:02d}"

    def _is_disabled(self, aisle: int, row: int, column: int, level: int) -> bool:
        return self._format_position_id(aisle, row, column, level) in self.disabled_position_ids

    def _normalize_disabled_positions(self, disabled_positions: Optional[List[Union[str, Dict[str, int]]]]) -> set:
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
    
    def _update_simplified_inventory(self):
        """"""
        self.current_inventory = {aisle: {sku: 0 for sku in self.sku_types} 
                                 for aisle in self.aisles}
        
        for position in self.inventory_positions:
            if position.is_double_layer:
                # 
                if position.upper_quantity > 0 and position.upper_sku:
                    self.current_inventory[position.aisle][position.upper_sku] += position.upper_quantity
                if position.lower_quantity > 0 and position.lower_sku:
                    self.current_inventory[position.aisle][position.lower_sku] += position.lower_quantity
            else:
                # 
                if not position.is_empty():
                    self.current_inventory[position.aisle][position.sku] += position.quantity
    
    def get_empty_positions(self, aisle: int = None) -> List[InventoryPosition]:
        """
        
        Args:
            aisle: None
            
        Returns:
            
        """
        if aisle is None:
            return [p for p in self.inventory_positions if p.is_empty()]
        else:
            return [p for p in self.inventory_positions if p.aisle == aisle and p.is_empty()]

    
    def get_sku_positions(self, sku: str, aisle: int = None, 
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
    
    def add_inventory(self, position: InventoryPosition, sku: str, quantity: int = 1,
                      layer: Optional[str] = None, attrs: Optional[Dict[str, Any]] = None):
        """
        在指定货位增加库存
        
        Args:
            position: 货位对象
            sku: SKU类型
            quantity: 数量
            layer: 层位 ('upper' 或 'lower' 或 None)
        """
        # 通过 position_map 获取实际的位置对象
        position_id = position.get_position_id()
        actual_position = self.position_map.get(position_id)

        # 跳过无效 sku
        if sku is None:
            return
        
        if actual_position is None:
            raise ValueError(f"位置 {position_id} 不存在于 position_map 中")
        
        if actual_position.is_double_layer:
            # 双层货位
            if layer == 'upper':
                if actual_position.upper_quantity > 0:
                    raise ValueError(f"位置 {actual_position.get_position_id()} 的上层已经有货物")
                actual_position.upper_sku = sku
                actual_position.upper_quantity = quantity
                actual_position.upper_attrs = attrs or {}
            elif layer == 'lower':
                if actual_position.lower_quantity > 0:
                    raise ValueError(f"位置 {actual_position.get_position_id()} 的下层已经有货物")
                actual_position.lower_sku = sku
                actual_position.lower_quantity = quantity
                actual_position.lower_attrs = attrs or {}
            else:
                # 自动分配到空的层
                if actual_position.upper_quantity == 0:
                    actual_position.upper_sku = sku
                    actual_position.upper_quantity = quantity
                    actual_position.upper_attrs = attrs or {}
                elif actual_position.lower_quantity == 0:
                    actual_position.lower_sku = sku
                    actual_position.lower_quantity = quantity
                    actual_position.lower_attrs = attrs or {}
                else:
                    raise ValueError(f"位置 {actual_position.get_position_id()} 的上下层都已有货物")
        else:
            # 单层货位
            if not actual_position.is_empty():
                raise ValueError(f"位置 {actual_position.get_position_id()} 已有货物")
            actual_position.sku = sku
            actual_position.quantity = quantity
            actual_position.sku_attrs = attrs or {}
        
        # 更新SKU索引
        if sku not in self.sku_position_index:
            self.sku_position_index[sku] = []
        if actual_position not in self.sku_position_index[sku]:
            self.sku_position_index[sku].append(actual_position)

        total_sku_qty = self.get_sku_total_quantity(sku)
        attrs_info = attrs if attrs is not None else {}
        print(f"[INFO] 位置 {position_id} 增加 {sku} {quantity} {layer} 库存，总计 {total_sku_qty} attrs={attrs_info}")
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
                    actual_position.lower_attrs = {}
            elif actual_position.upper_sku == sku and actual_position.upper_quantity >= quantity:
                actual_position.upper_quantity -= quantity
                # 如果该层库存为0，清空SKU信息
                if actual_position.upper_quantity == 0:
                    actual_position.upper_sku = None
                    actual_position.upper_attrs = {}
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
                actual_position.sku_attrs = {}
                if removed_sku in self.sku_position_index and actual_position in self.sku_position_index[removed_sku]:
                    self.sku_position_index[removed_sku].remove(actual_position)
        
        self._update_simplified_inventory()
    
    def get_inventory_snapshot(self) -> Dict:
        """获取库存快照"""
        return {
            'current_inventory': copy.deepcopy(self.current_inventory),
            'total_occupied': sum(1 for p in self.inventory_positions if not p.is_empty()),
            'total_beams': sum(p.quantity for p in self.inventory_positions),
            'sku_distribution': {
                sku: sum(self.current_inventory[aisle][sku] for aisle in self.aisles)
                for sku in self.sku_types
            }
        }
    
    def print_distribution(self):
        """打印库存分布情况"""
        snapshot = self.get_inventory_snapshot()
        
        print(f"[INFO] 库存分布情况:")
        print(f"  总占用货位: {snapshot['total_occupied']}/{self.total_positions} "
              f"({snapshot['total_occupied']/self.total_positions*100:.1f}%)")
        # print(f"  总梁数: {snapshot['total_beams']}/{self.max_beams} "
        #       f"({snapshot['total_beams']/self.max_beams*100:.1f}%)")
        
        for sku in self.sku_types:
            total_qty = snapshot['sku_distribution'][sku]
            if total_qty > 0:
                aisle_dist = {}
                for aisle in self.aisles:
                    qty = self.current_inventory[aisle][sku]
                    if qty > 0:
                        aisle_dist[aisle] = qty
                
                dist_str = ", ".join([f"{a}:{q}" for a, q in aisle_dist.items()])
                print(f"  {sku}: {total_qty} ({dist_str})")

    def get_pairing_stats(self) -> Dict[str, Union[int, float, Dict[int, int]]]:
        """
        统计当前双层货位的配对情况：
        - matched_pairs: 上下层 SKU 与 sku_pairs 成功配对的货位数量
        - total_pairs: 上下层均有货的双层货位数量
        - match_rate: matched_pairs / total_pairs（无总数时为 0）
        - solo_upper: 上层为 sku_solo 且下层为空的货位数量
        - unmatched_pairs: total_pairs - matched_pairs
        - double_slots: 双层货位总数（无论是否有货）
        - filled_slots: 至少一层有货的双层货位
        - match_rate_all_slots: matched_pairs / double_slots（无总数时为 0）
        - potential_pairs: 当前库存中从数量角度可以额外配出的组数
                        （同一批梁里，理论可配总数减去已配成功数）
        - max_possible_pairs: 在当前这批梁的 SKU 分布下，理论最大可配对组数
                            （对同一批梁是一个固定常数）
        - matched_pairs_by_aisle: 每个巷道中已配对成功的货位数量
        - double_slots_by_aisle: 每个巷道中的双层货位总数
        - total_goods: 总货物数量（总梁数）
        - goods_by_aisle: 每个巷道中的货物数量
        - beam_match_rate:  
        - beam_match_rate_including_solo: 
        - paired_beams: 
        - paired_beams_including_solo: paired_beams + solo_beams
        """

        matched_pairs = 0
        total_pairs = 0
        solo_upper = 0
        double_slots = 0
        filled_slots = 0
        sku_counts: Dict[str, int] = {}
        total_goods = 0
        goods_by_aisle = {aisle: 0 for aisle in self.aisles}

        matched_pairs_by_aisle = {aisle: 0 for aisle in self.aisles}
        double_slots_by_aisle = {aisle: 0 for aisle in self.aisles}

        # ------- 第一遍遍历：统计库存与已配对情况 -------
        for pos in self.inventory_positions:
            if not pos.is_double_layer:
                continue

            double_slots += 1
            double_slots_by_aisle[pos.aisle] += 1

            upper_filled = pos.upper_quantity > 0 and pos.upper_sku
            lower_filled = pos.lower_quantity > 0 and pos.lower_sku

            # 统计总货物（总梁数）及巷道货物
            if upper_filled:
                total_goods += 1
                goods_by_aisle[pos.aisle] += 1
            if lower_filled:
                total_goods += 1
                goods_by_aisle[pos.aisle] += 1

            if upper_filled or lower_filled:
                filled_slots += 1

            # 上下层都有货 → 一对“占用货位”
            if upper_filled and lower_filled:
                total_pairs += 1
                if (
                    pos.upper_sku in self.sku_pairs
                    and self.sku_pairs[pos.upper_sku] == pos.lower_sku
                ):
                    matched_pairs += 1
                    matched_pairs_by_aisle[pos.aisle] += 1

            # solo 统计：上层是 solo SKU 且下层为空
            if upper_filled and not lower_filled and pos.upper_sku in self.sku_solo:
                solo_upper += 1

            # 统计各 SKU 的总数量（梁数）
            if upper_filled and pos.upper_sku is not None:
                sku_counts[pos.upper_sku] = sku_counts.get(pos.upper_sku, 0) + 1
            if lower_filled and pos.lower_sku is not None:
                sku_counts[pos.lower_sku] = sku_counts.get(pos.lower_sku, 0) + 1

        # ------- 第二步：根据 SKU 分布计算理论最大可配对数（与策略无关的常数） -------
        remaining = dict(sku_counts)
        max_possible_pairs = 0
        processed = set()

        for sku, pair_sku in self.sku_pairs.items():
            if (sku, pair_sku) in processed or (pair_sku, sku) in processed:
                continue
            processed.add((sku, pair_sku))

            if sku == pair_sku:
                # 自配对，比如 A:A
                count = remaining.get(sku, 0)
                pairs = count // 2              # 一组要用掉 2 根
                max_possible_pairs += pairs
                remaining[sku] = count - 2 * pairs
            else:
                # 普通 A:B
                count_a = remaining.get(sku, 0)
                count_b = remaining.get(pair_sku, 0)
                pairs = min(count_a, count_b)
                max_possible_pairs += pairs
                remaining[sku] = count_a - pairs
                remaining[pair_sku] = count_b - pairs

        # ------- 第三步：潜在配对 = 理论最大可配对数 - 已经配对成功的数量 -------

        potential_pairs = max_possible_pairs - matched_pairs

        match_rate = matched_pairs / total_pairs if total_pairs else 0.0
        match_rate_all_slots = matched_pairs / double_slots if double_slots else 0.0
        unmatched_pairs = total_pairs - matched_pairs
        paired_beams = matched_pairs * 2
        paired_beams_including_solo = paired_beams + solo_upper
        beam_match_rate = paired_beams / total_goods if total_goods else 0.0
        beam_match_rate_including_solo = (
            paired_beams_including_solo / total_goods if total_goods else 0.0
        )

        return {
            "matched_pairs": matched_pairs,
            "total_pairs": total_pairs,
            "match_rate": match_rate,
            "solo_upper": solo_upper,
            "unmatched_pairs": unmatched_pairs,
            "double_slots": double_slots,
            "filled_slots": filled_slots,
            "match_rate_all_slots": match_rate_all_slots,
            "potential_pairs": potential_pairs,
            "max_possible_pairs": max_possible_pairs,
            "matched_pairs_by_aisle": matched_pairs_by_aisle,
            "double_slots_by_aisle": double_slots_by_aisle,
            "total_goods": total_goods,
            "goods_by_aisle": goods_by_aisle,
            "paired_beams": paired_beams,
            "paired_beams_including_solo": paired_beams_including_solo,
            "beam_match_rate": beam_match_rate,
            "beam_match_rate_including_solo": beam_match_rate_including_solo,
        }
