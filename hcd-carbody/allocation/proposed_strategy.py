from typing import List, Dict, Optional, Any, Set
from collections import deque
import os
from simulation.task_data import TaskData
from simulation.position import InventoryPosition
from estimate.time_estimator import load_time_estimator_config


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
        self.aisles = warehouse_core.aisles
        self._decision_count = 0
        aisle_cnt = max(1, len(self.aisles))
        
        def _env_int(name: str, default: int) -> int:
            try:
                return int(os.getenv(name, str(default)))
            except Exception:
                return default

        def _env_float(name: str, default: float) -> float:
            try:
                return float(os.getenv(name, str(default)))
            except Exception:
                return default
        # Lookahead outbound load (future n tasks), internal params.
        self._future_n = _env_int("PROPOSED_FUTURE_N", aisle_cnt)
        self._future_weight = _env_float("PROPOSED_FUTURE_WEIGHT", float(aisle_cnt))
        self._future_tail_weight = _env_float("PROPOSED_FUTURE_TAIL_WEIGHT", 0 * float(aisle_cnt))
        self._feature_weight = _env_float("PROPOSED_FEATURE_WEIGHT", float(aisle_cnt))
        self._pending_weight = _env_float("PROPOSED_PENDING_WEIGHT", 2 * float(aisle_cnt))
        self._recent_weight = _env_float("PROPOSED_RECENT_WEIGHT", 2 * float(aisle_cnt))
        # Capacity-aware controls to avoid overfilling small aisles.
        self._capacity_ratio_weight = _env_float("PROPOSED_CAPACITY_RATIO_WEIGHT", 30.0)
        self._debug_file = open('logs/proposed_allocation_debug.log', 'w', encoding='utf-8')
        self._recent_select_window = _env_int("PROPOSED_RECENT_SELECT_WINDOW", aisle_cnt)
        self._recent_selected_aisles = deque(maxlen=self._recent_select_window)

    def _compute_usable_capacity_by_aisle(self, inventory_positions: List[Any]) -> Dict[int, int]:
        """
        Count usable positions per aisle (exclude disabled positions).
        This is used as denominator for capacity ratio to normalize
        heterogeneous aisle sizes.
        """
        capacity = {aisle: 0 for aisle in self.aisles}
        for pos in inventory_positions:
            if getattr(pos, "disabled", False):
                continue
            capacity[pos.aisle] += 1
        return capacity

    def _extract_feature_filters(self, task_info: Any, feature_keys: List[str]) -> List[dict]:
        filters = []
        for s in getattr(task_info, "skus", []) or []:
            if not isinstance(s, dict):
                continue
            feats = s.get("features")
            if not feats and feature_keys:
                feats = {k: s.get(k) for k in feature_keys if k in s}
            if feats:
                filters.append(feats)
        return filters

    def _compute_feature_aisle_loads(self, task_info: Any) -> Dict[int, float]:
        loads = {aisle: 0.0 for aisle in self.aisles}
        inventory = getattr(self.warehouse_core, "inventory_manager", None)
        if not inventory:
            return loads
        production_line = getattr(task_info, "production_line", None) or 1
        feature_keys = self.warehouse_core._get_outbound_match_features(production_line)
        filters = self._extract_feature_filters(task_info, feature_keys)
        if not feature_keys or not filters:
            return loads
        target = filters[0]
        for pos in inventory.inventory_positions:
            if pos.is_double_layer:
                if pos.upper_quantity > 0 and inventory._features_match(pos.upper_features, target, feature_keys):
                    loads[pos.aisle] += pos.upper_quantity
                if pos.lower_quantity > 0 and inventory._features_match(pos.lower_features, target, feature_keys):
                    loads[pos.aisle] += pos.lower_quantity
            else:
                if pos.quantity > 0 and inventory._features_match(pos.features, target, feature_keys):
                    loads[pos.aisle] += pos.quantity
        return loads

    def _compute_future_outbound_loads(self) -> Dict[int, float]:
        loads = {aisle: 0.0 for aisle in self.aisles}
        plan = getattr(self.warehouse_core, "production_plan", {}) or {}
        current_idx = getattr(self.warehouse_core, "production_line_current_group", {}) or {}
        inventory = getattr(self.warehouse_core, "inventory_manager", None)
        if not inventory:
            return loads

        for pl in sorted(plan.keys()):
            groups = plan.get(pl, [])
            start = current_idx.get(pl, 0)
            pl_future_tasks = []
            for group in groups[start:]:
                for task_skus in group:
                    pl_future_tasks.append(task_skus)

            for idx, task_skus in enumerate(pl_future_tasks):
                if not task_skus:
                    continue
                sku_entry = task_skus[0]
                if isinstance(sku_entry, dict):
                    sku = sku_entry.get("skuId") or sku_entry.get("rfid") or sku_entry.get("RFID")
                else:
                    sku = sku_entry
                if not sku:
                    continue
                positions = inventory.get_sku_positions(sku, only_available=True)
                aisles = sorted({p.aisle for p in positions})
                if not aisles:
                    continue
                base = 1.0 if idx < self._future_n else self._future_tail_weight
                weight = base / len(aisles)
                for aisle in aisles:
                    loads[aisle] += weight

        return loads

    def allocate(self, task_info: Any, inventory_positions: List[Any]) -> Optional[int]:
        """
        Proposed aisle allocation:
        1) Count empty positions per aisle.
        2) Subtract pending inbound tasks (1 pending uses 1 empty slot).
        3) Apply future outbound load penalty.
        4) Choose aisle with max score (round-robin on ties).
        """
        print(
            "[DEBUG][ProposedAisleAllocator] task_info type=",
            type(task_info),
            "production_line=",
            getattr(task_info, "production_line", None),
        )
        # 1) Count empty positions.
        empty_by_aisle = {aisle: 0 for aisle in self.aisles}
        for pos in inventory_positions:
            if pos.is_empty():
                empty_by_aisle[pos.aisle] += 1

        # 2) Subtract pending inbound tasks.
        pending_by_aisle = {}
        pending_map = getattr(self.warehouse_core, "pending_inbound_by_aisle", {})
        for aisle in self.aisles:
            pending_by_aisle[aisle] = len(pending_map.get(aisle, []))

        effective_empty = {
            aisle: max(0, empty_by_aisle[aisle] - pending_by_aisle[aisle])
            for aisle in self.aisles
        }
        usable_capacity = self._compute_usable_capacity_by_aisle(inventory_positions)
        effective_ratio = {
            aisle: (
                float(effective_empty[aisle]) / float(max(1, usable_capacity.get(aisle, 0)))
            )
            for aisle in self.aisles
        }

        # Aisles with available slots.
        available_aisles = [
            aisle
            for aisle in self.aisles
            if effective_empty[aisle] > 0
            and (not hasattr(self.warehouse_core, "_is_aisle_enabled") or self.warehouse_core._is_aisle_enabled(aisle))
        ]
        # Apply forbidden-rule filtering in allocator stage as well.
        if hasattr(self.warehouse_core, "_get_valid_inbound_aisles"):
            production_line = getattr(task_info, "production_line", None)
            valid_aisles = set(self.warehouse_core._get_valid_inbound_aisles(task_info, production_line))
            available_aisles = [a for a in available_aisles if a in valid_aisles]
        if not available_aisles:
            return None
        # Filter out busy aisles (running tasks).
        # NOTE: Temporarily disabled by request; keep all available aisles in scoring.
        # busy_aisles = set()
        # running_tasks = getattr(self.warehouse_core, "running_tasks", {})
        # for task in running_tasks.values():
        #     aisle = getattr(task, "assigned_aisle", None)
        #     if aisle is not None:
        #         busy_aisles.add(aisle)
        #
        # candidate_aisles = [a for a in available_aisles if a not in busy_aisles]
        # If all aisles are busy, fall back to original available list.
        # if candidate_aisles:
        #     available_aisles = candidate_aisles

        # Decision counter.
        self._decision_count += 1

        future_loads = self._compute_future_outbound_loads()
        production_line = getattr(task_info, "production_line", None) or 1
        match_mode = self.warehouse_core._get_outbound_match_mode(production_line)
        if match_mode == "features":
            print("Using feature-based outbound matching")
        feature_loads = (
            self._compute_feature_aisle_loads(task_info)
            if match_mode == "features"
            else {aisle: 0.0 for aisle in self.aisles}
        )
        recent_counts_for_score = {a: 0 for a in available_aisles}
        for a in self._recent_selected_aisles:
            if a in recent_counts_for_score:
                recent_counts_for_score[a] += 1
        scores = {
            aisle: (
                self._capacity_ratio_weight * effective_ratio.get(aisle, 0.0)
                - self._future_weight * future_loads.get(aisle, 0.0)
                - self._feature_weight * feature_loads.get(aisle, 0.0)
                - self._pending_weight * pending_by_aisle.get(aisle, 0)
                - self._recent_weight * recent_counts_for_score.get(aisle, 0)
            )
            for aisle in available_aisles
        }

        # Debug log (first 500 decisions only).
        if self._decision_count <= 500:
            self._debug_file.write(
                f"[Decision #{self._decision_count}] empty={empty_by_aisle}, "
                f"pending={pending_by_aisle}, effective={effective_empty}, "
                f"capacity={usable_capacity}, effective_ratio={effective_ratio}, "
                f"future_loads={future_loads}, feature_loads={feature_loads}, recent_counts={recent_counts_for_score}, scores={scores}, "
                f"weights={{'capacity_ratio': {self._capacity_ratio_weight}, 'future': {self._future_weight}, 'feature': {self._feature_weight}, 'pending': {self._pending_weight}, 'recent': {self._recent_weight}}}, "
                f"available={available_aisles}\n"
            )
            self._debug_file.flush()

        # 4) Choose max score.
        max_score = max(scores[a] for a in available_aisles)
        candidate_aisles = [a for a in available_aisles if scores[a] == max_score]
        if not hasattr(self, '_rr_index'):
            self._rr_index = 0

        # Prefer the aisle that appears least in the most recent selection window.
        recent_counts = {a: 0 for a in candidate_aisles}
        for a in self._recent_selected_aisles:
            if a in recent_counts:
                recent_counts[a] += 1
        min_recent = min(recent_counts.values()) if recent_counts else 0
        least_recent_aisles = [a for a in candidate_aisles if recent_counts[a] == min_recent]

        # If still tied, use round-robin for deterministic tie-breaking.
        least_recent_aisles.sort()
        selected_index = self._rr_index % len(least_recent_aisles)
        selected_aisle = least_recent_aisles[selected_index]
        self._rr_index += 1
        self._recent_selected_aisles.append(selected_aisle)

        if self._decision_count <= 500:
            self._debug_file.write(
                f"  -> choose aisle {selected_aisle} "
                f"(reason: max score={max_score:.3f}, candidates={candidate_aisles}, "
                f"recent_counts={recent_counts}, least_recent={least_recent_aisles})\\n"
            )
            self._debug_file.flush()

        return selected_aisle



