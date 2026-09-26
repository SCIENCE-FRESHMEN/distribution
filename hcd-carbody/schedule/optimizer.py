"""
调度优化器模块
包含启发式调度器和基于采样的优化调度器
"""

import json
import math
import time
from typing import Dict, List, Optional, Tuple, Set
from simulation.task_data import TaskData, TASK_TYPE_INBOUND, TASK_TYPE_OUTBOUND
from simulation.position import InventoryPosition
from schedule.heuristic import HeuristicScheduler
import random


class OptimizationScheduler:
    """基于采样优化的调度器"""

    def __init__(self, warehouse_core, num_samples=100, num_evaluate=20):
        """
        Args:
            warehouse_core: WarehouseCore实例
            num_samples: 生成的候选方案数量
            num_evaluate: 实际评分的方案数量
        """
        self.warehouse_core = warehouse_core
        self.inventory_manager = warehouse_core.inventory_manager
        self.time_estimator = warehouse_core.time_estimator
        self.aisles = warehouse_core.aisles

        # 统计信息
        self.solve_count = 0
        self.total_time = 0.0

        # 采样参数
        self.num_samples = num_samples
        self.num_evaluate = num_evaluate
        # 入库紧急阈值：距当前组小于等于该值的需求视为紧急
        self.inbound_urgency_threshold = 3
        # 非 FIFO 出库中，同巷道可直接取出的货位越多，候选方案越具执行弹性。
        self.outbound_choice_bonus_weight = float(
            getattr(warehouse_core, "outbound_choice_bonus_weight", 0.0)
        )

        # 入库货位分配器
        self.position_allocator = None

        # 启发式调度器。
        self.heuristic_scheduler = HeuristicScheduler(warehouse_core)

    # ==========================================================================
    # 主函数：采样方案生成、仿真评分与最优方案选择
    # ==========================================================================
    def solve(self, inbound_tasks: List[TaskData], outbound_tasks: List[TaskData],
             running_tasks: Optional[Dict[str, TaskData]] = None, current_time: float = 0.0) -> Dict[int, List[TaskData]]:
        """
        基于采样优化的调度算法

        Args:
            inbound_tasks: 入库任务列表
            outbound_tasks: 出库任务列表
            running_tasks: 正在执行的任务列表
            current_time: 当前时间

        Returns:
            aisle_task_sequences: {aisle: [TaskData, ...]}
        """

        # 将同一货位分配器交给启发式方案，保证两类方案的货位口径一致。
        self.heuristic_scheduler.position_allocator = self.position_allocator
        # heuristic_solution 是兜底方案，也作为候选方案评分时的基准。
        heuristic_solution = self.heuristic_scheduler.solve(inbound_tasks, outbound_tasks, running_tasks, current_time)

        # heuristic_solution如果全空/没有出库任务，则直接返回空方案
        if all(len(tasks) == 0 for tasks in heuristic_solution.values()):
            return heuristic_solution

        # 记录本次优化器采样与评分的耗时起点。
        start_time = time.time()

        # 硬优先级分支：API 选中的空托盘出库请求，在本轮巷道空闲且未堵塞时优先执行。
        # forced_solution 存放必须抢占本拍首位的高优先级空滑橇出库任务；
        # outbound_tasks 则移除这些已强制处理的任务，供后续常规采样使用。
        forced_solution, outbound_tasks = self._extract_forced_high_priority_outbound(
            outbound_tasks, running_tasks or {}, current_time
        )

        # 1. 筛选出库任务：只保留当前组的任务
        # filtered_outbound_tasks 仅保留当前生产组的出库任务；入库任务不参与组过滤。
        filtered_outbound_tasks = self._filter_outbound_tasks(outbound_tasks)

        # 2. 计算每个任务的可行巷道和位置
        # task_feasible_assignments: taskId -> 可选的 (巷道, 货位方案) 列表。
        task_feasible_assignments = self._compute_feasible_assignments(
            filtered_outbound_tasks, inbound_tasks
        )

        # 如果没有任务，直接返回空方案
        if not task_feasible_assignments:
            if forced_solution:
                return forced_solution
            return {aisle: [] for aisle in self.aisles}

        # 3. 基于running_tasks计算每个巷道已有的任务数
        # aisle_task_count 表示各巷道当前运行任务折算后的负载，用于避免向忙碌巷道继续堆叠任务。
        aisle_task_count = self._compute_aisle_task_count(running_tasks, current_time)

        # 4. 预先计算启发式方案的未来可执行任务数量
        # heuristic_future_ready 表示采用启发式基准方案后，各产线预计可连续完成的出库任务数。
        heuristic_future_ready = self._calc_future_ready_outbound(heuristic_solution)

        # 5. 生成大量候选方案
        print(f"[优化器]开始生成{self.num_samples}个候选方案...")
        # solutions 保存去重后的随机候选方案及其最大巷道负载，供后续统一评分。
        solutions = self._generate_solutions(
            filtered_outbound_tasks, inbound_tasks, task_feasible_assignments, aisle_task_count, current_time, heuristic_future_ready
        )

        if not solutions:
            return {aisle: [] for aisle in self.aisles}

        # 6. 根据max_tasks进行加权采样，选择方案进行评分
        # best_solution 是评分选出的常规最优方案，随后再合并强制高优先级任务。
        best_solution = self._evaluate_and_select_best(solutions, heuristic_solution, heuristic_future_ready)
        if forced_solution:
            for aisle, forced_tasks in forced_solution.items():
                # aisle 是强制任务所属巷道；forced_tasks 是该巷道必须置顶的高优先级任务列表。
                if not forced_tasks:
                    continue
                # tail 为该巷道常规方案中的任务队列的尾部，强制任务必须排在其之前。
                tail = best_solution.get(aisle, []) if best_solution else []
                # 如果同一任务在采样方案中重复出现，则去除重复项。
                # forced_ids 用于移除常规方案中可能重复采样到的同一任务。
                forced_ids = {t.task_id for t in forced_tasks}
                # t 是常规方案中的单个任务，过滤条件避免与强制任务重复。
                tail = [t for t in tail if t.task_id not in forced_ids]
                best_solution[aisle] = forced_tasks + tail

        # solve_time 仅统计优化器自身的采样和评分耗时。
        solve_time = time.time() - start_time
        self.total_time += solve_time
        self.solve_count += 1

        print(f"[优化器]调度完成，耗时{solve_time:.3f}秒")

        return best_solution if best_solution else {aisle: [] for aisle in self.aisles}

    # ==========================================================================
    # 辅助函数：可行性、采样、评分和产线进度估算
    # ==========================================================================
    def _sample_single_solution(self, all_tasks: List[TaskData],
                                outbound_task_ids: Set[str],
                                task_feasible_assignments: Dict,
                                aisle_task_count: Dict[int, int],
                                task_idle_unblocked_aisles: Dict[str, List[Tuple]],
                                heuristic_future_ready: Dict[int, int]) -> Dict[int, List[TaskData]]:
        """生成一份候选方案。

        该核心入口紧跟 ``solve``，使主调度调用链保持连续；实际的随机混排、
        巷道抽样和任务副本构造放在后置实现中，避免主流程被细节淹没。

        Args:
            all_tasks: 本轮参与采样的当前组出库任务和全部入库任务。
            outbound_task_ids: 用于识别 ``all_tasks`` 中出库任务的任务 ID 集合。
            task_feasible_assignments: 每个任务的可行 ``(巷道, 货位方案)`` 候选。
            aisle_task_count: 调度开始时各巷道的运行负载。
            task_idle_unblocked_aisles: 可立即启动的出库任务及其空闲非阻塞巷道。
            heuristic_future_ready: 各产线的预计连续可出库数量，用于排序。

        Returns:
            按巷道组织的单份随机候选任务序列。
        """
        return self._build_sampled_solution(
            all_tasks,
            outbound_task_ids,
            task_feasible_assignments,
            aisle_task_count,
            task_idle_unblocked_aisles,
            heuristic_future_ready,
        )

    def _is_high_priority_empty_outbound(self, task: TaskData) -> bool:
        """
        检查任务是否是高优先级的空托盘出库任务

        Args:
            task: 任务数据

        Returns:
            bool: 是否为高优先级空托盘出库任务
        """
        rec = getattr(task, "task_record", {}) or {}
        return bool(rec.get("empty_skid_request")) and bool(rec.get("high_priority"))

    def _extract_forced_high_priority_outbound(
        self,
        outbound_tasks: List[TaskData],
        running_tasks: Dict,
        current_time: float,
    ) -> Tuple[Dict[int, List[TaskData]], List[TaskData]]:
        """
        Extract immediate high-priority outbound tasks (empty-skid).
        If their aisle is idle and unblocked now, pin them as first task of that aisle.
        """
        forced: Dict[int, List[TaskData]] = {aisle: [] for aisle in self.aisles}
        remaining: List[TaskData] = []

        busy_aisles = set()
        if running_tasks:
            for t in running_tasks.values():
                aa = getattr(t, "assigned_aisle", None)
                if aa is not None:
                    busy_aisles.add(int(aa))

        for task in outbound_tasks:
            if not self._is_high_priority_empty_outbound(task):
                remaining.append(task)
                continue
            aisle = getattr(task, "assigned_aisle", None)
            out_line = getattr(task, "out_line", None) or getattr(task, "production_line", 1)
            if aisle is None:
                remaining.append(task)
                continue
            if hasattr(self.warehouse_core, "_is_aisle_enabled") and (not self.warehouse_core._is_aisle_enabled(int(aisle))):
                remaining.append(task)
                continue
            blocked = self.warehouse_core.check_blockage(int(aisle), out_line, current_time=current_time)
            if (int(aisle) not in busy_aisles) and (not blocked):
                forced[int(aisle)].append(task)
            else:
                remaining.append(task)

        return forced, remaining

    def _resolve_task_group_idx(self, task: TaskData):
        """执行 resolve 任务 任务组 idx 对应的业务处理。

        Args:
            self: 当前对象实例。
            task: 待处理的任务对象。

        Returns:
            处理结果；具体类型由调用上下文决定。
        """
        explicit_group_idx = getattr(task, "group_idx", None)
        if explicit_group_idx is not None:
            try:
                return int(explicit_group_idx)
            except (TypeError, ValueError):
                pass

        try:
            parts = task.task_id.split('_')
            if len(parts) >= 3 and parts[0] == 'OUTBOUND' and parts[1].startswith('PL') and parts[2].startswith('GP'):
                task_group_number = int(parts[2][2:])
                return task_group_number - 1
        except (ValueError, IndexError):
            return None
        return None

    def _filter_outbound_tasks(self, outbound_tasks: List[TaskData]) -> List[TaskData]:
        """筛选出库任务：只保留当前组的任务"""
        filtered_outbound_tasks = []
        for task in outbound_tasks:
            production_line = task.production_line
            current_group_idx = self.warehouse_core.production_line_current_group[production_line]
            explicit_group_idx = self._resolve_task_group_idx(task)
            if explicit_group_idx is not None and explicit_group_idx != current_group_idx:
                continue
            task_group_idx = self._resolve_task_group_idx(task)
            if task_group_idx is None or task_group_idx == current_group_idx:
                filtered_outbound_tasks.append(task)

        return filtered_outbound_tasks

    def _extract_feature_filters(self, task: TaskData, feature_keys: List[str]) -> List[dict]:
        """执行 extract 特征 filters 对应的业务处理。

        Args:
            self: 当前对象实例。
            task: 待处理的任务对象。
            feature_keys: 用于本函数处理的 `feature_keys` 参数。

        Returns:
            List[dict]: 处理后的结果。
        """
        filters = []
        for s in (task.skus or []):
            if not isinstance(s, dict):
                continue
            feats = s.get('features')
            if not feats and feature_keys:
                feats = {k: s.get(k) for k in feature_keys if k in s}
            if feats:
                filters.append(feats)
        return filters

    def _compute_feasible_assignments(self, outbound_tasks: List[TaskData],
                                     inbound_tasks: List[TaskData]) -> Dict[str, List[Tuple]]:
        """
        计算每个任务的可行巷道和位置

        Returns:
            {task_id: [(aisle, [positions]), ...]}
        """
        task_feasible_assignments = {}

        # 计算出库任务的可行巷道和位置
        for task in outbound_tasks:
            feasible = []
            match_mode = self.warehouse_core._get_outbound_match_mode(task.production_line)
            feature_keys = self.warehouse_core._get_outbound_match_features(task.production_line)
            # 启用 FIFO 的特征匹配产线只保留最早入库的库存，保证采样阶段不能跳过它。
            fifo_enabled = self.warehouse_core.is_outbound_fifo_enabled(task.production_line)

            if match_mode == "features":
                feature_filters = self._extract_feature_filters(task, feature_keys)
                if not feature_filters:
                    continue
                positions_by_aisle = {}
                positions = self.inventory_manager.get_positions_by_features(
                    feature_filters[0], feature_keys, only_available=True
                )
                if fifo_enabled and positions:
                    # FIFO 元数据由库存同步阶段写入 SKU features；缺失值不能阻断出库，
                    # 仅作为最晚顺序处理。
                    def position_inbound_time(position: InventoryPosition) -> float:
                        features = getattr(position, "features", {}) or {}
                        raw_time = features.get("_inbound_time") if isinstance(features, dict) else None
                        return float(raw_time) if isinstance(raw_time, (int, float)) else float("inf")

                    positions = [min(
                        positions,
                        key=lambda p: (
                            position_inbound_time(p),
                            int(p.aisle), int(p.row), int(p.column), int(p.level),
                        ),
                    )]
                for pos in positions:
                    if hasattr(self.warehouse_core, "_is_aisle_enabled") and (not self.warehouse_core._is_aisle_enabled(pos.aisle)):
                        continue
                    if pos.aisle not in positions_by_aisle:
                        positions_by_aisle[pos.aisle] = []
                    positions_by_aisle[pos.aisle].append(pos)
                for aisle, positions in positions_by_aisle.items():
                    feasible.append((aisle, positions))
            else:
                sku_ids = task.get_sku_ids()
                if not sku_ids:
                    continue

                # 车身库中的单 SKU 出库任务。
                sku = sku_ids[0]
                positions_by_aisle = {}
                for pos in self.inventory_manager.get_sku_positions(sku, only_available=True):
                    if hasattr(self.warehouse_core, "_is_aisle_enabled") and (not self.warehouse_core._is_aisle_enabled(pos.aisle)):
                        continue
                    if pos.aisle not in positions_by_aisle:
                        positions_by_aisle[pos.aisle] = []
                    positions_by_aisle[pos.aisle].append(pos)

                for aisle, positions in positions_by_aisle.items():
                    feasible.append((aisle, positions))

            if feasible:
                task_feasible_assignments[task.task_id] = feasible

        # 计算入库任务的可行巷道和位置
        for task in inbound_tasks:
            if getattr(task, "positions", None):
                pos0 = task.positions[0]
                if pos0 is not None:
                    task_feasible_assignments[task.task_id] = [(pos0.aisle, list(task.positions))]
                continue
            target_aisle = task.assigned_aisle
            if hasattr(self.warehouse_core, "_is_aisle_enabled") and (not self.warehouse_core._is_aisle_enabled(target_aisle)):
                continue
            current_position = self.warehouse_core.current_position_by_aisle.get(target_aisle)

            if not target_aisle:
                continue

            # 获取该巷道的可用位置
            available_positions = [
                pos for pos in self.inventory_manager.inventory_positions
                if pos.aisle == target_aisle and pos.is_empty()
            ]

            if not available_positions:
                continue

            # 使用货位分配器为任务分配位置
            if self.position_allocator:
                allocated_positions = self.position_allocator.allocate(
                    self.inventory_manager.inventory_positions, task, current_position
                )

                if allocated_positions:
                    task_feasible_assignments[task.task_id] = [(target_aisle, allocated_positions)]
            else:
                task_feasible_assignments[task.task_id] = [(target_aisle, available_positions)]

        return task_feasible_assignments

    def _compute_aisle_task_count(self, running_tasks: Optional[Dict[str, TaskData]], current_time: float) -> Dict[int, int]:
        """
        基于running_tasks计算每个巷道已有的任务数

        Args:
            running_tasks: 正在执行的任务列表
            current_time: 当前时间
        Returns:
            {aisle: task_count, ...}
        """
        aisle_task_count = {aisle: 0 for aisle in self.aisles}
        if running_tasks:
            for task in running_tasks.values():
                if task.assigned_aisle is not None and task.assigned_aisle in aisle_task_count:
                    unfinished_task_percent = max(0, (task.task_record['delivery_time'] - current_time )) / task.task_record['duration']
                    aisle_task_count[task.assigned_aisle] += unfinished_task_percent + 0.01
        return aisle_task_count

    def _generate_solutions(self, outbound_tasks: List[TaskData],
                           inbound_tasks: List[TaskData],
                           task_feasible_assignments: Dict,
                           aisle_task_count: Dict[int, int],
                           current_time: float,
                           heuristic_future_ready: Dict[int, int]) -> Dict[str, Tuple]:
        """
        生成大量候选方案

        Returns:
            {solution_key: (aisle_task_sequences, max_tasks_in_aisle), ...}
        """
        # all_tasks 包含当前组出库任务与全部入库任务，是每次随机排程的完整候选集合。
        all_tasks = list(outbound_tasks) + list(inbound_tasks)
        # outbound_task_ids 用于在后续混排和选巷道时快速区分任务类型。
        outbound_task_ids = {task.task_id for task in outbound_tasks}
        # solutions: 去重键 -> (巷道任务序列, 合并运行负载后的最大巷道负载)。
        solutions = {}

        # 预计算每个出库任务的空闲非阻塞巷道,且该出库任务处于当前group_idx的出库任务
        task_idle_unblocked_aisles = self._precompute_idle_unblocked_aisles(
            outbound_tasks, task_feasible_assignments, aisle_task_count, current_time
        )

        for i in range(self.num_samples):
            # solution 是本次随机采样得到的巷道 -> 任务序列映射。
            solution = self._sample_single_solution(
                all_tasks, outbound_task_ids, task_feasible_assignments,
                aisle_task_count, task_idle_unblocked_aisles, heuristic_future_ready
            )

            if solution is None:
                continue

            # 计算solution的key用于去重
            # solution_key 只由各巷道的任务顺序构成，用于过滤内容相同的重复采样方案。
            solution_key = self._solution_to_key(solution)

            if solution_key not in solutions:
                # 再加上aisle_task_count
                # new_aisle_task_count 合并运行中的负载和本次方案新增任务数。
                new_aisle_task_count = {aisle: len(tasks) + aisle_task_count[aisle] for aisle, tasks in solution.items()}

                # 计算单个巷道的最高任务数
                # max_tasks 是方案负载均衡的粗粒度指标，值越小表示最忙巷道越不拥挤。
                max_tasks = max(new_aisle_task_count.values())

                solutions[solution_key] = (solution, max_tasks)

        return solutions

    def _precompute_idle_unblocked_aisles(self, outbound_tasks: List[TaskData],
                                          task_feasible_assignments: Dict,
                                          aisle_task_count, current_time: float) -> Dict[str, List[Tuple]]:
        """
        预计算每个出库任务的空闲且非阻塞的可行巷道（只处理当前组的任务）

        Returns:
            {task_id: [(aisle, positions), ...]}  空闲且非阻塞的巷道列表
        """
        task_idle_unblocked = {}

        for task in outbound_tasks:
            if task.task_id not in task_feasible_assignments:
                continue

            feasible = task_feasible_assignments[task.task_id]
            if not feasible:
                continue

            production_line = task.production_line
            current_group_idx = self.warehouse_core.production_line_current_group[production_line]

            # 筛选当前组的任务（参考heuristic.py的逻辑）
            try:
                parts = task.task_id.split('_')
                # 期望：['OUTBOUND', 'PL{pl}', 'GP{group}', sku1, sku2]
                if len(parts) >= 3 and parts[0] == 'OUTBOUND' and parts[1].startswith('PL') and parts[2].startswith('GP'):
                    task_group_number = int(parts[2][2:])  # 去掉 'GP'
                    task_group_idx = task_group_number - 1
                    if task_group_idx != current_group_idx:
                        # 非当前组任务，跳过
                        continue
                # 无法解析的任务ID格式，保留处理
            except (ValueError, IndexError):
                # 解析出错，保留处理
                pass

            idle_unblocked = []

            for aisle, positions in feasible:
                # 检查巷道是否空闲（task_count 为 0）
                is_idle = aisle_task_count.get(aisle, 0) == 0
                # 检查巷道是否被阻塞
                out_line = getattr(task, "out_line", None) or production_line
                is_blocked = self.warehouse_core.check_blockage(aisle, out_line, current_time=current_time)

                if is_idle and not is_blocked:
                    idle_unblocked.append((aisle, positions))

            task_idle_unblocked[task.task_id] = idle_unblocked

        return task_idle_unblocked

    def _calc_future_ready_outbound(self, solution: Dict[int, List[TaskData]]) -> Dict[int, int]:
        """
        预估各产线在当前库存下可连续执行的出库任务数量（不考虑后续入库补充）。
        用于在采样时倾向选择未来可连贯执行更多出库的产线。
        会先模拟当前解中的任务对库存的影响，再进行估算。特征匹配产线按
        ``color/skid_type/skid_state`` 等实际匹配字段统计，而不是仅按 SKU 汇总。
        """
        # ready_counts: 产线号 -> 从当前组起可连续满足库存的出库任务数量。
        ready_counts: Dict[int, int] = {}
        # qty_cache: 需求单元键 -> 当前解和计划前缀扣减后的可用数量。
        qty_cache: Dict[Tuple, int] = {}

        def get_qty(unit_key: Tuple) -> int:
            """从缓存读取需求单元库存，首次读取时按真实匹配口径统计。"""
            if unit_key not in qty_cache:
                qty_cache[unit_key] = self._get_forecast_available_quantity(unit_key)
            return qty_cache[unit_key]

        # 先根据当前解(solution)模拟库存变化：出库扣减，入库增加
        for tasks in solution.values():
            # tasks 是某一巷道内已排入当前候选方案的任务序列。
            for t in tasks:
                # t 是方案中的单个任务，用于把本拍入出库影响预先映射到虚拟库存。
                if t.task_type == TASK_TYPE_OUTBOUND:
                    for unit_key, quantity in self._get_forecast_demand_units(t):
                        # unit_key / quantity 是该任务的一项库存影响，出库从虚拟库存中扣减。
                        qty_cache[unit_key] = get_qty(unit_key) - quantity
                elif t.task_type == TASK_TYPE_INBOUND:
                    for unit_key, quantity in self._get_forecast_demand_units(t):
                        # unit_key / quantity 是该任务的一项库存影响，入库向虚拟库存中增加。
                        qty_cache[unit_key] = get_qty(unit_key) + quantity

        # plan: 产线号 -> 按生产组排列的计划任务集合。
        plan = getattr(self.warehouse_core, "production_plan", {}) or {}
        # current_group_idx: 产线号 -> 当前正在推进的内部组索引。
        current_group_idx = getattr(self.warehouse_core, "production_line_current_group", {}) or {}

        for pl, groups in plan.items():
            # pl 是计划所属产线号；groups 是该产线全部生产组。
            # start_idx 是该产线本轮前瞻计算的起始生产组索引。
            start_idx = current_group_idx.get(pl, 0)
            # qty_available 仅记录该产线计划前缀已经扣减过的需求单元。
            qty_available: Dict[Tuple, int] = {}
            # ready 是从 start_idx 开始、库存可连续满足的出库任务累计数量。
            ready = 0

            for group in groups[start_idx:]:
                # group 是待验证的单个生产组，组内全部需求满足才可继续推进。
                # demand 汇总当前生产组对每个 SKU 或特征组合的数量需求。
                demand: Dict[Tuple, int] = {}
                for task in group:
                    # task 是生产组内的单个出库需求，可能不携带自身产线号。
                    for unit_key, quantity in self._get_forecast_demand_units(task, production_line=pl):
                        demand[unit_key] = demand.get(unit_key, 0) + quantity

                # feasible 标记当前组是否能被完整满足；一旦失败，后续组不再继续计算。
                feasible = True
                for unit_key, need in demand.items():
                    # unit_key 是 SKU 或特征组合；need 是当前组对该单元的汇总需求量。
                    # available 是扣除当前产线此前已验证组后，该需求单元的剩余虚拟库存。
                    available = qty_available.get(unit_key, get_qty(unit_key))
                    if available < need:
                        feasible = False
                        break
                if not feasible:
                    break

                # 扣减模拟库存并累计可连续任务数
                for unit_key, need in demand.items():
                    qty_available[unit_key] = qty_available.get(unit_key, get_qty(unit_key)) - need
                ready += len(group)

            ready_counts[pl] = ready

        return ready_counts

    def _get_forecast_demand_units(
        self, task: TaskData, production_line: Optional[int] = None
    ) -> List[Tuple[Tuple, int]]:
        """将任务需求转换为前瞻库存统计使用的 SKU 或特征组合需求单元。"""
        # production_line 是任务所属产线；计划任务未携带时使用调用方传入的计划产线。
        production_line = production_line or getattr(task, "production_line", None)
        # match_mode / feature_keys 决定库存单元按 SKU 还是实际参与匹配的 features 构造。
        match_mode = self.warehouse_core._get_outbound_match_mode(production_line)
        feature_keys = self.warehouse_core._get_outbound_match_features(production_line)
        # demand_units: [(库存单元键, 需求数量), ...]。
        demand_units: List[Tuple[Tuple, int]] = []

        for sku_item in getattr(task, "skus", []) or []:
            # sku_item 是一条 SKU 请求明细；quantity 是该明细的数量，默认 1。
            if not isinstance(sku_item, dict):
                continue
            quantity = int(sku_item.get("quantity", 1) or 1)
            if match_mode == "features" and feature_keys:
                # raw_features 支持嵌套 features 和历史平铺字段两种请求格式。
                raw_features = sku_item.get("features") or {}
                if not raw_features:
                    raw_features = {key: sku_item.get(key) for key in feature_keys if sku_item.get(key) is not None}
                # features 是归一化后的特征；feature_key 保证字段顺序稳定且可哈希。
                features = self.warehouse_core._normalize_feature_dict(raw_features)
                if all(features.get(key) is not None for key in feature_keys):
                    feature_key = tuple((key, str(features[key])) for key in feature_keys)
                    demand_units.append((("features", feature_key), quantity))
                    continue

            # sku_id 是 SKU/RFID 模式或特征不完整时的库存键。
            sku_id = sku_item.get("skuId")
            if sku_id:
                demand_units.append((("sku", str(sku_id)), quantity))
        return demand_units

    def _get_forecast_available_quantity(self, unit_key: Tuple) -> int:
        """按实际 SKU 或 features 匹配口径统计可用库存，排除禁用和预占货位。"""
        # unit_type / unit_value 分别表示库存键类别及其具体 SKU 或特征组合。
        unit_type, unit_value = unit_key
        # inventory_positions 是仓库全部物理货位；双层货位按上下层分别累计。
        inventory_positions = self.inventory_manager.inventory_positions

        if unit_type == "sku":
            sku_id, total = unit_value, 0
            for position in inventory_positions:
                # position 是待检查货位，禁用或预占货位不计入可用库存。
                if position.disabled or position.reserved:
                    continue
                if position.is_double_layer:
                    total += position.upper_quantity if position.upper_sku == sku_id else 0
                    total += position.lower_quantity if position.lower_sku == sku_id else 0
                elif position.sku == sku_id:
                    total += position.quantity
            return total

        # feature_filter / feature_keys 供库存管理器按全部特征字段比较。
        feature_filter = dict(unit_value)
        feature_keys = list(feature_filter.keys())
        total = 0
        for position in inventory_positions:
            if position.disabled or position.reserved:
                continue
            if position.is_double_layer:
                if position.upper_quantity > 0 and self.inventory_manager._features_match(position.upper_features, feature_filter, feature_keys):
                    total += position.upper_quantity
                if position.lower_quantity > 0 and self.inventory_manager._features_match(position.lower_features, feature_filter, feature_keys):
                    total += position.lower_quantity
            elif position.quantity > 0 and self.inventory_manager._features_match(position.features, feature_filter, feature_keys):
                total += position.quantity
        return total

    def _get_inbound_urgency(self, task: TaskData, threshold: int = 2) -> int:
        """
        查找所有产线生产计划中何时会需要该入库任务的SKU集合（或其mate），距离当前组越近越紧急。
        返回距离当前组的组数，找不到则返回很大值。
        """
        try:
            skus = task.get_sku_ids()
        except Exception:
            skus = []
        if not skus:
            return 9999

        target_set = set(skus)

        plan = getattr(self.warehouse_core, "production_plan", {}) or {}
        best = 9999

        # 遍历所有产线的计划
        for pl, groups in plan.items():
            # 获取该产线当前的组索引
            current_idx = self.warehouse_core.production_line_current_group.get(pl, 0)

            # 检查从当前组开始的后续组
            for idx, group in enumerate(groups[current_idx:], start=current_idx):
                for task_item in group:
                    task_skus = getattr(task_item, "skus", [])
                    group_set = {s.get("skuId") for s in task_skus if isinstance(s, dict) and s.get("skuId")}
                    if target_set.issubset(group_set):
                        best = min(best, idx - current_idx)
                        break
                if best <= threshold:
                    break  # 如果已找到足够近的匹配，则提前退出
            if best <= threshold:
                break  # 如果已找到足够近的匹配，则提前退出所有产线循环

        return best

    def _build_sampled_solution(self, all_tasks: List[TaskData],
                               outbound_task_ids: Set[str],
                               task_feasible_assignments: Dict,
                               aisle_task_count: Dict[int, int],
                               task_idle_unblocked_aisles: Dict[str, List[Tuple]],
                               heuristic_future_ready: Dict[int, int]) -> Dict[int, List[TaskData]]:
        """执行单份候选方案的随机混排、巷道抽样和任务副本构造。"""
        # aisle_task_sequences 是本次采样方案，按巷道保存最终任务执行顺序。
        aisle_task_sequences = {aisle: [] for aisle in self.aisles}

        # 复制 aisle_task_count 用于内部更新
        # local_task_count 是采样过程中的临时巷道负载；每分配一个任务即时递增，不影响外部输入。
        local_task_count = aisle_task_count.copy()

        # current_group_ready_task_ids 是当前组中已有可行货位，且至少存在空闲、未阻塞巷道的出库任务 ID。
        current_group_ready_task_ids = set(task_idle_unblocked_aisles.keys())
        # current_group_ready_tasks 优先入队，保证本拍能够立即启动的当前组出库任务优先于其他候选任务。
        current_group_ready_tasks = [
            task for task in all_tasks if task.task_id in current_group_ready_task_ids
        ]

        # deferred_outbound_tasks 是当前组中暂时没有空闲非阻塞巷道的出库任务；并非其他生产组任务。
        deferred_outbound_tasks = [
            task for task in all_tasks
            if task.task_id not in current_group_ready_task_ids and task.task_id in outbound_task_ids
        ]
        # inbound_tasks_to_mix 包含全部入库任务；入库没有生产组约束，后续会按紧急程度与随机权重混排。
        inbound_tasks_to_mix = [
            task for task in all_tasks
            if task.task_id not in current_group_ready_task_ids and task.task_id not in outbound_task_ids
        ]

        # line_progress: 产线 -> 已完成组数占总组数的比例，用于给落后产线的出库任务加权。
        line_progress = self._get_line_progress_percentage()

        # 根据heuristic_future_ready和line_progress对outbound任务进行综合排序
        if heuristic_future_ready:
            def get_ready_count(task):
                """返回任务所属产线预计可连续执行的出库任务数量。"""
                return heuristic_future_ready.get(task.production_line, 0) if hasattr(task, 'production_line') and task.production_line else 0

            def get_progress(task):
                """返回任务所属产线当前已完成的生产计划比例。"""
                return line_progress.get(task.production_line, 0) if hasattr(task, 'production_line') and task.production_line else 0

            # 综合考虑：未来可执行任务数多的优先，但进度慢的产线需要额外加权
            # 使用 -progress 加大权重，使进度慢的任务排序更靠前；作用于所有出库任务
            # sort_key 越大越靠前：可连续出库多的产线优先，进度较慢的产线额外加权。
            sort_key = lambda t: get_ready_count(t) - 5.0 * get_progress(t)
            current_group_ready_tasks.sort(key=sort_key, reverse=True)
            deferred_outbound_tasks.sort(key=sort_key, reverse=True)

        # 打乱入库任务顺序实现随机性
        random.shuffle(inbound_tasks_to_mix)

        # 紧急入库：若某产线即将需要该 SKU 集合（距离当前组 <= 阈值），提前提升优先级
        # inbound_urgencies: taskId -> 距离任一产线当前组需求的最小组间隔。
        inbound_urgencies = {}
        for task in inbound_tasks_to_mix:
            # task 是待评估紧急度的单个入库任务。
            inbound_urgencies[task.task_id] = self._get_inbound_urgency(task, self.inbound_urgency_threshold)
        # urgent_inbound_tasks 是即将被生产计划消耗的物料，统一提升到该巷道任务序列前部。
        urgent_inbound_tasks = [t for t in inbound_tasks_to_mix if inbound_urgencies.get(t.task_id, 9999) <= self.inbound_urgency_threshold]
        urgent_inbound_tasks.sort(key=lambda t: inbound_urgencies.get(t.task_id, 9999))
        # remaining_inbound_tasks 为非紧急入库任务，参与后续按概率的入出库混排。
        remaining_inbound_tasks = [t for t in inbound_tasks_to_mix if t not in urgent_inbound_tasks]

        # inbound 和 outbound混起来，但是inbound被选中的概率更高（inbound权重为2）
        # mixed_tasks_list 保存非优先任务的随机混排顺序；紧急入库会在方案形成后单独前置。
        mixed_tasks_list = []
        # 两个索引分别追踪剩余入库和暂缓出库的消费位置。
        inbound_idx = 0
        outbound_idx = 0

        # 不要提前 break：只要 in/out 任一还有任务就继续
        while inbound_idx < len(remaining_inbound_tasks) or outbound_idx < len(deferred_outbound_tasks):
            # inbound_has / outbound_has 表示对应任务池是否仍有未加入混排队列的任务。
            inbound_has = inbound_idx < len(remaining_inbound_tasks)
            outbound_has = outbound_idx < len(deferred_outbound_tasks)

            # 某一侧已空，强制消费另一侧
            if inbound_has and not outbound_has:
                mixed_tasks_list.append(remaining_inbound_tasks[inbound_idx])
                inbound_idx += 1
                continue
            if outbound_has and not inbound_has:
                mixed_tasks_list.append(deferred_outbound_tasks[outbound_idx])
                outbound_idx += 1
                continue

            # 两侧都有时，保持原先"偏向入库(0.8)"策略
            if random.random() < 0.8:
                mixed_tasks_list.append(remaining_inbound_tasks[inbound_idx])
                inbound_idx += 1
            else:
                mixed_tasks_list.append(deferred_outbound_tasks[outbound_idx])
                outbound_idx += 1


        # 先尝试当前可启动的当前组出库，再按混排顺序补充其他任务。
        # task 是当前准备尝试放入方案的原始任务对象。
        for task in current_group_ready_tasks + mixed_tasks_list:
            if task.task_id not in task_feasible_assignments:
                continue

            # feasible 是当前任务可用的 (巷道, 货位方案) 候选。
            feasible = task_feasible_assignments[task.task_id]
            if not feasible:
                continue

            # is_outbound 控制出库的空闲巷道约束与入库的普通负载选择逻辑。
            is_outbound = task.task_id in outbound_task_ids
            # aisle / positions 保存本任务最终抽样选中的巷道及其货位方案。
            aisle = None
            positions = None

            # 对于出库任务，优先选择空闲且非阻塞的巷道
            if is_outbound:
                # idle_unblocked 是预计算的可立即启动候选，避免重复查询巷道阻塞状态。
                idle_unblocked = task_idle_unblocked_aisles.get(task.task_id, [])
                # 筛选出当前 local_task_count 仍为0的巷道
                # current_idle_unblocked 会剔除本次采样前面步骤已占用的巷道。
                current_idle_unblocked = [
                    # a 是候选巷道号，p 是该巷道下已计算好的货位方案。
                    (a, p) for a, p in idle_unblocked if local_task_count.get(a, 0) == 0
                ]
                if current_idle_unblocked:
                    # 使用加权选择而非随机选择：优先选择可连续执行任务数多的产线对应的巷道
                    # weights 对应候选巷道的抽样权重，负载越低权重越高。
                    weights = []
                    for a, _positions in current_idle_unblocked:
                        # a 是待评分的候选巷道；_positions 已在候选元组中保留，权重计算无需读取。
                        # 仅按照巷道当前负载做权重：越空闲越优先
                        # aisle_weight 是巷道 a 的随机抽样权重。
                        aisle_weight = 1.0 / (1.0 + local_task_count.get(a, 0))
                        weights.append(aisle_weight)

                    # 根据权重选择巷道
                    # selected_idx 是按负载权重随机选中的候选下标。
                    selected_idx = random.choices(range(len(current_idle_unblocked)), weights=weights, k=1)[0]
                    aisle, positions = current_idle_unblocked[selected_idx]

            # 如果没有找到空闲非阻塞巷道，使用加权随机选择
            if aisle is None:
                # 加权选择巷道：任务数越少的巷道权重越高
                # weights 对应全部可行巷道的抽样权重，用于入库或暂不能立即启动的出库任务。
                weights = []
                for a, _positions in feasible:
                    # a 是可行巷道；_positions 是其对应货位方案，本循环只计算负载权重。
                    base_w = 1.0 / (1.0 + local_task_count.get(a, 0))
                    weights.append(base_w)
                # selected_idx 是按临时负载随机选中的可行候选下标。
                selected_idx = random.choices(range(len(feasible)), weights=weights, k=1)[0]
                aisle, positions = feasible[selected_idx]

            if positions:
                # 由于分配器已经返回确定的位置方案，直接使用即可
                # 对于双SKU任务保留完整的货位列表
                # positions 是已选巷道中所有直接可出库位置；仅非 FIFO 出库使用数量
                # 计算选择弹性奖励，FIFO 任务在后续函数中会被显式跳过。
                choice_count = len(positions) if is_outbound and isinstance(positions, list) else 1
                if (not is_outbound) and isinstance(positions, list) and len(getattr(task, "skus", []) or []) > 1:
                    # selected_position 保留多 SKU 入库任务的完整货位集合。
                    selected_position = positions
                elif isinstance(positions, list):
                    # 单SKU任务只需要一个位置，取第一个即可
                    # 单货位任务只取该方案的第一个货位。
                    selected_position = positions[0] if positions else None
                else:
                    selected_position = positions
            else:
                selected_position = None
                choice_count = 1

            # 跳过没有有效位置的任务
            if selected_position is None:
                continue

            # 构造TaskData
            # skus_list 保留外部传入 SKU 明细；缺失时由任务 SKU ID 补成最小格式。
            skus_list = task.skus if task.skus else [{'skuId': sid} for sid in task.get_sku_ids()]
            # task_type 根据任务来源集合确定，供仿真与评分识别入出库行为。
            task_type = TASK_TYPE_OUTBOUND if is_outbound else TASK_TYPE_INBOUND

            # new_task 是写入候选方案的独立任务副本，避免覆盖 pending 原始任务的分配结果。
            new_task = TaskData(
                task_id=task.task_id,
                task_type=task_type,
                task_name=getattr(task, 'task_name', task.task_id),
                skus=skus_list,
                production_line=task.production_line,
                assigned_aisle=aisle,
                assigned_time=getattr(task, 'assigned_time', 0),
                positions=[selected_position] if not isinstance(selected_position, list) else selected_position,
                task_record=getattr(task, 'task_record', {})
            )
            if hasattr(task, "out_line"):
                new_task.out_line = getattr(task, "out_line", None)
            if hasattr(task, "in_line"):
                new_task.in_line = getattr(task, "in_line", None)
            if hasattr(task, "plan_id"):
                new_task.plan_id = getattr(task, "plan_id", None)
            if hasattr(task, "group_idx"):
                new_task.group_idx = getattr(task, "group_idx", None)
            if is_outbound:
                new_task.choice_count = choice_count

            # 添加到巷道任务序列中
            aisle_task_sequences[aisle].append(new_task)

            # 更新 local_task_count
            local_task_count[aisle] = local_task_count.get(aisle, 0) + 1

        # 仅将紧急入库任务提前，其余任务保持原有顺序
        # urgent_inbound_ids 用于在每条巷道内将紧急入库任务稳定前置。
        urgent_inbound_ids = {t.task_id for t in urgent_inbound_tasks}
        for a, tasks in aisle_task_sequences.items():
            # a 是当前巷道号；tasks 是该巷道已形成的候选任务序列。
            # urgent_inbound 与 others 保持各自原有相对顺序，仅调整两类任务的先后。
            # t 是当前巷道内的单个候选任务；urgent_inbound / others 保持各自相对顺序。
            urgent_inbound = [t for t in tasks if t.task_type == TASK_TYPE_INBOUND and t.task_id in urgent_inbound_ids]
            others = [t for t in tasks if not (t.task_type == TASK_TYPE_INBOUND and t.task_id in urgent_inbound_ids)]
            if urgent_inbound:
                aisle_task_sequences[a] = urgent_inbound + others

        return aisle_task_sequences

    def _calculate_outbound_choice_bonus(self, solution: Dict[int, List[TaskData]]) -> float:
        """计算非 FIFO 出库任务的直接货位选择弹性奖励。

        Args:
            solution: 候选方案，键为巷道号，值为该巷道内的任务序列。

        Returns:
            float: 非正奖励值；多个可选货位越多，返回值越小。
        """
        bonus = 0.0
        for tasks in solution.values():
            for task in tasks:
                if getattr(task, "task_type", None) != TASK_TYPE_OUTBOUND:
                    continue
                # FIFO 产线必须按最早库存执行，多个库存位置不能视为可自由选择。
                if self.warehouse_core.is_outbound_fifo_enabled(task.production_line):
                    continue
                choice_count = int(getattr(task, "choice_count", 1) or 1)
                if choice_count > 1:
                    bonus -= self.outbound_choice_bonus_weight * math.log1p(choice_count - 1)
        return bonus

    def _solution_to_key(self, solution: Dict[int, List[TaskData]]) -> str:
        """将solution转换为字符串key用于去重"""
        key_parts = []
        for aisle in sorted(solution.keys()):
            tasks = solution[aisle]
            task_ids = [task.task_id for task in tasks]
            key_parts.append(f"{aisle}:{','.join(task_ids)}")
        return '|'.join(key_parts)

    def _evaluate_and_select_best(self, solutions: Dict[str, Tuple], heuristic_solution: Dict[int, List[TaskData]], heuristic_future_ready: Optional[Dict[int, int]] = None) -> Dict[int, List[TaskData]]:
        """
        根据max_tasks进行加权采样，评分并选择最优方案

        Args:
            solutions: {solution_key: (aisle_task_sequences, max_tasks), ...}
            heuristic_solution: 启发式解决方案
            heuristic_future_ready: 启发式方案中各产线可连续执行的出库任务数
        Returns:
            最优的aisle_task_sequences
        """
        # solutions_list 保存去重后候选方案的二元组：(巷道任务序列, 最大巷道负载)。
        solutions_list = list(solutions.values())

        # max_tasks_values 与 solutions_list 一一对应，供后续计算基础负载权重。
        max_tasks_values = [max_tasks for _, max_tasks in solutions_list]

        # 预先计算每个方案的"优先产线"得分与产线进度平衡度，便于加权
        # 优先产线得分：按方案中出库任务所属产线累计 heuristic_future_ready 的值，鼓励让可连续执行更多任务的产线先被推进
        # line_priority_scores: 每个候选方案推动未来可连续出库产线的累计收益。
        line_priority_scores = []
        # line_balance_scores: 每个候选方案的产线进度不均衡惩罚，越小越好。
        line_balance_scores = []
        for solution, _max_tasks in solutions_list:
            # solution 是单个候选的“巷道 -> 任务序列”映射；_max_tasks 为最大巷道负载，此阶段无需读取。
            priority = 0.0
            for tasks in solution.values():
                # tasks 是当前巷道内按执行顺序排列的候选任务。
                for t in tasks:
                    # t 是单个候选任务；只有出库任务能推进对应产线的生产计划。
                    if getattr(t, "task_type", None) == TASK_TYPE_OUTBOUND and getattr(t, "production_line", None):
                        priority += (heuristic_future_ready or {}).get(t.production_line, 0)
            line_priority_scores.append(priority)
            # _calculate_line_progress_balance_score返回的是方差*100，数值越小越平衡
            line_balance_scores.append(self._calculate_line_progress_balance_score(solution))
        # max_priority 用于将不同方案的产线收益控制在到 0-1 区间。
        max_priority = max(line_priority_scores) if line_priority_scores else 0

        # weights 是候选方案进入精细评分阶段的抽样权重，与 solutions_list 下标一致。
        weights = []
        for idx, max_tasks in enumerate(max_tasks_values):
            # idx 是当前候选方案下标；max_tasks 是其合并运行负载后的最大巷道负载。
            base_w = 1.0 / (max_tasks + 1.0) 
            # ready_norm 是产线连续出库收益相对于候选集合最大收益的比例。
            ready_norm = (line_priority_scores[idx] / max_priority) if max_priority > 0 else 0.0
            # balance_penalty 是该候选的产线进度方差惩罚。
            balance_penalty = line_balance_scores[idx]
            # balance_factor 将不均衡惩罚转换成 0-1 的权重折减系数。
            balance_factor = 1.0 / (1.0 + balance_penalty)  # 越平衡越接近1，越不平衡越被压低
            # ready_factor 是连续出库收益对抽样权重的奖励系数。
            ready_factor = 1.0 + 0.5 * ready_norm  # future_ready越大，采样权重略微提升
            weights.append(base_w * ready_factor * balance_factor)

        # indexed_weights: (候选下标, 抽样权重)，按权重排序以保留高质量候选。
        # i 是候选方案在 solutions_list 中的下标。
        indexed_weights = [(i, weights[i]) for i in range(len(weights))]
        # item 是 (候选下标, 权重) 元组，按其权重字段降序排列。
        indexed_weights.sort(key=lambda item: item[1], reverse=True)

        # num_to_evaluate 是实际执行仓库评分函数的候选数，受配置上限和候选总数共同约束。
        num_to_evaluate = min(self.num_evaluate, len(solutions_list))

        # top_5_count 是按抽样权重直接保送进入精细评分的候选数量。
        top_5_count = min(5, num_to_evaluate)
        # selected_indices 收集最终需要调用 get_sol_score 的候选原始下标；保留该下标
        # 才能在评分日志中关联采样权重、连续出库收益和产线均衡惩罚。
        selected_indices: List[int] = []

        # 添加前top_5_count个评分最高的方案
        for i in range(top_5_count):
            # idx 是排序后第 i 名候选在 solutions_list 中的原始下标。
            idx = indexed_weights[i][0]
            selected_indices.append(idx)

        # remaining_indices 是未保送候选在 solutions_list 中的原始下标。
        # i 是排序数组的下标，取值范围排除已保送的前 top_5_count 个候选。
        remaining_indices = [indexed_weights[i][0] for i in range(top_5_count, len(indexed_weights))]
        # remaining_solutions 与 remaining_indices 同序，供后续无放回加权抽样。
        remaining_solutions = [solutions_list[i] for i in remaining_indices]

        # additional_count 是除保送候选外，还需随机补入精细评分的数量。
        additional_count = num_to_evaluate - top_5_count
        if additional_count > 0 and len(remaining_solutions) > 0:
            # remaining_weights 与 remaining_solutions 同序，保留每个剩余方案的原始抽样权重。
            remaining_weights = [weights[i] for i in remaining_indices]

            # additional_indices 保存无放回随机补入的候选原始下标。
            additional_indices: List[int] = []
            # remaining_indices_copy 是 remaining_solutions 的局部下标池，每选中一个就移除。
            remaining_indices_copy = list(range(len(remaining_solutions)))
            # remaining_weights_copy 与局部下标池同步删除，保证权重对齐。
            remaining_weights_copy = remaining_weights.copy()

            for sample_round in range(min(additional_count, len(remaining_solutions))):
                # sample_round 是随机补样轮次，仅用于表达当前正在进行无放回抽样。
                if not remaining_indices_copy:
                    break

                # 归一化概率
                # total_weight 是当前剩余候选的权重总和，用于构造概率分布。
                total_weight = sum(remaining_weights_copy)
                if total_weight > 0:
                    # normalized_weights 是与局部下标池同序的标准化抽样概率。
                    normalized_weights = [w / total_weight for w in remaining_weights_copy]
                else:
                    # 如果所有权重都是0，使用均匀分布
                    normalized_weights = [1.0 / len(remaining_weights_copy)] * len(remaining_weights_copy)

                # selected_idx 是抽中的局部下标；list_idx 是它在可变下标池中的位置。
                selected_idx = random.choices(remaining_indices_copy, weights=normalized_weights, k=1)[0]
                list_idx = remaining_indices_copy.index(selected_idx)

                additional_indices.append(remaining_indices[selected_idx])
                remaining_indices_copy.pop(list_idx)
                remaining_weights_copy.pop(list_idx)

            selected_indices.extend(additional_indices)


        # best_score 保存当前最小综合分；best_solution 保存该分数对应的巷道任务序列。
        best_score = float('inf')

        for idx_eval, solution_idx in enumerate(selected_indices, start=1):
            # solution_idx 是候选的原始下标；保留它以便完整输出该候选的采样和仿真评分过程。
            solution, max_tasks = solutions_list[solution_idx]
            # idx_eval 是评分日志序号；solution 是当前精细评分候选；max_tasks 是其最大巷道负载。
            base_score, score_details = self.warehouse_core.get_sol_score(solution)

            # 产线进度均衡已经由 WarehouseCore 基于本候选实际完成的出库任务计入
            # base_score。这里不再重复叠加旧的“候选任务数方差”，确保该目标只受
            # production_line_balance_weight 一处配置控制。
            line_progress_balance_score = float(
                score_details.get('production_line_balance_penalty', 0.0) or 0.0
            )

            # 非 FIFO 任务在已选巷道内保留越多直接出库位置，现场替代空间越大。
            outbound_choice_bonus = self._calculate_outbound_choice_bonus(solution)

            # score 是最终比较分数；基础惩罚来自独立事件仿真，选择弹性奖励来自
            # 当前候选的可直接出库位置数。
            score = base_score + outbound_choice_bonus
            # empty_penalty 防止“什么都不做”的空方案因基础分过低被错误选中。
            empty_penalty = 0.0
            if all(len(tasks) == 0 for tasks in solution.values()):
                empty_penalty = 10000.0
                score += empty_penalty
            # 只打印可比较的数值指标，排除含 TaskData 的 aisle_schedules，避免日志膨胀且
            # 保留 P95 所需的原始指标、加权项及最终评分。
            compact_score_details = {
                key: value
                for key, value in score_details.items()
                if key not in {"aisle_schedules", "production_line_times", "aisle_completion_times"}
            }
            print(
                "[OPT_SCHEDULE_SCORE] "
                + json.dumps(
                    {
                        "evaluationIndex": idx_eval,
                        "solutionIndex": solution_idx,
                        "maxAisleTaskCount": max_tasks,
                        "sampling": {
                            "baseWeight": 1.0 / (max_tasks + 1.0) ** 2 if max_tasks > 0 else 0.01,
                            "futureReadyScore": line_priority_scores[solution_idx],
                            "futureReadyNormalized": (
                                line_priority_scores[solution_idx] / max_priority if max_priority > 0 else 0.0
                            ),
                            # 该值仅用于候选抽样，不参与最终分数，保留便于分析抽样偏好。
                            "samplingLineProgressBalanceRaw": line_balance_scores[solution_idx],
                            "samplingWeight": weights[solution_idx],
                        },
                        "simulation": compact_score_details,
                        # 候选仿真后实际完成出库任务所形成的产线推进比例方差。
                        "simulationLineProgressBalanceRaw": score_details.get(
                            "production_line_balance_variance", 0.0
                        ),
                        # 最终评分使用 WarehouseCore 计算的实际完成进度方差惩罚。
                        "lineProgressBalancePenalty": line_progress_balance_score,
                        "outboundChoiceBonus": outbound_choice_bonus,
                        "emptySolutionPenalty": empty_penalty,
                        "finalScore": score,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                )
            )
            if score < best_score:
                best_score = score
                best_solution = solution

        return best_solution

    def _calculate_line_progress_balance_score(self, solution: Dict[int, List[TaskData]]) -> float:
        """
        计算产线进度平衡性得分，用于评估方案的均衡性方差越小，平衡性越好，得分越低
        """
        # 获取当前各产线的进度百分比
        current_progress = self._get_line_progress_percentage()

        # 统计方案中各产线的出库任务数量
        outbound_tasks_by_line = {}
        for tasks in solution.values():
            for task in tasks:
                if hasattr(task, 'production_line') and hasattr(task, 'task_type'):
                    if task.task_type == TASK_TYPE_OUTBOUND:
                        pl = task.production_line
                        if pl not in outbound_tasks_by_line:
                            outbound_tasks_by_line[pl] = 0
                        outbound_tasks_by_line[pl] += 1

        # 计算各产线在当前方案下的预期进度
        expected_progress = {}
        for pl in range(1, self.warehouse_core.num_production_lines + 1):
            total_groups = len(self.warehouse_core.production_plan.get(pl, []))
            if total_groups > 0:
                current_progress_val = current_progress.get(pl, 0)
                additional_tasks = outbound_tasks_by_line.get(pl, 0)
                expected_progress[pl] = (current_progress_val * total_groups + additional_tasks) / total_groups

        if len(expected_progress) <= 1:
            return 0.0  # 只有一个或没有产线，无需平衡

        # 计算进度的方差，作为平衡性得分
        progress_values = list(expected_progress.values())
        mean_progress = sum(progress_values) / len(progress_values)
        variance = sum((p - mean_progress) ** 2 for p in progress_values) / len(progress_values)

        # 将方差放大作为平衡性惩罚，方差越小越好
        return variance * 100  # 放大系数可调整

    def get_average_solve_time(self) -> float:
        """获取平均求解时间"""
        if self.solve_count == 0:
            return 0.0
        return self.total_time / self.solve_count

    def _get_line_progress_percentage(self) -> Dict[int, float]:
        """
        获取每个产线的当前进度百分比

        Returns:
            {production_line: progress_percentage, ...} 进度百分比（0-1之间的小数）
        """
        progress_percentage = {}

        for pl in range(1, self.warehouse_core.num_production_lines + 1):
            total_groups = len(self.warehouse_core.production_plan.get(pl, []))
            if total_groups <= 0:
                continue

            current_group_idx = self.warehouse_core.production_line_current_group.get(pl, 0)
            progress_percentage[pl] = current_group_idx / total_groups if total_groups > 0 else 0.0

        return progress_percentage
