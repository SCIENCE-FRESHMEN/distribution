
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
        if warehouse_core is None:
            self._core = WarehouseCore(
                scheduler_type="optimization",
                inbound_aisle_strategy="proposed",
                inbound_allocation_strategy="proposed",
                config_path="config/warehouse.json",
            )
            self._core.initialize()
        else:
            self._core = warehouse_core

        self._start_time = time.time()
        self._last_sync_time = self._start_time
        self._pending_execution_tasks: Dict[str, TaskData] = {}
        self._core.pending_execution_tasks = self._pending_execution_tasks
        self._add_plan_index_alias: Dict[Tuple[str, int], int] = {}

    @property
    def core(self) -> WarehouseCore:
        return self._core

    def _get_current_time(self) -> float:
        return time.time() - self._start_time

    def _sync_time(self) -> None:
        self._core.current_time = self._get_current_time()
        self._last_sync_time = time.time()

    def _sku_entry_to_dict(self, sku: Any) -> Dict[str, Any]:
        if isinstance(sku, dict):
            d = dict(sku)
        elif hasattr(sku, "model_dump"):
            d = sku.model_dump()
        elif hasattr(sku, "dict"):
            d = sku.dict()
        else:
            d = {"skuId": getattr(sku, "skuId", None), "quantity": getattr(sku, "quantity", 1)}

        features = d.get("features") if isinstance(d.get("features"), dict) else {}
        # Allow passing features either in `features` object or as sku top-level fields.
        for k, v in d.items():
            if k in ("skuId", "quantity", "features"):
                continue
            if v is not None and k not in features:
                features[k] = v
        if features:
            d["features"] = self._normalize_features(features)
        return d

    def _normalize_features(self, features: Any) -> Dict[str, Any]:
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
        features = sku_dict.get("features")
        if isinstance(features, dict):
            return self._normalize_features(dict(features))
        if not feature_keys:
            return {}
        raw = {k: sku_dict.get(k) for k in feature_keys if sku_dict.get(k) is not None}
        return self._normalize_features(raw)

    def _get_match_fields(self, production_line: Optional[int] = None) -> List[str]:
        return list(self._core._get_outbound_match_features(production_line) or [])

    def _get_outbound_match_features(self, production_line: Optional[int] = None) -> List[str]:
        # Backward-compatible alias for older call sites.
        return self._get_match_fields(production_line)

    def _normalize_line_id(self, value: Any) -> Optional[int]:
        return self._to_int_or_none(value)

    def _build_core_production_plan(self, production_plan: Any) -> Dict[int, List[Any]]:
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
                        sku_entry = {"skuId": sku.skuId if hasattr(sku, "skuId") else sku.get("skuId")}
                        quantity = sku.quantity if hasattr(sku, "quantity") else sku.get("quantity", 1)
                        for _ in range(int(quantity or 0)):
                            item = dict(sku_entry)
                            if feature_fields:
                                features = {}
                                for field in feature_fields:
                                    value = getattr(sku, field, None) if hasattr(sku, field) else sku.get(field)
                                    if value is not None:
                                        features[field] = value
                                raw_features = sku.features if hasattr(sku, "features") else sku.get("features")
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

    def _normalize_current_groups(
        self,
        current_groups: Any = None,
        legacy_current_groups: Any = None,
    ) -> Optional[Dict[int, int]]:
        normalized: Dict[int, int] = {}

        if current_groups is not None:
            if isinstance(current_groups, dict):
                rows = [{"lineId": key, "currentGroup": value} for key, value in current_groups.items()]
            else:
                rows = current_groups
            for row in rows or []:
                line_id = self._normalize_line_id(
                    row.lineId if hasattr(row, "lineId") else row.get("lineId")
                )
                group_num = row.currentGroup if hasattr(row, "currentGroup") else row.get("currentGroup")
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
    @staticmethod
    def _to_int_or_none(value: Any) -> Optional[int]:
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
        return 2 * (aisle - 1) + internal_row

    def _get_position_by_external_coords(
        self,
        aisle_id: int,
        external_row: int,
        column: int,
        level: int,
    ) -> InventoryPosition:
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

    def _position_to_api_dict(self, position: InventoryPosition) -> Dict[str, int]:
        aisle = int(getattr(position, "aisle", 0) or 0)
        row = int(getattr(position, "row", 0) or 0)
        return {
            "aisleId": str(aisle),
            "row": self._internal_to_external_row(row, aisle),
            "column": int(getattr(position, "column", 0) or 0),
            "level": int(getattr(position, "level", 0) or 0),
        }

    def _task_positions_to_api(self, task: Optional[TaskData]) -> Optional[List[Dict[str, int]]]:
        if task is None or not getattr(task, "positions", None):
            return None
        return [self._position_to_api_dict(pos) for pos in task.positions]

    def _find_task_in_pending_queues(self, task_id: str, task_type: Optional[str] = None) -> Optional[TaskData]:
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
        task = self._core.running_tasks.get(task_id)
        if task is not None:
            return task
        task = self._pending_execution_tasks.get(task_id)
        if task is not None:
            return task
        return self._find_task_in_pending_queues(task_id, task_type=task_type)

    def get_active_task_ids(self) -> Set[str]:
        task_ids: Set[str] = set(self._core.running_tasks.keys()) | set(self._pending_execution_tasks.keys())
        task_ids.update(t.task_id for t in self._core.pending_outbound_queue)
        for queue in self._core.pending_inbound_by_aisle.values():
            task_ids.update(t.task_id for t in queue)
        return task_ids

    def get_task_for_aisle(self, aisle_id: int, task_id: Optional[str] = None) -> Optional[TaskData]:
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

    def _get_task_effective_aisle(self, task: Optional[TaskData]) -> Optional[int]:
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

    def _task_brief_dict(self, task: TaskData) -> Dict[str, Any]:
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
        candidates: List[TaskData] = []
        if not self.is_aisle_available(int(aisle)):
            return candidates
        for task in list(self._core.pending_outbound_queue):
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

    def _is_task_logically_executable(
        self,
        task: Optional[TaskData],
        preferred_aisle: Optional[int] = None,
    ) -> Tuple[bool, Optional[str]]:
        if task is None:
            return False, "未找到对应任务。"
        task_id = str(getattr(task, "task_id", "") or "")
        task_type = str(getattr(task, "task_type", "") or "")
        if task_id in self._core.running_tasks or task_id in self._pending_execution_tasks:
            return True, None
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

        in_pending_outbound = any(str(getattr(t, "task_id", "") or "") == task_id for t in self._core.pending_outbound_queue)
        if not in_pending_outbound:
            return False, f"任务 {task_id} 不在当前可执行出库队列中。"
        group_state = self._classify_outbound_group_state(task)
        if group_state == "past":
            return False, f"任务 {task_id} 属于当前组之前的历史组，已忽略。"
        if group_state == "future":
            return False, f"任务 {task_id} 不属于当前组，暂不可执行。"
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
        grouped = self.get_tasks_by_aisle_for_api()
        return {
            int(aisle): list((payload.get("can_executing") or []))
            for aisle, payload in grouped.items()
        }

    def get_prioritized_executable_tasks_by_aisle(
        self,
        recommended_tasks: Optional[Dict[int, TaskData]] = None,
    ) -> Dict[int, List[TaskData]]:
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
        self._sync_time()
        grouped: Dict[int, Dict[str, List[TaskData]]] = {
            int(aisle): {"can_executing": [], "pending": [], "running": []}
            for aisle in self._core.aisles
        }

        def ensure_bucket(aisle_val: Optional[int]) -> Optional[Dict[str, List[TaskData]]]:
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
        return {
            "core_state": self._core._save_simulation_state(),
            "pending_execution_tasks": deepcopy(self._pending_execution_tasks),
            "aisle_availability": deepcopy(self._aisle_availability),
        }

    def restore_state(self, saved_state: Dict[str, Any]) -> None:
        self._core._restore_simulation_state(saved_state["core_state"])
        self._pending_execution_tasks = deepcopy(saved_state.get("pending_execution_tasks", {}))
        self._aisle_availability = deepcopy(saved_state.get("aisle_availability", {}))
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
        # Numeric line id: resolve to dock token with aisle-aware clamped column.
        try:
            est = getattr(self._core, "time_estimator", None)
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
                ck = canonical(k) if callable(canonical) else str(k)
                if str(ck) == "skid_state" and v is not None:
                    skid_state = str(v).strip()
                    break
        return skid_state == "0"

    def _extract_position_feature(self, pos: InventoryPosition, keys: List[str]) -> Optional[str]:
        feats = getattr(pos, "features", None)
        if not isinstance(feats, dict):
            return None
        feats = self._normalize_features(feats)
        canonical = getattr(self._core, "_canonical_feature_key", None)
        for k in keys:
            ck = canonical(k) if callable(canonical) else str(k)
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
        # If caller asks idle-only and this aisle is busy, make it non-competitive.
        if idle_only and running_cnt > 0:
            load += 1e6
        return (distance, load, int(pos.aisle))

    def _resolve_empty_skid_outbound(self, task: TaskData) -> Optional[TaskData]:
        """
        Convert placeholder empty-skid outbound request into concrete outbound:
        choose a specific occupied empty-skid position and bind skuId/aisle/position.
        """
        out_line = getattr(task, "out_line", None)
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
            # All candidate aisles busy or blocked now: choose lower future load.
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
        self._sync_time()
        for status in aisle_status_list:
            aisle_id = int(status.aisleId) if hasattr(status, "aisleId") else int(status["aisleId"])
            is_available = status.isAvailable if hasattr(status, "isAvailable") else status["isAvailable"]
            dock_availability = {"in": {}, "out": {}}
            dock_rows = status.dockAvailability if hasattr(status, "dockAvailability") else status.get("dockAvailability", [])
            for row in dock_rows or []:
                direction = row.direction if hasattr(row, "direction") else row.get("direction")
                direction_norm = self._normalize_direction(direction)
                line_ref = row.lineRef if hasattr(row, "lineRef") else row.get("lineRef")
                line_key = self._normalize_line_ref_key(line_ref, aisle_id=aisle_id, direction=direction_norm)
                available = row.isAvailable if hasattr(row, "isAvailable") else row.get("isAvailable", True)
                if direction_norm and line_key is not None:
                    dock_availability[direction_norm][line_key] = bool(available)
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
        self._sync_time()
        if not inventory_list:
            return

        self._clear_inventory_only()

        feature_keys = self._get_match_fields()
        for inv_item in inventory_list:
            aisle_id = int(inv_item.aisleId) if hasattr(inv_item, "aisleId") else int(inv_item["aisleId"])
            external_row = inv_item.row if hasattr(inv_item, "row") else inv_item["row"]
            column = inv_item.column if hasattr(inv_item, "column") else inv_item["column"]
            level = inv_item.level if hasattr(inv_item, "level") else inv_item["level"]
            shelf = inv_item.shelf if hasattr(inv_item, "shelf") else inv_item.get("shelf")
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
                sku_features = self._extract_sku_features(sku_dict, feature_keys)

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
        return self._aisle_availability.get(aisle_id, {}).get("is_available", True)

    def _is_path_available(self, aisle_id: int, line_ref: Any, direction: str) -> bool:
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
        # Fallback: if one side uses numeric and another uses token, try numeric extraction.
        line_num = self._to_int_or_none(line_key)
        if line_num is not None and line_num in dir_map:
            return bool(dir_map[line_num])
        return True

    def is_inbound_path_available(self, aisle_id: int, in_line: Any) -> bool:
        return self._is_path_available(aisle_id, in_line, "inbound")

    def is_outbound_path_available(self, aisle_id: int, out_line: Any) -> bool:
        return self._is_path_available(aisle_id, out_line, "outbound")

    def convert_schedule_tasks(self, tasks: List[Any]) -> Tuple[List[TaskData], List[TaskData]]:
        inbound_tasks: List[TaskData] = []
        outbound_tasks: List[TaskData] = []
        add_plan_auto_index_counter: Dict[str, int] = {}

        for task in tasks:
            def _get_field(obj: Any, name: str, default: Any = None) -> Any:
                if isinstance(obj, dict):
                    return obj.get(name, default)
                return getattr(obj, name, default)

            task_id = _get_field(task, "taskId")
            task_type = _get_field(task, "taskType")
            skus = _get_field(task, "skus", [])

            sku_list = []
            for sku in skus:
                sku_dict = self._sku_entry_to_dict(sku)
                sku_dict["skuId"] = sku_dict.get("skuId") or sku_dict.get("sku")
                sku_dict["quantity"] = sku_dict.get("quantity", 1)
                sku_list.append(sku_dict)

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
                plan_str = str(plan_id).upper()
                if "LINE" in plan_str:
                    # Extract only the production line number after "LINE",
                    # avoid mixing in date digits from values like PLAN-LINE1-20260121.
                    m = re.search(r"LINE[-_]?(\d+)", plan_str)
                    if m:
                        production_line = int(m.group(1))
                elif str(plan_id).isdigit():
                    production_line = int(plan_id)
            if production_line is None and sku_list:
                first_sku = sku_list[0].get("skuId", "")
                pl_value = self._core.sku_to_production_line.get(first_sku, 1)
                production_line = int(pl_value[0]) if isinstance(pl_value, list) and pl_value else int(pl_value or 1)
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
            if plan_index is None and plan_id is not None:
                plan_id_key = str(plan_id)
                if any(k[0] == plan_id_key for k in self._add_plan_index_alias.keys()):
                    next_local_idx = add_plan_auto_index_counter.get(plan_id_key, 1)
                    plan_index = next_local_idx
                    add_plan_auto_index_counter[plan_id_key] = next_local_idx + 1
            if plan_index is not None:
                try:
                    plan_index_int = int(plan_index)
                    if plan_id is not None:
                        mapped = self._add_plan_index_alias.get((str(plan_id), plan_index_int))
                        normalized_plan_index = int(mapped) if mapped is not None else plan_index_int
                    else:
                        normalized_plan_index = plan_index_int
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
                # Empty-skid outbound is an ad-hoc operational request rather than
                # a production-plan step: do not bind it to plan/group progression.
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
        sku_ids = task.get_sku_ids() if hasattr(task, "get_sku_ids") else []
        if not sku_ids:
            for s in (task.skus or []):
                sid = s.get("skuId") if isinstance(s, dict) else getattr(s, "skuId", None)
                if sid:
                    sku_ids.append(sid)
        if not sku_ids:
            return None

        production_line = task.production_line or 1
        out_line = getattr(task, "out_line", None) or production_line
        match_mode = self._core._get_outbound_match_mode(production_line)
        feature_keys = self._get_match_fields(production_line)
        sku_features_by_idx = [self._extract_sku_features(self._sku_entry_to_dict(s), feature_keys) for s in (task.skus or [])]

        def can_use(pos: InventoryPosition) -> bool:
            return (
                (preferred_aisle is None or int(pos.aisle) == int(preferred_aisle))
                and self.is_outbound_path_available(pos.aisle, out_line)
                and not self._core.check_blockage(pos.aisle, out_line, current_time=self._core.current_time)
            )

        if len(sku_ids) == 1:
            sku = sku_ids[0]
            feats = sku_features_by_idx[0] if sku_features_by_idx else {}
            if match_mode == "features" and feats:
                candidates = self._core.inventory_manager.get_positions_by_features(feats, feature_keys, only_available=True)
            else:
                candidates = self._core.inventory_manager.get_sku_positions(sku, only_available=True)
            return next(([p] for p in candidates if can_use(p)), None)

        sku1, sku2 = sku_ids[0], sku_ids[1]
        feats1 = sku_features_by_idx[0] if len(sku_features_by_idx) > 0 else {}
        feats2 = sku_features_by_idx[1] if len(sku_features_by_idx) > 1 else {}

        for pos in self._core.inventory_manager.inventory_positions:
            if not pos.is_double_layer or not can_use(pos):
                continue
            if match_mode == "features" and feats1 and feats2:
                up1 = pos.upper_quantity > 0 and self._core.inventory_manager._features_match(pos.upper_features, feats1, feature_keys)
                low2 = pos.lower_quantity > 0 and self._core.inventory_manager._features_match(pos.lower_features, feats2, feature_keys)
                up2 = pos.upper_quantity > 0 and self._core.inventory_manager._features_match(pos.upper_features, feats2, feature_keys)
                low1 = pos.lower_quantity > 0 and self._core.inventory_manager._features_match(pos.lower_features, feats1, feature_keys)
                if (up1 and low2) or (up2 and low1):
                    return [pos]
            else:
                has1 = (pos.upper_sku == sku1 and pos.upper_quantity > 0) or (pos.lower_sku == sku1 and pos.lower_quantity > 0)
                has2 = (pos.upper_sku == sku2 and pos.upper_quantity > 0) or (pos.lower_sku == sku2 and pos.lower_quantity > 0)
                if has1 and has2:
                    return [pos]

        p1 = self._core.inventory_manager.get_positions_by_features(feats1, feature_keys, only_available=True) if (match_mode == "features" and feats1) else self._core.inventory_manager.get_sku_positions(sku1, only_available=True)
        p2 = self._core.inventory_manager.get_positions_by_features(feats2, feature_keys, only_available=True) if (match_mode == "features" and feats2) else self._core.inventory_manager.get_sku_positions(sku2, only_available=True)
        for a in p1:
            for b in p2:
                if can_use(a) and can_use(b):
                    return [a, b]
        return None

    def _build_unsubmitted_outbound_item(
        self,
        task: TaskData,
        reason: str,
        blocked_by_task_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload = self._task_brief_dict(task)
        payload["productionLine"] = getattr(task, "production_line", None)
        payload["reason"] = reason
        if blocked_by_task_id:
            payload["blockedByTaskId"] = blocked_by_task_id
        return payload

    def _prepare_outbound_tasks_for_submission(
        self,
        outbound_tasks: List[TaskData],
    ) -> Tuple[List[TaskData], List[Dict[str, Any]]]:
        ready_outbound: List[TaskData] = []
        unsubmitted_outbound: List[Dict[str, Any]] = []
        blocked_lines: Dict[int, str] = {}

        for task in outbound_tasks:
            production_line = int(getattr(task, "production_line", 1) or 1)
            blocking_task_id = blocked_lines.get(production_line)
            if blocking_task_id:
                reason = f"产线 {production_line} 的任务 {blocking_task_id} 当前无对应库存，本任务未提交。"
                unsubmitted_outbound.append(
                    self._build_unsubmitted_outbound_item(
                        task,
                        reason=reason,
                        blocked_by_task_id=blocking_task_id,
                    )
                )
                continue

            resolved_task = task
            if bool((getattr(task, "task_record", {}) or {}).get("empty_skid_request")):
                resolved_task = self._resolve_empty_skid_outbound(task) or task

            if not getattr(resolved_task, "positions", None):
                resolved_task.positions = self._find_positions_for_outbound_task(resolved_task) or []

            if not getattr(resolved_task, "positions", None):
                blocked_lines[production_line] = str(getattr(task, "task_id", "") or "")
                reason = f"任务 {task.task_id} 当前无对应库存，未提交；该产线后续出库任务一并拦截。"
                unsubmitted_outbound.append(
                    self._build_unsubmitted_outbound_item(
                        resolved_task,
                        reason=reason,
                    )
                )
                continue

            ready_outbound.append(resolved_task)

        return ready_outbound, unsubmitted_outbound

    def execute_schedule(
        self,
        tasks: Tuple[List[TaskData], List[TaskData]],
        frozen_tasks: Optional[Dict[int, TaskData]] = None,
    ) -> Tuple[Dict[int, Optional[TaskData]], List[Dict[str, Any]]]:
        self._sync_time()
        inbound_tasks, outbound_tasks = tasks
        frozen_tasks = {int(aisle): task for aisle, task in (frozen_tasks or {}).items() if task is not None}
        ready_outbound, unsubmitted_outbound = self._prepare_outbound_tasks_for_submission(outbound_tasks)

        for task in inbound_tasks:
            if not task.assigned_aisle:
                continue
            aisle = int(task.assigned_aisle)
            in_line = getattr(task, "in_line", None)
            production_line = getattr(task, "production_line", None)
            explicit_target_aisle = bool((getattr(task, "task_record", {}) or {}).get("target_aisle_explicit"))
            valid_aisles = self._core._get_valid_inbound_aisles(task, production_line)
            if aisle not in valid_aisles:
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

        # Keep empty-skid high-priority outbound tasks at queue front.
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
        for aisle in self._core.aisles:
            line_buckets: Dict[int, TaskData] = {}
            for t in self._core.pending_inbound_by_aisle.get(aisle, []):
                line = getattr(t, "in_line", 1)
                if line not in line_buckets:
                    line_buckets[line] = t
            inbound_for_schedule.extend(line_buckets.values())

        aisle_task_sequences = self._core.scheduler.solve(
            inbound_tasks=inbound_for_schedule,
            outbound_tasks=list(self._core.pending_outbound_queue),
            running_tasks=self._core.running_tasks,
            current_time=self._core.current_time,
        )

        result: Dict[int, Optional[TaskData]] = {}
        busy_aisles = {t.assigned_aisle for t in self._core.running_tasks.values() if getattr(t, "assigned_aisle", None)}
        busy_aisles.update(frozen_tasks.keys())
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

    def allocate_inbound_aisle(
        self,
        task_id: str,
        skus: List[Dict],
        in_line: Any = 1,
        out_line: Any = None,
        production_line: Any = None,
    ) -> int:
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
        return (
            int(getattr(position, "aisle", 0) or 0),
            int(getattr(position, "row", 0) or 0),
            int(getattr(position, "column", 0) or 0),
            int(getattr(position, "level", 0) or 0),
        )

    def _position_key_to_external(self, key: Tuple[int, int, int, int]) -> str:
        aisle, internal_row, column, level = key
        external_row = self._internal_to_external_row(int(internal_row), int(aisle))
        return f"{int(aisle)}-{int(external_row)}-{int(column)}-{int(level)}"

    def _free_slot_count(self, position: InventoryPosition) -> int:
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
        return [str(sku_id) for sku_id in (task.get_sku_ids() if hasattr(task, "get_sku_ids") else []) if sku_id]

    def _find_positions_for_inbound_task(
        self,
        task: TaskData,
        preferred_aisle: Optional[int] = None,
    ) -> Optional[List[InventoryPosition]]:
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
        for sku_entry in (task.skus or []):
            sku_dict = self._sku_entry_to_dict(sku_entry)
            required_rows.append(
                {
                    "skuId": str(sku_dict.get("skuId") or ""),
                    "features": self._extract_sku_features(sku_dict, feature_keys),
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
                if sku_id and row.get("skuId") != sku_id:
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

    def _apply_executing_inventory(self, task: TaskData) -> None:
        if task.task_type != TASK_TYPE_OUTBOUND:
            return
        task_record = dict(getattr(task, "task_record", {}) or {})
        if task_record.get("inventory_deducted_at_executing"):
            task.task_record = task_record
            return

        execution_inventory_entries: List[Dict[str, Any]] = []
        for sku_entry in (task.skus or []):
            sku_dict = self._sku_entry_to_dict(sku_entry)
            sku_id = str(sku_dict.get("skuId") or "")
            if not sku_id:
                continue
            execution_inventory_entries.append(
                {
                    "skuId": sku_id,
                    "features": dict(sku_dict.get("features") or {}),
                }
            )

        removal_entries: List[Dict[str, Any]] = []
        for idx, entry in enumerate(execution_inventory_entries):
            pos = task.positions[min(idx, len(task.positions) - 1)]
            actual_pos = self._core.inventory_manager.position_map.get(pos.get_position_id())
            if actual_pos is None:
                raise ValueError(f"货位 {pos.get_position_id()} 不存在，无法扣减出库库存。")

            removal_entry: Dict[str, Any] = {
                "positionId": pos.get_position_id(),
                "skuId": entry["skuId"],
                "quantity": 1,
                "features": {},
                "in_line": None,
                "out_line": None,
                "layer": None,
            }

            if getattr(actual_pos, "is_double_layer", False):
                feature_keys = list((entry.get("features") or {}).keys())
                lower_match = (
                    actual_pos.lower_quantity > 0
                    and str(actual_pos.lower_sku or "") == entry["skuId"]
                    and (
                        not feature_keys
                        or self._core.inventory_manager._features_match(
                            getattr(actual_pos, "lower_features", {}) or {},
                            entry.get("features") or {},
                            feature_keys,
                        )
                    )
                )
                upper_match = (
                    actual_pos.upper_quantity > 0
                    and str(actual_pos.upper_sku or "") == entry["skuId"]
                    and (
                        not feature_keys
                        or self._core.inventory_manager._features_match(
                            getattr(actual_pos, "upper_features", {}) or {},
                            entry.get("features") or {},
                            feature_keys,
                        )
                    )
                )
                if lower_match:
                    removal_entry["layer"] = "lower"
                    removal_entry["features"] = dict(getattr(actual_pos, "lower_features", {}) or {})
                    removal_entry["in_line"] = getattr(actual_pos, "lower_in_line", None)
                    removal_entry["out_line"] = getattr(actual_pos, "lower_out_line", None)
                elif upper_match:
                    removal_entry["layer"] = "upper"
                    removal_entry["features"] = dict(getattr(actual_pos, "upper_features", {}) or {})
                    removal_entry["in_line"] = getattr(actual_pos, "upper_in_line", None)
                    removal_entry["out_line"] = getattr(actual_pos, "upper_out_line", None)
                else:
                    raise ValueError(
                        f"货位 {pos.get_position_id()} 中未找到与任务要求一致的库存："
                        f"SKU={entry['skuId']}，features={entry.get('features') or {}}。"
                    )
            else:
                removal_entry["features"] = dict(getattr(actual_pos, "features", {}) or {})
                removal_entry["in_line"] = getattr(actual_pos, "in_line", None)
                removal_entry["out_line"] = getattr(actual_pos, "out_line", None)

            self._core.inventory_manager.remove_inventory(pos, entry["skuId"], 1)
            removal_entries.append(removal_entry)

        task_record["inventory_deducted_at_executing"] = True
        task_record["executing_inventory_entries"] = removal_entries
        task.task_record = task_record

    def _restore_executing_inventory(self, task: TaskData) -> None:
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
        self._sync_time()
        task_id = feedback.get("taskId")
        status = str(feedback.get("status", "")).upper()
        task_type = str(feedback.get("taskType", "")).upper()
        if not task_id:
            return {"success": False, "taskId": "", "status": status, "reason": "缺少 taskId。"}

        task = self.get_task_by_id(task_id, task_type=task_type)
        if status == "EXECUTING":
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

            override_aisle = self._to_int_or_none(feedback.get("aisleId"))
            mixed_assigned_aisle = self._to_int_or_none(getattr(task, "assigned_aisle", None))
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
            feedback_payload = dict(feedback)
            if task is not None:
                feedback_payload.update(self._feedback_position_payload(task))
            self._core.apply_task_feedback(feedback_payload)
            if not self._complete_task_execution(task_id):
                return {"success": False, "taskId": task_id, "status": status, "reason": "任务当前不在执行中。"}
            return self._build_feedback_response(task, task_id=task_id, status=status, success=True, message="任务执行完成。")
        if status == "FAILED":
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
        if not getattr(task, "positions", None):
            return

        if task.task_type == TASK_TYPE_INBOUND:
            sku_entries = [s for s in (task.skus or []) if isinstance(s, dict) and s.get("skuId") is not None]
            sku_ids = [s.get("skuId") for s in sku_entries]
            for idx, sku_id in enumerate(sku_ids):
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
                )
            return

        if task.task_type == TASK_TYPE_OUTBOUND:
            task_record = dict(getattr(task, "task_record", {}) or {})
            if not task_record.get("inventory_deducted_at_executing"):
                sku_ids = []
                for sku_entry in (task.skus or []):
                    if isinstance(sku_entry, dict):
                        sku_id = sku_entry.get("skuId")
                    else:
                        sku_id = getattr(sku_entry, "skuId", None)
                    if sku_id:
                        sku_ids.append(str(sku_id))

                for idx, sku_id in enumerate(sku_ids):
                    pos = task.positions[min(idx, len(task.positions) - 1)]
                    self._core.inventory_manager.remove_inventory(pos, sku_id, 1)

            production_line = getattr(task, "production_line", None)
            if production_line is not None:
                self._core.mark_outbound_completed(int(production_line), task, self._core.current_time)

    def _complete_task_execution(self, task_id: str) -> bool:
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
        self._pending_execution_tasks.pop(task_id, None)
        task = self._core.running_tasks.pop(task_id, None)
        self._core.pending_outbound_queue = [t for t in self._core.pending_outbound_queue if t.task_id != task_id]
        for aisle in list(self._core.pending_inbound_by_aisle.keys()):
            self._core.pending_inbound_by_aisle[aisle] = [t for t in self._core.pending_inbound_by_aisle[aisle] if t.task_id != task_id]
        if task is not None:
            self._restore_executing_inventory(task)
        return True

    def _clear_pending_only(self) -> None:
        self._core.pending_outbound_queue.clear()
        for aisle in list(self._core.pending_inbound_by_aisle.keys()):
            self._core.pending_inbound_by_aisle[aisle].clear()

    def _clear_all_assigned_and_pending(self) -> None:
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
    ) -> bool:
        self._sync_time()
        self._add_plan_index_alias = {}
        if isinstance(production_plan, dict) and "production_plan" in production_plan:
            core_plan = production_plan.get("production_plan", {}) or {}
        elif isinstance(production_plan, dict) and "plans" in production_plan:
            core_plan = self._build_core_production_plan(production_plan)
        elif hasattr(production_plan, "plans"):
            core_plan = self._build_core_production_plan(production_plan)
        else:
            core_plan = production_plan or {}
        if not update and ((isinstance(production_plan, dict) and "plans" in production_plan) or hasattr(production_plan, "plans")):
            self._add_plan_index_alias = self._build_add_plan_index_alias(production_plan)

        current_group_map = self._normalize_current_groups(current_groups, legacy_current_groups)
        has_explicit_current_groups = current_group_map is not None
        final_plan: Dict[int, List[Any]]
        if update:
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
            if bool(reset_assigned):
                self._clear_all_assigned_and_pending()
            else:
                self._clear_pending_only()
        if has_explicit_current_groups:
            self._validate_current_group_runtime_constraints(final_plan, current_group_map)
        self._core.set_production_plan(final_plan, current_groups=current_group_map)
        return True

    def set_current_groups(self, current_groups: Any = None, legacy_current_groups: Any = None) -> bool:
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
        for line_id, group_idx in (current_group_map or {}).items():
            group_count = len((production_plan or {}).get(int(line_id), []) or [])
            if int(group_idx) > group_count:
                raise ValueError(
                    f"产线 {int(line_id)} 设置的组 {int(group_idx) + 1} 超过当前上限 {group_count}"
                )

    def _extract_task_group_idx(self, task: TaskData) -> Optional[int]:
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
        return self._core.production_plan

    def get_running_tasks(self) -> Dict[str, TaskData]:
        return self._core.running_tasks.copy()

    def get_pending_tasks(self) -> Dict[str, List[TaskData]]:
        return {
            "inbound": {aisle: list(tasks) for aisle, tasks in self._core.pending_inbound_by_aisle.items()},
            "outbound": list(self._core.pending_outbound_queue),
        }

    def get_completed_tasks(self) -> List[TaskData]:
        return list(self._core.completed_tasks)

    def get_inventory_summary(self) -> Dict[int, Dict[str, int]]:
        return {aisle: {sku: qty for sku, qty in skus.items() if qty > 0} for aisle, skus in self._core.inventory_manager.current_inventory.items()}

    def get_full_inventory(self) -> List[Dict[str, Any]]:
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
                full_inventory.append({**base_info, "positions": [upper_entry, lower_entry]})
            else:
                entry = {"skuId": getattr(position, "sku", "") or "", "quantity": getattr(position, "quantity", 0) or 0}
                features = getattr(position, "features", {}) or {}
                for field in match_fields:
                    if field in features:
                        entry[field] = features[field]
                full_inventory.append({**base_info, "positions": [entry]})
        return full_inventory

    def get_aisle_status(self) -> Dict[int, Dict[str, Any]]:
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
    global _warehouse_service
    if _warehouse_service is None:
        _warehouse_service = WarehouseService()
    return _warehouse_service


def get_warehouse_service(request: Request) -> WarehouseService:
    global _warehouse_service
    service = getattr(request.app.state, "warehouse_service", None)
    if service is None:
        service = _get_or_create_warehouse_service()
        request.app.state.warehouse_service = service
    _warehouse_service = service
    return service


def init_warehouse_service(warehouse_core: Optional[WarehouseCore] = None) -> WarehouseService:
    global _warehouse_service
    _warehouse_service = WarehouseService(warehouse_core)
    return _warehouse_service


def reset_warehouse_service() -> None:
    global _warehouse_service
    _warehouse_service = None

