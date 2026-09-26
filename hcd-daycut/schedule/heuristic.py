"""启发式调度模块。

根据提供的JAVA文件编写的旧调度策略（DMW）
在库存、货位、产线当前组和巷道资源约束已满足的前提下，快速生成一组可执行的
入库和出库任务序列。该模块既可直接作为基线调度器，也可为优化调度提供候选参考。
"""

# 模块职责：在硬约束过滤后快速生成可执行的基线巷道任务序列，供仿真
# 直接执行或作为 OptimizationScheduler 的比较基线。
import math
import time
from typing import Any, Dict, List, Optional
from simulation.task_data import TaskData, TASK_TYPE_INBOUND, TASK_TYPE_OUTBOUND
import random


class HeuristicScheduler:
    """按规则快速生成基线调度方案。

    该调度器优先处理满足当前组和库存条件的出库任务，再处理已经
    确定巷道的入库任务；running 任务以巷道负载形式参与筛选。它的
    返回结果既可直接执行，也作为 OptimizationScheduler 的保底方案。
    """

    def __init__(self, warehouse_core):
        """Args:
            warehouse_core: WarehouseCore

        Returns:
            None: 通过实例状态、队列或外部副作用完成处理。
        """
        self.warehouse_core = warehouse_core
        self.inventory_manager = warehouse_core.inventory_manager
        self.time_estimator = warehouse_core.time_estimator
        self.aisles = warehouse_core.aisles
        self.solve_count = 0
        self.total_time = 0.0

        # 入库货位分配器（如果warehouse_core有设置）
        self.position_allocator = None

    # ========================================================================
    # 主流程：规则调度与巷道任务序列生成
    # ========================================================================
    def solve(self, inbound_tasks: List[TaskData], outbound_tasks: List[TaskData],
             running_tasks: Optional[Dict[str, TaskData]] = None,
             current_time: float = 0.0) -> Dict[int, List[TaskData]]:
        """
        生成按巷道分桶的基线任务序列。

        出库阶段只保留当前生产组，并先扫描真实库存中的合法位置；入库
        阶段复用已固定 positions，否则调用 position_allocator 生成位置。
        每个任务最终都会复制为带 assigned_aisle 和 positions 的 TaskData，
        供后续时间估算和事件调度使用。

        Args:
            inbound_tasks:
            outbound_tasks:
            running_tasks: 正在执行的任务字典 {task_id: task_info}，用于统计巷道任务数量

        Returns:
            aisle_task_sequences: {aisle: [task_info, ...]}
                task_info: task_id, task_type, production_line, sku, position, priority

        局部变量：``task_position_assignments`` 缓存任务与其已确定位置的对应关系；
        ``aisle_task_count`` 是 running 任务占用后的巷道任务数；``filtered_outbound_tasks``
        是当前生产组出库任务；``aisle_task_sequences`` 是最终按巷道输出的新任务副本；
        ``start_time`` 与 ``solve_time`` 仅用于算法耗时统计，不参与仓库时间推进。
        """
        # 记录本次调度墙钟耗时的起点。
        start_time = time.time()

        # {task_id: 已选择的位置或位置列表}，避免重复扫描。
        task_position_assignments = {}
        # aisle_task_count 是巷道已有运行任务数量，后续候选优先选任务数更少的巷道。
        aisle_task_count = {aisle: 0 for aisle in self.aisles}
        if running_tasks:
            for task_info in running_tasks.values():
                aisle = task_info.assigned_aisle
                if aisle in aisle_task_count:
                    aisle_task_count[aisle] += 1

        # 0. 筛选出库任务：只保留当前组，防止启发式方案跨组推进。
        # 通过task_id中的组号判断（新格式：OUTBOUND_PL{pl}_GP{group}_{sku1}_{sku2}）
        # 只含当前生产组，避免启发式调度跨组推进。
        filtered_outbound_tasks = []
        for task in outbound_tasks:
            production_line = task.production_line
            current_group_idx = self.warehouse_core.production_line_current_group[production_line]
            explicit_group_idx = getattr(task, "group_idx", None)
            if explicit_group_idx is not None:
                try:
                    if int(explicit_group_idx) == current_group_idx:
                        filtered_outbound_tasks.append(task)
                    else:
                        print(f"  [启发式]筛选掉非当前组任务: {task.task_id} (任务组={explicit_group_idx}, 当前组={current_group_idx})")
                    continue
                except (TypeError, ValueError):
                    pass
            try:
                parts = task.task_id.split('_')
                # 期望：['OUTBOUND', 'PL{pl}', 'GP{group}', sku1, sku2]
                if len(parts) >= 3 and parts[0] == 'OUTBOUND' and parts[1].startswith('PL') and parts[2].startswith('GP'):
                    # 去掉 'GP' 前缀。
                    task_group_number = int(parts[2][2:])
                    task_group_idx = task_group_number - 1
                    if task_group_idx == current_group_idx:
                        filtered_outbound_tasks.append(task)
                    else:
                        print(f"  [启发式]筛选掉非当前组任务: {task.task_id} (任务组={task_group_idx}, 当前组={current_group_idx})")
                else:
                    print(f"  [启发式]警告：无法解析任务ID格式: {task.task_id}，保留该任务")
                    filtered_outbound_tasks.append(task)
            except (ValueError, IndexError):
                print(f"  [启发式]警告：解析任务ID时出错: {task.task_id}，保留该任务")
                filtered_outbound_tasks.append(task)

        outbound_tasks = filtered_outbound_tasks

        # 2. 出库任务分配：先建立每条巷道的合法位置集合，再做负载筛选。
        for task in outbound_tasks:
            # SKU 数量异常时也保留默认值，供后续统一的缺货诊断分支读取。
            found = False
            # 获取SKU列表
            sku_ids = task.get_sku_ids()
            if not sku_ids:
                continue
            production_line = task.production_line

            sku_attrs_map = {}
            for s in (task.skus or []):
                sku_dict = self._sku_entry_to_dict(s)
                sku_id = sku_dict.get("skuId")
                if sku_id and sku_id not in sku_attrs_map:
                    sku_attrs_map[sku_id] = self._extract_sku_attrs(sku_dict)

            # 查找可用位置
            available_positions_by_aisle = {}

            if len(sku_ids) == 1:
                # 单梁任务：查找包含该SKU的位置
                sku = sku_ids[0]
                found = False
                sku_attrs = sku_attrs_map.get(sku, {})
                for pos in self.inventory_manager.get_sku_positions(sku, only_available=True):
                    if not pos.matches_sku(sku, sku_attrs, self.warehouse_core.match_fields):
                        continue
                    if pos.aisle not in available_positions_by_aisle:
                        available_positions_by_aisle[pos.aisle] = []
                    available_positions_by_aisle[pos.aisle].append(pos)
                    found = True
                if not found:
                    # 没有找到该SKU
                    for aisle in self.aisles:
                        try:
                            print(f"[调试] SKU {sku} 在巷道{aisle}的current_inventory数量: {self.inventory_manager.current_inventory[aisle][sku]}")
                        except Exception as e:
                            print(f"[调试] SKU {sku} 在巷道{aisle} 查询current_inventory出错: {e}")
            elif len(sku_ids) == 2:
                # 双梁任务：查找同时包含两个SKU的双层位置
                found = False
                sku1, sku2 = sku_ids
                attrs1 = sku_attrs_map.get(sku1, {})
                attrs2 = sku_attrs_map.get(sku2, {})
                for pos in self.inventory_manager.inventory_positions:
                    if (pos.is_double_layer
                        and pos.matches_pair(sku1, attrs1, sku2, attrs2, self.warehouse_core.match_fields)
                        and pos.upper_quantity > 0
                        and pos.lower_quantity > 0):
                        if self.warehouse_core._is_position_reserved_for_other_task(pos, task.task_id):
                            continue
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
                    sku_attrs = sku_attrs_map.get(sku, {})
                    raw_positions = [
                        pos for pos in self.inventory_manager.inventory_positions
                        if (pos.upper_sku == sku and pos.upper_quantity > 0)
                        or (pos.lower_sku == sku and pos.lower_quantity > 0)
                        or (not pos.is_double_layer and pos.sku == sku and pos.quantity > 0)
                    ]
                    print(f"[调试] SKU {sku} 存放位置(含attrs):")
                    if not raw_positions:
                        print("  (无)")
                    else:
                        for pos in raw_positions:
                            try:
                                upper_attrs = getattr(pos, "upper_attrs", {})
                                lower_attrs = getattr(pos, "lower_attrs", {})
                                single_attrs = getattr(pos, "sku_attrs", {})
                                info = (
                                    f"位置ID: {pos.get_position_id()}, 巷道: {pos.aisle}, "
                                    f"upper: {pos.upper_sku}/{pos.upper_quantity if hasattr(pos,'upper_quantity') else '?'} attrs={upper_attrs}, "
                                    f"lower: {pos.lower_sku}/{pos.lower_quantity if hasattr(pos,'lower_quantity') else '?'} attrs={lower_attrs}, "
                                    f"single attrs={single_attrs}"
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
                    is_blocked = self.warehouse_core.check_blockage(aisle, production_line, current_time=current_time)
                    if not is_blocked:
                        valid_aisles.append(aisle)

                # 从有效巷道中选择任务最少的巷道
                if valid_aisles:
                    best_aisle = min(
                        valid_aisles,
                        key=lambda a: (
                            aisle_task_count[a],
                            self._position_fifo_time(task, self._select_outbound_position(task, available_positions_by_aisle[a])) if self._fifo_enabled() else 0.0,
                            a,
                        ),
                    )
                    best_position = self._select_outbound_position(task, available_positions_by_aisle[best_aisle])
                    aisle_task_count[best_aisle] += 1
                    task_position_assignments[task.task_id] = [best_position]

        # 3. 入库任务分配；已冻结位置优先，避免重复调度改变任务落位。
        for task in inbound_tasks:
            target_aisle = task.assigned_aisle
            if target_aisle is None or self.position_allocator is None:
                continue
            if aisle_task_count[target_aisle] > 0:
                continue
            if getattr(task, "positions", None):
                task_position_assignments[task.task_id] = list(task.positions)
                continue
            current_position = self.warehouse_core.current_position_by_aisle.get(target_aisle)
            allocated_positions = self.position_allocator.allocate(
                self.warehouse_core.inventory_manager.inventory_positions, task, current_position
            )
            if allocated_positions:
                task_position_assignments[task.task_id] = allocated_positions

        # 4. 将带位置的任务重新组织为巷道序列，作为统一调度输入。
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
                    task_record=task.task_record
                )
                new_task.plan_id = getattr(task, "plan_id", None)
                new_task.plan_index_public = getattr(task, "plan_index_public", None)
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
                    task_record=task.task_record
                )
                new_task.plan_id = getattr(task, "plan_id", None)
                new_task.plan_index_public = getattr(task, "plan_index_public", None)
                new_task.group_idx = getattr(task, "group_idx", None)
                aisle_task_sequences[aisle].append(new_task)


        solve_time = time.time() - start_time
        self.total_time += solve_time
        self.solve_count += 1

        return aisle_task_sequences


    # ========================================================================
    # 辅助函数：FIFO、SKU 属性和候选货位排序
    # ========================================================================
    def _fifo_enabled(self) -> bool:
        """读取出库 FIFO 开关。

        输入：无显式参数；读取 ``WarehouseCore.outbound_fifo_enabled``。
        输出：启用 FIFO 时返回 ``True``，否则返回 ``False``。
        作用：控制出库候选货位是按先入库先出库排序，还是按基线随机方式选取。

        Args:
            None: 无显式业务参数；使用实例状态或模块配置。

        """
        return bool(getattr(self.warehouse_core, "outbound_fifo_enabled", False))

    def _extract_position_inbound_time(self, pos, sku_id: str, attrs: dict) -> float:
        """读取一个候选货位中指定 SKU 的最早入库时间。

        输入：``pos`` 为单层或双层货位，``sku_id`` 和 ``attrs`` 为任务要求。
        输出：可匹配库存的最小 ``_inbound_time``；没有可匹配层时返回 ``math.inf``。
        双层货位需逐层验证属性匹配，缺失时间按 0 处理，使历史未记录时间的库存
        不会被 FIFO 规则永久排除。

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

        # 一个双层货位可能有两层同 SKU；统一转换时间后取最早的一层作为 FIFO 键。
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
        """计算任务从候选货位取货时使用的 FIFO 时间键。

        输入：出库 ``task`` 与已通过 SKU 匹配的候选 ``pos``。
        输出：任务各需求 SKU 中最早的可匹配入库时间；无 SKU 时返回 ``math.inf``。
        作用：先按任务携带的 ``match_fields`` 重建属性字典，再逐个 SKU 查询货位层，
        使同 SKU、不同属性的库存不会因只比较 SKU 而被错误排序。

        """
        sku_ids = task.get_sku_ids()
        if not sku_ids:
            return math.inf

        sku_attrs_map = {}
        # 同一 SKU 只保留一次属性快照，避免任务存在重复条目时重复解析。
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

    def _select_outbound_position(self, task: TaskData, positions: List[Any]):
        """从同一巷道的合法出库位置中选定一个实际执行位置。

        输入：出库 ``task`` 和已完成库存、属性、预留校验的 ``positions`` 列表。
        输出：一个货位对象；列表为空时返回 ``None``。
        FIFO 关闭时保留基线随机性；开启时先按入库时间升序，再按
        ``position_column_preference_mode`` 和层号排序，使最早库存优先，时间相同
        时仍与入库货位选择使用同一列号方向。

        """
        if not positions:
            return None
        if not self._fifo_enabled():
            return random.choice(positions)
        return min(
            positions,
            key=lambda p: (
                self._position_fifo_time(task, p),
                *self.warehouse_core.get_position_column_preference_key(p.aisle, p.column),
                p.level,
            ),
        )

    def _sku_entry_to_dict(self, sku):
        """将字典、Pydantic 模型或普通对象统一转换为 SKU 字典。

        输入：``sku`` 为 API 模型、字典或带 ``skuId``/``quantity`` 属性的对象。
        输出：至少包含 ``skuId`` 和 ``quantity`` 的新字典。
        作用：让调度器无需依赖上游调用方的具体数据模型即可读取 SKU 和匹配属性。

        """
        if isinstance(sku, dict):
            return dict(sku)
        if hasattr(sku, "model_dump"):
            return sku.model_dump()
        if hasattr(sku, "dict"):
            return sku.dict()
        return {"skuId": getattr(sku, "skuId", None), "quantity": getattr(sku, "quantity", 1)}

    def _extract_sku_attrs(self, sku_dict: dict) -> dict:
        """按仓库配置提取影响 SKU 配对的属性字段。

        输入：已规范化的 ``sku_dict``。
        输出：只包含 ``WarehouseCore.match_fields`` 中字段的字典；未配置字段时为空字典。

        """
        match_fields = getattr(self.warehouse_core, "match_fields", [])
        if not match_fields:
            return {}
        return {k: sku_dict.get(k) for k in match_fields}


    def get_average_solve_time(self) -> float:
        """读取并返回指定条件下的状态、对象或计算结果，不主动改变业务状态。

        输入：无显式业务输入；依赖实例字段或模块配置。
        输出：float

        Args:
            None: 无显式业务参数；使用实例状态或模块配置。

        """
        """"""
        if self.solve_count == 0:
            return 0.0
        return self.total_time / self.solve_count
