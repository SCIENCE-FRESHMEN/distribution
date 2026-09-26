"""
Baseline Strategy
"""

import re
from typing import List, Optional

from simulation.position import InventoryPosition
from simulation.task_data import TaskData


# ==========================================================================
# 主类：基线巷道与货位分配策略
# ==========================================================================
class BaselineAisleAllocator:
    """Aisle allocator: capacity-aware with round-robin tie-break."""

    def __init__(self, warehouse_core):
        """初始化对象依赖、配置和运行时状态。

        Args:
            self: 当前对象实例。
            warehouse_core: 用于本函数处理的 `warehouse_core` 参数。

        Returns:
            处理结果；具体类型由调用上下文决定。
        """
        self.warehouse_core = warehouse_core
        # 保证按 1..n 的固定顺序轮询，避免同一输入因集合顺序变化而得到不同结果。
        self.aisles = sorted(warehouse_core.aisles)
        self._rr_index = 0

    # ==========================================================================
    # 辅助函数：巷道待处理数量与评分计算
    # ==========================================================================
    def _pending_inbound_count(self, aisle: int) -> int:
        """执行 待处理 入库 count 对应的业务处理。

        Args:
            self: 当前对象实例。
            aisle: 目标巷道编号。

        Returns:
            int: 处理后的结果。
        """
        pending = getattr(self.warehouse_core, "pending_inbound_by_aisle", None)
        pending_execution = getattr(self.warehouse_core, "pending_execution_tasks", {}) or {}
        running_tasks = getattr(self.warehouse_core, "running_tasks", {}) or {}
        extra = sum(
            1
            for task in pending_execution.values()
            if getattr(task, "task_type", "") == "INBOUND" and getattr(task, "assigned_aisle", None) == aisle
        )
        extra += sum(
            1
            for task in running_tasks.values()
            if getattr(task, "task_type", "") == "INBOUND" and getattr(task, "assigned_aisle", None) == aisle
        )
        if isinstance(pending, dict):
            try:
                return len(pending.get(aisle, [])) + extra
            except Exception:
                return extra
        return extra

    def allocate(
        self,
        task_info: TaskData,
        inventory_positions: List[InventoryPosition],
    ) -> Optional[int]:
        """执行 allocate 对应的业务处理。

        Args:
            self: 当前对象实例。
            task_info: 待处理的任务或任务描述对象。
            inventory_positions: 库存货位集合。

        Returns:
            Optional[int]: 处理后的结果。
        """
        if not self.aisles:
            return None

        empty_by_aisle = {a: 0 for a in self.aisles}
        total_by_aisle = {a: 0 for a in self.aisles}
        for pos in inventory_positions:
            if pos.aisle not in empty_by_aisle:
                continue
            # 禁用货位不计入巷道的有效容量。
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

        # 如果核心提供运行时开关，则只保留当前启用的巷道。
        if hasattr(self.warehouse_core, "_is_aisle_enabled"):
            aisles_with_empty = [a for a in aisles_with_empty if self.warehouse_core._is_aisle_enabled(a)]
        if not aisles_with_empty:
            return None

        # 如果核心能够提供带业务规则的过滤，则只保留允许入库的巷道。
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
            # 容量保护：结合当前库存和待入库任务的预计占用，跳过预计已经装满的巷道。
            # pending/running inbound tasks (empty - pending - running <= 0).
            guarded = [a for a in aisles_with_empty if projected_free_by_aisle.get(a, 0) > 0]
            if guarded:
                aisles_with_empty = guarded
        else:
            projected_free_by_aisle = {a: empty_by_aisle.get(a, 0) for a in aisles_with_empty}

        rr_rank = {self.aisles[(self._rr_index + i) % len(self.aisles)]: i for i in range(len(self.aisles))}

        def _score(aisle: int):
            # 分数越小越优：
            # 1) prefer higher projected-free ratio (capacity-aware)
            # 2) prefer lower pending inbound queue (lightweight load-balance tie-break)
            # 3) prefer higher absolute empty count
            # 4) use round-robin rank as tie-break for stability/fairness
            """执行 score 对应的业务处理。

            Args:
                aisle: 目标巷道编号。

            Returns:
                处理结果；具体类型由调用上下文决定。
            """
            total = max(1, total_by_aisle.get(aisle, 0))
            empty = empty_by_aisle.get(aisle, 0)
            projected_free = projected_free_by_aisle.get(aisle, 0)
            projected_free_ratio = projected_free / total
            pending_cnt = self._pending_inbound_count(aisle)
            return (-projected_free_ratio, pending_cnt, -empty, rr_rank.get(aisle, 10**9), aisle)

        chosen = min(aisles_with_empty, key=_score)
        # 选中后推进轮询游标，使分数接近时仍能稳定分散到不同巷道。
        chosen_idx = self.aisles.index(chosen)
        self._rr_index = (chosen_idx + 1) % len(self.aisles)
        return chosen


# ==========================================================================
# 主类：基线货位分配策略
# ==========================================================================
class BaselinePositionAllocator:
    """Position allocator: column desc, then level asc within assigned aisle."""

    def __init__(self, warehouse_core):
        """初始化对象依赖、配置和运行时状态。

        Args:
            self: 当前对象实例。
            warehouse_core: 用于本函数处理的 `warehouse_core` 参数。

        Returns:
            处理结果；具体类型由调用上下文决定。
        """
        self.warehouse_core = warehouse_core

    def _parse_in_line_col(self, task_info: TaskData) -> Optional[int]:
        """执行 parse in line col 对应的业务处理。

        Args:
            self: 当前对象实例。
            task_info: 待处理的任务或任务描述对象。

        Returns:
            Optional[int]: 处理后的结果。
        """
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
        """执行 resolve preferred dock 对应的业务处理。

        Args:
            self: 当前对象实例。
            task_info: 待处理的任务或任务描述对象。
            aisle: 目标巷道编号。

        Returns:
            tuple[Optional[int], Optional[int]]: 处理后的结果。
        """
        in_line = getattr(task_info, "in_line", None)
        col = self._parse_in_line_col(task_info)
        level = None
        te = getattr(self.warehouse_core, "time_estimator", None)
        if hasattr(self.warehouse_core, "_resolve_inbound_virtual_outbound_dock"):
            try:
                dock_col, dock_level = self.warehouse_core._resolve_inbound_virtual_outbound_dock(aisle)
                if dock_col is not None:
                    col = int(dock_col)
                if dock_level is not None:
                    level = int(dock_level)
            except Exception:
                pass
        # 出库口映射不可用时，回退到入库口。
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
        """执行 allocate 对应的业务处理。

        Args:
            self: 当前对象实例。
            inventory_positions: 库存货位集合。
            task_info: 待处理的任务或任务描述对象。
            current_position: 用于本函数处理的 `current_position` 参数。

        Returns:
            List[InventoryPosition]: 处理后的结果。
        """
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
            """计算基线策略中候选空货位的字典序评分。

            Args:
                p: 已通过巷道、禁用位和空位过滤的候选货位。

            Returns:
                tuple[int | float, ...]: 入库口距离、设备当前位置距离、层排及列号偏好的排序键。
            """
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

            # 入库口和设备位置是主要成本；仅在这些成本相同后使用列号模式保持确定性。
            return (
                dock_col_dist,
                dock_lvl_dist,
                move_from_current,
                p.level,
                p.row,
                *self.warehouse_core.get_position_column_preference_key(p.aisle, p.column),
            )

        aisle_positions.sort(key=_score)
        return [aisle_positions[0]]
