"""
仓库服务层 - 桥接API与WarehouseCore

此模块负责：
1. 将API请求转换为warehouse_core可理解的格式
2. 调用warehouse_core的相关方法
3. 同步外部状态到warehouse_core
4. 管理仿真时间的推进

服务层同时维护真实库存、入库模拟占位、移库预留、出库虚拟预占和 API 冻结
位置等不同状态层。它们的写入时机不同：真实库存仅在任务完成时更新；入库
模拟占位仅用于分配推演；移库预留锁定可用货位；出库虚拟预占避免已提交任务
重复引用同一库存；冻结 positions 用于保证 repeated mixed 不改变已承诺任务。
"""

import time
import random
import json
import re
from copy import deepcopy
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple, Set
from dataclasses import dataclass

# 导入warehouse_core相关模块
import sys
from pathlib import Path
from config_loader import get_runtime_root

# 确保可以导入simulation模块
# 打包后模块位于 _internal，但用户可编辑 config 在 exe 同级目录；因此不能
# 由 __file__ 反推写入位置，统一使用源码/部署运行根。
project_root = get_runtime_root()
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from simulation.warehouse_core import WarehouseCore
from simulation.task_data import TaskData, TASK_TYPE_INBOUND, TASK_TYPE_OUTBOUND
from simulation.position import InventoryPosition
from simulation.event import Event, EVENT_TASK_COMPLETE, EVENT_INBOUND_ARRIVAL_AT_AISLE
from api.state import TaskStateManager


