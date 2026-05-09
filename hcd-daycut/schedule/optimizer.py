"""
调度优化器模块
包含启发式调度器和基于采样的优化调度器
"""

import math
import time
from typing import Any, Dict, List, Tuple, Set
from simulation.task_data import TaskData, TASK_TYPE_INBOUND, TASK_TYPE_OUTBOUND
from schedule.heuristic import HeuristicScheduler
import random


class OptimizationScheduler:
    """基于采样优化的调度器"""
    
    def __init__(self, warehouse_core, num_samples=100, num_evaluate=15):
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
        # 非 FIFO 场景下，出库任务在选定巷道内可直接出库的位置越多，分数越小
        self.outbound_choice_bonus_weight = float(
            getattr(warehouse_core, "outbound_choice_bonus_weight", 0.2)
        )
        
        # 入库货位分配器
        self.position_allocator = None

        # heuristic scheduler
        self.heuristic_scheduler = HeuristicScheduler(warehouse_core)

    def _sku_entry_to_dict(self, sku):
        if isinstance(sku, dict):
            return dict(sku)
        if hasattr(sku, "model_dump"):
            return sku.model_dump()
        if hasattr(sku, "dict"):
            return sku.dict()
        return {"skuId": getattr(sku, "skuId", None), "quantity": getattr(sku, "quantity", 1)}

    def _extract_sku_attrs(self, sku_dict: dict) -> dict:
        match_fields = getattr(self.warehouse_core, "match_fields", [])
        if not match_fields:
            return {}
        return {k: sku_dict.get(k) for k in match_fields}

    def _fifo_enabled(self) -> bool:
        return bool(getattr(self.warehouse_core, "outbound_fifo_enabled", False))

    def _extract_position_inbound_time(self, pos, sku_id: str, attrs: dict) -> float:
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
        if not self._fifo_enabled() or not positions:
            return positions
        return sorted(
            positions,
            key=lambda p: (
                self._position_fifo_time(task, p),
                -p.column,
                p.level,
            ),
        )
            
    def solve(self, inbound_tasks: List[TaskData], outbound_tasks: List[TaskData], 
             running_tasks: List[TaskData] = None, current_time: float = 0.0) -> Dict[int, List[TaskData]]:
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

        self.heuristic_scheduler.position_allocator = self.position_allocator
        heuristic_solution = self.heuristic_scheduler.solve(inbound_tasks, outbound_tasks, running_tasks, current_time)

        # heuristic_solution如果全空/没有出库任务，则直接返回空方案
        if all(len(tasks) == 0 for tasks in heuristic_solution.values()):
            return heuristic_solution

        start_time = time.time()
        
        # 1. 筛选出库任务：只保留当前组的任务
        filtered_outbound_tasks = self._filter_outbound_tasks(outbound_tasks)
        
        # 2. 计算每个任务的可行巷道和位置
        task_feasible_assignments = self._compute_feasible_assignments(
            filtered_outbound_tasks, inbound_tasks
        )
        
        # 如果没有任务，直接返回空方案
        if not task_feasible_assignments:
            return {aisle: [] for aisle in self.aisles}
        
        # 3. 基于running_tasks计算每个巷道已有的任务数
        aisle_task_count = self._compute_aisle_task_count(running_tasks, current_time)
        
        # 4. 预先计算启发式方案的未来可执行任务数量
        heuristic_future_ready = self._calc_future_ready_outbound(heuristic_solution)
        
        # 5. 生成大量候选方案
        print(f"[优化器]开始生成{self.num_samples}个候选方案...")
        solutions = self._generate_solutions(
            filtered_outbound_tasks, inbound_tasks, task_feasible_assignments, aisle_task_count, current_time, heuristic_future_ready
        )
        
        if not solutions:
            print("[优化器]警告：未能生成任何可行方案，返回空方案")
            return {aisle: [] for aisle in self.aisles}
        
        print(f"[优化器]成功生成{len(solutions)}个不同的候选方案")
        
        # 5. 根据max_tasks进行加权采样，选择方案进行评分
        best_solution = self._evaluate_and_select_best(solutions, heuristic_solution, heuristic_future_ready)
        
        solve_time = time.time() - start_time
        self.total_time += solve_time
        self.solve_count += 1
        
        print(f"[优化器]调度完成，耗时{solve_time:.3f}秒")
        
        return best_solution if best_solution else {aisle: [] for aisle in self.aisles}
    
    def _filter_outbound_tasks(self, outbound_tasks: List[TaskData]) -> List[TaskData]:
        """筛选出库任务：只保留当前组的任务"""
        filtered_outbound_tasks = []
        for task in outbound_tasks:
            production_line = task.production_line
            current_group_idx = self.warehouse_core.production_line_current_group[production_line]
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
        计算每个任务的可行巷道和位置
        
        Returns:
            {task_id: [(aisle, [positions]), ...]}
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
                sku1, sku1_quantity = task.skus[0].get('skuId'), task.skus[0].get('quantity')
                sku2, sku2_quantity = task.skus[1].get('skuId'), task.skus[1].get('quantity')
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
    
    def _compute_aisle_task_count(self, running_tasks: List[TaskData], current_time: float) -> Dict[int, int]:
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
        all_tasks = list(outbound_tasks) + list(inbound_tasks)
        outbound_task_ids = {task.task_id for task in outbound_tasks}  # 用task_id集合加速查找
        solutions = {}
        
        # 预计算每个出库任务的空闲非阻塞巷道,且该出库任务处于当前group_idx的出库任务
        task_idle_unblocked_aisles = self._precompute_idle_unblocked_aisles(
            outbound_tasks, task_feasible_assignments, aisle_task_count, current_time
        )
        
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
                is_blocked = self.warehouse_core.check_blockage(aisle, production_line, current_time=current_time)
                
                if is_idle and not is_blocked:
                    idle_unblocked.append((aisle, positions))
            
            task_idle_unblocked[task.task_id] = idle_unblocked
        
        return task_idle_unblocked

    def _calc_future_ready_outbound(self, solution: Dict[int, List[TaskData]]) -> Dict[int, int]:
        """
        预估各产线在当前库存下可连续执行的出库任务数量（不考虑后续入库补充）。
        用于在采样时倾向选择未来可连贯执行更多出库的产线。
        会先模拟当前解中的任务对库存的影响，再进行估算。
        """
        ready_counts: Dict[int, int] = {}
        inv = self.warehouse_core.inventory_manager

        qty_cache: Dict[str, int] = {}

        def get_qty(sku: str) -> int:
            if sku not in qty_cache:
                qty_cache[sku] = inv.get_sku_total_quantity(sku)
            return qty_cache[sku]

        # 先根据当前解(solution)模拟库存变化：出库扣减，入库增加
        for tasks in solution.values():
            for t in tasks:
                if t.task_type == TASK_TYPE_OUTBOUND:
                    for s in getattr(t, "skus", []):
                        sid = s.get("skuId") if isinstance(s, dict) else None
                        if sid:
                            qty_cache[sid] = get_qty(sid) - 1
                elif t.task_type == TASK_TYPE_INBOUND:
                    for s in getattr(t, "skus", []):
                        sid = s.get("skuId") if isinstance(s, dict) else None
                        if sid:
                            qty_cache[sid] = get_qty(sid) + 1

        plan = getattr(self.warehouse_core, "production_plan", {}) or {}
        current_group_idx = getattr(self.warehouse_core, "production_line_current_group", {}) or {}

        for pl, groups in plan.items():
            start_idx = current_group_idx.get(pl, 0)
            qty_available: Dict[str, int] = {}
            ready = 0

            for group in groups[start_idx:]:
                demand: Dict[str, int] = {}
                for task in group:
                    skus = getattr(task, "skus", [])
                    for s in skus:
                        sid = s.get("skuId") if isinstance(s, dict) else None
                        if sid:
                            demand[sid] = demand.get(sid, 0) + 1

                feasible = True
                for sid, need in demand.items():
                    available = qty_available.get(sid, get_qty(sid))
                    if available < need:
                        feasible = False
                        break
                if not feasible:
                    break

                # 扣减模拟库存并累计可连续任务数
                for sid, need in demand.items():
                    qty_available[sid] = qty_available.get(sid, get_qty(sid)) - need
                ready += len(group)

            ready_counts[pl] = ready

        return ready_counts

    def _calculate_outbound_choice_bonus(self, solution: Dict[int, List[TaskData]]) -> float:
        """非 FIFO 场景下，出库任务在已选巷道内可直接出库的位置越多，给予更小的评分。"""
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
        """
        查找所有产线生产计划中何时会需要该入库任务的SKU集合（或其mate），距离当前组越近越紧急。
        返回距离当前组的组数，找不到则返回很大值。
        """
        try:
            skus = task.get_sku_ids()
        except Exception:
            skus = []
        if not skus:
            return 999999

        target_set = set(skus)
        if len(target_set) == 1:
            mate = getattr(self.warehouse_core, "sku_pairs", {}).get(skus[0])
            if mate:
                target_set.add(mate)

        plan = getattr(self.warehouse_core, "production_plan", {}) or {}
        best = 999999

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
    
    def _sample_single_solution(self, all_tasks: List[TaskData],
                               outbound_task_ids: Set[str],
                               task_feasible_assignments: Dict,
                               aisle_task_count: Dict[int, int],
                               task_idle_unblocked_aisles: Dict[str, List[Tuple]], 
                               heuristic_future_ready: Dict[int, int]) -> Dict[int, List[TaskData]]:
        """生成一个随机候选方案"""
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
                return heuristic_future_ready.get(task.production_line, 0) if hasattr(task, 'production_line') and task.production_line else 0
            
            def get_progress(task):
                return line_progress.get(task.production_line, 0) if hasattr(task, 'production_line') and task.production_line else 0
            
            # 综合考虑：未来可执行任务数多的优先，但进度慢的产线需要额外加权
            # 使用 -progress 加大权重，使进度慢的任务排序更靠前；作用于所有出库任务
            sort_key = lambda t: get_ready_count(t) - 5.0 * get_progress(t)
            current_group_tasks.sort(key=sort_key, reverse=True)
            other_outbound_tasks_list.sort(key=sort_key, reverse=True)
        
        # 打乱入库任务顺序实现随机性
        random.shuffle(other_inbound_tasks_list)

        # 紧急入库：若某产线即将需要该 SKU 集合（距离当前组 <= 阈值），提前提升优先级
        inbound_urgencies = {}
        for task in other_inbound_tasks_list:
            inbound_urgencies[task.task_id] = self._get_inbound_urgency(task, self.inbound_urgency_threshold)
        urgent_inbound_tasks = [t for t in other_inbound_tasks_list if inbound_urgencies.get(t.task_id, 999999) <= self.inbound_urgency_threshold]
        urgent_inbound_tasks.sort(key=lambda t: inbound_urgencies.get(t.task_id, 999999))
        other_inbound_tasks_list = [t for t in other_inbound_tasks_list if t not in urgent_inbound_tasks]
        
        # inbound 和 outbound混起来，但是inbound被选中的概率更高（inbound权重为2）
        mixed_tasks_list = []
        inbound_idx = 0
        outbound_idx = 0
        while True:
            if random.random() < 0.8 and inbound_idx < len(other_inbound_tasks_list):
                mixed_tasks_list.append(other_inbound_tasks_list[inbound_idx])
                inbound_idx += 1
            elif outbound_idx < len(other_outbound_tasks_list):
                mixed_tasks_list.append(other_outbound_tasks_list[outbound_idx])
                outbound_idx += 1
            else:
                break

        for task in current_group_tasks + mixed_tasks_list:
            if task.task_id not in task_feasible_assignments:
                continue
            
            feasible = task_feasible_assignments[task.task_id]
            if not feasible:
                continue
            
            is_outbound = task.task_id in outbound_task_ids
            aisle = None
            positions = None
            
            # 对于出库任务，优先选择空闲且非阻塞的巷道
            if is_outbound:
                idle_unblocked = task_idle_unblocked_aisles.get(task.task_id, [])
                # 筛选出当前 local_task_count 仍为0的巷道
                current_idle_unblocked = [
                    (a, p) for a, p in idle_unblocked if local_task_count.get(a, 0) == 0
                ]
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
            
            # 如果没有找到空闲非阻塞巷道，使用加权随机选择
            if aisle is None:
                # 加权选择巷道：任务数越少的巷道权重越高
                weights = []
                for a, _ in feasible:
                    base_w = 1.0 / (1.0 + local_task_count.get(a, 0)) ** 1
                    weights.append(base_w)
                selected_idx = random.choices(range(len(feasible)), weights=weights, k=1)[0]
                aisle, positions = feasible[selected_idx]
            
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
            
            # 跳过没有有效位置的任务
            if selected_position is None:
                continue
                
            # 构造TaskData
            skus_list = task.skus if task.skus else [{'skuId': sid} for sid in task.get_sku_ids()]
            task_type = TASK_TYPE_OUTBOUND if is_outbound else TASK_TYPE_INBOUND
            
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
        """将solution转换为字符串key用于去重"""
        key_parts = []
        for aisle in sorted(solution.keys()):
            tasks = solution[aisle]
            task_ids = [task.task_id for task in tasks]
            key_parts.append(f"{aisle}:{','.join(task_ids)}")
        return '|'.join(key_parts)
    
    def _evaluate_and_select_best(self, solutions: Dict[str, Tuple], heuristic_solution: Dict[int, List[TaskData]], heuristic_future_ready: Dict[int, int] = None) -> Dict[int, List[TaskData]]:
        """
        根据max_tasks进行加权采样，评分并选择最优方案
        
        Args:
            solutions: {solution_key: (aisle_task_sequences, max_tasks), ...}
            heuristic_solution: 启发式解决方案
            heuristic_future_ready: 启发式方案中各产线可连续执行的出库任务数
        Returns:
            最优的aisle_task_sequences
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
        max_priority = max(line_priority_scores) if line_priority_scores else 0

        weights = []
        for idx, max_tasks in enumerate(max_tasks_values):
            base_w = 1.0 / (1.0 + max_tasks) ** 2
            ready_norm = (line_priority_scores[idx] / max_priority) if max_priority > 0 else 0.0
            balance_penalty = line_balance_scores[idx]
            balance_factor = 1.0 / (1.0 + 0.01 * balance_penalty)  # 越平衡越接近1，越不平衡越被压低
            ready_factor = 1.0 + 0.5 * ready_norm  # future_ready越大，采样权重略微提升
            weights.append(base_w * ready_factor * balance_factor)
        
        # 根据权重对方案进行排序，得到索引
        indexed_weights = [(i, weights[i]) for i in range(len(weights))]
        indexed_weights.sort(key=lambda x: x[1], reverse=True)  # 按权重降序排列
        
        # 确定采样数量
        num_to_evaluate = min(self.num_evaluate, len(solutions_list))
        
        # 确保前5个（或如果总数不足5个则全部）评分最高的方案被选中
        top_5_count = min(5, num_to_evaluate)
        selected_solutions = []
        
        # 添加前top_5_count个评分最高的方案
        for i in range(top_5_count):
            idx = indexed_weights[i][0]
            selected_solutions.append(solutions_list[idx])
        
        # 从剩余方案中随机选择 num_to_evaluate - top_5_count 个
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
        
        # 评分并选择最优
        best_score = float('inf')

        for idx, (solution, max_tasks) in enumerate(selected_solutions, 1):
            # 计算基础得分
            base_score, base_details = self.warehouse_core.get_sol_score(solution)
            
            # 计算产线进度平衡性得分
            line_progress_balance_score = self._calculate_line_progress_balance_score(solution)
            outbound_choice_bonus = self._calculate_outbound_choice_bonus(solution)
            
            # 综合得分 = 基础得分 + 进度平衡性惩罚 + 出库选择灵活度奖励（负分更优）
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
    
    def _calculate_line_progress_balance_score(self, solution: Dict[int, List[TaskData]]) -> float:
        """
        计算产线进度平衡性得分，用于评估方案的均衡性
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