# ==========================================
# 2. 货位分配器 (ProposedPositionAllocator)
# ==========================================
class ProposedPositionAllocator:
    """
    Position allocator with explicit inbound/outbound dock awareness.

    Scoring objective (lower is better):
    - inbound leg: inbound dock -> target position
    - outbound leg: target position -> outbound dock

    This uses task-level `in_line` / `out_line` (when available), so decisions
    are aligned with the actual I/O ports of each task.
    """

    def __init__(self, warehouse_core):
        self.warehouse_core = warehouse_core
        self._time_cfg = load_time_estimator_config("config/time_estimator.json")
        try:
            self._w_in = float(os.getenv("PROPOSED_W_IN", "1.0"))
        except Exception:
            self._w_in = 1.0
        try:
            self._w_out = float(os.getenv("PROPOSED_W_OUT", "4.0"))
        except Exception:
            self._w_out = 4.0

    @staticmethod
    def _travel_time_2d(
        time_estimator,
        delta_col: int,
        delta_level: int,
        physics_cfg: Dict[str, Any],
    ) -> float:
        if not time_estimator:
            # Fallback to Manhattan distance proxy.
            return float(delta_col + delta_level)
        try:
            return float(
                time_estimator._physics_time_2d(
                    delta_col,
                    delta_level,
                    col_scale=float(physics_cfg.get("col_scale", 15.0)),
                    layer_scale=float(physics_cfg.get("layer_scale", 0.5)),
                    v_col_max=float(physics_cfg.get("v_col_max", 1.5)),
                    v_layer_max=float(physics_cfg.get("v_layer_max", 0.625)),
                    a_col=float(physics_cfg.get("a_col", 0.15)),
                    a_layer=float(physics_cfg.get("a_layer", 0.075)),
                )
            )
        except Exception:
            return float(delta_col + delta_level)

    def allocate(
        self,
        inventory_positions: List[InventoryPosition],
        task_info: TaskData,
        current_position: Optional[InventoryPosition] = None,
    ) -> List[InventoryPosition]:
        aisle = getattr(task_info, "assigned_aisle", None)
        if aisle is None:
            return []

        aisle_positions = [
            p for p in inventory_positions if p.aisle == aisle and p.is_empty()
        ]
        if not aisle_positions:
            return []

        time_estimator = getattr(self.warehouse_core, "time_estimator", None)
        production_line = getattr(task_info, "production_line", None) or 1
        in_line = getattr(task_info, "in_line", None) or 1
        out_line = getattr(task_info, "out_line", None) or production_line

        if time_estimator:
            dock_in_col, dock_in_level = time_estimator.resolve_inbound_dock(in_line, default_layer=1, aisle=aisle)
            dock_out_col, dock_out_level = time_estimator.resolve_outbound_dock(out_line, default_layer=1, aisle=aisle)
        else:
            # Conservative fallback if estimator is not available.
            dock_in_col = int(self._time_cfg.get("dock_in_col", 1))
            dock_out_col = int(self._time_cfg.get("dock_out_col", 1))
            dock_in_level, dock_out_level = 1, 1

        physics_cfg = self._time_cfg.get("physics", {}) or {}

        def _key(p: InventoryPosition):
            # inbound leg
            in_leg = self._travel_time_2d(
                time_estimator,
                abs(p.column - dock_in_col),
                abs(p.level - dock_in_level),
                physics_cfg,
            )
            # outbound leg
            out_leg = self._travel_time_2d(
                time_estimator,
                abs(p.column - dock_out_col),
                abs(p.level - dock_out_level),
                physics_cfg,
            )
            est_time = self._w_in * in_leg + self._w_out * out_leg
            # tie: prefer larger column, then lower level, then lower row
            return (est_time, -p.column, p.level, p.row)

        aisle_positions.sort(key=_key)
        return [aisle_positions[0]]
