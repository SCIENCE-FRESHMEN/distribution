"""承载 API 与仓库仿真核心之间的状态同步、调度和反馈业务逻辑。"""


from __future__ import annotations

import json
import random
import re
import sys
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from fastapi import Request
from simulation.position import InventoryPosition
from simulation.task_data import TASK_TYPE_INBOUND, TASK_TYPE_OUTBOUND, TaskData
from simulation.warehouse_core import WarehouseCore

project_root = Path(__file__).parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))


class WarehouseService:
    _aisle_availability: Dict[int, Dict[str, Any]] = {}

    def __init__(self, warehouse_core: Optional[WarehouseCore] = None):
        """初始化 API 的状态适配层和与 Core 共享的待执行任务缓存。

        Args:
            warehouse_core: 已初始化的核心对象；缺省时按 API 默认策略创建并初始化。

        Returns:
            None: 服务通过 ``_core``、时间戳和计划映射字段保存运行状态。
        """
        if warehouse_core is None:
            self._core = WarehouseCore(
                scheduler_type="optimization",
                inbound_aisle_strategy="proposed",
                inbound_allocation_strategy="proposed",
                config_path="config/warehouse.json",
            )
            # API 只建立空货位结构；不能生成仿真随机库存，也不输出仿真初始化统计。
            self._core.initialize(populate_initial_inventory=False, log_initialization=False)
        else:
            self._core = warehouse_core

        # API 服务启动时间；Core 的 API 模式 current_time 由相对该时间的秒数表示。
        self._start_time = time.time()
        # 最近一次库存/状态同步结束的墙钟时间，用于诊断外部状态是否过期。
        self._last_sync_time = self._start_time
        # 已推荐并等待 EXECUTING 反馈的任务；与 Core 共享同一对象，避免重复占用货位。
        self._pending_execution_tasks: Dict[str, TaskData] = {}
        self._core.pending_execution_tasks = self._pending_execution_tasks
        # ADD 计划中 (planId, 外部 planIndex) 到合并后内部组号的偏移映射。
        self._add_plan_index_alias: Dict[Tuple[str, int], int] = {}
        # 外部 planId 到实际 lineId 的映射，优先于任务 ID 或 SKU 推断产线。
        self._plan_id_to_line_id: Dict[str, int] = {}

    # ==========================================================================
    # 主函数：mixed 调度提交与巷道推荐
    # ==========================================================================
    def execute_schedule(
        self,
        tasks: Tuple[List[TaskData], List[TaskData]],
        frozen_tasks: Optional[Dict[int, TaskData]] = None,
    ) -> Tuple[Dict[int, Optional[TaskData]], List[Dict[str, Any]]]:
        """将本次 mixed 任务入队并生成各巷道的推荐任务。

        Args:
            self: 当前对象实例。
            tasks: 转换后的入库任务和出库任务。
            frozen_tasks: 已下发但尚未确认执行的巷道任务；这些任务优先原样返回。

        Returns:
            Tuple[Dict[int, Optional[TaskData]], List[Dict[str, Any]]]: 巷道推荐结果和未提交出库任务。
        """
        self._sync_time()
        # inbound_tasks / outbound_tasks 是本次 mixed 请求转换后的两类任务。
        inbound_tasks, outbound_tasks = tasks
        # frozen_tasks: 巷道 -> 已下发未确认任务；本轮必须原样优先返回，不能被新推荐覆盖。
        frozen_tasks = {int(aisle): task for aisle, task in (frozen_tasks or {}).items() if task is not None}
        # ready_outbound 可提交 pending；unsubmitted_outbound 为库存或组约束拦截的出库任务说明。
        ready_outbound, unsubmitted_outbound = self._prepare_outbound_tasks_for_submission(outbound_tasks)

        # 入库任务进入所属巷道队列前，先验证目标巷道/入库口并固化货位。
        for task in inbound_tasks:
            if not task.assigned_aisle:
                continue
            # aisle / in_line / production_line 是任务当前分配上下文；explicit_target_aisle 决定非法时是否允许回退。
            aisle = int(task.assigned_aisle)
            in_line = getattr(task, "in_line", None)
            production_line = getattr(task, "production_line", None)
            explicit_target_aisle = bool((getattr(task, "task_record", {}) or {}).get("target_aisle_explicit"))
            # valid_aisles 是排除禁配规则后的合法入库巷道。
            valid_aisles = self._core._get_valid_inbound_aisles(task, production_line)
            if aisle not in valid_aisles:
                # 显式指定巷道必须严格失败；系统推荐巷道失效时才允许回退到其他合法巷道。
                if explicit_target_aisle:
                    raise ValueError(
                        f"任务 {task.task_id} 指定的 targetAisle={aisle} 不允许当前货物入库。"
                    )
                candidates = [a for a in valid_aisles if self.is_inbound_path_available(a, in_line)]
                if candidates:
                    aisle = min(candidates, key=lambda a: len(self._core.pending_inbound_by_aisle.get(a, [])))
                    task.assigned_aisle = aisle
                else:
                    continue
            if not self.is_inbound_path_available(aisle, in_line):
                if explicit_target_aisle:
                    raise ValueError(
                        f"任务 {task.task_id} 指定的 targetAisle={aisle} 当前不可入库。"
                    )
                continue
            if not getattr(task, "positions", None):
                allocated = self._allocate_feedback_positions_for_aisle(task, aisle)
                if not allocated:
                    continue
                task.positions = list(allocated)
            existing_ids = {t.task_id for t in self._core.pending_inbound_by_aisle.get(aisle, [])}
            if task.task_id not in existing_ids:
                self._core.pending_inbound_by_aisle[aisle].append(task)

        # 空滑橇高优先级出库任务插入队首，其他出库任务维持到达顺序。
        existing_outbound = {t.task_id for t in self._core.pending_outbound_queue}
        normal_tasks: List[TaskData] = []
        priority_tasks: List[TaskData] = []
        for task in ready_outbound:
            if task.task_id in existing_outbound:
                continue
            rec = getattr(task, "task_record", {}) or {}
            if bool(rec.get("high_priority")) and bool(rec.get("empty_skid_request")):
                priority_tasks.append(task)
            else:
                normal_tasks.append(task)
        if priority_tasks:
            self._core.pending_outbound_queue = priority_tasks + self._core.pending_outbound_queue
        if normal_tasks:
            self._core.pending_outbound_queue.extend(normal_tasks)

        inbound_for_schedule: List[TaskData] = []
        # 每条入库线只取队首参加本拍调度，防止同一入库口并行下发多个任务。
        for aisle in self._core.aisles:
            line_buckets: Dict[int, TaskData] = {}
            for t in self._core.pending_inbound_by_aisle.get(aisle, []):
                line = getattr(t, "in_line", 1)
                if line not in line_buckets:
                    line_buckets[line] = t
            inbound_for_schedule.extend(line_buckets.values())

        aisle_task_sequences = self._core.scheduler.solve(
            inbound_tasks=inbound_for_schedule,
            outbound_tasks=self._get_schedulable_outbound_heads(),
            running_tasks=self._core.running_tasks,
            current_time=self._core.current_time,
        )

        result: Dict[int, Optional[TaskData]] = {}
        busy_aisles = {t.assigned_aisle for t in self._core.running_tasks.values() if getattr(t, "assigned_aisle", None)}
        busy_aisles.update(frozen_tasks.keys())
        # 组装返回时，running 和冻结任务优先于本轮新推荐，确保 repeated mixed 不会顶替已下发任务。
        for aisle in self._core.aisles:
            if not self.is_aisle_available(aisle):
                result[aisle] = None
                continue
            if aisle in frozen_tasks:
                result[aisle] = frozen_tasks[aisle]
                continue
            if aisle in busy_aisles:
                result[aisle] = next((t for t in self._core.running_tasks.values() if t.assigned_aisle == aisle), None)
                continue

            sequence = aisle_task_sequences.get(aisle, [])
            if not sequence:
                result[aisle] = None
                continue

            task = sequence[0]
            if task.task_type == TASK_TYPE_OUTBOUND and task.production_line is not None:
                out_line = getattr(task, "out_line", None) or task.production_line
                if not self.is_outbound_path_available(aisle, out_line):
                    result[aisle] = None
                    continue
                if self._core.check_blockage(aisle, out_line, current_time=self._core.current_time):
                    result[aisle] = None
                    continue
                if not self._core.can_start_outbound_task(
                    task.task_id,
                    task.production_line,
                    task_group_idx=getattr(task, "group_idx", None),
                ):
                    result[aisle] = None
                    continue

            if not getattr(task, "positions", None):
                result[aisle] = None
                continue

            task.assigned_aisle = aisle
            task.task_record = self._core.generate_task_record(task, self._core.current_time)
            self._move_task_to_pending_execution(task)
            result[aisle] = task

        return result, unsubmitted_outbound

    @property
    def core(self) -> WarehouseCore:
        """返回服务所管理的仓库核心对象。

        Returns:
            WarehouseCore: 真实库存、事件队列、生产计划和设备状态的权威来源。
        """
        return self._core

    # ==========================================================================
    # 辅助函数：时间同步、请求归一化与任务查询
    # ==========================================================================
    def _get_current_time(self) -> float:
        """计算 API 服务当前相对仿真时间。

        Returns:
            float: 自服务创建以来经过的秒数。
        """
        return time.time() - self._start_time

    def _sync_time(self) -> None:
        """将 API 相对时间写入 Core，并记录本次同步的墙钟时间。

        Returns:
            None: 修改 ``_core.current_time`` 和 ``_last_sync_time``。
        """
        self._core.current_time = self._get_current_time()
        self._last_sync_time = time.time()

    def _sku_entry_to_dict(self, sku: Any) -> Dict[str, Any]:
        """将 Pydantic、字典或兼容对象统一为内部 SKU 字典。

        Args:
            sku: API 模型、字典或带 skuId/quantity 属性的兼容对象。

        Returns:
            Dict[str, Any]: 含 skuId、quantity 及已归一化 ``features`` 的字典。
        """
        if isinstance(sku, dict):
            d = dict(sku)
        elif hasattr(sku, "model_dump"):
            d = sku.model_dump()
        elif hasattr(sku, "dict"):
            d = sku.dict()
        else:
            d = {"skuId": getattr(sku, "skuId", None), "quantity": getattr(sku, "quantity", 1)}

        raw_features = d.get("features")
        features: Dict[str, Any] = raw_features if isinstance(raw_features, dict) else {}
        # Allow passing features either in `features` object or as sku top-level fields.
        for k, v in d.items():
            # FIFO 时间是 SKU 元数据而非匹配特征；不能把 API 字段名混入 color、rfid
            # 等业务特征字典，否则特征匹配或库存回显会出现无意义字段。
            if k in ("skuId", "quantity", "features", "inboundTime", "arrivalTime"):
                continue
            if v is not None and k not in features:
                features[k] = v
        if features:
            d["features"] = self._normalize_features(features)
        return d

    def _normalize_features(self, features: Any) -> Dict[str, Any]:
        """通过 Core 的别名表统一车身特征键，过滤空值。

        Args:
            features: 原始特征字典，例如 color、rfid、skid_state。

        Returns:
            Dict[str, Any]: 使用内部规范字段名的非空特征字典。
        """
        if not isinstance(features, dict):
            return {}
        normalizer = getattr(self._core, "_normalize_feature_dict", None)
        if callable(normalizer):
            try:
                out = normalizer(features)
                if isinstance(out, dict):
                    return out
            except Exception:
                pass
        return {str(k): v for k, v in features.items() if v is not None}

    def _extract_sku_features(self, sku_dict: Dict[str, Any], feature_keys: Optional[List[str]] = None) -> Dict[str, Any]:
        """提取一个 SKU 中参与匹配的特征，并限制为指定字段集合。

        Args:
            sku_dict: 已归一化或原始 SKU 数据。
            feature_keys: 当前产线要求的特征键；为空时保留嵌套 features 全部字段。

        Returns:
            Dict[str, Any]: 用于库存查询和货位匹配的标准化特征。
        """
        features = sku_dict.get("features")
        if isinstance(features, dict):
            normalized = self._normalize_features(dict(features))
            if feature_keys:
                feature_key_set = {str(k) for k in feature_keys}
                normalized = {k: v for k, v in normalized.items() if k in feature_key_set}
            return normalized
        if not feature_keys:
            return {}
        raw = {k: sku_dict.get(k) for k in feature_keys if sku_dict.get(k) is not None}
        return self._normalize_features(raw)

    def _get_match_fields(self, production_line: Optional[int] = None) -> List[str]:
        """获取匹配 fields相关逻辑。

        Args:
            self: 当前对象实例。
            production_line: 生产线编号。

        Returns:
            List[str]: 处理后的结果。
        """
        return list(self._core._get_outbound_match_features(production_line) or [])

    def _get_outbound_match_features(self, production_line: Optional[int] = None) -> List[str]:
        # 为旧调用方保留兼容别名。
        """获取出库 匹配 features相关逻辑。

        Args:
            self: 当前对象实例。
            production_line: 生产线编号。

        Returns:
            List[str]: 处理后的结果。
        """
        return self._get_match_fields(production_line)

    def _normalize_line_id(self, value: Any) -> Optional[int]:
        """标准化line id相关逻辑。

        Args:
            self: 当前对象实例。
            value: 待处理的单个值。

        Returns:
            Optional[int]: 处理后的结果。
        """
        return self._to_int_or_none(value)

    def _build_core_production_plan(self, production_plan: Any) -> Dict[int, List[Any]]:
        """构建core 生产 计划相关逻辑。

        Args:
            self: 当前对象实例。
            production_plan: 用于本函数处理的 `production_plan` 参数。

        Returns:
            Dict[int, List[Any]]: 处理后的结果。
        """
        plans = production_plan.plans if hasattr(production_plan, "plans") else production_plan.get("plans", [])
        core_plan: Dict[int, List[Any]] = {}

        for plan in plans or []:
            line_id_raw = plan.lineId if hasattr(plan, "lineId") else plan.get("lineId")
            line_id = self._normalize_line_id(line_id_raw)
            if line_id is None:
                continue

            match_fields = list(self._core._get_outbound_match_features(line_id) or [])
            feature_fields = [f for f in match_fields if str(f).lower() != "rfid"]
            groups = []
            plan_groups = plan.planIndex if hasattr(plan, "planIndex") else plan.get("planIndex", [])
            for group in plan_groups or []:
                tasks_in_group = []
                required_skus = group.requiredSkus if hasattr(group, "requiredSkus") else group.get("requiredSkus", [])
                for task_skus in required_skus or []:
                    sku_list = []
                    for sku in task_skus or []:
                        if isinstance(sku, dict):
                            sku_entry = {"skuId": sku.get("skuId")}
                            quantity = sku.get("quantity", 1)
                            raw_features = sku.get("features")
                        else:
                            sku_entry = {"skuId": getattr(sku, "skuId", None)}
                            quantity = getattr(sku, "quantity", 1)
                            raw_features = getattr(sku, "features", None)
                        for _ in range(int(quantity or 0)):
                            item = dict(sku_entry)
                            if feature_fields:
                                features = {}
                                for field in feature_fields:
                                    if isinstance(sku, dict):
                                        value = sku.get(field)
                                    else:
                                        value = getattr(sku, field, None)
                                    if value is not None:
                                        features[field] = value
                                if isinstance(raw_features, dict):
                                    for field in feature_fields:
                                        if field in raw_features and raw_features[field] is not None:
                                            features[field] = raw_features[field]
                                if features:
                                    item["features"] = features
                            sku_list.append(item)
                    tasks_in_group.append(sku_list)
                groups.append(tasks_in_group)
            core_plan[line_id] = groups

        return core_plan

    def _build_add_plan_index_alias(self, production_plan: Any) -> Dict[Tuple[str, int], int]:
        """构建add 计划 index alias相关逻辑。

        Args:
            self: 当前对象实例。
            production_plan: 用于本函数处理的 `production_plan` 参数。

        Returns:
            Dict[Tuple[str, int], int]: 处理后的结果。
        """
        alias: Dict[Tuple[str, int], int] = {}
        plans = production_plan.plans if hasattr(production_plan, "plans") else production_plan.get("plans", [])
        if not plans:
            return alias
        line_offsets: Dict[int, int] = {
            int(line_id): len(groups or [])
            for line_id, groups in (self._core.production_plan or {}).items()
        }
        for plan in plans or []:
            plan_id = plan.planId if hasattr(plan, "planId") else plan.get("planId")
            line_raw = plan.lineId if hasattr(plan, "lineId") else plan.get("lineId")
            line_id = self._normalize_line_id(line_raw)
            if not plan_id or line_id is None:
                continue
            groups = plan.planIndex if hasattr(plan, "planIndex") else plan.get("planIndex", [])
            base = int(line_offsets.get(int(line_id), 0))
            for idx, _ in enumerate(groups or [], start=1):
                alias[(str(plan_id), int(idx))] = base + idx
            line_offsets[int(line_id)] = base + len(groups or [])
        return alias

    def _extract_plan_id_line_mapping(self, production_plan: Any) -> Dict[str, int]:
        """执行 extract 计划 id line mapping 对应的业务处理。

        Args:
            self: 当前对象实例。
            production_plan: 用于本函数处理的 `production_plan` 参数。

        Returns:
            Dict[str, int]: 处理后的结果。
        """
        mapping: Dict[str, int] = {}
        plans = production_plan.plans if hasattr(production_plan, "plans") else production_plan.get("plans", [])
        for plan in plans or []:
            plan_id = plan.planId if hasattr(plan, "planId") else plan.get("planId")
            line_raw = plan.lineId if hasattr(plan, "lineId") else plan.get("lineId")
            line_id = self._normalize_line_id(line_raw)
            if not plan_id or line_id is None:
                continue
            mapping[str(plan_id)] = int(line_id)
        return mapping

    def _normalize_current_groups(
        self,
        current_groups: Any = None,
        legacy_current_groups: Any = None,
    ) -> Optional[Dict[int, int]]:
        """标准化当前 groups相关逻辑。

        Args:
            self: 当前对象实例。
            current_groups: 用于本函数处理的 `current_groups` 参数。
            legacy_current_groups: 用于本函数处理的 `legacy_current_groups` 参数。

        Returns:
            Optional[Dict[int, int]]: 处理后的结果。
        """
        normalized: Dict[int, int] = {}

        if current_groups is not None:
            if isinstance(current_groups, dict):
                rows = [{"lineId": key, "currentGroup": value} for key, value in current_groups.items()]
            else:
                rows = current_groups
            for row in rows or []:
                if isinstance(row, dict):
                    line_value = row.get("lineId")
                    group_num = row.get("currentGroup")
                else:
                    line_value = getattr(row, "lineId", None)
                    group_num = getattr(row, "currentGroup", None)
                line_id = self._normalize_line_id(line_value)
                if line_id is None or group_num is None:
                    continue
                normalized[line_id] = max(0, int(group_num) - 1)
            return normalized

        if legacy_current_groups is None:
            return None

        rows = legacy_current_groups.items() if isinstance(legacy_current_groups, dict) else []
        for line_key, group_idx in rows:
            line_id = self._normalize_line_id(line_key)
            if line_id is None or group_idx is None:
                continue
                normalized[line_id] = max(0, int(group_idx))
        return normalized

    def _production_plan_to_dict(self, production_plan: Any) -> Dict[str, Any]:
        """执行 生产 计划 to dict 对应的业务处理。

        Args:
            self: 当前对象实例。
            production_plan: 用于本函数处理的 `production_plan` 参数。

        Returns:
            Dict[str, Any]: 处理后的结果。
        """
        if isinstance(production_plan, dict):
            return deepcopy(production_plan)
        if hasattr(production_plan, "model_dump"):
            return deepcopy(production_plan.model_dump())
        if hasattr(production_plan, "dict"):
            return deepcopy(production_plan.dict())
        plans = getattr(production_plan, "plans", None)
        return {"plans": deepcopy(plans or [])}

    def _dedupe_plan_ids(self, production_plan: Any, *, update: bool) -> Tuple[Dict[str, Any], List[str]]:
        """执行 dedupe 计划 ids 对应的业务处理。

        Args:
            self: 当前对象实例。
            production_plan: 用于本函数处理的 `production_plan` 参数。
            update: 用于本函数处理的 `update` 参数。

        Returns:
            Tuple[Dict[str, Any], List[str]]: 处理后的结果。
        """
        payload = self._production_plan_to_dict(production_plan)
        plans = list(payload.get("plans", []) or [])
        if not plans:
            return payload, []

        seen_in_request: Set[str] = set()
        ignored: List[str] = []
        filtered_plans: List[Dict[str, Any]] = []

        for plan in plans:
            if hasattr(plan, "model_dump"):
                plan_dict = plan.model_dump()
            elif hasattr(plan, "dict"):
                plan_dict = plan.dict()
            else:
                plan_dict = dict(plan)

            plan_id = str(plan_dict.get("planId") or "").strip()
            if not plan_id:
                filtered_plans.append(plan_dict)
                continue

            duplicate_existing = (not update) and (plan_id in self._plan_id_to_line_id)
            duplicate_request = plan_id in seen_in_request
            if duplicate_existing or duplicate_request:
                if plan_id not in ignored:
                    ignored.append(plan_id)
                continue

            seen_in_request.add(plan_id)
            filtered_plans.append(plan_dict)

        payload["plans"] = filtered_plans
        return payload, ignored
    @staticmethod
    def _to_int_or_none(value: Any) -> Optional[int]:
        """执行 to int or none 对应的业务处理。

        Args:
            value: 待处理的单个值。

        Returns:
            Optional[int]: 处理后的结果。
        """
        if value is None:
            return None
        if isinstance(value, int):
            return int(value)
        s = str(value).strip()
        if not s:
            return None
        if s.isdigit():
            return int(s)
        m = re.search(r"LINE[-_]?(\d+)", s.upper())
        if m:
            return int(m.group(1))
        m2 = re.search(r"[lL](\d+)[cC]\d+", s)
        if m2:
            return int(m2.group(1))
        return None

    def _external_to_internal_row(self, external_row: int, aisle: int) -> int:
        """执行 external to internal row 对应的业务处理。

        Args:
            self: 当前对象实例。
            external_row: 用于本函数处理的 `external_row` 参数。
            aisle: 目标巷道编号。

        Returns:
            int: 处理后的结果。
        """
        row_val = int(external_row)
        aisle_val = int(aisle)
        expanded_row_1 = 2 * (aisle_val - 1) + 1
        expanded_row_2 = 2 * (aisle_val - 1) + 2
        if row_val in (1, 2):
            return row_val
        if row_val == expanded_row_1:
            return 1
        if row_val == expanded_row_2:
            return 2
        raise ValueError(
            f"{aisle_val}巷道的row={row_val}不合法，只允许传1/2或{expanded_row_1}/{expanded_row_2}。"
        )

    def _internal_to_external_row(self, internal_row: int, aisle: int) -> int:
        """执行 internal to external row 对应的业务处理。

        Args:
            self: 当前对象实例。
            internal_row: 用于本函数处理的 `internal_row` 参数。
            aisle: 目标巷道编号。

        Returns:
            int: 处理后的结果。
        """
        return 2 * (aisle - 1) + internal_row

    def _get_position_by_external_coords(
        self,
        aisle_id: int,
        external_row: int,
        column: int,
        level: int,
    ) -> InventoryPosition:
        """获取货位 by external coords相关逻辑。

        Args:
            self: 当前对象实例。
            aisle_id: 目标巷道编号。
            external_row: 用于本函数处理的 `external_row` 参数。
            column: 用于本函数处理的 `column` 参数。
            level: 用于本函数处理的 `level` 参数。

        Returns:
            InventoryPosition: 处理后的结果。
        """
        internal_row = self._external_to_internal_row(external_row, aisle_id)
        position_id = f"{int(aisle_id):01d}-{int(internal_row):01d}-{int(column):02d}-{int(level):02d}"
        position = self._core.inventory_manager.position_map.get(position_id)
        if position is None:
            raise ValueError(
                f"货位不存在：aisleId={aisle_id}, row={external_row}, column={column}, level={level}。"
            )
        if getattr(position, "disabled", False):
            raise ValueError(
                f"货位已禁用：aisleId={aisle_id}, row={external_row}, column={column}, level={level}。"
            )
        return position

    def _position_to_api_dict(self, position: InventoryPosition) -> Dict[str, Any]:
        """执行 货位 to api dict 对应的业务处理。

        Args:
            self: 当前对象实例。
            position: 货位对象或货位描述。

        Returns:
            Dict[str, Any]: 对外使用的货位坐标字典。
        """
        aisle = int(getattr(position, "aisle", 0) or 0)
        row = int(getattr(position, "row", 0) or 0)
        return {
            "aisleId": str(aisle),
            "row": self._internal_to_external_row(row, aisle),
            "column": int(getattr(position, "column", 0) or 0),
            "level": int(getattr(position, "level", 0) or 0),
        }

    def _task_positions_to_api(self, task: Optional[TaskData]) -> Optional[List[Dict[str, Any]]]:
        """执行 任务 positions to api 对应的业务处理。

        Args:
            self: 当前对象实例。
            task: 待处理的任务对象。

        Returns:
            Optional[List[Dict[str, Any]]]: 已转换的货位列表；没有固定货位时返回 None。
        """
        if task is None or not getattr(task, "positions", None):
            return None
        return [self._position_to_api_dict(pos) for pos in task.positions]

    def _find_task_in_pending_queues(self, task_id: str, task_type: Optional[str] = None) -> Optional[TaskData]:
        """查找任务 in 待处理 queues相关逻辑。

        Args:
            self: 当前对象实例。
            task_id: 任务唯一标识。
            task_type: 任务类型。

        Returns:
            Optional[TaskData]: 处理后的结果。
        """
        task_type_norm = str(task_type or "").upper()
        if task_type_norm != "INBOUND":
            task = next((t for t in self._core.pending_outbound_queue if t.task_id == task_id), None)
            if task is not None:
                return task
        if task_type_norm != "OUTBOUND":
            for queue in self._core.pending_inbound_by_aisle.values():
                task = next((t for t in queue if t.task_id == task_id), None)
                if task is not None:
                    return task
        return None

    def get_task_by_id(self, task_id: str, task_type: Optional[str] = None) -> Optional[TaskData]:
        """获取任务 by id相关逻辑。

        Args:
            self: 当前对象实例。
            task_id: 任务唯一标识。
            task_type: 任务类型。

        Returns:
            Optional[TaskData]: 处理后的结果。
        """
        task = self._core.running_tasks.get(task_id)
        if task is not None:
            return task
        task = self._pending_execution_tasks.get(task_id)
        if task is not None:
            return task
        return self._find_task_in_pending_queues(task_id, task_type=task_type)

    def get_active_task_ids(self) -> Set[str]:
        """获取active 任务 ids相关逻辑。

        Args:
            self: 当前对象实例。

        Returns:
            Set[str]: 处理后的结果。
        """
        task_ids: Set[str] = set(self._core.running_tasks.keys()) | set(self._pending_execution_tasks.keys())
        task_ids.update(t.task_id for t in self._core.pending_outbound_queue)
        for queue in self._core.pending_inbound_by_aisle.values():
            task_ids.update(t.task_id for t in queue)
        return task_ids

    def get_task_for_aisle(self, aisle_id: int, task_id: Optional[str] = None) -> Optional[TaskData]:
        """获取任务 for 巷道相关逻辑。

        Args:
            self: 当前对象实例。
            aisle_id: 目标巷道编号。
            task_id: 任务唯一标识。

        Returns:
            Optional[TaskData]: 处理后的结果。
        """
        candidates: List[TaskData] = []
        for task in self._core.running_tasks.values():
            if self._get_task_effective_aisle(task) == int(aisle_id):
                candidates.append(task)
        for task in self._pending_execution_tasks.values():
            if self._get_task_effective_aisle(task) == int(aisle_id):
                candidates.append(task)
        for task in self._core.pending_outbound_queue:
            if self._get_task_effective_aisle(task) == int(aisle_id):
                candidates.append(task)
        for queue in self._core.pending_inbound_by_aisle.values():
            for task in queue:
                if self._get_task_effective_aisle(task) == int(aisle_id):
                    candidates.append(task)
        if task_id is not None:
            return next((task for task in candidates if task.task_id == task_id), None)
        return candidates[0] if candidates else None

    # ==========================================================================
    # 辅助函数：出库位置、资源单元与生产组处理
    # ==========================================================================
    def _get_task_effective_aisle(self, task: Optional[TaskData]) -> Optional[int]:
        """获取任务 effective 巷道相关逻辑。

        Args:
            self: 当前对象实例。
            task: 待处理的任务对象。

        Returns:
            Optional[int]: 处理后的结果。
        """
        if task is None:
            return None
        assigned = self._to_int_or_none(getattr(task, "assigned_aisle", None))
        if assigned is not None:
            return int(assigned)
        positions = list(getattr(task, "positions", None) or [])
        if positions:
            return self._to_int_or_none(getattr(positions[0], "aisle", None))
        return None

    def _find_running_task_on_aisle(
        self,
        aisle: Optional[int],
        exclude_task_id: Optional[str] = None,
    ) -> Optional[TaskData]:
        """查找执行中 任务 on 巷道相关逻辑。

        Args:
            self: 当前对象实例。
            aisle: 目标巷道编号。
            exclude_task_id: 用于本函数处理的 `exclude_task_id` 参数。

        Returns:
            Optional[TaskData]: 处理后的结果。
        """
        if aisle is None:
            return None
        aisle_val = int(aisle)
        exclude = str(exclude_task_id or "")
        for other_task in self._core.running_tasks.values():
            other_id = str(getattr(other_task, "task_id", "") or "")
            if exclude and other_id == exclude:
                continue
            if self._get_task_effective_aisle(other_task) == aisle_val:
                return other_task
        return None

    def _iter_known_outbound_tasks(self):
        """执行 iter known 出库 tasks 对应的业务处理。

        Args:
            self: 当前对象实例。

        Returns:
            处理结果；具体类型由调用上下文决定。
        """
        for task in self._core.pending_outbound_queue:
            if str(getattr(task, "task_type", "") or "") == TASK_TYPE_OUTBOUND:
                yield task
        for task in self._core.running_tasks.values():
            if str(getattr(task, "task_type", "") or "") == TASK_TYPE_OUTBOUND:
                yield task
        for task in self._core.completed_tasks:
            if str(getattr(task, "task_type", "") or "") == TASK_TYPE_OUTBOUND:
                yield task

    def _build_line_task_group_counts(self) -> Dict[int, int]:
        """构建line 任务 任务组 counts相关逻辑。

        Args:
            self: 当前对象实例。

        Returns:
            Dict[int, int]: 处理后的结果。
        """
        line_group_counts: Dict[int, int] = {}
        for task in self._iter_known_outbound_tasks():
            production_line = getattr(task, "production_line", None)
            group_idx = self._extract_task_group_idx(task)
            if production_line is None or group_idx is None:
                continue
            line_id = int(production_line)
            line_group_counts[line_id] = max(line_group_counts.get(line_id, 0), int(group_idx) + 1)
        return line_group_counts

    def _task_brief_dict(self, task: TaskData) -> Dict[str, Any]:
        """执行 任务 brief dict 对应的业务处理。

        Args:
            self: 当前对象实例。
            task: 待处理的任务对象。

        Returns:
            Dict[str, Any]: 处理后的结果。
        """
        plan_id = getattr(task, "plan_id", None)
        group_idx = getattr(task, "group_idx", None)
        plan_index = (int(group_idx) + 1) if group_idx is not None else None
        return {
            "taskId": str(getattr(task, "task_id", "") or ""),
            "taskType": str(getattr(task, "task_type", "") or ""),
            "planId": plan_id,
            "planIndex": plan_index,
            "aisleId": str(self._get_task_effective_aisle(task)) if self._get_task_effective_aisle(task) is not None else None,
            "inLine": getattr(task, "in_line", None),
            "outLine": getattr(task, "out_line", None),
            "positions": self._task_positions_to_api(task),
        }

    def _iter_executable_inbound_candidates(self, aisle: int) -> List[TaskData]:
        """执行 iter executable 入库 candidates 对应的业务处理。

        Args:
            self: 当前对象实例。
            aisle: 目标巷道编号。

        Returns:
            List[TaskData]: 处理后的结果。
        """
        queue = list(self._core.pending_inbound_by_aisle.get(int(aisle), []) or [])
        if not queue:
            return []
        line_buckets: Dict[Any, TaskData] = {}
        for task in queue:
            line_key = getattr(task, "in_line", 1)
            if line_key not in line_buckets:
                line_buckets[line_key] = task
        candidates: List[TaskData] = []
        for task in line_buckets.values():
            if not self.is_aisle_available(int(aisle)):
                continue
            if not self.is_inbound_path_available(int(aisle), getattr(task, "in_line", None)):
                continue
            if not getattr(task, "positions", None):
                continue
            candidates.append(task)
        return candidates

    def _iter_executable_outbound_candidates(self, aisle: int) -> List[TaskData]:
        """执行 iter executable 出库 candidates 对应的业务处理。

        Args:
            self: 当前对象实例。
            aisle: 目标巷道编号。

        Returns:
            List[TaskData]: 处理后的结果。
        """
        candidates: List[TaskData] = []
        if not self.is_aisle_available(int(aisle)):
            return candidates
        for task in list(self._core.pending_outbound_queue):
            if not self._is_outbound_line_head(task):
                continue
            out_line = getattr(task, "out_line", None) or getattr(task, "production_line", None)
            if out_line is None:
                continue
            if not self.is_outbound_path_available(int(aisle), out_line):
                continue
            if self._core.check_blockage(int(aisle), out_line, current_time=self._core.current_time):
                continue
            pl = getattr(task, "production_line", None)
            if pl is not None and not self._core.can_start_outbound_task(
                task.task_id,
                int(pl),
                task_group_idx=getattr(task, "group_idx", None),
            ):
                continue
            candidates.append(task)
        return candidates

    def _get_active_outbound_line_task(self, production_line: Optional[int]) -> Optional[TaskData]:
        """获取active 出库 line 任务相关逻辑。

        Args:
            self: 当前对象实例。
            production_line: 生产线编号。

        Returns:
            Optional[TaskData]: 处理后的结果。
        """
        if production_line is None:
            return None
        line_int = int(production_line)
        for task in self._core.running_tasks.values():
            if str(getattr(task, "task_type", "") or "") != TASK_TYPE_OUTBOUND:
                continue
            task_line = getattr(task, "production_line", None)
            if task_line is not None and int(task_line) == line_int:
                return task
        for task in self._pending_execution_tasks.values():
            if str(getattr(task, "task_type", "") or "") != TASK_TYPE_OUTBOUND:
                continue
            task_line = getattr(task, "production_line", None)
            if task_line is not None and int(task_line) == line_int:
                return task
        for task in list(self._core.pending_outbound_queue):
            if str(getattr(task, "task_type", "") or "") != TASK_TYPE_OUTBOUND:
                continue
            task_line = getattr(task, "production_line", None)
            if task_line is None or int(task_line) != line_int:
                continue
            if self._classify_outbound_group_state(task) == "past":
                continue
            return task
        return None

    def _is_outbound_line_head(self, task: Optional[TaskData]) -> bool:
        """判断出库 line head相关逻辑。

        Args:
            self: 当前对象实例。
            task: 待处理的任务对象。

        Returns:
            bool: 判断结果。
        """
        if task is None or str(getattr(task, "task_type", "") or "") != TASK_TYPE_OUTBOUND:
            return False
        production_line = getattr(task, "production_line", None)
        if production_line is None:
            return True
        active_task = self._get_active_outbound_line_task(int(production_line))
        if active_task is None:
            return False
        return str(getattr(active_task, "task_id", "") or "") == str(getattr(task, "task_id", "") or "")

    def _get_schedulable_outbound_heads(self) -> List[TaskData]:
        """获取schedulable 出库 heads相关逻辑。

        Args:
            self: 当前对象实例。

        Returns:
            List[TaskData]: 处理后的结果。
        """
        heads: List[TaskData] = []
        seen_lines: Set[int] = set()
        for task in list(self._core.pending_outbound_queue):
            if str(getattr(task, "task_type", "") or "") != TASK_TYPE_OUTBOUND:
                continue
            group_state = self._classify_outbound_group_state(task)
            if group_state != "current":
                continue
            production_line = getattr(task, "production_line", None)
            if production_line is None:
                heads.append(task)
                continue
            line_int = int(production_line)
            active_task = self._get_active_outbound_line_task(line_int)
            if active_task is not None and str(getattr(active_task, "task_id", "") or "") != str(getattr(task, "task_id", "") or ""):
                continue
            if line_int in seen_lines:
                continue
            seen_lines.add(line_int)
            heads.append(task)
        return heads

    def _is_task_logically_executable(
        self,
        task: Optional[TaskData],
        preferred_aisle: Optional[int] = None,
    ) -> Tuple[bool, Optional[str]]:
        """判断任务是否满足业务上的可执行条件。

        Args:
            self: 当前对象实例。
            task: 待判断的入库或出库任务。
            preferred_aisle: 可选的目标巷道覆盖值；未传时使用任务已分配巷道。

        Returns:
            Tuple[bool, Optional[str]]: 是否可执行及不可执行时的中文原因。
        """
        if task is None:
            return False, "未找到对应任务。"
        task_id = str(getattr(task, "task_id", "") or "")
        task_type = str(getattr(task, "task_type", "") or "")
        if task_id in self._core.running_tasks or task_id in self._pending_execution_tasks:
            return True, None
        # 入库只允许同巷道、同入库线的队首任务启动，避免后到任务越过前序任务。
        if task_type == TASK_TYPE_INBOUND:
            aisle = preferred_aisle if preferred_aisle is not None else self._get_task_effective_aisle(task)
            if aisle is None:
                return False, "入库任务缺少巷道信息，无法执行。"
            if not getattr(task, "positions", None):
                return False, "入库任务尚未分配执行位置，暂不可执行。"
            aisle = int(aisle)
            queue = list(self._core.pending_inbound_by_aisle.get(aisle, []) or [])
            if not queue:
                return False, f"巷道 {aisle} 当前没有可执行入库任务。"
            in_line = getattr(task, "in_line", 1)
            line_head = next((t for t in queue if getattr(t, "in_line", 1) == in_line), None)
            if line_head is None or str(getattr(line_head, "task_id", "") or "") != task_id:
                return False, f"任务 {task_id} 不是巷道 {aisle}、inLine={in_line} 的队首任务。"
            if not self.is_aisle_available(aisle):
                return False, f"巷道 {aisle} 当前不可用。"
            if not self.is_inbound_path_available(aisle, in_line):
                return False, f"巷道 {aisle} 当前不可从 inLine={in_line} 入库。"
            return True, None

        # 出库先验证该任务仍在待执行队列中，再依次应用生产组、产线队首和通道约束。
        in_pending_outbound = any(str(getattr(t, "task_id", "") or "") == task_id for t in self._core.pending_outbound_queue)
        if not in_pending_outbound:
            return False, f"任务 {task_id} 不在当前可执行出库队列中。"
        group_state = self._classify_outbound_group_state(task)
        if group_state == "past":
            return False, f"任务 {task_id} 属于当前组之前的历史组，已忽略。"
        if group_state == "future":
            return False, f"任务 {task_id} 不属于当前组，暂不可执行。"
        if not self._is_outbound_line_head(task):
            production_line = getattr(task, "production_line", None)
            if production_line is not None:
                return False, f"任务 {task_id} 不是产线 {int(production_line)} 当前队首出库任务。"
            return False, f"任务 {task_id} 不是当前队首出库任务。"
        aisle = preferred_aisle if preferred_aisle is not None else self._get_task_effective_aisle(task)
        if aisle is None:
            return False, f"任务 {task_id} 缺少巷道信息，暂不可执行。"
        aisle = int(aisle)
        if not self.is_aisle_available(aisle):
            return False, f"巷道 {aisle} 当前不可用。"
        out_line = getattr(task, "out_line", None) or getattr(task, "production_line", None)
        if out_line is None:
            return False, f"任务 {task_id} 缺少出库线信息，暂不可执行。"
        if not self.is_outbound_path_available(aisle, out_line):
            return False, f"巷道 {aisle} 当前不可向 outLine={out_line} 出库。"
        if self._core.check_blockage(aisle, out_line, current_time=self._core.current_time):
            return False, f"巷道 {aisle} 当前出库路径受阻。"
        pl = getattr(task, "production_line", None)
        if pl is not None and not self._core.can_start_outbound_task(
            task_id,
            int(pl),
            task_group_idx=getattr(task, "group_idx", None),
        ):
            return False, f"任务 {task_id} 不满足当前组约束，暂不可执行。"
        return True, None

    def get_executable_tasks_by_aisle(self) -> Dict[int, List[TaskData]]:
        """获取executable tasks by 巷道相关逻辑。

        Args:
            self: 当前对象实例。

        Returns:
            Dict[int, List[TaskData]]: 处理后的结果。
        """
        grouped = self.get_tasks_by_aisle_for_api()
        return {
            int(aisle): list((payload.get("can_executing") or []))
            for aisle, payload in grouped.items()
        }

    def get_prioritized_executable_tasks_by_aisle(
        self,
        recommended_tasks: Optional[Dict[int, TaskData]] = None,
    ) -> Dict[int, List[TaskData]]:
        """获取prioritized executable tasks by 巷道相关逻辑。

        Args:
            self: 当前对象实例。
            recommended_tasks: 用于本函数处理的 `recommended_tasks` 参数。

        Returns:
            Dict[int, List[TaskData]]: 处理后的结果。
        """
        base = self.get_executable_tasks_by_aisle()
        recommended_tasks = {int(k): v for k, v in (recommended_tasks or {}).items() if v is not None}
        for aisle, rec in recommended_tasks.items():
            arr = list(base.get(int(aisle), []))
            rec_id = str(getattr(rec, "task_id", "") or "")
            if rec_id:
                arr = [t for t in arr if str(getattr(t, "task_id", "") or "") != rec_id]
            arr.insert(0, rec)
            base[int(aisle)] = arr
        return base

    def _is_task_currently_executable(self, task: TaskData, preferred_aisle: Optional[int] = None) -> Tuple[bool, Optional[str]]:
        """判断任务 currently executable相关逻辑。

        Args:
            self: 当前对象实例。
            task: 待处理的任务对象。
            preferred_aisle: 用于本函数处理的 `preferred_aisle` 参数。

        Returns:
            bool: 判断结果。
        """
        if task is None:
            return False, "未找到对应任务。"
        task_id = str(getattr(task, "task_id", "") or "")
        if task_id in self._core.running_tasks:
            return True, None
        logical_ok, logical_reason = self._is_task_logically_executable(task, preferred_aisle=preferred_aisle)
        if not logical_ok:
            return False, logical_reason
        aisle = preferred_aisle if preferred_aisle is not None else self._get_task_effective_aisle(task)
        running_task = self._find_running_task_on_aisle(aisle, exclude_task_id=task_id)
        if running_task is not None:
            other_id = str(getattr(running_task, "task_id", "") or "")
            return False, f"任务 {other_id} 当前正在执行。"
        return True, None

    def get_tasks_by_aisle_for_api(self) -> Dict[int, Dict[str, List[TaskData]]]:
        """获取tasks by 巷道 for api相关逻辑。

        Args:
            self: 当前对象实例。

        Returns:
            Dict[int, Dict[str, List[TaskData]]]: 处理后的结果。
        """
        self._sync_time()
        grouped: Dict[int, Dict[str, List[TaskData]]] = {
            int(aisle): {"can_executing": [], "pending": [], "running": []}
            for aisle in self._core.aisles
        }

        def ensure_bucket(aisle_val: Optional[int]) -> Optional[Dict[str, List[TaskData]]]:
            """执行 ensure bucket 对应的业务处理。

            Args:
                aisle_val: 用于本函数处理的 `aisle_val` 参数。

            Returns:
                Optional[Dict[str, List[TaskData]]]: 处理后的结果。
            """
            if aisle_val is None:
                return None
            aisle_int = int(aisle_val)
            if aisle_int not in grouped:
                grouped[aisle_int] = {"can_executing": [], "pending": [], "running": []}
            return grouped[aisle_int]

        seen_ids: Set[str] = set()

        for task in self._core.running_tasks.values():
            task_id = str(getattr(task, "task_id", "") or "")
            bucket = ensure_bucket(self._get_task_effective_aisle(task))
            if bucket is None or not task_id or task_id in seen_ids:
                continue
            bucket["running"].append(task)
            seen_ids.add(task_id)

        for task in self._pending_execution_tasks.values():
            task_id = str(getattr(task, "task_id", "") or "")
            bucket = ensure_bucket(self._get_task_effective_aisle(task))
            if bucket is None or not task_id or task_id in seen_ids:
                continue
            bucket["can_executing"].append(task)
            seen_ids.add(task_id)

        for aisle, queue in self._core.pending_inbound_by_aisle.items():
            bucket = ensure_bucket(int(aisle))
            if bucket is None:
                continue
            for task in list(queue or []):
                task_id = str(getattr(task, "task_id", "") or "")
                if not task_id or task_id in seen_ids:
                    continue
                logical_ok, _ = self._is_task_logically_executable(task, preferred_aisle=int(aisle))
                bucket["can_executing" if logical_ok else "pending"].append(task)
                seen_ids.add(task_id)

        for task in list(self._core.pending_outbound_queue):
            task_id = str(getattr(task, "task_id", "") or "")
            bucket = ensure_bucket(self._get_task_effective_aisle(task))
            if bucket is None or not task_id or task_id in seen_ids:
                continue
            group_state = self._classify_outbound_group_state(task)
            if group_state == "past":
                continue
            logical_ok, _ = self._is_task_logically_executable(task)
            bucket["can_executing" if logical_ok else "pending"].append(task)
            seen_ids.add(task_id)

        return grouped

    def save_state(self) -> Dict[str, Any]:
        """执行 save 状态 对应的业务处理。

        Args:
            self: 当前对象实例。

        Returns:
            Dict[str, Any]: 处理后的结果。
        """
        return {
            "core_state": self._core._save_simulation_state(),
            "pending_execution_tasks": deepcopy(self._pending_execution_tasks),
            "aisle_availability": deepcopy(self._aisle_availability),
            "add_plan_index_alias": deepcopy(self._add_plan_index_alias),
            "plan_id_to_line_id": deepcopy(self._plan_id_to_line_id),
        }

    def restore_state(self, saved_state: Dict[str, Any]) -> None:
        """执行 restore 状态 对应的业务处理。

        Args:
            self: 当前对象实例。
            saved_state: 用于本函数处理的 `saved_state` 参数。

        Returns:
            None: 处理后的结果。
        """
        self._core._restore_simulation_state(saved_state["core_state"])
        self._pending_execution_tasks = deepcopy(saved_state.get("pending_execution_tasks", {}))
        self._aisle_availability = deepcopy(saved_state.get("aisle_availability", {}))
        self._add_plan_index_alias = deepcopy(saved_state.get("add_plan_index_alias", {}))
        self._plan_id_to_line_id = deepcopy(saved_state.get("plan_id_to_line_id", {}))
        self._core.pending_execution_tasks = self._pending_execution_tasks

    def _parse_line_ref(self, value: Any) -> Optional[Any]:
        """
        Parse line reference from API input.
        Supports:
        - int / numeric string: 1, "2"
        - dock token string: "L4C1", "l1c17"
        Returns normalized int or upper token string.
        """
        if value is None:
            return None
        if isinstance(value, int):
            return int(value)
        s = str(value).strip()
        if not s:
            return None
        if s.isdigit():
            return int(s)
        m = re.fullmatch(r"[lL]\d+[cC]\d+", s)
        if m:
            return s.upper()
        return s

    def _clamp_col_by_aisle(self, col: int, aisle_id: Optional[int]) -> int:
        """执行 clamp col by 巷道 对应的业务处理。

        Args:
            self: 当前对象实例。
            col: 用于本函数处理的 `col` 参数。
            aisle_id: 目标巷道编号。

        Returns:
            int: 处理后的结果。
        """
        if aisle_id is None:
            return int(col)
        try:
            est = getattr(self._core, "time_estimator", None)
            max_col = int(getattr(est, "aisle_max_columns", {}).get(int(aisle_id), 0))
        except Exception:
            max_col = 0
        if max_col <= 0:
            return int(col)
        return max(1, min(int(col), max_col))

    def _normalize_line_ref_key(self, value: Any, aisle_id: Optional[int] = None, direction: Optional[str] = None) -> Optional[Any]:
        """标准化line ref key相关逻辑。

        Args:
            self: 当前对象实例。
            value: 待处理的单个值。
            aisle_id: 目标巷道编号。
            direction: 用于本函数处理的 `direction` 参数。

        Returns:
            Optional[Any]: 处理后的结果。
        """
        line = self._parse_line_ref(value)
        if line is None:
            return None
        if isinstance(line, str):
            s = line.upper()
            m = re.fullmatch(r"[L](\d+)[C](\d+)", s)
            if m:
                level = int(m.group(1))
                col = int(m.group(2))
                col = self._clamp_col_by_aisle(col, aisle_id)
                return f"L{level}C{col}"
            return s
        # 产线号为数字时，结合巷道规则解析为列号受限的出入库口标识。
        try:
            est = getattr(self._core, "time_estimator", None)
            if est is None:
                return int(line)
            if direction == "in":
                col, level = est.resolve_inbound_dock(int(line), default_layer=1, aisle=aisle_id)
            elif direction == "out":
                col, level = est.resolve_outbound_dock(int(line), default_layer=1, aisle=aisle_id)
            else:
                return int(line)
            return f"L{int(level)}C{int(col)}"
        except Exception:
            return int(line)

    @staticmethod
    def _normalize_direction(direction: Any) -> Optional[str]:
        """标准化direction相关逻辑。

        Args:
            direction: 用于本函数处理的 `direction` 参数。

        Returns:
            Optional[str]: 处理后的结果。
        """
        if direction is None:
            return None
        s = str(direction).strip().upper()
        if s in ("IN", "INBOUND"):
            return "in"
        if s in ("OUT", "OUTBOUND"):
            return "out"
        return None

    def _is_empty_skid_request(self, sku_list: List[Dict[str, Any]]) -> bool:
        """Detect API outbound request that asks system to choose any empty skid.
        Rule: only check skid_state == 0, skuId can be present or absent.
        """
        if not sku_list:
            return False
        sku = sku_list[0] or {}
        feats = self._normalize_features(sku.get("features") if isinstance(sku.get("features"), dict) else {})
        skid_state = str(feats.get("skid_state", "")).strip()
        if not skid_state:
            canonical = getattr(self._core, "_canonical_feature_key", None)
            for k, v in sku.items():
                if k in ("skuId", "quantity", "features"):
                    continue
                ck = str(canonical(k)) if callable(canonical) else str(k)
                if str(ck) == "skid_state" and v is not None:
                    skid_state = str(v).strip()
                    break
        return skid_state == "0"

    def _extract_position_feature(self, pos: InventoryPosition, keys: List[str]) -> Optional[str]:
        """执行 extract 货位 特征 对应的业务处理。

        Args:
            self: 当前对象实例。
            pos: 用于本函数处理的 `pos` 参数。
            keys: 用于本函数处理的 `keys` 参数。

        Returns:
            Optional[str]: 处理后的结果。
        """
        feats = getattr(pos, "features", None)
        if not isinstance(feats, dict):
            return None
        feats = self._normalize_features(feats)
        canonical = getattr(self._core, "_canonical_feature_key", None)
        for k in keys:
            ck = str(canonical(k)) if callable(canonical) else str(k)
            if ck in feats and feats.get(ck) is not None:
                return str(feats.get(ck)).strip()
        return None

    def _empty_skid_positions(self) -> List[InventoryPosition]:
        """Return occupied positions whose feature skid_state indicates empty skid."""
        result: List[InventoryPosition] = []
        for p in self._core.inventory_manager.inventory_positions:
            if p.is_empty():
                continue
            skid_state = self._extract_position_feature(p, ["skid_state", "滑橇状态", "skidState"])
            if skid_state == "0":
                result.append(p)
        return result

    def _is_aisle_idle(self, aisle: int) -> bool:
        """判断巷道 idle相关逻辑。

        Args:
            self: 当前对象实例。
            aisle: 目标巷道编号。

        Returns:
            bool: 判断结果。
        """
        return not any(
            (getattr(t, "assigned_aisle", None) == aisle)
            for t in self._core.running_tasks.values()
        )

    def _empty_skid_outbound_score(self, pos: InventoryPosition, out_line: Any, idle_only: bool) -> Tuple[float, float, int]:
        """
        Lower is better:
        1) distance to out dock
        2) projected load (pending inbound + pending outbound in aisle + running in aisle)
        3) aisle id
        """
        dock_col, dock_level = self._core.time_estimator.resolve_outbound_dock(
            out_line, default_layer=1, aisle=pos.aisle
        )
        distance = abs(int(pos.column) - int(dock_col)) + 0.2 * abs(int(pos.level) - int(dock_level))
        running_cnt = sum(
            1 for t in self._core.running_tasks.values()
            if getattr(t, "assigned_aisle", None) == pos.aisle
        )
        pending_in = len(self._core.pending_inbound_by_aisle.get(pos.aisle, []))
        pending_out = sum(
            1 for t in self._core.pending_outbound_queue
            if getattr(t, "assigned_aisle", None) == pos.aisle
        )
        load = float(running_cnt + pending_in + pending_out)
        # 如果调用方只要求空闲巷道，而当前巷道正忙，则将其排除在候选之外。
        if idle_only and running_cnt > 0:
            load += 1e6
        return (distance, load, int(pos.aisle))

    def _resolve_empty_skid_outbound(self, task: TaskData) -> Optional[TaskData]:
        """
        Convert placeholder empty-skid outbound request into concrete outbound:
        choose a specific occupied empty-skid position and bind skuId/aisle/position.
        """
        raw_out_line = getattr(task, "out_line", None)
        out_line = self._to_int_or_none(raw_out_line)
        if out_line is None:
            out_line = int(getattr(task, "production_line", 1) or 1)
        all_candidates = [
            p
            for p in self._empty_skid_positions()
            if self.is_outbound_path_available(int(p.aisle), out_line)
        ]
        if not all_candidates:
            return None

        available_now = [
            p for p in all_candidates
            if self._is_aisle_idle(int(p.aisle))
            and self.is_outbound_path_available(int(p.aisle), out_line)
            and (not self._core.check_blockage(int(p.aisle), out_line, current_time=self._core.current_time))
        ]
        if available_now:
            chosen = min(available_now, key=lambda p: self._empty_skid_outbound_score(p, out_line, idle_only=True))
        else:
        # 当前所有候选巷道都繁忙或堵塞时，选择预计后续负载较低的巷道。
            chosen = min(all_candidates, key=lambda p: self._empty_skid_outbound_score(p, out_line, idle_only=False))

        sku_id = str(getattr(chosen, "sku", "") or "")
        qty = int(getattr(chosen, "quantity", 1) or 1)
        feats = dict(getattr(chosen, "features", {}) or {})
        task.positions = [chosen]
        task.assigned_aisle = int(chosen.aisle)
        task.skus = [{"skuId": sku_id, "quantity": qty, "features": feats}]
        task.task_record = dict(getattr(task, "task_record", {}) or {})
        task.task_record["empty_skid_request"] = True
        task.task_record["high_priority"] = True
        task.task_record["resolved_by"] = "api_empty_skid_selector"
        return task

    def _rebuild_inventory_views(self) -> None:
        """执行 rebuild 库存 views 对应的业务处理。

        Args:
            self: 当前对象实例。

        Returns:
            None: 处理后的结果。
        """
        manager = self._core.inventory_manager
        manager.sku_position_index = {}
        manager.current_inventory = {aisle: {} for aisle in self._core.aisles}

        dynamic_skus = set()
        for position in manager.inventory_positions:
            if position.is_double_layer:
                if position.upper_quantity > 0 and position.upper_sku:
                    dynamic_skus.add(position.upper_sku)
                    manager.current_inventory[position.aisle][position.upper_sku] = manager.current_inventory[position.aisle].get(position.upper_sku, 0) + position.upper_quantity
                    manager.sku_position_index.setdefault(position.upper_sku, []).append(position)
                if position.lower_quantity > 0 and position.lower_sku:
                    dynamic_skus.add(position.lower_sku)
                    manager.current_inventory[position.aisle][position.lower_sku] = manager.current_inventory[position.aisle].get(position.lower_sku, 0) + position.lower_quantity
                    manager.sku_position_index.setdefault(position.lower_sku, []).append(position)
            elif position.quantity > 0 and position.sku:
                dynamic_skus.add(position.sku)
                manager.current_inventory[position.aisle][position.sku] = manager.current_inventory[position.aisle].get(position.sku, 0) + position.quantity
                manager.sku_position_index.setdefault(position.sku, []).append(position)

        self._core.sku_types = sorted(dynamic_skus)
        manager.sku_types = sorted(dynamic_skus)

    def sync_aisle_status(self, aisle_status_list: List[Any]) -> None:
        """执行 sync 巷道 status 对应的业务处理。

        Args:
            self: 当前对象实例。
            aisle_status_list: 用于本函数处理的 `aisle_status_list` 参数。

        Returns:
            None: 处理后的结果。
        """
        # 将接口时间同步到 Core，确保状态快照和后续阻塞时间计算使用同一时间基准。
        self._sync_time()
        for status in aisle_status_list:
            # status 是单条巷道状态快照；aisle_id / is_available 是该巷道的总可用性。
            aisle_id = int(status.aisleId) if hasattr(status, "aisleId") else int(status["aisleId"])
            is_available = status.isAvailable if hasattr(status, "isAvailable") else status["isAvailable"]
            # dock_availability: 方向 -> 线路键 -> 是否可用。口位状态是增量维护的运行状态：
            # 本次未传或传空数组时沿用内存中的禁用记录，避免空快照意外解除检修口。
            previous_status = self._aisle_availability.get(aisle_id, {})
            previous_dock_availability = previous_status.get("dock_availability", {"in": {}, "out": {}})
            dock_rows = status.dockAvailability if hasattr(status, "dockAvailability") else status.get("dockAvailability", [])
            if dock_rows:
                # 非空列表代表发送方提供了该巷道完整的口位快照，按本次内容覆盖旧状态。
                dock_availability = {"in": {}, "out": {}}
                for row in dock_rows:
                    direction = row.direction if hasattr(row, "direction") else row.get("direction")
                    direction_norm = self._normalize_direction(direction)
                    line_ref = row.lineRef if hasattr(row, "lineRef") else row.get("lineRef")
                    line_key = self._normalize_line_ref_key(line_ref, aisle_id=aisle_id, direction=direction_norm)
                    available = row.isAvailable if hasattr(row, "isAvailable") else row.get("isAvailable", True)
                    if direction_norm and line_key is not None:
                        dock_availability[direction_norm][line_key] = bool(available)
            else:
                # deepcopy 防止后续更新新快照时改写仍由旧状态引用的嵌套方向字典。
                dock_availability = deepcopy(previous_dock_availability)
            self._aisle_availability[aisle_id] = {
                "is_available": is_available,
                "unavailable_reason": status.unavailableReason if hasattr(status, "unavailableReason") else status.get("unavailableReason"),
                "bank": status.bank if hasattr(status, "bank") else status.get("bank"),
                "dock_availability": dock_availability,
            }

            if not is_available:
                for out_line in range(1, self._core.num_production_lines + 1):
                    self._core.update_blockage_status(
                        aisle=aisle_id,
                        out_line=out_line,
                        blocked=True,
                        unblock_time=self._core.current_time + self._core.outbound_congestion_time,
                    )
                continue

            exit_congestion = status.exitCongestion if hasattr(status, "exitCongestion") else status.get("exitCongestion", [])
            for congestion in exit_congestion:
                line_id_str = congestion.lineId if hasattr(congestion, "lineId") else congestion["lineId"]
                line_id = int(str(line_id_str).replace("LINE-", ""))
                is_congested = congestion.isCongested if hasattr(congestion, "isCongested") else congestion["isCongested"]
                self._core.update_blockage_status(
                    aisle=aisle_id,
                    out_line=line_id,
                    blocked=bool(is_congested),
                    unblock_time=(self._core.current_time + self._core.outbound_congestion_time) if is_congested else 0.0,
                )

    def sync_inventory(self, inventory_list: List[Any]) -> None:
        """执行 sync 库存 对应的业务处理。

        Args:
            self: 当前对象实例。
            inventory_list: 用于本函数处理的 `inventory_list` 参数。

        Returns:
            None: 处理后的结果。
        """
        # 空库存数组表示“不更新库存”，非空数组则按完整快照重建库存视图。
        self._sync_time()
        if not inventory_list:
            return

        # 全量快照重建前保留“位置 + SKU -> FIFO 时间”。时间存于 SKU features，
        # 因此会随库存本身迁移或清除，而不会错误地绑定到同一物理货位的下一件库存。
        previous_fifo_times: Dict[Tuple[str, str], Optional[float]] = {}
        for previous_position in self._core.inventory_manager.inventory_positions:
            if previous_position.is_double_layer:
                continue
            previous_sku = str(getattr(previous_position, "sku", "") or "")
            previous_features = getattr(previous_position, "features", {}) or {}
            previous_time = previous_features.get("_inbound_time")
            if previous_sku:
                previous_fifo_times[(previous_position.get_position_id(), previous_sku)] = (
                    float(previous_time) if isinstance(previous_time, (int, float)) else None
                )

        # 清理库存本身，不清任务队列、执行状态或生产计划。
        self._clear_inventory_only()
        for inv_item in inventory_list:
            # inv_item 是一个外部库位快照；external_row 按接口行号口径，后续转换为内部货位。
            aisle_id = int(inv_item.aisleId) if hasattr(inv_item, "aisleId") else int(inv_item["aisleId"])
            external_row = inv_item.row if hasattr(inv_item, "row") else inv_item["row"]
            column = inv_item.column if hasattr(inv_item, "column") else inv_item["column"]
            level = inv_item.level if hasattr(inv_item, "level") else inv_item["level"]
            shelf = inv_item.shelf if hasattr(inv_item, "shelf") else inv_item.get("shelf")
            # positions_data 是该库位包含的 SKU 层明细，双层货位可包含上下层数据。
            positions_data = inv_item.positions if hasattr(inv_item, "positions") else inv_item.get("positions", [])

            position = self._get_position_by_external_coords(aisle_id, external_row, column, level)

            if position.is_double_layer:
                position.upper_sku = None
                position.upper_quantity = 0
                position.upper_features = None
                position.lower_sku = None
                position.lower_quantity = 0
                position.lower_features = None
            else:
                position.sku = ""
                position.quantity = 0
                position.features = None

            for pos_data in (positions_data or []):
                sku_dict = self._sku_entry_to_dict(pos_data)
                sku_id = sku_dict.get("skuId")
                quantity = sku_dict.get("quantity", 0)
                if not sku_id or quantity <= 0:
                    continue
                raw_features = sku_dict.get("features")
                if isinstance(raw_features, dict):
                    sku_features = self._normalize_features(dict(raw_features))
                else:
                    sku_features = {}
                # 无论单层还是兼容双层路径，FIFO 时间均写入当前 SKU 的 features。
                # 未传时间时，只有同一货位、同一 SKU 才可沿用旧时间；否则视为新库存。
                previous_time = previous_fifo_times.get((position.get_position_id(), str(sku_id)))
                supplied_time = sku_dict.get("inboundTime")
                if supplied_time is None:
                    supplied_time = sku_dict.get("arrivalTime")
                if supplied_time is None:
                    supplied_time = previous_time
                sku_features["_inbound_time"] = self._core.inventory_manager.resolve_inbound_time(supplied_time)

                if position.is_double_layer:
                    shelf_str = str(shelf).upper() if shelf else None
                    if shelf_str and "UPPER" in shelf_str:
                        position.upper_sku = sku_id
                        position.upper_quantity = quantity
                        position.upper_features = sku_features
                    elif shelf_str and "LOWER" in shelf_str:
                        position.lower_sku = sku_id
                        position.lower_quantity = quantity
                        position.lower_features = sku_features
                    else:
                        if position.upper_quantity == 0:
                            position.upper_sku = sku_id
                            position.upper_quantity = quantity
                            position.upper_features = sku_features
                        else:
                            position.lower_sku = sku_id
                            position.lower_quantity = quantity
                            position.lower_features = sku_features
                else:
                    position.sku = sku_id
                    position.quantity = quantity
                    position.features = sku_features

        self._rebuild_inventory_views()

    def _clear_inventory_only(self) -> None:
        """清理库存 only相关逻辑。

        Args:
            self: 当前对象实例。

        Returns:
            None: 处理后的结果。
        """
        for position in self._core.inventory_manager.inventory_positions:
            if position.is_double_layer:
                position.upper_sku = None
                position.upper_quantity = 0
                position.upper_features = None
                position.lower_sku = None
                position.lower_quantity = 0
                position.lower_features = None
            else:
                position.sku = ""
                position.quantity = 0
                position.features = None

        self._core.inventory_manager.current_inventory = {aisle: {} for aisle in self._core.aisles}
        self._core.inventory_manager.sku_position_index = {}
        self._core.sku_types = []
        self._core.inventory_manager.sku_types = []

    def _clear_all_inventory(self) -> None:
        """清理all 库存相关逻辑。

        Args:
            self: 当前对象实例。

        Returns:
            None: 处理后的结果。
        """
        self._clear_inventory_only()
        self._core.running_tasks.clear()
        self._core.completed_tasks.clear()
        self._core.pending_outbound_queue.clear()
        for aisle in list(self._core.pending_inbound_by_aisle.keys()):
            self._core.pending_inbound_by_aisle[aisle].clear()
        self._pending_execution_tasks.clear()
        for aisle in self._core.aisles:
            self._core.current_position_by_aisle[aisle] = None

    def is_aisle_available(self, aisle_id: int) -> bool:
        """判断巷道 可用相关逻辑。

        Args:
            self: 当前对象实例。
            aisle_id: 目标巷道编号。

        Returns:
            bool: 判断结果。
        """
        return self._aisle_availability.get(aisle_id, {}).get("is_available", True)

    def _is_path_available(self, aisle_id: int, line_ref: Any, direction: str) -> bool:
        """判断path 可用相关逻辑。

        Args:
            self: 当前对象实例。
            aisle_id: 目标巷道编号。
            line_ref: 用于本函数处理的 `line_ref` 参数。
            direction: 用于本函数处理的 `direction` 参数。

        Returns:
            bool: 判断结果。
        """
        if not self.is_aisle_available(aisle_id):
            return False
        direction_norm = self._normalize_direction(direction)
        if direction_norm not in ("in", "out"):
            return True
        cfg = self._aisle_availability.get(aisle_id, {})
        dock_cfg = cfg.get("dock_availability", {}) or {}
        dir_map = dock_cfg.get(direction_norm, {}) or {}
        line_key = self._normalize_line_ref_key(line_ref, aisle_id=aisle_id, direction=direction_norm)
        if line_key is None:
            return True
        if line_key in dir_map:
            return bool(dir_map[line_key])
        # 回退处理：一侧使用数字、另一侧使用标识字符串时，尝试从字符串中提取数字。
        line_num = self._to_int_or_none(line_key)
        if line_num is not None and line_num in dir_map:
            return bool(dir_map[line_num])
        return True

    def is_inbound_path_available(self, aisle_id: int, in_line: Any) -> bool:
        """判断入库 path 可用相关逻辑。

        Args:
            self: 当前对象实例。
            aisle_id: 目标巷道编号。
            in_line: 用于本函数处理的 `in_line` 参数。

        Returns:
            bool: 判断结果。
        """
        return self._is_path_available(aisle_id, in_line, "inbound")

    def is_outbound_path_available(self, aisle_id: int, out_line: Any) -> bool:
        """判断出库 path 可用相关逻辑。

        Args:
            self: 当前对象实例。
            aisle_id: 目标巷道编号。
            out_line: 用于本函数处理的 `out_line` 参数。

        Returns:
            bool: 判断结果。
        """
        return self._is_path_available(aisle_id, out_line, "outbound")

    def convert_schedule_tasks(self, tasks: List[Any]) -> Tuple[List[TaskData], List[TaskData]]:
        """将接口任务转换为内部入库和出库任务，并补齐计划上下文。

        Args:
            self: 当前对象实例。
            tasks: mixed 请求中的原始任务模型或字典集合。

        Returns:
            Tuple[List[TaskData], List[TaskData]]: 内部入库任务列表和出库任务列表。
        """
        # inbound_tasks / outbound_tasks 是转换完成后准备进入不同 pending 队列的内部任务。
        inbound_tasks: List[TaskData] = []
        outbound_tasks: List[TaskData] = []
        # 两个计数器记录同次请求中 ADD 计划任务和仅传任务的自动 planIndex 接续值。
        add_plan_auto_index_counter: Dict[str, int] = {}
        task_only_plan_auto_index_counter: Dict[int, int] = {}
        # line_task_group_counts 是每条产线现有真实任务组数，用于新任务组号接续。
        line_task_group_counts = self._build_line_task_group_counts()

        # 每个外部任务独立规范化；计划索引计数器保证同次请求中的任务组连续。
        for task in tasks:
            def _get_field(obj: Any, name: str, default: Any = None) -> Any:
                """获取field相关逻辑。

                Args:
                    obj: 用于本函数处理的 `obj` 参数。
                    name: 用于本函数处理的 `name` 参数。
                    default: 用于本函数处理的 `default` 参数。

                Returns:
                    Any: 处理后的结果。
                """
                if isinstance(obj, dict):
                    return obj.get(name, default)
                return getattr(obj, name, default)

            # task_id / task_type / skus 是外部任务的标识、方向和 SKU 请求明细。
            task_id = _get_field(task, "taskId")
            task_type = _get_field(task, "taskType")
            skus = _get_field(task, "skus", [])

            # 将 Pydantic 模型、字典等多种 SKU 输入归一为内部字典，统一特征字段。
            # sku_list 是统一为字典与标准字段名后的内部 SKU 明细。
            sku_list = []
            for sku in skus:
                sku_dict = self._sku_entry_to_dict(sku)
                sku_dict["skuId"] = sku_dict.get("skuId") or sku_dict.get("sku")
                sku_dict["quantity"] = sku_dict.get("quantity", 1)
                sku_list.append(sku_dict)

            # 入库保留目标巷道/入出库口和显式货位；出库在下方根据计划映射组号。
            if "INBOUND" in str(task_type).upper():
                target_aisle = _get_field(task, "targetAisle")
                in_line_raw = _get_field(task, "inLine")
                out_line_raw = _get_field(task, "outLine")
                production_line_raw = _get_field(task, "productionLine")
                explicit_positions = _get_field(task, "positions", [])
                in_line = self._parse_line_ref(in_line_raw)
                out_line = self._parse_line_ref(out_line_raw)
                production_line = int(production_line_raw) if production_line_raw is not None else 1
                task_data = TaskData(
                    task_id=task_id,
                    task_type=TASK_TYPE_INBOUND,
                    task_name=task_id,
                    skus=sku_list,
                    assigned_aisle=int(target_aisle) if target_aisle else None,
                    in_line=(in_line if in_line is not None else 1),
                    out_line=out_line,
                    production_line=production_line,
                )
                if explicit_positions:
                    aisle_for_positions = int(target_aisle) if target_aisle else None
                    parsed_positions = self._normalize_feedback_positions(explicit_positions, aisle_for_positions)
                    validate_error = self._validate_feedback_positions_for_task(task_data, parsed_positions)
                    if validate_error:
                        raise ValueError(f"任务 {task_id} 的 positions 无效：{validate_error}")
                    task_data.positions = list(parsed_positions)
                    if parsed_positions and task_data.assigned_aisle is None:
                        task_data.assigned_aisle = int(parsed_positions[0].aisle)
                inbound_tasks.append(task_data)
                inbound_tasks[-1].task_record = dict(getattr(inbound_tasks[-1], "task_record", {}) or {})
                inbound_tasks[-1].task_record["target_aisle_explicit"] = bool(target_aisle)
                continue

            plan_id = _get_field(task, "planId")
            plan_index = _get_field(task, "planIndex")
            out_line = _get_field(task, "outLine")
            production_line_raw = _get_field(task, "productionLine")
            out_line = self._parse_line_ref(out_line)
            production_line = int(production_line_raw) if production_line_raw is not None else None
            if plan_id:
                mapped_line_id = self._plan_id_to_line_id.get(str(plan_id))
                if mapped_line_id is not None:
                    production_line = int(mapped_line_id)
                plan_str = str(plan_id).upper()
                if production_line is None and "LINE" in plan_str:
                    # Extract only the production line number after "LINE",
                    # 避免把 PLAN-LINE1-20260121 这类标识中的日期数字误当成产线号。
                    m = re.search(r"LINE[-_]?(\d+)", plan_str)
                    if m:
                        production_line = int(m.group(1))
                elif production_line is None and str(plan_id).isdigit():
                    production_line = int(plan_id)
            if production_line is None and sku_list:
                first_sku = sku_list[0].get("skuId", "")
                pl_value = self._core.sku_to_production_line.get(first_sku, 1)
                first_line = pl_value[0] if isinstance(pl_value, list) and pl_value else pl_value
                production_line = self._to_int_or_none(first_line) or 1
            if production_line is None:
                production_line = self._to_int_or_none(out_line)
            if production_line is None and plan_id:
                production_line = self._to_int_or_none(plan_id)
            if production_line is None:
                production_line = 1

            task_data = TaskData(task_id=task_id, task_type=TASK_TYPE_OUTBOUND, task_name=task_id, skus=sku_list, production_line=production_line or 1)
            explicit_positions = _get_field(task, "positions", [])
            if out_line is not None:
                task_data.out_line = out_line
            task_data.plan_id = plan_id
            normalized_plan_index = None
            has_plan_context = plan_id is not None or plan_index is not None
            if plan_index is None and plan_id is not None:
                plan_id_key = str(plan_id)
                if any(k[0] == plan_id_key for k in self._add_plan_index_alias.keys()):
                    next_local_idx = add_plan_auto_index_counter.get(plan_id_key, 1)
                    plan_index = next_local_idx
                    add_plan_auto_index_counter[plan_id_key] = next_local_idx + 1
            if plan_index is None and has_plan_context:
                next_local_idx = task_only_plan_auto_index_counter.get(int(production_line), 1)
                plan_index = next_local_idx
                task_only_plan_auto_index_counter[int(production_line)] = next_local_idx + 1
            if plan_index is not None and has_plan_context:
                try:
                    plan_index_int = int(plan_index)
                    if plan_id is not None:
                        mapped = self._add_plan_index_alias.get((str(plan_id), plan_index_int))
                        if mapped is not None:
                            normalized_plan_index = int(mapped)
                        else:
                            base_group_count = int(line_task_group_counts.get(int(production_line), 0))
                            normalized_plan_index = base_group_count + plan_index_int
                    else:
                        base_group_count = int(line_task_group_counts.get(int(production_line), 0))
                        normalized_plan_index = base_group_count + plan_index_int
                except (TypeError, ValueError):
                    normalized_plan_index = None
            task_data.group_idx = (int(normalized_plan_index) - 1) if normalized_plan_index is not None else None
            if explicit_positions:
                parsed_positions = self._normalize_feedback_positions(explicit_positions, None)
                validate_error = self._validate_feedback_positions_for_task(task_data, parsed_positions)
                if validate_error:
                    raise ValueError(f"任务 {task_id} 的 positions 无效：{validate_error}")
                task_data.positions = list(parsed_positions)
                if parsed_positions and getattr(task_data, "assigned_aisle", None) is None:
                    task_data.assigned_aisle = int(parsed_positions[0].aisle)
            if self._is_empty_skid_request(sku_list):
                # 空托盘出库属于临时作业请求，不是生产计划步骤，因此不绑定生产计划组推进。
                task_data.plan_id = None
                task_data.group_idx = None
                task_data.task_record = {
                    "empty_skid_request": True,
                    "high_priority": True,
                }
            outbound_tasks.append(task_data)

        return inbound_tasks, outbound_tasks

    def _find_positions_for_outbound_task(
        self,
        task: TaskData,
        preferred_aisle: Optional[int] = None,
    ) -> Optional[List[InventoryPosition]]:
        """查找positions for 出库 任务相关逻辑。

        Args:
            self: 当前对象实例。
            task: 待处理的任务对象。
            preferred_aisle: 用于本函数处理的 `preferred_aisle` 参数。

        Returns:
            Optional[List[InventoryPosition]]: 处理后的结果。
        """
        positions, _, _ = self._find_outbound_positions_with_occupancy(
            task,
            preferred_aisle=preferred_aisle,
            occupied_units=None,
        )
        return positions

    def _get_task_sku_attr_pairs(self, task: TaskData) -> List[Tuple[str, Dict[str, Any]]]:
        """获取任务 sku attr pairs相关逻辑。

        Args:
            self: 当前对象实例。
            task: 待处理的任务对象。

        Returns:
            List[Tuple[str, Dict[str, Any]]]: 处理后的结果。
        """
        sku_ids = task.get_sku_ids() if hasattr(task, "get_sku_ids") else []
        pairs: List[Tuple[str, Dict[str, Any]]] = []
        match_mode = self._core._get_outbound_match_mode(getattr(task, "production_line", None) or 1)
        if task.skus:
            for raw in (task.skus or []):
                sku_dict = self._sku_entry_to_dict(raw)
                sku_id = str(sku_dict.get("skuId") or sku_dict.get("sku") or "").strip()
                quantity = int(sku_dict.get("quantity", 1) or 1)
                features = self._extract_sku_features(sku_dict, self._get_match_fields(getattr(task, "production_line", None)))
                if not sku_id and not (match_mode == "features" and features):
                    continue
                for _ in range(max(1, quantity)):
                    pairs.append((sku_id, features))
        if pairs:
            return pairs
        for sku_id in sku_ids:
            if sku_id:
                pairs.append((str(sku_id), {}))
        return pairs

    def _get_outbound_candidate_positions(
        self,
        task: TaskData,
        preferred_aisle: Optional[int] = None,
    ) -> List[List[InventoryPosition]]:
        """获取出库 candidate positions相关逻辑。

        Args:
            self: 当前对象实例。
            task: 待处理的任务对象。
            preferred_aisle: 用于本函数处理的 `preferred_aisle` 参数。

        Returns:
            List[List[InventoryPosition]]: 处理后的结果。
        """
        sku_pairs = self._get_task_sku_attr_pairs(task)
        if not sku_pairs:
            return []

        production_line = getattr(task, "production_line", None) or 1
        out_line = getattr(task, "out_line", None) or production_line
        match_mode = self._core._get_outbound_match_mode(production_line)
        feature_keys = self._get_match_fields(production_line)

        def can_use(pos: InventoryPosition) -> bool:
            """判断是否可以use相关逻辑。

            Args:
                pos: 用于本函数处理的 `pos` 参数。

            Returns:
                bool: 判断结果。
            """
            return (
                (preferred_aisle is None or int(pos.aisle) == int(preferred_aisle))
                and self.is_outbound_path_available(pos.aisle, out_line)
                and not self._core.check_blockage(pos.aisle, out_line, current_time=self._core.current_time)
            )

        def sort_by_fifo(candidates: List[List[InventoryPosition]]) -> List[List[InventoryPosition]]:
            """在启用 FIFO 的非 RFID 产线中，按最早入库库存排序候选组合。

            Args:
                candidates: 已完成匹配、路径和禁用状态过滤的候选货位组合。

            Returns:
                List[List[InventoryPosition]]: FIFO 未启用时保持原顺序；启用时最早入库组合在前。
            """
            if not self._core.is_outbound_fifo_enabled(production_line):
                return candidates

            def fifo_key(positions: List[InventoryPosition]) -> Tuple[float, Tuple[Tuple[int, int, int, int], ...]]:
                # 车身库单层任务只有一个位置；保留组合处理以兼容共享的双层接口。
                # _inbound_time 在库存写入时已统一转换为数值秒；缺失的历史库存排到末尾。
                def position_inbound_time(position: InventoryPosition) -> float:
                    features = getattr(position, "features", {}) or {}
                    raw_time = features.get("_inbound_time") if isinstance(features, dict) else None
                    return float(raw_time) if isinstance(raw_time, (int, float)) else float("inf")

                inbound_time = min(
                    position_inbound_time(position)
                    for position in positions
                )
                coordinates = tuple(
                    (int(position.aisle), int(position.row), int(position.column), int(position.level))
                    for position in positions
                )
                return inbound_time, coordinates

            return sorted(candidates, key=fifo_key)

        if len(sku_pairs) == 1:
            sku, feats = sku_pairs[0]
            if match_mode == "features" and feats:
                single_candidates = self._core.inventory_manager.get_positions_by_features(feats, feature_keys, only_available=True)
            else:
                single_candidates = self._core.inventory_manager.get_sku_positions(sku, only_available=True)
            return sort_by_fifo([[p] for p in single_candidates if can_use(p)])

        sku1, feats1 = sku_pairs[0]
        sku2, feats2 = sku_pairs[1]
        candidates: List[List[InventoryPosition]] = []

        for pos in self._core.inventory_manager.inventory_positions:
            if not pos.is_double_layer or not can_use(pos):
                continue
            if match_mode == "features" and feats1 and feats2:
                up1 = (
                    pos.upper_quantity > 0
                    and self._core.inventory_manager._features_match(pos.upper_features, feats1, feature_keys)
                )
                low2 = (
                    pos.lower_quantity > 0
                    and self._core.inventory_manager._features_match(pos.lower_features, feats2, feature_keys)
                )
                up2 = (
                    pos.upper_quantity > 0
                    and self._core.inventory_manager._features_match(pos.upper_features, feats2, feature_keys)
                )
                low1 = (
                    pos.lower_quantity > 0
                    and self._core.inventory_manager._features_match(pos.lower_features, feats1, feature_keys)
                )
                if (up1 and low2) or (up2 and low1):
                    candidates.append([pos])
            else:
                has1 = (pos.upper_sku == sku1 and pos.upper_quantity > 0) or (pos.lower_sku == sku1 and pos.lower_quantity > 0)
                has2 = (pos.upper_sku == sku2 and pos.upper_quantity > 0) or (pos.lower_sku == sku2 and pos.lower_quantity > 0)
                if has1 and has2:
                    candidates.append([pos])

        p1 = self._core.inventory_manager.get_positions_by_features(feats1, feature_keys, only_available=True) if (match_mode == "features" and feats1) else self._core.inventory_manager.get_sku_positions(sku1, only_available=True)
        p2 = self._core.inventory_manager.get_positions_by_features(feats2, feature_keys, only_available=True) if (match_mode == "features" and feats2) else self._core.inventory_manager.get_sku_positions(sku2, only_available=True)
        for a in p1:
            if not can_use(a):
                continue
            for b in p2:
                if not can_use(b):
                    continue
                if self._position_key(a) == self._position_key(b):
                    continue
                candidates.append([a, b])
        return sort_by_fifo(candidates)

    def _position_unit_key(self, position: InventoryPosition, layer: Optional[str] = None) -> Tuple[Any, ...]:
        """执行 货位 unit key 对应的业务处理。

        Args:
            self: 当前对象实例。
            position: 货位对象或货位描述。
            layer: 用于本函数处理的 `layer` 参数。

        Returns:
            Tuple[Any, ...]: 处理后的结果。
        """
        base = self._position_key(position)
        if getattr(position, "is_double_layer", False):
            return (*base, str(layer or "").upper() or "BOTH")
        return (*base, "SINGLE")

    def _match_position_layer(
        self,
        position: InventoryPosition,
        sku_id: str,
        features: Dict[str, Any],
        production_line: Optional[int],
        used_layers: Optional[Set[str]] = None,
    ) -> Optional[str]:
        """执行 匹配 货位 layer 对应的业务处理。

        Args:
            self: 当前对象实例。
            position: 货位对象或货位描述。
            sku_id: 用于本函数处理的 `sku_id` 参数。
            features: 用于本函数处理的 `features` 参数。
            production_line: 生产线编号。
            used_layers: 用于本函数处理的 `used_layers` 参数。

        Returns:
            Optional[str]: 处理后的结果。
        """
        used_layers = used_layers or set()
        feature_keys = self._get_match_fields(production_line)
        match_mode = self._core._get_outbound_match_mode(production_line)

        if not getattr(position, "is_double_layer", False):
            if position.quantity <= 0:
                return None
            if match_mode == "features" and features:
                if not self._core.inventory_manager._features_match(position.features, features, feature_keys):
                    return None
            elif str(position.sku or "") != str(sku_id):
                return None
            return "SINGLE"

        layer_specs = [
            ("UPPER", getattr(position, "upper_sku", None), getattr(position, "upper_quantity", 0), getattr(position, "upper_features", None)),
            ("LOWER", getattr(position, "lower_sku", None), getattr(position, "lower_quantity", 0), getattr(position, "lower_features", None)),
        ]
        for layer_name, layer_sku, qty, layer_features in layer_specs:
            if layer_name in used_layers:
                continue
            if qty <= 0:
                continue
            if match_mode == "features" and features:
                if not self._core.inventory_manager._features_match(layer_features, features, feature_keys):
                    continue
            elif str(layer_sku or "") != str(sku_id):
                continue
            return layer_name
        return None

    def _get_position_units_for_task(
        self,
        task: TaskData,
        positions: List[InventoryPosition],
    ) -> Optional[List[Tuple[Tuple[Any, ...], Optional[InventoryPosition], Optional[str]]]]:
        """获取货位 units for 任务相关逻辑。

        Args:
            self: 当前对象实例。
            task: 待处理的任务对象。
            positions: 货位对象或货位描述集合。

        Returns:
            Optional[List[Tuple[Tuple[Any, ...], Optional[InventoryPosition], Optional[str]]]]: 处理后的结果。
        """
        sku_pairs = self._get_task_sku_attr_pairs(task)
        if not sku_pairs:
            return None

        units: List[Tuple[Tuple[Any, ...], Optional[InventoryPosition], Optional[str]]] = []
        used_layers_by_pos: Dict[Tuple[int, int, int, int], Set[str]] = {}
        if len(positions) == 1:
            pos = positions[0]
            pos_key = self._position_key(pos)
            used_layers_by_pos.setdefault(pos_key, set())
            for sku_id, feats in sku_pairs:
                layer = self._match_position_layer(pos, sku_id, feats, getattr(task, "production_line", None), used_layers_by_pos[pos_key])
                if layer is None:
                    return None
                if layer not in ("SINGLE", None):
                    used_layers_by_pos[pos_key].add(layer)
                units.append((self._position_unit_key(pos, layer), pos, layer))
            return units

        for idx, (sku_id, feats) in enumerate(sku_pairs):
            if idx >= len(positions):
                return None
            pos = positions[idx]
            pos_key = self._position_key(pos)
            used_layers_by_pos.setdefault(pos_key, set())
            layer = self._match_position_layer(pos, sku_id, feats, getattr(task, "production_line", None), used_layers_by_pos[pos_key])
            if layer is None:
                return None
            if layer not in ("SINGLE", None):
                used_layers_by_pos[pos_key].add(layer)
            units.append((self._position_unit_key(pos, layer), pos, layer))
        return units

    def _reserve_outbound_task_units(
        self,
        occupied_units: Dict[Tuple[Any, ...], str],
        task: TaskData,
        positions: List[InventoryPosition],
    ) -> Optional[str]:
        """执行 reserve 出库 任务 units 对应的业务处理。

        Args:
            self: 当前对象实例。
            occupied_units: 用于本函数处理的 `occupied_units` 参数。
            task: 待处理的任务对象。
            positions: 货位对象或货位描述集合。

        Returns:
            Optional[str]: 处理后的结果。
        """
        units = self._get_position_units_for_task(task, positions)
        if not units:
            return None
        task_id = str(getattr(task, "task_id", "") or "")
        for unit_key, _, _ in units:
            existing_task_id = occupied_units.get(unit_key)
            if existing_task_id and existing_task_id != task_id:
                return existing_task_id
        for unit_key, _, _ in units:
            occupied_units[unit_key] = task_id
        return None

    def _collect_submitted_outbound_occupancy(self) -> Dict[Tuple[Any, ...], str]:
        """汇总已提交出库任务对库存资源单元的虚拟占用。

        Args:
            self: 当前对象实例。

        Returns:
            Dict[Tuple[Any, ...], str]: 处理后的结果。
        """
        occupied_units: Dict[Tuple[Any, ...], str] = {}
        submitted_tasks: List[TaskData] = []
        # running、已下发待确认和 pending 出库均已承诺库存，后续任务不得复用其货位。
        submitted_tasks.extend(
            t for t in self._core.running_tasks.values()
            if str(getattr(t, "task_type", "") or "") == TASK_TYPE_OUTBOUND
        )
        submitted_tasks.extend(
            t for t in self._pending_execution_tasks.values()
            if str(getattr(t, "task_type", "") or "") == TASK_TYPE_OUTBOUND
        )
        submitted_tasks.extend(
            t for t in self._core.pending_outbound_queue
            if str(getattr(t, "task_type", "") or "") == TASK_TYPE_OUTBOUND
        )
        # 仅已固化货位的任务形成占用；无货位任务仍由后续匹配流程处理。
        for task in submitted_tasks:
            positions = list(getattr(task, "positions", None) or [])
            if not positions:
                continue
            self._reserve_outbound_task_units(occupied_units, task, positions)
        return occupied_units

    def _find_outbound_positions_with_occupancy(
        self,
        task: TaskData,
        *,
        preferred_aisle: Optional[int] = None,
        occupied_units: Optional[Dict[Tuple[Any, ...], str]] = None,
    ) -> Tuple[Optional[List[InventoryPosition]], Optional[str], List[str]]:
        """在已承诺库存之外，为出库任务查找无冲突的候选货位。

        Args:
            self: 当前对象实例。
            task: 待处理的任务对象。
            preferred_aisle: 用于本函数处理的 `preferred_aisle` 参数。
            occupied_units: 用于本函数处理的 `occupied_units` 参数。

        Returns:
            Tuple[Optional[List[InventoryPosition]], Optional[str], List[str]]: 处理后的结果。
        """
        candidate_positions = self._get_outbound_candidate_positions(task, preferred_aisle=preferred_aisle)
        if not candidate_positions:
            return None, "库存不足", []

        occupied_units = occupied_units or {}
        conflict_task_ids: List[str] = []
        seen_conflicts: Set[str] = set()

        # 按候选优先级试探资源单元；首个没有被其他任务预占的候选即为可提交结果。
        for positions in candidate_positions:
            units = self._get_position_units_for_task(task, positions)
            if not units:
                continue
            conflict_task_id: Optional[str] = None
            for unit_key, _, _ in units:
                existing_task_id = occupied_units.get(unit_key)
                if existing_task_id and existing_task_id != str(getattr(task, "task_id", "") or ""):
                    conflict_task_id = existing_task_id
                    break
            if conflict_task_id is None:
                return list(positions), None, []
            if conflict_task_id not in seen_conflicts:
                seen_conflicts.add(conflict_task_id)
                conflict_task_ids.append(conflict_task_id)

        if conflict_task_ids:
            return None, "已提交出库任务占用导致当前优先级资源不足", conflict_task_ids
        return None, "库存不足", []

    def _build_unsubmitted_outbound_item(
        self,
        task: TaskData,
        reason: str,
        blocked_by_task_id: Optional[str] = None,
        conflict_task_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """构建unsubmitted 出库 item相关逻辑。

        Args:
            self: 当前对象实例。
            task: 待处理的任务对象。
            reason: 用于本函数处理的 `reason` 参数。
            blocked_by_task_id: 用于本函数处理的 `blocked_by_task_id` 参数。
            conflict_task_ids: 用于本函数处理的 `conflict_task_ids` 参数。

        Returns:
            Dict[str, Any]: 处理后的结果。
        """
        payload = self._task_brief_dict(task)
        payload["productionLine"] = getattr(task, "production_line", None)
        payload["reason"] = reason
        if blocked_by_task_id:
            payload["blockedByTaskId"] = blocked_by_task_id
        if conflict_task_ids:
            payload["conflictTaskIds"] = list(conflict_task_ids)
        return payload

    def _prepare_outbound_tasks_for_submission(
        self,
        outbound_tasks: List[TaskData],
    ) -> Tuple[List[TaskData], List[Dict[str, Any]]]:
        """按生产线和任务组试提交出库任务，并返回可入队及未提交任务。

        Args:
            self: 当前对象实例。
            outbound_tasks: 用于本函数处理的 `outbound_tasks` 参数。

        Returns:
            Tuple[List[TaskData], List[Dict[str, Any]]]: 处理后的结果。
        """
        ready_outbound: List[TaskData] = []
        unsubmitted_outbound: List[Dict[str, Any]] = []
        blocked_lines: Dict[int, str] = {}
        occupied_units = self._collect_submitted_outbound_occupancy()
        grouped_tasks: List[List[TaskData]] = []
        group_index_map: Dict[Tuple[int, Optional[int]], int] = {}

        # 先按“产线 + 内部组号”保持请求顺序分组，确保同组任务做原子可行性检查。
        for task in outbound_tasks:
            production_line = int(getattr(task, "production_line", 1) or 1)
            group_idx = getattr(task, "group_idx", None)
            group_key: Tuple[int, Optional[int]]
            if group_idx is None:
                group_key = (production_line, None if not grouped_tasks else None)
                grouped_tasks.append([task])
                continue
            group_key = (production_line, int(group_idx))
            group_pos = group_index_map.get(group_key)
            if group_pos is None:
                group_index_map[group_key] = len(grouped_tasks)
                grouped_tasks.append([task])
            else:
                grouped_tasks[group_pos].append(task)

        # 逐组在临时占用视图中试探，只有整组成功才提交其占用到全局视图。
        for group in grouped_tasks:
            first_task = group[0]
            production_line = int(getattr(first_task, "production_line", 1) or 1)
            blocking_task_id = blocked_lines.get(production_line)
            # 同产线前一组已因库存不足被阻断时，后续组不再抢占库存。
            if blocking_task_id:
                for task in group:
                    reason = f"产线 {production_line} 的任务 {blocking_task_id} 当前无对应库存，本任务未提交。"
                    unsubmitted_outbound.append(
                        self._build_unsubmitted_outbound_item(
                            task,
                            reason=reason,
                            blocked_by_task_id=blocking_task_id,
                        )
                    )
                continue

            temp_occupied_units = dict(occupied_units)
            prepared_group: List[TaskData] = []
            group_failed = False
            failure_reason: Optional[str] = None
            failure_conflicts: List[str] = []
            failure_task_id: Optional[str] = None

            # 在组内按任务顺序找货位并预占；任何一项失败都会撤销本组临时结果。
            for task in group:
                resolved_task = task
                if bool((getattr(task, "task_record", {}) or {}).get("empty_skid_request")):
                    resolved_task = self._resolve_empty_skid_outbound(task) or task

                positions = list(getattr(resolved_task, "positions", None) or [])
                if not positions:
                    positions, reason, conflict_task_ids = self._find_outbound_positions_with_occupancy(
                        resolved_task,
                        occupied_units=temp_occupied_units,
                    )
                    if not positions:
                        group_failed = True
                        failure_reason = reason or "库存不足"
                        failure_conflicts = list(conflict_task_ids or [])
                        failure_task_id = str(getattr(resolved_task, "task_id", "") or "")
                        blocked_lines[production_line] = failure_task_id
                        unsubmitted_outbound.append(
                            self._build_unsubmitted_outbound_item(
                                resolved_task,
                                reason=f"任务 {failure_task_id} 当前无对应库存，未提交；该产线后续出库任务一并拦截。"
                                if failure_reason == "库存不足"
                                else f"任务 {failure_task_id} 因{failure_reason}，未提交；该产线后续出库任务一并拦截。",
                                conflict_task_ids=failure_conflicts,
                            )
                        )
                        break
                    resolved_task.positions = list(positions)

                conflict_task_id = self._reserve_outbound_task_units(temp_occupied_units, resolved_task, list(getattr(resolved_task, "positions", None) or []))
                if conflict_task_id:
                    group_failed = True
                    failure_reason = "已提交出库任务占用导致当前优先级资源不足"
                    failure_conflicts = [conflict_task_id]
                    failure_task_id = str(getattr(resolved_task, "task_id", "") or "")
                    blocked_lines[production_line] = failure_task_id
                    unsubmitted_outbound.append(
                        self._build_unsubmitted_outbound_item(
                            resolved_task,
                            reason=f"任务 {failure_task_id} 因{failure_reason}，未提交；该产线后续出库任务一并拦截。",
                            conflict_task_ids=failure_conflicts,
                        )
                    )
                    break

                prepared_group.append(resolved_task)

            if group_failed:
                # 失败组不写入 pending，同时为同组其余任务补充可追溯的阻断原因。
                remaining_tasks = [t for t in group if str(getattr(t, "task_id", "") or "") != str(failure_task_id or "")]
                for task in remaining_tasks:
                    unsubmitted_outbound.append(
                        self._build_unsubmitted_outbound_item(
                            task,
                            reason="同组出库任务库存不足",
                            blocked_by_task_id=failure_task_id,
                            conflict_task_ids=failure_conflicts,
                        )
                    )
                continue

            # 仅整组可行时才提交虚拟库存占用，防止半组任务占用资源。
            occupied_units = temp_occupied_units
            ready_outbound.extend(prepared_group)

        return ready_outbound, unsubmitted_outbound

    def allocate_inbound_aisle(
        self,
        task_id: str,
        skus: List[Dict],
        in_line: Any = 1,
        out_line: Any = None,
        production_line: Any = None,
    ) -> int:
        """执行 allocate 入库 巷道 对应的业务处理。

        Args:
            self: 当前对象实例。
            task_id: 任务唯一标识。
            skus: 用于本函数处理的 `skus` 参数。
            in_line: 用于本函数处理的 `in_line` 参数。
            out_line: 用于本函数处理的 `out_line` 参数。
            production_line: 生产线编号。

        Returns:
            int: 处理后的结果。
        """
        self._sync_time()
        production_line_val = int(production_line) if production_line is not None else None
        if production_line_val is None:
            for sku in (skus or []):
                sku_id = self._sku_entry_to_dict(sku).get("skuId")
                if not sku_id:
                    continue
                pl_value = self._core.sku_to_production_line.get(sku_id)
                if isinstance(pl_value, list):
                    production_line_val = int(pl_value[0]) if pl_value else None
                elif pl_value is not None:
                    production_line_val = int(pl_value)
                if production_line_val:
                    break

        in_line_norm = self._parse_line_ref(in_line)
        out_line_norm = self._parse_line_ref(out_line)
        stub = type(
            "TaskStub",
            (),
            {
                "skus": skus,
                "in_line": (in_line_norm if in_line_norm is not None else 1),
                "out_line": out_line_norm,
                "assigned_aisle": None,
                "production_line": production_line_val,
            },
        )()
        valid_aisles = self._core._get_valid_inbound_aisles(stub, production_line_val)
        valid_aisles = [a for a in valid_aisles if self.is_inbound_path_available(a, in_line_norm)]
        valid_with_capacity = [a for a in valid_aisles if self._core._get_projected_free_slots(a) > 0]
        if self._core.inbound_aisle_allocator is not None:
            try:
                aisle = self._core.inbound_aisle_allocator.allocate(stub, self._core.inventory_manager.inventory_positions)
                if aisle:
                    aisle = int(aisle)
                    if aisle in valid_aisles:
                        return aisle
            except Exception:
                pass

        if valid_with_capacity:
            return min(valid_with_capacity, key=lambda a: len(self._core.pending_inbound_by_aisle.get(a, [])))
        if valid_aisles:
            return min(valid_aisles, key=lambda a: len(self._core.pending_inbound_by_aisle.get(a, [])))
        if production_line_val is not None:
            pl_aisles = [a for a in self._core.aisles if production_line_val in self._core.aisle_production_line_mapping.get(a, [])]
            pl_aisles = [a for a in pl_aisles if self.is_aisle_available(a)]
            if pl_aisles:
                return random.choice(pl_aisles)
        return random.choice([a for a in self._core.aisles if self.is_aisle_available(a)] or self._core.aisles)

    def _normalize_feedback_positions(
        self,
        positions: Any,
        aisle_id: Optional[int],
        default_aisle_id: Optional[int] = None,
    ) -> List[InventoryPosition]:
        """标准化反馈 positions相关逻辑。

        Args:
            self: 当前对象实例。
            positions: 货位对象或货位描述集合。
            aisle_id: 目标巷道编号。
            default_aisle_id: 用于本函数处理的 `default_aisle_id` 参数。

        Returns:
            List[InventoryPosition]: 处理后的结果。
        """
        normalized: List[InventoryPosition] = []
        for pos in positions or []:
            pos_aisle_val = self._to_int_or_none(pos.aisleId if hasattr(pos, "aisleId") else pos.get("aisleId"))
            aisle_val = aisle_id
            if aisle_val is None:
                aisle_val = pos_aisle_val if pos_aisle_val is not None else default_aisle_id
            elif pos_aisle_val is not None and int(pos_aisle_val) != int(aisle_val):
                raise ValueError(
                    f"顶层 aisleId={int(aisle_val)} 与 positions 中 aisleId={int(pos_aisle_val)} 不一致。"
                )
            row = pos.row if hasattr(pos, "row") else pos.get("row")
            column = pos.column if hasattr(pos, "column") else pos.get("column")
            level = pos.level if hasattr(pos, "level") else pos.get("level")
            if aisle_val is None or row is None or column is None or level is None:
                raise ValueError("反馈位置必须包含 row、column、level；未提供 aisleId 时将沿用 mixed 分配巷道。")
            normalized.append(self._get_position_by_external_coords(int(aisle_val), int(row), int(column), int(level)))
        return normalized

    def _reserve_fixed_positions_for_aisle(self, aisle: int, exclude_task_id: Optional[str] = None) -> Dict[Tuple[int, int, int, int], bool]:
        """执行 reserve fixed positions for 巷道 对应的业务处理。

        Args:
            self: 当前对象实例。
            aisle: 目标巷道编号。
            exclude_task_id: 用于本函数处理的 `exclude_task_id` 参数。

        Returns:
            Dict[Tuple[int, int, int, int], bool]: 处理后的结果。
        """
        original_reserved: Dict[Tuple[int, int, int, int], bool] = {}
        task_groups = [
            list(self._core.running_tasks.values()),
            list(self._pending_execution_tasks.values()),
            list(self._core.pending_inbound_by_aisle.get(int(aisle), []) or []),
            [t for t in self._core.pending_outbound_queue if int(getattr(t, "assigned_aisle", -1) or -1) == int(aisle)],
        ]
        for group in task_groups:
            for task in group:
                task_id = str(getattr(task, "task_id", "") or "")
                if exclude_task_id and task_id == str(exclude_task_id):
                    continue
                for pos in (getattr(task, "positions", None) or []):
                    if int(getattr(pos, "aisle", -1)) != int(aisle):
                        continue
                    key = self._position_key(pos)
                    if key not in original_reserved:
                        original_reserved[key] = bool(getattr(pos, "reserved", False))
                    pos.reserved = True
        return original_reserved

    def _restore_reserved_flags(self, original_reserved: Dict[Tuple[int, int, int, int], bool]) -> None:
        """执行 restore reserved flags 对应的业务处理。

        Args:
            self: 当前对象实例。
            original_reserved: 用于本函数处理的 `original_reserved` 参数。

        Returns:
            None: 处理后的结果。
        """
        if not original_reserved:
            return
        for pos in self._core.inventory_manager.inventory_positions:
            key = self._position_key(pos)
            if key in original_reserved:
                pos.reserved = bool(original_reserved[key])

    def _allocate_feedback_positions_for_aisle(
        self,
        task: TaskData,
        aisle: int,
        *,
        exclude_task_id: Optional[str] = None,
    ) -> Optional[List[InventoryPosition]]:
        """执行 allocate 反馈 positions for 巷道 对应的业务处理。

        Args:
            self: 当前对象实例。
            task: 待处理的任务对象。
            aisle: 目标巷道编号。
            exclude_task_id: 用于本函数处理的 `exclude_task_id` 参数。

        Returns:
            Optional[List[InventoryPosition]]: 处理后的结果。
        """
        aisle_val = int(aisle)
        original_aisle = getattr(task, "assigned_aisle", None)
        task.assigned_aisle = aisle_val
        reserved_snapshot = self._reserve_fixed_positions_for_aisle(aisle_val, exclude_task_id=exclude_task_id)
        try:
            return self._find_positions_for_inbound_task(task, preferred_aisle=aisle_val) or None
        finally:
            self._restore_reserved_flags(reserved_snapshot)
            task.assigned_aisle = original_aisle

    def _position_key(self, position: InventoryPosition) -> Tuple[int, int, int, int]:
        """执行 货位 key 对应的业务处理。

        Args:
            self: 当前对象实例。
            position: 货位对象或货位描述。

        Returns:
            Tuple[int, int, int, int]: 处理后的结果。
        """
        return (
            int(getattr(position, "aisle", 0) or 0),
            int(getattr(position, "row", 0) or 0),
            int(getattr(position, "column", 0) or 0),
            int(getattr(position, "level", 0) or 0),
        )

    def _position_key_to_external(self, key: Tuple[int, int, int, int]) -> str:
        """执行 货位 key to external 对应的业务处理。

        Args:
            self: 当前对象实例。
            key: 用于本函数处理的 `key` 参数。

        Returns:
            str: 处理后的结果。
        """
        aisle, internal_row, column, level = key
        external_row = self._internal_to_external_row(int(internal_row), int(aisle))
        return f"{int(aisle)}-{int(external_row)}-{int(column)}-{int(level)}"

    def _free_slot_count(self, position: InventoryPosition) -> int:
        """执行 free slot count 对应的业务处理。

        Args:
            self: 当前对象实例。
            position: 货位对象或货位描述。

        Returns:
            int: 处理后的结果。
        """
        if getattr(position, "reserved", False) or getattr(position, "disabled", False):
            return 0
        if not position.is_double_layer:
            return 1 if position.quantity == 0 else 0
        slots = 0
        if getattr(position, "upper_quantity", 0) == 0:
            slots += 1
        if getattr(position, "lower_quantity", 0) == 0:
            slots += 1
        return slots

    def _required_sku_ids(self, task: TaskData) -> List[str]:
        """执行 required sku ids 对应的业务处理。

        Args:
            self: 当前对象实例。
            task: 待处理的任务对象。

        Returns:
            List[str]: 处理后的结果。
        """
        return [str(sku_id) for sku_id in (task.get_sku_ids() if hasattr(task, "get_sku_ids") else []) if sku_id]

    def _find_positions_for_inbound_task(
        self,
        task: TaskData,
        preferred_aisle: Optional[int] = None,
    ) -> Optional[List[InventoryPosition]]:
        """查找positions for 入库 任务相关逻辑。

        Args:
            self: 当前对象实例。
            task: 待处理的任务对象。
            preferred_aisle: 用于本函数处理的 `preferred_aisle` 参数。

        Returns:
            Optional[List[InventoryPosition]]: 处理后的结果。
        """
        aisle = preferred_aisle if preferred_aisle is not None else getattr(task, "assigned_aisle", None)
        if aisle is None:
            return None
        aisle = int(aisle)
        in_line = getattr(task, "in_line", None)
        if not self.is_aisle_available(aisle) or not self.is_inbound_path_available(aisle, in_line):
            return None
        if self._core._get_projected_free_slots(aisle) <= 0:
            return None

        original_aisle = getattr(task, "assigned_aisle", None)
        task.assigned_aisle = aisle
        try:
            allocator = getattr(self._core, "inbound_position_allocator", None)
            positions: List[InventoryPosition] = []
            if allocator is not None:
                try:
                    positions = list(allocator.allocate(self._core.inventory_manager.inventory_positions, task) or [])
                except Exception:
                    positions = []

            sku_count = len(self._required_sku_ids(task))
            if not positions:
                candidates = [
                    p for p in self._core.inventory_manager.inventory_positions
                    if int(getattr(p, "aisle", 0) or 0) == aisle and self._free_slot_count(p) > 0
                ]
                candidates.sort(key=lambda p: self._position_key(p))
                if sku_count <= 1:
                    positions = candidates[:1]
                else:
                    double_slot = next(
                        (
                            p for p in candidates
                            if getattr(p, "is_double_layer", False) and self._free_slot_count(p) >= sku_count
                        ),
                        None,
                    )
                    if double_slot is not None:
                        positions = [double_slot, double_slot]
                    elif len(candidates) >= sku_count:
                        positions = candidates[:sku_count]

            if len(positions) == 1 and sku_count == 2 and getattr(positions[0], "is_double_layer", False):
                positions = [positions[0], positions[0]]
            return positions or None
        finally:
            task.assigned_aisle = original_aisle

    def _validate_feedback_positions_for_task(
        self,
        task: TaskData,
        positions: List[InventoryPosition],
    ) -> Optional[str]:
        """校验反馈 positions for 任务相关逻辑。

        Args:
            self: 当前对象实例。
            task: 待处理的任务对象。
            positions: 货位对象或货位描述集合。

        Returns:
            Optional[str]: 处理后的结果。
        """
        if not positions:
            return None

        if task.task_type == TASK_TYPE_INBOUND:
            required_count = max(1, len(self._required_sku_ids(task)))
            position_usage: Dict[Tuple[int, int, int, int], int] = {}
            current_task_keys = {
                self._position_key(pos)
                for pos in (getattr(task, "positions", None) or [])
            }

            def free_slots_ignoring_self_reservation(pos: InventoryPosition) -> int:
                """执行 free slots ignoring self reservation 对应的业务处理。

                Args:
                    pos: 用于本函数处理的 `pos` 参数。

                Returns:
                    int: 处理后的结果。
                """
                key = self._position_key(pos)
                free_slots = self._free_slot_count(pos)
                if key not in current_task_keys or not getattr(pos, "reserved", False):
                    return free_slots
                if getattr(pos, "disabled", False):
                    return free_slots
                if not getattr(pos, "is_double_layer", False):
                    return 1 if getattr(pos, "quantity", 0) == 0 else 0
                slots = 0
                if getattr(pos, "upper_quantity", 0) == 0:
                    slots += 1
                if getattr(pos, "lower_quantity", 0) == 0:
                    slots += 1
                return slots

            for pos in positions:
                key = self._position_key(pos)
                position_usage[key] = position_usage.get(key, 0) + 1
                if getattr(pos, "disabled", False):
                    return f"货位 {pos.get_position_id()} 已禁用，不能作为入库位置。"
                if getattr(pos, "reserved", False) and key not in current_task_keys:
                    return f"货位 {pos.get_position_id()} 已被占用，不能作为入库位置。"

            if len(positions) < required_count:
                return "反馈位置数量不足，无法容纳当前入库任务。"

            for pos in positions:
                key = self._position_key(pos)
                use_count = position_usage.get(key, 0)
                if use_count > 1:
                    if not getattr(pos, "is_double_layer", False):
                        return f"单层货位 {pos.get_position_id()} 不能重复分配。"
                    if free_slots_ignoring_self_reservation(pos) < use_count:
                        return f"货位 {pos.get_position_id()} 可用层数不足，不能完成入库。"
                elif free_slots_ignoring_self_reservation(pos) < 1:
                    return f"货位 {pos.get_position_id()} 已无可用空间，不能完成入库。"
            return None

        required_rows: List[Dict[str, Any]] = []
        feature_keys = self._get_match_fields(getattr(task, "production_line", None))
        match_mode = self._core._get_outbound_match_mode(getattr(task, "production_line", None) or 1)
        for sku_entry in (task.skus or []):
            sku_dict = self._sku_entry_to_dict(sku_entry)
            req_features = self._extract_sku_features(sku_dict, feature_keys)
            sku_id = str(sku_dict.get("skuId") or "")
            if not sku_id and not (match_mode == "features" and req_features):
                continue
            required_rows.append(
                {
                    "skuId": sku_id,
                    "features": req_features,
                }
            )

        available_rows: List[Dict[str, Any]] = []
        for pos in positions:
            if pos.is_double_layer:
                if pos.lower_quantity > 0 and pos.lower_sku:
                    available_rows.append(
                        {"skuId": str(pos.lower_sku), "features": dict(getattr(pos, "lower_features", {}) or {})}
                    )
                if pos.upper_quantity > 0 and pos.upper_sku:
                    available_rows.append(
                        {"skuId": str(pos.upper_sku), "features": dict(getattr(pos, "upper_features", {}) or {})}
                    )
            else:
                if pos.quantity > 0 and pos.sku:
                    available_rows.append(
                        {"skuId": str(pos.sku), "features": dict(getattr(pos, "features", {}) or {})}
                    )

        remaining = list(available_rows)
        for req in required_rows:
            sku_id = req.get("skuId", "")
            req_features = req.get("features") or {}
            matched_idx = None
            for idx, row in enumerate(remaining):
                use_feature_match = match_mode == "features" and bool(req_features)
                if not use_feature_match and sku_id and row.get("skuId") != sku_id:
                    continue
                if req_features and not self._core.inventory_manager._features_match(
                    row.get("features"), req_features, list(req_features.keys())
                ):
                    continue
                matched_idx = idx
                break
            if matched_idx is None:
                if req_features:
                    return f"反馈位置中未找到与任务要求一致的库存：SKU={sku_id}，features={req_features}。"
                return f"反馈位置中未找到任务所需 SKU {sku_id}，不能执行出库。"
            remaining.pop(matched_idx)
        return None

    def _validate_feedback_aisle_for_task(
        self,
        task: TaskData,
        aisle: Optional[int],
    ) -> Optional[str]:
        """校验反馈 巷道 for 任务相关逻辑。

        Args:
            self: 当前对象实例。
            task: 待处理的任务对象。
            aisle: 目标巷道编号。

        Returns:
            Optional[str]: 处理后的结果。
        """
        if aisle is None or task.task_type != TASK_TYPE_INBOUND:
            return None
        production_line = getattr(task, "production_line", None)
        valid_aisles = [int(a) for a in (self._core._get_valid_inbound_aisles(task, production_line) or [])]
        if int(aisle) not in valid_aisles:
            hits = self._collect_aisle_forbidden_hits(task, int(aisle))
            if hits:
                first = hits[0]
                return (
                    f"巷道 {int(aisle)} 禁配命中：feature={first['feature']}、value={first['value']}、"
                    f"skuId={first['skuId']}。"
                )
            return f"巷道 {int(aisle)} 不满足当前任务的禁配规则，无法执行入库。"
        return None

    def _collect_aisle_forbidden_hits(self, task: TaskData, aisle: int) -> List[Dict[str, Any]]:
        """执行 collect 巷道 forbidden hits 对应的业务处理。

        Args:
            self: 当前对象实例。
            task: 待处理的任务对象。
            aisle: 目标巷道编号。

        Returns:
            List[Dict[str, Any]]: 处理后的结果。
        """
        rules = (getattr(self._core, "aisle_forbidden", {}) or {}).get(int(aisle), {}) or {}
        if not rules:
            return []
        hits: List[Dict[str, Any]] = []
        for sku_entry in (getattr(task, "skus", None) or []):
            sku_dict = self._sku_entry_to_dict(sku_entry)
            sku_id = str(sku_dict.get("skuId") or "")
            raw_features = sku_dict.get("features") or {}
            if hasattr(self._core, "_normalize_feature_dict"):
                features = self._core._normalize_feature_dict(raw_features) or {}
            else:
                features = {str(k).strip(): str(v).strip() for k, v in dict(raw_features).items()}
            for feature_key, blocked_vals in rules.items():
                if feature_key not in features:
                    continue
                value = str(features.get(feature_key)).strip()
                if value in blocked_vals:
                    hits.append(
                        {
                            "skuId": sku_id,
                            "feature": str(feature_key),
                            "value": value,
                            "blockedValues": sorted([str(v) for v in blocked_vals]),
                        }
                    )
        return hits

    def _resolve_outbound_inventory_removal_entries(self, task: TaskData) -> List[Dict[str, Any]]:
        """执行 resolve 出库 库存 removal entries 对应的业务处理。

        Args:
            self: 当前对象实例。
            task: 待处理的任务对象。

        Returns:
            List[Dict[str, Any]]: 处理后的结果。
        """
        production_line = getattr(task, "production_line", None) or 1
        match_mode = self._core._get_outbound_match_mode(production_line)
        request_entries: List[Dict[str, Any]] = []
        for sku_entry in (task.skus or []):
            sku_dict = self._sku_entry_to_dict(sku_entry)
            sku_id = str(sku_dict.get("skuId") or "")
            features = dict(sku_dict.get("features") or {})
            if not sku_id and not (match_mode == "features" and features):
                continue
            request_entries.append({"skuId": sku_id, "features": features})

        removal_entries: List[Dict[str, Any]] = []
        for idx, entry in enumerate(request_entries):
            pos = task.positions[min(idx, len(task.positions) - 1)]
            actual_pos = self._core.inventory_manager.position_map.get(pos.get_position_id())
            if actual_pos is None:
                raise ValueError(f"货位 {pos.get_position_id()} 不存在，无法扣减出库库存。")

            feature_keys = list((entry.get("features") or {}).keys())
            removal_entry: Dict[str, Any] = {
                "positionId": pos.get_position_id(),
                "skuId": "",
                "quantity": 1,
                "features": {},
                "in_line": None,
                "out_line": None,
                "layer": None,
            }

            if getattr(actual_pos, "is_double_layer", False):
                lower_match = (
                    actual_pos.lower_quantity > 0
                    and (
                        (
                            match_mode == "features"
                            and feature_keys
                            and self._core.inventory_manager._features_match(
                                getattr(actual_pos, "lower_features", {}) or {},
                                entry.get("features") or {},
                                feature_keys,
                            )
                        )
                        or (
                            (match_mode != "features" or not feature_keys)
                            and str(actual_pos.lower_sku or "") == entry["skuId"]
                        )
                    )
                )
                upper_match = (
                    actual_pos.upper_quantity > 0
                    and (
                        (
                            match_mode == "features"
                            and feature_keys
                            and self._core.inventory_manager._features_match(
                                getattr(actual_pos, "upper_features", {}) or {},
                                entry.get("features") or {},
                                feature_keys,
                            )
                        )
                        or (
                            (match_mode != "features" or not feature_keys)
                            and str(actual_pos.upper_sku or "") == entry["skuId"]
                        )
                    )
                )
                if lower_match:
                    removal_entry["layer"] = "lower"
                    removal_entry["skuId"] = str(getattr(actual_pos, "lower_sku", "") or "")
                    removal_entry["features"] = dict(getattr(actual_pos, "lower_features", {}) or {})
                    removal_entry["in_line"] = getattr(actual_pos, "lower_in_line", None)
                    removal_entry["out_line"] = getattr(actual_pos, "lower_out_line", None)
                elif upper_match:
                    removal_entry["layer"] = "upper"
                    removal_entry["skuId"] = str(getattr(actual_pos, "upper_sku", "") or "")
                    removal_entry["features"] = dict(getattr(actual_pos, "upper_features", {}) or {})
                    removal_entry["in_line"] = getattr(actual_pos, "upper_in_line", None)
                    removal_entry["out_line"] = getattr(actual_pos, "upper_out_line", None)
                else:
                    raise ValueError(
                        f"货位 {pos.get_position_id()} 中未找到与任务要求一致的库存："
                        f"SKU={entry['skuId']}，features={entry.get('features') or {}}。"
                    )
            else:
                single_match = (
                    actual_pos.quantity > 0
                    and (
                        (
                            match_mode == "features"
                            and feature_keys
                            and self._core.inventory_manager._features_match(
                                getattr(actual_pos, "features", {}) or {},
                                entry.get("features") or {},
                                feature_keys,
                            )
                        )
                        or (
                            (match_mode != "features" or not feature_keys)
                            and str(getattr(actual_pos, "sku", "") or "") == entry["skuId"]
                        )
                    )
                )
                if not single_match:
                    raise ValueError(
                        f"货位 {pos.get_position_id()} 中未找到与任务要求一致的库存："
                        f"SKU={entry['skuId']}，features={entry.get('features') or {}}。"
                    )
                removal_entry["skuId"] = str(getattr(actual_pos, "sku", "") or "")
                removal_entry["features"] = dict(getattr(actual_pos, "features", {}) or {})
                removal_entry["in_line"] = getattr(actual_pos, "in_line", None)
                removal_entry["out_line"] = getattr(actual_pos, "out_line", None)

            removal_entries.append(removal_entry)
        return removal_entries

    def _apply_executing_inventory(self, task: TaskData) -> None:
        """应用executing 库存相关逻辑。

        Args:
            self: 当前对象实例。
            task: 待处理的任务对象。

        Returns:
            None: 处理后的结果。
        """
        if task.task_type != TASK_TYPE_OUTBOUND:
            return
        task_record = dict(getattr(task, "task_record", {}) or {})
        if task_record.get("inventory_deducted_at_executing"):
            task.task_record = task_record
            return

        removal_entries = self._resolve_outbound_inventory_removal_entries(task)
        for entry in removal_entries:
            pos = self._core.inventory_manager.position_map.get(str(entry.get("positionId") or ""))
            if pos is None:
                raise ValueError(f"货位 {entry.get('positionId')} 不存在，无法扣减出库库存。")
            self._core.inventory_manager.remove_inventory(pos, str(entry.get("skuId") or ""), 1)

        task_record["inventory_deducted_at_executing"] = True
        task_record["executing_inventory_entries"] = removal_entries
        task.task_record = task_record

    def _restore_executing_inventory(self, task: TaskData) -> None:
        """执行 restore executing 库存 对应的业务处理。

        Args:
            self: 当前对象实例。
            task: 待处理的任务对象。

        Returns:
            None: 处理后的结果。
        """
        if task.task_type != TASK_TYPE_OUTBOUND:
            return
        task_record = dict(getattr(task, "task_record", {}) or {})
        if not task_record.get("inventory_deducted_at_executing"):
            task.task_record = task_record
            return

        entries = list(task_record.get("executing_inventory_entries") or [])
        for entry in entries:
            position_id = str(entry.get("positionId") or "")
            actual_pos = self._core.inventory_manager.position_map.get(position_id)
            if actual_pos is None:
                continue
            self._core.inventory_manager.add_inventory(
                actual_pos,
                str(entry.get("skuId") or ""),
                int(entry.get("quantity") or 1),
                entry.get("layer"),
                features=entry.get("features"),
                in_line=entry.get("in_line"),
                out_line=entry.get("out_line"),
            )

        task_record["inventory_deducted_at_executing"] = False
        task_record.pop("executing_inventory_entries", None)
        task.task_record = task_record

    def _find_feedback_conflict(
        self,
        task_id: str,
        target_aisle: Optional[int],
        target_positions: List[InventoryPosition],
    ) -> Optional[str]:
        """查找反馈 conflict相关逻辑。

        Args:
            self: 当前对象实例。
            task_id: 任务唯一标识。
            target_aisle: 用于本函数处理的 `target_aisle` 参数。
            target_positions: 用于本函数处理的 `target_positions` 参数。

        Returns:
            Optional[str]: 处理后的结果。
        """
        target_keys = {self._position_key(pos) for pos in (target_positions or [])}
        task_groups = (
            ("执行中任务", self._core.running_tasks),
            ("已下发未确认任务", self._pending_execution_tasks),
        )
        for state_label, task_map in task_groups:
            for other_id, other_task in task_map.items():
                if other_id == task_id:
                    continue
                other_aisle = getattr(other_task, "assigned_aisle", None)
                if target_aisle is not None and other_aisle is not None and int(other_aisle) == int(target_aisle):
                    return f"巷道 {target_aisle} 已被任务 {other_id} 占用（{state_label}）。"
                other_positions = getattr(other_task, "positions", None) or []
                other_keys = {self._position_key(pos) for pos in other_positions}
                overlap = target_keys & other_keys
                if overlap:
                    overlap_key = sorted(overlap)[0]
                    return (
                        f"货位 {self._position_key_to_external(overlap_key)} "
                        f"已被任务 {other_id} 占用（{state_label}）。"
                    )
        return None

    def _find_pending_inbound_conflict(
        self,
        task_id: str,
        target_aisle: int,
        target_positions: List[InventoryPosition],
    ) -> Optional[str]:
        """查找待处理 入库 conflict相关逻辑。

        Args:
            self: 当前对象实例。
            task_id: 任务唯一标识。
            target_aisle: 用于本函数处理的 `target_aisle` 参数。
            target_positions: 用于本函数处理的 `target_positions` 参数。

        Returns:
            Optional[str]: 处理后的结果。
        """
        target_keys = {self._position_key(pos) for pos in (target_positions or [])}
        if not target_keys:
            return None
        for other_task in list(self._core.pending_inbound_by_aisle.get(int(target_aisle), []) or []):
            other_id = str(getattr(other_task, "task_id", "") or "")
            if not other_id or other_id == str(task_id):
                continue
            other_positions = list(getattr(other_task, "positions", None) or [])
            if not other_positions:
                continue
            other_keys = {self._position_key(pos) for pos in other_positions}
            overlap = target_keys & other_keys
            if overlap:
                overlap_key = sorted(overlap)[0]
                return (
                    f"货位 {self._position_key_to_external(overlap_key)} "
                    f"已被任务 {other_id} 占用（目标巷道待执行入库任务）。"
                )
        return None

    def _resolve_feedback_positions(
        self,
        task: TaskData,
        override_aisle: Optional[int],
        override_positions: List[InventoryPosition],
    ) -> Tuple[Optional[int], List[InventoryPosition], Optional[str]]:
        """执行 resolve 反馈 positions 对应的业务处理。

        Args:
            self: 当前对象实例。
            task: 待处理的任务对象。
            override_aisle: 用于本函数处理的 `override_aisle` 参数。
            override_positions: 用于本函数处理的 `override_positions` 参数。

        Returns:
            Tuple[Optional[int], List[InventoryPosition], Optional[str]]: 处理后的结果。
        """
        resolved_aisle = override_aisle if override_aisle is not None else getattr(task, "assigned_aisle", None)
        if override_positions:
            resolved_aisle = int(override_positions[0].aisle)
            return resolved_aisle, list(override_positions), None

        if override_aisle is None:
            return resolved_aisle, list(getattr(task, "positions", None) or []), None

        existing_positions = list(getattr(task, "positions", None) or [])
        if not existing_positions:
            return int(override_aisle), [], "任务尚未固化执行位置，不能直接 EXECUTING。"
        return int(override_aisle), existing_positions, None

    def _feedback_override_matches_fixed(
        self,
        task: TaskData,
        override_aisle: Optional[int],
        override_positions: List[InventoryPosition],
    ) -> Optional[str]:
        """执行 反馈 override matches fixed 对应的业务处理。

        Args:
            self: 当前对象实例。
            task: 待处理的任务对象。
            override_aisle: 用于本函数处理的 `override_aisle` 参数。
            override_positions: 用于本函数处理的 `override_positions` 参数。

        Returns:
            Optional[str]: 处理后的结果。
        """
        fixed_aisle = self._to_int_or_none(getattr(task, "assigned_aisle", None))
        if override_aisle is not None and fixed_aisle is not None and int(override_aisle) != int(fixed_aisle):
            return "EXECUTING 不支持修改巷道，请先调用 /api/v1/task/adjust。"
        if override_positions:
            fixed_positions = list(getattr(task, "positions", None) or [])
            fixed_keys = [self._position_key(pos) for pos in fixed_positions]
            override_keys = [self._position_key(pos) for pos in override_positions]
            if override_keys != fixed_keys:
                return "EXECUTING 不支持修改位置，请先调用 /api/v1/task/adjust。"
        return None

    def _feedback_position_payload(self, task: TaskData) -> Dict[str, Any]:
        """执行 反馈 货位 payload 对应的业务处理。

        Args:
            self: 当前对象实例。
            task: 待处理的任务对象。

        Returns:
            Dict[str, Any]: 处理后的结果。
        """
        if not getattr(task, "positions", None):
            return {}
        pos = task.positions[-1]
        return {
            "aisleId": str(getattr(task, "assigned_aisle", getattr(pos, "aisle", ""))),
            "targetPosition": self._position_to_api_dict(pos),
        }

    def _validate_adjust_positions(
        self,
        task: TaskData,
        aisle_id: int,
        positions: List[InventoryPosition],
    ) -> Optional[str]:
        """校验adjust positions相关逻辑。

        Args:
            self: 当前对象实例。
            task: 待处理的任务对象。
            aisle_id: 目标巷道编号。
            positions: 货位对象或货位描述集合。

        Returns:
            Optional[str]: 处理后的结果。
        """
        if not positions:
            return "调整后的位置不能为空。"
        if any(int(getattr(pos, "aisle", -1)) != int(aisle_id) for pos in positions):
            return "positions 必须全部属于目标巷道。"
        pos_error = self._validate_feedback_positions_for_task(task, positions)
        if pos_error:
            return pos_error
        conflict = self._find_feedback_conflict(str(getattr(task, "task_id", "") or ""), int(aisle_id), positions)
        if conflict:
            return conflict
        pending_conflict = self._find_pending_inbound_conflict(
            str(getattr(task, "task_id", "") or ""),
            int(aisle_id),
            positions,
        )
        if pending_conflict:
            return pending_conflict
        return None

    def adjust_inbound_task(
        self,
        *,
        task_id: str,
        aisle_id: Optional[int] = None,
        in_line: Optional[Any] = None,
        skus: Optional[List[Dict[str, Any]]] = None,
        positions: Optional[List[InventoryPosition]] = None,
    ) -> Dict[str, Any]:
        """执行 adjust 入库 任务 对应的业务处理。

        Args:
            self: 当前对象实例。
            task_id: 任务唯一标识。
            aisle_id: 目标巷道编号。
            in_line: 用于本函数处理的 `in_line` 参数。
            skus: 用于本函数处理的 `skus` 参数。
            positions: 货位对象或货位描述集合。

        Returns:
            Dict[str, Any]: 处理后的结果。
        """
        self._sync_time()
        task = self.get_task_by_id(task_id, task_type=TASK_TYPE_INBOUND)
        if task is None or getattr(task, "task_type", "") != TASK_TYPE_INBOUND:
            return {"success": False, "taskId": task_id, "reason": "未找到可调整的入库任务。"}
        if task_id in self._core.running_tasks:
            return {"success": False, "taskId": task_id, "reason": "任务已进入执行中，不能调整。"}

        old_aisle = getattr(task, "assigned_aisle", None)
        new_aisle = int(aisle_id) if aisle_id is not None else (int(old_aisle) if old_aisle is not None else None)
        if new_aisle is None:
            return {"success": False, "taskId": task_id, "reason": "任务缺少目标巷道，无法调整。"}

        if in_line is not None:
            parsed_line = self._parse_line_ref(in_line)
            if parsed_line is not None:
                task.in_line = parsed_line
        if skus is not None:
            task.skus = [self._sku_entry_to_dict(s) for s in (skus or [])]

        # 从所有 pending 入库队列移除，再按规则插到新巷道队首
        self._pending_execution_tasks.pop(str(task_id), None)
        for aisle in list(self._core.pending_inbound_by_aisle.keys()):
            self._core.pending_inbound_by_aisle[aisle] = [
                t for t in self._core.pending_inbound_by_aisle[aisle] if str(getattr(t, "task_id", "") or "") != str(task_id)
            ]
        task.assigned_aisle = int(new_aisle)

        if positions:
            pos_error = self._validate_adjust_positions(task, int(new_aisle), list(positions))
            if pos_error:
                return {"success": False, "taskId": task_id, "reason": pos_error}
            task.positions = list(positions)
        else:
            allocated = self._allocate_feedback_positions_for_aisle(task, int(new_aisle), exclude_task_id=task_id)
            if not allocated:
                return {"success": False, "taskId": task_id, "reason": f"巷道 {new_aisle} 无可用位置，调整失败。"}
            task.positions = list(allocated)

        self._core.pending_inbound_by_aisle[int(new_aisle)].insert(0, task)
        return {
            "success": True,
            "taskId": task_id,
            "aisleId": str(new_aisle),
            "positions": self._task_positions_to_api(task),
            "reason": None,
        }

    def _build_feedback_response(
        self,
        task: Optional[TaskData],
        *,
        task_id: str,
        status: str,
        success: bool,
        message: str = "",
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        """构建反馈 响应相关逻辑。

        Args:
            self: 当前对象实例。
            task: 待处理的任务对象。
            task_id: 任务唯一标识。
            status: 任务或系统状态。
            success: 用于本函数处理的 `success` 参数。
            message: 用于本函数处理的 `message` 参数。
            reason: 用于本函数处理的 `reason` 参数。

        Returns:
            Dict[str, Any]: 处理后的结果。
        """
        payload: Dict[str, Any] = {
            "success": bool(success),
            "taskId": task_id,
            "status": status,
        }
        if message:
            payload["message"] = message
        if reason:
            payload["reason"] = reason
        if task is not None and getattr(task, "assigned_aisle", None) is not None:
            payload["aisleId"] = str(task.assigned_aisle)
        positions = self._task_positions_to_api(task)
        if positions:
            payload["positions"] = positions
        return payload

    def apply_feedback(self, feedback: Dict[str, Any]) -> Dict[str, Any]:
        """按反馈状态推进任务，并保证执行前校验和失败回滚。

        Args:
            self: 当前对象实例。
            feedback: 包含 taskId、taskType、status、可选巷道和货位的反馈数据。

        Returns:
            Dict[str, Any]: 反馈处理结果、实际生效巷道/位置或失败原因。
        """
        self._sync_time()
        # task_id / status / task_type 是反馈状态机的查找键和目标状态。
        task_id = feedback.get("taskId")
        status = str(feedback.get("status", "")).upper()
        task_type = str(feedback.get("taskType", "")).upper()
        if not task_id:
            return {"success": False, "taskId": "", "status": status, "reason": "缺少 taskId。"}

        # task 是从 pending、待确认或 running 状态中定位到的真实内部任务。
        task = self.get_task_by_id(task_id, task_type=task_type)
        if status == "EXECUTING":
            # EXECUTING 是幂等的：已在 running 中的任务直接返回成功，不重复占用资源。
            if task_id in self._core.running_tasks:
                task = self._core.running_tasks[task_id]
                return self._build_feedback_response(
                    task,
                    task_id=task_id,
                    status=status,
                    success=True,
                    message="任务已处于执行中。",
                )

            if task is None:
                return {"success": False, "taskId": task_id, "status": status, "reason": "未找到对应任务。"}

            # override_aisle 是外部显式覆盖的巷道；mixed_assigned_aisle 是 mixed 已固化的默认巷道。
            override_aisle = self._to_int_or_none(feedback.get("aisleId"))
            mixed_assigned_aisle = self._to_int_or_none(getattr(task, "assigned_aisle", None))
            # 先统一解析顶层巷道和 positions 中的巷道，拒绝两者口径不一致的请求。
            # override_positions 是按统一外部坐标口径解析后的可选执行货位。
            override_positions = self._normalize_feedback_positions(
                feedback.get("positions"),
                override_aisle,
                default_aisle_id=mixed_assigned_aisle,
            )
            if override_positions:
                override_aisle = int(override_positions[0].aisle)
                if any(int(pos.aisle) != int(override_aisle) for pos in override_positions):
                    return {
                        "success": False,
                        "taskId": task_id,
                        "status": status,
                        "reason": "反馈位置必须全部属于同一巷道。",
                    }

            # 任务调整需走 adjust 接口；EXECUTING 只能确认既有分配，不能隐式改巷道或位置。
            override_error = self._feedback_override_matches_fixed(task, override_aisle, override_positions)
            if override_error:
                return {
                    "success": False,
                    "taskId": task_id,
                    "status": status,
                    "reason": override_error,
                }

            check_aisle = override_aisle if override_aisle is not None else self._to_int_or_none(getattr(task, "assigned_aisle", None))
            executable, executable_reason = self._is_task_currently_executable(task, preferred_aisle=check_aisle)
            if not executable:
                return {
                    "success": False,
                    "taskId": task_id,
                    "status": status,
                    "reason": executable_reason or "任务当前不可执行。",
                }

            original_aisle = getattr(task, "assigned_aisle", None)
            original_positions = list(getattr(task, "positions", None) or [])

            resolved_aisle, resolved_positions, resolve_error = self._resolve_feedback_positions(
                task,
                override_aisle,
                override_positions,
            )
            if resolve_error:
                return {
                    "success": False,
                    "taskId": task_id,
                    "status": status,
                    "reason": resolve_error,
                }

            aisle_error = self._validate_feedback_aisle_for_task(task, resolved_aisle)
            if aisle_error:
                return {
                    "success": False,
                    "taskId": task_id,
                    "status": status,
                    "reason": aisle_error,
                }

            position_error = self._validate_feedback_positions_for_task(task, resolved_positions)
            if position_error:
                return {
                    "success": False,
                    "taskId": task_id,
                    "status": status,
                    "reason": position_error,
                }

            conflict_reason = self._find_feedback_conflict(task_id, resolved_aisle, resolved_positions)
            if conflict_reason:
                return {
                    "success": False,
                    "taskId": task_id,
                    "status": status,
                    "reason": conflict_reason,
                }

            if resolved_aisle is not None:
                task.assigned_aisle = int(resolved_aisle)
            if resolved_positions:
                task.positions = list(resolved_positions)
            elif override_aisle is not None:
                task.assigned_aisle = original_aisle
                task.positions = original_positions
                return {
                    "success": False,
                    "taskId": task_id,
                    "status": status,
                    "reason": "未能根据反馈生成有效的执行位置。",
                }

            feedback_payload = dict(feedback)
            feedback_payload.update(self._feedback_position_payload(task))
            # 库存预扣、核心反馈和 running 状态需作为一个事务处理，任一环节失败都恢复快照。
            saved_state = self.save_state()
            self._core.apply_task_feedback(feedback_payload)
            try:
                self._apply_executing_inventory(task)
            except Exception as exc:
                self.restore_state(saved_state)
                return {
                    "success": False,
                    "taskId": task_id,
                    "status": status,
                    "reason": str(exc),
                }
            started, start_reason = self._start_task_execution(task_id, task_type)
            if not started:
                self.restore_state(saved_state)
                return {
                    "success": False,
                    "taskId": task_id,
                    "status": status,
                    "reason": start_reason or "任务进入执行态失败。",
                }
            task = self._core.running_tasks.get(task_id, task)
            return self._build_feedback_response(task, task_id=task_id, status=status, success=True, message="任务开始执行。")
        if status == "COMPLETED":
            # 完成仅接受 running 任务；完成后写入真实库存并释放执行资源。
            feedback_payload = dict(feedback)
            if task is not None:
                feedback_payload.update(self._feedback_position_payload(task))
            self._core.apply_task_feedback(feedback_payload)
            if not self._complete_task_execution(task_id):
                return {"success": False, "taskId": task_id, "status": status, "reason": "任务当前不在执行中。"}
            return self._build_feedback_response(task, task_id=task_id, status=status, success=True, message="任务执行完成。")
        if status == "FAILED":
            # 失败任务从所有待执行/执行容器清理，并恢复 EXECUTING 阶段临时扣减的库存。
            if task is None:
                return {
                    "success": False,
                    "taskId": task_id,
                    "status": status,
                    "reason": "未找到对应任务。",
                }
            feedback_payload = dict(feedback)
            if task is not None:
                feedback_payload.update(self._feedback_position_payload(task))
            self._core.apply_task_feedback(feedback_payload)
            self._fail_task_execution(task_id)
            return self._build_feedback_response(
                task,
                task_id=task_id,
                status=status,
                success=True,
                message="任务失败已记录。",
                reason=str(feedback.get("reason") or ""),
            )
        self._core.apply_task_feedback(feedback)
        return self._build_feedback_response(task, task_id=task_id, status=status, success=True)

    def _start_task_execution(self, task_id: str, task_type: str) -> Tuple[bool, Optional[str]]:
        """执行 start 任务 execution 对应的业务处理。

        Args:
            self: 当前对象实例。
            task_id: 任务唯一标识。
            task_type: 任务类型。

        Returns:
            Tuple[bool, Optional[str]]: 处理后的结果。
        """
        task = self._pending_execution_tasks.pop(task_id, None)
        came_from_pending_execution = task is not None
        if task is None:
            if task_type == "OUTBOUND":
                task = next((t for t in self._core.pending_outbound_queue if t.task_id == task_id), None)
            else:
                for queue in self._core.pending_inbound_by_aisle.values():
                    task = next((t for t in queue if t.task_id == task_id), None)
                    if task:
                        break
        if task is None:
            return task_id in self._core.running_tasks, None

        effective_aisle = self._get_task_effective_aisle(task)
        blocking_task = self._find_running_task_on_aisle(effective_aisle, exclude_task_id=task_id)
        if blocking_task is not None:
            other_id = str(getattr(blocking_task, "task_id", "") or "")
            if came_from_pending_execution:
                self._pending_execution_tasks[task_id] = task
            return False, f"任务 {other_id} 当前正在执行。"

        if task_type == "OUTBOUND":
            self._core.pending_outbound_queue = [t for t in self._core.pending_outbound_queue if t.task_id != task_id]
        else:
            for aisle in list(self._core.pending_inbound_by_aisle.keys()):
                self._core.pending_inbound_by_aisle[aisle] = [
                    t for t in self._core.pending_inbound_by_aisle[aisle] if t.task_id != task_id
                ]

        if not getattr(task, "task_record", None):
            task.task_record = self._core.generate_task_record(task, self._core.current_time)
        self._core.running_tasks[task_id] = task
        return True, None

    def _move_task_to_pending_execution(self, task: TaskData) -> None:
        """执行 move 任务 to 待处理 execution 对应的业务处理。

        Args:
            self: 当前对象实例。
            task: 待处理的任务对象。

        Returns:
            None: 处理后的结果。
        """
        task_id = str(getattr(task, "task_id", "") or "")
        if not task_id:
            return
        self._pending_execution_tasks[task_id] = task
        if getattr(task, "task_type", "") == TASK_TYPE_OUTBOUND:
            self._core.pending_outbound_queue = [t for t in self._core.pending_outbound_queue if t.task_id != task_id]
            return
        for aisle in list(self._core.pending_inbound_by_aisle.keys()):
            self._core.pending_inbound_by_aisle[aisle] = [
                t for t in self._core.pending_inbound_by_aisle[aisle] if t.task_id != task_id
            ]

    def _apply_completion_inventory(self, task: TaskData) -> None:
        """应用completion 库存相关逻辑。

        Args:
            self: 当前对象实例。
            task: 待处理的任务对象。

        Returns:
            None: 处理后的结果。
        """
        if not getattr(task, "positions", None):
            return

        if task.task_type == TASK_TYPE_INBOUND:
            sku_entries = [s for s in (task.skus or []) if isinstance(s, dict) and s.get("skuId") is not None]
            sku_ids = [s.get("skuId") for s in sku_entries]
            for idx, sku_id in enumerate(sku_ids):
                if not isinstance(sku_id, str) or not sku_id:
                    continue
                pos = task.positions[min(idx, len(task.positions) - 1)]
                sku_entry = sku_entries[idx] if idx < len(sku_entries) else {}
                sku_features = sku_entry.get("features") if isinstance(sku_entry.get("features"), dict) else None
                layer = None
                if pos.is_double_layer and len(sku_ids) > 1:
                    if len(task.positions) == 2 and task.positions[0] == task.positions[1]:
                        if pos.row == 1:
                            layer = "upper" if idx == 0 else "lower"
                        elif pos.row == 2:
                            layer = "lower" if idx == 0 else "upper"
                    elif len(task.positions) == 2:
                        if (pos.upper_sku not in (None, "")) and (pos.lower_sku in (None, "")):
                            layer = "lower"
                        elif (pos.upper_sku in (None, "")) and (pos.lower_sku in (None, "")):
                            layer = "upper"
                    if layer is None:
                        layer = "upper" if idx == 0 else "lower"
                elif pos.is_double_layer and len(sku_ids) == 1:
                    if pos.upper_quantity == 0:
                        layer = "upper"
                    elif pos.lower_quantity == 0:
                        layer = "lower"
                    else:
                        raise ValueError(f"position {pos.get_position_id()} is already full")

                self._core.inventory_manager.add_inventory(
                    pos,
                    sku_id,
                    1,
                    layer,
                    features=sku_features,
                    in_line=getattr(task, "in_line", None),
                    out_line=getattr(task, "out_line", None),
                    inbound_time=(
                        sku_entry.get("inboundTime")
                        if sku_entry.get("inboundTime") is not None
                        else sku_entry.get("arrivalTime")
                    ),
                )
            return

        if task.task_type == TASK_TYPE_OUTBOUND:
            task_record = dict(getattr(task, "task_record", {}) or {})
            if not task_record.get("inventory_deducted_at_executing"):
                for entry in self._resolve_outbound_inventory_removal_entries(task):
                    pos = self._core.inventory_manager.position_map.get(str(entry.get("positionId") or ""))
                    if pos is None:
                        raise ValueError(f"货位 {entry.get('positionId')} 不存在，无法扣减出库库存。")
                    self._core.inventory_manager.remove_inventory(pos, str(entry.get("skuId") or ""), 1)

            production_line = getattr(task, "production_line", None)
            if production_line is not None:
                completion_task = self._build_outbound_progress_task(task)
                self._core.mark_outbound_completed(int(production_line), completion_task, self._core.current_time)

    def _build_outbound_progress_task(self, task: TaskData) -> TaskData:
        """
        API允许外部自定义 outbound taskId（如 OUT11/OUT12），但仿真内部的组推进
        默认按标准生成的 taskId 识别整组完成情况。这里仅在 API 完成回写时生成一个
        进度识别用 task 视图，不改变外部可见 taskId，也不影响仿真内部原有任务对象。
        """
        if str(getattr(task, "task_type", "") or "") != TASK_TYPE_OUTBOUND:
            return task

        production_line = getattr(task, "production_line", None)
        group_idx = getattr(task, "group_idx", None)
        if production_line is None or group_idx is None:
            return task

        plan_group = None
        try:
            line_plan = (self._core.production_plan or {}).get(int(production_line), []) or []
            if 0 <= int(group_idx) < len(line_plan):
                plan_group = line_plan[int(group_idx)]
        except Exception:
            plan_group = None

        match_mode = self._core._get_outbound_match_mode(int(production_line))
        feature_keys = self._get_match_fields(int(production_line))

        def _normalize_plan_task(task_skus: Any) -> List[Dict[str, Any]]:
            """标准化计划 任务相关逻辑。

            Args:
                task_skus: 用于本函数处理的 `task_skus` 参数。

            Returns:
                List[Dict[str, Any]]: 处理后的结果。
            """
            normalized: List[Dict[str, Any]] = []
            for sku in (task_skus or []):
                sku_dict = self._sku_entry_to_dict(sku)
                normalized.append(
                    {
                        "skuId": str(sku_dict.get("skuId") or "").strip(),
                        "features": self._extract_sku_features(sku_dict, feature_keys),
                    }
                )
            return normalized

        def _normalize_runtime_task() -> List[Dict[str, Any]]:
            """标准化runtime 任务相关逻辑。

            Returns:
                List[Dict[str, Any]]: 处理后的结果。
            """
            normalized: List[Dict[str, Any]] = []
            for sku in (getattr(task, "skus", None) or []):
                sku_dict = self._sku_entry_to_dict(sku)
                normalized.append(
                    {
                        "skuId": str(sku_dict.get("skuId") or "").strip(),
                        "features": self._extract_sku_features(sku_dict, feature_keys),
                    }
                )
            return normalized

        def _matches_plan_task(plan_task_skus: Any) -> bool:
            """执行 matches 计划 任务 对应的业务处理。

            Args:
                plan_task_skus: 用于本函数处理的 `plan_task_skus` 参数。

            Returns:
                bool: 处理后的结果。
            """
            runtime_items = _normalize_runtime_task()
            plan_items = _normalize_plan_task(plan_task_skus)
            if len(plan_items) != len(runtime_items):
                return False
            remaining = list(runtime_items)
            for plan_item in plan_items:
                matched_idx = None
                for idx, runtime_item in enumerate(remaining):
                    if match_mode == "features":
                        if plan_item["features"] == runtime_item["features"]:
                            matched_idx = idx
                            break
                    else:
                        if plan_item["skuId"] and plan_item["skuId"] == runtime_item["skuId"]:
                            matched_idx = idx
                            break
                if matched_idx is None:
                    return False
                remaining.pop(matched_idx)
            return True

        source_task_skus = None
        if isinstance(plan_group, list):
            if len(plan_group) == 1:
                source_task_skus = plan_group[0]
            else:
                for plan_task_skus in plan_group:
                    if _matches_plan_task(plan_task_skus):
                        source_task_skus = plan_task_skus
                        break

        sku_labels: List[str] = []
        for sku in (source_task_skus if source_task_skus is not None else (getattr(task, "skus", None) or [])):
            try:
                if source_task_skus is None and match_mode == "features":
                    sku_dict = self._sku_entry_to_dict(sku)
                    features = self._extract_sku_features(sku_dict, feature_keys)
                    if features:
                        sku_labels.append(self._core._task_sku_label({"skuId": "", "features": features}))
                        continue
                sku_labels.append(self._core._task_sku_label(sku))
            except Exception:
                sku_dict = self._sku_entry_to_dict(sku)
                if source_task_skus is None and match_mode == "features":
                    features = self._extract_sku_features(sku_dict, feature_keys)
                    if features:
                        sku_labels.append(self._core._task_sku_label({"skuId": "", "features": features}))
                        continue
                sku_id = str(sku_dict.get("skuId") or "").strip()
                if sku_id:
                    sku_labels.append(sku_id)

        if not sku_labels:
            return task

        internal_task_id = (
            f"{TASK_TYPE_OUTBOUND}_PL{int(production_line)}_GP{int(group_idx) + 1}_{'_'.join(sku_labels)}"
        )
        current_task_id = str(getattr(task, "task_id", "") or "")
        if current_task_id == internal_task_id:
            return task

        progress_task = deepcopy(task)
        progress_task.task_id = internal_task_id
        progress_task.task_name = internal_task_id
        return progress_task

    def _complete_task_execution(self, task_id: str) -> bool:
        """执行 complete 任务 execution 对应的业务处理。

        Args:
            self: 当前对象实例。
            task_id: 任务唯一标识。

        Returns:
            bool: 处理后的结果。
        """
        self._pending_execution_tasks.pop(task_id, None)
        task = self._core.running_tasks.pop(task_id, None)
        self._core.pending_outbound_queue = [t for t in self._core.pending_outbound_queue if t.task_id != task_id]
        for aisle in list(self._core.pending_inbound_by_aisle.keys()):
            self._core.pending_inbound_by_aisle[aisle] = [t for t in self._core.pending_inbound_by_aisle[aisle] if t.task_id != task_id]
        if task is None:
            return False
        self._apply_completion_inventory(task)
        self._core.completed_tasks.append(task)
        if getattr(task, "positions", None) and task.assigned_aisle is not None:
            self._core.current_position_by_aisle[task.assigned_aisle] = task.positions[-1]
        return True

    def _fail_task_execution(self, task_id: str) -> bool:
        """执行 fail 任务 execution 对应的业务处理。

        Args:
            self: 当前对象实例。
            task_id: 任务唯一标识。

        Returns:
            bool: 处理后的结果。
        """
        self._pending_execution_tasks.pop(task_id, None)
        task = self._core.running_tasks.pop(task_id, None)
        self._core.pending_outbound_queue = [t for t in self._core.pending_outbound_queue if t.task_id != task_id]
        for aisle in list(self._core.pending_inbound_by_aisle.keys()):
            self._core.pending_inbound_by_aisle[aisle] = [t for t in self._core.pending_inbound_by_aisle[aisle] if t.task_id != task_id]
        if task is not None:
            self._restore_executing_inventory(task)
        return True

    def _clear_pending_only(self) -> None:
        """清理待处理 only相关逻辑。

        Args:
            self: 当前对象实例。

        Returns:
            None: 处理后的结果。
        """
        self._core.pending_outbound_queue.clear()
        for aisle in list(self._core.pending_inbound_by_aisle.keys()):
            self._core.pending_inbound_by_aisle[aisle].clear()

    def _clear_all_assigned_and_pending(self) -> None:
        """清理all assigned and 待处理相关逻辑。

        Args:
            self: 当前对象实例。

        Returns:
            None: 处理后的结果。
        """
        self._clear_pending_only()
        self._pending_execution_tasks.clear()
        self._core.running_tasks.clear()

    def set_production_plan(
        self,
        production_plan: Any,
        update: bool = False,
        reset_assigned: bool = False,
        current_groups: Any = None,
        legacy_current_groups: Any = None,
    ) -> Dict[str, Any]:
        """新增或替换生产计划，并同步计划编号、当前组和任务状态。

        Args:
            self: 当前对象实例。
            production_plan: 外部生产计划或已经转换的内部计划。
            update: True 表示替换计划；False 表示按产线追加计划组。
            reset_assigned: UPDATE 时是否同时清理 pending、已下发和 running 任务。
            current_groups: 当前组设置，使用外部的一基组号口径。
            legacy_current_groups: 兼容旧请求的当前组设置。

        Returns:
            Dict[str, Any]: 是否成功及本次忽略的重复计划编号。
        """
        self._sync_time()
        # _add_plan_index_alias 仅在本次 ADD 中保存外部 planIndex 到内部接续组号的别名。
        self._add_plan_index_alias = {}
        # new_plan_mapping: 本次接受的 planId -> lineId；ignored_plan_ids 保存被去重忽略的编号。
        new_plan_mapping: Dict[str, int] = {}
        ignored_plan_ids: List[str] = []
        # effective_production_plan 是去重后的本次请求计划，不会覆盖已存在重复 planId。
        effective_production_plan = production_plan
        # 先对本次计划去重；重复 planId 不覆盖既有计划，只在返回中说明被忽略的编号。
        if (isinstance(production_plan, dict) and "plans" in production_plan) or hasattr(production_plan, "plans"):
            effective_production_plan, ignored_plan_ids = self._dedupe_plan_ids(production_plan, update=update)
        if isinstance(effective_production_plan, dict) and "production_plan" in effective_production_plan:
            raw_core_plan: Any = effective_production_plan.get("production_plan", {}) or {}
        elif isinstance(effective_production_plan, dict) and "plans" in effective_production_plan:
            raw_core_plan = self._build_core_production_plan(effective_production_plan)
            new_plan_mapping = self._extract_plan_id_line_mapping(effective_production_plan)
        elif hasattr(effective_production_plan, "plans"):
            raw_core_plan = self._build_core_production_plan(effective_production_plan)
            new_plan_mapping = self._extract_plan_id_line_mapping(effective_production_plan)
        else:
            raw_core_plan = effective_production_plan or {}
        core_plan: Dict[int, List[Any]] = {}
        if isinstance(raw_core_plan, dict):
            for raw_line_id, raw_groups in raw_core_plan.items():
                line_id = self._normalize_line_id(raw_line_id)
                if line_id is not None:
                    core_plan[line_id] = list(raw_groups) if isinstance(raw_groups, list) else []
        if not update and ((isinstance(effective_production_plan, dict) and "plans" in effective_production_plan) or hasattr(effective_production_plan, "plans")):
            self._add_plan_index_alias = self._build_add_plan_index_alias(effective_production_plan)

        # current_group_map 使用内部零基组索引；has_explicit_current_groups 区分未传与显式传空。
        current_group_map = self._normalize_current_groups(current_groups, legacy_current_groups)
        has_explicit_current_groups = current_group_map is not None
        final_plan: Dict[int, List[Any]]
        if update:
            # UPDATE 使用请求计划整体替换；ADD 保留原计划并在每条产线尾部追加新组。
            final_plan = core_plan
        else:
            final_plan = {
                int(line_id): list(groups or [])
                for line_id, groups in deepcopy(self._core.production_plan).items()
            }
            for line_id, groups in (core_plan or {}).items():
                lid = int(line_id)
                final_plan.setdefault(lid, [])
                final_plan[lid].extend(list(groups or []))

        if current_group_map is None:
            if update:
                current_group_map = {}
            else:
                current_group_map = {
                    int(line_id): int(group_idx)
                    for line_id, group_idx in (self._core.production_line_current_group or {}).items()
                }

        self._validate_current_group_map(final_plan, current_group_map)
        if update:
            # 不重置时只清未下发 pending，已下发/运行任务可继续完成；重置时清全部任务状态。
            if bool(reset_assigned):
                self._clear_all_assigned_and_pending()
            else:
                self._clear_pending_only()
        if has_explicit_current_groups:
            self._validate_current_group_runtime_constraints(final_plan, current_group_map)
        self._core.set_production_plan(final_plan, current_groups=current_group_map)
        if update:
            self._plan_id_to_line_id = dict(new_plan_mapping)
        else:
            self._plan_id_to_line_id.update(new_plan_mapping)
        return {
            "success": True,
            "ignoredPlanIds": ignored_plan_ids,
        }

    def set_current_groups(self, current_groups: Any = None, legacy_current_groups: Any = None) -> bool:
        """设置当前 groups相关逻辑。

        Args:
            self: 当前对象实例。
            current_groups: 用于本函数处理的 `current_groups` 参数。
            legacy_current_groups: 用于本函数处理的 `legacy_current_groups` 参数。

        Returns:
            bool: 处理后的结果。
        """
        self._sync_time()
        current_group_map = self._normalize_current_groups(current_groups, legacy_current_groups)
        if current_group_map is None:
            return True
        self._validate_current_group_map(self._core.production_plan, current_group_map)
        self._validate_current_group_runtime_constraints(self._core.production_plan, current_group_map)
        for line_id, group_idx in current_group_map.items():
            group_count = len(self._core.production_plan.get(line_id, []) or [])
            self._core.production_line_current_group[line_id] = max(0, min(int(group_idx), group_count))
            self._core.production_line_completed_tasks[line_id] = set()
            self._core.production_line_group_completion_times[line_id] = []
        return True

    def _validate_current_group_map(self, production_plan: Dict[int, List[Any]], current_group_map: Dict[int, int]) -> None:
        """校验当前 任务组 map相关逻辑。

        Args:
            self: 当前对象实例。
            production_plan: 用于本函数处理的 `production_plan` 参数。
            current_group_map: 用于本函数处理的 `current_group_map` 参数。

        Returns:
            None: 处理后的结果。
        """
        for line_id, group_idx in (current_group_map or {}).items():
            group_count = len((production_plan or {}).get(int(line_id), []) or [])
            if int(group_idx) > group_count:
                raise ValueError(
                    f"产线 {int(line_id)} 设置的组 {int(group_idx) + 1} 超过当前上限 {group_count}"
                )

    def _extract_task_group_idx(self, task: TaskData) -> Optional[int]:
        """执行 extract 任务 任务组 idx 对应的业务处理。

        Args:
            self: 当前对象实例。
            task: 待处理的任务对象。

        Returns:
            Optional[int]: 处理后的结果。
        """
        group_idx = getattr(task, "group_idx", None)
        if group_idx is not None:
            try:
                return int(group_idx)
            except (TypeError, ValueError):
                pass
        task_id = str(getattr(task, "task_id", "") or "")
        m = re.search(r"_GP(\d+)_", task_id)
        if not m:
            m = re.search(r"GP(\d+)", task_id)
        if not m:
            return None
        try:
            return max(0, int(m.group(1)) - 1)
        except (TypeError, ValueError):
            return None

    def _classify_outbound_group_state(self, task: Optional[TaskData]) -> str:
        """执行 classify 出库 任务组 状态 对应的业务处理。

        Args:
            self: 当前对象实例。
            task: 待处理的任务对象。

        Returns:
            str: 处理后的结果。
        """
        if task is None or str(getattr(task, "task_type", "") or "") != TASK_TYPE_OUTBOUND:
            return "current"
        production_line = getattr(task, "production_line", None)
        if production_line is None:
            return "current"
        task_group_idx = self._extract_task_group_idx(task)
        if task_group_idx is None:
            return "current"
        current_group_idx = int((self._core.production_line_current_group or {}).get(int(production_line), 0))
        if int(task_group_idx) < current_group_idx:
            return "past"
        if int(task_group_idx) > current_group_idx:
            return "future"
        return "current"

    def _validate_current_group_runtime_constraints(
        self,
        production_plan: Dict[int, List[Any]],
        current_group_map: Dict[int, int],
    ) -> None:
        """校验当前 任务组 runtime constraints相关逻辑。

        Args:
            self: 当前对象实例。
            production_plan: 用于本函数处理的 `production_plan` 参数。
            current_group_map: 用于本函数处理的 `current_group_map` 参数。

        Returns:
            None: 处理后的结果。
        """
        active_outbound_tasks: List[TaskData] = []
        active_outbound_tasks.extend(
            t for t in self._core.running_tasks.values()
            if getattr(t, "task_type", "") == TASK_TYPE_OUTBOUND
        )
        active_outbound_tasks.extend(
            t for t in self._pending_execution_tasks.values()
            if getattr(t, "task_type", "") == TASK_TYPE_OUTBOUND
        )
        active_outbound_tasks.extend(
            t for t in self._core.pending_outbound_queue
            if getattr(t, "task_type", "") == TASK_TYPE_OUTBOUND
        )

        for line_id, requested_group_idx in (current_group_map or {}).items():
            line = int(line_id)
            requested_idx = int(requested_group_idx)
            group_count = len((production_plan or {}).get(line, []) or [])
            if group_count <= 0:
                continue

            min_unfinished_idx: Optional[int] = None
            for task in active_outbound_tasks:
                task_line = getattr(task, "production_line", None)
                if task_line is None or int(task_line) != line:
                    continue
                task_group_idx = self._extract_task_group_idx(task)
                if task_group_idx is None:
                    continue
                if min_unfinished_idx is None or int(task_group_idx) < min_unfinished_idx:
                    min_unfinished_idx = int(task_group_idx)

            if min_unfinished_idx is None:
                continue
            if requested_idx > min_unfinished_idx:
                raise ValueError(
                    f"产线{line}仍有未完成的第{min_unfinished_idx + 1}组任务，"
                    f"不能直接切到第{requested_idx + 1}组。"
                )

    def get_production_plan(self) -> Dict[int, List]:
        """获取生产 计划相关逻辑。

        Args:
            self: 当前对象实例。

        Returns:
            Dict[int, List]: 处理后的结果。
        """
        return self._core.production_plan

    def get_running_tasks(self) -> Dict[str, TaskData]:
        """获取执行中 tasks相关逻辑。

        Args:
            self: 当前对象实例。

        Returns:
            Dict[str, TaskData]: 处理后的结果。
        """
        return self._core.running_tasks.copy()

    def get_pending_tasks(self) -> Dict[str, Any]:
        """获取待处理 tasks相关逻辑。

        Args:
            self: 当前对象实例。

        Returns:
            Dict[str, List[TaskData]]: 处理后的结果。
        """
        return {
            "inbound": {aisle: list(tasks) for aisle, tasks in self._core.pending_inbound_by_aisle.items()},
            "outbound": list(self._core.pending_outbound_queue),
        }

    def get_completed_tasks(self) -> List[TaskData]:
        """获取已完成 tasks相关逻辑。

        Args:
            self: 当前对象实例。

        Returns:
            List[TaskData]: 处理后的结果。
        """
        return list(self._core.completed_tasks)

    def get_inventory_summary(self) -> Dict[int, Dict[str, int]]:
        """获取库存 summary相关逻辑。

        Args:
            self: 当前对象实例。

        Returns:
            Dict[int, Dict[str, int]]: 处理后的结果。
        """
        return {aisle: {sku: qty for sku, qty in skus.items() if qty > 0} for aisle, skus in self._core.inventory_manager.current_inventory.items()}

    def get_full_inventory(self) -> List[Dict[str, Any]]:
        """获取full 库存相关逻辑。

        Args:
            self: 当前对象实例。

        Returns:
            List[Dict[str, Any]]: 处理后的结果。
        """
        full_inventory: List[Dict[str, Any]] = []
        match_fields = self._get_match_fields()
        for position in self._core.inventory_manager.inventory_positions:
            base_info = {
                "aisleId": str(position.aisle),
                "row": self._internal_to_external_row(position.row, position.aisle),
                "column": position.column,
                "level": position.level,
            }
            if position.is_double_layer:
                upper_entry = {"skuId": position.upper_sku or "", "quantity": position.upper_quantity or 0}
                lower_entry = {"skuId": position.lower_sku or "", "quantity": position.lower_quantity or 0}
                upper_features = getattr(position, "upper_features", {}) or {}
                lower_features = getattr(position, "lower_features", {}) or {}
                for field in match_fields:
                    if field in upper_features:
                        upper_entry[field] = upper_features[field]
                    if field in lower_features:
                        lower_entry[field] = lower_features[field]
                if isinstance(upper_features.get("_inbound_time"), (int, float)):
                    upper_entry["inboundTime"] = float(upper_features["_inbound_time"])
                if isinstance(lower_features.get("_inbound_time"), (int, float)):
                    lower_entry["inboundTime"] = float(lower_features["_inbound_time"])
                full_inventory.append({**base_info, "positions": [upper_entry, lower_entry]})
            else:
                entry = {"skuId": getattr(position, "sku", "") or "", "quantity": getattr(position, "quantity", 0) or 0}
                features = getattr(position, "features", {}) or {}
                for field in match_fields:
                    if field in features:
                        entry[field] = features[field]
                inbound_time = features.get("_inbound_time")
                if isinstance(inbound_time, (int, float)):
                    entry["inboundTime"] = float(inbound_time)
                full_inventory.append({**base_info, "positions": [entry]})
        return full_inventory

    def get_aisle_status(self) -> Dict[int, Dict[str, Any]]:
        """获取巷道 status相关逻辑。

        Args:
            self: 当前对象实例。

        Returns:
            Dict[int, Dict[str, Any]]: 处理后的结果。
        """
        result: Dict[int, Dict[str, Any]] = {}
        for aisle in self._core.aisles:
            is_busy = any(t.assigned_aisle == aisle for t in self._core.running_tasks.values())
            blockage = {}
            for pl in range(1, self._core.num_production_lines + 1):
                status = self._core.blockage_status.get((aisle, pl), {})
                unblock_time = status.get("unblock_time", 0.0)
                if unblock_time == float("inf"):
                    unblock_time = -1
                blockage[pl] = {"blocked": status.get("blocked", False), "unblock_time": unblock_time}
            current_position = self._core.current_position_by_aisle.get(aisle)
            if current_position is not None:
                external_row = self._internal_to_external_row(
                    int(getattr(current_position, "row", 0) or 0),
                    int(getattr(current_position, "aisle", aisle) or aisle),
                )
                current_position_data = {
                    "aisle": int(getattr(current_position, "aisle", aisle)),
                    "row": external_row,
                    "column": int(getattr(current_position, "column", 0) or 0),
                    "level": int(getattr(current_position, "level", 0) or 0),
                }
            else:
                current_position_data = None
            result[aisle] = {"is_busy": is_busy, "blockage": blockage, "current_position": current_position_data}
        return result

    def update_sku_config(self, config_data: Dict[str, Any]) -> bool:
        """更新sku 配置相关逻辑。

        Args:
            self: 当前对象实例。
            config_data: 用于本函数处理的 `config_data` 参数。

        Returns:
            bool: 处理后的结果。
        """
        required_fields = ["sku_types", "sku_pairs", "sku_solo", "sku_to_production_line"]
        for field in required_fields:
            if field not in config_data:
                raise ValueError(f"Missing required field: {field}")
        config_path = Path(project_root) / "simulation" / "data" / "sku_config.json"
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config_data, f, ensure_ascii=False, indent=2)
        self._core.sku_types = config_data["sku_types"]
        self._core.sku_to_production_line = config_data["sku_to_production_line"]
        return True


_warehouse_service: Optional[WarehouseService] = None


def _get_or_create_warehouse_service() -> WarehouseService:
    """获取or create warehouse service相关逻辑。

    Returns:
        WarehouseService: 处理后的结果。
    """
    global _warehouse_service
    if _warehouse_service is None:
        _warehouse_service = WarehouseService()
    return _warehouse_service


def get_warehouse_service(request: Request) -> WarehouseService:
    """获取warehouse service相关逻辑。

    Args:
        request: 接口请求对象。

    Returns:
        WarehouseService: 处理后的结果。
    """
    global _warehouse_service
    service = getattr(request.app.state, "warehouse_service", None)
    if service is None:
        service = _get_or_create_warehouse_service()
        request.app.state.warehouse_service = service
    _warehouse_service = service
    return service


def init_warehouse_service(warehouse_core: Optional[WarehouseCore] = None) -> WarehouseService:
    """执行 init warehouse service 对应的业务处理。

    Args:
        warehouse_core: 用于本函数处理的 `warehouse_core` 参数。

    Returns:
        WarehouseService: 处理后的结果。
    """
    global _warehouse_service
    _warehouse_service = WarehouseService(warehouse_core)
    return _warehouse_service


def reset_warehouse_service() -> None:
    """执行 reset warehouse service 对应的业务处理。

    Returns:
        None: 处理后的结果。
    """
    global _warehouse_service
    _warehouse_service = None
