"""
Baseline Strategy
"""

import re
from typing import List, Optional

from simulation.position import InventoryPosition
from simulation.task_data import TaskData


class BaselineAisleAllocator:
    """Aisle allocator: capacity-aware with round-robin tie-break."""

    def __init__(self, warehouse_core):
        self.warehouse_core = warehouse_core
        # Ensure deterministic 1..n round-robin order.
        self.aisles = sorted(warehouse_core.aisles)
        self._rr_index = 0

    def _pending_inbound_count(self, aisle: int) -> int:
        pending = getattr(self.warehouse_core, "pending_inbound_by_aisle", None)
        if isinstance(pending, dict):
            try:
                return len(pending.get(aisle, []))
            except Exception:
                return 0
        return 0

    def allocate(
        self,
        task_info: TaskData,
        inventory_positions: List[InventoryPosition],
    ) -> Optional[int]:
        if not self.aisles:
            return None

        empty_by_aisle = {a: 0 for a in self.aisles}
        total_by_aisle = {a: 0 for a in self.aisles}
        for pos in inventory_positions:
            if pos.aisle not in empty_by_aisle:
                continue
            # Exclude disabled slots from effective aisle capacity.
            if getattr(pos, "disabled", False):
                continue
            total_by_aisle[pos.aisle] += 1
            if pos.is_empty():
                empty_by_aisle[pos.aisle] += 1

        aisles_with_empty = [
            a for a in self.aisles
            if empty_by_aisle.get(a, 0) > 0 and total_by_aisle.get(a, 0) > 0
        ]
        if not aisles_with_empty:
            return None

        # Keep only currently enabled aisles when the core provides this runtime switch.
        if hasattr(self.warehouse_core, "_is_aisle_enabled"):
            aisles_with_empty = [a for a in aisles_with_empty if self.warehouse_core._is_aisle_enabled(a)]
        if not aisles_with_empty:
            return None

        # Restrict to valid inbound aisles if core can provide rule-aware filtering.
        if hasattr(self.warehouse_core, "_get_valid_inbound_aisles"):
            production_line = getattr(task_info, "production_line", None)
            valid = set(self.warehouse_core._get_valid_inbound_aisles(task_info, production_line))
            aisles_with_empty = [a for a in aisles_with_empty if a in valid]
        if not aisles_with_empty:
            return None

        projected_free_by_aisle = {}
        if hasattr(self.warehouse_core, "_get_projected_free_slots"):
            for a in aisles_with_empty:
                try:
                    projected_free_by_aisle[a] = int(self.warehouse_core._get_projected_free_slots(a))
                except Exception:
                    projected_free_by_aisle[a] = 0
            # Capacity guard: avoid aisles that are already projected full after considering
            # pending/running inbound tasks (empty - pending - running <= 0).
            guarded = [a for a in aisles_with_empty if projected_free_by_aisle.get(a, 0) > 0]
            if guarded:
                aisles_with_empty = guarded
        else:
            projected_free_by_aisle = {a: empty_by_aisle.get(a, 0) for a in aisles_with_empty}

        rr_rank = {self.aisles[(self._rr_index + i) % len(self.aisles)]: i for i in range(len(self.aisles))}

        def _score(aisle: int):
            # lower score is better:
            # 1) prefer higher projected-free ratio (capacity-aware)
            # 2) prefer lower pending inbound queue (lightweight load-balance tie-break)
            # 3) prefer higher absolute empty count
            # 4) use round-robin rank as tie-break for stability/fairness
            total = max(1, total_by_aisle.get(aisle, 0))
            empty = empty_by_aisle.get(aisle, 0)
            projected_free = projected_free_by_aisle.get(aisle, 0)
            projected_free_ratio = projected_free / total
            pending_cnt = self._pending_inbound_count(aisle)
            return (-projected_free_ratio, pending_cnt, -empty, rr_rank.get(aisle, 10**9), aisle)

        chosen = min(aisles_with_empty, key=_score)
        # Advance RR cursor after selection to keep a stable spread when scores are close.
        chosen_idx = self.aisles.index(chosen)
        self._rr_index = (chosen_idx + 1) % len(self.aisles)
        return chosen


class BaselinePositionAllocator:
    """Position allocator: column desc, then level asc within assigned aisle."""

    def __init__(self, warehouse_core):
        self.warehouse_core = warehouse_core

    def _parse_in_line_col(self, task_info: TaskData) -> Optional[int]:
        in_line = getattr(task_info, "in_line", None)
        if in_line is None:
            return None
        m = re.search(r"[cC](\d+)", str(in_line))
        if m:
            try:
                return int(m.group(1))
            except Exception:
                return None
        return None

    def _resolve_preferred_dock(self, task_info: TaskData, aisle: int) -> tuple[Optional[int], Optional[int]]:
        in_line = getattr(task_info, "in_line", None)
        out_line = getattr(task_info, "out_line", None) or getattr(task_info, "production_line", None)
        col = self._parse_in_line_col(task_info)
        level = None
        te = getattr(self.warehouse_core, "time_estimator", None)
        if te is not None and hasattr(te, "resolve_outbound_dock"):
            try:
                # Prefer outbound dock proximity (nearer to future retrieval).
                dock_col, dock_level = te.resolve_outbound_dock(out_line, default_layer=1, aisle=aisle)
                if dock_col is not None:
                    col = int(dock_col)
                if dock_level is not None:
                    level = int(dock_level)
            except Exception:
                pass
        # Fallback to inbound dock if outbound mapping is unavailable.
        if (col is None or level is None) and te is not None and hasattr(te, "resolve_inbound_dock"):
            try:
                dock_col, dock_level = te.resolve_inbound_dock(in_line, default_layer=1, aisle=aisle)
                if col is None and dock_col is not None:
                    col = int(dock_col)
                if level is None and dock_level is not None:
                    level = int(dock_level)
            except Exception:
                pass
        return col, level

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
            p for p in inventory_positions
            if p.aisle == aisle and p.is_empty() and not getattr(p, "disabled", False)
        ]
        if not aisle_positions:
            return []

        in_col, in_level = self._resolve_preferred_dock(task_info, aisle)
        cur_col = getattr(current_position, "column", None) if current_position is not None else None
        cur_level = getattr(current_position, "level", None) if current_position is not None else None

        def _score(p: InventoryPosition):
            # Conservative optimization:
            # 1) near inbound dock (column, level)
            # 2) shorter move from current crane position (if available)
            # 3) stable tie-breaks
            if in_col is None:
                dock_col_dist = 0
            else:
                dock_col_dist = abs(int(p.column) - int(in_col))
            if in_level is None:
                dock_lvl_dist = 0
            else:
                dock_lvl_dist = abs(int(p.level) - int(in_level))

            if cur_col is None or cur_level is None:
                move_from_current = 0
            else:
                move_from_current = abs(int(p.column) - int(cur_col)) +  abs(int(p.level) - int(cur_level))

            return (dock_col_dist, dock_lvl_dist, move_from_current, p.level, p.row, -p.column)

        aisle_positions.sort(key=_score)
        return [aisle_positions[0]]
