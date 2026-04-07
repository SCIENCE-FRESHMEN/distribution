from typing import List, Dict, Optional, Any, Set
import random
import json
import os
from collections import defaultdict
from estimate.time_estimator import TimeEstimator
from simulation.position import InventoryPosition

# 巷道分配：配对优先，2选1的时候，考虑匹配紧急的出库任务；分配时不再考虑当前库存的balance而是产线对应任务的balance（巷道中同一产线的任务升序、空货位数量降序）
# 货位分配：1.双配对，去找各自的配对。
#  2.只有一个配对，另外一个放在正对侧，对侧被占的话尽可能小的移动（考虑层数和列数加和）。
#  3.已经配好对，放在sku对应产线同层，离出口最近（考虑层数和列数加和）。
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
        

    def _build_sku_to_pl_map(self) -> Dict[str, int]:
        """从生产计划中实时解析 SKU 所属的产线"""
        history_path = "simulation/data/sku_pl_history.json"
        today_path = "simulation/data/sku_pl_today.json"

        # 新 run 时重置历史文件，只使用当前计划计数（仅清理一次）
        if not getattr(self, "_history_cleared", False):
            for p in [history_path, today_path]:
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass
            self._history_cleared = True

        def _load_counts(path: str) -> Dict[str, Dict[int, int]]:
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        # 统一使用 int 类型的产线键，避免每次比较因类型差异而重复累加
                        return {sku: {int(pl): int(cnt) for pl, cnt in pls.items()} for sku, pls in data.items()}
                except Exception:
                    return {}
            return {}

        def _save_counts(path: str, data: Dict[str, Dict[str, int]]):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

        # 第一步：统计每个 SKU 在各个产线中出现的总次数
        # 格式: { sku_id: { pl_id: count } }
        sku_pl_counts = defaultdict(lambda: defaultdict(int))
        
        for pl, groups in self.warehouse_core.production_plan.items():
            for group in groups:
                for task_skus in group:
                    for sku in task_skus:
                        sku_pl_counts[sku][pl] += 1

        # 如果当天统计为空，则回退历史统计
        today_counts: Dict[str, Dict[int, int]] = {
            sku: {int(pl): int(cnt) for pl, cnt in pls.items()} for sku, pls in sku_pl_counts.items()
        }
        history_counts = _load_counts(history_path)
        today_counts_saved = _load_counts(today_path)

        if today_counts:
            # 保存今日统计
            _save_counts(today_path, today_counts)
            # 合并到历史 - 避免重复添加当天统计
            # 如果today_counts_saved与today_counts不同，说明今天是新一天或第一次运行
            # 这时才将今天的统计加到历史中
            if today_counts != today_counts_saved:
                for sku, pls in today_counts.items():
                    if sku not in history_counts:
                        history_counts[sku] = {}
                    for pl, cnt in pls.items():
                        history_counts[sku][pl] = history_counts[sku].get(pl, 0) + cnt
                _save_counts(history_path, history_counts)
            counts_for_mapping = today_counts
        else:
            # 当日为空，使用历史统计
            counts_for_mapping = history_counts

        # 第二步：为每个 SKU 选择计数最高的产线
        mapping = {}
        for sku, pl_dict in counts_for_mapping.items():
            # max(dict, key=dict.get) 会返回字典中 value 最大的 key
            best_pl_key = max(pl_dict, key=pl_dict.get)
            try:
                best_pl = int(best_pl_key)
            except Exception:
                best_pl = best_pl_key
            mapping[sku] = best_pl
        return mapping

    def allocate(self, task_info: Any, inventory_positions: List[Any]) -> Optional[int]:
        task_id = getattr(task_info, 'task_id', None)
        if task_id is None and isinstance(task_info, dict):
            task_id = task_info.get('task_id') or task_info.get('id')
        # 兼容 dict 和 TaskData 对象
        self.plan_sku_to_pl = self._build_sku_to_pl_map()
        skus_data = task_info.skus if hasattr(task_info, 'skus') else task_info.get('skus', [])
        if not skus_data:
            return random.choice(self.aisles)
            
        sku_ids = [s['skuId'] for s in skus_data]
        target_pls = [self.plan_sku_to_pl.get(sid) for sid in sku_ids]
        
        
        valid_aisles = self.aisles
        for pl in target_pls:
            if pl is not None:
                pl_aisles = [a for a, pls in self.warehouse_core.aisle_production_line_mapping.items() if pl in pls]
                valid_aisles = list(set(valid_aisles) & set(pl_aisles))
        
        if not valid_aisles: valid_aisles = self.aisles

        # 2. 情况二：入库双梁本身已配对 (A, B 是配对)
        if len(sku_ids) == 2 and self.sku_pairs.get(sku_ids[0]) == sku_ids[1]:
            # return self._balance_by_sku_distribution(sku_ids, valid_aisles, inventory_positions)
            result = self._balance_by_production_line(target_pls, valid_aisles, inventory_positions)
            if result is None:
                print(f"[WARN][allocator] ProposedAisleAllocator: no aisle allocated task={task_id} skus={sku_ids}")
            return result

        # 3. 情况三：非配对梁或单梁，进行紧急度对比
        result = self._allocate_with_urgency_comparison(sku_ids, target_pls, valid_aisles, inventory_positions)
        if result is None:
            print(f"[WARN][allocator] ProposedAisleAllocator: no aisle allocated task={task_id} skus={sku_ids}")
        return result
    def _get_pair_urgency_index(self, pl: int, sku: str, mate: str) -> int:
        """在生产计划中搜索 {sku, mate} 这一对出现的首个组索引（越小越紧急）"""
        if not mate: target_set = {sku}
        plan = self.warehouse_core.production_plan.get(pl, [])
        # 只关心“当前正在执行的组”之后首次出现的位置，因此从 current_idx + 1开始搜索
        current_idx = self.warehouse_core.production_line_current_group.get(pl, 0) + 1
        target_set = {sku, mate}
        for i in range(current_idx, len(plan)):
            for task_skus in plan[i]:
                if set(task_skus) == target_set:
                    return i - current_idx
        return 999999

    def _allocate_with_urgency_comparison(self, sku_ids, target_pl, aisles, inventory_positions):
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
            cands_r1 = self._find_paired_beam_in_side(sku, inventory_positions, 1)
            cands_r2 = self._find_paired_beam_in_side(sku, inventory_positions, 2)
            
            # 合并所有能实现配对的巷道
            candidates = list(set(cands_r1) | set(cands_r2))
            candidates = [a for a in candidates if a in aisles]
            
            if candidates:
                # 只要能配对，就在这些巷道里选一个该产线分布最均匀的
                # return self._balance_by_sku_distribution(sku_ids, candidates, inventory_positions)
                return self._balance_by_production_line(target_pl, candidates, inventory_positions)
            else:
                # 无法配对，全局均匀分布
                # return self._balance_by_sku_distribution(sku_ids, aisles, inventory_positions)
                return self._balance_by_production_line(target_pl, aisles, inventory_positions)
        # --- 场景 B：双梁入库 ---
        candidates1 = self._find_paired_beam_in_side(sku_ids[0], inventory_positions, 1)
        candidates2 = self._find_paired_beam_in_side(sku_ids[1], inventory_positions, 2)

        # 过滤物理不可达巷道
        candidates1 = [a for a in candidates1 if a in aisles]
        candidates2 = [a for a in candidates2 if a in aisles]

        # A. 同一巷道能同时解决两个配对
        both_match = list(set(candidates1) & set(candidates2))
        if both_match:
            # return self._balance_by_sku_distribution(sku_ids, both_match, inventory_positions)
            return self._balance_by_production_line(target_pl, both_match, inventory_positions)

        # B. 核心：如果两个SKU在不同巷道能配对，看谁的配对在未来计划中更紧急
        if candidates1 and candidates2:
            mate1 = self.sku_pairs.get(sku_ids[0])
            mate2 = self.sku_pairs.get(sku_ids[1])
            urg_idx1 = self._get_pair_urgency_index(target_pl[0], sku_ids[0], mate1)
            urg_idx2 = self._get_pair_urgency_index(target_pl[1], sku_ids[1], mate2)
            if urg_idx1 < urg_idx2: # SKU1配对更紧急
                pref = self._prefer_same_aisle_with_mate(candidates1, sku_ids[1], inventory_positions)
                if not pref:
                    pref = candidates1
                return self._balance_by_production_line(target_pl, pref, inventory_positions)
            elif urg_idx2 < urg_idx1: # SKU2配对更紧急
                pref = self._prefer_same_aisle_with_mate(candidates2, sku_ids[0], inventory_positions)
                if not pref:
                    pref = candidates2
                return self._balance_by_production_line(target_pl, pref, inventory_positions)
            else:
                # 优先：在能配对 SKU1 的巷道里，挑含 SKU2 配对梁的巷道；否则换另一侧；最后再均衡
                pref = self._prefer_same_aisle_with_mate(candidates1, sku_ids[1], inventory_positions)
                if not pref:
                    pref = self._prefer_same_aisle_with_mate(candidates2, sku_ids[0], inventory_positions)
                if not pref:
                    pref = list(set(candidates1) | set(candidates2))
                return self._balance_by_production_line(target_pl, pref, inventory_positions)

        # C. 只有单侧能配对
        if candidates1 or candidates2:
            options = list(set(candidates1) | set(candidates2))
            # 优先包含另一SKU配对品的巷道，便于同巷道移库
            other_sku = sku_ids[1] if candidates1 else sku_ids[0]
            pref = self._prefer_same_aisle_with_mate(options, other_sku, inventory_positions)
            if not pref:
                pref = options
            return self._balance_by_production_line(target_pl, pref, inventory_positions)
            
        # D. 无法配对，执行产线均匀分布
        # 尝试寻找含配对SKU的巷道以便同巷道移库；若仍无，则执行产线均匀分布
        mate_aisles_1 = self._find_aisles_with_mate_anywhere(sku_ids[0], inventory_positions) if sku_ids[0] else []
        mate_aisles_2 = self._find_aisles_with_mate_anywhere(sku_ids[1], inventory_positions) if sku_ids[1] else []
        options = list(set(mate_aisles_1) & set(mate_aisles_2))
        options = [a for a in options if a in aisles]
        if options:
            return self._balance_by_production_line(target_pl, options, inventory_positions)
        return self._balance_by_production_line(target_pl, aisles, inventory_positions)

    def _balance_by_production_line(self, target_pl, candidate_aisles, inventory_positions):
        """同一产线的任务尽可能分布均匀（寻找该产线货物最少的巷道）"""
        pending_by_aisle = getattr(self.warehouse_core, 'pending_inbound_by_aisle', {}) or {}
        scored_aisles = []
        for aisle in candidate_aisles:
            target_pl_load = 0
            empty_slots = 0
            sku_count = 0
            for pos in inventory_positions:
                if pos.aisle == aisle:
                    if pos.is_empty(): 
                        empty_slots += 1
                    else:
                        available_skus = pos.get_available_skus()
                        # 统计目标SKU在该巷道中的数量
                        for s in available_skus:
                            if s:
                                sku_count += 1
                        for s in [pos.upper_sku, pos.lower_sku, pos.sku]:
                            if s:
                                s_pl = self.plan_sku_to_pl.get(s)
                                if s_pl in target_pl: # 命中任务中涉及的任一产线
                                    target_pl_load += 1
            
            # 统计目标SKU在该巷道中的待入任务数量
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
                    sku_count += 1
                    if len(skus) == 2:
                        empty_slots -= 1
                    s_pl = self.plan_sku_to_pl.get(sku_id)
                    if s_pl in target_pl:
                        target_pl_load += 1
            scored_aisles.append({
                'aisle': aisle,
                'pl_load': target_pl_load,  # 目标产线负载
                'sku_count': sku_count,  # 目标产线SKU存量（越少越好）
                'empties': empty_slots
            })

        # 排序：负载升序 > 目标产线SKU存量升序 > 空位降序
        scored_aisles.sort(key=lambda x: (x['pl_load'], -x['empties']))
        return scored_aisles[0]['aisle'] if scored_aisles else random.choice(self.aisles)
    def _balance_by_sku_distribution(self, sku_ids: List[Optional[str]], candidate_aisles: List[int], inventory_positions: List[Any]) -> int:
            """
            核心策略：基于SKU分布数量和空货位数量进行巷道评分。
            1. 优先选择在候选列表(candidate_aisles)中的巷道。
            2. 目标SKU在巷道中的总数越少越好（实现SKU均匀分布）。
            3. 巷道空位越多越好。
            """
            # 过滤掉 None 值，只保留实际的 SKU ID
            valid_target_skus = [s for s in sku_ids if s is not None]
            candidate_set = set(candidate_aisles)

            # 初始化统计字典
            per_aisle_sku_counts = {aisle: 0 for aisle in self.aisles}
            per_aisle_empty_counts = {aisle: 0 for aisle in self.aisles}

            # 一次性遍历所有货位进行统计，提高效率
            for pos in inventory_positions:
                aisle = pos.aisle
                if pos.is_empty():
                    per_aisle_empty_counts[aisle] += 1
                else:
                    # 检查该位置是否含有任务中的任一 SKU
                    available_skus = pos.get_available_skus()
                    for t_sku in valid_target_skus:
                        if t_sku in available_skus:
                            # 累加该 SKU 在此位置的数量
                            per_aisle_sku_counts[aisle] += pos.get_total_quantity()

            # 构造评分数据结构
            scored_aisles = []
            for aisle in self.aisles:
                scored_aisles.append({
                    'aisle': aisle,
                    'is_candidate': 0 if aisle in candidate_set else 1, # 0表示在候选内，优先级更高
                    'sku_count': per_aisle_sku_counts[aisle],           # 存量越小越好
                    'empties': per_aisle_empty_counts[aisle]            # 空位越多越好
                })

            # 排序逻辑：
            # 1. 候选状态升序 (0 优于 1)
            # 2. SKU 存量升序 (越少越好)
            # 3. 空位数量降序 (-x 升序，即越多越好)
            scored_aisles.sort(key=lambda x: (x['is_candidate'], x['sku_count'], -x['empties']))

            # 返回评分最高的巷道 ID
            if scored_aisles:
                return scored_aisles[0]['aisle']
            return random.choice(self.aisles)   
    def _find_paired_beam_in_side(self, sku, inventory_positions, target_row):
        candidate_aisles = []
        pair_sku = self.sku_pairs.get(sku)
        if not pair_sku: return []
        for pos in inventory_positions:
            if pos.row == target_row and not pos.is_empty():
                if pair_sku in pos.get_available_skus() and pos.has_space():
                    candidate_aisles.append(pos.aisle)
        return list(set(candidate_aisles))

    def _find_aisles_with_mate_anywhere(self, sku: str, inventory_positions) -> List[int]:
        """返回包含配对SKU的巷道（不要求特定排/层，用于同巷道移库可行性估计）"""
        mate = self.sku_pairs.get(sku)
        if not mate:
            return []
        aisles = set()
        for pos in inventory_positions:
            if not pos.is_empty():
                available = pos.get_available_skus()
                if mate in available:
                    aisles.add(pos.aisle)
        return list(aisles)

    def _prefer_same_aisle_with_mate(self, candidate_aisles: List[int], other_sku: str, inventory_positions) -> List[int]:
        """将包含另一SKU配对品的巷道优先排序，便于后续同巷道移库"""
        if not other_sku:
            return candidate_aisles
        mate_aisles = set(self._find_aisles_with_mate_anywhere(other_sku, inventory_positions))
        return sorted(candidate_aisles, key=lambda a: (a not in mate_aisles, a))


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
        
    def _build_sku_to_pl_map(self) -> Dict[str, int]:
        sku_pl_counts = defaultdict(lambda: defaultdict(int))
        for pl, groups in self.warehouse_core.production_plan.items():
            for group in groups:
                for task_skus in group:
                    for sku in task_skus:
                        sku_pl_counts[sku][pl] += 1
        mapping = {}
        for sku, pl_dict in sku_pl_counts.items():
            mapping[sku] = max(pl_dict, key=pl_dict.get)
        return mapping

    def allocate(self, inventory_positions: List[Any], task_info: Any, current_position: Optional[InventoryPosition] = None) -> List[Any]:
        task_id = getattr(task_info, 'task_id', None)
        if task_id is None and isinstance(task_info, dict):
            task_id = task_info.get('task_id') or task_info.get('id')
        self.plan_sku_to_pl = self._build_sku_to_pl_map()
        aisle = task_info.assigned_aisle
        skus_data = task_info.skus if hasattr(task_info, 'skus') else task_info.get('skus', [])
        if not skus_data:
            print(f"[WARN][allocator] ProposedPositionAllocator: empty skus task={task_id}")
            return []
            
        sku_ids = [s['skuId'] for s in skus_data]
        pl_ids = [self.plan_sku_to_pl.get(sid) for sid in sku_ids]
        attrs1 = self._get_sku_attrs(skus_data, sku_ids[0]) if len(sku_ids) > 0 else {}
        attrs2 = self._get_sku_attrs(skus_data, sku_ids[1]) if len(sku_ids) > 1 else {}
        
        positions = [p for p in inventory_positions if p.aisle == aisle]
        is_solo_1 = sku_ids[1] is None  # [SKU, None]
        is_solo_2 = sku_ids[0] is None  # [None, SKU]
        if is_solo_1:
            pref_level = self._get_pl_preferred_level_from_plan(pl_ids[0]) if pl_ids[0] else max(self.inventory_manager.num_levels/2, 8)
            result = self._allocate_single_beam_flexible(positions, sku_ids[0], pref_level, home_row=1, guest_row=2, sku_attrs=attrs1)
            if not result:
                print(f"[WARN][allocator] ProposedPositionAllocator: no position allocated task={task_id} aisle={aisle} skus={sku_ids}")
            return result
        if is_solo_2:
            pref_level = self._get_pl_preferred_level_from_plan(pl_ids[1]) if pl_ids[1] else max(self.inventory_manager.num_levels/2, 8)
            result = self._allocate_single_beam_flexible(positions, sku_ids[1], pref_level, home_row=2, guest_row=1, sku_attrs=attrs2)
            if not result:
                print(f"[WARN][allocator] ProposedPositionAllocator: no position allocated task={task_id} aisle={aisle} skus={sku_ids}")
            return result
        # 2. 场景一：入库双梁即是配对梁 (A, B)
        if self.sku_pairs.get(sku_ids[0]) == sku_ids[1] and self._attrs_equal(attrs1, attrs2):
            pref_level = self._get_pl_preferred_level_from_plan(pl_ids[0]) if pl_ids[0] else max(self.inventory_manager.num_levels/2, 8)
            pos = self._find_best_empty_slot(positions, pref_level)
            result = [pos, pos] if pos else []
            if not result:
                print(f"[WARN][allocator] ProposedPositionAllocator: no position allocated task={task_id} aisle={aisle} skus={sku_ids}")
            return result

        # 3. 场景二：非配对双梁 (业务约束：SKU1->Row1, SKU2->Row2)
        pref_level1 = self._get_pl_preferred_level_from_plan(pl_ids[0]) if pl_ids[0] else max(self.inventory_manager.num_levels/2, 8)
        pref_level2 = self._get_pl_preferred_level_from_plan(pl_ids[1]) if pl_ids[1] else max(self.inventory_manager.num_levels/2, 8)
        result = self._allocate_double_constrained(positions, sku_ids, pref_level1, pref_level2, attrs1, attrs2)
        if not result:
            print(f"[WARN][allocator] ProposedPositionAllocator: no position allocated task={task_id} aisle={aisle} skus={sku_ids}")
        return result

    def _allocate_single_beam_flexible(self, positions, sku, pref_level, home_row, guest_row, sku_attrs: Optional[Dict] = None):
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

        # SKU1 必须在 Row 1 找配对
        target_p1 = self._find_mate_in_row(positions, mate1, 1, sku_attrs=attrs1)
        # SKU2 必须在 Row 2 找配对
        target_p2 = self._find_mate_in_row(positions, mate2, 2, sku_attrs=attrs2)

        if target_p1 and target_p2:
            return [target_p1, target_p2]

        if target_p1:
            res[0] = target_p1
            # SKU2 放在 SKU1 的正对侧 (Row 2)，实现对称移动最小化
            opposite = self._get_opposite_slot(positions, target_p1, 2)
            res[1] = opposite if opposite else self._find_nearest_empty(positions, target_p1, 2, pref_level2)
        elif target_p2:
            res[1] = target_p2
            opposite = self._get_opposite_slot(positions, target_p2, 1)
            res[0] = opposite if opposite else self._find_nearest_empty(positions, target_p2, 1, pref_level1)
        else:
            # 均无配对：SKU1选左排最佳，SKU2选其右排对侧
            res[0] = self._find_best_empty_slot(positions, pref_level1, row=1)
            if res[0]:
                opposite = self._get_opposite_slot(positions, res[0], 2)
                res[1] = opposite if opposite else self._find_nearest_empty(positions, res[0], 2, pref_level2)
        
        return [p for p in res if p]

    def _get_pl_preferred_level_from_plan(self, pl: int) -> int:
        """根据产线 ID 映射层级（产线层聚合）"""
        return self.time_estimator.dock_map_out.get(pl)

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
