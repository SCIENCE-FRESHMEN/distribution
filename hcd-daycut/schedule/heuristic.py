"""


"""

import math
import time
from typing import Any, Dict, List
from simulation.task_data import TaskData, TASK_TYPE_INBOUND, TASK_TYPE_OUTBOUND
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

    def _select_outbound_position(self, task: TaskData, positions: List[Any]):
        if not positions:
            return None
        if not self._fifo_enabled():
            return random.choice(positions)
        return min(
            positions,
            key=lambda p: (
                self._position_fifo_time(task, p),
                -p.column,
                p.level,
            ),
        )

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
    
    def solve(self, inbound_tasks: List[TaskData], outbound_tasks: List[TaskData], 
             running_tasks: Dict = None, current_time: float = 0.0) -> Dict[int, List[TaskData]]:
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
        # 通过task_id中的组号判断（新格式：OUTBOUND_PL{pl}_GP{group}_{sku1}_{sku2}）
        filtered_outbound_tasks = []
        for task in outbound_tasks:
            production_line = task.production_line
            current_group_idx = self.warehouse_core.production_line_current_group[production_line]
            try:
                parts = task.task_id.split('_')
                # 期望：['OUTBOUND', 'PL{pl}', 'GP{group}', sku1, sku2]
                if len(parts) >= 3 and parts[0] == 'OUTBOUND' and parts[1].startswith('PL') and parts[2].startswith('GP'):
                    task_group_number = int(parts[2][2:])  # 去掉 'GP'
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
        
        # 2. 出库任务分配
        for task in outbound_tasks:
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
        
        # 3. 入库任务分配（先来先做，in_line 仅决定目标层，不影响排序）
        for task in inbound_tasks:
            target_aisle = task.assigned_aisle
            if aisle_task_count[target_aisle] > 0:
                continue
            current_position = self.warehouse_core.current_position_by_aisle.get(target_aisle)
            allocated_positions = self.position_allocator.allocate(
                self.warehouse_core.inventory_manager.inventory_positions, task, current_position
            )
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
                aisle_task_sequences[aisle].append(new_task)
        
        
        solve_time = time.time() - start_time
        self.total_time += solve_time
        self.solve_count += 1
        
        return aisle_task_sequences
    
    def get_average_solve_time(self) -> float:
        """"""
        if self.solve_count == 0:
            return 0.0
        return self.total_time / self.solve_count