class WarehouseService:
    """
    仓库服务类 - API与WarehouseCore的桥接层

    核心职责：
    1. 管理WarehouseCore实例的生命周期
    2. 同步外部系统状态到内部状态
    3. 转换API数据格式
    4. 调用warehouse_core方法并返回结果

    路由层通过本类调用 mixed、任务反馈和任务调整方法。``WarehouseCore`` 仍是
    事件、真实库存和队列的权威状态；本类负责 API 数据归一化、预览、冻结和
    对外响应所需的状态解释。
    """

    def __init__(self, warehouse_core: Optional[WarehouseCore] = None):
        """初始化仓库服务

        Args:
            warehouse_core: 可选的WarehouseCore实例，如果未提供则创建新实例

        Returns:
            None: 通过实例状态、队列或外部副作用完成处理。
        """
        # API 服务默认采用优化调度和改进入库策略；实际权重和设备参数仍由 warehouse.json 决定。
        scheduler_type = 'optimization'
        inbound_aisle_strategy = 'proposed'
        inbound_allocation_strategy = 'proposed'
        if warehouse_core is None:
            self._core = WarehouseCore(
                scheduler_type=scheduler_type,
                inbound_aisle_strategy=inbound_aisle_strategy,
                inbound_allocation_strategy=inbound_allocation_strategy,
                config_path='config/warehouse.json',
                # API 与命令行仿真使用独立 BOM：API 初始文件为空，后续由 BOM
                # 接口写入；仿真继续使用 simulation/data/sku_config.json。
                sku_config_path='config/sku_config.json',
            )
            # API 启动只初始化货位结构、禁用位和索引；不生成仿真随机库存。
            # 外部库存以 mixed 同步或已完成入库任务反馈为唯一写入来源。
            self._core.initialize(populate_initial_inventory=False)
        else:
            self._core = warehouse_core

        # 服务启动时固化一份 BOM 快照；用于在库存同步前校验请求中 SKU 是否已配置。
        self._bom_config_snapshot = self._build_bom_snapshot()

        # 服务实例创建的 Unix 时间戳（秒），作为 API 状态持续时间的计算起点。
        self._start_time = time.time()
        # 最近一次外部库存、巷道状态或任务同步完成的 Unix 时间戳（秒）。
        self._last_sync_time = self._start_time

        # 已接受、已冻结位置但尚未收到 EXECUTING 反馈的任务缓存。
        # 键为 taskId，值为 TaskData；它与 Core 的 pending 入/出库队列协同维护。
        self._pending_execution_tasks: Dict[str, TaskData] = {}
        # 最近一次反馈处理失败的原因；路由层读取后返回给调用方。
        self._last_feedback_error: Optional[str] = None
        # 最近一次反馈处理成功但无需重复执行时产生的提示，例如重复 EXECUTING。
        self._last_feedback_notice: Optional[str] = None
        # 最近一次 mixed 调度产生的非阻断提示，例如任务被延后或按规则跳过。
        self._last_schedule_notices: List[str] = []

        # 最近一次 mixed 调度中各巷道已匹配、且 positions 已冻结的候选任务。
        # 键为 aisleId，值为该巷道的 TaskData 列表；重复 mixed 时据此保持位置稳定。
        self._last_matched_tasks_by_aisle: Dict[int, List[TaskData]] = {}
        # 最近一次 mixed 调度中每个巷道优先推荐执行的任务；键为 aisleId，值为 taskId。
        self._last_recommended_task_by_aisle: Dict[int, str] = {}
        # 最近一次 mixed 未提交出库任务的结构化原因列表，供 API 返回 conflictTaskIds 等信息。
        self._last_unsubmitted_outbound_tasks: List[Dict[str, Any]] = []
        # 当前计划批次的 planId 到实际产线号映射，优先于从计划名称或 SKU 推断产线。
        self._plan_id_to_line: Dict[str, int] = {}
        # 当前计划批次的 planId 到该计划首组在产线总计划中偏移量的映射。
        self._plan_id_to_group_offset: Dict[str, int] = {}
        # 当前计划批次的 planId 到其提交计划组数量的映射，用于校验 planIndex 合法范围。
        self._plan_id_to_group_count: Dict[str, int] = {}
        # ADD 前每条产线已有的生产计划组数量，用于在新计划后追加，而非覆盖旧计划。
        self._line_id_to_base_group_count: Dict[int, int] = {}
        # ADD 前每条产线已有的任务组数量，用于将外部从 1 开始的 planIndex 转为内部组号。
        self._line_id_to_base_task_group_count: Dict[int, int] = {}
        # 当前计划同步操作类型（ADD 或 UPDATE），决定组号偏移和既有计划的保留方式。
        self._current_plan_operation: Optional[str] = None
        # 未传 productionPlan 的出库任务按产线维护的虚拟当前组号；任务完成后向下一组推进。
        self._virtual_outbound_current_group: Dict[int, int] = {}

    # ========================================================================
    # 主流程：混合任务调度、资源结算与预览返回
    # ========================================================================
    def execute_schedule(self, tasks: Tuple[List[TaskData], List[TaskData]]) -> Dict[int, Optional[TaskData]]:
        """
        执行混合调度

        此方法在每次mixed调度请求时被调用，执行以下步骤：
        1. 同步时间
        2. 为出库任务查找库存货位
        3. 将新任务添加到等待队列
        4. 调用调度器进行调度决策
        5. 将分配的任务保存到待执行队列（等待EXECUTING反馈后再真正执行）
        6. 返回各巷道分配的任务

        更新的参数：
        - pending_inbound_by_aisle: 入库等待队列
        - pending_outbound_queue: 出库等待队列
        - _pending_execution_tasks: 待执行任务缓存

        Args:
            tasks: (inbound_tasks, outbound_tasks) 任务元组

        Returns:
            Dict[int, TaskData]: 各巷道分配的任务 {aisle_id: task_data}
        """
        result, _matched = self.execute_schedule_with_preview(tasks)
        return result

    def execute_schedule_with_preview(
        self, tasks: Tuple[List[TaskData], List[TaskData]]
    ) -> Tuple[Dict[int, Optional[TaskData]], Dict[int, List[TaskData]]]:
        """执行 mixed 预览、资源结算和任务位置冻结。

        流程先把 running/pending 出库任务装入虚拟资源视图，再按生产组优先级
        处理新任务；同一组先在临时占用集合中整体试算，成功后才提交该组占用。
        调度器给出推荐顺序后，服务层先冻结推荐任务的位置，再固定其他已接受
        出库任务位置。真实库存仍由 COMPLETED 反馈路径扣减。

        局部变量：``inbound_tasks``、``outbound_tasks`` 是本轮请求拆分结果；
        ``accepted_outbound_tasks`` 是通过当前层资源结算的新增任务；``matched_by_aisle``
        是已冻结位置的可执行候选；``occupied_units`` / ``occupied_owner_map`` 是层级
        资源键及其任务所有者；``outbound_groups`` 按产线和组归并；``layer_to_groups``
        按当前/未来组的优先级分层；``result`` 是每巷道推荐或运行中任务。

        Args:
            tasks (Tuple[List[TaskData], List[TaskData]]): 本轮 mixed 请求归一化后的
                ``(inbound_tasks, outbound_tasks)``。任务可带已冻结的 ``positions``；
                本方法只为尚未冻结且通过资源结算的任务补充位置。

        Returns:
            Tuple[Dict[int, Optional[TaskData]], Dict[int, List[TaskData]]]: ``result`` 按
            巷道给出 running 或本轮推荐任务；``matched_by_aisle`` 给出资源结算后
            仍可执行且已冻结位置的任务，不表示真实库存已经扣减。
        """
        # 将 API 墙钟时间推进到 Core，便于释放已到期的运行状态。
        self._sync_time()
        # mixed 请求输入按任务类型拆分。
        inbound_tasks, outbound_tasks = tasks
        self._last_unsubmitted_outbound_tasks = []

        print(f"[WarehouseService] 执行调度决策，入库任务: {len(inbound_tasks)}，出库任务: {len(outbound_tasks)}")

        # 1. 为出库任务做“已提交任务优先”的虚拟资源结算。
        #    running + 既有 pending_outbound_queue 会先占用资源；
        #    本轮新任务再按优先级层逐层尝试提交。
        # 本轮通过资源结算、可进入 pending 的新增出库。
        accepted_outbound_tasks: List[TaskData] = []
        # 已冻结位置的任务，供 matchedTasks 返回。
        matched_by_aisle: Dict[int, List[TaskData]] = {}
        # 同产线前序组失败后阻断后续组的任务 ID。
        blocked_outbound_line_tasks: Dict[int, str] = {}
        # 已承诺任务资源基线。
        occupied_units, occupied_owner_map = self._collect_submitted_outbound_occupancy()

        def _append_unsubmitted_outbound(
            task: TaskData,
            reason: str,
            blocked_by_task_id: Optional[str] = None,
            conflict_task_ids: Optional[List[str]] = None,
        ) -> None:
            """将本轮未提交的出库任务转换为 API 响应记录。

            输入：任务、失败原因、可选的前序阻塞任务和资源冲突任务集合。
            输出：无返回值；向 ``_last_unsubmitted_outbound_tasks`` 写入 JSON 可序列化字典。
            该记录保留任务所属计划和产线，使调用方可区分前序组阻塞与跨产线资源冲突。

            Args:
                task (TaskData): 当前处理的任务记录。
                reason (str): 本次未提交的直接原因，例如库存不足或前序组阻断。
                blocked_by_task_id (Optional[str]): 同产线前序失败时的阻塞任务 ID。
                conflict_task_ids (Optional[List[str]]): 与当前任务争用同一虚拟库存
                    单元的已提交或同层候选任务 ID。

            Returns:
                None: 向 ``_last_unsubmitted_outbound_tasks`` 追加一条可直接返回给
                调用方的拒绝记录。
            """
            # 对齐API返回的 planIndex，若未传入 productionPlan 则按产线维护虚拟组号。
            public_plan_index = getattr(task, "plan_index_public", None)
            if public_plan_index is None:
                core_group_idx = getattr(task, "group_idx", None)
                public_plan_index = int(core_group_idx) + 1 if core_group_idx is not None else None
            payload: Dict[str, Any] = {
                "taskId": getattr(task, "task_id", ""),
                "taskType": getattr(task, "task_type", ""),
                "planId": getattr(task, "plan_id", None),
                "planIndex": public_plan_index,
                "productionLine": getattr(task, "production_line", None),
                "skus": [self._sku_entry_to_dict(s) for s in (getattr(task, "skus", None) or [])],
                "reason": reason,
            }
            if blocked_by_task_id:
                payload["blockedByTaskId"] = blocked_by_task_id
            if conflict_task_ids:
                payload["conflictTaskIds"] = list(conflict_task_ids)
            self._last_unsubmitted_outbound_tasks.append(payload)

        # 先以“产线 + 组”归并。本组内的多条出库任务是同一推进单元，不能因字典或任务传入顺序不同而只提交其中一部分。
        # {(产线, 内部组号): 同组任务}。
        outbound_groups: Dict[Tuple[int, int], List[TaskData]] = {}
        for task in outbound_tasks:
            production_line = int(getattr(task, "production_line", 0) or 0)
            outbound_group_state = self._get_outbound_group_state(task)
            if outbound_group_state == "past":
                print(f"[WarehouseService] 出库任务 {task.task_id} 属于 past 组，直接忽略，不进入候选与提交队列")
                continue
            group_key = getattr(task, "group_idx", None)
            if group_key is None:
                public_plan_index = getattr(task, "plan_index_public", None)
                group_key = int(public_plan_index) - 1 if public_plan_index is not None else 0
            outbound_groups.setdefault((production_line, int(group_key)), []).append(task)

        # 优先级层由当前组/未来组决定；不同产线的同层组共用一份虚拟库存与货位单元，因此会在下面的同一层内共同结算冲突。
        # {距当前组偏移: 生产组集合}。
        layer_to_groups: Dict[int, List[Tuple[int, int, List[TaskData]]]] = {}
        for (production_line, group_idx), group_tasks in outbound_groups.items():
            sample_task = group_tasks[0] if group_tasks else None
            if sample_task is None:
                continue
            layer_offset = self._get_outbound_priority_offset(sample_task)
            if layer_offset is None:
                continue
            layer_to_groups.setdefault(int(layer_offset), []).append((production_line, group_idx, group_tasks))

        for layer_offset in sorted(layer_to_groups.keys()):
            group_entries = list(layer_to_groups.get(layer_offset, []))

            def _group_priority(entry: Tuple[int, int, List[TaskData]]) -> Tuple[int, int, str]:
                """为同一优先级层的生产组计算稳定试算顺序。

                输入：``(production_line, group_idx, group_tasks)``。
                输出：``(候选位置总数, 产线号, 最小任务号)``。
                候选位置较少的组优先试算，减少高弹性组抢占稀缺位置造成的误拒绝；
                产线号和任务号只用于消除并列时的不确定性。

                Args:
                    entry (Tuple[int, int, List[TaskData]]): 同一优先级层中的 ``(产线号, 组号, 同组任务列表)`` 记录。

                Returns:
                    Tuple[int, int, str]: 当前处理得到的文本标识、格式化结果或诊断信息。
                """
                production_line, group_idx, group_tasks = entry
                candidate_count = 0
                for group_task in group_tasks:
                    candidate_count += len(self._get_outbound_candidate_positions(group_task))
                first_task_id = min((getattr(t, "task_id", "") for t in group_tasks), default="")
                return (candidate_count or 10**9, int(production_line), first_task_id)

            # 候选越少的组先试算，降低稀缺资源被高弹性任务先占后误拒绝的概率；
            # 仍以产线和任务 ID 作为稳定的确定性并列规则。
            group_entries.sort(key=_group_priority)

            # 同一优先级层的不同产线共用正式 occupied_units。组与组之间只要资源
            # 不重叠即可同时成立；排序仅用于在资源不足时稳定地确定冲突归属。
            for production_line, _group_idx, group_tasks in group_entries:
                blocked_by_task_id = blocked_outbound_line_tasks.get(production_line)
                if blocked_by_task_id:
                    for task in group_tasks:
                        print(
                            f"[WarehouseService] 出库任务 {task.task_id} 因产线 {production_line} 前序任务 {blocked_by_task_id} 库存不足而不提交"
                        )
                        _append_unsubmitted_outbound(task, "前序出库任务库存不足", blocked_by_task_id=blocked_by_task_id)
                    continue

                # 组内先在副本上试算。只有该组全部任务均获得互不冲突的资源时，
                # 才把副本回写为本轮正式占用，避免任务遍历顺序造成误拒绝。
                temp_occupied_units = set(occupied_units)
                temp_occupied_owner_map = {
                    unit_key: set(owner_ids)
                    for unit_key, owner_ids in occupied_owner_map.items()
                }
                group_ready: List[TaskData] = []
                failed_task_id: Optional[str] = None
                failed_reason = "库存不足"
                conflict_task_ids: List[str] = []

                # 同组内也按可选位置数量排序，但占用只写入临时集合，确保本组
                # 全部成功才一次性提交到正式虚拟资源视图。
                tasks_in_group = sorted(
                    list(group_tasks),
                    key=lambda task: (
                        len(self._get_outbound_candidate_positions(task)) or 10**9,
                        str(getattr(task, "task_id", "")),
                    ),
                )

                # 先在临时资源视图逐任务选位置并占用。若循环中任何一个任务失败，
                # 本组不会把临时结果回写，因此不会留下“半组已占库存”的假承诺。
                for task in tasks_in_group:
                    positions, reason, blockers, _candidate_count = self._find_outbound_positions_with_occupancy(
                        task,
                        temp_occupied_units,
                        temp_occupied_owner_map,
                    )
                    if not positions:
                        print(f"[WarehouseService] 警告: 出库任务 {task.task_id} 在虚拟资源视图下不可提交: {reason}")
                        failed_task_id = task.task_id
                        failed_reason = reason
                        conflict_task_ids = list(blockers or [])
                        break
                    reserved, blockers = self._reserve_outbound_task_units(
                        task,
                        positions,
                        temp_occupied_units,
                        temp_occupied_owner_map,
                    )
                    if not reserved:
                        failed_task_id = task.task_id
                        failed_reason = "已提交出库任务占用导致当前优先级资源不足"
                        conflict_task_ids = sorted(list(blockers or []))
                        break

                    group_ready.append(task)

                if failed_task_id:
                    blocked_outbound_line_tasks[production_line] = failed_task_id
                    for task in group_tasks:
                        if task.task_id == failed_task_id:
                            _append_unsubmitted_outbound(
                                task,
                                failed_reason,
                                conflict_task_ids=conflict_task_ids or None,
                            )
                        else:
                            _append_unsubmitted_outbound(task, "同组出库任务库存不足", blocked_by_task_id=failed_task_id)
                    continue

                # 整组可行后才提交占用；下一层和下一次 mixed 都将其当作已承诺资源。
                occupied_units = temp_occupied_units
                occupied_owner_map = temp_occupied_owner_map
                accepted_outbound_tasks.extend(group_ready)

        # 2. 入库任务进入 pending 时必须已有冻结位置。这样执行反馈不会重新分配
        # 位置，后续入库模拟也能把其占位作为确定前序条件。
        for task in inbound_tasks:
            if task.assigned_aisle:
                aisle = task.assigned_aisle
                # 检查巷道是否可用
                if not self.is_aisle_available(aisle):
                    print(f"[WarehouseService] 入库任务 {task.task_id} 的目标巷道 {aisle} 不可用，跳过")
                    continue
                if not getattr(task, "positions", None):
                    allocated_positions = self._allocate_feedback_positions_for_aisle(task, aisle)
                    if not allocated_positions:
                        print(f"[WarehouseService] 入库任务 {task.task_id} 没有可分配的位置；跳过待处理队列")
                        continue
                    task.positions = allocated_positions
                self.freeze_task_api_positions(task)
                if aisle in self._core.pending_inbound_by_aisle:
                    # 避免重复添加
                    existing_ids = {t.task_id for t in self._core.pending_inbound_by_aisle[aisle]}
                    if task.task_id not in existing_ids:
                        self._core.pending_inbound_by_aisle[aisle].append(task)
                        print(f"[WarehouseService] 入库任务 {task.task_id} 添加到巷道 {aisle} 等待队列")

        # 3. 使用调度器进行调度决策
        # 合并每条入库线的队首任务与已接受出库任务，形成调度器按巷道排序的候选池。
        inbound_for_schedule: List[TaskData] = []
        for aisle in self._core.aisles:
            line_buckets = {}
            for t in self._core.pending_inbound_by_aisle.get(aisle, []):
                line = getattr(t, "in_line", 1)
                if line not in line_buckets:
                    line_buckets[line] = t
            inbound_for_schedule.extend(line_buckets.values())

        outbound_for_schedule: List[TaskData] = list(self._core.pending_outbound_queue) + list(accepted_outbound_tasks)

        # 调度器只给每条空闲巷道推荐一个优先任务；推荐本身不扣减真实库存。
        aisle_task_sequences = self._core.scheduler.solve(
            inbound_tasks=inbound_for_schedule,
            outbound_tasks=outbound_for_schedule,
            running_tasks=self._core.running_tasks,
            current_time=self._core.current_time,
        )

        # 4. 先按调度推荐冻结推荐任务的位置，再冻结其余已接受任务的位置。
        # 冻结的 positions 是 API 任务承诺；重调 mixed 只读取该结果，不能因库存
        # 新增或候选排序变化而重新选到其他巷道。
        accepted_outbound_by_id: Dict[str, TaskData] = {
            getattr(task, "task_id", ""): task
            for task in accepted_outbound_tasks
            if getattr(task, "task_id", None)
        }
        occupied_units, occupied_owner_map = self._collect_submitted_outbound_occupancy()
        recommended_task_ids: set = set()

        def _preferred_aisle_from_task(task_obj: Optional[TaskData]) -> Optional[int]:
            """从已推荐或已冻结任务中读取优先巷道。

            输入：可选任务对象。
            输出：冻结位置首项的巷道，若未冻结则返回 ``assigned_aisle``；无法转换时为 ``None``。

            Args:
                task_obj (Optional[TaskData]): 当前处理的任务对象。

            Returns:
                Optional[int]: 计算得到的编号或索引；无可用结果时返回 ``None``。
            """
            if task_obj is None:
                return None
            positions = getattr(task_obj, "positions", None) or []
            if positions:
                try:
                    return int(getattr(positions[0], "aisle", 0) or 0)
                except Exception:
                    return None
            aisle = getattr(task_obj, "assigned_aisle", None)
            if aisle is None:
                return None
            try:
                return int(aisle)
            except (TypeError, ValueError):
                return None

        def _finalize_outbound_task(
            task: TaskData,
            preferred_aisle: Optional[int],
        ) -> bool:
            """在虚拟资源视图中固定一项已接受出库任务的位置。

            输入：待固定任务和调度器推荐的优先巷道。
            输出：找到位置、预占资源并完成位置冻结时返回 ``True``，否则返回 ``False``。
            优先尝试推荐巷道，推荐巷道已无资源时才回退到其他合法巷道；成功后同时更新
            任务 ``positions``、``assigned_aisle`` 和按巷道的 matched 结果。

            Args:
                task (TaskData): 当前处理的任务记录。
                preferred_aisle (Optional[int]): 已由推荐或外部指定的巷道；传入后仅在该巷道内筛选候选位置。

            Returns:
                bool: 条件满足、处理成功或校验通过时为 ``True``，否则为 ``False``。
            """
            positions, _reason, _blockers, _candidate_count = self._find_outbound_positions_with_occupancy(
                task,
                occupied_units,
                occupied_owner_map,
                preferred_aisle=preferred_aisle,
            )
            if not positions and preferred_aisle is not None:
                positions, _reason, _blockers, _candidate_count = self._find_outbound_positions_with_occupancy(
                    task,
                    occupied_units,
                    occupied_owner_map,
                )
            if not positions:
                return False
            reserved, _blockers = self._reserve_outbound_task_units(
                task,
                positions,
                occupied_units,
                occupied_owner_map,
            )
            if not reserved:
                return False
            task.positions = positions
            try:
                task.assigned_aisle = int(getattr(positions[0], "aisle", 0) or 0)
            except Exception:
                task.assigned_aisle = preferred_aisle
            self.freeze_task_api_positions(task)
            if task.assigned_aisle:
                matched_by_aisle.setdefault(int(task.assigned_aisle), []).append(task)
            return True

        for aisle in self._core.aisles:
            sequence = list(aisle_task_sequences.get(aisle, []) or [])
            for candidate in sequence:
                task_id = getattr(candidate, "task_id", None)
                if not task_id or task_id not in accepted_outbound_by_id or task_id in recommended_task_ids:
                    continue
                canonical_task = accepted_outbound_by_id[task_id]
                if _finalize_outbound_task(canonical_task, _preferred_aisle_from_task(candidate)):
                    recommended_task_ids.add(task_id)
                break

        for task_id, canonical_task in accepted_outbound_by_id.items():
            if task_id in recommended_task_ids:
                continue
            sequence_task = None
            for sequence in aisle_task_sequences.values():
                for candidate in sequence:
                    if getattr(candidate, "task_id", None) == task_id:
                        sequence_task = candidate
                        break
                if sequence_task is not None:
                    break
            _finalize_outbound_task(canonical_task, _preferred_aisle_from_task(sequence_task))

        for task in accepted_outbound_tasks:
            if getattr(task, "positions", None):
                continue
            _append_unsubmitted_outbound(task, "已提交出库任务占用导致当前优先级资源不足")

        # 5. 将出库任务添加到等待队列
        for task in accepted_outbound_tasks:
            if not getattr(task, "positions", None):
                continue
            existing_ids = {t.task_id for t in self._core.pending_outbound_queue}
            if task.task_id not in existing_ids:
                self._core.pending_outbound_queue.append(task)
                print(f"[WarehouseService] 出库任务 {task.task_id} 添加到出库队列")

        # 6. 构建返回结果，并将分配的任务保存到待执行队列
        result: Dict[int, Optional[TaskData]] = {}

        for aisle in self._core.aisles:
            # 外部同步的巷道状态不可用时，保留其队列任务但本轮不生成 assignedTask。
            if not self.is_aisle_available(aisle):
                result[aisle] = None
                continue

            # 如果该巷道已有正在执行的任务，返回执行中的任务作为 assignedTask，
            # 新任务仅做匹配预览（matchedTasks），不在此时下发。
            running_task = None
            for t in self._core.running_tasks.values():
                if getattr(t, "assigned_aisle", None) == aisle:
                    running_task = t
                    break
            if running_task is not None:
                result[aisle] = running_task
                continue

            # 获取调度器建议的首任务；后续仍要校验生产组、拥堵和冻结位置是否成立。
            sequence = aisle_task_sequences.get(aisle, [])
            used_fallback = False
            if sequence and all(t.task_id in self._core.running_tasks for t in sequence):
                for pending_task in accepted_outbound_tasks:
                    positions = getattr(pending_task, "positions", None) or []
                    if positions and str(positions[0].aisle) == str(aisle):
                        sequence = [pending_task]
                        used_fallback = True
                        break
            if not sequence:
                fallback_task = None
                for pending_task in accepted_outbound_tasks:
                    positions = getattr(pending_task, "positions", None) or []
                    if positions and str(positions[0].aisle) == str(aisle):
                        fallback_task = pending_task
                        break
                if fallback_task is None:
                    for pending_task in self._core.pending_inbound_by_aisle.get(aisle, []):
                        if getattr(pending_task, "positions", None):
                            fallback_task = pending_task
                            break
                if fallback_task is None:
                    result[aisle] = None
                    for running_task in self._core.running_tasks.values():
                        if running_task.assigned_aisle == aisle:
                            result[aisle] = running_task
                            break
                    continue
                sequence = [fallback_task]
                used_fallback = True

            # 从序列中挑选第一个“可以下发”的任务（序列可能包含已匹配但暂不可启动的任务）。
            task = None
            for candidate in sequence:
                candidate = self._resolve_pending_task_instance(candidate)
                if candidate is None:
                    # 队列中可能保留已被失败反馈移除的旧引用；跳过后继续寻找下一候选。
                    continue
                # 出库任务的约束条件
                if candidate.task_type == TASK_TYPE_OUTBOUND and candidate.production_line is not None:
                    # 下游拥堵时出库候选不进入 can_executing；任务仍保留在 pending 队列等待解除。
                    if self._core.check_blockage(aisle, candidate.production_line, current_time=self._core.current_time):
                        continue
                    # 组顺序约束：与 /task/unconfirmed 保持一致，支持真实 productionPlan 与虚拟分组两种模式
                    if not self._is_task_logically_executable(candidate):
                        continue

                # 确保任务有positions
                if not getattr(candidate, 'positions', None):
                    continue

                task = candidate
                break

            if task is None:
                result[aisle] = None
                continue

            # 已固化位置的出库任务必须保持其原始巷道，避免 repeated mixed 导致语义漂移。
            if not (
                getattr(task, "task_type", None) == TASK_TYPE_OUTBOUND
                and getattr(task, "positions", None)
            ):
                task.assigned_aisle = aisle

            # 建立待执行缓存；EXECUTING/COMPLETED/FAILED 反馈均以 taskId 回查该对象。
            task.task_record = self._core.generate_task_record(task, self._core.current_time)

            # 保存到待执行队列（等待EXECUTING反馈）
            result[aisle] = task
            print(f"[WarehouseService] 任务 {task.task_id} 推荐到巷道 {aisle}（待现场选择EXECUTING启动）")

        # 保存本轮推荐与已冻结匹配结果，供 /task/unconfirmed 和 executableTasksByAisle 复用。
        assigned_count = sum(1 for t in result.values() if t is not None)
        print(f"[WarehouseService] 调度完成，分配了 {assigned_count} 个任务")

        self._last_matched_tasks_by_aisle = {int(a): list(ts) for a, ts in (matched_by_aisle or {}).items()}
        self._last_recommended_task_by_aisle = {
            int(aisle): getattr(task, "task_id", "")
            for aisle, task in result.items()
            if task is not None and getattr(task, "task_id", None)
        }
        return result, matched_by_aisle


    @property
    # ========================================================================
    # 基础访问：核心状态、时间和任务字段规范化
    # ========================================================================
    def core(self) -> WarehouseCore:
        """获取WarehouseCore实例

        Returns:
            WarehouseCore: 当前处理流程产生的结果，具体结构见函数摘要。
        """
        return self._core

    def get_last_feedback_error(self) -> Optional[str]:
        """读取并返回指定条件下的状态、对象或计算结果，不主动改变业务状态。

        输入：无显式业务输入；依赖实例字段或模块配置。
        输出：Optional[str]

        Returns:
            Optional[str]: 计算得到的结果；当前条件不成立或不存在可用对象时返回 ``None``。
        """
        return self._last_feedback_error

    def get_last_feedback_notice(self) -> Optional[str]:
        """读取并返回指定条件下的状态、对象或计算结果，不主动改变业务状态。

        输入：无显式业务输入；依赖实例字段或模块配置。
        输出：Optional[str]

        Returns:
            Optional[str]: 计算得到的结果；当前条件不成立或不存在可用对象时返回 ``None``。
        """
        return self._last_feedback_notice

    def get_last_schedule_notices(self) -> List[str]:
        """读取并返回指定条件下的状态、对象或计算结果，不主动改变业务状态。

        输入：无显式业务输入；依赖实例字段或模块配置。
        输出：List[str]

        Returns:
            List[str]: 符合当前处理条件的结果列表或集合。
        """
        return list(self._last_schedule_notices)

    def _get_current_time(self) -> float:
        """获取当前仿真时间（从启动开始的秒数）

        Returns:
            float: 当前计算得到的时间、评分或比例数值。
        """
        return time.time() - self._start_time

    def _sku_entry_to_dict(self, sku: Any) -> Dict[str, Any]:
        """将 API 模型、字典或任务对象统一为 SKU 字典。

        输入：``sku`` 可为 JSON 字典、Pydantic v1/v2 模型或带 SKU 属性的对象。
        输出：保留模型字段的字典；普通对象至少生成 ``skuId`` 和 ``quantity``。
        作用：mixed、库存匹配和未提交任务响应都通过该方法读取同一种字段结构。

        Args:
            sku (Any): 当前处理的 SKU 标识。

        Returns:
            Dict[str, Any]: 按函数定义字段组织的结果映射。
        """
        if isinstance(sku, dict):
            return dict(sku)
        if hasattr(sku, "model_dump"):
            return sku.model_dump()
        if hasattr(sku, "dict"):
            return sku.dict()
        return {
            "skuId": getattr(sku, "skuId", None),
            "quantity": getattr(sku, "quantity", 1),
        }

    def _extract_sku_attrs(self, sku_dict: Dict[str, Any]) -> Dict[str, Any]:
        """从 SKU 字典中提取配对属性和 FIFO 元数据。

        输入：已规范化的 ``sku_dict``。
        输出：包含 ``WarehouseCore.match_fields`` 的属性字典；``_inbound_time``
        是内部 FIFO 元数据，不参与双梁属性匹配。

        Args:
            sku_dict (Dict[str, Any]): 已归一化的单个 SKU 字典，包含 skuId、数量和可选匹配属性。

        Returns:
            Dict[str, Any]: 按函数定义字段组织的结果映射。
        """
        match_fields = getattr(self._core, "match_fields", [])
        attrs = {k: sku_dict.get(k) for k in match_fields} if match_fields else {}
        supplied_time = sku_dict.get("inboundTime")
        if supplied_time is None:
            supplied_time = sku_dict.get("arrivalTime")
        if supplied_time is not None:
            attrs["_inbound_time"] = self._resolve_inbound_time(supplied_time)
        return attrs

    def _resolve_inbound_time(self, supplied_time: Any, fallback: Optional[float] = None) -> float:
        """将 API 入库时间归一为服务内部的秒数。

        ``inboundTime`` 支持非负的 ``int``/``float`` Unix 秒，以及 ISO 8601 字符串
        （例如 ``2026-09-03T08:30:00+08:00`` 或以 ``Z`` 结尾的 UTC 时间）。API
        服务没有收到有效值时采用 ``fallback``；首次同步的新库存以当前服务时间作为
        兜底，保证 FIFO 仍有稳定的先后顺序。

        Args:
            supplied_time (Any): 上游传入的 inboundTime 或 arrivalTime。
            fallback (Optional[float]): 未传或格式无效时沿用的内部时间。

        Returns:
            float: 可比较的秒时间；数值越小表示越早入库。
        """
        if isinstance(supplied_time, (int, float)) and not isinstance(supplied_time, bool):
            timestamp = float(supplied_time)
            if timestamp >= 0:
                return timestamp
        if isinstance(supplied_time, str) and supplied_time.strip():
            try:
                return datetime.fromisoformat(supplied_time.strip().replace("Z", "+00:00")).timestamp()
            except ValueError:
                pass
        # API 库存快照与 API 完成反馈均使用 Unix 秒；仿真模式的相对秒时间只在
        # WarehouseCore 的独立事件流内使用，不能与外部快照的真实时间混用。
        return float(time.time() if fallback is None else fallback)

    def _collect_inventory_inbound_times(self) -> Dict[Tuple[str, str, str], float]:
        """读取全量重建前仍有效库存的 FIFO 时间。

        Args:
            None: 读取当前库存货位，不接收额外参数。

        Returns:
            Dict[Tuple[str, str, str], float]: ``(货位ID, 层位, SKU) -> 入库时间`` 映射。
        """
        previous_times: Dict[Tuple[str, str, str], float] = {}
        for position in self._core.inventory_manager.inventory_positions:
            position_id = position.get_position_id()
            if position.is_double_layer:
                layer_entries = (
                    ("UPPER", position.upper_sku, position.upper_quantity, position.upper_attrs),
                    ("LOWER", position.lower_sku, position.lower_quantity, position.lower_attrs),
                )
            else:
                layer_entries = (("SINGLE", position.sku, position.quantity, position.sku_attrs),)
            for layer, sku_id, quantity, attrs in layer_entries:
                inbound_time = (attrs or {}).get("_inbound_time")
                if sku_id and quantity > 0 and isinstance(inbound_time, (int, float)):
                    previous_times[(position_id, layer, str(sku_id))] = float(inbound_time)
        return previous_times

    def _get_value(self, obj: Any, key: str, default: Any = None) -> Any:
        """兼容读取字典和对象属性。

        输入：对象或字典 ``obj``、字段名 ``key``、缺失时返回的 ``default``。
        输出：对应字段值或默认值。该方法只读取数据，用于兼容 API 负载和 TaskData。

        Args:
            obj (Any): 需要兼容读取的字典、Pydantic 对象或普通对象。
            key (str): 从对象或字典读取的字段名称。
            default (Any): 对象缺少目标字段或对象为空时返回的默认值。

        Returns:
            Any: 当前处理流程产生的结果，具体结构见函数摘要。
        """
        if obj is None:
            return default
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    # ========================================================================
    # 辅助函数：出库位置、资源单元与生产组处理
    # ========================================================================
    def _sort_positions(self, positions: List[InventoryPosition]) -> List[InventoryPosition]:
        """按巷道、排、列、层生成稳定货位顺序。

        输入：任意顺序的 ``InventoryPosition`` 列表。
        输出：新的排序列表。该顺序用于候选位置的确定性并列处理，避免同一请求因
        容器遍历顺序不同而得到不同的冻结位置。

        Args:
            positions (List[InventoryPosition]): 当前任务对应的候选或冻结货位列表。

        Returns:
            List[InventoryPosition]: 符合当前处理条件的结果列表或集合。
        """
        return sorted(
            list(positions or []),
            key=lambda p: (
                int(getattr(p, "aisle", 0) or 0),
                int(getattr(p, "row", 0) or 0),
                int(getattr(p, "column", 0) or 0),
                int(getattr(p, "level", 0) or 0),
            ),
        )

    def _make_position_unit_key(self, position: InventoryPosition, shelf: Optional[str]) -> str:
        """返回出库虚拟预占的最小资源单元键。

        双层货位按 ``position_id::UPPER/LOWER`` 区分，单层货位使用 ``single``。
        此键只存在于 API 资源视图，不会写入 ``InventoryPosition.reserved``；后者
        专用于移库预留。由 ``_get_position_units_for_task`` 和预占方法调用。

        Args:
            position (InventoryPosition): 当前处理的货位对象。
            shelf (Optional[str]): 双层货位的层标识；为空时单层货位使用 single，双层按调用方规则解析。

        Returns:
            str: 当前处理得到的文本标识、状态或诊断信息。
        """
        normalized_shelf = (shelf or "single").upper()
        return f"{position.get_position_id()}::{normalized_shelf}"

    def _get_task_sku_attr_pairs(self, task: TaskData) -> List[Tuple[str, Dict[str, Any]]]:
        """读取并返回指定条件下的状态、对象或计算结果，不主动改变业务状态。

        输入：task（TaskData）
        输出：List[Tuple[str, Dict[str, Any]]]

        Args:
            task (TaskData): 当前处理的 TaskData 任务对象。

        Returns:
            List[Tuple[str, Dict[str, Any]]]: 由函数摘要中所列字段组成的多值结果。
        """
        result: List[Tuple[str, Dict[str, Any]]] = []
        for sku in (getattr(task, "skus", None) or []):
            sku_dict = self._sku_entry_to_dict(sku)
            sku_id = sku_dict.get("skuId") or sku_dict.get("sku")
            if not sku_id:
                continue
            result.append((sku_id, self._extract_sku_attrs(sku_dict)))
        return result

    def _get_position_units_for_task(
        self,
        task: TaskData,
        positions: List[InventoryPosition],
    ) -> Optional[List[str]]:
        """把任务已选 positions 解析为其实际消耗的库存资源单元。

        Args:
            task: 出库 ``TaskData``，其中的 SKU/属性定义实际取货要求。
            positions: 冻结或候选的货位列表；双梁同货位时列表可只含一个位置。

        Returns:
            层级资源键列表；位置与 SKU/属性不匹配时返回 ``None``。调用方据此
            判断两个已提交任务是否引用同一层库存，而非只比较 SKU 数量。
        """
        if not positions:
            return None

        sku_pairs = self._get_task_sku_attr_pairs(task)
        if not sku_pairs:
            return None

        units: List[str] = []
        if len(sku_pairs) == 1:
            # 单梁只占用实际命中的一层；不能仅按 position_id 预占整个双层货位，
            # 否则会把同一双层货位另一层的合法任务误判为资源冲突。
            sku_id, attrs = sku_pairs[0]
            position = positions[0]
            if position.is_double_layer:
                if position.matches_sku(sku_id, attrs, self._core.match_fields, shelf="UPPER"):
                    units.append(self._make_position_unit_key(position, "UPPER"))
                elif position.matches_sku(sku_id, attrs, self._core.match_fields, shelf="LOWER"):
                    units.append(self._make_position_unit_key(position, "LOWER"))
                else:
                    return None
            else:
                if not position.matches_sku(sku_id, attrs, self._core.match_fields):
                    return None
                units.append(self._make_position_unit_key(position, None))
            return units

        if len(sku_pairs) == 2:
            if len(positions) == 1:
                # 天然配对双梁共用一个双层货位时，两个 SKU 必须分别绑定上下层；
                # remaining_shelves 防止两个需求在属性相同的情况下重复占用同一层。
                position = positions[0]
                if not position.is_double_layer:
                    return None
                remaining_shelves = {"UPPER", "LOWER"}
                for sku_id, attrs in sku_pairs:
                    if "UPPER" in remaining_shelves and position.matches_sku(sku_id, attrs, self._core.match_fields, shelf="UPPER"):
                        units.append(self._make_position_unit_key(position, "UPPER"))
                        remaining_shelves.discard("UPPER")
                    elif "LOWER" in remaining_shelves and position.matches_sku(sku_id, attrs, self._core.match_fields, shelf="LOWER"):
                        units.append(self._make_position_unit_key(position, "LOWER"))
                        remaining_shelves.discard("LOWER")
                    else:
                        return None
                return units

            # 分散货位的双梁按任务 SKU 顺序逐个解析，返回的单位键仍是层级粒度。
            for idx, (sku_id, attrs) in enumerate(sku_pairs):
                position = positions[min(idx, len(positions) - 1)]
                if position.is_double_layer:
                    if position.matches_sku(sku_id, attrs, self._core.match_fields, shelf="UPPER"):
                        units.append(self._make_position_unit_key(position, "UPPER"))
                    elif position.matches_sku(sku_id, attrs, self._core.match_fields, shelf="LOWER"):
                        units.append(self._make_position_unit_key(position, "LOWER"))
                    else:
                        return None
                else:
                    if not position.matches_sku(sku_id, attrs, self._core.match_fields):
                        return None
                    units.append(self._make_position_unit_key(position, None))
            return units

        return None

    def _reserve_outbound_task_units(
        self,
        task: TaskData,
        positions: List[InventoryPosition],
        occupied_units: Set[str],
        occupied_owner_map: Dict[str, Set[str]],
    ) -> Tuple[bool, Optional[Set[str]]]:
        """尝试将一项出库任务写入 API 虚拟占用集合。

        返回 ``(True, set())`` 表示任务可独占所有解析出的层级资源；返回
        ``(False, blockers)`` 时 ``blockers`` 是已占用这些资源的任务 ID 集合。
        该方法只修改传入的临时集合，供 mixed 分层试算和最终位置冻结调用。

        Args:
            task (TaskData): 当前处理的 TaskData 任务对象。
            positions (List[InventoryPosition]): 当前任务对应的候选或冻结货位列表。
            occupied_units (Set[str]): 已被 submitted 出库任务占用的虚拟资源单元键集合。
            occupied_owner_map (Dict[str, Set[str]]): 虚拟资源单元到占用任务 ID 集合的映射。

        Returns:
            Tuple[bool, Optional[Set[str]]]: 处理成功、条件成立或校验通过时为 ``True``，否则为 ``False``。
        """
        unit_keys = self._get_position_units_for_task(task, positions)
        if not unit_keys:
            return False, None

        blockers: Set[str] = set()
        for unit_key in unit_keys:
            # 先完整收集冲突所有者，不在循环中部分写入；这样失败任务不会留下脏预占。
            if unit_key in occupied_units:
                blockers.update(occupied_owner_map.get(unit_key, set()))
        if blockers:
            return False, blockers

        task_id = getattr(task, "task_id", "")
        for unit_key in unit_keys:
            occupied_units.add(unit_key)
            occupied_owner_map.setdefault(unit_key, set()).add(task_id)
        return True, set()

    def _collect_submitted_outbound_occupancy(self) -> Tuple[Set[str], Dict[str, Set[str]]]:
        """收集已承诺出库任务的虚拟资源占用基线。

        ``running_tasks`` 与 ``pending_outbound_queue`` 中已有冻结 positions 的
        出库任务都先占用资源。mixed 的新任务只能使用剩余资源，因此重复调度
        或跨产线请求不能挤掉先前已提交任务。返回资源键集合及键到任务 ID 的
        所有者映射；真实库存不会在这里扣减。

        Returns:
            Tuple[Set[str], Dict[str, Set[str]]]: 由函数摘要中所列字段组成的多值结果。
        """
        occupied_units: Set[str] = set()
        occupied_owner_map: Dict[str, Set[str]] = {}

        submitted_tasks: List[TaskData] = []
        submitted_tasks.extend(
            task
            for task in self._core.running_tasks.values()
            if getattr(task, "task_type", None) == TASK_TYPE_OUTBOUND
        )
        submitted_tasks.extend(list(getattr(self._core, "pending_outbound_queue", []) or []))

        # 只将已经具备冻结位置的旧任务纳入基线。没有位置的任务尚未形成可验证的
        # 资源承诺，不能仅凭 SKU 数量抢占真实库存。
        for task in submitted_tasks:
            positions = list(getattr(task, "positions", None) or [])
            if not positions:
                continue
            self._reserve_outbound_task_units(task, positions, occupied_units, occupied_owner_map)

        return occupied_units, occupied_owner_map

    def _get_outbound_priority_offset(self, task: TaskData) -> Optional[int]:
        """读取并返回指定条件下的状态、对象或计算结果，不主动改变业务状态。

        输入：task（TaskData）
        输出：Optional[int]

        Args:
            task (TaskData): 当前处理的 TaskData 任务对象。

        Returns:
            Optional[int]: 计算得到的结果；当前条件不成立或不存在可用对象时返回 ``None``。
        """
        state = self._get_outbound_group_state(task)
        if state == "past":
            return None
        if state == "current":
            return 0

        production_line = getattr(task, "production_line", None)
        group_idx = getattr(task, "group_idx", None)
        if production_line is None or group_idx is None:
            return 0
        try:
            normalized_line = int(production_line)
            normalized_group = int(group_idx)
        except (TypeError, ValueError):
            return 0

        if bool(getattr(task, "uses_virtual_group", False)):
            current_group_idx = self._get_virtual_current_group(normalized_line)
        else:
            current_group_idx = self._core.production_line_current_group.get(normalized_line, 0)
        return max(0, normalized_group - current_group_idx)

    def _get_outbound_candidate_positions(self, task: TaskData) -> List[List[InventoryPosition]]:
        """列出任务在真实库存中可直接出库的位置组合，尚不检查虚拟占用。

        单梁返回每个属性匹配的单位置候选；双梁仅返回位于同一双层货位上下层的
        配对候选。下游 ``_find_outbound_positions_with_occupancy`` 会再叠加已提交
        任务的资源冲突过滤，并选定可冻结的第一组位置。

        Args:
            task (TaskData): 当前处理的 TaskData 任务对象。

        Returns:
            List[List[InventoryPosition]]: 符合当前处理条件的结果列表或集合。
        """
        sku_pairs = self._get_task_sku_attr_pairs(task)
        if not sku_pairs:
            return []

        if len(sku_pairs) == 1:
            sku_id, attrs = sku_pairs[0]
            positions = [
                p
                for p in self._core.inventory_manager.get_sku_positions(sku_id, only_available=True)
                if p.matches_sku(sku_id, attrs, self._core.match_fields)
            ]
            return [[position] for position in self._sort_positions(positions)]

        if len(sku_pairs) == 2:
            sku1, attrs1 = sku_pairs[0]
            sku2, attrs2 = sku_pairs[1]
            result: List[List[InventoryPosition]] = []
            for position in self._sort_positions(self._core.inventory_manager.inventory_positions):
                if (
                    position.is_double_layer
                    and position.matches_pair(sku1, attrs1, sku2, attrs2, self._core.match_fields)
                    and position.upper_quantity > 0
                    and position.lower_quantity > 0
                ):
                    result.append([position])
            return result

        return []

    def _find_outbound_positions_with_occupancy(
        self,
        task: TaskData,
        occupied_units: Set[str],
        occupied_owner_map: Dict[str, Set[str]],
        preferred_aisle: Optional[int] = None,
    ) -> Tuple[Optional[List[InventoryPosition]], str, List[str], int]:
        """在候选集合中按既定约束查找匹配对象或可用位置。

        输出：Tuple[Optional[List[InventoryPosition]], str, List[str], int]

        Args:
            task (TaskData): 当前处理的 TaskData 任务对象。
            occupied_units (Set[str]): 已被 submitted 出库任务占用的虚拟资源单元键集合。
            occupied_owner_map (Dict[str, Set[str]]): 虚拟资源单元到占用任务 ID 集合的映射。
            preferred_aisle (Optional[int]): 已由推荐或外部指定的巷道；传入后仅在该巷道内筛选候选位置。

        Returns:
            Tuple[Optional[List[InventoryPosition]], str, List[str], int]: 计算得到的结果；当前条件不成立或不存在可用对象时返回 ``None``。
        """
        candidates = self._get_outbound_candidate_positions(task)
        if not candidates:
            return None, "库存不足", [], 0

        filtered_candidates = candidates
        if preferred_aisle is not None:
            try:
                normalized_preferred_aisle = int(preferred_aisle)
            except (TypeError, ValueError):
                normalized_preferred_aisle = None
            if normalized_preferred_aisle is not None:
                filtered_candidates = [
                    positions
                    for positions in candidates
                    if positions and int(getattr(positions[0], "aisle", 0) or 0) == normalized_preferred_aisle
                ]

        if not filtered_candidates:
            return None, "库存不足", [], len(candidates)

        blocking_task_ids: Set[str] = set()
        for positions in filtered_candidates:
            unit_keys = self._get_position_units_for_task(task, positions)
            if not unit_keys:
                continue
            conflicts = [unit_key for unit_key in unit_keys if unit_key in occupied_units]
            if not conflicts:
                return list(positions), "", [], len(filtered_candidates)
            for conflict in conflicts:
                blocking_task_ids.update(occupied_owner_map.get(conflict, set()))

        reason = "已提交出库任务占用导致当前优先级资源不足" if blocking_task_ids else "库存不足"
        return None, reason, sorted(task_id for task_id in blocking_task_ids if task_id), len(filtered_candidates)

    def _line_has_real_production_plan(self, production_line: Optional[int]) -> bool:
        """判断产线是否存在由 productionPlan 明确提供的真实生产组。

        输入：产线标识。
        输出：该产线计划组非空时返回 ``True``。
        作用：有真实计划时以计划索引推进；无计划时才为外部出库任务维护虚拟组号。

        Args:
            production_line (Optional[int]): 任务所属生产线编号。

        Returns:
            bool: 处理成功、条件成立或校验通过时为 ``True``，否则为 ``False``。
        """
        if production_line is None:
            return False
        try:
            line = int(production_line)
        except (TypeError, ValueError):
            return False
        return bool((getattr(self._core, "production_plan", {}) or {}).get(line))

    def _extract_group_idx_from_task_id(self, task_id: str) -> Optional[int]:
        """从复合对象中提取目标字段，统一不同输入格式的读取方式。

        输入：task_id（str）
        输出：Optional[int]

        Args:
            task_id (str): 任务唯一标识，用于定位队列、冻结位置或反馈对象。

        Returns:
            Optional[int]: 计算得到的结果；当前条件不成立或不存在可用对象时返回 ``None``。
        """
        match = re.search(r"_GP(\d+)(?:_|$)", str(task_id).upper())
        if not match:
            return None
        try:
            public_group = int(match.group(1))
        except (TypeError, ValueError):
            return None
        return public_group - 1 if public_group > 0 else None

    def _iter_known_outbound_tasks(self) -> List[TaskData]:
        """汇总系统已知的 pending、running 和 completed 出库任务。

        输出：按队列、运行态、完成态顺序拼接的任务列表。
        作用：用于推断无 productionPlan 产线的虚拟组号，不能只看 pending 队列，
        否则已执行或已完成的同产线任务会导致组号重复。

        Returns:
            List[TaskData]: 符合当前处理条件的结果列表或集合。
        """
        tasks: List[TaskData] = []
        tasks.extend(list(getattr(self._core, "pending_outbound_queue", []) or []))
        tasks.extend(
            task
            for task in list((getattr(self._core, "running_tasks", {}) or {}).values())
            if getattr(task, "task_type", None) == TASK_TYPE_OUTBOUND
        )
        tasks.extend(
            task
            for task in list(getattr(self._core, "completed_tasks", []) or [])
            if getattr(task, "task_type", None) == TASK_TYPE_OUTBOUND
        )
        return tasks

    def _next_virtual_group_idx(self, production_line: int) -> int:
        """为没有生产计划的产线分配下一个连续虚拟组号。

        输入：内部整型 ``production_line``。
        输出：已知该产线任务最大 ``group_idx`` 加一；没有历史任务时为 0。

        Args:
            production_line (int): 任务所属生产线编号。

        Returns:
            int: 当前计算得到的数量、索引、时间或编号。
        """
        known_groups: List[int] = []
        for task in self._iter_known_outbound_tasks():
            if int(getattr(task, "production_line", 0) or 0) != production_line:
                continue
            group_idx = getattr(task, "group_idx", None)
            if isinstance(group_idx, int):
                known_groups.append(group_idx)
        if not known_groups:
            return 0
        return max(known_groups) + 1

    def _build_line_task_group_counts(self) -> Dict[int, int]:
        """Count existing outbound groups by production line from actual system tasks.

        This is intentionally based on known outbound task instances rather than
        production_plan length. For API ADD semantics, newly submitted outbound
        tasks should append after the currently known task groups on that line,
        even when the production plan already contains future groups with no
        submitted task instances yet.

        Returns:
            Dict[int, int]: 按函数定义字段组织的结果映射。
        """
        line_to_max_group: Dict[int, int] = {}
        for task in self._iter_known_outbound_tasks():
            production_line = getattr(task, "production_line", None)
            group_idx = getattr(task, "group_idx", None)
            if production_line is None or group_idx is None:
                continue
            try:
                normalized_line = int(production_line)
                normalized_group = int(group_idx)
            except (TypeError, ValueError):
                continue
            current_max = line_to_max_group.get(normalized_line, -1)
            if normalized_group > current_max:
                line_to_max_group[normalized_line] = normalized_group
        return {line_id: max_group + 1 for line_id, max_group in line_to_max_group.items()}

    def _assign_outbound_group_metadata(
        self,
        task_data: TaskData,
        plan_id: Any,
        plan_index: Any,
        effective_plan_index: Any,
    ) -> None:
        """执行 `_assign_outbound_group_metadata` 对应的模块处理步骤，并返回该步骤产生的结果。

        输入：task_data（TaskData）、plan_id（Any）、plan_index（Any）、effective_plan_index（Any）
        输出：None

        Args:
            task_data (TaskData): 正在构造生产组元数据的出库任务。
            plan_id (Any): 当前对象的唯一标识。
            plan_index (Any): 接口传入的计划组序号，可能使用从 1 开始的外部口径。
            effective_plan_index (Any): 转换为当前计划合并结果后的内部组索引。

        Returns:
            None: 不返回业务数据；处理结果通过实例状态、队列或传入对象体现。
        """
        production_line = int(getattr(task_data, "production_line", 0) or 0) or 1
        has_real_plan = self._line_has_real_production_plan(production_line)

        task_data.plan_id = plan_id
        task_data.plan_index_public = effective_plan_index

        if has_real_plan:
            task_data.group_idx = int(effective_plan_index) - 1 if effective_plan_index is not None else None
            task_data.uses_virtual_group = False
            return

        virtual_group_idx: Optional[int] = None
        if effective_plan_index is not None:
            virtual_group_idx = int(effective_plan_index) - 1
        else:
            virtual_group_idx = self._extract_group_idx_from_task_id(getattr(task_data, "task_id", ""))
        if virtual_group_idx is None:
            virtual_group_idx = self._next_virtual_group_idx(production_line)

        task_data.group_idx = virtual_group_idx
        task_data.plan_index_public = virtual_group_idx + 1
        task_data.uses_virtual_group = True
        self._virtual_outbound_current_group.setdefault(production_line, virtual_group_idx)

    def _get_virtual_current_group(self, production_line: int) -> int:
        """读取并返回指定条件下的状态、对象或计算结果，不主动改变业务状态。

        输入：production_line（int）
        输出：int

        Args:
            production_line (int): 任务所属生产线编号。

        Returns:
            int: 当前计算得到的数量、索引、时间或编号。
        """
        if production_line not in self._virtual_outbound_current_group:
            known_groups: List[int] = []
            for task in self._iter_known_outbound_tasks():
                if int(getattr(task, "production_line", 0) or 0) != production_line:
                    continue
                group_idx = getattr(task, "group_idx", None)
                if isinstance(group_idx, int):
                    known_groups.append(group_idx)
            self._virtual_outbound_current_group[production_line] = min(known_groups) if known_groups else 0
        return self._virtual_outbound_current_group[production_line]

    def _advance_virtual_outbound_group(self, production_line: int) -> None:
        """执行 `_advance_virtual_outbound_group` 对应的模块处理步骤，并返回该步骤产生的结果。

        输入：production_line（int）
        输出：None

        Args:
            production_line (int): 任务所属生产线编号。

        Returns:
            None: 不返回业务数据；处理结果通过实例状态、队列或传入对象体现。
        """
        current_group = self._get_virtual_current_group(production_line)
        remaining_groups: List[int] = []
        remaining_tasks: List[TaskData] = []
        remaining_tasks.extend(list(getattr(self._core, "pending_outbound_queue", []) or []))
        remaining_tasks.extend(
            task
            for task in list((getattr(self._core, "running_tasks", {}) or {}).values())
            if getattr(task, "task_type", None) == TASK_TYPE_OUTBOUND
        )
        for task in remaining_tasks:
            if int(getattr(task, "production_line", 0) or 0) != production_line:
                continue
            if not bool(getattr(task, "uses_virtual_group", False)):
                continue
            group_idx = getattr(task, "group_idx", None)
            if isinstance(group_idx, int):
                remaining_groups.append(group_idx)

        if current_group in remaining_groups:
            return

        future_groups = sorted(group for group in set(remaining_groups) if group > current_group)
        if future_groups:
            self._virtual_outbound_current_group[production_line] = future_groups[0]
        else:
            self._virtual_outbound_current_group[production_line] = current_group + 1

    def _line_id_to_int(self, line_id: Any) -> int:
        """执行 `_line_id_to_int` 对应的模块处理步骤，并返回该步骤产生的结果。

        输入：line_id（Any）
        输出：int

        Args:
            line_id (Any): 生产线编号。

        Returns:
            int: 当前计算得到的数量、索引、时间或编号。
        """
        text = str(line_id).strip().upper()
        if text.startswith("LINE-"):
            text = text.split("LINE-", 1)[1]
        elif text.startswith("LINE"):
            text = text.split("LINE", 1)[1]
        return int(text)

    # ========================================================================
    # 阶段处理：生产计划同步与生产组推进
    # ========================================================================
    def _build_production_plan_payload(self, plan_request: Any) -> Dict[str, Any]:
        """构造下游调用所需的对象、请求载荷或配置结果。

        输入：plan_request（Any）
        输出：Dict[str, Any]

        Args:
            plan_request (Any): 包含 planId、lineId 和 planIndex 的生产计划请求。

        Returns:
            Dict[str, Any]: 按函数定义字段组织的结果映射。
        """
        production_plan: Dict[int, List] = {}
        match_fields = list(getattr(self._core, "match_fields", []) or [])
        production_plan_attrs: Dict[str, Dict[int, List]] = {field: {} for field in match_fields}
        plan_id_to_line: Dict[str, int] = {}

        plans = self._get_value(plan_request, "plans", []) or []
        for plan in plans:
            line_id = self._line_id_to_int(self._get_value(plan, "lineId"))
            plan_id = self._get_value(plan, "planId")
            if plan_id:
                plan_id_to_line[str(plan_id)] = line_id

            groups = []
            attrs_by_field = {field: [] for field in match_fields}
            for group in self._get_value(plan, "planIndex", []) or []:
                tasks_in_group = []
                group_attrs_by_field = {field: [] for field in match_fields}
                for task_skus in self._get_value(group, "requiredSkus", []) or []:
                    sku_list = []
                    task_attrs_by_field = {field: [] for field in match_fields}
                    for sku in task_skus:
                        sku_dict = self._sku_entry_to_dict(sku)
                        sku_id = sku_dict.get("skuId") or sku_dict.get("sku")
                        quantity = int(sku_dict.get("quantity", 1) or 1)
                        for _ in range(quantity):
                            sku_list.append(sku_id)
                            for field in match_fields:
                                task_attrs_by_field[field].append(sku_dict.get(field))
                    tasks_in_group.append(sku_list)
                    for field in match_fields:
                        group_attrs_by_field[field].append(task_attrs_by_field[field])
                groups.append(tasks_in_group)
                for field in match_fields:
                    attrs_by_field[field].append(group_attrs_by_field[field])

            existing_groups = production_plan.get(line_id, [])
            production_plan[line_id] = deepcopy(existing_groups) + groups
            for field in match_fields:
                existing_attrs = production_plan_attrs[field].get(line_id, [])
                production_plan_attrs[field][line_id] = deepcopy(existing_attrs) + attrs_by_field[field]

        self._plan_id_to_line = plan_id_to_line
        return {
            "production_plan": production_plan,
            "production_plan_attrs": production_plan_attrs if match_fields else {},
        }

    def _update_plan_id_mappings(self, plan_request: Any, operation_value: str) -> None:
        """基于输入更新当前对象或关联状态，并返回更新后的结果。

        输入：plan_request（Any）、operation_value（str）
        输出：None

        Args:
            plan_request (Any): 包含 planId、lineId 和 planIndex 的生产计划请求。
            operation_value (str): 计划同步操作类型，例如 ADD 或 UPDATE。

        Returns:
            None: 不返回业务数据；处理结果通过实例状态、队列或传入对象体现。
        """
        plans = self._get_value(plan_request, "plans", []) or []
        base_group_counts = {
            int(line_id): len(groups)
            for line_id, groups in (getattr(self._core, "production_plan", {}) or {}).items()
        }
        self._line_id_to_base_group_count = dict(base_group_counts) if operation_value == "ADD" else {}
        self._line_id_to_base_task_group_count = (
            self._build_line_task_group_counts() if operation_value == "ADD" else {}
        )
        self._current_plan_operation = str(operation_value or "").upper()
        line_offsets = dict(base_group_counts) if operation_value == "ADD" else {}

        if operation_value != "ADD":
            self._plan_id_to_line = {}
            self._plan_id_to_group_offset = {}
            self._plan_id_to_group_count = {}

        for plan in plans:
            line_id = self._line_id_to_int(self._get_value(plan, "lineId"))
            plan_id = self._get_value(plan, "planId")
            group_count = len(self._get_value(plan, "planIndex", []) or [])
            offset = int(line_offsets.get(line_id, 0))
            if plan_id:
                plan_key = str(plan_id)
                self._plan_id_to_line[plan_key] = line_id
                self._plan_id_to_group_offset[plan_key] = offset
                self._plan_id_to_group_count[plan_key] = group_count
            line_offsets[line_id] = offset + group_count

    def _normalize_production_line_current_group(self, current_group_map: Any) -> Dict[int, int]:
        """执行 `_normalize_production_line_current_group` 对应的模块处理步骤，并返回该步骤产生的结果。

        输入：current_group_map（Any）
        输出：Dict[int, int]

        Args:
            current_group_map (Any): 当前流程使用的键值映射。

        Returns:
            Dict[int, int]: 按函数定义字段组织的结果映射。
        """
        if not current_group_map:
            return {}
        if not isinstance(current_group_map, dict):
            raise ValueError("productionLineCurrentGroup must be a dict like {'LINE-1': 0}")

        normalized: Dict[int, int] = {}
        for line_id, core_group_idx in current_group_map.items():
            line = self._line_id_to_int(line_id)
            idx = int(core_group_idx)
            if idx < 0:
                raise ValueError("productionLineCurrentGroup uses core 0-based indexes; values must be >= 0")
            total_groups = len((getattr(self._core, "production_plan", {}) or {}).get(line, []))
            if total_groups and idx > total_groups:
                raise ValueError(
                    f"productionLineCurrentGroup line {line} points to core group {idx}, "
                    f"but the plan only has {total_groups} groups"
                )
            normalized[line] = idx
        return normalized

    def apply_production_line_current_group(self, current_group_map: Any) -> None:
        """执行 `apply_production_line_current_group` 对应的模块处理步骤，并返回该步骤产生的结果。

        输入：current_group_map（Any）
        输出：None

        Args:
            current_group_map (Any): 当前流程使用的键值映射。

        Returns:
            None: 不返回业务数据；处理结果通过实例状态、队列或传入对象体现。
        """
        for line, core_group_idx in self._normalize_production_line_current_group(current_group_map).items():
            self._core.production_line_current_group[line] = core_group_idx
            self._core.production_line_completed_tasks.setdefault(line, set())
            self._core.production_line_group_completion_times.setdefault(line, [])
            print(
                f"[WarehouseService] 产线 {line} 当前组同步为 core={core_group_idx}, public={core_group_idx + 1}"
            )

    def _normalize_current_groups(
        self,
        current_groups: Any,
        production_plan_map: Optional[Dict[int, List]] = None,
        notices: Optional[List[str]] = None,
    ) -> Dict[int, int]:
        """执行 `_normalize_current_groups` 对应的模块处理步骤，并返回该步骤产生的结果。

        输出：Dict[int, int]

        Args:
            current_groups (Any): 外部传入的各产线当前生产组位置。
            production_plan_map (Optional[Dict[int, List]]): 当前流程使用的键值映射。
            notices (Optional[List[str]]): 同步过程中收集并返回给调用方的兼容或忽略提示。

        Returns:
            Dict[int, int]: 按函数定义字段组织的结果映射。
        """
        if not current_groups:
            return {}

        if isinstance(current_groups, dict):
            iterable = current_groups.items()
        else:
            if isinstance(current_groups, (str, bytes)) or not hasattr(current_groups, "__iter__"):
                raise ValueError("currentGroups must be a dict like {'LINE-1': 1} or a list of {lineId, currentGroup}")
            iterable = []
            for item in current_groups:
                line_id = self._get_value(item, "lineId")
                group_number = self._get_value(item, "currentGroup")
                if group_number is None:
                    group_number = self._get_value(item, "group")
                if group_number is None:
                    group_number = self._get_value(item, "planIndex")
                iterable.append((line_id, group_number))

        normalized: Dict[int, int] = {}
        for line_id, public_group in iterable:
            if line_id is None or public_group is None:
                raise ValueError("currentGroups entries must include lineId and currentGroup")
            line = self._line_id_to_int(line_id)
            public_group_int = int(public_group)
            if public_group_int == 0:
                if notices is not None:
                    notices.append(f"产线 {line} 的 currentGroups=0 已自动按第 1 组处理")
                public_group_int = 1
            elif public_group_int < 0:
                raise ValueError("currentGroups 使用对外 1-based 组号，取值不能小于 0")
            core_group_idx = public_group_int - 1
            target_plan = production_plan_map if production_plan_map is not None else (getattr(self._core, "production_plan", {}) or {})
            total_groups = len((target_plan or {}).get(line, []))
            if total_groups and core_group_idx > total_groups:
                raise ValueError(
                    f"产线 {line} 的 currentGroups={public_group_int} 超出计划组上限，当前计划仅有 {total_groups} 组"
                )
            normalized[line] = core_group_idx
        return normalized

    def apply_current_groups(self, current_groups: Any) -> None:
        """执行 `apply_current_groups` 对应的模块处理步骤，并返回该步骤产生的结果。

        输入：current_groups（Any）
        输出：None

        Args:
            current_groups (Any): 外部传入的各产线当前生产组位置。

        Returns:
            None: 不返回业务数据；处理结果通过实例状态、队列或传入对象体现。
        """
        for line, core_group_idx in self._normalize_current_groups(current_groups).items():
            self._core.production_line_current_group[line] = core_group_idx
            self._core.production_line_completed_tasks.setdefault(line, set())
            self._core.production_line_group_completion_times.setdefault(line, [])
            print(
                f"[WarehouseService] 产线 {line} 当前组同步为 public={core_group_idx + 1}, core={core_group_idx}"
            )

    def _build_effective_plan_payload(self, payload: Dict[str, Any], replace_existing: bool) -> Dict[str, Any]:
        """构造下游调用所需的对象、请求载荷或配置结果。

        输入：payload（Dict[str, Any]）、replace_existing（bool）
        输出：Dict[str, Any]

        Args:
            payload (Dict[str, Any]): 接口或内部流程使用的原始数据载荷。
            replace_existing (bool): 是否用本次生产计划完全替换已保存的计划。

        Returns:
            Dict[str, Any]: 按函数定义字段组织的结果映射。
        """
        if replace_existing:
            return payload

        merged_plan = {
            int(line_id): deepcopy(groups)
            for line_id, groups in (getattr(self._core, "production_plan", {}) or {}).items()
            if groups
        }
        for line_id, groups in (payload.get("production_plan") or {}).items():
            line_id = int(line_id)
            existing_groups = merged_plan.get(line_id, [])
            merged_plan[line_id] = deepcopy(existing_groups) + deepcopy(groups)

        merged_attrs = deepcopy(getattr(self._core, "production_plan_attrs", {}) or {})
        for field, by_line in (payload.get("production_plan_attrs") or {}).items():
            merged_attrs.setdefault(field, {})
            for line_id, attrs in by_line.items():
                line_id = int(line_id)
                existing_attrs = merged_attrs[field].get(line_id, [])
                merged_attrs[field][line_id] = deepcopy(existing_attrs) + deepcopy(attrs)

        return {
            "production_plan": merged_plan,
            "production_plan_attrs": merged_attrs,
        }

    def apply_inline_schedule_plan(
        self,
        schedule_request: Any,
        task_manager: Optional[TaskStateManager] = None,
    ) -> bool:
        """执行 `apply_inline_schedule_plan` 对应的模块处理步骤，并返回该步骤产生的结果。

        输出：bool

        Args:
            schedule_request (Any): mixed 请求对象，包含任务、库存、计划和状态同步字段。
            task_manager (Optional[TaskStateManager]): 当前函数使用的 `task_manager` 参数。

        Returns:
            bool: 处理成功、条件成立或校验通过时为 ``True``，否则为 ``False``。
        """
        self._last_schedule_notices = []
        inline_plan = self._get_value(schedule_request, "productionPlan")
        if inline_plan is None and self._get_value(schedule_request, "plans") is not None:
            inline_plan = schedule_request

        current_groups = self._get_value(schedule_request, "currentGroups")
        production_line_current_group = self._get_value(schedule_request, "productionLineCurrentGroup")
        if inline_plan is None and current_groups is None and not production_line_current_group:
            return True

        if inline_plan is not None:
            payload = self._build_production_plan_payload(inline_plan)
            operation_type = self._get_value(inline_plan, "operationType")
            operation_value = str(getattr(operation_type, "value", operation_type or "UPDATE")).upper()
            reset_assigned = bool(self._get_value(inline_plan, "resetAssigned", False))
            # mixed 内嵌计划通常提交完整最新计划；同时保留 ADD 模式的合并兼容性。
            replace_existing = operation_value != "ADD"
            effective_payload = self._build_effective_plan_payload(payload, replace_existing=replace_existing)
            if current_groups is not None:
                self._normalize_current_groups(
                    current_groups,
                    production_plan_map=effective_payload.get("production_plan") or {},
                    notices=self._last_schedule_notices,
                )
            self._update_plan_id_mappings(inline_plan, operation_value)
            if not self.set_production_plan(payload, update=replace_existing):
                return False
            if replace_existing:
                if reset_assigned:
                    self._clear_assigned_and_running_state(task_manager=task_manager)
                else:
                    self._clear_undispatched_pending_tasks()

        if current_groups is not None:
            self.apply_current_groups(current_groups)
        elif production_line_current_group:
            self.apply_production_line_current_group(production_line_current_group)

        return True

    def _build_bom_snapshot(self) -> Dict[str, Any]:
        """构造下游调用所需的对象、请求载荷或配置结果。

        输入：无显式业务输入；依赖实例字段或模块配置。
        输出：Dict[str, Any]

        Returns:
            Dict[str, Any]: 按函数定义字段组织的结果映射。
        """
        config_data = getattr(self._core, "config_data", {}) or {}
        return {
            "sku_types": list(deepcopy(config_data.get("sku_types", []) or [])),
            "sku_to_production_line": dict(deepcopy(config_data.get("sku_to_production_line", {}) or {})),
        }

    def _get_known_sku_ids(self) -> set:
        """读取并返回指定条件下的状态、对象或计算结果，不主动改变业务状态。

        输入：无显式业务输入；依赖实例字段或模块配置。
        输出：set

        Returns:
            set: 符合当前处理条件的结果列表或集合。
        """
        static_config = getattr(self, "_bom_config_snapshot", {}) or {}
        known = set(static_config.get("sku_types", []) or [])
        known.update((static_config.get("sku_to_production_line", {}) or {}).keys())
        return known

    def find_invalid_skus(self, tasks: List[Any], task_types: Optional[List[str]] = None) -> Dict[str, List[str]]:
        """在候选集合中按既定约束查找匹配对象或可用位置。

        输出：Dict[str, List[str]]

        Args:
            tasks (List[Any]): 参与当前调度、过滤或统计的任务集合。
            task_types (Optional[List[str]]): 需要参与查询或冲突检查的任务类型过滤集合。

        Returns:
            Dict[str, List[str]]: 符合当前处理条件的结果列表或集合。
        """
        known_skus = self._get_known_sku_ids()
        invalid_skus: set = set()
        task_ids: List[str] = []
        task_types_upper = {str(t).upper() for t in task_types} if task_types else None

        for task in tasks:
            # ``tasks`` 同时兼容 Pydantic 请求模型和 JSON 映射。统一通过
            # _get_value 读取，避免静态检查把字典误判为带 taskId/skus 属性的对象。
            task_id = self._get_value(task, "taskId")
            task_type = self._get_value(task, "taskType")

            task_type_value = getattr(task_type, "value", task_type)
            if task_types_upper is not None and task_type_value is not None and str(task_type_value).upper() not in task_types_upper:
                continue
            skus = self._get_value(task, "skus", []) or []
            task_invalid = False
            for sku in skus:
                sku_dict = self._sku_entry_to_dict(sku)
                sku_id = sku_dict.get("skuId") or sku_dict.get("sku")
                quantity = sku_dict.get("quantity", 1)
                if sku_id and quantity > 0 and sku_id not in known_skus:
                    invalid_skus.add(sku_id)
                    task_invalid = True
            if task_invalid and task_id:
                task_ids.append(task_id)

        return {
            "invalidSkus": sorted(invalid_skus),
            "taskIds": task_ids,
        }

    def find_duplicate_task_ids(self, tasks: List[Any]) -> List[str]:
        """在候选集合中按既定约束查找匹配对象或可用位置。

        输入：tasks（List[Any]）
        输出：List[str]

        Args:
            tasks (List[Any]): 参与当前调度、过滤或统计的任务集合。

        Returns:
            List[str]: 符合当前处理条件的结果列表或集合。
        """
        seen: set = set()
        duplicates: set = set()
        for task in tasks:
            task_id = self._get_value(task, "taskId")
            if not task_id:
                continue
            task_id = str(task_id)
            if task_id in seen:
                duplicates.add(task_id)
            else:
                seen.add(task_id)
        return sorted(duplicates)

    def _collect_active_task_ids(self) -> set:
        """遍历相关状态集合，汇总当前流程需要的对象或占用信息。

        输入：无显式业务输入；依赖实例字段或模块配置。
        输出：set

        Returns:
            set: 符合当前处理条件的结果列表或集合。
        """
        active_ids: set = set()
        active_ids.update(str(task_id) for task_id in self._pending_execution_tasks.keys())
        active_ids.update(str(task_id) for task_id in self._core.running_tasks.keys())
        active_ids.update(
            str(getattr(task, "task_id", ""))
            for queue in self._core.pending_inbound_by_aisle.values()
            for task in queue
            if getattr(task, "task_id", None)
        )
        active_ids.update(
            str(getattr(task, "task_id", ""))
            for task in self._core.pending_outbound_queue
            if getattr(task, "task_id", None)
        )
        return active_ids

    def find_existing_task_id_conflicts(self, tasks: List[Any]) -> List[str]:
        """在候选集合中按既定约束查找匹配对象或可用位置。

        输入：tasks（List[Any]）
        输出：List[str]

        Args:
            tasks (List[Any]): 参与当前调度、过滤或统计的任务集合。

        Returns:
            List[str]: 符合当前处理条件的结果列表或集合。
        """
        active_ids = self._collect_active_task_ids()
        conflicts: set = set()
        for task in tasks:
            task_id = self._get_value(task, "taskId")
            if task_id and str(task_id) in active_ids:
                conflicts.add(str(task_id))
        return sorted(conflicts)

    def _external_to_internal_row(self, external_row: int, aisle: int) -> int:
        """
        将外部 API 的 row 编号转换为内部 row 编号
        外部 row = 2 * (aisle - 1) + 内部 row
        因此：内部 row = 外部 row - 2 * (aisle - 1)

        Args:
            external_row: 外部 row 编号 (1-10 for 5 aisles)
            aisle: 巷道编号 (1-5)

        Returns:
            内部 row 编号 (1-2)
        """
        internal_row = int(external_row) - 2 * (int(aisle) - 1)
        if internal_row not in (1, 2):
            raise ValueError(
                f"row={external_row} 不属于 aisle={aisle}（该巷道仅允许 row={2 * (int(aisle) - 1) + 1} 或 {2 * (int(aisle) - 1) + 2}）"
            )
        return internal_row

    def _internal_to_external_row(self, internal_row: int, aisle: int) -> int:
        """
        将内部 row 编号转换为外部 API 的 row 编号
        外部 row = 2 * (aisle - 1) + 内部 row

        Args:
            internal_row: 内部 row 编号 (1-2)
            aisle: 巷道编号 (1-5)

        Returns:
            外部 row 编号 (1-10 for 5 aisles)
        """
        external_row = 2 * (aisle - 1) + internal_row
        return external_row

    def _api_row_to_internal_row(self, api_row: int, aisle: int) -> int:
        """将 API 请求中的 row 转换为内部 row。

        兼容两种写法：
        1. 巷道内局部 row：1 / 2
        2. 旧版全局 row：2 * (aisle - 1) + row

        Args:
            api_row (int): 外部接口使用的货位排号，需要按巷道规则转换为内部排号。
            aisle (int): 目标巷道编号。

        Returns:
            int: 当前计算得到的数量、索引、时间或编号。
        """
        api_row = int(api_row)
        aisle = int(aisle)
        if api_row in (1, 2):
            return api_row
        return self._external_to_internal_row(api_row, aisle)

    def _serialize_position_for_api(self, position: Optional[InventoryPosition]) -> Optional[Dict[str, Any]]:
        """将内部货位对象序列化为 API 返回结构，并把 row 转回外部口径。

        Args:
            position (Optional[InventoryPosition]): 当前处理的货位对象。

        Returns:
            Optional[Dict[str, Any]]: 计算得到的结果；当前条件不成立或不存在可用对象时返回 ``None``。
        """
        if position is None:
            return None

        return {
            "aisle": position.aisle,
            "row": self._internal_to_external_row(position.row, position.aisle),
            "column": position.column,
            "level": position.level,
            "is_double_layer": position.is_double_layer,
            "sku": getattr(position, "sku", ""),
            "quantity": getattr(position, "quantity", 0),
            "sku_attrs": dict(getattr(position, "sku_attrs", {}) or {}),
            "upper_sku": getattr(position, "upper_sku", None),
            "upper_quantity": getattr(position, "upper_quantity", 0),
            "lower_sku": getattr(position, "lower_sku", None),
            "lower_quantity": getattr(position, "lower_quantity", 0),
            "upper_attrs": dict(getattr(position, "upper_attrs", {}) or {}),
            "lower_attrs": dict(getattr(position, "lower_attrs", {}) or {}),
            "reserved": getattr(position, "reserved", False),
            "disabled": getattr(position, "disabled", False),
        }

    def build_task_api_positions(self, task_obj: Optional[TaskData]) -> List[Dict[str, Any]]:
        """Build stable external positions payload for a task.

        Args:
            task_obj (Optional[TaskData]): 需要读取或冻结位置的任务实例；为空时直接返回空位置结果。

        Returns:
            List[Dict[str, Any]]: 符合当前处理条件的结果列表或集合。
        """
        if task_obj is None:
            return []

        positions: List[Dict[str, Any]] = []
        match_fields = list(getattr(self._core, "match_fields", []) or [])
        task_skus: List[Dict[str, Any]] = []
        for sku in (getattr(task_obj, "skus", None) or []):
            task_skus.append(self._sku_entry_to_dict(sku))

        def append_position_payload(pos: InventoryPosition, shelf: Optional[str], sku_id: str, quantity: int, sku_attrs: Optional[Dict[str, Any]] = None) -> None:
            """执行 `append_position_payload` 对应的模块处理步骤，并返回该步骤产生的结果。

            输入：pos（InventoryPosition）、shelf（Optional[str]）、sku_id（str）、quantity（int）、sku_attrs（Optional[Dict[str, Any]]，可选参数）
            输出：None

            Args:
                pos (InventoryPosition): 当前处理的 InventoryPosition 货位对象。
                shelf (Optional[str]): 双层货位层标识，通常为 upper 或 lower。
                sku_id (str): 当前处理的 SKU 标识。
                quantity (int): 本次写入、扣减或校验的数量。
                sku_attrs (Optional[Dict[str, Any]]，可选): 按 match_fields 提取的 SKU 匹配属性。

            Returns:
                None: 不返回业务数据；处理结果写入实例状态、传入对象或外部响应。
            """
            payload: Dict[str, Any] = {
                "row": 2 * (int(pos.aisle) - 1) + int(pos.row),
                "column": int(pos.column),
                "level": int(pos.level),
                "shelf": shelf,
                "skuId": sku_id,
                "quantity": quantity,
            }
            for field in match_fields:
                if sku_attrs and field in sku_attrs:
                    payload[field] = sku_attrs.get(field)
            positions.append(payload)

        is_outbound_task = getattr(task_obj, "task_type", "") == TASK_TYPE_OUTBOUND
        task_positions = list(getattr(task_obj, "positions", None) or [])

        for idx, pos in enumerate(task_positions):
            task_sku_dict = task_skus[idx] if idx < len(task_skus) else {}

            if getattr(pos, "is_double_layer", False):
                if is_outbound_task:
                    if getattr(pos, "upper_quantity", 0) > 0 and getattr(pos, "upper_sku", None):
                        append_position_payload(
                            pos,
                            "UPPER",
                            getattr(pos, "upper_sku", "") or "",
                            int(getattr(pos, "upper_quantity", 0) or 0),
                            dict(getattr(pos, "upper_attrs", {}) or {}),
                        )
                    if getattr(pos, "lower_quantity", 0) > 0 and getattr(pos, "lower_sku", None):
                        append_position_payload(
                            pos,
                            "LOWER",
                            getattr(pos, "lower_sku", "") or "",
                            int(getattr(pos, "lower_quantity", 0) or 0),
                            dict(getattr(pos, "lower_attrs", {}) or {}),
                        )
                    continue

                sku_id = task_sku_dict.get("skuId", "") or ""
                quantity = int(task_sku_dict.get("quantity", 0) or 1)
                if not sku_id:
                    continue

                # 两根入库梁分配到同一双层货位时，必须保持 mixed/adjust 输出中已经确定的
                # UPPER/LOWER 顺序，避免库存同步后上下层与任务固化位置不一致。
                same_slot_pair = (
                    len(task_skus) >= 2
                    and len(task_positions) >= 2
                    and task_positions.count(pos) >= 2
                )
                if same_slot_pair:
                    row_val = int(getattr(pos, "row", 0) or 0)
                    if row_val == 1:
                        shelf = "UPPER" if idx == 0 else "LOWER"
                    elif row_val == 2:
                        shelf = "LOWER" if idx == 0 else "UPPER"
                    else:
                        shelf = "UPPER" if idx == 0 else "LOWER"
                    append_position_payload(pos, shelf, sku_id, quantity, task_sku_dict)
                    continue

                has_upper_space = getattr(pos, "upper_quantity", 0) == 0
                has_lower_space = getattr(pos, "lower_quantity", 0) == 0
                if has_upper_space and has_lower_space:
                    append_position_payload(pos, "UPPER", sku_id, quantity, task_sku_dict)
                elif has_upper_space:
                    append_position_payload(pos, "UPPER", sku_id, quantity, task_sku_dict)
                elif has_lower_space:
                    append_position_payload(pos, "LOWER", sku_id, quantity, task_sku_dict)
                continue

            sku_id = getattr(pos, "sku", "") or task_sku_dict.get("skuId", "") or ""
            quantity = int(getattr(pos, "quantity", 0) or task_sku_dict.get("quantity", 0) or 1)
            if sku_id:
                append_position_payload(pos, None, sku_id, quantity, dict(getattr(pos, "sku_attrs", {}) or task_sku_dict))

        return positions

    def freeze_task_api_positions(self, task_obj: Optional[TaskData]) -> List[Dict[str, Any]]:
        """Persist the current external positions payload on the task object.

        Args:
            task_obj (Optional[TaskData]): 需要读取或冻结位置的任务实例；为空时直接返回空位置结果。

        Returns:
            List[Dict[str, Any]]: 符合当前处理条件的结果列表或集合。
        """
        frozen = self.build_task_api_positions(task_obj)
        if task_obj is not None:
            setattr(task_obj, "_api_positions", deepcopy(frozen))
        return frozen

    def get_task_api_positions(self, task_obj: Optional[TaskData]) -> List[Dict[str, Any]]:
        """Return frozen external positions when available; otherwise build and freeze them.

        Args:
            task_obj (Optional[TaskData]): 需要读取或冻结位置的任务实例；为空时直接返回空位置结果。

        Returns:
            List[Dict[str, Any]]: 符合当前处理条件的结果列表或集合。
        """
        if task_obj is None:
            return []
        frozen = getattr(task_obj, "_api_positions", None)
        if frozen:
            return deepcopy(list(frozen))
        return deepcopy(self.freeze_task_api_positions(task_obj))

    # ========================================================================
    # 阶段处理：库存、巷道状态和外部任务同步
    # ========================================================================
    def _sync_time(self):
        """同步仿真时间

        Returns:
            None: 通过实例状态、队列或外部副作用完成处理。
        """
        current = self._get_current_time()
        self._core.current_time = current
        self._last_sync_time = time.time()

    # ============================================================
    # 状态同步方法
    # ============================================================

    # 存储巷道可用性状态（用于调度决策）
    _aisle_availability: Dict[int, Dict[str, Any]] = {}

    def sync_aisle_status(self, aisle_status_list: List[Any]) -> None:
        """同步巷道状态到warehouse_core

        每次mixed调度请求时调用，根据外部系统提供的状态更新内部状态。

        更新的参数：
        - blockage_status: 各巷道*产线的拥堵状态
        - _aisle_availability: 巷道可用性（维护/故障/占用）

        Args:
            aisle_status_list: API请求中的巷道状态列表


        Returns:
            None: 不返回业务数据；处理结果通过实例状态、队列或传入对象体现。
        """
        self._sync_time()
        print(f"[WarehouseService] 同步巷道状态，共 {len(aisle_status_list)} 个巷道")

        for status in aisle_status_list:
            aisle_id = int(status.aisleId) if hasattr(status, 'aisleId') else int(status['aisleId'])
            is_available = status.isAvailable if hasattr(status, 'isAvailable') else status['isAvailable']
            unavailable_reason = status.unavailableReason if hasattr(status, 'unavailableReason') else status.get('unavailableReason')
            bank = status.bank if hasattr(status, 'bank') else status.get('bank')

            # 存储巷道可用性状态
            self._aisle_availability[aisle_id] = {
                'is_available': is_available,
                'unavailable_reason': unavailable_reason,
                'bank': bank
            }

            # 如果巷道不可用（维护/故障/占用），阻塞所有产线
            if not is_available:
                for pl in range(1, self._core.num_production_lines + 1):
                    self._core.update_blockage_status(
                        aisle=aisle_id,
                        production_line=pl,
                        blocked=True,
                        # 由外部反馈解除
                        unblock_time=self._core.current_time + self._core.outbound_congestion_time
                    )
                print(f"[WarehouseService] 巷道 {aisle_id} 不可用: {unavailable_reason}")
                continue

            # 更新各产线拥堵状态
            exit_congestion = status.exitCongestion if hasattr(status, 'exitCongestion') else status.get('exitCongestion', [])

            for congestion in exit_congestion:
                line_id_str = congestion.lineId if hasattr(congestion, 'lineId') else congestion['lineId']
                line_id = int(line_id_str.replace("LINE-", "")) if "LINE-" in str(line_id_str) else int(line_id_str)
                is_congested = congestion.isCongested if hasattr(congestion, 'isCongested') else congestion['isCongested']

                if is_congested:
                    # 外部 ``isCongested=True`` 表示下游仍在持续拥堵，不应按本地
                    # ``outbound_congestion_time`` 自动恢复；只有后续 API 明确传入
                    # ``isCongested=False`` 时才解除该巷道和产线组合的阻塞。
                    self._core.update_blockage_status(
                        aisle=aisle_id,
                        production_line=line_id,
                        blocked=True,
                        unblock_time=float("inf"),
                    )
                    print(f"[WarehouseService] 巷道 {aisle_id} 产线 {line_id} 拥堵")
                else:
                    # 解除拥堵
                    self._core.update_blockage_status(
                        aisle=aisle_id,
                        production_line=line_id,
                        blocked=False,
                        unblock_time=0.0
                    )

    def sync_inventory(self, inventory_list: List[Any]) -> None:
        """同步库存状态到warehouse_core。

        当前语义：
        - 数量 = 0：保持原有库存状态不变
        - 数量 > 0：按本次传入内容进行全量重置

        更新的参数：
        - inventory_manager.current_inventory: 各巷道各SKU的数量统计
        - inventory_manager.inventory_positions: 每个货位的详细状态
        - inventory_manager.sku_position_index: SKU到货位的索引

        Args:
            inventory_list: API请求中的库存信息列表


        Returns:
            None: 不返回业务数据；处理结果通过实例状态、队列或传入对象体现。
        """
        self._sync_time()

        # 空数组的语义是“本次未同步库存”，不是清空仓库；只有提供至少一条库存
        # 记录时才执行全量覆盖，避免 mixed 请求未携带 inventory 时误清库存。
        if not inventory_list:
            print(f"[WarehouseService] 库存数据为空，保持原有库存状态")
            return

        print(f"[WarehouseService] 全量重置库存状态，共 {len(inventory_list)} 条记录")
        # inventory 是完整快照。先保留同一物理层、同一 SKU 的时间，避免后续 mixed
        # 或周期同步因重建位置对象而将已在库梁错误地当成新到库存。
        previous_inbound_times = self._collect_inventory_inbound_times()
        self._clear_all_inventory_slots()

        # 每条外部库存记录对应一个物理货位或双层货位的一层。先清本记录覆盖的层，
        # 再按 positions 写入，允许调用方只刷新上层或下层而不破坏另一层库存。
        for inv_item in inventory_list:
            aisle_id = int(inv_item.aisleId) if hasattr(inv_item, 'aisleId') else int(inv_item['aisleId'])
            external_row = inv_item.row if hasattr(inv_item, 'row') else inv_item['row']
            column = inv_item.column if hasattr(inv_item, 'column') else inv_item['column']
            level = inv_item.level if hasattr(inv_item, 'level') else inv_item['level']
            shelf = inv_item.shelf if hasattr(inv_item, 'shelf') else inv_item.get('shelf')
            positions_data = inv_item.positions if hasattr(inv_item, 'positions') else inv_item.get('positions', [])

            # 转换外部 row 为内部 row
            internal_row = self._external_to_internal_row(external_row, aisle_id)

            # 查找对应的货位
            position_id = f"{aisle_id:01d}-{internal_row:01d}-{column:02d}-{level:02d}"
            position = self._core.inventory_manager.position_map.get(position_id)
            if position is not None and getattr(position, "disabled", False):
                raise ValueError(f"货位 {position_id} 当前为 disabled，禁止通过 inventory 写入库存")

            if position is None:
                print(f"[WarehouseService] 警告: 货位 {position_id} 不存在，跳过")
                continue

            # 更新货位状态
            if position.is_double_layer:
                shelf_str = str(shelf).upper() if shelf else ""
                if "UPPER" in shelf_str:
                    position.upper_sku = None
                    position.upper_quantity = 0
                    position.upper_attrs = {}
                elif "LOWER" in shelf_str:
                    position.lower_sku = None
                    position.lower_quantity = 0
                    position.lower_attrs = {}
                else:
                    position.upper_sku = None
                    position.upper_quantity = 0
                    position.upper_attrs = {}
                    position.lower_sku = None
                    position.lower_quantity = 0
                    position.lower_attrs = {}
            else:
                position.sku = None
                position.quantity = 0
                position.sku_attrs = {}

            # 一个位置请求可包含多个 SKU 条目。双层未显式给 shelf 时采用“上层优先、
            # 再下层”的兼容写入；正式 API 一般应携带 shelf，避免层位歧义。
            for pos_data in (positions_data or []):
                sku_dict = self._sku_entry_to_dict(pos_data)
                sku_id = sku_dict.get('skuId')
                quantity = sku_dict.get('quantity', 0)
                sku_attrs = self._extract_sku_attrs(sku_dict)

                if not sku_id or quantity <= 0:
                    continue

                # 更新货位
                if position.is_double_layer:
                    shelf_str = str(shelf).upper() if shelf else ""
                    if "UPPER" in shelf_str:
                        target_layer = "UPPER"
                    elif "LOWER" in shelf_str:
                        target_layer = "LOWER"
                    else:
                        # 未指定层，默认放上层
                        if position.upper_quantity == 0:
                            target_layer = "UPPER"
                        else:
                            target_layer = "LOWER"
                    # 外部未传时间时，只能沿用同一“货位 + 层位 + SKU”的历史值；
                    # SKU 或层位改变即视为新库存，使用本次服务时刻作为 FIFO 起点。
                    time_key = (position.get_position_id(), target_layer, str(sku_id))
                    sku_attrs.setdefault(
                        "_inbound_time",
                        self._resolve_inbound_time(None, previous_inbound_times.get(time_key)),
                    )
                    if target_layer == "UPPER":
                        position.upper_sku = sku_id
                        position.upper_quantity = quantity
                        position.upper_attrs = sku_attrs
                    else:
                        position.lower_sku = sku_id
                        position.lower_quantity = quantity
                        position.lower_attrs = sku_attrs
                else:
                    time_key = (position.get_position_id(), "SINGLE", str(sku_id))
                    sku_attrs.setdefault(
                        "_inbound_time",
                        self._resolve_inbound_time(None, previous_inbound_times.get(time_key)),
                    )
                    position.sku = sku_id
                    position.quantity = quantity
                    position.sku_attrs = sku_attrs

                # 更新current_inventory统计（动态添加新SKU）

                # 更新SKU位置索引（动态添加新SKU）

                # 如果是新SKU，添加到sku_types列表

        # 输出同步结果统计
        # 按权威货位状态重建派生索引和汇总计数。
        self._rebuild_inventory_indexes()

        total_beams = sum(
            sum(skus.values())
            for skus in self._core.inventory_manager.current_inventory.values()
        )
        mode = "全量重置"
        print(f"[WarehouseService] 库存同步完成 ({mode})，总梁数: {total_beams}")

    def _rebuild_inventory_indexes(self) -> None:
        """从权威货位状态重建 ``current_inventory`` 和 ``sku_position_index``。

        使增量库存同步支持清空和移动操作，同时保持索引与汇总数量一致。

        Returns:
            None: 不返回业务数据；处理结果通过实例状态、队列或传入对象体现。
        """
        current_inventory: Dict[int, Dict[str, int]] = {}
        sku_position_index: Dict[str, List[InventoryPosition]] = {}
        seen_skus: set[str] = set()

        for position in self._core.inventory_manager.inventory_positions:
            aisle_id = int(position.aisle)
            if aisle_id not in current_inventory:
                current_inventory[aisle_id] = {}

            if getattr(position, "is_double_layer", False):
                for sku_id, qty in (
                    (getattr(position, "upper_sku", None), getattr(position, "upper_quantity", 0)),
                    (getattr(position, "lower_sku", None), getattr(position, "lower_quantity", 0)),
                ):
                    if not sku_id or qty <= 0:
                        continue
                    sku_id_s = str(sku_id)
                    seen_skus.add(sku_id_s)
                    current_inventory[aisle_id][sku_id_s] = current_inventory[aisle_id].get(sku_id_s, 0) + int(qty)
                    sku_position_index.setdefault(sku_id_s, [])
                    if position not in sku_position_index[sku_id_s]:
                        sku_position_index[sku_id_s].append(position)
            else:
                sku_id = getattr(position, "sku", None)
                qty = getattr(position, "quantity", 0)
                if sku_id and qty > 0:
                    sku_id_s = str(sku_id)
                    seen_skus.add(sku_id_s)
                    current_inventory[aisle_id][sku_id_s] = current_inventory[aisle_id].get(sku_id_s, 0) + int(qty)
                    sku_position_index.setdefault(sku_id_s, [])
                    if position not in sku_position_index[sku_id_s]:
                        sku_position_index[sku_id_s].append(position)

        self._core.inventory_manager.current_inventory = current_inventory
        self._core.inventory_manager.sku_position_index = sku_position_index

        # 确保 sku_types 包含本次库存同步动态引入的所有 SKU 标识。
        for sku_id_s in sorted(seen_skus):
            if sku_id_s not in self._core.sku_types:
                self._core.sku_types.append(sku_id_s)
            if sku_id_s not in self._core.inventory_manager.sku_types:
                self._core.inventory_manager.sku_types.append(sku_id_s)

    def _clear_all_inventory_slots(self) -> None:
        """清空所有库存数据，但保留任务状态。

        在 inventory 全量同步时，仅重建库存快照，不影响：
        - running_tasks
        - pending_* 队列
        - completed_tasks
        - 待执行任务缓存
        - 巷道当前位置

        Returns:
            None: 不返回业务数据；处理结果通过实例状态、队列或传入对象体现。
        """
        print(f"[WarehouseService] 清空所有库存槽位状态...")

        # 1. 清空所有货位的库存
        for position in self._core.inventory_manager.inventory_positions:
            if position.is_double_layer:
                position.upper_sku = None
                position.upper_quantity = 0
                position.upper_attrs = {}
                position.lower_sku = None
                position.lower_quantity = 0
                position.lower_attrs = {}
            else:
                position.sku = None
                position.quantity = 0
                position.sku_attrs = {}

        # 2. 清空统计数据
        for aisle in self._core.inventory_manager.current_inventory:
            for sku in self._core.inventory_manager.current_inventory[aisle]:
                self._core.inventory_manager.current_inventory[aisle][sku] = 0
        for sku in self._core.inventory_manager.sku_position_index:
            self._core.inventory_manager.sku_position_index[sku].clear()

        print(f"[WarehouseService] 库存槽位状态清空完成")

    def _clear_undispatched_pending_tasks(self) -> None:
        """Clear only undispatched pending queues.

        Returns:
            None: 不返回业务数据；处理结果通过实例状态、队列或传入对象体现。
        """
        self._core.pending_outbound_queue.clear()
        for aisle in list(self._core.pending_inbound_by_aisle.keys()):
            self._core.pending_inbound_by_aisle[aisle].clear()

    def _clear_assigned_and_running_state(self, task_manager: Optional[TaskStateManager] = None) -> None:
        """Clear pending, pending_execution, and running state for resetAssigned=true.

        Args:
            task_manager (Optional[TaskStateManager]): 当前函数使用的 `task_manager` 参数。

        Returns:
            None: 不返回业务数据；处理结果通过实例状态、队列或传入对象体现。
        """
        self._clear_undispatched_pending_tasks()
        self._pending_execution_tasks.clear()
        self._core.running_tasks.clear()
        for aisle in self._core.aisles:
            self._core.current_position_by_aisle[aisle] = None
        self._virtual_outbound_current_group.clear()
        if task_manager is not None:
            task_manager.clear_all()

    def _clear_all_inventory(self) -> None:
        """清空所有库存数据和相关统计，包括任务状态。

        Returns:
            None: 不返回业务数据；处理结果通过实例状态、队列或传入对象体现。
        """
        print(f"[WarehouseService] 清空所有库存和任务状态...")

        # 1. 清空所有货位的库存
        for position in self._core.inventory_manager.inventory_positions:
            if position.is_double_layer:
                position.upper_sku = None
                position.upper_quantity = 0
                position.lower_sku = None
                position.lower_quantity = 0
            else:
                position.sku = None
                position.quantity = 0

        # 2. 清空统计数据
        for aisle in self._core.inventory_manager.current_inventory:
            for sku in self._core.inventory_manager.current_inventory[aisle]:
                self._core.inventory_manager.current_inventory[aisle][sku] = 0
        for sku in self._core.inventory_manager.sku_position_index:
            self._core.inventory_manager.sku_position_index[sku].clear()

        self._core.running_tasks.clear()
        self._core.completed_tasks.clear()
        self._core.pending_outbound_queue.clear()
        for aisle in list(self._core.pending_inbound_by_aisle.keys()):
            self._core.pending_inbound_by_aisle[aisle].clear()

        # 4. 清空待执行任务缓存
        self._pending_execution_tasks.clear()

        # 5. 重置巷道当前位置
        for aisle in self._core.aisles:
            self._core.current_position_by_aisle[aisle] = None
        self._virtual_outbound_current_group.clear()

        print(f"[WarehouseService] 库存和任务状态清空完成")

    def is_aisle_available(self, aisle_id: int) -> bool:
        """检查巷道是否可用（基于外部同步的状态）

        Args:
            aisle_id (int): 目标巷道编号。

        Returns:
            bool: 处理成功、条件成立或校验通过时为 ``True``，否则为 ``False``。
        """
        status = self._aisle_availability.get(aisle_id, {})
        return status.get('is_available', True)

    # ============================================================
    # 任务转换方法
    # ============================================================

    # ========================================================================
    # 主流程：外部任务转换、执行反馈与任务调整
    # ========================================================================
    def convert_schedule_tasks(self, tasks: List[Any]) -> Tuple[List[TaskData], List[TaskData]]:
        """
        将API请求中的任务列表转换为warehouse_core的TaskData格式

        Args:
            tasks: API请求中的任务列表

        Returns:
            (inbound_tasks, outbound_tasks): 入库任务列表和出库任务列表
        """
        inbound_tasks = []
        outbound_tasks = []

        for task in tasks:
            # 与 SKU 校验保持同一读取规则：请求模型走属性访问，原始 JSON 走键访问。
            task_id = self._get_value(task, "taskId")
            task_type = self._get_value(task, "taskType")
            skus = self._get_value(task, "skus", []) or []

            # 转换SKU格式
            sku_list = []
            for sku in skus:
                sku_dict = self._sku_entry_to_dict(sku)
                sku_id = sku_dict.get('skuId') or sku_dict.get('sku')
                quantity = sku_dict.get('quantity', 1)
                sku_dict['skuId'] = sku_id
                sku_dict['quantity'] = quantity
                sku_list.append(sku_dict)

            if "INBOUND" in str(task_type).upper():
                # 入库任务
                target_aisle = self._get_value(task, "targetAisle")
                in_line = self._get_value(task, "inLine", 1)
                inbound_urgent = self._get_value(task, "inboundUrgent", False)

                task_data = TaskData(
                    task_id=task_id,
                    task_type=TASK_TYPE_INBOUND,
                    task_name=task_id,
                    skus=sku_list,
                    in_line=int(in_line) if in_line else 1,
                    assigned_aisle=int(target_aisle) if target_aisle else None,
                )
                provided_positions = self._get_value(task, "positions")
                if provided_positions and task_data.assigned_aisle is not None:
                    positions_payload: List[Dict[str, Any]] = []
                    for p in provided_positions:
                        if isinstance(p, dict):
                            positions_payload.append(dict(p))
                        elif hasattr(p, "model_dump"):
                            positions_payload.append(p.model_dump())
                        elif hasattr(p, "dict"):
                            positions_payload.append(p.dict())
                    resolved_positions = self._resolve_feedback_positions(task_data.assigned_aisle, positions_payload)
                    if resolved_positions is None:
                        raise ValueError(f"任务 {task_id} 提供的 positions 无效")
                    task_data.positions = resolved_positions
                inbound_tasks.append(task_data)

            # OUTBOUND
            else:
                # 出库任务
                plan_id = self._get_value(task, "planId")
                plan_index = self._get_value(task, "planIndex")

                # 确定产线
                production_line = None
                if plan_id:
                    mapped_line = self._plan_id_to_line.get(str(plan_id))
                    if mapped_line is not None:
                        production_line = mapped_line
                    # 尝试从plan_id中提取产线信息
                    # 支持格式: "PLAN-LINE1", "LINE-1", "1"
                    try:
                        plan_str = str(plan_id).upper()
                        if production_line is None and "LINE" in plan_str:
                            # 提取LINE后面的数字
                            import re
                            match = re.search(r'LINE[-]?(\d+)', plan_str)
                            if match:
                                production_line = int(match.group(1))
                        elif production_line is None and str(plan_id).isdigit():
                            production_line = int(plan_id)
                    except:
                        pass

                # 如果无法从plan_id获取产线，从SKU推断
                if production_line is None and sku_list:
                    first_sku = sku_list[0].get('skuId', '')
                    pl_value = self._core.sku_to_production_line.get(first_sku, 1)
                    # sku_to_production_line可能返回列表，取第一个值
                    if isinstance(pl_value, list):
                        production_line = int(pl_value[0]) if pl_value else 1
                    else:
                        production_line = int(pl_value) if pl_value else 1

                effective_plan_index = plan_index
                if plan_id and plan_index is not None:
                    plan_key = str(plan_id)
                    public_plan_index = int(plan_index)
                    if self._current_plan_operation == "ADD":
                        base_task_group_count = self._line_id_to_base_task_group_count.get(
                            int(production_line or 1), 0
                        )
                        if public_plan_index >= 1:
                            effective_plan_index = public_plan_index + base_task_group_count
                    else:
                        offset = self._plan_id_to_group_offset.get(plan_key)
                        group_count = self._plan_id_to_group_count.get(plan_key)
                        if offset is not None and group_count is not None and 1 <= public_plan_index <= group_count:
                            effective_plan_index = public_plan_index + offset

                task_data = TaskData(
                    task_id=task_id,
                    task_type=TASK_TYPE_OUTBOUND,
                    task_name=task_id,
                    skus=sku_list,
                    production_line=production_line or 1,
                )
                self._assign_outbound_group_metadata(
                    task_data,
                    plan_id=plan_id,
                    plan_index=plan_index,
                    effective_plan_index=effective_plan_index,
                )

                outbound_tasks.append(task_data)

        return inbound_tasks, outbound_tasks

    # ============================================================
    # 核心业务方法
    # ============================================================



    def _find_positions_for_outbound_task(self, task: TaskData) -> Optional[List[InventoryPosition]]:
        """
        为出库任务查找库存中对应SKU的货位

        优先级：
        1. 查找已配对的双层货位（两个SKU在同一位置）
        2. 查找分别包含两个SKU的货位（需要移库配对）
        3. 查找只有一个SKU的货位（部分满足）

        Args:
            task: 出库任务

        Returns:
            找到的货位列表，如果未找到返回None
        """
        sku_ids = task.get_sku_ids() if hasattr(task, 'get_sku_ids') else []
        if not sku_ids:
            print(f"[WarehouseService] 任务 {task.task_id} 没有SKU信息")
            return None
        production_line = task.production_line or 1
        print(f"[WarehouseService] 为任务 {task.task_id} 查找货位, SKUs: {sku_ids}, 产线: {production_line}")
        candidates = self._get_outbound_candidate_positions(task)
        if not candidates:
            print(f"[WarehouseService] 任务 {task.task_id} 未找到任何可用货位")
            return None

        selected = list(candidates[0])
        print(
            "[WarehouseService] 选择位置: "
            + ", ".join(position.get_position_id() for position in selected)
        )
        return selected

    def allocate_inbound_aisle(self, task_id: str, skus: List[Dict]) -> int:
        """
        为入库任务分配巷道

        调用的warehouse_core方法：
        - allocate_inbound_aisle(): 分配巷道

        Args:
            task_id: 任务ID
            skus: SKU列表

        Returns:
            推荐的巷道ID
        """
        self._sync_time()

        # 创建临时任务对象用于分配
        task_stub = type('TaskStub', (), {
            'skus': skus,
            'in_line': 1,
            'assigned_aisle': None
        })()

        # 调用core的分配方法
        if self._core.inbound_aisle_allocator:
            aisle = self._core.inbound_aisle_allocator.allocate(
                {'skus': skus},
                self._core.inventory_manager.inventory_positions
            )
            if aisle:
                return aisle

        # 默认分配策略
        return random.choice(self._core.aisles)

    def apply_feedback(self, feedback: Dict[str, Any]) -> bool:
        """应用外部 EXECUTING/COMPLETED/FAILED 反馈并驱动状态转换。

        处理逻辑：
        - EXECUTING状态：将任务从待执行队列移入running_tasks，开始执行
        - COMPLETED状态：从running_tasks移除，更新库存和生产计划进度
        - FAILED状态：从running_tasks移除，标记失败

        更新的参数：
        - running_tasks: 正在执行的任务
        - pending_inbound_by_aisle / pending_outbound_queue: 等待队列
        - completed_tasks: 已完成的任务列表
        - inventory_manager: 库存状态（出库完成时扣减）
        - production_line_current_group: 产线进度（出库完成时更新）
        - blockage_status: 拥堵状态（出库完成时更新）

        Args:
            feedback: 反馈信息字典，包含taskId, status, taskType等

        Returns:
            是否成功应用反馈

        EXECUTING 只把已冻结任务转入 ``running_tasks``；COMPLETED 才通过 Core
        事件更新真实库存，FAILED 则移除其待执行/运行状态并释放后续资源判断可
        复用的任务承诺。位置不会在本方法中重新分配。

        局部变量：``task_id`` 是反馈关联键；``status`` 是标准化后的生命周期状态；
        ``task_type`` 用于选择入库/出库反馈路径；``task`` 是从 pending 或 running
        解析出的权威任务实例；``positions`` 是反馈携带或已冻结的位置；``result``
        表示本次状态迁移是否成功，错误原因写入 ``_last_feedback_error``。
        """
        self._last_feedback_error = None
        self._last_feedback_notice = None
        self._sync_time()

        try:
            task_id = feedback.get('taskId')
            status = feedback.get('status', '').upper()
            task_type = feedback.get('taskType', '').upper()

            if not task_id:
                self._last_feedback_error = "缺少 taskId"
                print(f"[WarehouseService] 反馈缺少taskId")
                return False

            print(f"[WarehouseService] 处理任务反馈: task_id={task_id}, status={status}, type={task_type}")

            # 调用core的反馈记录方法（用于记录）
            self._core.apply_task_feedback(feedback)

            if status == 'EXECUTING':
                return self._start_task_execution(task_id, task_type, feedback)

            elif status == 'COMPLETED':
                # COMPLETED状态：完成任务
                return self._complete_task_execution(task_id, task_type)

            elif status == 'FAILED':
                # FAILED状态：任务失败
                return self._fail_task_execution(task_id, feedback.get('reason', 'Unknown'))

            return True

        except Exception as e:
            import traceback
            self._last_feedback_error = str(e)
            print(f"[WarehouseService] 应用反馈失败: {e}")
            traceback.print_exc()
            return False

    def _resolve_feedback_positions(self, aisle: int, positions_data: List[Dict[str, Any]]) -> Optional[List[InventoryPosition]]:
        """将反馈中的外部坐标转换为核心位置对象。

        本方法只完成外部 row 到内部排号的转换和 position_map 存在性
        检查，不修改库存，也不判断 SKU、层位或资源冲突；业务校验在
        后续反馈/调整流程中完成。

        Args:
            aisle (int): 目标巷道编号。
            positions_data (List[Dict[str, Any]]): 外部接口传入的位置描述列表，使用 API 的排号和 shelf 口径。

        Returns:
            Optional[List[InventoryPosition]]: 计算得到的结果；当前条件不成立或不存在可用对象时返回 ``None``。
        """
        if not positions_data:
            return []
        inventory_manager = getattr(self._core, "inventory_manager", None)
        position_map = getattr(inventory_manager, "position_map", None) if inventory_manager is not None else None
        if not position_map:
            return None

        resolved_positions: List[InventoryPosition] = []
        for pos_data in positions_data:
            external_row = self._get_value(pos_data, "row")
            column = self._get_value(pos_data, "column")
            level = self._get_value(pos_data, "level")
            if external_row is None or column is None or level is None:
                return None
            try:
                internal_row = self._api_row_to_internal_row(int(external_row), int(aisle))
                position_id = f"{int(aisle):01d}-{int(internal_row):01d}-{int(column):02d}-{int(level):02d}"
            except (TypeError, ValueError):
                return None
            actual_position = position_map.get(position_id)
            if actual_position is None:
                return None
            resolved_positions.append(actual_position)
        return resolved_positions

    def _allocate_feedback_positions_for_aisle(self, task: TaskData, aisle: int) -> Optional[List[InventoryPosition]]:
        """临时为尚未固定位置的入库任务寻找反馈位置。

        调用前屏蔽同巷道 pending/running 入库任务已固定的位置，调用后
        恢复 reserved 原值；因此该方法是候选查询，不覆盖既有冻结位置。

        Args:
            task (TaskData): 当前处理的 TaskData 任务对象。
            aisle (int): 目标巷道编号。

        Returns:
            Optional[List[InventoryPosition]]: 计算得到的结果；当前条件不成立或不存在可用对象时返回 ``None``。
        """
        allocator = getattr(self._core, "inbound_position_allocator", None)
        inventory_manager = getattr(self._core, "inventory_manager", None)
        inventory_positions = getattr(inventory_manager, "inventory_positions", None) if inventory_manager is not None else None
        if allocator is None or inventory_positions is None:
            return None

        original_aisle = getattr(task, "assigned_aisle", None)
        task.assigned_aisle = aisle

        touched_reserved: Dict[int, Tuple[InventoryPosition, bool]] = {}
        def _mark_reserved(pos: InventoryPosition) -> None:
            """执行 `_mark_reserved` 对应的模块处理步骤，并返回该步骤产生的结果。

            输入：pos（InventoryPosition）
            输出：None

            Args:
                pos (InventoryPosition): 当前处理的 InventoryPosition 货位对象。

            Returns:
                None: 不返回业务数据；处理结果写入实例状态、传入对象或外部响应。
            """
            key = id(pos)
            if key not in touched_reserved:
                touched_reserved[key] = (pos, bool(getattr(pos, "reserved", False)))
            pos.reserved = True

        try:
            # 保留该巷道 pending/running 任务已经冻结的位置，避免为当前任务重复分配。
            for pending_task in list(self._core.pending_inbound_by_aisle.get(aisle, [])):
                if getattr(pending_task, "task_id", None) == getattr(task, "task_id", None):
                    continue
                for pos in (getattr(pending_task, "positions", None) or []):
                    _mark_reserved(pos)
            for running_task in self._core.running_tasks.values():
                if getattr(running_task, "task_type", None) != TASK_TYPE_INBOUND:
                    continue
                if getattr(running_task, "assigned_aisle", None) != aisle:
                    continue
                for pos in (getattr(running_task, "positions", None) or []):
                    _mark_reserved(pos)

            current_positions = getattr(self._core, "current_position_by_aisle", {}) or {}
            current_position = current_positions.get(aisle)
            allocated = allocator.allocate(
                inventory_positions,
                task,
                current_position=current_position,
            )
        finally:
            for pos, old_reserved in touched_reserved.values():
                pos.reserved = old_reserved
            task.assigned_aisle = original_aisle

        return list(allocated) if allocated else None

    def _validate_outbound_inventory_for_execution(self, task: TaskData) -> Tuple[bool, Optional[str]]:
        """在 EXECUTING 前校验冻结位置上的真实库存。

        API 预占只防止服务层重复承诺，不提前扣减真实库存；启动时仍需
        按冻结的坐标、层位、SKU 和属性回查真实位置，失败时返回原因。

        Args:
            task (TaskData): 当前处理的 TaskData 任务对象。

        Returns:
            Tuple[bool, Optional[str]]: 处理成功、条件成立或校验通过时为 ``True``，否则为 ``False``。
        """
        if getattr(task, "task_type", None) != TASK_TYPE_OUTBOUND:
            return True, None

        position_payloads = self.get_task_api_positions(task)
        if not position_payloads:
            return False, f"任务 {getattr(task, 'task_id', '')} 没有分配 positions"

        inventory_manager = getattr(self._core, "inventory_manager", None)
        position_map = getattr(inventory_manager, "position_map", None) if inventory_manager is not None else None
        if not position_map:
            return False, "库存位置索引不可用"

        match_fields = list(getattr(self._core, "match_fields", []) or [])
        for pos_data in position_payloads:
            try:
                aisle = int(self._get_value(pos_data, "aisle") or getattr(task, "assigned_aisle", None) or 0)
                external_row = int(self._get_value(pos_data, "row"))
                column = int(self._get_value(pos_data, "column"))
                level = int(self._get_value(pos_data, "level"))
                shelf = str(self._get_value(pos_data, "shelf") or "").strip().lower()
                sku_id = str(self._get_value(pos_data, "skuId") or "")
            except (TypeError, ValueError):
                return False, f"任务 {getattr(task, 'task_id', '')} 的固化 positions 无效"

            if aisle <= 0 or not sku_id or shelf not in {"upper", "lower"}:
                return False, f"任务 {getattr(task, 'task_id', '')} 的固化 positions 无效"

            try:
                internal_row = self._api_row_to_internal_row(external_row, aisle)
            except (TypeError, ValueError):
                return False, f"任务 {getattr(task, 'task_id', '')} 的固化 positions 无效"

            position_id = f"{aisle:01d}-{internal_row:01d}-{column:02d}-{level:02d}"
            actual_position = position_map.get(position_id)
            if actual_position is None:
                return False, f"任务 {getattr(task, 'task_id', '')} 的位置 {aisle}-{external_row}-{column:02d}-{level:02d} 不存在"

            attrs = {field: self._get_value(pos_data, field) for field in match_fields}
            if not actual_position.matches_sku(sku_id, attrs, match_fields, shelf=shelf):
                return False, (
                    f"任务 {getattr(task, 'task_id', '')} 的位置 "
                    f"{aisle}-{external_row}-{column:02d}-{level:02d}-{shelf.upper()} "
                    f"当前已无可执行库存"
                )

        return True, None

    def _start_task_execution(self, task_id: str, task_type: str, feedback: Optional[Dict[str, Any]] = None) -> bool:
        """将已提交任务切换为运行中状态。

        由反馈接口 ``EXECUTING`` 分支调用。任务优先从 API 可执行缓存查找，再
        回退到核心 pending 队列；随后校验可选的巷道/位置覆盖、巷道独占和冻结
        位置。此阶段不扣减真实库存，真实写入或扣减只在 ``COMPLETED`` 发生。

        Args:
            task_id (str): 任务唯一标识，用于定位队列、冻结位置或反馈对象。
            task_type (str): 任务类型，例如 INBOUND 或 OUTBOUND。
            feedback (Optional[Dict[str, Any]]): 外部执行反馈数据，包含任务状态、位置或异常信息。

        Returns:
            bool: 处理成功、条件成立或校验通过时为 ``True``，否则为 ``False``。
        """
        # 查找顺序固定为 API 可执行缓存 -> Core pending 队列 -> running 去重。这样
        # 已被推荐但尚未启动的任务优先保持自身冻结位置，重复 EXECUTING 不重复占巷道。
        task = self._pending_execution_tasks.get(task_id)

        if task is None:
            # 2. 尝试从pending队列查找
            if task_type == 'OUTBOUND':
                for t in self._core.pending_outbound_queue:
                    if t.task_id == task_id:
                        task = t
                        break
            # INBOUND
            else:
                for aisle, queue in self._core.pending_inbound_by_aisle.items():
                    for t in queue:
                        if t.task_id == task_id:
                            task = t
                            break
                    if task is not None:
                        break

        if task is None:
            # 3. 检查是否已在running_tasks中（重复反馈）
            if task_id in self._core.running_tasks:
                self._last_feedback_notice = f"任务 {task_id} 已在执行中，重复EXECUTING反馈已忽略"
                print(f"[WarehouseService] 任务 {task_id} 已在执行中（重复EXECUTING反馈）")
                return True
            self._last_feedback_error = f"未找到任务 {task_id}"
            print(f"[WarehouseService] 未找到任务 {task_id}，无法开始执行")
            return False

        # 保存原始冻结结果。任何执行期覆盖校验失败都应恢复该结果，而不是留下
        # 半更新的巷道或位置。
        override_aisle = None
        original_assigned_aisle = getattr(task, 'assigned_aisle', None)
        original_positions = list(getattr(task, "positions", None) or [])
        original_api_positions = deepcopy(getattr(task, "_api_positions", None))
        override_positions = None
        if feedback:
            # 外部可覆盖巷道或位置，但覆盖只在完整校验通过后才写回任务。原始值先
            # 保存，后续任一失败分支都能保持原冻结结果而不是留下半修改状态。
            override_aisle_value = feedback.get('aisleId')
            if override_aisle_value is not None:
                try:
                    override_aisle = int(override_aisle_value)
                except (TypeError, ValueError):
                    self._last_feedback_error = f"任务 {task_id} 提供的 aisleId 无效"
                    print(f"[WarehouseService] 任务 {task_id} 提供的 aisleId 无效，无法开始执行")
                    return False

            positions_data = feedback.get('positions') or []
            if positions_data:
                effective_aisle = override_aisle if override_aisle is not None else getattr(task, 'assigned_aisle', None)
                if effective_aisle is None:
                    self._last_feedback_error = f"任务 {task_id} 提供了 positions，但缺少可用巷道"
                    print(f"[WarehouseService] 任务 {task_id} 提供了 positions，但缺少可用巷道，无法开始执行")
                    return False
                override_positions = self._resolve_feedback_positions(effective_aisle, positions_data)
                if override_positions is None:
                    self._last_feedback_error = f"任务 {task_id} 提供的 positions 无效"
                    print(f"[WarehouseService] 任务 {task_id} 提供的 positions 无效，无法开始执行")
                    return False

        effective_aisle = override_aisle if override_aisle is not None else self._get_task_effective_aisle(task)
        occupying_task = self._find_running_task_on_aisle(effective_aisle, exclude_task_id=task_id)
        if occupying_task is not None:
            occupying_task_id = getattr(occupying_task, "task_id", None) or ""
            self._last_feedback_error = f"任务 {occupying_task_id} 当前正在执行"
            print(
                f"[WarehouseService] 任务 {task_id} 对应巷道 {effective_aisle} 已被任务 "
                f"{occupying_task_id} 占用，保持待处理状态"
            )
            return False

        # 逻辑可执行性不只检查库存，还包含入库口队首、出库当前组和资源预占语义。
        if not self._is_task_currently_executable(task):
            self._last_feedback_error = f"任务 {task_id} 当前不可执行"
            print(f"[WarehouseService] 任务 {task_id} 当前不可执行，无法开始执行")
            return False

        # 覆盖位置优先级高于覆盖巷道；只覆盖巷道时必须在新巷道重新找位置并冻结。
        # 这使执行期位置始终可由 API 查询和完成反馈共同复用。
        if override_aisle is not None:
            task.assigned_aisle = override_aisle
        elif getattr(task, "assigned_aisle", None) is None and effective_aisle is not None:
            task.assigned_aisle = effective_aisle
        if override_positions:
            task.positions = override_positions
            self.freeze_task_api_positions(task)
        elif override_aisle is not None and override_aisle != original_assigned_aisle:
            reallocated_positions = self._allocate_feedback_positions_for_aisle(task, override_aisle)
            if not reallocated_positions:
                self._last_feedback_error = f"巷道 {override_aisle} 当前无法为任务 {task_id} 重新分配位置"
                print(f"[WarehouseService] 巷道 {override_aisle} 当前无法为任务 {task_id} 重新分配位置，保持待处理状态")
                task.assigned_aisle = original_assigned_aisle
                return False
            task.positions = reallocated_positions
            self.freeze_task_api_positions(task)
        elif not getattr(task, "_api_positions", None):
            self.freeze_task_api_positions(task)

        if not getattr(task, 'positions', None):
            self._last_feedback_error = f"任务 {task_id} 没有分配 positions"
            print(f"[WarehouseService] 任务 {task_id} 没有分配 positions，无法开始执行")
            return False

        # 出库在真正进入 running 前再以冻结位置校验一次真实库存，防止外部库存
        # 同步已改变时仍按过期推荐执行。
        ok, reason = self._validate_outbound_inventory_for_execution(task)
        if not ok:
            task.assigned_aisle = original_assigned_aisle
            task.positions = original_positions
            task._api_positions = deepcopy(original_api_positions) or []
            self._last_feedback_error = reason or f"任务 {task_id} 当前不可执行"
            print(f"[WarehouseService] {self._last_feedback_error}，无法开始执行")
            return False

        # 确保任务有task_record
        if not getattr(task, 'task_record', None):
            task.task_record = self._core.generate_task_record(task, self._core.current_time)

        # 状态迁移的唯一写入点：从 API 展示缓存和核心 pending 队列转入 running。
        self._pending_execution_tasks.pop(task_id, None)
        # 添加到running_tasks
        self._core.running_tasks[task_id] = task
        print(f"[WarehouseService] 任务 {task_id} 已移入running_tasks开始执行")

        # 从等待队列移除
        if task.task_type == TASK_TYPE_OUTBOUND:
            self._core.pending_outbound_queue = [
                t for t in self._core.pending_outbound_queue if t.task_id != task_id
            ]
        # INBOUND
        else:
            for aisle, queue in list(self._core.pending_inbound_by_aisle.items()):
                self._core.pending_inbound_by_aisle[aisle] = [
                    t for t in queue if t.task_id != task_id
                ]

        return True

    def _is_task_logically_executable(self, task: TaskData) -> bool:
        """逻辑可执行性检查。

        Args:
            task (TaskData): 当前处理的 TaskData 任务对象。

        Returns:
            bool: 处理成功、条件成立或校验通过时为 ``True``，否则为 ``False``。
        """
        if task.task_type == TASK_TYPE_OUTBOUND:
            pending_task = None
            for t in self._core.pending_outbound_queue:
                if t.task_id == task.task_id:
                    pending_task = t
                    break
            if pending_task is None:
                return False

            production_line = getattr(task, "production_line", None)
            if bool(getattr(task, "uses_virtual_group", False)):
                return self._get_outbound_group_state(task) == "current"
            if production_line is not None:
                if not self._core.can_start_outbound_task(
                    task.task_id,
                    production_line,
                    getattr(task, "group_idx", None),
                ):
                    return False
            return True

        aisle = getattr(task, "assigned_aisle", None)
        if aisle is None:
            return False
        queue = list(self._core.pending_inbound_by_aisle.get(aisle, []))
        if not queue:
            return False

        line_heads: Dict[int, str] = {}
        for t in queue:
            line = int(getattr(t, "in_line", 1) or 1)
            if line not in line_heads:
                line_heads[line] = t.task_id

        task_line = int(getattr(task, "in_line", 1) or 1)
        return line_heads.get(task_line) == task.task_id

    def _get_outbound_group_state(self, task: TaskData) -> str:
        """Classify outbound task relative to the current production-line group.

        Args:
            task (TaskData): 当前处理的 TaskData 任务对象。

        Returns:
            str: ``past`` 表示组已完成，``current`` 表示当前可推进组，``future``
            表示后续组，``unknown`` 表示任务缺少可判断的产线或组号。
        """
        if getattr(task, "task_type", None) != TASK_TYPE_OUTBOUND:
            return "unknown"

        production_line = getattr(task, "production_line", None)
        group_idx = getattr(task, "group_idx", None)
        if production_line is None or group_idx is None:
            return "unknown"

        try:
            normalized_line = int(production_line)
            normalized_group = int(group_idx)
        except (TypeError, ValueError):
            return "unknown"

        if bool(getattr(task, "uses_virtual_group", False)):
            current_group_idx = self._get_virtual_current_group(normalized_line)
        else:
            current_group_idx = self._core.production_line_current_group.get(normalized_line, 0)
        if normalized_group < current_group_idx:
            return "past"
        if normalized_group == current_group_idx:
            return "current"
        return "future"

    def _is_task_currently_executable(self, task: TaskData) -> bool:
        """检查任务是否可以从当前 pending 队列直接启动。

        该检查在逻辑条件之外再叠加巷道运行冲突；不会重新分配出库位置，
        真正的 pending 到 running 切换仍由 EXECUTING 反馈完成。

        Args:
            task (TaskData): 当前处理的 TaskData 任务对象。

        Returns:
            bool: 处理成功、条件成立或校验通过时为 ``True``，否则为 ``False``。
        """
        if not self._is_task_logically_executable(task):
            return False
        task_aisle = self._get_task_effective_aisle(task)
        return self._find_running_task_on_aisle(task_aisle, exclude_task_id=getattr(task, "task_id", None)) is None

    def _get_task_effective_aisle(self, task: Optional[TaskData]) -> Optional[int]:
        """解析任务当前实际使用的巷道，优先使用出库冻结位置。

        出库任务的巷道可能只存在于 positions，入库任务通常从
        assigned_aisle 读取；统一解析结果供 running 冲突和 API 分桶使用。

        Args:
            task (Optional[TaskData]): 当前处理的 TaskData 任务对象。

        Returns:
            Optional[int]: 计算得到的结果；当前条件不成立或不存在可用对象时返回 ``None``。
        """
        if task is None:
            return None
        if getattr(task, "task_type", None) == TASK_TYPE_OUTBOUND:
            positions = getattr(task, "positions", None) or []
            if positions:
                try:
                    return int(getattr(positions[0], "aisle", 0) or 0)
                except (TypeError, ValueError):
                    return None
        aisle = getattr(task, "assigned_aisle", None)
        if aisle is not None:
            try:
                return int(aisle)
            except (TypeError, ValueError):
                return None
        positions = getattr(task, "positions", None) or []
        if positions:
            try:
                return int(getattr(positions[0], "aisle", 0) or 0)
            except (TypeError, ValueError):
                return None
        return None

    def _find_running_task_on_aisle(
        self,
        aisle: Optional[int],
        exclude_task_id: Optional[str] = None,
    ) -> Optional[TaskData]:
        """在候选集合中按既定约束查找匹配对象或可用位置。

        输出：Optional[TaskData]

        Args:
            aisle (Optional[int]): 目标巷道编号。
            exclude_task_id (Optional[str]): 当前对象的唯一标识。

        Returns:
            Optional[TaskData]: 计算得到的结果；当前条件不成立或不存在可用对象时返回 ``None``。
        """
        if aisle is None:
            return None
        normalized_aisle = int(aisle)
        for running_task in self._core.running_tasks.values():
            running_task_id = getattr(running_task, "task_id", None)
            if exclude_task_id is not None and running_task_id == exclude_task_id:
                continue
            if self._get_task_effective_aisle(running_task) == normalized_aisle:
                return running_task
        return None

    def _resolve_pending_task_instance(self, task: Optional[TaskData]) -> Optional[TaskData]:
        """Resolve scheduler-returned task copies back to the canonical pending-queue instance.

        Args:
            task (Optional[TaskData]): 当前处理的 TaskData 任务对象。

        Returns:
            Optional[TaskData]: 计算得到的结果；当前条件不成立或不存在可用对象时返回 ``None``。
        """
        if task is None:
            return None

        task_id = getattr(task, "task_id", None)
        task_type = getattr(task, "task_type", None)
        if not task_id or not task_type:
            return task

        if task_type == TASK_TYPE_OUTBOUND:
            for pending_task in list(self._core.pending_outbound_queue):
                if getattr(pending_task, "task_id", None) == task_id:
                    return pending_task
            return task

        assigned_aisle = getattr(task, "assigned_aisle", None)
        if assigned_aisle is not None:
            for pending_task in list(self._core.pending_inbound_by_aisle.get(int(assigned_aisle), [])):
                if getattr(pending_task, "task_id", None) == task_id:
                    return pending_task

        for queue in self._core.pending_inbound_by_aisle.values():
            for pending_task in list(queue):
                if getattr(pending_task, "task_id", None) == task_id:
                    return pending_task
        return task

    # ========================================================================
    # 查询输出：可执行任务、运行状态与库存视图
    # ========================================================================
    def get_tasks_by_aisle_for_api(self) -> Dict[int, Dict[str, List[TaskData]]]:
        """返回按巷道分组的 ``can_executing/pending/running`` 任务。

        ``can_executing`` 表示当前组、库存/配对和巷道资源条件均已成立；
        ``pending`` 表示任务仍在系统内但暂不能启动。该查询只读核心队列，
        不会改变任务状态或真实库存。

        Returns:
            Dict[int, Dict[str, List[TaskData]]]: 符合当前处理条件的结果列表或集合。
        """
        result: Dict[int, Dict[str, List[TaskData]]] = {
            int(aisle): {"can_executing": [], "pending": [], "running": []}
            for aisle in self._core.aisles
        }

        for task in self._core.running_tasks.values():
            aisle = self._get_task_effective_aisle(task)
            if aisle is None:
                continue
            normalized_aisle = int(aisle)
            result.setdefault(normalized_aisle, {"can_executing": [], "pending": [], "running": []})
            result[normalized_aisle]["running"].append(task)
        # 构造``can_executing/pending/running``任务列表
        for aisle in self._core.aisles:
            normalized_aisle = int(aisle)
            can_executing_list: List[TaskData] = []
            pending_list: List[TaskData] = []

            for task in list(self._core.pending_inbound_by_aisle.get(aisle, [])):
                if self._is_task_logically_executable(task):
                    can_executing_list.append(task)
                else:
                    pending_list.append(task)

            for task in list(self._core.pending_outbound_queue):
                positions = getattr(task, "positions", None) or []
                if not positions:
                    continue
                try:
                    task_aisle = int(getattr(positions[0], "aisle", 0) or 0)
                except Exception:
                    task_aisle = 0
                if task_aisle != normalized_aisle:
                    continue
                outbound_group_state = self._get_outbound_group_state(task)
                if outbound_group_state == "past":
                    continue
                if outbound_group_state == "current" and self._is_task_logically_executable(task):
                    can_executing_list.append(task)
                else:
                    pending_list.append(task)

            result.setdefault(normalized_aisle, {"can_executing": [], "pending": [], "running": []})
            result[normalized_aisle]["can_executing"] = can_executing_list
            result[normalized_aisle]["pending"] = pending_list

        return result

    def get_executable_tasks_by_aisle(self) -> Dict[int, List[TaskData]]:
        """返回当前各巷道可提交 EXECUTING 的任务列表。

        扫描时会临时标记位置以防止同一次查询重复选择，退出前恢复原
        reserved 状态；API 出库虚拟预占仍只保存在服务层集合中。

        Returns:
            Dict[int, List[TaskData]]: 符合当前处理条件的结果列表或集合。
        """
        executable_by_aisle: Dict[int, List[TaskData]] = {int(aisle): [] for aisle in self._core.aisles}
        seen_by_aisle: Dict[int, set] = {int(aisle): set() for aisle in self._core.aisles}
        touched_reserved: Dict[int, Tuple[InventoryPosition, bool]] = {}

        def _append(aisle: int, task: TaskData) -> None:
            """执行 `_append` 对应的模块处理步骤，并返回该步骤产生的结果。

            输入：aisle（int）、task（TaskData）
            输出：None

            Args:
                aisle (int): 目标巷道编号。
                task (TaskData): 当前处理的任务记录。

            Returns:
                None: 不返回业务数据；处理结果写入实例状态、传入对象或外部响应。
            """
            if aisle not in executable_by_aisle:
                return
            task_id = getattr(task, "task_id", None)
            if not task_id or task_id in seen_by_aisle[aisle]:
                return
            executable_by_aisle[aisle].append(task)
            seen_by_aisle[aisle].add(task_id)

        def _mark_reserved(pos: InventoryPosition) -> None:
            """执行 `_mark_reserved` 对应的模块处理步骤，并返回该步骤产生的结果。

            输入：pos（InventoryPosition）
            输出：None

            Args:
                pos (InventoryPosition): 当前处理的 InventoryPosition 货位对象。

            Returns:
                None: 不返回业务数据；处理结果写入实例状态、传入对象或外部响应。
            """
            key = id(pos)
            if key not in touched_reserved:
                touched_reserved[key] = (pos, bool(getattr(pos, "reserved", False)))
            pos.reserved = True

        try:
            # 预先占用 pending 入库任务已分配的位置，避免重复货位分配。
            for queue in self._core.pending_inbound_by_aisle.values():
                for task in queue:
                    for pos in (getattr(task, "positions", None) or []):
                        _mark_reserved(pos)

            # 入库：每条 in_line 只有队首任务可执行。
            for aisle in self._core.aisles:
                queue = list(self._core.pending_inbound_by_aisle.get(aisle, []))
                line_heads: Dict[int, TaskData] = {}
                for task in queue:
                    line = int(getattr(task, "in_line", 1) or 1)
                    if line not in line_heads:
                        line_heads[line] = task
                for task in line_heads.values():
                    if self._is_task_currently_executable(task):
                        if not getattr(task, "positions", None):
                            continue
                        _append(int(aisle), task)

            # 出库：将可执行的 pending 出库任务映射到已匹配的巷道。
            for task in list(self._core.pending_outbound_queue):
                if not self._is_task_currently_executable(task):
                    continue
                positions = getattr(task, "positions", None) or []
                if not positions:
                    continue
                try:
                    aisle = int(getattr(positions[0], "aisle", 0) or 0)
                except Exception:
                    aisle = 0
                if aisle > 0:
                    _append(aisle, task)
        finally:
            for pos, old_reserved in touched_reserved.values():
                pos.reserved = old_reserved

        return executable_by_aisle

    def adjust_inbound_task(
        self,
        task_id: str,
        target_aisle: Optional[int] = None,
        skus: Optional[List[Dict[str, Any]]] = None,
        positions_data: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[bool, Optional[str], Optional[TaskData]]:
        """在 EXECUTING 前调整 pending 入库任务的巷道、SKU 或位置。

        调用来源为任务调整接口。换巷道时任务 ID 与 ``in_line`` 保持不变，并被
        移到目标巷道 pending 队首；换 SKU 或巷道且未显式给位置时，基于目标巷道
        已冻结任务重新分配。显式 positions 先经过外部行号映射和冲突校验。成功
        后写回并冻结新 positions，返回 ``(success, reason, task)``。

        Args:
            task_id (str): 任务唯一标识，用于定位队列、冻结位置或反馈对象。
            target_aisle (Optional[int]): 入库调整后希望使用的巷道；为空时维持原巷道。
            skus (Optional[List[Dict[str, Any]]]): 当前任务或计划中的 SKU 条目列表。
            positions_data (Optional[List[Dict[str, Any]]]): 外部接口传入的位置描述列表，使用 API 的排号和 shelf 口径。

        Returns:
            Tuple[bool, Optional[str], Optional[TaskData]]: 处理成功、条件成立或校验通过时为 ``True``，否则为 ``False``。
        """
        self._last_feedback_error = None

        current_aisle = None
        task = None
        for aisle, queue in self._core.pending_inbound_by_aisle.items():
            for t in queue:
                if getattr(t, "task_id", None) == task_id:
                    current_aisle = int(aisle)
                    task = t
                    break
            if task is not None:
                break

        if task is None:
            return False, f"任务 {task_id} 不在入库pending队列中", None
        if task_id in self._core.running_tasks:
            return False, f"任务 {task_id} 正在执行中，不能调整", None

        if current_aisle is None:
            return False, f"任务 {task_id} 缺少当前巷道，不能调整", None
        new_aisle = int(target_aisle) if target_aisle is not None else int(current_aisle)
        if new_aisle not in self._core.aisles:
            return False, f"目标巷道 {new_aisle} 无效", None
        if not self.is_aisle_available(new_aisle):
            return False, f"目标巷道 {new_aisle} 当前不可用", None

        if skus is not None:
            normalized_skus: List[Dict[str, Any]] = []
            for sku in skus:
                sku_dict = self._sku_entry_to_dict(sku)
                normalized_skus.append(sku_dict)
            invalid_result = self.find_invalid_skus(
                [{"taskId": task_id, "taskType": TASK_TYPE_INBOUND, "skus": normalized_skus}],
                task_types=[TASK_TYPE_INBOUND],
            )
            invalid_skus = invalid_result.get("invalidSkus", [])
            if invalid_skus:
                invalid_text = ", ".join(str(sku) for sku in invalid_skus)
                return False, f"存在未维护在BOM中的SKU: {invalid_text}", None
            task.skus = normalized_skus

        # 换巷道必须保持入口顺序语义：任务被放到新巷道可执行队首，而不是重新
        # 生成任务或改变 in_line。
        if new_aisle != current_aisle:
            old_queue = self._core.pending_inbound_by_aisle.get(current_aisle, [])
            self._core.pending_inbound_by_aisle[current_aisle] = [t for t in old_queue if getattr(t, "task_id", None) != task_id]
            target_queue = self._core.pending_inbound_by_aisle.get(new_aisle, [])
            task.assigned_aisle = new_aisle
            self._core.pending_inbound_by_aisle[new_aisle] = [task] + target_queue

        # 位置优先级：显式位置先校验；否则以目标巷道已冻结的位置为前序占用重算。
        effective_positions = None
        if positions_data:
            resolved = self._resolve_feedback_positions(new_aisle, positions_data)
            if resolved is None:
                return False, f"任务 {task_id} 提供的 positions 无效", None
            ok, reason = self._validate_adjust_positions(task_id, new_aisle, resolved)
            if not ok:
                return False, reason, None
            effective_positions = resolved
        else:
            # 巷道或 SKU 调整时，基于目标巷道内已固化的位置重新分配。
            effective_positions = self._allocate_feedback_positions_for_aisle(task, new_aisle)
            if not effective_positions:
                return False, f"任务 {task_id} 在巷道 {new_aisle} 无法分配位置", None

        task.positions = list(effective_positions)
        self.freeze_task_api_positions(task)
        return True, None, task

    def _validate_adjust_positions(
        self,
        task_id: str,
        aisle: int,
        positions: List[InventoryPosition],
    ) -> Tuple[bool, Optional[str]]:
        """校验手工调整位置是否与巷道当前位置和冻结任务冲突。

        依次检查巷道当前位置、可执行任务、所有 pending 入库任务及 running
        任务。这些集合覆盖设备终点、已对外承诺位置、尚未成为队首的固化位置和
        实际执行占位；返回文本保持 API 外部行号口径。

        Args:
            task_id (str): 任务唯一标识，用于定位队列、冻结位置或反馈对象。
            aisle (int): 目标巷道编号。
            positions (List[InventoryPosition]): 当前任务对应的候选或冻结货位列表。

        Returns:
            Tuple[bool, Optional[str]]: 处理成功、条件成立或校验通过时为 ``True``，否则为 ``False``。
        """
        requested_ids = {p.get_position_id() for p in (positions or [])}
        requested_pos_map = {p.get_position_id(): p for p in (positions or [])}
        if not requested_ids:
            return False, "positions 不能为空"

        # 1）检查巷道当前位置。
        current_pos = (getattr(self._core, "current_position_by_aisle", {}) or {}).get(aisle)
        if current_pos is not None and current_pos.get_position_id() in requested_ids:
            return False, f"位置 {current_pos.get_position_id()} 与巷道当前位置冲突"

        # 2）检查可执行任务已占用的位置。
        occupied_ids = set()
        occupied_pos_map: Dict[str, InventoryPosition] = {}
        executable = self.get_executable_tasks_by_aisle().get(int(aisle), [])
        for t in executable:
            if getattr(t, "task_id", None) == task_id:
                continue
            for pos in (getattr(t, "positions", None) or []):
                pos_id = pos.get_position_id()
                occupied_ids.add(pos_id)
                occupied_pos_map[pos_id] = pos

        # 3）检查巷道内全部 pending 入库任务占用的位置，不仅限于可执行队首。
        for t in (self._core.pending_inbound_by_aisle.get(int(aisle), []) or []):
            if getattr(t, "task_id", None) == task_id:
                continue
            for pos in (getattr(t, "positions", None) or []):
                pos_id = pos.get_position_id()
                occupied_ids.add(pos_id)
                occupied_pos_map[pos_id] = pos

        # 4）检查巷道内 running 入库和出库任务占用的位置。
        for t in self._core.running_tasks.values():
            if getattr(t, "task_id", None) == task_id:
                continue
            if int(getattr(t, "assigned_aisle", 0) or 0) != int(aisle):
                continue
            for pos in (getattr(t, "positions", None) or []):
                pos_id = pos.get_position_id()
                occupied_ids.add(pos_id)
                occupied_pos_map[pos_id] = pos

        conflicts = requested_ids & occupied_ids
        if conflicts:
            def _to_api_position_text(pos: Optional[InventoryPosition], fallback_id: str) -> str:
                """执行 `_to_api_position_text` 对应的模块处理步骤，并返回该步骤产生的结果。

                输入：pos（Optional[InventoryPosition]）、fallback_id（str）
                输出：str

                Args:
                    pos (Optional[InventoryPosition]): 当前处理的 InventoryPosition 货位对象。
                    fallback_id (str): 当前对象的唯一标识。

                Returns:
                    str: 当前处理得到的文本标识、格式化结果或诊断信息。
                """
                if pos is None:
                    return fallback_id
                try:
                    return f"{int(pos.aisle)}-{int(pos.row)}-{int(pos.column):02d}-{int(pos.level):02d}"
                except Exception:
                    return fallback_id

            conflict_texts = []
            for pos_id in sorted(conflicts):
                pos_obj = occupied_pos_map.get(pos_id) or requested_pos_map.get(pos_id)
                conflict_texts.append(_to_api_position_text(pos_obj, pos_id))
            return False, f"位置冲突: {', '.join(conflict_texts)}"
        return True, None

    def get_prioritized_executable_tasks_by_aisle(self) -> Dict[int, List[TaskData]]:
        """Return executable tasks by aisle, with last recommended task first when present.

        Returns:
            Dict[int, List[TaskData]]: 符合当前处理条件的结果列表或集合。
        """
        executable = self.get_executable_tasks_by_aisle()
        prioritized: Dict[int, List[TaskData]] = {}
        for aisle, tasks in executable.items():
            task_list = list(tasks or [])
            recommended_task_id = (self._last_recommended_task_by_aisle or {}).get(int(aisle))
            if recommended_task_id:
                task_list.sort(
                    key=lambda t: (
                        0 if getattr(t, "task_id", None) == recommended_task_id else 1,
                        str(getattr(t, "task_id", "")),
                    )
                )
            prioritized[int(aisle)] = task_list
        return prioritized

    def _complete_task_execution(self, task_id: str, task_type: str) -> bool:
        """处理 ``COMPLETED`` 反馈并把虚拟承诺转化为真实库存变化。

        出库按任务冻结的 ``positions`` 和 SKU 顺序扣减；入库按同一映射写入。
        完成后才删除 ``running_tasks`` 中的任务，并推进真实或虚拟生产组，确保
        API 虚拟预占与真实库存扣减在时间上分离。

        Args:
            task_id (str): 任务唯一标识，用于定位队列、冻结位置或反馈对象。
            task_type (str): 任务类型，例如 INBOUND 或 OUTBOUND。

        Returns:
            bool: 处理成功、条件成立或校验通过时为 ``True``，否则为 ``False``。
        """
        task = self._core.running_tasks.get(task_id)

        if task is None:
            print(f"[WarehouseService] 任务 {task_id} 不在running_tasks中，无法完成")
            return False

        aisle = task.assigned_aisle
        production_line = task.production_line

        if task.task_type == TASK_TYPE_OUTBOUND:
            # 出库任务完成
            # 1. 扣减库存
            if getattr(task, 'positions', None):
                sku_ids_list = []
                for s in (task.skus or []):
                    if isinstance(s, dict):
                        sid = s.get('skuId')
                    else:
                        sid = getattr(s, 'skuId', None)
                    if sid:
                        sku_ids_list.append(sid)

                # 按 SKU 顺序消费冻结位置。天然配对双梁可复用同一双层位置，
                # remove_inventory 会依据 SKU 与属性定位实际 upper/lower 层。
                for idx, sku in enumerate(sku_ids_list):
                    # 同一双层货位承接双梁时，位置会按 SKU 顺序复用；库存管理器
                    # 再根据 SKU 与属性定位对应上下层并同步索引。
                    pos = task.positions[min(idx, len(task.positions) - 1)]
                    try:
                        self._core.inventory_manager.remove_inventory(pos, sku, 1)
                        print(f"[WarehouseService] 出库扣减库存: SKU {sku} 从位置 {pos.get_position_id()}")
                    except Exception as e:
                        print(f"[WarehouseService] 扣减库存失败: {sku}, 错误: {e}")

            # 2. 更新拥堵状态
            if production_line is not None and aisle is not None:
                # 设置拥堵，一段时间后自动解除
                outbound_congestion_time = getattr(self._core, 'outbound_congestion_time', 1.0)
                outbound_finish_time = self._core.current_time + outbound_congestion_time
                self._core.update_blockage_status(
                    aisle, production_line,
                    blocked=True,
                    unblock_time=outbound_finish_time
                )
                print(f"[WarehouseService] 设置巷道 {aisle} 产线 {production_line} 拥堵直到 {outbound_finish_time:.2f}s")

            # 3. 标记生产计划进度
            if production_line is not None:
                if not bool(getattr(task, "uses_virtual_group", False)):
                    self._core.mark_outbound_completed(production_line, task, self._core.current_time)
                    current_group = self._core.production_line_current_group.get(production_line, 0)
                    print(f"[WarehouseService] 产线 {production_line} 当前组索引: {current_group}")

        # INBOUND
        else:
            # 入库任务完成
            # 增加库存（如果任务有位置信息）
            if getattr(task, 'positions', None) and task.skus:
                # 入库同样复用冻结位置，不在完成反馈时再次调用分配器；否则 pending
                # 阶段的模拟占位与真实落账会发生偏离。
                for idx, sku_info in enumerate(task.skus):
                    sku_dict = self._sku_entry_to_dict(sku_info)
                    sku_id = sku_dict.get('skuId')
                    sku_attrs = self._extract_sku_attrs(sku_dict)
                    if not sku_id:
                        continue
                    # API 完成反馈没有附带实际时间时，以此刻作为新入库梁的 FIFO
                    # 起点；若任务 SKU 已带 inboundTime/arrivalTime，提取函数保留其值。
                    sku_attrs.setdefault("_inbound_time", self._resolve_inbound_time(None))
                    pos = task.positions[min(idx, len(task.positions) - 1)]
                    try:
                        self._core.inventory_manager.add_inventory(pos, sku_id, 1, attrs=sku_attrs)
                        print(f"[WarehouseService] 入库增加库存: SKU {sku_id} 到位置 {pos.get_position_id()}")
                    except Exception as e:
                        print(f"[WarehouseService] 增加库存失败: {sku_id}, 错误: {e}")

        # 从running_tasks移除
        # 真实库存成功写入/扣减后才释放 running 占用，后续查询将不再预占该任务。
        del self._core.running_tasks[task_id]

        # 添加到已完成列表
        self._core.completed_tasks.append(task)

        if task.task_type == TASK_TYPE_OUTBOUND and production_line is not None and bool(getattr(task, "uses_virtual_group", False)):
            self._advance_virtual_outbound_group(int(production_line))
            current_group = self._virtual_outbound_current_group.get(int(production_line), 0)
            print(f"[WarehouseService] 虚拟产线 {production_line} 当前组索引推进为: {current_group}")

        # 更新巷道当前位置
        if getattr(task, 'positions', None) and aisle is not None:
            self._core.current_position_by_aisle[aisle] = task.positions[-1]

        print(f"[WarehouseService] 任务 {task_id} 已完成并从running_tasks移除")
        return True

    def _fail_task_execution(self, task_id: str, reason: str) -> bool:
        """处理 ``FAILED`` 反馈并释放该任务的 API 层承诺。

        失败不改写真实库存，因为库存只在完成时更新；它从 pending/running 队列
        和 API 可执行缓存移除任务，并清理任务造成的阻塞。下一次 mixed 或任务
        查询会据此重新构造虚拟资源视图。

        Args:
            task_id (str): 任务唯一标识，用于定位队列、冻结位置或反馈对象。
            reason (str): 失败、拒绝或状态变更的原因说明。

        Returns:
            bool: 处理成功、条件成立或校验通过时为 ``True``，否则为 ``False``。
        """
        # 从待执行队列移除
        # 首先清除 API 展示缓存，避免 FAILED 任务继续出现在可执行任务列表。
        self._pending_execution_tasks.pop(task_id, None)

        # FAILED 释放的是 API 队列、运行占用和展示缓存；它不调用库存扣减/写入，
        # 所以该任务占用的虚拟出库资源会在下一次查询时自然从基线中消失。
        task = self._core.running_tasks.pop(task_id, None)

        if task:
            aisle = task.assigned_aisle
            production_line = task.production_line

            # 从pending队列移除（以防任务还在队列中）
            if task.task_type == TASK_TYPE_OUTBOUND:
                self._core.pending_outbound_queue = [
                    t for t in self._core.pending_outbound_queue if t.task_id != task_id
                ]
            # INBOUND
            else:
                if aisle and aisle in self._core.pending_inbound_by_aisle:
                    self._core.pending_inbound_by_aisle[aisle] = [
                        t for t in self._core.pending_inbound_by_aisle[aisle] if t.task_id != task_id
                    ]

            # 清理拥堵状态（如果任务设置了无限期拥堵）
            if aisle is not None and production_line is not None:
                status = self._core.blockage_status.get((aisle, production_line), {})
                if status.get('blocked', False):
                    # 解除拥堵
                    self._core.update_blockage_status(
                        aisle, production_line,
                        blocked=False,
                        unblock_time=0.0
                    )
                    print(f"[WarehouseService] 清理任务失败导致的巷道 {aisle} 产线 {production_line} 拥堵状态")

            print(f"[WarehouseService] 任务 {task_id} 失败: {reason}，已完成所有状态清理")
        else:
            # 即使不在running_tasks，也尝试从pending队列移除
            # 清理出库队列
            self._core.pending_outbound_queue = [
                t for t in self._core.pending_outbound_queue if t.task_id != task_id
            ]
            # 清理入库队列
            for aisle in list(self._core.pending_inbound_by_aisle.keys()):
                self._core.pending_inbound_by_aisle[aisle] = [
                    t for t in self._core.pending_inbound_by_aisle[aisle] if t.task_id != task_id
                ]

            print(f"[WarehouseService] 任务 {task_id} 失败: {reason}（任务不在running_tasks中）")

        return True

    def set_production_plan(self, production_plan: Any,
                           update: bool = False) -> bool:
        """
        设置生产计划

        调用的warehouse_core方法：
        - set_production_plan(): 设置生产计划

        更新的参数：
        - production_plan: 生产计划
        - production_line_current_group: 各产线当前组
        - production_line_completed_tasks: 各产线已完成任务
        - production_line_group_completion_times: 各产线组完成时间

        Args:
            production_plan: 生产计划字典
            update: 是否为更新操作（True则替换现有计划）

        Returns:
            是否成功设置
        """
        self._sync_time()

        try:
            preserve_progress = not update
            saved_current_group = deepcopy(getattr(self._core, "production_line_current_group", {}) or {})
            saved_completed_tasks = deepcopy(getattr(self._core, "production_line_completed_tasks", {}) or {})
            saved_group_completion_times = deepcopy(getattr(self._core, "production_line_group_completion_times", {}) or {})

            if preserve_progress and isinstance(production_plan, dict) and "production_plan" in production_plan:
                production_plan = self._build_effective_plan_payload(production_plan, replace_existing=False)
            self._core.set_production_plan(production_plan)
            if preserve_progress:
                new_plan = (production_plan.get("production_plan") if isinstance(production_plan, dict) else {}) or {}
                for line_id in new_plan.keys():
                    line_id = int(line_id)
                    self._virtual_outbound_current_group.pop(line_id, None)
                    if line_id in saved_current_group:
                        self._core.production_line_current_group[line_id] = saved_current_group[line_id]
                    if line_id in saved_completed_tasks:
                        self._core.production_line_completed_tasks[line_id] = saved_completed_tasks[line_id]
                    if line_id in saved_group_completion_times:
                        self._core.production_line_group_completion_times[line_id] = saved_group_completion_times[line_id]
            else:
                new_plan = (production_plan.get("production_plan") if isinstance(production_plan, dict) else {}) or {}
                for line_id in new_plan.keys():
                    self._virtual_outbound_current_group.pop(int(line_id), None)
            return True
        except Exception as e:
            print(f"[WarehouseService] 设置生产计划失败: {e}")
            return False

    def get_production_plan(self) -> Dict[int, List]:
        """获取当前生产计划

        Returns:
            Dict[int, List]: 符合当前处理条件的结果列表或集合。
        """
        return self._core.production_plan

    # ============================================================
    # 状态查询方法
    # ============================================================

    def get_running_tasks(self) -> Dict[str, TaskData]:
        """获取正在执行的任务

        Returns:
            Dict[str, TaskData]: 按函数定义字段组织的结果映射。
        """
        return self._core.running_tasks.copy()

    def get_pending_tasks(self) -> Dict[str, Any]:
        """获取等待中的任务

        Returns:
            Dict[str, List[TaskData]]: 符合当前处理条件的结果列表或集合。
        """
        return {
            'inbound': {
                aisle: list(tasks)
                for aisle, tasks in self._core.pending_inbound_by_aisle.items()
            },
            'outbound': list(self._core.pending_outbound_queue)
        }

    def get_last_matched_tasks_by_aisle(self) -> Dict[int, List[TaskData]]:
        """获取上一次调度请求的匹配预览结果（仅用于API返回）。

        Returns:
            Dict[int, List[TaskData]]: 符合当前处理条件的结果列表或集合。
        """
        return {aisle: list(tasks) for aisle, tasks in (self._last_matched_tasks_by_aisle or {}).items()}

    def get_last_unsubmitted_outbound_tasks(self) -> List[Dict[str, Any]]:
        """获取上一次 mixed 请求中因库存不足而未提交的出库任务。

        Returns:
            List[Dict[str, Any]]: 符合当前处理条件的结果列表或集合。
        """
        return [dict(task) for task in (self._last_unsubmitted_outbound_tasks or [])]

    def get_completed_tasks(self) -> List[TaskData]:
        """获取已完成的任务

        Returns:
            List[TaskData]: 符合当前处理条件的结果列表或集合。
        """
        return list(self._core.completed_tasks)

    def get_inventory_summary(self) -> Dict[int, Dict[str, int]]:
        """获取库存摘要（过滤数量为0的SKU）

        Returns:
            Dict[int, Dict[str, int]]: 按函数定义字段组织的结果映射。
        """
        summary: Dict[int, Dict[str, int]] = {}
        for aisle, skus in self._core.inventory_manager.current_inventory.items():
            summary[aisle] = {sku: qty for sku, qty in skus.items() if qty > 0}
        return summary

    def get_full_inventory(self) -> List[Dict[str, Any]]:
        """获取全量库存列表（与inventory请求结构一致）

        Returns:
            List[Dict[str, Any]]: 符合当前处理条件的结果列表或集合。
        """
        full_inventory: List[Dict[str, Any]] = []
        match_fields = list(getattr(self._core, "match_fields", []) or [])
        for position in self._core.inventory_manager.inventory_positions:
            aisle_id = str(position.aisle)
            # 转换内部 row 为外部 row
            external_row = self._internal_to_external_row(position.row, position.aisle)
            base_info = {
                "aisleId": aisle_id,
                "row": external_row,
                "column": position.column,
                "level": position.level,
            }

            if position.is_double_layer:
                upper_sku = position.upper_sku or ""
                upper_qty = position.upper_quantity or 0
                lower_sku = position.lower_sku or ""
                lower_qty = position.lower_quantity or 0
                upper_entry = {"skuId": upper_sku, "quantity": upper_qty}
                lower_entry = {"skuId": lower_sku, "quantity": lower_qty}
                upper_attrs = getattr(position, "upper_attrs", {}) or {}
                lower_attrs = getattr(position, "lower_attrs", {}) or {}
                if match_fields and upper_sku and upper_qty > 0:
                    for field in match_fields:
                        if field in upper_attrs:
                            upper_entry[field] = upper_attrs.get(field)
                if match_fields and lower_sku and lower_qty > 0:
                    for field in match_fields:
                        if field in lower_attrs:
                            lower_entry[field] = lower_attrs.get(field)
                if "_inbound_time" in upper_attrs:
                    upper_entry["inboundTime"] = upper_attrs["_inbound_time"]
                if "_inbound_time" in lower_attrs:
                    lower_entry["inboundTime"] = lower_attrs["_inbound_time"]
                full_inventory.append({
                    **base_info,
                    "shelf": "UPPER",
                    "positions": [upper_entry],
                })
                full_inventory.append({
                    **base_info,
                    "shelf": "LOWER",
                    "positions": [lower_entry],
                })
            else:
                sku = getattr(position, "sku", None) or ""
                qty = getattr(position, "quantity", None) or 0
                entry = {"skuId": sku, "quantity": qty}
                sku_attrs = getattr(position, "sku_attrs", {}) or {}
                if match_fields and sku and qty > 0:
                    for field in match_fields:
                        if field in sku_attrs:
                            entry[field] = sku_attrs.get(field)
                if "_inbound_time" in sku_attrs:
                    entry["inboundTime"] = sku_attrs["_inbound_time"]
                full_inventory.append({
                    **base_info,
                    "shelf": None,
                    "positions": [entry],
                })

        return full_inventory

    def get_aisle_status(self) -> Dict[int, Dict]:
        """获取巷道状态

        Returns:
            Dict[int, Dict]: 按函数定义字段组织的结果映射。
        """
        result = {}
        for aisle in self._core.aisles:
            is_busy = any(
                t.assigned_aisle == aisle
                for t in self._core.running_tasks.values()
            )
            blockage_info = {}
            for pl in range(1, self._core.num_production_lines + 1):
                status = self._core.blockage_status.get((aisle, pl), {})
                unblock_time = status.get('unblock_time', 0.0)
                # float('inf') 无法被JSON序列化，转换为 -1 表示无限
                if unblock_time == float('inf'):
                    unblock_time = -1
                blockage_info[pl] = {
                    'blocked': status.get('blocked', False),
                    'unblock_time': unblock_time
                }

            result[aisle] = {
                'is_busy': is_busy,
                'blockage': blockage_info,
                'current_position': self._serialize_position_for_api(
                    self._core.current_position_by_aisle.get(aisle)
                )
            }
        return result

    def update_sku_config(self, config_data: Dict[str, Any]) -> bool:
        """
        更新 SKU 配置

        Args:
            config_data: 包含 sku_types, sku_pairs, sku_solo, sku_to_production_line 的字典

        Returns:
            bool: 更新是否成功

        操作步骤：
        1. 验证配置数据的完整性
        2. 将配置数据写入 API 专用的 config/sku_config.json
        3. 重新加载 WarehouseCore 的 SKU 配置属性
        """
        try:
            # 验证必填字段
            required_fields = ['sku_types', 'sku_pairs', 'sku_solo', 'sku_to_production_line']
            for field in required_fields:
                if field not in config_data:
                    raise ValueError(f"缺少必填字段: {field}")

            # 写入配置文件
            config_path = Path(project_root) / "config" / "sku_config.json"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump(config_data, f, ensure_ascii=False, indent=2)

            # 重新加载 WarehouseCore 的 SKU 配置
            self._core.config_data = config_data
            self._core.sku_config_path = str(config_path)
            self._core.sku_types = config_data["sku_types"]
            self._core.sku_to_production_line = config_data["sku_to_production_line"]
            self._core.sku_pairs = config_data["sku_pairs"]
            self._core.sku_solo = config_data["sku_solo"]
            self._bom_config_snapshot = self._build_bom_snapshot()

            # 关键：入库巷道/货位分配器（如 ProposedAisleAllocator / ProposedPositionAllocator）
            # 在初始化时会缓存 sku_pairs/sku_solo；BOM update 若只替换 core 引用，
            # 可能导致分配器仍使用旧 BOM。这里显式刷新分配器内部缓存，让热更新立即生效。
            try:
                aisle_alloc = getattr(self._core, "inbound_aisle_allocator", None)
                if aisle_alloc is not None:
                    if hasattr(aisle_alloc, "sku_pairs"):
                        aisle_alloc.sku_pairs = self._core.sku_pairs
                    if hasattr(aisle_alloc, "sku_solo"):
                        aisle_alloc.sku_solo = self._core.sku_solo
                    if hasattr(aisle_alloc, "paired_sku_set"):
                        aisle_alloc.paired_sku_set = set(self._core.sku_pairs.keys()) | set(self._core.sku_pairs.values())

                pos_alloc = getattr(self._core, "inbound_position_allocator", None)
                if pos_alloc is not None:
                    if hasattr(pos_alloc, "sku_pairs"):
                        pos_alloc.sku_pairs = self._core.sku_pairs
            except Exception as e:
                # 分配器刷新失败不应导致 BOM update 整体失败；仍然保证 core 配置已更新。
                print(f"[WARN] 刷新入库分配器缓存失败: {e}")

            return True
        except Exception as e:
            print(f"[ERROR] 更新 SKU 配置失败: {e}")
            raise


# ============================================================
# 全局服务实例管理
# ============================================================

_warehouse_service: Optional[WarehouseService] = None


def get_warehouse_service() -> WarehouseService:
    """获取全局仓库服务实例（用于FastAPI依赖注入）

    Args:
        None: 无显式业务参数；使用实例状态或模块配置。

    Returns:
        WarehouseService: 当前处理流程产生的结果；具体结构由函数摘要说明。
    """
    global _warehouse_service
    if _warehouse_service is None:
        _warehouse_service = WarehouseService()
    return _warehouse_service


def init_warehouse_service(warehouse_core: Optional[WarehouseCore] = None) -> WarehouseService:
    """初始化仓库服务（可选传入已有的WarehouseCore）

    Args:
        warehouse_core (Optional[WarehouseCore]，可选): 仓库核心对象，提供库存、任务队列、生产计划及配置状态。

    Returns:
        WarehouseService: 当前处理流程产生的结果；具体结构由函数摘要说明。
    """
    global _warehouse_service
    _warehouse_service = WarehouseService(warehouse_core)
    return _warehouse_service


def reset_warehouse_service():
    """重置仓库服务（用于测试）

    Args:
        None: 无显式业务参数；使用实例状态或模块配置。

    Returns:
        None: 通过实例状态、队列或外部副作用完成处理。
    """
    global _warehouse_service
    _warehouse_service = None
