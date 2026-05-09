"""
Baseline Strategy
"""

from typing import List, Dict, Optional, Tuple
from copy import deepcopy
import sys
sys.path.append('..')
from simulation.position import InventoryPosition
from simulation.task_data import TaskData
from simulation.config_bulider.sku_config_builder import SKUConfigBuilder

class BaselineAisleAllocator:
    """
    完全复刻 Java AllocateService 逻辑的巷道分配策略
    """

    def __init__(self, warehouse_core):
        self.warehouse_core = warehouse_core
        self.aisles = warehouse_core.aisles
        self.match_fields = getattr(warehouse_core, 'match_fields', [])
        self.used_roadway_list = [] # 需要外部维护或每次从core获取

        # 配置加载
        self.config_data = SKUConfigBuilder.load_json("simulation/data/sku_config.json")
        self.sku_pairs = self.config_data["sku_pairs"]
        self.sku_solo = self.config_data["sku_solo"]
        self.position_allocator = BaselinePositionAllocator(warehouse_core)

    def _extract_attrs(self, sku_entry: Optional[Dict]) -> Dict:
        if not self.match_fields or not isinstance(sku_entry, dict):
            return {}
        return {k: sku_entry.get(k) for k in self.match_fields}

    def _get_sku_attrs(self, skus_data: List[Dict], sku_id: str) -> Dict:
        for entry in skus_data or []:
            if isinstance(entry, dict) and entry.get('skuId') == sku_id:
                return self._extract_attrs(entry)
        return {}

    def _attrs_equal(self, attrs_a: Optional[Dict], attrs_b: Optional[Dict]) -> bool:
        if not self.match_fields:
            return True
        if attrs_a is None or attrs_b is None:
            return False
        for field in self.match_fields:
            if attrs_a.get(field) != attrs_b.get(field):
                return False
        return True

    def _build_task_for_simulation(self, skus_data: List[Dict], aisle: int,
                                   task_id: Optional[str] = None) -> TaskData:
        return TaskData(
            task_id=task_id or f"SIM_INBOUND_{aisle}",
            task_type="INBOUND",
            skus=deepcopy(skus_data),
            assigned_aisle=aisle,
        )

    def _resolve_inbound_layer(self, pos: InventoryPosition,
                               allocated_positions: List[InventoryPosition],
                               sku_count: int,
                               non_null_idx: int) -> Optional[str]:
        if pos.is_double_layer and sku_count > 1:
            if len(allocated_positions) == 2:
                if allocated_positions[0] == allocated_positions[1]:
                    if pos.row == 1:
                        return 'upper' if non_null_idx == 0 else 'lower'
                    if pos.row == 2:
                        return 'lower' if non_null_idx == 0 else 'upper'
                    return None
                if ((pos.upper_sku is not None and pos.upper_sku != '') and
                    (pos.lower_sku is None or pos.lower_sku == '')):
                    return 'lower'
                if ((pos.upper_sku is None or pos.upper_sku == '') and
                    (pos.lower_sku is None or pos.lower_sku == '')):
                    return 'upper'
                return None
            return 'upper' if non_null_idx == 0 else 'lower'

        if pos.is_double_layer and sku_count == 1:
            if pos.upper_quantity == 0:
                return 'upper'
            if pos.lower_quantity == 0:
                return 'lower'
            return None

        return None

    def _apply_simulated_inbound(self, allocated_positions: List[InventoryPosition],
                                 skus_data: List[Dict]) -> bool:
        sku_entries = [s for s in skus_data if isinstance(s, dict)]
        sku_ids = [s.get('skuId') for s in sku_entries if s.get('skuId') is not None]
        non_null_idx = 0

        for sku_entry in sku_entries:
            sku_id = sku_entry.get('skuId')
            if sku_id is None:
                continue

            pos = allocated_positions[min(non_null_idx, len(allocated_positions) - 1)]
            layer = self._resolve_inbound_layer(pos, allocated_positions, len(sku_ids), non_null_idx)

            if pos.is_double_layer:
                if layer == 'upper':
                    if pos.upper_quantity > 0:
                        return False
                    pos.upper_sku = sku_id
                    pos.upper_quantity = 1
                    pos.upper_attrs = self._extract_attrs(sku_entry)
                elif layer == 'lower':
                    if pos.lower_quantity > 0:
                        return False
                    pos.lower_sku = sku_id
                    pos.lower_quantity = 1
                    pos.lower_attrs = self._extract_attrs(sku_entry)
                else:
                    return False
            else:
                if not pos.is_empty():
                    return False
                pos.sku = sku_id
                pos.quantity = 1
                pos.sku_attrs = self._extract_attrs(sku_entry)

            non_null_idx += 1

        return True

    def _can_accept_task_in_aisle(self, aisle: int,
                                  inventory_positions: List[InventoryPosition],
                                  skus_data: List[Dict],
                                  current_task_id: Optional[str] = None) -> bool:
        aisle_positions = [deepcopy(p) for p in inventory_positions if p.aisle == aisle]
        if not aisle_positions:
            return False

        pending_by_aisle = getattr(self.warehouse_core, 'pending_inbound_by_aisle', {}) or {}
        pending_tasks = list(pending_by_aisle.get(aisle, []))

        for pending_task in pending_tasks:
            if current_task_id and getattr(pending_task, 'task_id', None) == current_task_id:
                continue
            sim_task = self._build_task_for_simulation(
                getattr(pending_task, 'skus', []) or [],
                aisle,
                getattr(pending_task, 'task_id', None),
            )
            allocated = self.position_allocator.allocate(aisle_positions, sim_task)
            if not allocated or not self._apply_simulated_inbound(allocated, sim_task.skus):
                return False

        current_task = self._build_task_for_simulation(skus_data, aisle, current_task_id)
        allocated = self.position_allocator.allocate(aisle_positions, current_task)
        if not allocated:
            return False
        return self._apply_simulated_inbound(allocated, current_task.skus)

    def allocate(self, task_info: TaskData,
                inventory_positions: List[InventoryPosition]) -> Optional[int]:
        """
        对应 Java: allocateLocation 方法
        """
        try:
            task_id = getattr(task_info, 'task_id', None)
            if task_id is None and isinstance(task_info, dict):
                task_id = task_info.get('task_id') or task_info.get('id')
            # 1. 解析任务 SKU
            if hasattr(task_info, 'skus') and not isinstance(task_info, dict):
                skus_data = task_info.skus
            elif isinstance(task_info, dict) and 'skus' in task_info:
                skus_data = task_info['skus']
            else:
                print(f"[WARN][allocator] BaselineAisleAllocator: no skus, return None task={task_id}")
                return None
            
            if not skus_data:
                print(f"[WARN][allocator] BaselineAisleAllocator: empty skus, return None task={task_id}")
                return None

            # 提取SKU槽位信息，保留null值的位置信息
            sku_slots = [sku.get('skuId') if isinstance(sku, dict) else None for sku in skus_data]
            sku_ids = [sku for sku in sku_slots if sku is not None]

            result = None
            # 2. 分支处理
            # 对应 Java: if (beamCodeA != null && beamCodeB != null)
            if len(sku_ids) == 2:
                sku1, sku2 = sku_ids       
                # 模拟 Java: isSingleMatch 判断

                attrs1 = self._get_sku_attrs(skus_data, sku1)
                attrs2 = self._get_sku_attrs(skus_data, sku2)
                is_match = self._is_match_sku(sku1, sku2) and self._attrs_equal(attrs1, attrs2)
                
                if is_match:
                    # 对应 allocateEmptyLocationForMatchedBeam
                    result = self._allocate_empty_location_for_matched_beam(sku1, sku2, inventory_positions, skus_data, task_id)
                else:
                    # 对应 allocateForNotMatched
                    result = self._allocate_for_not_matched(sku1, sku2, inventory_positions, attrs1, attrs2, skus_data, task_id)

            # 对应 Java: else (单梁分配)
            elif len(sku_ids) == 1:
                preferred_side = None
                # 检查原始SKU槽位信息，确定preferred_side
                if len(sku_slots) == 2:
                    # 如果原始数据中有两个槽位，根据哪个槽位为null确定preferred_side
                    if sku_slots[0] is None and sku_slots[1] is not None:
                        preferred_side = 'B'  # SKU在B侧(Side B)
                    elif sku_slots[0] is not None and sku_slots[1] is None:
                        preferred_side = 'A'  # SKU在A侧(Side A)
                # 调用改进的单梁分配方法
                single_attrs = self._get_sku_attrs(skus_data, sku_ids[0])
                result = self._allocate_single_for_beam_code(sku_ids[0], inventory_positions, preferred_side, single_attrs, skus_data, task_id)

            if result is None:
                print(f"[WARN][allocator] BaselineAisleAllocator: no aisle allocated task={task_id} skus={sku_ids}")
            return result

        except Exception as e:
            print(f"BaselineAisleAllocator分配失败: {e}")
            import traceback
            traceback.print_exc()
            return None

    def _is_single_layer_sku(self, sku: str) -> bool:
        """
        判断是否为单层梁 SKU。
        逻辑：在 sku_solo 列表中，或者在 sku_pairs 中但配对是自己，或者不在配对表中(视为独立)
        """
        # 如果明确在 solo 列表
        if sku in self.sku_solo:
            return True
        
        # 如果在配对列表
        if sku in self.sku_pairs:
            # 配对是自己 -> 单层梁
            if self.sku_pairs[sku] == sku:
                return True
            # 配对是别人 -> 双层梁 (Dual Beam)
            return False
            
        # 默认情况: 如果既不在pairs也不在solo，通常视为单层梁(独立)
        return True

    def _is_match_sku(self, sku1: str, sku2: str) -> bool:
        """辅助判断：是否配对"""
        # 严格配对：sku1 的 mate 是 sku2
        return self.sku_pairs.get(sku1) == sku2

    def _roadway_rank(self, empty_location_list: List[InventoryPosition],
                     sku_location_list: List[InventoryPosition],
                     available_roadway_list: List[int],
                     used_roadway_list: List[int]) -> List[int]:
        """
        对应 Java: roadwayRank 方法
        完全复刻排序逻辑：Used -> BOM Asc -> Empty Desc
        """
        # 1. sku 计数
        seen_ids = set()
        unique_sku_locs = []
        for loc in sku_location_list:
            pid = self._get_position_id(loc)
            if pid not in seen_ids:
                seen_ids.add(pid)
                unique_sku_locs.append(loc)

        bom_count_map = {r: 0 for r in available_roadway_list}
        for loc in unique_sku_locs:
            if loc.aisle in bom_count_map:
                bom_count_map[loc.aisle] += 1
        
        # 2. Empty 计数
        empty_count_map = {r: 0 for r in available_roadway_list}
        for loc in empty_location_list:
            if loc.aisle in empty_count_map:
                empty_count_map[loc.aisle] += 1

        def rank_key(roadway_id):
            is_used = roadway_id in used_roadway_list 
            bom_count = bom_count_map.get(roadway_id, 0)
            empty_count = empty_count_map.get(roadway_id, 0)
            
            # Python sort 是升序
            # 1. used: False(0) < True(1) -> 未使用优先
            # 2. bom: 小 < 大 -> 分散库存
            # 3. empty: -大 < -小 -> 空位多的优先
            return (is_used, bom_count, -empty_count)

        return sorted(available_roadway_list, key=rank_key)

    def _allocate_for_not_matched(self, sku1: str, sku2: str, inventory_positions: List[InventoryPosition],
                                  attrs1: Optional[Dict] = None, attrs2: Optional[Dict] = None,
                                  skus_data: Optional[List[Dict]] = None, task_id: Optional[str] = None) -> Optional[int]:
        """
        对应 Java: allocateForNotMatched
        """
        empty_location_list = [p for p in inventory_positions if p.is_empty()]
        
        # 模拟 findBomLocationList
        sku_location_list = []
        for p in inventory_positions:
            if p.is_empty(): continue
            av_skus = p.get_available_skus()
            
            if sku1 in av_skus:
                sku_location_list.append(p)

        ranked_roadway_list = self._roadway_rank(
            empty_location_list, sku_location_list, self.aisles, self.used_roadway_list
        )

        best_result_list = []   # Low + Low
        better_result_list = [] # Mixed
        good_result_list = []   # High + High

        for roadway_id in ranked_roadway_list:
            if not self._can_accept_task_in_aisle(roadway_id, inventory_positions, skus_data or [], task_id):
                continue
            # 尝试在 Side A 分配 SKU1
            res_a = self._allocate_single_in_roadway(sku1, roadway_id, inventory_positions, side='A', sku_attrs=attrs1)
            # 尝试在 Side B 分配 SKU2
            res_b = self._allocate_single_in_roadway(sku2, roadway_id, inventory_positions, side='B', sku_attrs=attrs2)

            if res_a and res_b:
                if res_a == "LOW" and res_b == "LOW":
                    best_result_list.append(roadway_id)
                elif res_a == "HIGH" and res_b == "HIGH":
                    good_result_list.append(roadway_id)
                else:
                    better_result_list.append(roadway_id)

        # 返回顺序 (Best -> Better -> Good)
        if best_result_list:
            return best_result_list[0]
        if better_result_list:
            return better_result_list[0]
        if good_result_list:
            return good_result_list[0]
            
        return None

    def _allocate_empty_location_for_matched_beam(self, sku1: str, sku2: str, inventory_positions: List[InventoryPosition],
                                                  skus_data: Optional[List[Dict]] = None, task_id: Optional[str] = None) -> Optional[int]:
        """
        对应 Java: allocateEmptyLocationForMatchedBeam
        """
        empty_location_list = [p for p in inventory_positions if p.is_empty()]
        # 配对成功的BOM Location逻辑
        sku_location_list = [p for p in inventory_positions 
                            if not p.is_empty() and (sku1 in p.get_available_skus())]
        
        ranked_roadway_list = self._roadway_rank(
            empty_location_list, sku_location_list, self.aisles, self.used_roadway_list
        )
        
        # 只在empty里找
        for roadway_id in ranked_roadway_list:
            if self._can_accept_task_in_aisle(roadway_id, inventory_positions, skus_data or [], task_id):
                return roadway_id
        
        return None

    def _allocate_single_for_beam_code(self, sku: str, inventory_positions: List[InventoryPosition],
                                       preferred_side: Optional[str] = None, sku_attrs: Optional[Dict] = None,
                                       skus_data: Optional[List[Dict]] = None, task_id: Optional[str] = None) -> Optional[int]:
        """
        Corresponds to Java: allocateSingleForBeamCodeA
        """
        empty_location_list = [p for p in inventory_positions if p.is_empty()]
        
        sku_location_list = [p for p in inventory_positions 
                            if not p.is_empty() and sku in p.get_available_skus()]

        ranked_roadway_list = self._roadway_rank(
            empty_location_list, sku_location_list, self.aisles, self.used_roadway_list
        )
        
        best_result_list = []   # 找到 LOW 位
        better_result_list = [] # 只找到 HIGH 位
        
        # 确定尝试顺序：preferred_side 优先
        sides_to_try = []
        if preferred_side in ('A', 'B'):
            sides_to_try.append(preferred_side)
        # 添加另一个侧边
        sides_to_try += [s for s in ('A', 'B') if s not in sides_to_try]
        
        for roadway_id in ranked_roadway_list:
            if not self._can_accept_task_in_aisle(roadway_id, inventory_positions, skus_data or [], task_id):
                continue
            found_placement = False
            found_low = False
            
            # 按优先级尝试两侧
            for side in sides_to_try:
                result = self._allocate_single_in_roadway(sku, roadway_id, inventory_positions, side=side, sku_attrs=sku_attrs)
                if result:
                    found_placement = True
                    if result == "LOW":
                        found_low = True
                        break  # 找到LOW即可停止
                    # 如果是HIGH，继续看是否能在另一侧找到LOW
            if found_placement:
                if found_low:
                    best_result_list.append(roadway_id)
                else:
                    better_result_list.append(roadway_id)

        # 返回顺序 (Best -> Better)
        if best_result_list:
            return best_result_list[0]
        if better_result_list:
            return better_result_list[0]
        
        return None

    def _allocate_single_in_roadway(self, sku: str, roadway_id: int,
                                  inventory_positions: List[InventoryPosition], side: str,
                                  sku_attrs: Optional[Dict] = None) -> Optional[str]:
        """
        通用单SKU在指定巷道查找逻辑
        Side A -> Row 1 (Odd)
        Side B -> Row 2 (Even)
        Returns: 'LOW', 'HIGH', or None
        """
        target_row = 1 if side == 'A' else 2
        
        # 1. 尝试 LOW
        if sku in self.sku_solo:
            for pos in inventory_positions:
                if (pos.aisle == roadway_id and 
                    pos.row == target_row and 
                    pos.is_empty()):
                    return 'HIGH'
        target_sku_for_match = self.sku_pairs.get(sku)
        
        if target_sku_for_match:
            for pos in inventory_positions:
                if (pos.aisle == roadway_id and 
                    pos.row == target_row and 
                    not pos.is_empty() and
                    pos.matches_sku(target_sku_for_match, sku_attrs, self.match_fields)):
                    
                    # 检查是否有空间 (Stock状态)
                    if pos.has_space() and pos.can_place_sku('lower'):
                        return 'LOW'

        # 2. 尝试 HIGH
        for pos in inventory_positions:
            if (pos.aisle == roadway_id and 
                pos.row == target_row and 
                pos.is_empty()):
                return 'HIGH'
                
        return None

    def _get_position_id(self, location: InventoryPosition) -> str:
        return f"{location.aisle}-{location.row}-{location.column}-{location.level}"


