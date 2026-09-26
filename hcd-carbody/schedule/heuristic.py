"""实现基于规则和随机混合顺序的启发式调度器。"""

"""


"""

import time
from typing import Dict, List, Optional
from simulation.task_data import TaskData, TASK_TYPE_INBOUND, TASK_TYPE_OUTBOUND
from simulation.position import InventoryPosition
import random


class HeuristicScheduler:
    """"""

    def __init__(self, warehouse_core):
        """
        Args:
            warehouse_core: WarehouseCore
        """
        self.warehouse_core = warehouse_core
        self.inventory_manager = warehouse_core.inventory_manager
        self.time_estimator = warehouse_core.time_estimator
        self.aisles = warehouse_core.aisles

        #
        self.solve_count = 0
        self.total_time = 0.0

        # 入库货位分配器（如果warehouse_core有设置）
        self.position_allocator = None

    # ==========================================================================
    # 辅助函数：任务特征与生产组解析
    # ==========================================================================
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

    # ==========================================================================
    # 主函数：启发式任务调度
    # ==========================================================================
    def solve(self, inbound_tasks: List[TaskData], outbound_tasks: List[TaskData],
             running_tasks: Optional[Dict[str, TaskData]] = None, current_time: float = 0.0) -> Dict[int, List[TaskData]]:
        """

        Args:
            inbound_tasks:
            outbound_tasks:
            running_tasks: 正在执行的任务字典 {task_id: task_info}，用于统计巷道任务数量

        Returns:
            aisle_task_sequences: {aisle: [task_info, ...]}
                task_info: task_id, task_type, production_line, sku, position, priority
        """
        start_time = time.time()

        task_position_assignments = {}
        # 根据running_tasks初始化aisle_task_count
        aisle_task_count = {aisle: 0 for aisle in self.aisles}
        if running_tasks:
            for task_info in running_tasks.values():
                aisle = task_info.assigned_aisle
                if aisle in aisle_task_count:
                    aisle_task_count[aisle] += 1

        # 0. 筛选出库任务：只保留当前组的任务
        filtered_outbound_tasks = []
        for task in outbound_tasks:
            production_line = task.production_line
            current_group_idx = self.warehouse_core.production_line_current_group[production_line]
            task_group_idx = self._resolve_task_group_idx(task)
            if task_group_idx is None:
                filtered_outbound_tasks.append(task)
                continue
            if task_group_idx == current_group_idx:
                filtered_outbound_tasks.append(task)
            else:
                print(f"  [启发式]筛选掉非当前组任务: {task.task_id} (任务组={task_group_idx}, 当前组={current_group_idx})")

        outbound_tasks = filtered_outbound_tasks

        # 2. 出库任务分配
        for task in outbound_tasks:
            if getattr(task, "positions", None):
                first_pos = task.positions[0]
                if first_pos is not None:
                    task_position_assignments[task.task_id] = list(task.positions)
                    if first_pos.aisle in aisle_task_count:
                        aisle_task_count[first_pos.aisle] += 1
                continue
            production_line = task.production_line
            match_mode = self.warehouse_core._get_outbound_match_mode(production_line)
            feature_keys = self.warehouse_core._get_outbound_match_features(production_line)
            # FIFO 是非 RFID 产线的硬约束：本轮仅保留同一匹配库存中最早入库的位置，
            # 不再因巷道负载或随机选择跳过较早库存。
            fifo_enabled = self.warehouse_core.is_outbound_fifo_enabled(production_line)

            # 查找可用位置
            available_positions_by_aisle = {}

            if match_mode == "features":
                feature_filters = self._extract_feature_filters(task, feature_keys)
                if not feature_filters:
                    continue
                found = False
                positions = self.inventory_manager.get_positions_by_features(
                    feature_filters[0], feature_keys, only_available=True
                )
                if fifo_enabled and positions:
                    # API 同步阶段会将 inboundTime 规范化为 features 中的数值秒；
                    # 未携带时间的历史库存按无穷大处理，使其排在已知时间库存之后。
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
                    if pos.aisle not in available_positions_by_aisle:
                        available_positions_by_aisle[pos.aisle] = []
                    available_positions_by_aisle[pos.aisle].append(pos)
                    found = True
                if not found:
                    continue
            else:
                # SKU 字段。
                sku_ids = task.get_sku_ids()
                if not sku_ids:
                    continue
                found = False
                if len(sku_ids) == 1:
                    # 单个 SKU
                    sku = sku_ids[0]
                    for pos in self.inventory_manager.get_sku_positions(sku, only_available=True):
                        if pos.aisle not in available_positions_by_aisle:
                            available_positions_by_aisle[pos.aisle] = []
                        available_positions_by_aisle[pos.aisle].append(pos)
                        found = True
                    if not found:
                        # 调试信息：打印该SKU在各巷道的库存情况
                        for aisle in self.aisles:
                            try:
                                print(f"[调试] SKU {sku} 在巷道{aisle}的current_inventory数量: {self.inventory_manager.current_inventory[aisle][sku]}")
                            except Exception as e:
                                print(f"[调试] SKU {sku} 在巷道{aisle} 查询current_inventory出错: {e}")
                elif len(sku_ids) == 2:
                    # 双梁任务：查找同时包含两个SKU的双层位置
                    for pos in self.inventory_manager.inventory_positions:
                        if (pos.is_double_layer
                            and set([pos.upper_sku, pos.lower_sku]) == set(sku_ids)
                            and pos.upper_quantity > 0
                            and pos.lower_quantity > 0):
                            if pos.aisle not in available_positions_by_aisle:
                                available_positions_by_aisle[pos.aisle] = []
                            available_positions_by_aisle[pos.aisle].append(pos)
                            found = True
                    if not found:
                        for aisle in self.aisles:
                            for sku in sku_ids:
                                try:
                                    print(f"[调试] SKU {sku} 在巷道{aisle}的current_inventory数量: {self.inventory_manager.current_inventory[aisle][sku]}")
                                except Exception as e:
                                    print(f"[调试] SKU {sku} 在巷道{aisle} 查询current_inventory出错: {e}")
                # print一下sku的具体存放位置
                if not found:
                    for sku in sku_ids:
                        all_positions = [pos for pos in self.inventory_manager.inventory_positions if (getattr(pos, 'upper_sku', None) == sku or getattr(pos, 'lower_sku', None) == sku)]
                        print(f"[调试] SKU {sku} 存放位置:")
                        for pos in all_positions:
                            try:
                                info = (
                                    f"位置ID: {pos.get_position_id()}, 巷道: {pos.aisle}, "
                                    f"upper: {pos.upper_sku}/{pos.upper_quantity if hasattr(pos,'upper_quantity') else '?'}, "
                                    f"lower: {pos.lower_sku}/{pos.lower_quantity if hasattr(pos,'lower_quantity') else '?'}"
                                )
                            except Exception as e:
                                info = f"(位置信息无法获取: {e})"
                            print(info)

            if available_positions_by_aisle:
                # 筛选非堵塞且非忙碌的巷道
                valid_aisles = []
                for aisle in available_positions_by_aisle.keys():
                    if aisle_task_count[aisle] > 0:
                        continue
                    # 检查堵塞状态
                    out_line = getattr(task, "out_line", None) or production_line
                    is_blocked = self.warehouse_core.check_blockage(aisle, out_line, current_time=current_time)
                    if not is_blocked:
                        valid_aisles.append(aisle)

                # 从有效巷道中选择任务最少的巷道
                if valid_aisles:
                    best_aisle = min(valid_aisles, key=lambda a: aisle_task_count[a])
                    # 在该巷道中选择最优位置,随便选一个
                    best_position = random.choice(available_positions_by_aisle[best_aisle])
                    aisle_task_count[best_aisle] += 1
                    task_position_assignments[task.task_id] = [best_position]

        # 3. 入库任务分配（先来先做，in_line 仅决定目标层，不影响排序）
        for task in inbound_tasks:
            target_aisle = task.assigned_aisle
            if target_aisle is None or target_aisle not in aisle_task_count:
                continue
            if aisle_task_count[target_aisle] > 0:
                continue
            empty_positions = [
                p for p in self.warehouse_core.inventory_manager.inventory_positions
                if p.aisle == target_aisle and p.is_empty()
            ]
            if not empty_positions:
                print(f"[HEU] skip inbound {task.task_id}: aisle {target_aisle} has no empty position")
                continue
            current_position = self.warehouse_core.current_position_by_aisle.get(target_aisle)
            if self.position_allocator is not None:
                allocated_positions = self.position_allocator.allocate(
                    self.warehouse_core.inventory_manager.inventory_positions, task, current_position
                )
            else:
                allocated_positions = [random.choice(empty_positions)]
                # 强约束：入库任务必须落到当前为空且可用的货位。
            if allocated_positions:
                if any((p is None) or (not p.is_empty()) for p in allocated_positions):
                    print(f"[HEU] skip inbound {task.task_id}: allocated position not empty")
                    allocated_positions = []
            if allocated_positions:
                task_position_assignments[task.task_id] = allocated_positions

        # 4. 生成巷道任务序列
        aisle_task_sequences = {aisle: [] for aisle in self.aisles}

        # 添加出库任务
        for task in outbound_tasks:
            if task.task_id in task_position_assignments:
                position = task_position_assignments[task.task_id]
                if isinstance(position, list):
                    if not position:
                        continue
                    aisle = position[0].aisle
                    pos_list = position
                else:
                    aisle = position.aisle
                    pos_list = [position]
                # 构造 TaskData（保持原 task 的关键信息，填充 positions）
                skus_list = task.skus if task.skus else [{'skuId': sid} for sid in task.get_sku_ids()]
                new_task = TaskData(
                    task_id=task.task_id,
                    task_type=TASK_TYPE_OUTBOUND,
                    task_name=getattr(task, 'task_name', task.task_id),
                    skus=skus_list,
                    production_line=task.production_line,
                    assigned_aisle=aisle,
                    assigned_time=getattr(task, 'assigned_time', 0),
                    positions=pos_list,
                    task_record=getattr(task, 'task_record', {})
                )
                # 保留业务字段，供 API 响应和后续校验使用。
                if hasattr(task, "out_line"):
                    new_task.out_line = getattr(task, "out_line", None)
                if hasattr(task, "in_line"):
                    new_task.in_line = getattr(task, "in_line", None)
                if hasattr(task, "plan_id"):
                    new_task.plan_id = getattr(task, "plan_id", None)
                if hasattr(task, "group_idx"):
                    new_task.group_idx = getattr(task, "group_idx", None)
                aisle_task_sequences[aisle].append(new_task)

        # 添加入库任务
        for task in inbound_tasks:
            if task.task_id in task_position_assignments:
                position = task_position_assignments[task.task_id]
                if isinstance(position, list):
                    if not position:
                        continue
                    aisle = position[0].aisle
                    pos_list = position
                else:
                    aisle = position.aisle
                    pos_list = [position]
                skus_list = task.skus if task.skus else [{'skuId': sid} for sid in task.get_sku_ids()]
                new_task = TaskData(
                    task_id=task.task_id,
                    task_type=TASK_TYPE_INBOUND,
                    task_name=getattr(task, 'task_name', task.task_id),
                    skus=skus_list,
                    production_line=task.production_line,
                    assigned_aisle=aisle,
                    assigned_time=getattr(task, 'assigned_time', 0),
                    positions=pos_list,
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
                aisle_task_sequences[aisle].append(new_task)


        solve_time = time.time() - start_time
        self.total_time += solve_time
        self.solve_count += 1

        return aisle_task_sequences

    def get_average_solve_time(self) -> float:
        """获取average solve 时间相关逻辑。

        Args:
            self: 当前对象实例。

        Returns:
            float: 处理后的结果。
        """
        """"""
        if self.solve_count == 0:
            return 0.0
        return self.total_time / self.solve_count
