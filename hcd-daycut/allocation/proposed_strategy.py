from typing import List, Dict, Optional, Any, Set, Tuple
import random
from collections import defaultdict
from estimate.time_estimator import TimeEstimator
from simulation.position import InventoryPosition

# 巷道分配：优先选择能形成配对或便于后续同巷道移库的候选巷道；
# 在候选集合内，按目标 SKU 分布更少、剩余空位更多的方向做平衡。
# 对非天然配对的双梁任务，会结合未来计划中相关配对的紧急度决定优先侧。
# 货位分配：1. 已有双侧配对时，直接选择可用配对位。
#  2. 仅一侧能配对时，另一侧优先放到正对位；必要时选择代价更小的位置。
#  3. 都无法直接配对时，按偏好层、列距离和可用性选择当前位置。
# ==========================================
# 1. 巷道分配器 (ProposedAisleAllocator)
# ==========================================
class ProposedAisleAllocator:
    """
    策略核心：
    - 全局混存：不同产线可进入同一巷道。
    - 局部均衡：同一产线的货物尽可能均匀分布在不同巷道。
    - 计划驱动：SKU与产线的关系、任务紧急度均从 production_plan 动态获取。
    """
    def __init__(self, warehouse_core):
        self.warehouse_core = warehouse_core
        self.inventory_manager = warehouse_core.inventory_manager
        self.sku_pairs = warehouse_core.sku_pairs
        self.aisles = warehouse_core.aisles
        self.paired_sku_set = set(self.sku_pairs.keys()) | set(self.sku_pairs.values())
        # 动态构建 SKU -> 产线 ID 的映射
        sku_pl_counts = defaultdict(lambda: defaultdict(int))

    def _get_simulated_positions_for_aisle(
        self,
        aisle: int,
        inventory_positions: List[Any],
        current_task: Any = None,
        current_position: Optional[InventoryPosition] = None,
    ) -> List[Any]:
        position_allocator = getattr(self.warehouse_core, "inbound_position_allocator", None)
        if position_allocator is None or not hasattr(position_allocator, "simulate_pending_positions_for_aisle"):
            return [p for p in inventory_positions if p.aisle == aisle]
        try:
            return position_allocator.simulate_pending_positions_for_aisle(
                aisle=aisle,
                inventory_positions=inventory_positions,
                current_task=current_task,
                current_position=current_position,
            )
        except Exception:
            return [p for p in inventory_positions if p.aisle == aisle]

    def _build_simulated_positions_by_aisle(
        self,
        inventory_positions: List[Any],
        current_task: Any = None,
        current_position: Optional[InventoryPosition] = None,
    ) -> Dict[int, List[Any]]:
        return {
            aisle: self._get_simulated_positions_for_aisle(
                aisle,
                inventory_positions,
                current_task=current_task,
                current_position=current_position,
            )
            for aisle in self.aisles
        }

    def _get_distribution_metrics(
        self,
        sku_ids: List[Optional[str]],
        candidate_aisles: List[int],
        inventory_positions: List[Any],
        simulated_positions_by_aisle: Optional[Dict[int, List[Any]]] = None,
    ) -> List[Dict[str, Any]]:
        valid_target_skus = [s for s in sku_ids if s is not None]
        candidate_set = set(candidate_aisles)
        pending_by_aisle = getattr(self.warehouse_core, 'pending_inbound_by_aisle', {}) or {}
        if simulated_positions_by_aisle is None:
            simulated_positions_by_aisle = self._build_simulated_positions_by_aisle(inventory_positions)

        target_suffixes = {
            s.split('-', 1)[1]
            for s in valid_target_skus
            if isinstance(s, str) and '-' in s
        }
        target_sku_set = set(valid_target_skus)

        per_aisle_sku_counts = {aisle: 0 for aisle in self.aisles}
        per_aisle_same_suffix_counts = {aisle: 0 for aisle in self.aisles}
        per_aisle_empty_counts = {aisle: 0 for aisle in self.aisles}
        per_aisle_direct_pair_counts = {aisle: 0 for aisle in self.aisles}
        per_aisle_anywhere_pair_counts = {aisle: 0 for aisle in self.aisles}

        def _position_sku_qty(pos: Any, sku_id: Optional[str]) -> int:
            if not sku_id:
                return 0
            qty = 0
            if getattr(pos, "is_double_layer", False):
                if getattr(pos, "upper_sku", None) == sku_id:
                    qty += int(getattr(pos, "upper_quantity", 0) or 0)
                if getattr(pos, "lower_sku", None) == sku_id:
                    qty += int(getattr(pos, "lower_quantity", 0) or 0)
            else:
                if getattr(pos, "sku", None) == sku_id:
                    qty += int(getattr(pos, "quantity", 0) or 0)
            return qty

        for aisle, sim_positions in simulated_positions_by_aisle.items():
            for pos in sim_positions:
                if pos.is_empty():
                    per_aisle_empty_counts[aisle] += 1
                else:
                    for t_sku in valid_target_skus:
                        per_aisle_sku_counts[aisle] += _position_sku_qty(pos, t_sku)

                    for idx, t_sku in enumerate(sku_ids):
                        if not t_sku:
                            continue
                        pair_sku = self.sku_pairs.get(t_sku)
                        if not pair_sku or not pos.has_space():
                            continue
                        if pair_sku in pos.get_available_skus():
                            per_aisle_anywhere_pair_counts[aisle] += 1
                            target_row = 1 if idx == 0 else 2
                            if getattr(pos, "row", None) == target_row:
                                per_aisle_direct_pair_counts[aisle] += 1

                    if target_suffixes:
                        if getattr(pos, "is_double_layer", False):
                            if (
                                pos.upper_sku
                                and pos.upper_sku not in target_sku_set
                                and isinstance(pos.upper_sku, str)
                                and '-' in pos.upper_sku
                                and pos.upper_sku.split('-', 1)[1] in target_suffixes
                                and pos.upper_quantity > 0
                            ):
                                per_aisle_same_suffix_counts[aisle] += pos.upper_quantity
                            if (
                                pos.lower_sku
                                and pos.lower_sku not in target_sku_set
                                and isinstance(pos.lower_sku, str)
                                and '-' in pos.lower_sku
                                and pos.lower_sku.split('-', 1)[1] in target_suffixes
                                and pos.lower_quantity > 0
                            ):
                                per_aisle_same_suffix_counts[aisle] += pos.lower_quantity
                        else:
                            if (
                                pos.sku
                                and pos.sku not in target_sku_set
                                and isinstance(pos.sku, str)
                                and '-' in pos.sku
                                and pos.sku.split('-', 1)[1] in target_suffixes
                                and pos.quantity > 0
                            ):
                                per_aisle_same_suffix_counts[aisle] += pos.quantity

        for aisle in self.aisles:
            pending_tasks = pending_by_aisle.get(aisle, [])
            for task in pending_tasks:
                skus = getattr(task, 'skus', []) or []
                for sku_entry in skus:
                    sku_id = None
                    if isinstance(sku_entry, dict):
                        sku_id = sku_entry.get('skuId')
                    elif isinstance(sku_entry, str):
                        sku_id = sku_entry
                    if not sku_id:
                        continue
                    if sku_id in valid_target_skus:
                        per_aisle_sku_counts[aisle] += 1
                    elif (
                        target_suffixes
                        and isinstance(sku_id, str)
                        and '-' in sku_id
                        and sku_id.split('-', 1)[1] in target_suffixes
                    ):
                        per_aisle_same_suffix_counts[aisle] += 1

        scored_aisles = []
        for aisle in self.aisles:
                scored_aisles.append({
                    'aisle': aisle,
                    'is_candidate': 0 if aisle in candidate_set else 1,
                    'direct_pair_count': per_aisle_direct_pair_counts[aisle],
                    'anywhere_pair_count': per_aisle_anywhere_pair_counts[aisle],
                    'sku_count': per_aisle_sku_counts[aisle],
                    'same_suffix_sku_count': per_aisle_same_suffix_counts[aisle],
                    'empties': per_aisle_empty_counts[aisle],
                })
        return scored_aisles


    def allocate(self, task_info: Any, inventory_positions: List[Any]) -> Optional[int]:
        task_id = getattr(task_info, 'task_id', None)
        if task_id is None and isinstance(task_info, dict):
            task_id = task_info.get('task_id') or task_info.get('id')
        # 兼容 dict 和 TaskData 对象
        skus_data = task_info.skus if hasattr(task_info, 'skus') else task_info.get('skus', [])
        production_line_value = (
            getattr(task_info, "production_line", None)
            if hasattr(task_info, "production_line")
            else task_info.get("production_line") if isinstance(task_info, dict) else None
        )
        if not skus_data:
            return random.choice(self.aisles)

        sku_ids = [s.get('skuId', None) for s in skus_data]

        valid_aisles = self.aisles
        if production_line_value is None:
            target_pls = [None] * len(sku_ids)
        elif isinstance(production_line_value, (list, tuple, set)):
            target_pls = list(production_line_value)
        else:
            target_pls = [production_line_value]

        if len(target_pls) < len(sku_ids):
            target_pls = target_pls + [None] * (len(sku_ids) - len(target_pls))

        for pl in target_pls:
            if pl is not None:
                pl_aisles = [a for a, pls in self.warehouse_core.aisle_production_line_mapping.items() if pl in pls]
                valid_aisles = list(set(valid_aisles) & set(pl_aisles))

        if not valid_aisles: valid_aisles = self.aisles

        simulated_positions_by_aisle = self._build_simulated_positions_by_aisle(
            inventory_positions,
            current_task=task_info,
        )

        # API 单梁场景会直接传单元素列表；这里归一化成旧的双槽位表示，避免策略层只兼容旧格式。
        if len(sku_ids) == 1:
            beam_side = None
            if skus_data and isinstance(skus_data[0], dict):
                beam_side = getattr(skus_data[0].get("beamSide"), "value", skus_data[0].get("beamSide"))
            if str(beam_side).upper() == "RIGHT":
                sku_ids = [None, sku_ids[0]]
            else:
                sku_ids = [sku_ids[0], None]

        # 2. 情况二：入库双梁本身已配对 (A, B 是配对)
        if len(sku_ids) == 2 and self.sku_pairs.get(sku_ids[0]) == sku_ids[1]:
            result = self._balance_by_sku_distribution(
                sku_ids,
                valid_aisles,
                inventory_positions,
                simulated_positions_by_aisle=simulated_positions_by_aisle,
            )
            # result = self._balance_by_production_line(target_pls, valid_aisles, inventory_positions)
            if result is None:
                print(f"[WARN][allocator] ProposedAisleAllocator: no aisle allocated task={task_id} skus={sku_ids}")
            return result

        # 3. 情况三：非配对梁或包含单梁，进行紧急度对比
        result = self._allocate_with_urgency_comparison(
            sku_ids,
            target_pls,
            valid_aisles,
            inventory_positions,
            simulated_positions_by_aisle=simulated_positions_by_aisle,
            task_id=task_id,
        )
        if result is None:
            print(f"[WARN][allocator] ProposedAisleAllocator: no aisle allocated task={task_id} skus={sku_ids}")
        return result
    def _get_pair_urgency_index(self, pl: int, sku: str, mate: str) -> int:
        """按当前库内 + pending入库的可用数量估计紧急度，越少越紧急。"""
        if not sku:
            return 999999

        inventory_manager = getattr(self.warehouse_core, "inventory_manager", None)
        pending_by_aisle = getattr(self.warehouse_core, "pending_inbound_by_aisle", {}) or {}

        def _count_with_pending(target_sku: Optional[str]) -> int:
            if not target_sku:
                return 999999
            total = 0
            if inventory_manager is not None:
                try:
                    total += int(inventory_manager.get_sku_total_quantity(target_sku))
                except Exception:
                    pass
            for tasks in pending_by_aisle.values():
                for task in tasks or []:
                    for sku_entry in (getattr(task, "skus", []) or []):
                        sku_id = sku_entry.get("skuId") if isinstance(sku_entry, dict) else sku_entry
                        if sku_id == target_sku:
                            total += 1
            return total

        sku_count = _count_with_pending(sku)
        mate_count = _count_with_pending(mate)
        return min(sku_count, mate_count)

    def _allocate_with_urgency_comparison(
        self,
        sku_ids,
        target_pl,
        aisles,
        inventory_positions,
        simulated_positions_by_aisle: Optional[Dict[int, List[Any]]] = None,
        task_id: Optional[str] = None,
    ):
        # 第一个SKU匹配左排(Row1)，第二个SKU匹配右排(Row2)
        # --- 场景 A：单梁入库 ---
        is_solo_1 = sku_ids[1] is None  # [SKU, None]
        is_solo_2 = sku_ids[0] is None  # [None, SKU]
        # 1. 情况一：单梁入库
        if is_solo_1 or is_solo_2:
            if is_solo_1:
                sku = sku_ids[0]
            if is_solo_2:
                sku = sku_ids[1]
            # 同时在左排和右排寻找配对
            cands_r1 = self._find_paired_beam_in_side(
                sku, inventory_positions, 1, simulated_positions_by_aisle=simulated_positions_by_aisle
            )
            cands_r2 = self._find_paired_beam_in_side(
                sku, inventory_positions, 2, simulated_positions_by_aisle=simulated_positions_by_aisle
            )
            
            # 合并所有能实现配对的巷道
            candidates = list(set(cands_r1) | set(cands_r2))
            candidates = [a for a in candidates if a in aisles]
            
            if candidates:
                # 只要能配对，就在这些巷道里选一个该产线分布最均匀的
                return self._balance_by_sku_distribution(
                    sku_ids, candidates, inventory_positions, simulated_positions_by_aisle=simulated_positions_by_aisle
                )
                # return self._balance_by_production_line(target_pl, candidates, inventory_positions)
            else:
                # 无法配对，全局均匀分布
                return self._balance_by_sku_distribution(
                    sku_ids, aisles, inventory_positions, simulated_positions_by_aisle=simulated_positions_by_aisle
                )
                # return self._balance_by_production_line(target_pl, aisles, inventory_positions)
        # --- 场景 B：双梁入库 ---
        # Excel API payloads may submit double inbound tasks where either SKU can
        # pair with existing inventory in either row, so do not hard-code row 1/2.
        candidates1 = list(set(
            self._find_paired_beam_in_side(
                sku_ids[0], inventory_positions, 1, simulated_positions_by_aisle=simulated_positions_by_aisle
            )
            + self._find_paired_beam_in_side(
                sku_ids[0], inventory_positions, 2, simulated_positions_by_aisle=simulated_positions_by_aisle
            )
        ))
        candidates2 = list(set(
            self._find_paired_beam_in_side(
                sku_ids[1], inventory_positions, 1, simulated_positions_by_aisle=simulated_positions_by_aisle
            )
            + self._find_paired_beam_in_side(
                sku_ids[1], inventory_positions, 2, simulated_positions_by_aisle=simulated_positions_by_aisle
            )
        ))

        # 过滤物理不可达巷道
        candidates1 = [a for a in candidates1 if a in aisles]
        candidates2 = [a for a in candidates2 if a in aisles]

        # A. 同一巷道能同时解决两个配对
        both_match = list(set(candidates1) & set(candidates2))
        if both_match:
            return self._balance_by_sku_distribution(
                sku_ids, both_match, inventory_positions, simulated_positions_by_aisle=simulated_positions_by_aisle
            )
            # return self._balance_by_production_line(target_pl, both_match, inventory_positions)

        # B. 核心：如果两个SKU在不同巷道能配对，看谁的配对在未来计划中更紧急
        if candidates1 and candidates2:
            mate1 = self.sku_pairs.get(sku_ids[0])
            mate2 = self.sku_pairs.get(sku_ids[1])
            urg_idx1 = self._get_pair_urgency_index(target_pl[0], sku_ids[0], mate1)
            urg_idx2 = self._get_pair_urgency_index(target_pl[1], sku_ids[1], mate2)
            if urg_idx1 < urg_idx2: # SKU1配对更紧急
                pref = self._prefer_same_aisle_with_mate(
                    candidates1, sku_ids[1], inventory_positions, simulated_positions_by_aisle=simulated_positions_by_aisle
                )
                if not pref:
                    pref = candidates1
                return self._balance_by_sku_distribution(
                    sku_ids, pref, inventory_positions, simulated_positions_by_aisle=simulated_positions_by_aisle
                )
            elif urg_idx2 < urg_idx1: # SKU2配对更紧急
                pref = self._prefer_same_aisle_with_mate(
                    candidates2, sku_ids[0], inventory_positions, simulated_positions_by_aisle=simulated_positions_by_aisle
                )
                if not pref:
                    pref = candidates2
                return self._balance_by_sku_distribution(
                    sku_ids, pref, inventory_positions, simulated_positions_by_aisle=simulated_positions_by_aisle
                )
            else:
                # 优先：在能配对 SKU1 的巷道里，挑含 SKU2 配对梁的巷道；否则换另一侧；最后再均衡
                pref = self._prefer_same_aisle_with_mate(
                    candidates1, sku_ids[1], inventory_positions, simulated_positions_by_aisle=simulated_positions_by_aisle
                )
                if not pref:
                    pref = self._prefer_same_aisle_with_mate(
                        candidates2, sku_ids[0], inventory_positions, simulated_positions_by_aisle=simulated_positions_by_aisle
                    )
                if not pref:
                    pref = list(set(candidates1) | set(candidates2))
                return self._balance_by_sku_distribution(
                    sku_ids, pref, inventory_positions, simulated_positions_by_aisle=simulated_positions_by_aisle
                )

        # C. 只有单侧能配对
        if candidates1 or candidates2:
            options = list(set(candidates1) | set(candidates2))
            # 优先包含另一SKU配对品的巷道，便于同巷道移库
            other_sku = sku_ids[1] if candidates1 else sku_ids[0]
            pref = self._prefer_same_aisle_with_mate(
                options, other_sku, inventory_positions, simulated_positions_by_aisle=simulated_positions_by_aisle
            )
            if not pref:
                pref = options
            return self._balance_by_sku_distribution(
                sku_ids, pref, inventory_positions, simulated_positions_by_aisle=simulated_positions_by_aisle
            )
            
        # D. 无法配对，执行产线均匀分布
        # 尝试寻找含配对SKU的巷道以便同巷道移库；若仍无，则执行产线均匀分布
        mate_aisles_1 = self._find_aisles_with_mate_anywhere(
            sku_ids[0], inventory_positions, simulated_positions_by_aisle=simulated_positions_by_aisle
        ) if sku_ids[0] else []
        mate_aisles_2 = self._find_aisles_with_mate_anywhere(
            sku_ids[1], inventory_positions, simulated_positions_by_aisle=simulated_positions_by_aisle
        ) if sku_ids[1] else []
        options = list(set(mate_aisles_1) & set(mate_aisles_2))
        options = [a for a in options if a in aisles]
        if options:
            return self._balance_by_sku_distribution(
                sku_ids, options, inventory_positions, simulated_positions_by_aisle=simulated_positions_by_aisle
            )
        return self._balance_by_sku_distribution(
            sku_ids, aisles, inventory_positions, simulated_positions_by_aisle=simulated_positions_by_aisle
        )
    def _balance_by_sku_distribution(
        self,
        sku_ids: List[Optional[str]],
        candidate_aisles: List[int],
        inventory_positions: List[Any],
        simulated_positions_by_aisle: Optional[Dict[int, List[Any]]] = None,
        ) -> int:
            """
            核心策略：基于可配对数量、SKU分布和空货位数量进行巷道评分。
            """
            scored_aisles = self._get_distribution_metrics(
                sku_ids,
                candidate_aisles,
                inventory_positions,
                simulated_positions_by_aisle=simulated_positions_by_aisle,
            )

            scored_aisles.sort(
                key=lambda x: (
                    x['is_candidate'],
                    x['sku_count'],
                    x['same_suffix_sku_count'],
                    -x['empties'],
                )
            )

            if scored_aisles:
                return scored_aisles[0]['aisle']
            return random.choice(self.aisles)
    def _find_paired_beam_in_side(
        self,
        sku,
        inventory_positions,
        target_row,
        simulated_positions_by_aisle: Optional[Dict[int, List[Any]]] = None,
    ):
        candidate_aisles = []
        pair_sku = self.sku_pairs.get(sku)
        if not pair_sku: return []
        aisle_positions_source = simulated_positions_by_aisle or self._build_simulated_positions_by_aisle(inventory_positions)
        for aisle, positions in aisle_positions_source.items():
            for pos in positions:
                if pos.row == target_row and not pos.is_empty():
                    if pair_sku in pos.get_available_skus() and pos.has_space():
                        candidate_aisles.append(aisle)
        return list(set(candidate_aisles))

    def _find_aisles_with_mate_anywhere(
        self,
        sku: str,
        inventory_positions,
        simulated_positions_by_aisle: Optional[Dict[int, List[Any]]] = None,
    ) -> List[int]:
        """返回包含配对SKU的巷道（不要求特定排/层，用于同巷道移库可行性估计）"""
        mate = self.sku_pairs.get(sku)
        if not mate:
            return []
        aisles = set()
        aisle_positions_source = simulated_positions_by_aisle or self._build_simulated_positions_by_aisle(inventory_positions)
        for aisle, positions in aisle_positions_source.items():
            for pos in positions:
                if (not pos.is_empty()) and pos.has_space():
                    available = pos.get_available_skus()
                    if mate in available:
                        aisles.add(aisle)
        return list(aisles)

    def _prefer_same_aisle_with_mate(
        self,
        candidate_aisles: List[int],
        other_sku: str,
        inventory_positions,
        simulated_positions_by_aisle: Optional[Dict[int, List[Any]]] = None,
    ) -> List[int]:
        """将包含另一SKU配对品的巷道优先排序，便于后续同巷道移库"""
        if not other_sku:
            return candidate_aisles
        mate_aisles = set(
            self._find_aisles_with_mate_anywhere(
                other_sku,
                inventory_positions,
                simulated_positions_by_aisle=simulated_positions_by_aisle,
            )
        )
        preferred = [aisle for aisle in candidate_aisles if aisle in mate_aisles]
        return preferred if preferred else candidate_aisles


