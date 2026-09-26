"""
调度优化器模块
包含启发式调度器和基于采样的优化调度器
"""

import math
import time
from typing import Any, Dict, List, Optional, Tuple, Set
from simulation.task_data import TaskData, TASK_TYPE_INBOUND, TASK_TYPE_OUTBOUND
from schedule.heuristic import HeuristicScheduler
import random


class OptimizationScheduler:
    """基于采样优化的调度器。

    围绕当前入库、出库和运行中任务生成多个候选巷道序列，再交给 WarehouseCore 的仿真评分接口比较。
    因此这里的 ``positions``、巷道负载和产线推进量都是候选方案视图，只有调用方最终采用返回序列后才会进入真实调度流程。
    """

    def __init__(self, warehouse_core, num_samples=100, num_evaluate=15):
        """Args:
            warehouse_core: WarehouseCore实例
            num_samples: 生成的候选方案数量
            num_evaluate: 实际评分的方案数量

        Returns:
            None: 通过实例状态、队列或外部副作用完成处理。
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
        # 非 FIFO 场景下，出库任务在选定巷道内可直接出库的位置越多，分数越小
        self.outbound_choice_bonus_weight = float(
            getattr(warehouse_core, "outbound_choice_bonus_weight", 0.2)
        )

        # 入库货位分配器
        self.position_allocator = None

        # 启发式调度器作为优化器的基线和兜底方案来源。
        self.heuristic_scheduler = HeuristicScheduler(warehouse_core)

    # ========================================================================
    # 主流程：候选方案生成、仿真评分与最优解选择
    # ========================================================================
    def solve(self, inbound_tasks: List[TaskData], outbound_tasks: List[TaskData],
             running_tasks: Optional[Dict[str, TaskData]] = None,
             current_time: float = 0.0) -> Dict[int, List[TaskData]]:
        """
        基于采样优化的调度算法。

        处理顺序是“启发式基线 -> 当前组过滤 -> 可行位置计算 -> 候选
        序列采样 -> 仿真评分 -> 与启发式基线比较”。running_tasks 只作为
        巷道已占用程度和资源约束输入，不会被重复加入返回序列。

        Args:
            inbound_tasks: 入库任务列表
            outbound_tasks: 出库任务列表
            running_tasks: 正在执行的任务列表
            current_time: 当前时间

        Returns:
            aisle_task_sequences: {aisle: [TaskData, ...]}

        局部变量：
            heuristic_solution: 启发式调度得到的兜底序列，也是后续未来可执行量的基线。
            filtered_outbound_tasks: 仅保留各产线当前组的出库任务，禁止跨组抢占资源。
            task_feasible_assignments: ``task_id -> [(aisle, positions)]`` 的硬约束候选表。
            aisle_task_count: running 任务折算出的各巷道剩余负载，用于候选加权。
            heuristic_future_ready: 按 SKU 数量预估的各产线连续可推进任务数。
            solutions: 去重后的随机候选方案字典；best_solution: 评分最低的最终序列。
        """

        # 步骤 1：先生成启发式基线方案。该方案既是优化失败时的兜底结果，
        # 也是后续“未来连续可执行量”比较的基线，候选采样阶段不得修改它。
        self.heuristic_scheduler.position_allocator = self.position_allocator
        heuristic_solution = self.heuristic_scheduler.solve(inbound_tasks, outbound_tasks, running_tasks, current_time)

        # 步骤 1.1：启发式方案已无任何可调度任务时，无需继续生成随机候选。
        if all(len(tasks) == 0 for tasks in heuristic_solution.values()):
            return heuristic_solution

        # 本次 solve 的耗时起点，仅用于调度性能统计。
        start_time = time.time()

        # 步骤 2：过滤出库任务，仅保留各产线当前组，禁止未来组提前占用库存和巷道。
        filtered_outbound_tasks = self._filter_outbound_tasks(outbound_tasks)

        # 步骤 3：计算每项任务的硬约束候选表，统一校验库存属性、双梁配对、
        # 预留位置和可服务巷道，得到 task_id -> [(aisle, positions)]。
        task_feasible_assignments = self._compute_feasible_assignments(
            filtered_outbound_tasks, inbound_tasks
        )

        # 没有任何合法任务候选时，直接返回空方案。
        if not task_feasible_assignments:
            return {aisle: [] for aisle in self.aisles}

        # 步骤 4：把 running 任务折算为各巷道剩余负载，用于后续候选巷道加权。
        aisle_task_count = self._compute_aisle_task_count(running_tasks, current_time)

        # 步骤 5：按属性库存桶预估基线方案下各产线可连续推进的出库任务数量。
        heuristic_future_ready = self._calc_future_ready_outbound(heuristic_solution)

        # 步骤 6：生成去重的随机候选序列
        solutions = self._generate_solutions(
            filtered_outbound_tasks, inbound_tasks, task_feasible_assignments, aisle_task_count, current_time, heuristic_future_ready
        )

        # 全部采样候选均不可行时，返回空方案。
        if not solutions:
            print("[优化器]警告：未能生成任何可行方案，返回空方案")
            return {aisle: [] for aisle in self.aisles}

        print(f"[优化器]成功生成{len(solutions)}个不同的候选方案")

        # 步骤 7：对候选方案执行完整仿真评分，并与启发式基线比较后选出最低分方案。
        best_solution = self._evaluate_and_select_best(solutions, heuristic_solution, heuristic_future_ready)

        # 本轮算法墙钟耗时，不是仓库仿真时间。
        solve_time = time.time() - start_time
        self.total_time += solve_time
        self.solve_count += 1

        print(f"[优化器]调度完成，耗时{solve_time:.3f}秒")

        return best_solution if best_solution else {aisle: [] for aisle in self.aisles}


    # ========================================================================
    # 辅助函数：SKU、属性、FIFO 与可行性计算
    # ========================================================================
    def _sku_entry_to_dict(self, sku):
        """将 API 或仿真层传入的 SKU 对象规范为字典。

        输入：字典、Pydantic 模型或含 ``skuId``/``quantity`` 属性的对象。
        输出：包含 SKU 标识、数量及可能属性的新字典。
        作用：保证可行位置计算和评分使用同一属性读取方式，而不依赖输入模型类型。

        """
        if isinstance(sku, dict):
            return dict(sku)
        # 仿真生产计划通常以 ``['SKU_A', 'SKU_B']`` 保存任务，字符串 SKU 必须
        # 规范为与 API 条目相同的最小字典，否则计划预测会把它误当作空需求。
        if isinstance(sku, str):
            return {"skuId": sku, "quantity": 1}
        if hasattr(sku, "model_dump"):
            return sku.model_dump()
        if hasattr(sku, "dict"):
            return sku.dict()
        return {"skuId": getattr(sku, "skuId", None), "quantity": getattr(sku, "quantity", 1)}

    def _extract_sku_attrs(self, sku_dict: dict) -> dict:
        """按仓库 ``match_fields`` 提取会影响配对的 SKU 属性。

        输入：已规范化的 SKU 字典。
        输出：只含配置字段的属性字典；未配置匹配字段时返回空字典。

        """
        match_fields = getattr(self.warehouse_core, "match_fields", [])
        if not match_fields:
            return {}
        return {k: sku_dict.get(k) for k in match_fields}

    def _fifo_enabled(self) -> bool:
        """返回仓库配置的出库 FIFO 开关，用于决定候选位置是否按入库时间排序。

        Args:
            None: 无显式业务参数；使用实例状态或模块配置。

        """
        return bool(getattr(self.warehouse_core, "outbound_fifo_enabled", False))

    def _extract_position_inbound_time(self, pos, sku_id: str, attrs: dict) -> float:
        """提取候选货位内与任务匹配的库存入库时间。

        输入：候选货位 ``pos``、目标 ``sku_id`` 及匹配 ``attrs``。
        输出：命中库存层的最早 ``_inbound_time``；无匹配时为 ``math.inf``。
        双层货位分别检查上下层，以保证属性不同的同 SKU 不会混合参与 FIFO。

        """
        times = []
        if getattr(pos, "is_double_layer", False):
            if pos.matches_sku(sku_id, attrs, self.warehouse_core.match_fields, shelf='upper'):
                times.append((getattr(pos, "upper_attrs", {}) or {}).get("_inbound_time"))
            if pos.matches_sku(sku_id, attrs, self.warehouse_core.match_fields, shelf='lower'):
                times.append((getattr(pos, "lower_attrs", {}) or {}).get("_inbound_time"))
        else:
            if pos.matches_sku(sku_id, attrs, self.warehouse_core.match_fields):
                times.append((getattr(pos, "sku_attrs", {}) or {}).get("_inbound_time"))

        parsed = []
        for value in times:
            if value is None:
                parsed.append(0.0)
                continue
            try:
                parsed.append(float(value))
            except Exception:
                parsed.append(0.0)
        return min(parsed) if parsed else math.inf

    def _position_fifo_time(self, task: TaskData, pos) -> float:
        """为任务和候选货位计算 FIFO 排序键。

        输入：出库任务 ``task`` 与通过基础位置筛选的 ``pos``。
        输出：任务中各需求 SKU 可匹配库存的最早入库时间

        """
        sku_ids = task.get_sku_ids()
        if not sku_ids:
            return math.inf

        sku_attrs_map = {}
        for s in (task.skus or []):
            sku_dict = self._sku_entry_to_dict(s)
            sku_id = sku_dict.get("skuId")
            if sku_id and sku_id not in sku_attrs_map:
                sku_attrs_map[sku_id] = self._extract_sku_attrs(sku_dict)

        times = [
            self._extract_position_inbound_time(pos, sku_id, sku_attrs_map.get(sku_id, {}))
            for sku_id in sku_ids
        ]
        return min(times) if times else math.inf

    def _sort_positions_for_outbound(self, task: TaskData, positions: List[Any]) -> List[Any]:
        """返回符合 FIFO 规则的稳定出库候选顺序。

        输入：出库任务和同一巷道的合法货位列表。
        输出：FIFO 未启用时原列表，启用时按入库时间、配置列号偏好、层号排序后的新列表。

        """
        if not self._fifo_enabled() or not positions:
            return positions
        return sorted(
            positions,
            key=lambda p: (
                self._position_fifo_time(task, p),
                *self.warehouse_core.get_position_column_preference_key(p.aisle, p.column),
                p.level,
            ),
        )


    def _filter_outbound_tasks(self, outbound_tasks: List[TaskData]) -> List[TaskData]:
        """筛选当前可参与优化的出库组。

        显式 ``group_idx`` 优先；没有显式组号时尝试从标准任务号中的
        ``OUTBOUND_PL*_GP*`` 解析。无法解析的外部任务号保留，兼容不带
        生产计划元数据的调用。

        Args:
            outbound_tasks: 出库任务列表

        Returns:
            filtered_outbound_tasks: 当前可参与优化的出库任务列表
        """
        filtered_outbound_tasks = []
        for task in outbound_tasks:
            production_line = task.production_line
            current_group_idx = self.warehouse_core.production_line_current_group[production_line]
            explicit_group_idx = getattr(task, "group_idx", None)
            if explicit_group_idx is not None:
                try:
                    if int(explicit_group_idx) == current_group_idx:
                        filtered_outbound_tasks.append(task)
                    continue
                except (TypeError, ValueError):
                    pass
            try:
                parts = task.task_id.split('_')
                if len(parts) >= 3 and parts[0] == 'OUTBOUND' and parts[1].startswith('PL') and parts[2].startswith('GP'):
                    task_group_number = int(parts[2][2:])
                    task_group_idx = task_group_number - 1
                    if task_group_idx == current_group_idx:
                        filtered_outbound_tasks.append(task)
                else:
                    filtered_outbound_tasks.append(task)
            except (ValueError, IndexError):
                filtered_outbound_tasks.append(task)

        return filtered_outbound_tasks

    def _compute_feasible_assignments(self, outbound_tasks: List[TaskData],
                                     inbound_tasks: List[TaskData]) -> Dict[str, List[Tuple]]:
        """
        计算每个任务的可行巷道和位置。

        出库候选必须同时满足 SKU 属性匹配、双梁上下层配对和位置未被
        其他任务预留；入库候选则调用货位分配器，以保证优化器和入库
        策略使用相同的左右侧、禁用、预占与层位规则。

        Args:
            outbound_tasks: 出库任务列表
            inbound_tasks: 入库任务列表

        Returns:
            task_feasible_assignments: 各个任务的可行巷道和位置列表
        """
        task_feasible_assignments = {}

        # 计算出库任务的可行巷道和位置
        for task in outbound_tasks:
            sku_ids = task.get_sku_ids()
            if not sku_ids:
                continue
            sku_attrs_map = {}
            for s in (task.skus or []):
                sku_dict = self._sku_entry_to_dict(s)
                sku_id = sku_dict.get("skuId")
                if sku_id and sku_id not in sku_attrs_map:
                    sku_attrs_map[sku_id] = self._extract_sku_attrs(sku_dict)

            feasible = []

            if len(sku_ids) == 1:
                # 单梁任务
                sku = sku_ids[0]
                positions_by_aisle = {}
                sku_attrs = sku_attrs_map.get(sku, {})
                for pos in self.inventory_manager.get_sku_positions(sku, only_available=True):
                    if not pos.matches_sku(sku, sku_attrs, self.warehouse_core.match_fields):
                        continue
                    if pos.aisle not in positions_by_aisle:
                        positions_by_aisle[pos.aisle] = []
                    positions_by_aisle[pos.aisle].append(pos)

                for aisle, positions in positions_by_aisle.items():
                    feasible.append((aisle, self._sort_positions_for_outbound(task, positions)))
            elif len(sku_ids) == 2:
                # 双梁任务
                sku1, sku1_quantity = task.skus[0].get('skuId'), int(task.skus[0].get('quantity') or 0)
                sku2, sku2_quantity = task.skus[1].get('skuId'), int(task.skus[1].get('quantity') or 0)
                attrs1 = sku_attrs_map.get(sku1, {})
                attrs2 = sku_attrs_map.get(sku2, {})

                positions_by_aisle = {}
                for pos in self.inventory_manager.inventory_positions:
                    if (pos.is_double_layer
                        and pos.matches_pair(sku1, attrs1, sku2, attrs2, self.warehouse_core.match_fields)):
                        if self.warehouse_core._is_position_reserved_for_other_task(pos, task.task_id):
                            continue
                        if pos.upper_quantity + pos.lower_quantity >= sku1_quantity + sku2_quantity:
                            if pos.aisle not in positions_by_aisle:
                                positions_by_aisle[pos.aisle] = []
                            positions_by_aisle[pos.aisle].append(pos)

                for aisle, positions in positions_by_aisle.items():
                    feasible.append((aisle, self._sort_positions_for_outbound(task, positions)))

            if feasible:
                task_feasible_assignments[task.task_id] = feasible

        # 计算入库任务的可行巷道和位置
        for task in inbound_tasks:
            target_aisle = task.assigned_aisle
            current_position = self.warehouse_core.current_position_by_aisle.get(target_aisle)

            if not target_aisle:
                continue
            if getattr(task, "positions", None):
                task_feasible_assignments[task.task_id] = [(target_aisle, list(task.positions))]
                continue

            # 获取该巷道的可用位置
            available_positions = [
                pos for pos in self.inventory_manager.inventory_positions
                if pos.aisle == target_aisle and pos.has_space()
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

    def _compute_aisle_task_count(
        self, running_tasks: Optional[Dict[str, TaskData]], current_time: float
    ) -> Dict[int, float]:
        """
        基于 running_tasks 计算每个巷道已有的剩余负载。

        未完成比例而不是简单任务个数被加入计数，目的是让即将完成的
        任务不会长期阻塞巷道，而刚开始执行的任务仍会显著影响候选排序。

        Args:
            running_tasks: 正在执行的任务列表
            current_time: 当前时间
        Returns:
            {aisle: task_count, ...}
        """
        aisle_task_count: Dict[int, float] = {aisle: 0.0 for aisle in self.aisles}
        if running_tasks:
            for task in running_tasks.values():
                if task.assigned_aisle is not None and task.assigned_aisle in aisle_task_count:
                    delivery_time = float(task.task_record.get('delivery_time', current_time) or current_time)
                    duration = float(task.task_record.get('duration', 0.0) or 0.0)
                    unfinished_task_percent = max(0.0, delivery_time - current_time) / duration if duration > 0 else 0.0
                    aisle_task_count[task.assigned_aisle] += unfinished_task_percent + 0.01
        return aisle_task_count

    # ========================================================================
    # 阶段处理：候选采样、连续可出库估算与评分
    # ========================================================================
    def _generate_solutions(self, outbound_tasks: List[TaskData],
                           inbound_tasks: List[TaskData],
                           task_feasible_assignments: Dict,
                           aisle_task_count: Dict[int, float],
                           current_time: float,
                           heuristic_future_ready: Dict[int, int]) -> Dict[str, Tuple]:
        """
        生成大量候选方案，并以任务序列内容去重。

        每个候选值包含按巷道分桶的 TaskData 列表和加入 running 负载后
        的最大巷道负载，后者仅用于初筛和加权采样，不是最终评分。

        Args:
            outbound_tasks: 待执行出库任务列表
            inbound_tasks: 待执行入库任务列表
            task_feasible_assignments: 所有可执行任务的可执行位置列表
            aisle_task_count: 当前巷道负载
            current_time: 当前仿真器时间，用于估计正在进行的任务还需要多长时间可以完成
            heuristic_future_ready: 各产线预计未来可连续执行的出库任务数量。

        Returns:
            solutions: 可执行的调度结果列表 {solution_key: (aisle_task_sequences, max_tasks_in_aisle), ...}
        """
        #混合所有任务
        all_tasks = list(outbound_tasks) + list(inbound_tasks)
        # 用 task_id 集合快速判断候选任务类型。
        outbound_task_ids = {task.task_id for task in outbound_tasks}
        solutions = {}

        # 预计算每个出库任务的空闲非阻塞巷道,且该出库任务处于当前group_idx的出库任务
        task_idle_unblocked_aisles = self._precompute_idle_unblocked_aisles(
            outbound_tasks, task_feasible_assignments, aisle_task_count, current_time
        )

        # 采样一个候选方案
        for i in range(self.num_samples):
            solution = self._sample_single_solution(
                all_tasks, outbound_task_ids, task_feasible_assignments,
                aisle_task_count, task_idle_unblocked_aisles, heuristic_future_ready
            )

            if solution is None:
                continue

            # 计算solution的key用于去重
            solution_key = self._solution_to_key(solution)

            if solution_key not in solutions:
                # 再加上aisle_task_count
                new_aisle_task_count = {aisle: len(tasks) + aisle_task_count[aisle] for aisle, tasks in solution.items()}

                # 计算单个巷道的最高任务数
                max_tasks = max(new_aisle_task_count.values())

                solutions[solution_key] = (solution, max_tasks)

        return solutions

    def _precompute_idle_unblocked_aisles(self, outbound_tasks: List[TaskData],
                                          task_feasible_assignments: Dict,
                                          aisle_task_count, current_time: float) -> Dict[str, List[Tuple]]:
        """
        预计算每个出库任务的空闲且非阻塞的可行巷道（只处理当前组的任务）

        Args:
            outbound_tasks: 待执行出库任务列表
            task_feasible_assignments: 所有可执行任务可执行位置列表
            aisle_task_count: 当前巷道负载
            current_time: 当前时间

        Returns:
            task_idle_unblocked：空闲且非阻塞的巷道列表 {task_id: [(aisle, positions), ...]}
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
                    # 去掉 'GP' 前缀。
                    task_group_number = int(parts[2][2:])
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
                is_blocked = self.warehouse_core.check_blockage(aisle, production_line, current_time=current_time)

                if is_idle and not is_blocked:
                    idle_unblocked.append((aisle, positions))

            task_idle_unblocked[task.task_id] = idle_unblocked

        return task_idle_unblocked

    def _calc_future_ready_outbound(self, solution: Dict[int, List[TaskData]]) -> Dict[int, int]:
        """预估各产线在当前库存下可连续执行的出库任务数量。
        用于在采样时倾向选择未来可连贯执行更多出库的产线。
        会先模拟当前解中的任务对属性库存桶的影响，再逐组扣减估算。

        属性说明：当前项目没有独立的 ``features`` 字段；SKU 特征由任务条目和库存
        位置的属性字典承载，字段集合来自 ``WarehouseCore.match_fields``。这些属性在
        ``InventoryPosition.matches_sku`` / ``matches_pair`` 中参与实际可执行性判断；
        本方法以相同的“任务中值为 ``None`` 时不限制该字段”的规则，将真实库存拆为
        ``skuId + match_fields`` 属性桶。出库任务和生产计划只会扣减属性匹配的库存桶，
        因此不同颜色、版本或其他配置属性相同 SKU 的库存不能在本估算中相互替代。

        局部变量：``inventory_buckets`` 是方案影响后的属性库存视图；``trial_buckets``
        是试算当前生产组时的临时副本；``ready`` 是在第一次资源不足前可连续放行的
        任务数。

        """
        # 输出：每条产线的连续可推进任务数量。
        ready_counts: Dict[int, int] = {}
        # 权威库存来源；本方法只读取，不回写。
        inv = self.warehouse_core.inventory_manager
        match_fields = list(getattr(self.warehouse_core, "match_fields", []) or [])
        # 每个桶的 attrs 仅保留 match_fields；_inbound_time 等运行字段不得影响可执行性估算。
        inventory_buckets: List[Dict[str, Any]] = []

        def make_attrs(raw_attrs: Any) -> Dict[str, Any]:
            """提取会参与 SKU 匹配的属性字段。

            Args:
                raw_attrs (Any): 任务 SKU 字典或货位层属性字典。

            Returns:
                Dict[str, Any]: 仅含 ``match_fields`` 的属性字典。
            """
            raw_attrs = raw_attrs or {}
            return {field: raw_attrs.get(field) for field in match_fields}

        def attrs_match(stored_attrs: Dict[str, Any], required_attrs: Dict[str, Any]) -> bool:
            """按 ``InventoryPosition._attrs_match`` 的规则比较库存桶和任务属性。

            Args:
                stored_attrs (Dict[str, Any]): 库存位置中实际保存的属性。
                required_attrs (Dict[str, Any]): 任务或计划要求的属性。

            Returns:
                bool: ``True`` 表示所有任务明确给出的匹配字段均与库存一致。
            """
            return all(
                required_attrs.get(field) is None or stored_attrs.get(field) == required_attrs.get(field)
                for field in match_fields
            )

        def add_bucket(buckets: List[Dict[str, Any]], sku_id: str,
                       attrs: Dict[str, Any], quantity: int) -> None:
            """将数量并入相同 SKU 和属性组合的虚拟库存桶。

            Args:
                buckets (List[Dict[str, Any]]): 要修改的属性库存桶列表。
                sku_id (str): 需要写入的 SKU 标识。
                attrs (Dict[str, Any]): SKU 在 ``match_fields`` 下的属性。
                quantity (int): 增加的数量；非正数不写入。

            Returns:
                None: 直接更新 ``buckets``。
            """
            if not sku_id or quantity <= 0:
                return
            for bucket in buckets:
                if bucket["sku_id"] == sku_id and bucket["attrs"] == attrs:
                    bucket["quantity"] += quantity
                    return
            buckets.append({"sku_id": sku_id, "attrs": dict(attrs), "quantity": quantity})

        def consume_bucket(buckets: List[Dict[str, Any]], sku_id: str,
                           attrs: Dict[str, Any], quantity: int) -> bool:
            """从满足任务属性的库存桶中扣减指定数量。

            未指定的匹配字段沿用 ``matches_sku`` 的通配语义，并优先使用同样缺省
            属性的库存桶，避免无属性约束的任务过早消耗特征更明确的库存。

            Args:
                buckets (List[Dict[str, Any]]): 要修改的属性库存桶列表。
                sku_id (str): 需要扣减的 SKU 标识。
                attrs (Dict[str, Any]): 任务要求的 ``match_fields`` 属性。
                quantity (int): 需要扣减的数量。

            Returns:
                bool: 库存足够并已完成扣减时返回 ``True``，否则不修改原桶并返回 ``False``。
            """
            remaining = quantity
            candidates = [
                bucket for bucket in buckets
                if bucket["sku_id"] == sku_id and attrs_match(bucket["attrs"], attrs)
            ]
            candidates.sort(
                key=lambda bucket: sum(
                    attrs.get(field) is None and bucket["attrs"].get(field) is not None
                    for field in match_fields
                )
            )
            if sum(bucket["quantity"] for bucket in candidates) < remaining:
                return False
            for bucket in candidates:
                deducted = min(bucket["quantity"], remaining)
                bucket["quantity"] -= deducted
                remaining -= deducted
                if remaining == 0:
                    break
            return True

        # 将真实货位展开为按层记录的属性库存桶；双层货位的上下层各自独立计数。
        for position in inv.inventory_positions:
            if position.is_double_layer:
                add_bucket(inventory_buckets, position.upper_sku, make_attrs(position.upper_attrs), position.upper_quantity)
                add_bucket(inventory_buckets, position.lower_sku, make_attrs(position.lower_attrs), position.lower_quantity)
            else:
                add_bucket(inventory_buckets, position.sku, make_attrs(position.sku_attrs), position.quantity)

        def task_sku_requirements(task: TaskData) -> List[Tuple[str, Dict[str, Any], int]]:
            """将任务 SKU 列表转换为属性敏感的数量需求。

            Args:
                task (TaskData): 待模拟的入库或出库任务。

            Returns:
                List[Tuple[str, Dict[str, Any], int]]: ``(sku_id, attrs, quantity)`` 列表。
            """
            requirements = []
            for sku_entry in getattr(task, "skus", []) or []:
                sku_dict = self._sku_entry_to_dict(sku_entry)
                sku_id = sku_dict.get("skuId") or sku_dict.get("sku")
                if sku_id:
                    requirements.append((sku_id, make_attrs(sku_dict), int(sku_dict.get("quantity", 1) or 1)))
            return requirements

        def plan_task_sku_requirements(
            production_line: int,
            group_index: int,
            task_index: int,
            plan_task: Any,
        ) -> List[Tuple[str, Dict[str, Any], int]]:
            """将生产计划中的列表任务转换为与库存桶一致的属性需求。

            ``production_plan`` 的常见结构是 ``List[List[str]]``，属性不在该列表
            内，而平行存储于 ``production_plan_attrs``。因此不能把计划任务直接交给
            ``task_sku_requirements``；必须按组、任务和 SKU 下标还原属性。

            Args:
                production_line (int): 计划所属产线。
                group_index (int): 计划组下标。
                task_index (int): 组内任务下标。
                plan_task (Any): SKU 列表或兼容的 TaskData 对象。

            Returns:
                List[Tuple[str, Dict[str, Any], int]]: 属性敏感的 SKU 数量需求。
            """
            if hasattr(plan_task, 'skus'):
                return task_sku_requirements(plan_task)
            raw_skus = list(plan_task or [])
            plan_attrs = self.warehouse_core._build_attrs_for_task_from_plan(
                production_line, group_index, task_index, raw_skus
            )
            requirements = []
            for sku_index, raw_sku in enumerate(raw_skus):
                sku_dict = self._sku_entry_to_dict(raw_sku)
                sku_id = sku_dict.get('skuId') or sku_dict.get('sku')
                if not sku_id:
                    continue
                attrs = make_attrs(sku_dict)
                if sku_index < len(plan_attrs):
                    attrs.update(make_attrs(plan_attrs[sku_index]))
                requirements.append((sku_id, attrs, int(sku_dict.get('quantity', 1) or 1)))
            return requirements

        # 先模拟当前解对属性库存桶的影响：出库仅扣减匹配属性；入库带入其任务属性。
        for tasks in solution.values():
            for t in tasks:
                if t.task_type == TASK_TYPE_OUTBOUND:
                    for sku_id, attrs, quantity in task_sku_requirements(t):
                        consume_bucket(inventory_buckets, sku_id, attrs, quantity)
                elif t.task_type == TASK_TYPE_INBOUND:
                    for sku_id, attrs, quantity in task_sku_requirements(t):
                        add_bucket(inventory_buckets, sku_id, attrs, quantity)

        # {产线: [生产组, ...]}。
        plan = getattr(self.warehouse_core, "production_plan", {}) or {}
        # 每条产线当前组下标。
        current_group_idx = getattr(self.warehouse_core, "production_line_current_group", {}) or {}

        for pl, groups in plan.items():
            start_idx = current_group_idx.get(pl, 0)
            # 每条产线使用独立副本进行连续推进估算。
            line_buckets = [dict(bucket) for bucket in inventory_buckets]
            # 从 start_idx 起连续满足库存的任务累计数。
            ready = 0

            for group_offset, group in enumerate(groups[start_idx:]):
                group_index = start_idx + group_offset
                # 生产组必须整体成立；先在副本试扣，成功后才提交给本产线的后续组。
                trial_buckets = [dict(bucket) for bucket in line_buckets]
                feasible = True
                for task_index, plan_task in enumerate(group):
                    for sku_id, attrs, quantity in plan_task_sku_requirements(
                        pl, group_index, task_index, plan_task
                    ):
                        if not consume_bucket(trial_buckets, sku_id, attrs, quantity):
                            feasible = False
                            break
                    if not feasible:
                        break
                if not feasible:
                    break

                line_buckets = trial_buckets
                ready += len(group)

            ready_counts[pl] = ready

        return ready_counts

    def _calculate_outbound_choice_bonus(self, solution: Dict[int, List[TaskData]]) -> float:
        """非 FIFO 场景下，出库任务在已选巷道内可直接出库的位置越多，给予更小的评分。

        """
        if self._fifo_enabled():
            return 0.0

        bonus = 0.0
        for tasks in solution.values():
            for task in tasks:
                if getattr(task, "task_type", None) != TASK_TYPE_OUTBOUND:
                    continue
                choice_count = int(getattr(task, "choice_count", 1) or 1)
                if choice_count > 1:
                    bonus -= self.outbound_choice_bonus_weight * math.log1p(choice_count - 1)
        return bonus

    def _get_inbound_urgency(self, task: TaskData, threshold: int = 2) -> int:
        """查找所有产线生产计划中何时会需要该入库任务的SKU集合（或其mate），距离当前组越近越紧急。
        返回距离当前组的组数，找不到则返回很大值。

        """
        task_entries = [self._sku_entry_to_dict(s) for s in (getattr(task, 'skus', []) or [])]
        required_skus = [entry for entry in task_entries if entry.get('skuId')]
        if not required_skus:
            return 999999

        # 保持原有单梁语义：单梁入库既要看自身，也要看其配对梁何时共同进入计划；
        # 同时把任务条目的 match_fields 作为匹配条件，避免同 SKU 的不同属性误判为紧急。
        required_pairs = [
            (entry['skuId'], self._extract_sku_attrs(entry))
            for entry in required_skus
        ]
        if len(required_pairs) == 1:
            mate = getattr(self.warehouse_core, "sku_pairs", {}).get(required_pairs[0][0])
            if mate:
                required_pairs.append((mate, dict(required_pairs[0][1])))

        plan = getattr(self.warehouse_core, "production_plan", {}) or {}
        best = 999999

        # 遍历所有产线的计划
        for pl, groups in plan.items():
            # 获取该产线当前的组索引
            current_idx = self.warehouse_core.production_line_current_group.get(pl, 0)

            # 检查从当前组开始的后续组
            for idx, group in enumerate(groups[current_idx:], start=current_idx):
                for task_idx, task_item in enumerate(group):
                    if hasattr(task_item, 'skus'):
                        plan_entries = [self._sku_entry_to_dict(s) for s in (task_item.skus or [])]
                    else:
                        raw_skus = list(task_item or [])
                        plan_attrs = self.warehouse_core._build_attrs_for_task_from_plan(
                            pl, idx, task_idx, raw_skus
                        )
                        plan_entries = []
                        for sku_idx, raw_sku in enumerate(raw_skus):
                            entry = self._sku_entry_to_dict(raw_sku)
                            if sku_idx < len(plan_attrs):
                                entry.update(plan_attrs[sku_idx])
                            plan_entries.append(entry)

                    # 每个所需 SKU 必须在同一计划任务中找到一个未被重复使用、且属性
                    # 不冲突的条目，保持原来的“集合整体出现”判断。
                    used_indexes = set()
                    is_needed = True
                    for required_sku, required_attrs in required_pairs:
                        matched_index = next(
                            (
                                entry_index for entry_index, entry in enumerate(plan_entries)
                                if entry_index not in used_indexes
                                and entry.get('skuId') == required_sku
                                and all(
                                    required_attrs.get(field) is None
                                    or entry.get(field) is None
                                    or entry.get(field) == required_attrs.get(field)
                                    for field in getattr(self.warehouse_core, 'match_fields', []) or []
                                )
                            ),
                            None,
                        )
                        if matched_index is None:
                            is_needed = False
                            break
                        used_indexes.add(matched_index)
                    if is_needed:
                        best = min(best, idx - current_idx)
                        break
                if best <= threshold:
                    # 如果已找到足够近的匹配，则提前退出
                    break
            if best <= threshold:
                # 如果已找到足够近的匹配，则提前退出所有产线循环
                break

        return best

    def _sample_single_solution(self, all_tasks: List[TaskData],
                               outbound_task_ids: Set[str],
                               task_feasible_assignments: Dict,
                               aisle_task_count: Dict[int, float],
                               task_idle_unblocked_aisles: Dict[str, List[Tuple]],
                               heuristic_future_ready: Dict[int, int]) -> Dict[int, List[TaskData]]:
        """
        生成一个随机候选方案

        Args:
            all_tasks: 所有任务列表
            outbound_task_ids: 出库任务列表
            task_feasible_assignments: 任务可执行巷道及位置
            aisle_task_count: 巷道任务数
            task_idle_unblocked_aisles: 任务空闲且未阻塞的巷道
            heuristic_future_ready: 未来可连续完成出库任务数

        Returns:
            aisle_task_sequences：Dict[int, List[TaskData]]: 随机生成的候选方案

        局部变量：
            aisle_task_sequences: 当前采样方案的 ``aisle -> 新 TaskData 序列``。
            local_task_count: 候选内部可变巷道负载，基于 running 负载拷贝而来。
            current_group_tasks: 具备空闲非阻塞巷道的当前组出库任务，优先放入方案。
            other_outbound_tasks_list / other_inbound_tasks_list: 其余出库/入库候选。
            inbound_urgencies: 入库任务到最近需求组的距离；mixed_tasks_list: 非当前组任务的
            随机交织顺序；selected_position: 当前任务最终写入新 TaskData 的位置或位置列表。

        """

        aisle_task_sequences = {aisle: [] for aisle in self.aisles}

        # 复制 aisle_task_count 用于内部更新
        local_task_count = aisle_task_count.copy()

        # 优先分配current_group_tasks, 然后分配其他的
        current_group_task_ids = [task for task in task_idle_unblocked_aisles.keys()]
        current_group_tasks = [task for task in all_tasks if task.task_id in current_group_task_ids]

        # 分离出库和入库任务
        other_outbound_tasks_list = [task for task in all_tasks if (task.task_id not in current_group_task_ids and task.task_id in outbound_task_ids)]
        other_inbound_tasks_list = [task for task in all_tasks if (task.task_id not in current_group_task_ids and task.task_id not in outbound_task_ids)]

        # 获取当前各产线的进度百分比
        line_progress = self._get_line_progress_percentage()

        # 根据heuristic_future_ready和line_progress对outbound任务进行综合排序
        if heuristic_future_ready:
            def get_ready_count(task):
                """返回任务未来可连续执行的出库任务数

                Args:
                    task (Any): 当前处理的任务记录。

                Returns:
                    Any: 当前处理流程产生的结果；具体结构由函数摘要说明。
                """
                return heuristic_future_ready.get(task.production_line, 0) if hasattr(task, 'production_line') and task.production_line else 0

            def get_progress(task):
                """返回当前产线的进度百分比

                Args:
                    task (Any): 当前处理的任务记录。

                Returns:
                    Any: 当前处理流程产生的结果；具体结构由函数摘要说明。
                """
                return line_progress.get(task.production_line, 0) if hasattr(task, 'production_line') and task.production_line else 0

            # 综合考虑：未来可执行任务数多的优先，但进度慢的产线需要额外加权
            # 使用 -progress 加大权重，使进度慢的任务排序更靠前；作用于所有出库任务
            sort_key = lambda t: get_ready_count(t) - 5.0 * get_progress(t)
            current_group_tasks.sort(key=sort_key, reverse=True)
            other_outbound_tasks_list.sort(key=sort_key, reverse=True)

        # 打乱入库任务顺序
        random.shuffle(other_inbound_tasks_list)

        # 紧急入库：若某产线即将需要该 SKU 集合（距离当前组 <= 阈值[inbound_urgency_threshold]），提前提升优先级
        inbound_urgencies = {}
        for task in other_inbound_tasks_list:
            inbound_urgencies[task.task_id] = self._get_inbound_urgency(task, self.inbound_urgency_threshold)
        # 获取紧急入库的紧急程度
        urgent_inbound_tasks = [t for t in other_inbound_tasks_list if inbound_urgencies.get(t.task_id, 999999) <= self.inbound_urgency_threshold]
        # 按紧急程度排序
        urgent_inbound_tasks.sort(key=lambda t: inbound_urgencies.get(t.task_id, 999999))
        other_inbound_tasks_list = [t for t in other_inbound_tasks_list if t not in urgent_inbound_tasks]

        # 将非紧急入库和非当前组出库交错排列，作为本次采样的候选处理顺序。
        # ``current_group_tasks`` 不参与随机混排，会在下面优先处理，确保当前生产组不会被未来组或普通入库任务抢占可行巷道。
        # 本次样本中位于当前组任务之后的交错候选序列。
        mixed_tasks_list = []
        # 下一个待插入普通入库任务在 other_inbound_tasks_list 中的下标。
        inbound_idx = 0
        # 下一个待插入后续出库任务在 other_outbound_tasks_list 中的下标。
        outbound_idx = 0
        # 保证所有任务轮询完
        while inbound_idx < len(other_inbound_tasks_list) or outbound_idx < len(other_outbound_tasks_list):
            if inbound_idx >= len(other_inbound_tasks_list):
                # 入库序列已耗尽，只能顺序追加剩余出库任务，避免遗漏候选。
                mixed_tasks_list.append(other_outbound_tasks_list[outbound_idx])
                outbound_idx += 1
            elif outbound_idx >= len(other_outbound_tasks_list):
                # 出库序列已耗尽，只能顺序追加剩余入库任务，避免遗漏候选。
                mixed_tasks_list.append(other_inbound_tasks_list[inbound_idx])
                inbound_idx += 1
            elif random.random() < 0.8:
                # 80% 概率插入一条入库，同时保留每次采样的序列差异。
                mixed_tasks_list.append(other_inbound_tasks_list[inbound_idx])
                inbound_idx += 1
            else:
                # 以20% 概率插入一条出库，探索不同的巷道负载与任务交织方式。
                mixed_tasks_list.append(other_outbound_tasks_list[outbound_idx])
                outbound_idx += 1

        # 固定优先级：当前生产组 -> 本轮随机混排任务。循环只构造候选方案，
        # ``local_task_count``、``aisle`` 和 ``positions`` 均为局部视图，不写回核心状态。
        for task in current_group_tasks + mixed_tasks_list:
            # ``task_feasible_assignments`` 已预先完成 SKU 属性、双层配对、禁用/预留
            # 货位和目标巷道约束筛选；缺少条目说明该任务不能加入本轮候选方案
            if task.task_id not in task_feasible_assignments:
                continue

            # [(aisle, positions), ...]，每项是一种合法执行方案。
            feasible = task_feasible_assignments[task.task_id]
            if not feasible:
                continue

            # 决定是否优先避开当前运行或拥堵巷道。
            is_outbound = task.task_id in outbound_task_ids
            # 本次采样为任务选定的巷道；None 表示尚未从合法候选中选择。
            aisle = None
            # 与选定巷道绑定的原始货位方案，单梁可能有多个候选，双梁为配对位置集合。
            positions = None

            # 筛选出库任务，优先选择空闲且非阻塞的巷道
            if is_outbound:
                idle_unblocked = task_idle_unblocked_aisles.get(task.task_id, [])
                # 筛选出当前 local_task_count (已分配任务)仍为0的巷道
                current_idle_unblocked = [
                    (a, p) for a, p in idle_unblocked if local_task_count.get(a, 0) == 0
                ]
                # 如果有可用的空闲巷道，则优先选择
                if current_idle_unblocked:
                    # 使用加权选择而非随机选择：优先选择可连续执行任务数多的产线对应的巷道
                    weights = []
                    for a, p in current_idle_unblocked:
                        # 仅按照巷道当前负载做权重：越空闲越优先
                        aisle_weight = 1.0 / (1.0 + local_task_count.get(a, 0))
                        weights.append(aisle_weight)

                    # 根据权重选择巷道
                    selected_idx = random.choices(range(len(current_idle_unblocked)), weights=weights, k=1)[0]
                    aisle, positions = current_idle_unblocked[selected_idx]

            # 如果没有找到空闲非阻塞巷道，只能在仍未阻塞的可行巷道中回退选择。
            # 不能重新使用原 ``feasible``，否则下游持续拥堵的产线会被放回候选方案，
            # 最终在事件执行阶段无法启动，导致评分结果与实际可执行性不一致。
            if aisle is None:
                fallback_feasible = feasible
                if is_outbound:
                    production_line = task.production_line
                    fallback_feasible = [
                        (candidate_aisle, candidate_positions)
                        for candidate_aisle, candidate_positions in feasible
                        if not self.warehouse_core.check_blockage(
                            candidate_aisle,
                            production_line,
                            current_time=self.warehouse_core.current_time,
                        )
                    ]
                if not fallback_feasible:
                    continue
                # 加权选择巷道：任务数越少的巷道权重越高
                weights = []
                for a, _ in fallback_feasible:
                    base_w = 1.0 / (1.0 + local_task_count.get(a, 0))
                    weights.append(base_w)
                selected_idx = random.choices(range(len(fallback_feasible)), weights=weights, k=1)[0]
                aisle, positions = fallback_feasible[selected_idx]

            if positions:
                # 由于分配器已经返回确定的位置方案，直接使用即可
                # 对于双SKU任务保留完整的货位列表
                choice_count = len(positions) if is_outbound and isinstance(positions, list) else 1
                if (not is_outbound) and isinstance(positions, list) and len(getattr(task, "skus", []) or []) > 1:
                    selected_position = positions
                elif isinstance(positions, list):
                    # 单SKU任务只需要一个位置，取第一个即可
                    selected_position = positions[0] if positions else None
                else:
                    selected_position = positions
            else:
                selected_position = None
                choice_count = 1

            # 任务没有选择到可用位置（出库任务无梁/入库位置已满）
            if selected_position is None:
                continue

            # 构造TaskData
            skus_list = task.skus if task.skus else [{'skuId': sid} for sid in task.get_sku_ids()]
            task_type = TASK_TYPE_OUTBOUND if is_outbound else TASK_TYPE_INBOUND

            # 构造新任务对象，保留原任务的属性，并添加分配的巷道和位置
            """
            Args:
                task_id (str): 任务ID
                task_type (str): 任务类型
                task_name (str): 任务名称
                skus (List[Dict]): 任务包含的SKU列表
                production_line (str): 任务所属生产线
                assigned_aisle (str): 任务分配的巷道
                assigned_time (float): 任务分配的时间
                positions (List[Union[str, Tuple]]): 任务分配的货位或货位对列表
                task_record (Dict): 任务的记录信息
            """
            new_task = TaskData(
                task_id=task.task_id,
                task_type=task_type,
                task_name=getattr(task, 'task_name', task.task_id),
                skus=skus_list,
                production_line=task.production_line,
                assigned_aisle=aisle,
                assigned_time=getattr(task, 'assigned_time', 0),
                positions=[selected_position] if not isinstance(selected_position, list) else selected_position,
                task_record=task.task_record
            )
            new_task.plan_id = getattr(task, "plan_id", None)
            new_task.plan_index_public = getattr(task, "plan_index_public", None)
            new_task.group_idx = getattr(task, "group_idx", None)
            if is_outbound:
                new_task.choice_count = choice_count

            # 添加到巷道任务序列中
            aisle_task_sequences[aisle].append(new_task)

            # 更新 local_task_count
            local_task_count[aisle] = local_task_count.get(aisle, 0) + 1

        # 仅将紧急入库任务提前，其余任务保持原有顺序
        urgent_inbound_ids = {t.task_id for t in urgent_inbound_tasks} if 'urgent_inbound_tasks' in locals() else set()
        for a, tasks in aisle_task_sequences.items():
            urgent_inbound = [t for t in tasks if t.task_type == TASK_TYPE_INBOUND and t.task_id in urgent_inbound_ids]
            others = [t for t in tasks if not (t.task_type == TASK_TYPE_INBOUND and t.task_id in urgent_inbound_ids)]
            if urgent_inbound:
                aisle_task_sequences[a] = urgent_inbound + others

        return aisle_task_sequences

    def _solution_to_key(self, solution: Dict[int, List[TaskData]]) -> str:
        """将solution转换为字符串key用于去重

        """
        key_parts = []
        for aisle in sorted(solution.keys()):
            tasks = solution[aisle]
            task_ids = [task.task_id for task in tasks]
            key_parts.append(f"{aisle}:{','.join(task_ids)}")
        return '|'.join(key_parts)

    def _evaluate_and_select_best(
        self,
        solutions: Dict[str, Tuple],
        heuristic_solution: Dict[int, List[TaskData]],
        heuristic_future_ready: Optional[Dict[int, int]] = None,
    ) -> Dict[int, List[TaskData]]:
        """
        根据候选负载进行加权抽样，并通过完整仿真评分选择最优方案。

        前五个高权重方案强制进入评分集合，其余按权重随机补充；每个
        方案调用 ``WarehouseCore.get_sol_score``，再叠加产线推进均衡
        惩罚和非 FIFO 出库选择灵活度奖励，最后与启发式基线比较。

        Args:
            solutions: {solution_key: (aisle_task_sequences, max_tasks), ...}
            heuristic_solution: 启发式解决方案
            heuristic_future_ready: 启发式方案中各产线可连续执行的出库任务数
        Returns:
            best_solution: 最优的aisle_task_sequences

        局部变量：
            solutions_list: 去除字符串键后的候选值列表；max_tasks_values: 每方案最大巷道负载。
            line_priority_scores: 方案推进高连续性产线的偏好得分；line_balance_scores: 产线
            进度方差惩罚；weights: 两者与负载合成后的抽样权重。
            selected_solutions: 必选高权重方案与无放回加权抽样方案的集合。
            best_score / best_solution: 当前已评分候选中的最小总分及其序列。
        """
        solutions_list = list(solutions.values())

        # 计算采样权重：以巷道负载为基准，加上产线进度与future_ready的偏好
        max_tasks_values = [max_tasks for _, max_tasks in solutions_list]

        # 预先计算每个方案的“优先产线”得分与产线进度平衡度，便于加权
        # 优先产线得分：按方案中出库任务所属产线累计 heuristic_future_ready 的值，鼓励让可连续执行更多任务的产线先被推进
        line_priority_scores = []
        line_balance_scores = []
        for solution, _ in solutions_list:
            priority = 0.0
            for tasks in solution.values():
                for t in tasks:
                    if getattr(t, "task_type", None) == TASK_TYPE_OUTBOUND and getattr(t, "production_line", None):
                        priority += (heuristic_future_ready or {}).get(t.production_line, 0)
            line_priority_scores.append(priority)
            # _calculate_line_progress_balance_score返回的是方差*100，数值越小越平衡
            line_balance_scores.append(self._calculate_line_progress_balance_score(solution))
        # 计算最大优先产线得分，后期将相关得分压缩到[0, 1]区间，例如[1,2,3,4,5]，可以用最大值{5}，归一化为[0.2,0.4,0.6,0.8,1.0]，避免过大差异导致权重失衡
        max_priority = max(line_priority_scores) if line_priority_scores else 0

        weights = []
        # 计算每个方案的权重
        for idx, max_tasks in enumerate(max_tasks_values):
            # 基础权重：巷道任务越多，越小权重
            base_w = 1.0 / (1.0 + max_tasks) ** 2
            # 优先产线得分归一化，越高越优先
            ready_norm = (line_priority_scores[idx] / max_priority) if max_priority > 0 else 0.0
            balance_penalty = line_balance_scores[idx]
            # 进度方差惩罚
            # 越平衡越接近1，越不平衡越被压低
            balance_factor = 1.0 / (1.0 + 0.01 * balance_penalty)
            # future_ready越大，采样权重略微提升
            ready_factor = 1.0 + 0.5 * ready_norm
            # 具体使用的权重
            weights.append(base_w * ready_factor * balance_factor)

        # 根据权重对方案进行排序，得到索引
        indexed_weights = [(i, weights[i]) for i in range(len(weights))]
        # 按权重降序排列
        indexed_weights.sort(key=lambda x: x[1], reverse=True)

        # 确定采样数量
        num_to_evaluate = min(self.num_evaluate, len(solutions_list))

        # 确保前5个（或如果总数不足5个则全部）评分最高的方案被选中
        top_5_count = min(5, num_to_evaluate)
        selected_solutions = []

        # 添加前top_5_count个评分最高的方案
        for i in range(top_5_count):
            idx = indexed_weights[i][0]
            selected_solutions.append(solutions_list[idx])

        # 从剩余方案中随机选择 10 个
        remaining_indices = [indexed_weights[i][0] for i in range(top_5_count, len(indexed_weights))]
        remaining_solutions = [solutions_list[i] for i in remaining_indices]

        additional_count = num_to_evaluate - top_5_count
        if additional_count > 0 and len(remaining_solutions) > 0:
            # 计算剩余方案的权重用于随机选择
            remaining_weights = [weights[i] for i in remaining_indices]

            # 使用加权随机采样（不重复）选择额外的方案
            additional_solutions = []
            remaining_indices_copy = list(range(len(remaining_solutions)))
            remaining_weights_copy = remaining_weights.copy()

            for _ in range(min(additional_count, len(remaining_solutions))):
                if not remaining_indices_copy:
                    break

                # 归一化概率
                total_weight = sum(remaining_weights_copy)
                if total_weight > 0:
                    normalized_weights = [w / total_weight for w in remaining_weights_copy]
                else:
                    # 如果所有权重都是0，使用均匀分布
                    normalized_weights = [1.0 / len(remaining_weights_copy)] * len(remaining_weights_copy)

                # 选择一个
                selected_idx = random.choices(remaining_indices_copy, weights=normalized_weights, k=1)[0]
                list_idx = remaining_indices_copy.index(selected_idx)

                additional_solutions.append(remaining_solutions[selected_idx])
                remaining_indices_copy.pop(list_idx)
                remaining_weights_copy.pop(list_idx)

            selected_solutions.extend(additional_solutions)

        print(f"[优化器]其中前{top_5_count}个为评分最高的方案，额外随机选择{len(selected_solutions) - top_5_count}个方案")


        # 将筛选后的方案带入仿真内部评分计算最优方案
        best_score = float('inf')
        best_solution = heuristic_solution

        for idx, (solution, max_tasks) in enumerate(selected_solutions, 1):
            # 计算基础得分
            base_score, base_details = self.warehouse_core.get_sol_score(solution)

            # 计算产线进度平衡性得分（产线进度越平衡越好）
            line_progress_balance_score = self._calculate_line_progress_balance_score(solution)
            # 计算出库选择灵活度奖励（出库可选的位置越多越好）
            outbound_choice_bonus = self._calculate_outbound_choice_bonus(solution)

            # 综合得分 = 基础得分 + 进度平衡性惩罚 + 出库选择灵活度奖励
            score = base_score + line_progress_balance_score + outbound_choice_bonus
            extra_terms = [("line_progress_balance_penalty", line_progress_balance_score)]
            if outbound_choice_bonus:
                extra_terms.append(("outbound_choice_bonus", outbound_choice_bonus))
            # 避免全空方案被选中
            if all(len(tasks) == 0 for tasks in solution.values()):
                score += 10000.0
                extra_terms.append(("empty_solution_penalty", 10000.0))
            print(f"[优化器]候选方案 #{idx}: max_tasks={max_tasks}")
            for line in self.warehouse_core.format_score_breakdown(
                base_details,
                extra_terms=extra_terms,
            ):
                print(line)
            if score < best_score:
                best_score = score
                best_solution = solution

        # 和DMW的方案得分结果对比
        heuristic_score, heuristic_details = self.warehouse_core.get_sol_score(heuristic_solution)
        heuristic_line_progress_balance_score = self._calculate_line_progress_balance_score(heuristic_solution)
        heuristic_outbound_choice_bonus = self._calculate_outbound_choice_bonus(heuristic_solution)
        heuristic_score += heuristic_line_progress_balance_score + heuristic_outbound_choice_bonus
        heuristic_extra_terms = [("line_progress_balance_penalty", heuristic_line_progress_balance_score)]
        if heuristic_outbound_choice_bonus:
            heuristic_extra_terms.append(("outbound_choice_bonus", heuristic_outbound_choice_bonus))
        if all(len(tasks) == 0 for tasks in heuristic_solution.values()):
            heuristic_score += 10000.0
            heuristic_extra_terms.append(("empty_solution_penalty", 10000.0))
        print("[优化器]heuristic 基线方案评分:")
        for line in self.warehouse_core.format_score_breakdown(
            heuristic_details,
            extra_terms=heuristic_extra_terms,
        ):
            print(line)
        if heuristic_score < best_score:
            score_diff = best_score - heuristic_score
            best_score = heuristic_score
            best_solution = heuristic_solution
            print(f"heuristic_solution is better than selected_solutions, + {score_diff:.2f}")

        print(f"[优化器]最优方案得分: {best_score:.2f}, 最优方案:{best_solution}")

        return best_solution

    """========================================以下为辅助函数=================================================================="""

    def _is_production_line_fully_blocked(self, production_line: int) -> bool:
        """判断一条产线是否在所有可服务巷道上均被下游拥堵阻塞。

        Args:
            production_line: 需要判断的生产线编号。

        Returns:
            当该产线不存在任何未阻塞的可服务巷道时返回 ``True``；未配置服务范围时，
            按全部巷道判断。该结果仅用于评分，不改变拥堵状态。
        """
        aisle_mapping = getattr(self.warehouse_core, "aisle_production_line_mapping", {}) or {}
        service_aisles = [
            aisle
            for aisle in self.aisles
            if production_line in (aisle_mapping.get(aisle, []) or [])
        ]
        if not service_aisles:
            service_aisles = list(self.aisles)
        if not service_aisles:
            return False
        return all(
            self.warehouse_core.check_blockage(
                aisle,
                production_line,
                current_time=self.warehouse_core.current_time,
            )
            for aisle in service_aisles
        )

    def _calculate_line_progress_balance_score(self, solution: Dict[int, List[TaskData]]) -> float:
        """计算方案执行后各产线推进比例的方差惩罚。

        该项只用于多个可行方案之间的择优，不会替代生产组顺序和库存
        配对等硬约束；只有存在两条及以上有计划产线时才产生惩罚。
        方差越小，平衡性越好，得分越低

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

        # 计算各产线在当前方案下的预期进度。持续下游拥堵使某条产线无法在
        # 任一可服务巷道推进时，不把它视作可由本轮调度修复的“失衡”来源，
        # 否则会错误惩罚其他仍能执行出库的产线。
        expected_progress = {}
        for pl in range(1, self.warehouse_core.num_production_lines + 1):
            total_groups = len(self.warehouse_core.production_plan.get(pl, []))
            if total_groups > 0 and not self._is_production_line_fully_blocked(pl):
                current_progress_val = current_progress.get(pl, 0)
                additional_tasks = outbound_tasks_by_line.get(pl, 0)
                expected_progress[pl] = (current_progress_val * total_groups + additional_tasks) / total_groups

        if len(expected_progress) <= 1:
            # 只有一个或没有产线，无需平衡
            return 0.0

        # 计算进度的方差，作为平衡性得分
        progress_values = list(expected_progress.values())
        mean_progress = sum(progress_values) / len(progress_values)
        variance = sum((p - mean_progress) ** 2 for p in progress_values) / len(progress_values)

        # 将方差放大作为平衡性惩罚，方差越小越好
        # 放大系数可调整
        return variance * 100

    # ========================================================================
    # 查询输出：调度耗时与产线进度指标
    # ========================================================================
    def get_average_solve_time(self) -> float:
        """获取平均求解时间

        Args:
            None: 无显式业务参数；使用实例状态或模块配置。

        """
        if self.solve_count == 0:
            return 0.0
        return self.total_time / self.solve_count

    def _get_line_progress_percentage(self) -> Dict[int, float]:
        """获取每个产线的当前进度百分比

        Returns:
            {production_line: progress_percentage, ...} 进度百分比（0-1之间的小数）

        Args:
            None: 无显式业务参数；使用实例状态或模块配置。
        """
        progress_percentage = {}

        for pl in range(1, self.warehouse_core.num_production_lines + 1):
            total_groups = len(self.warehouse_core.production_plan.get(pl, []))
            if total_groups <= 0:
                continue

            current_group_idx = self.warehouse_core.production_line_current_group.get(pl, 0)
            progress_percentage[pl] = current_group_idx / total_groups if total_groups > 0 else 0.0

        return progress_percentage