class BaselinePositionAllocator:
    """
    完全复刻 Java 逻辑的货位分配策略 (执行阶段)
    """

    def __init__(self, warehouse_core):
        self.warehouse_core = warehouse_core
        self.config_data = SKUConfigBuilder.load_json("simulation/data/sku_config.json")
        self.sku_pairs = self.config_data["sku_pairs"]
        self.sku_solo = self.config_data["sku_solo"]
        self.match_fields = getattr(warehouse_core, 'match_fields', [])

    def _extract_attrs(self, sku_entry: Optional[Dict]) -> Dict:
        if not self.match_fields or not isinstance(sku_entry, dict):
            return {}
        return {k: sku_entry.get(k) for k in self.match_fields}

    def _get_sku_attrs(self, skus_data: List[Dict], sku_id: str) -> Dict:
        for entry in skus_data or []:
            if isinstance(entry, dict) and entry.get('skuId') == sku_id:
                return self._extract_attrs(entry)
        return {}

    def _resolve_single_beam_side(self, skus_data: List[Dict], sku_slots: List[Optional[str]]) -> Optional[str]:
        for entry in skus_data or []:
            if not isinstance(entry, dict) or not entry.get("skuId"):
                continue
            beam_side = getattr(entry.get("beamSide"), "value", entry.get("beamSide"))
            if beam_side:
                side_str = str(beam_side).upper()
                if side_str == "LEFT":
                    return "A"
                if side_str == "RIGHT":
                    return "B"

            legacy_side = getattr(entry.get("side"), "value", entry.get("side"))
            if legacy_side:
                legacy_str = str(legacy_side).upper()
                if legacy_str in {"A", "B"}:
                    return legacy_str

        if len(sku_slots) == 2:
            if sku_slots[0] is None and sku_slots[1] is not None:
                return "B"
            if sku_slots[0] is not None and sku_slots[1] is None:
                return "A"
        return None
    def _attrs_equal(self, attrs_a: Optional[Dict], attrs_b: Optional[Dict]) -> bool:
        if not self.match_fields:
            return True
        if attrs_a is None or attrs_b is None:
            return False
        for field in self.match_fields:
            if attrs_a.get(field) != attrs_b.get(field):
                return False
        return True

    def allocate(self, inventory_positions: List[InventoryPosition], 
                 task_info: TaskData, 
                 current_position: Optional[InventoryPosition] = None) -> List[InventoryPosition]:
        task_id = getattr(task_info, 'task_id', None)
        if task_id is None and isinstance(task_info, dict):
            task_id = task_info.get('task_id') or task_info.get('id')
        aisle = task_info.assigned_aisle
        if aisle is None:
            print(f"[WARN][allocator] BaselinePositionAllocator: no aisle assigned task={task_id}")
            return []
        aisle_positions = [p for p in inventory_positions if p.aisle == aisle]
        skus_data = task_info.skus
        sku_slots = [sku.get('skuId') if isinstance(sku, dict) else None for sku in skus_data]
        sku_ids = [sku for sku in sku_slots if sku is not None]

        if len(sku_ids) == 2:
            sku1, sku2 = sku_ids
            attrs1 = self._get_sku_attrs(skus_data, sku1)
            attrs2 = self._get_sku_attrs(skus_data, sku2)
            is_pair = self._is_match_sku(sku1, sku2) and self._attrs_equal(attrs1, attrs2)

            if is_pair:
                # Matched: 找一个空货位放两个
                result = self._allocate_matched_positions(aisle_positions, sku1, sku2)
            else:
                # Not Matched: 分别找
                result = self._allocate_not_matched_positions(aisle_positions, sku1, sku2, attrs1, attrs2)
        elif len(sku_ids) == 1:
            preferred_side = self._resolve_single_beam_side(skus_data, sku_slots)
            single_attrs = self._get_sku_attrs(skus_data, sku_ids[0])
            result = self._allocate_single_position(aisle_positions, sku_ids[0], side=preferred_side or "A", sku_attrs=single_attrs)
        else:
            result = []

        if not result:
            sku_ids = [sku.get('skuId') for sku in (task_info.skus or []) if isinstance(sku, dict) and sku.get('skuId') is not None]
            print(f"[WARN][allocator] BaselinePositionAllocator: no position allocated task={task_id} aisle={aisle} skus={sku_ids}")
        return result

    def _is_single_layer_sku(self, sku: str) -> bool:
        if sku in self.sku_solo: return True
        if sku in self.sku_pairs:
            return self.sku_pairs[sku] == sku
        return True

    def _is_match_sku(self, sku1: str, sku2: str) -> bool:
        return self.sku_pairs.get(sku1) == sku2

    def _allocate_matched_positions(self, positions: List[InventoryPosition], sku1: str, sku2: str) -> List[InventoryPosition]:
        # 找第一个空货位
        for pos in positions:
            if pos.is_empty():
                return [pos, pos]
        return []

    def _allocate_not_matched_positions(self, positions: List[InventoryPosition], sku1: str, sku2: str,
                                        attrs1: Optional[Dict] = None, attrs2: Optional[Dict] = None) -> List[InventoryPosition]:
        pos1, _ = self._find_best_in_side(positions, sku1, 'A', sku_attrs=attrs1)
        
        if not pos1:
            return []
        pos2, _ = self._find_best_in_side(positions, sku2, 'B', sku_attrs=attrs2)
        
        if pos1 and pos2:
            return [pos1, pos2]
            
        return []

    def _allocate_single_position(self, positions: List[InventoryPosition], sku: str, side: str,
                                  sku_attrs: Optional[Dict] = None) -> List[InventoryPosition]:
        pos, _ = self._find_best_in_side(positions, sku, side, sku_attrs=sku_attrs)
        if pos:
            return [pos]
        return []

    def _find_best_in_side(self, positions: List[InventoryPosition], sku: str, side: str,
                           sku_attrs: Optional[Dict] = None) -> Tuple[Optional[InventoryPosition], Optional[str]]:
        """
        在指定侧查找，并过滤掉 excluded 列表中的位置。
        Returns: (Position, Status['LOW'|'HIGH'|None])
        """
        target_row = 1 if side == 'A' else 2
        
        # 1. 优先找 Stock (LOW)
    
        target_sku_for_match = self.sku_pairs.get(sku)

        # 过滤候选：行符合 + 不在排除列表中
        candidates = [p for p in positions if p.row == target_row]
        # 排序: layer, col desc
        candidates.sort(key=lambda p: (p.level, -p.column))
        if sku in self.sku_solo:
            for pos in candidates:
                if pos.is_empty():
                    return pos, 'HIGH'
            
        if target_sku_for_match:
            for pos in candidates:
                if (not pos.is_empty() and 
                    pos.matches_sku(target_sku_for_match, sku_attrs, self.match_fields) and 
                    pos.can_place_sku('lower')):
                    return pos, 'LOW'
                
        # 2. 其次找 Empty (HIGH)
        for pos in candidates:
            if pos.is_empty():
                return pos, 'HIGH'
                
        return None, None