# ==========================================
# 2. 货位分配器 (ProposedPositionAllocator)
# ==========================================
class ProposedPositionAllocator:
    """
    策略核心：
    - 动态偏好层：由产线ID决定，实现产线在纵向空间的聚合。
    - 曼哈顿排序：产线层 > Column最小 > 物理距离(Column+|Level-1|)最小。
    - 场景化分配：单梁双排灵活匹配，非配对双梁Row1/Row2对称约束。
    """
    def __init__(self, warehouse_core):
        self.warehouse_core = warehouse_core
        self.inventory_manager = warehouse_core.inventory_manager
        self.sku_pairs = warehouse_core.sku_pairs
        # 使用warehouse_core中的time_estimator实例，而不是创建新的实例
        self.time_estimator = warehouse_core.time_estimator
        self.match_fields = getattr(warehouse_core, 'match_fields', [])

    def _extract_attrs(self, sku_entry: Optional[Dict]) -> Dict:
        if not self.match_fields or not isinstance(sku_entry, dict):
            return {}
        return {k: sku_entry.get(k) for k in self.match_fields}

    def _attrs_equal(self, attrs_a: Optional[Dict], attrs_b: Optional[Dict]) -> bool:
        if not self.match_fields:
            return True
        if attrs_a is None or attrs_b is None:
            return False
        for field in self.match_fields:
            if attrs_a.get(field) != attrs_b.get(field):
                return False
        return True

    def _get_sku_attrs(self, skus_data: List[Dict], sku_id: str) -> Dict:
        for entry in skus_data or []:
            if isinstance(entry, dict) and entry.get('skuId') == sku_id:
                return self._extract_attrs(entry)
        return {}

    def _resolve_single_beam_side(self, skus_data: List[Dict], sku_index: int) -> Optional[str]:
        if sku_index < 0 or sku_index >= len(skus_data):
            return None
        entry = skus_data[sku_index]
        if not isinstance(entry, dict):
            return None

        beam_side = getattr(entry.get("beamSide"), "value", entry.get("beamSide"))
        if beam_side:
            side_str = str(beam_side).upper()
            if side_str in {"LEFT", "RIGHT"}:
                return side_str

        legacy_side = getattr(entry.get("side"), "value", entry.get("side"))
        if legacy_side:
            legacy_str = str(legacy_side).upper()
            if legacy_str == "A":
                return "LEFT"
            if legacy_str == "B":
                return "RIGHT"

        if len(skus_data) == 2:
            first_sku = skus_data[0].get("skuId") if isinstance(skus_data[0], dict) else None
            second_sku = skus_data[1].get("skuId") if isinstance(skus_data[1], dict) else None
            if first_sku and not second_sku:
                return "LEFT"
            if second_sku and not first_sku:
                return "RIGHT"

        return None
        
    def _get_default_preferred_level(self, current_position: Optional[InventoryPosition] = None) -> int:
        if current_position is not None and getattr(current_position, "level", None) is not None:
            return current_position.level
        num_levels = getattr(self.inventory_manager, "num_levels", 0) or 0
        if num_levels > 0:
            return max(int(num_levels / 2), 8)
        return 8

    def allocate(self, inventory_positions: List[Any], task_info: Any, current_position: Optional[InventoryPosition] = None) -> List[Any]:
        task_id = getattr(task_info, 'task_id', None)
        if task_id is None and isinstance(task_info, dict):
            task_id = task_info.get('task_id') or task_info.get('id')
        aisle = task_info.assigned_aisle
        skus_data = task_info.skus if hasattr(task_info, 'skus') else task_info.get('skus', [])
        if not skus_data:
            print(f"[WARN][allocator] ProposedPositionAllocator: empty skus task={task_id}")
            return []
        positions = [p for p in inventory_positions if p.aisle == aisle]
        sku_ids = [s['skuId'] for s in skus_data]
        attrs1 = self._get_sku_attrs(skus_data, sku_ids[0]) if len(sku_ids) > 0 else {}
        attrs2 = self._get_sku_attrs(skus_data, sku_ids[1]) if len(sku_ids) > 1 else {}
        beam_side_1 = self._resolve_single_beam_side(skus_data, 0)
        beam_side_2 = self._resolve_single_beam_side(skus_data, 1)
        default_pref_level = self._get_default_preferred_level(current_position)

        is_solo_1 = sku_ids[1] is None if len(sku_ids) > 1 else True
        is_solo_2 = sku_ids[0] is None if len(sku_ids) > 1 else False
        if is_solo_1:
            home_row = 2 if str(beam_side_1).upper() == "RIGHT" else 1
            guest_row = 1 if home_row == 2 else 2
            result = self._allocate_single_beam_flexible(
                positions, sku_ids[0], default_pref_level, home_row=home_row, guest_row=guest_row,
                sku_attrs=attrs1
            )
        elif is_solo_2:
            home_row = 2 if str(beam_side_2).upper() == "RIGHT" else 1
            guest_row = 1 if home_row == 2 else 2
            result = self._allocate_single_beam_flexible(
                positions, sku_ids[1], default_pref_level, home_row=home_row, guest_row=guest_row,
                sku_attrs=attrs2
            )
        elif self.sku_pairs.get(sku_ids[0]) == sku_ids[1] and self._attrs_equal(attrs1, attrs2):
            pos = self._find_best_empty_slot(positions, default_pref_level)
            result = [pos, pos] if pos else []
        else:
            result = self._allocate_double_constrained(
                positions, sku_ids, default_pref_level, default_pref_level, attrs1, attrs2,
            )

        if not result:
            print(f"[WARN][allocator] ProposedPositionAllocator: no position allocated task={task_id} aisle={aisle} skus={sku_ids}")
        return result

    def _allocate_single_beam_flexible(
        self,
        positions,
        sku,
        pref_level,
        home_row,
        guest_row,
        sku_attrs: Optional[Dict] = None,
    ):
        mate = self.sku_pairs.get(sku)
        if mate:
            # 配对优先：轮询两排寻找已有配对
            for row in [1, 2]:
                matched = self._find_mate_in_row(positions, mate, row, sku_attrs=sku_attrs)
                if matched: return [matched]
        
        # 无法配对，找全巷道最佳位（不限排）
        pos = self._find_best_empty_slot(positions, pref_level, row=home_row)
        return [pos] if pos else []

    def _allocate_double_constrained(self, positions, sku_ids, pref_level1, pref_level2,
                                     attrs1: Optional[Dict] = None, attrs2: Optional[Dict] = None):
        res = [None, None]
        sku1, sku2 = sku_ids[0], sku_ids[1]
        mate1, mate2 = self.sku_pairs.get(sku1), self.sku_pairs.get(sku2)

        # Match aisle allocation: prefer the historical rows, then fall back to
        # the opposite row when existing inventory can form a valid pair there.
        target_p1 = (
            self._find_mate_in_row(positions, mate1, 1, sku_attrs=attrs1)
            or self._find_mate_in_row(positions, mate1, 2, sku_attrs=attrs1)
        )
        target_p2 = (
            self._find_mate_in_row(positions, mate2, 2, sku_attrs=attrs2)
            or self._find_mate_in_row(positions, mate2, 1, sku_attrs=attrs2)
        )

        if target_p1 and target_p2:
            return [target_p1, target_p2]

        if target_p1:
            res[0] = target_p1
            target_row = 1 if target_p1.row == 2 else 2
            opposite = self._get_opposite_slot(positions, target_p1, target_row)
            res[1] = opposite if opposite else self._find_nearest_empty(
                positions, target_p1, target_row, pref_level2
            )
        elif target_p2:
            res[1] = target_p2
            target_row = 1 if target_p2.row == 2 else 2
            opposite = self._get_opposite_slot(positions, target_p2, target_row)
            res[0] = opposite if opposite else self._find_nearest_empty(
                positions, target_p2, target_row, pref_level1
            )
        else:
            # 均无配对：SKU1选左排最佳，SKU2选其右排对侧
            res[0] = self._find_best_empty_slot(positions, pref_level1, row=1)
            if res[0]:
                opposite = self._get_opposite_slot(positions, res[0], 2)
                res[1] = opposite if opposite else self._find_nearest_empty(
                    positions, res[0], 2, pref_level2
                )
        
        return [p for p in res if p]
    def _find_best_empty_slot(self, positions, pref_level, row=None):
        """
        排序规则：
        1. 列数越大越优先
        2. 层级差低越优先
        """
        empty_slots = [p for p in positions if p.is_empty()]
        if row is not None:
            empty_slots = [p for p in empty_slots if p.row == row]
        
        if not empty_slots: return None
        
        empty_slots.sort(key=lambda p: (
            - p.column, 
            (-10 * p.column + abs(p.level - pref_level)) ,
        ))
        return empty_slots[0]

    def _find_mate_in_row(self, positions, mate_sku, target_row, sku_attrs: Optional[Dict] = None):
        if not mate_sku: return None
        # 按 Column 排序，确保配对也选离出口近的
        row_pos = sorted([p for p in positions if p.row == target_row], key=lambda x: x.column,reverse=True)
        for p in row_pos:
            if not p.is_empty() and p.matches_sku(mate_sku, sku_attrs, self.match_fields) and p.has_space():
                return p
        return None

    def _get_opposite_slot(self, positions, target_pos, target_row):
        """获取正对侧位置 (相同 Col/Level, 不同 Row)"""
        for p in positions:
            if (
                p.row == target_row
                and p.column == target_pos.column
                and p.level == target_pos.level
                and p.is_empty()
            ):
                return p
        return None

    def _find_nearest_empty(self, positions, target_pos, target_row, pref_level=None):
        """在目标排寻找距离最近的空位"""
        empty_slots = [p for p in positions if p.row == target_row and p.is_empty()]
        if not empty_slots: return None
        # 优先同列同层，其次同列层差更小，最后按曼哈顿距离（列差+层差）
        def _score(p):
            col_diff = abs(p.column - target_pos.column)
            level_diff = abs(p.level - target_pos.level)
            return (
                col_diff,      # 同列优先
                - p.column ,     # 同距离时列更大优先
                level_diff,    # 同列时层差越小越好
            )
        empty_slots.sort(key=_score)
        return empty_slots[0]
