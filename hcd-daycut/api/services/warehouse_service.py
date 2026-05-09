"""
仓库服务层 - 桥接API与WarehouseCore

此模块负责：
1. 将API请求转换为warehouse_core可理解的格式
2. 调用warehouse_core的相关方法
3. 同步外部状态到warehouse_core
4. 管理仿真时间的推进
"""

import time
import random
import json
from copy import deepcopy
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass

# 导入warehouse_core相关模块
import sys
from pathlib import Path

# 确保可以导入simulation模块
project_root = Path(__file__).parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from simulation.warehouse_core import WarehouseCore
from simulation.task_data import TaskData, TASK_TYPE_INBOUND, TASK_TYPE_OUTBOUND
from simulation.position import InventoryPosition
from simulation.event import Event, EVENT_TASK_COMPLETE, EVENT_INBOUND_ARRIVAL_AT_AISLE


class WarehouseService:
    """
    仓库服务类 - API与WarehouseCore的桥接层
    
    核心职责：
    1. 管理WarehouseCore实例的生命周期
    2. 同步外部系统状态到内部状态
    3. 转换API数据格式
    4. 调用warehouse_core方法并返回结果
    """
    
    def __init__(self, warehouse_core: Optional[WarehouseCore] = None):
        """
        初始化仓库服务
        
        Args:
            warehouse_core: 可选的WarehouseCore实例，如果未提供则创建新实例
        """
        scheduler_type = 'optimization'
        inbound_aisle_strategy = 'proposed'
        inbound_allocation_strategy = 'proposed'
        if warehouse_core is None:
            self._core = WarehouseCore(
                scheduler_type=scheduler_type,
                inbound_aisle_strategy=inbound_aisle_strategy,
                inbound_allocation_strategy=inbound_allocation_strategy,
                config_path='config/warehouse.json'
            )
            # 初始化核心
            self._core.initialize()
        else:
            self._core = warehouse_core

        self._bom_config_snapshot = self._build_bom_snapshot()
        
        # 时间管理：使用实际时间戳（秒）
        self._start_time = time.time()
        self._last_sync_time = self._start_time
        
        # 待执行任务缓存 - 保存已分配但等待EXECUTING反馈的任务
        # {task_id: TaskData}
        self._pending_execution_tasks: Dict[str, TaskData] = {}

        # Last schedule call: tasks that can match a given aisle (planning/preview only).
        # {aisle_id: [TaskData, ...]}
        self._last_matched_tasks_by_aisle: Dict[int, List[TaskData]] = {}
        self._plan_id_to_line: Dict[str, int] = {}
        
    @property
    def core(self) -> WarehouseCore:
        """获取WarehouseCore实例"""
        return self._core
    
    def _get_current_time(self) -> float:
        """获取当前仿真时间（从启动开始的秒数）"""
        return time.time() - self._start_time

    def _sku_entry_to_dict(self, sku: Any) -> Dict[str, Any]:
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
        match_fields = getattr(self._core, "match_fields", [])
        if not match_fields:
            return {}
        return {k: sku_dict.get(k) for k in match_fields}

    def _get_value(self, obj: Any, key: str, default: Any = None) -> Any:
        if obj is None:
            return default
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    def _line_id_to_int(self, line_id: Any) -> int:
        text = str(line_id).strip().upper()
        if text.startswith("LINE-"):
            text = text.split("LINE-", 1)[1]
        elif text.startswith("LINE"):
            text = text.split("LINE", 1)[1]
        return int(text)

    def _build_production_plan_payload(self, plan_request: Any) -> Dict[str, Any]:
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

            production_plan[line_id] = groups
            for field in match_fields:
                production_plan_attrs[field][line_id] = attrs_by_field[field]

        self._plan_id_to_line = plan_id_to_line
        return {
            "production_plan": production_plan,
            "production_plan_attrs": production_plan_attrs if match_fields else {},
        }

    def _normalize_production_line_current_group(self, current_group_map: Any) -> Dict[int, int]:
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
        for line, core_group_idx in self._normalize_production_line_current_group(current_group_map).items():
            self._core.production_line_current_group[line] = core_group_idx
            self._core.production_line_completed_tasks.setdefault(line, set())
            self._core.production_line_group_completion_times.setdefault(line, [])
            print(
                f"[WarehouseService] 产线 {line} 当前组同步为 core={core_group_idx}, public={core_group_idx + 1}"
            )

    def _normalize_current_groups(self, current_groups: Any) -> Dict[int, int]:
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
            if public_group_int < 1:
                raise ValueError("currentGroups uses public 1-based group numbers; values must be >= 1")
            core_group_idx = public_group_int - 1
            total_groups = len((getattr(self._core, "production_plan", {}) or {}).get(line, []))
            if total_groups and core_group_idx > total_groups:
                raise ValueError(
                    f"currentGroups line {line} points to public group {public_group_int}, "
                    f"but the plan only has {total_groups} groups"
                )
            normalized[line] = core_group_idx
        return normalized

    def apply_current_groups(self, current_groups: Any) -> None:
        for line, core_group_idx in self._normalize_current_groups(current_groups).items():
            self._core.production_line_current_group[line] = core_group_idx
            self._core.production_line_completed_tasks.setdefault(line, set())
            self._core.production_line_group_completion_times.setdefault(line, [])
            print(
                f"[WarehouseService] 产线 {line} 当前组同步为 public={core_group_idx + 1}, core={core_group_idx}"
            )

    def apply_inline_schedule_plan(self, schedule_request: Any) -> bool:
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
            # Inline mixed requests usually send the complete latest plan; preserve ADD merge compatibility.
            replace_existing = operation_value != "ADD"
            if not self.set_production_plan(payload, update=replace_existing):
                return False

        if current_groups is not None:
            self.apply_current_groups(current_groups)
        elif production_line_current_group:
            self.apply_production_line_current_group(production_line_current_group)

        return True

    def _build_bom_snapshot(self) -> Dict[str, Any]:
        config_data = getattr(self._core, "config_data", {}) or {}
        return {
            "sku_types": list(deepcopy(config_data.get("sku_types", []) or [])),
            "sku_to_production_line": dict(deepcopy(config_data.get("sku_to_production_line", {}) or {})),
        }

    def _get_known_sku_ids(self) -> set:
        static_config = getattr(self, "_bom_config_snapshot", {}) or {}
        known = set(static_config.get("sku_types", []) or [])
        known.update((static_config.get("sku_to_production_line", {}) or {}).keys())
        return known

    def find_invalid_skus(self, tasks: List[Any], task_types: Optional[List[str]] = None) -> Dict[str, List[str]]:
        known_skus = self._get_known_sku_ids()
        invalid_skus: set = set()
        task_ids: List[str] = []
        task_types_upper = {str(t).upper() for t in task_types} if task_types else None

        for task in tasks:
            task_id = task.taskId if hasattr(task, "taskId") else task.get("taskId")
            if hasattr(task, "taskType"):
                task_type = getattr(task, "taskType")
            elif isinstance(task, dict):
                task_type = task.get("taskType")
            else:
                task_type = None

            task_type_value = getattr(task_type, "value", task_type)
            if task_types_upper is not None and task_type_value is not None and str(task_type_value).upper() not in task_types_upper:
                continue
            skus = task.skus if hasattr(task, "skus") else task.get("skus", [])
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
    
    def _external_to_internal_row(self, external_row: int, aisle: int) -> int:
        """
        将外部 API 的 row 编号转换为内部 row 编号
        外部 row = 2 * (aisle - 1) + 内部 row
        因此：内部 row = (外部 row - 1) % 2 + 1
        
        Args:
            external_row: 外部 row 编号 (1-10 for 5 aisles)
            aisle: 巷道编号 (1-5)
        
        Returns:
            内部 row 编号 (1-2)
        """
        internal_row = (external_row - 1) % 2 + 1
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
    
    def _sync_time(self):
        """同步仿真时间"""
        current = self._get_current_time()
        self._core.current_time = current
        self._last_sync_time = time.time()
    
    # ============================================================
    # 状态同步方法
    # ============================================================
    
    # 存储巷道可用性状态（用于调度决策）
    _aisle_availability: Dict[int, Dict[str, Any]] = {}
    
    def sync_aisle_status(self, aisle_status_list: List[Any]) -> None:
        """
        同步巷道状态到warehouse_core
        
        每次mixed调度请求时调用，根据外部系统提供的状态更新内部状态。
        
        更新的参数：
        - blockage_status: 各巷道*产线的拥堵状态
        - _aisle_availability: 巷道可用性（维护/故障/占用）
        
        Args:
            aisle_status_list: API请求中的巷道状态列表
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
                        unblock_time=self._core.current_time + self._core.outbound_congestion_time  # 由外部反馈解除
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
                    # 设置为拥堵状态，解除时间设为无穷大（由外部反馈解除）
                    self._core.update_blockage_status(
                        aisle=aisle_id,
                        production_line=line_id,
                        blocked=True,
                        unblock_time=self._core.current_time + self._core.outbound_congestion_time
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
        """
        同步库存状态到warehouse_core（自动判断全量/增量模式）
        
        根据 inventory_list 的数量自动判断更新模式：
        - 数量 >= 15：全量重置模式，清空所有库存后重新设置
        - 数量 < 15：增量更新模式，只更新提供的货位
        - 数量 = 0：保持原有库存状态不变
        
        更新的参数：
        - inventory_manager.current_inventory: 各巷道各SKU的数量统计
        - inventory_manager.inventory_positions: 每个货位的详细状态
        - inventory_manager.sku_position_index: SKU到货位的索引
        
        Args:
            inventory_list: API请求中的库存信息列表
        """
        self._sync_time()
        
        # 如果没有提供库存数据，跳过更新，保持原有库存状态
        if not inventory_list:
            print(f"[WarehouseService] 库存数据为空，保持原有库存状态")
            return
        
        # 判断是全量重置还是增量更新
        is_full_reset = len(inventory_list) >= 15
        
        if is_full_reset:
            print(f"[WarehouseService] 全量重置库存状态，共 {len(inventory_list)} 条记录")
            # 清空所有库存
            self._clear_all_inventory()
        else:
            print(f"[WarehouseService] 增量更新库存状态，共 {len(inventory_list)} 条记录")
        
        # 增量更新：只更新API提供的货位
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
            
            if position is None:
                print(f"[WarehouseService] 警告: 货位 {position_id} 不存在，跳过")
                continue
            
            # 更新货位状态
            # IMPORTANT: incremental sync must support clearing (moves/relocation/adjustments).
            # For the touched slot+shelf we treat the payload as the source of truth.
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
                        position.upper_sku = sku_id
                        position.upper_quantity = quantity
                        position.upper_attrs = sku_attrs
                    elif "LOWER" in shelf_str:
                        position.lower_sku = sku_id
                        position.lower_quantity = quantity
                        position.lower_attrs = sku_attrs
                    else:
                        # 未指定层，默认放上层
                        if position.upper_quantity == 0:
                            position.upper_sku = sku_id
                            position.upper_quantity = quantity
                            position.upper_attrs = sku_attrs
                        else:
                            position.lower_sku = sku_id
                            position.lower_quantity = quantity
                            position.lower_attrs = sku_attrs
                else:
                    position.sku = sku_id
                    position.quantity = quantity
                    position.sku_attrs = sku_attrs
                
                # 更新current_inventory统计（动态添加新SKU）
                
                # 更新SKU位置索引（动态添加新SKU）
                
                # 如果是新SKU，添加到sku_types列表
        
        # 输出同步结果统计
        # Rebuild derived indexes/counters based on authoritative slot state.
        self._rebuild_inventory_indexes()

        total_beams = sum(
            sum(skus.values()) 
            for skus in self._core.inventory_manager.current_inventory.values()
        )
        mode = "全量重置" if is_full_reset else "增量更新"
        print(f"[WarehouseService] 库存同步完成 ({mode})，总梁数: {total_beams}")
    
    def _rebuild_inventory_indexes(self) -> None:
        """
        Rebuild `current_inventory` and `sku_position_index` from the authoritative slot state.

        This makes incremental inventory updates support clears/moves while keeping counters consistent.
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

        # Ensure sku_types contains any dynamically introduced SKU IDs.
        for sku_id_s in sorted(seen_skus):
            if sku_id_s not in self._core.sku_types:
                self._core.sku_types.append(sku_id_s)
            if sku_id_s not in self._core.inventory_manager.sku_types:
                self._core.inventory_manager.sku_types.append(sku_id_s)

    def _clear_all_inventory(self) -> None:
        """
        清空所有库存数据和相关任务状态
        
        在全量重置时，需要清理:
        - 所有货位的库存
        - 库存统计数据
        - 所有任务队列（running_tasks, pending_*, completed_tasks）
        - 待执行任务缓存
        - 巷道当前位置
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
        
        # 3. 清空所有任务队列
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
        
        print(f"[WarehouseService] 库存和任务状态清空完成")
    
    def is_aisle_available(self, aisle_id: int) -> bool:
        """检查巷道是否可用（基于外部同步的状态）"""
        status = self._aisle_availability.get(aisle_id, {})
        return status.get('is_available', True)
    
    # ============================================================
    # 任务转换方法
    # ============================================================
    
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
            task_id = task.taskId if hasattr(task, 'taskId') else task['taskId']
            task_type = task.taskType if hasattr(task, 'taskType') else task['taskType']
            skus = task.skus if hasattr(task, 'skus') else task['skus']
            
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
                target_aisle = task.targetAisle if hasattr(task, 'targetAisle') else task.get('targetAisle')
                inbound_urgent = task.inboundUrgent if hasattr(task, 'inboundUrgent') else task.get('inboundUrgent', False)
                
                task_data = TaskData(
                    task_id=task_id,
                    task_type=TASK_TYPE_INBOUND,
                    task_name=task_id,
                    skus=sku_list,
                    assigned_aisle=int(target_aisle) if target_aisle else None,
                )
                inbound_tasks.append(task_data)
                
            else:  # OUTBOUND
                # 出库任务
                plan_id = task.planId if hasattr(task, 'planId') else task.get('planId')
                plan_index = task.planIndex if hasattr(task, 'planIndex') else task.get('planIndex')
                
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
                
                task_data = TaskData(
                    task_id=task_id,
                    task_type=TASK_TYPE_OUTBOUND,
                    task_name=task_id,
                    skus=sku_list,
                    production_line=production_line or 1,
                )
                # 存储额外信息
                task_data.plan_id = plan_id
                task_data.plan_index_public = plan_index
                task_data.group_idx = int(plan_index) - 1 if plan_index is not None else None
                
                outbound_tasks.append(task_data)
        
        return inbound_tasks, outbound_tasks
    
    # ============================================================
    # 核心业务方法
    # ============================================================
    
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
        """
        执行混合调度，并返回“匹配预览”信息。

        Returns:
            (result, matched_by_aisle)
            - result: {aisle_id: assigned_task_or_running_task_or_none}
            - matched_by_aisle: {aisle_id: [matched_task, ...]} (includes tasks that are matched but not dispatchable yet)
        """
        self._sync_time()
        inbound_tasks, outbound_tasks = tasks
        
        print(f"[WarehouseService] 执行调度决策，入库任务: {len(inbound_tasks)}，出库任务: {len(outbound_tasks)}")
        
        # 1. 为出库任务查找库存货位并设置positions 
        task_with_positions: List[TaskData] = []
        matched_by_aisle: Dict[int, List[TaskData]] = {}
        for task in outbound_tasks:
            if not getattr(task, 'positions', None):
                positions = self._find_positions_for_outbound_task(task)
                if not positions:
                    print(f"[WarehouseService] 警告: 出库任务 {task.task_id} 未找到匹配的库存货位")
                else:
                    task.positions = positions
                    task_with_positions.append(task)
                    try:
                        aisle = int(getattr(positions[0], "aisle", 0) or 0)
                    except Exception:
                        aisle = 0
                    if aisle:
                        matched_by_aisle.setdefault(aisle, []).append(task)
        
        # 2. 将入库任务添加到等待队列（只添加到可用巷道）
        for task in inbound_tasks:
            if task.assigned_aisle:
                aisle = task.assigned_aisle
                # 检查巷道是否可用
                if not self.is_aisle_available(aisle):
                    print(f"[WarehouseService] 入库任务 {task.task_id} 的目标巷道 {aisle} 不可用，跳过")
                    continue
                if aisle in self._core.pending_inbound_by_aisle:
                    # 避免重复添加
                    existing_ids = {t.task_id for t in self._core.pending_inbound_by_aisle[aisle]}
                    if task.task_id not in existing_ids:
                        self._core.pending_inbound_by_aisle[aisle].append(task)
                        print(f"[WarehouseService] 入库任务 {task.task_id} 添加到巷道 {aisle} 等待队列")
        
        # 3. 将出库任务添加到等待队列
        for task in task_with_positions:
            existing_ids = {t.task_id for t in self._core.pending_outbound_queue}
            if task.task_id not in existing_ids:
                self._core.pending_outbound_queue.append(task)
                print(f"[WarehouseService] 出库任务 {task.task_id} 添加到出库队列")
        
        # 4. 使用调度器进行调度决策
        # 收集待调度任务
        inbound_for_schedule: List[TaskData] = []
        for aisle in self._core.aisles:
            line_buckets = {}
            for t in self._core.pending_inbound_by_aisle.get(aisle, []):
                line = getattr(t, "in_line", 1)
                if line not in line_buckets:
                    line_buckets[line] = t
            inbound_for_schedule.extend(line_buckets.values())
        
        outbound_for_schedule: List[TaskData] = list(self._core.pending_outbound_queue)
        
        # 调用调度器
        aisle_task_sequences = self._core.scheduler.solve(
            inbound_tasks=inbound_for_schedule,
            outbound_tasks=outbound_for_schedule,
            running_tasks=self._core.running_tasks,
            current_time=self._core.current_time,
        )
        
        # 5. 构建返回结果，并将分配的任务保存到待执行队列
        result: Dict[int, Optional[TaskData]] = {}
        
        # 获取当前忙碌的巷道
        
        for aisle in self._core.aisles:
            # 检查巷道可用性
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
            
            # 检查巷道是否忙碌
            
            # 获取调度器分配给该巷道的任务
            sequence = aisle_task_sequences.get(aisle, [])
            used_fallback = False
            if sequence and all(t.task_id in self._core.running_tasks for t in sequence):
                for pending_task in task_with_positions:
                    positions = getattr(pending_task, "positions", None) or []
                    if positions and str(positions[0].aisle) == str(aisle):
                        sequence = [pending_task]
                        used_fallback = True
                        break
            if not sequence:
                fallback_task = None
                for pending_task in task_with_positions:
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
                # 出库任务的约束条件
                if candidate.task_type == TASK_TYPE_OUTBOUND and candidate.production_line is not None:
                    # 阻塞状态
                    if self._core.check_blockage(aisle, candidate.production_line, current_time=self._core.current_time):
                        continue
                    # 组顺序约束
                    if not self._core.can_start_outbound_task(
                        candidate.task_id,
                        candidate.production_line,
                        getattr(candidate, "group_idx", None),
                    ):
                        continue

                # 确保任务有positions
                if not getattr(candidate, 'positions', None):
                    continue

                task = candidate
                break

            if task is None:
                result[aisle] = None
                continue
            
            # 设置任务的巷道
            task.assigned_aisle = aisle
            
            # 生成任务记录
            task.task_record = self._core.generate_task_record(task, self._core.current_time)
            
            # 保存到待执行队列（等待EXECUTING反馈）
            self._pending_execution_tasks[task.task_id] = task
            
            result[aisle] = task
            print(f"[WarehouseService] 任务 {task.task_id} 分配到巷道 {aisle}（等待EXECUTING反馈）")
        
        # 输出调度结果统计
        assigned_count = sum(1 for t in result.values() if t is not None)
        print(f"[WarehouseService] 调度完成，分配了 {assigned_count} 个任务")

        return result, matched_by_aisle
    
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
            # 从skus字段提取
            for s in (task.skus or []):
                if isinstance(s, dict):
                    sid = s.get('skuId')
                else:
                    sid = getattr(s, 'skuId', None)
                if sid:
                    sku_ids.append(sid)
        
        if not sku_ids:
            print(f"[WarehouseService] 任务 {task.task_id} 没有SKU信息")
            return None

        sku_attrs_map: Dict[str, Dict[str, Any]] = {}
        for s in (task.skus or []):
            sku_dict = self._sku_entry_to_dict(s)
            sku_id = sku_dict.get('skuId')
            if sku_id and sku_id not in sku_attrs_map:
                sku_attrs_map[sku_id] = self._extract_sku_attrs(sku_dict)
        
        production_line = task.production_line or 1
        print(f"[WarehouseService] 为任务 {task.task_id} 查找货位, SKUs: {sku_ids}, 产线: {production_line}")
        
        if len(sku_ids) == 1:
            # 单梁任务：查找包含该SKU的位置
            sku = sku_ids[0]
            sku_attrs = sku_attrs_map.get(sku, {})
            positions = [
                p for p in self._core.inventory_manager.get_sku_positions(sku, only_available=True)
                if p.matches_sku(sku, sku_attrs, self._core.match_fields)
            ]
            print(f"[WarehouseService] SKU {sku} 找到 {len(positions)} 个可用位置")
            
            for pos in positions:
                print(f"[WarehouseService] 选择位置: {pos.get_position_id()}")
                return [pos]
        
        elif len(sku_ids) == 2:
            sku1, sku2 = sku_ids
            attrs1 = sku_attrs_map.get(sku1, {})
            attrs2 = sku_attrs_map.get(sku2, {})
            
            # 1. 优先查找同时包含两个SKU的双层位置（已配对）
            for pos in self._core.inventory_manager.inventory_positions:
                if (pos.is_double_layer
                    and pos.matches_pair(sku1, attrs1, sku2, attrs2, self._core.match_fields)
                    and pos.upper_quantity > 0
                    and pos.lower_quantity > 0):
                    print(f"[WarehouseService] 找到配对位置: {pos.get_position_id()}")
                    return [pos]
                        
            print(f"[WarehouseService] 任务 {task.task_id} 未找到任何可用货位")
        return None
    
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
        """
        应用任务执行反馈
        
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
        """
        self._sync_time()
        
        try:
            task_id = feedback.get('taskId')
            status = feedback.get('status', '').upper()
            task_type = feedback.get('taskType', '').upper()
            
            if not task_id:
                print(f"[WarehouseService] 反馈缺少taskId")
                return False
            
            print(f"[WarehouseService] 处理任务反馈: task_id={task_id}, status={status}, type={task_type}")
            
            # 调用core的反馈记录方法（用于记录）
            self._core.apply_task_feedback(feedback)
            
            if status == 'EXECUTING':
                # EXECUTING状态：将任务移入running_tasks
                return self._start_task_execution(task_id, task_type)
                
            elif status == 'COMPLETED':
                # COMPLETED状态：完成任务
                return self._complete_task_execution(task_id, task_type)
                
            elif status == 'FAILED':
                # FAILED状态：任务失败
                return self._fail_task_execution(task_id, feedback.get('reason', 'Unknown'))
            
            return True
            
        except Exception as e:
            import traceback
            print(f"[WarehouseService] 应用反馈失败: {e}")
            traceback.print_exc()
            return False
    
    def _start_task_execution(self, task_id: str, task_type: str) -> bool:
        """
        开始执行任务（收到EXECUTING反馈后调用）
        
        将任务从待执行队列移入running_tasks
        
        Args:
            task_id: 任务ID
            task_type: 任务类型
            
        Returns:
            是否成功
        """
        # 1. 先从待执行队列查找任务
        task = self._pending_execution_tasks.pop(task_id, None)
        
        if task is None:
            # 2. 尝试从pending队列查找
            if task_type == 'OUTBOUND':
                for t in self._core.pending_outbound_queue:
                    if t.task_id == task_id:
                        task = t
                        break
            else:  # INBOUND
                for aisle, queue in self._core.pending_inbound_by_aisle.items():
                    for t in queue:
                        if t.task_id == task_id:
                            task = t
                            break
                    if task:
                        break
        
        if task is None:
            # 3. 检查是否已在running_tasks中（重复反馈）
            if task_id in self._core.running_tasks:
                print(f"[WarehouseService] 任务 {task_id} 已在执行中（重复EXECUTING反馈）")
                return True
            print(f"[WarehouseService] 未找到任务 {task_id}，无法开始执行")
            return False
        
        # 确保任务有task_record
        if not getattr(task, 'task_record', None):
            task.task_record = self._core.generate_task_record(task, self._core.current_time)
        
        # 添加到running_tasks
        self._core.running_tasks[task_id] = task
        print(f"[WarehouseService] 任务 {task_id} 已移入running_tasks开始执行")
        
        # 从等待队列移除
        if task.task_type == TASK_TYPE_OUTBOUND:
            self._core.pending_outbound_queue = [
                t for t in self._core.pending_outbound_queue if t.task_id != task_id
            ]
        else:  # INBOUND
            aisle = task.assigned_aisle
            if aisle and aisle in self._core.pending_inbound_by_aisle:
                self._core.pending_inbound_by_aisle[aisle] = [
                    t for t in self._core.pending_inbound_by_aisle[aisle] if t.task_id != task_id
                ]
        
        return True
    
    def _complete_task_execution(self, task_id: str, task_type: str) -> bool:
        """
        完成任务执行（收到COMPLETED反馈后调用）
        
        - 从running_tasks移除
        - 更新库存（出库扣减/入库增加）
        - 更新生产计划进度（出库任务）
        - 更新拥堵状态
        
        Args:
            task_id: 任务ID
            task_type: 任务类型
            
        Returns:
            是否成功
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
                
                for idx, sku in enumerate(sku_ids_list):
                    pos = task.positions[min(idx, len(task.positions) - 1)]
                    try:
                        self._core.inventory_manager.remove_inventory(pos, sku, 1)
                        print(f"[WarehouseService] 出库扣减库存: SKU {sku} 从位置 {pos.get_position_id()}")
                    except Exception as e:
                        print(f"[WarehouseService] 扣减库存失败: {sku}, 错误: {e}")
            
            # 2. 更新拥堵状态
            if production_line is not None and aisle is not None:
                # 设置拥堵，一段时间后自动解除
                outbound_congestion_time = getattr(self._core, 'outbound_congestion_time', 5.0)
                outbound_finish_time = self._core.current_time + outbound_congestion_time
                self._core.update_blockage_status(
                    aisle, production_line, 
                    blocked=True, 
                    unblock_time=outbound_finish_time
                )
                print(f"[WarehouseService] 设置巷道 {aisle} 产线 {production_line} 拥堵直到 {outbound_finish_time:.2f}s")
            
            # 3. 标记生产计划进度
            if production_line is not None:
                self._core.mark_outbound_completed(production_line, task, self._core.current_time)
                current_group = self._core.production_line_current_group.get(production_line, 0)
                print(f"[WarehouseService] 产线 {production_line} 当前组索引: {current_group}")
        
        else:  # INBOUND
            # 入库任务完成
            # 增加库存（如果任务有位置信息）
            if getattr(task, 'positions', None) and task.skus:
                for idx, sku_info in enumerate(task.skus):
                    sku_dict = self._sku_entry_to_dict(sku_info)
                    sku_id = sku_dict.get('skuId')
                    sku_attrs = self._extract_sku_attrs(sku_dict)
                    if not sku_id:
                        continue
                    pos = task.positions[min(idx, len(task.positions) - 1)]
                    try:
                        self._core.inventory_manager.add_inventory(pos, sku_id, 1, attrs=sku_attrs)
                        print(f"[WarehouseService] 入库增加库存: SKU {sku_id} 到位置 {pos.get_position_id()}")
                    except Exception as e:
                        print(f"[WarehouseService] 增加库存失败: {sku_id}, 错误: {e}")
        
        # 从running_tasks移除
        del self._core.running_tasks[task_id]
        
        # 添加到已完成列表
        self._core.completed_tasks.append(task)
        
        # 更新巷道当前位置
        if getattr(task, 'positions', None) and aisle is not None:
            self._core.current_position_by_aisle[aisle] = task.positions[-1]
        
        print(f"[WarehouseService] 任务 {task_id} 已完成并从running_tasks移除")
        return True
    
    def _fail_task_execution(self, task_id: str, reason: str) -> bool:
        """
        处理任务失败
        
        完整的状态清理包括:
        - 从 running_tasks 移除
        - 从 pending_outbound_queue / pending_inbound_by_aisle 移除
        - 从 _pending_execution_tasks 移除
        - 清理巷道拥堵状态（如果任务导致了拥堵）
        
        Args:
            task_id: 任务ID
            reason: 失败原因
            
        Returns:
            是否成功处理
        """
        # 从待执行队列移除
        self._pending_execution_tasks.pop(task_id, None)
        
        # 从running_tasks移除（如果存在）
        task = self._core.running_tasks.pop(task_id, None)
        
        if task:
            aisle = task.assigned_aisle
            production_line = task.production_line
            
            # 从pending队列移除（以防任务还在队列中）
            if task.task_type == TASK_TYPE_OUTBOUND:
                self._core.pending_outbound_queue = [
                    t for t in self._core.pending_outbound_queue if t.task_id != task_id
                ]
            else:  # INBOUND
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
            if not update and isinstance(production_plan, dict) and "production_plan" in production_plan:
                merged_plan = {
                    int(line_id): deepcopy(groups)
                    for line_id, groups in (getattr(self._core, "production_plan", {}) or {}).items()
                    if groups
                }
                for line_id, groups in (production_plan.get("production_plan") or {}).items():
                    merged_plan[int(line_id)] = groups

                merged_attrs = deepcopy(getattr(self._core, "production_plan_attrs", {}) or {})
                for field, by_line in (production_plan.get("production_plan_attrs") or {}).items():
                    merged_attrs.setdefault(field, {})
                    for line_id, attrs in by_line.items():
                        merged_attrs[field][int(line_id)] = attrs

                production_plan = {
                    "production_plan": merged_plan,
                    "production_plan_attrs": merged_attrs,
                }
            self._core.set_production_plan(production_plan)
            return True
        except Exception as e:
            print(f"[WarehouseService] 设置生产计划失败: {e}")
            return False
    
    def get_production_plan(self) -> Dict[int, List]:
        """获取当前生产计划"""
        return self._core.production_plan
    
    # ============================================================
    # 状态查询方法
    # ============================================================
    
    def get_running_tasks(self) -> Dict[str, TaskData]:
        """获取正在执行的任务"""
        return self._core.running_tasks.copy()
    
    def get_pending_tasks(self) -> Dict[str, List[TaskData]]:
        """获取等待中的任务"""
        return {
            'inbound': {
                aisle: list(tasks) 
                for aisle, tasks in self._core.pending_inbound_by_aisle.items()
            },
            'outbound': list(self._core.pending_outbound_queue)
        }

    def get_last_matched_tasks_by_aisle(self) -> Dict[int, List[TaskData]]:
        """获取上一次调度请求的匹配预览结果（仅用于API返回）。"""
        return {aisle: list(tasks) for aisle, tasks in (self._last_matched_tasks_by_aisle or {}).items()}
    
    def get_completed_tasks(self) -> List[TaskData]:
        """获取已完成的任务"""
        return list(self._core.completed_tasks)
    
    def get_inventory_summary(self) -> Dict[int, Dict[str, int]]:
        """获取库存摘要（过滤数量为0的SKU）"""
        summary: Dict[int, Dict[str, int]] = {}
        for aisle, skus in self._core.inventory_manager.current_inventory.items():
            summary[aisle] = {sku: qty for sku, qty in skus.items() if qty > 0}
        return summary

    def get_full_inventory(self) -> List[Dict[str, Any]]:
        """获取全量库存列表（与inventory请求结构一致）"""
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
                if match_fields and upper_sku and upper_qty > 0:
                    upper_attrs = getattr(position, "upper_attrs", {}) or {}
                    for field in match_fields:
                        if field in upper_attrs:
                            upper_entry[field] = upper_attrs.get(field)
                if match_fields and lower_sku and lower_qty > 0:
                    lower_attrs = getattr(position, "lower_attrs", {}) or {}
                    for field in match_fields:
                        if field in lower_attrs:
                            lower_entry[field] = lower_attrs.get(field)
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
                if match_fields and sku and qty > 0:
                    sku_attrs = getattr(position, "sku_attrs", {}) or {}
                    for field in match_fields:
                        if field in sku_attrs:
                            entry[field] = sku_attrs.get(field)
                full_inventory.append({
                    **base_info,
                    "shelf": None,
                    "positions": [entry],
                })

        return full_inventory
    
    def get_aisle_status(self) -> Dict[int, Dict]:
        """获取巷道状态"""
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
                'current_position': self._core.current_position_by_aisle.get(aisle)
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
        2. 将配置数据写入 simulation/data/sku_config.json
        3. 重新加载 WarehouseCore 的 SKU 配置属性
        """
        try:
            # 验证必填字段
            required_fields = ['sku_types', 'sku_pairs', 'sku_solo', 'sku_to_production_line']
            for field in required_fields:
                if field not in config_data:
                    raise ValueError(f"缺少必填字段: {field}")
            
            # 写入配置文件
            config_path = Path(project_root) / "simulation" / "data" / "sku_config.json"
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump(config_data, f, ensure_ascii=False, indent=2)
            
            # 重新加载 WarehouseCore 的 SKU 配置
            self._core.config_data = config_data
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
    """获取全局仓库服务实例（用于FastAPI依赖注入）"""
    global _warehouse_service
    if _warehouse_service is None:
        _warehouse_service = WarehouseService()
    return _warehouse_service


def init_warehouse_service(warehouse_core: Optional[WarehouseCore] = None) -> WarehouseService:
    """初始化仓库服务（可选传入已有的WarehouseCore）"""
    global _warehouse_service
    _warehouse_service = WarehouseService(warehouse_core)
    return _warehouse_service


def reset_warehouse_service():
    """重置仓库服务（用于测试）"""
    global _warehouse_service
    _warehouse_service = None
