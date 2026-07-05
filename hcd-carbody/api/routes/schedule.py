"""Mixed scheduling API routes."""

import traceback
import uuid
from datetime import datetime
from typing import Any, Dict, List
import re

from fastapi import APIRouter, Depends

from ..models import MixedScheduleRequest, PositionInfo, TaskType
from ..response import fail, ok
from ..services.warehouse_service import WarehouseService, get_warehouse_service
from ..state import PendingTaskStatus, TaskStateManager, get_task_state_manager

router = APIRouter(prefix="/schedule", tags=["schedule"])


def _line_ref_to_token(core: Any, line_ref: Any, *, inbound: bool, aisle: Any = None) -> Any:
    if line_ref is None:
        return None
    s = str(line_ref).strip()
    if not s:
        return None
    if re.fullmatch(r"[lL]\d+[cC]\d+", s):
        return s.upper()
    # numeric line id -> map to dock token L{level}C{col}
    try:
        line_num = int(s)
    except Exception:
        return s
    try:
        est = getattr(core, "time_estimator", None)
        if inbound:
            col, level = est.resolve_inbound_dock(line_num, default_layer=1, aisle=aisle)
        else:
            col, level = est.resolve_outbound_dock(line_num, default_layer=1, aisle=aisle)
        return f"L{int(level)}C{int(col)}"
    except Exception:
        return s


def _extract_line_meta_from_positions(core: Any, positions: List[Any], *, inbound: bool) -> Any:
    if not positions:
        return None

    # Preferred: dedicated position-level metadata (not features payload).
    for pos in positions:
        if inbound:
            val = getattr(pos, "in_line", None)
            if val is not None and str(val).strip():
                return val
            for attr in ("upper_in_line", "lower_in_line"):
                v = getattr(pos, attr, None)
                if v is not None and str(v).strip():
                    return v
        else:
            val = getattr(pos, "out_line", None)
            if val is not None and str(val).strip():
                return val
            for attr in ("upper_out_line", "lower_out_line"):
                v = getattr(pos, attr, None)
                if v is not None and str(v).strip():
                    return v

    def _normalize_key(k: Any) -> str:
        return str(k).replace("_", "").strip().lower()

    def _pick_from_dict(d: Any) -> Any:
        if not isinstance(d, dict):
            return None
        # first pass: exact common names
        for key in ("inLine", "in_line") if inbound else ("outLine", "out_line"):
            if key in d and d[key] is not None and str(d[key]).strip():
                return d[key]
        # second pass: normalized key lookup
        for k, v in d.items():
            if v is None or not str(v).strip():
                continue
            if _normalize_key(k) in ("inline" if inbound else "outline",):
                return v
        return None

    for pos in positions:
        for feat in (
            getattr(pos, "features", None),
            getattr(pos, "upper_features", None),
            getattr(pos, "lower_features", None),
        ):
            val = _pick_from_dict(feat)
            if val is not None:
                return val
    return None


def _normalize_sku_features(core: Any, skus: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for s in skus or []:
        if not isinstance(s, dict):
            continue
        feats = s.get("features") if isinstance(s.get("features"), dict) else {}
        merged = dict(feats)
        for k, v in s.items():
            if k in ("skuId", "quantity", "features"):
                continue
            if k not in merged and v is not None:
                merged[k] = v

        norm: Dict[str, str] = {}
        for k, v in merged.items():
            if v is None:
                continue
            try:
                ck = core._canonical_feature_key(k) if hasattr(core, "_canonical_feature_key") else str(k)
            except Exception:
                ck = str(k)
            sv = str(v).strip()
            if ck and sv:
                norm[str(ck)] = sv
        if norm:
            rows.append(norm)
    return rows


def _evaluate_forbidden(core: Any, aisle_id: int, skus: List[Dict[str, Any]], task_id: str) -> Dict[str, Any]:
    rules = (getattr(core, "aisle_forbidden", {}) or {}).get(int(aisle_id), {})
    if not rules:
        return {
            "taskId": task_id,
            "aisleId": str(aisle_id),
            "checked_count": 0,
            "passed": True,
            "violated": [],
        }

    feat_rows = _normalize_sku_features(core, skus)
    violated: List[Dict[str, Any]] = []
    checked_count = 0
    for feats in feat_rows:
        for fkey, blocked_vals in rules.items():
            checked_count += 1
            blocked = set(str(x).strip() for x in blocked_vals)
            if fkey in feats and str(feats[fkey]).strip() in blocked:
                violated.append(
                    {
                        "feature": fkey,
                        "value": feats[fkey],
                        "blocked_values": sorted([str(x) for x in blocked_vals]),
                    }
                )

    return {
        "taskId": task_id,
        "aisleId": str(aisle_id),
        "checked_count": checked_count,
        "passed": len(violated) == 0,
        "violated": violated,
    }


def _build_aisle_forbidden_checks(
    core: Any,
    aisle_assignments: List[Dict[str, Any]],
    task_skus_map: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, Any]:
    checked: List[Dict[str, Any]] = []
    violations: List[Dict[str, Any]] = []
    for row in aisle_assignments:
        task = row.get("assignedTask")
        if not task:
            continue
        if str(task.get("taskType")) != TaskType.INBOUND.value:
            continue
        task_id = str(task.get("taskId"))
        aisle_id = int(row.get("aisleId"))
        skus = task_skus_map.get(task_id, [])
        item = _evaluate_forbidden(core, aisle_id, skus, task_id)
        checked.append(item)
        if not item.get("passed", True):
            violations.append(item)

    return {
        "rule": "driven by config/warehouse.json aisle_forbidden",
        "checked_count": len(checked),
        "passed": len(violations) == 0,
        "violations": violations,
    }


def _find_duplicate_task_ids(tasks: List[Any]) -> List[str]:
    seen = set()
    duplicates = set()
    for task in tasks or []:
        task_id = task.taskId if hasattr(task, "taskId") else task.get("taskId")
        if not task_id:
            continue
        task_id = str(task_id)
        if task_id in seen:
            duplicates.add(task_id)
        seen.add(task_id)
    return sorted(duplicates)


@router.post("/mixed")
async def mixed_schedule(
    request: MixedScheduleRequest,
    warehouse_service: WarehouseService = Depends(get_warehouse_service),
    task_manager: TaskStateManager = Depends(get_task_state_manager),
):
    """Unified inbound/outbound scheduling endpoint."""
    current_stage = "init"
    service_state = None
    task_manager_state = None
    try:
        duplicate_task_ids = _find_duplicate_task_ids(request.tasks or [])
        if duplicate_task_ids:
            return fail(
                message="存在重复的taskId，无法执行调度。",
                http_status=400,
                data={"duplicateTaskIds": duplicate_task_ids},
            )

        active_task_ids = warehouse_service.get_active_task_ids() | set(task_manager.get_all_pending_tasks().keys())
        duplicate_active_ids = sorted(
            {
                str(task.taskId if hasattr(task, "taskId") else task.get("taskId"))
                for task in (request.tasks or [])
                if str(task.taskId if hasattr(task, "taskId") else task.get("taskId")) in active_task_ids
            }
        )
        if duplicate_active_ids:
            return fail(
                message="存在已下发或执行中的taskId，无法重复提交。",
                http_status=400,
                data={"duplicateTaskIds": duplicate_active_ids},
            )

        service_state = warehouse_service.save_state()
        task_manager_state = task_manager.save_state()

        current_stage = "sync_production_context"
        if request.productionPlan is not None:
            operation_type = getattr(request.productionPlan, "operationType", None)
            op_value = str(getattr(operation_type, "value", operation_type or "")).upper()
            is_update = op_value == "UPDATE"
            reset_assigned = bool(getattr(request.productionPlan, "resetAssigned", False))
            warehouse_service.set_production_plan(
                request.productionPlan,
                update=is_update,
                reset_assigned=reset_assigned,
                current_groups=request.currentGroups,
                legacy_current_groups=request.productionLineCurrentGroup,
            )
            if is_update and reset_assigned:
                task_manager.clear_all()
        elif request.currentGroups is not None or request.productionLineCurrentGroup is not None:
            warehouse_service.set_current_groups(
                current_groups=request.currentGroups,
                legacy_current_groups=request.productionLineCurrentGroup,
            )

        current_stage = "sync_aisle_status"
        warehouse_service.sync_aisle_status(request.aisleStatus)

        current_stage = "sync_inventory"
        warehouse_service.sync_inventory(request.inventory)

        current_stage = "convert_schedule_tasks"
        tasks = warehouse_service.convert_schedule_tasks(request.tasks)

        task_skus_map: Dict[str, List[Dict[str, Any]]] = {}
        task_line_map: Dict[str, Dict[str, Any]] = {}
        for t in (request.tasks or []):
            tid = t.taskId if hasattr(t, "taskId") else t.get("taskId")
            skus = t.skus if hasattr(t, "skus") else t.get("skus", [])
            in_line_raw = t.inLine if hasattr(t, "inLine") else t.get("inLine")
            out_line_raw = t.outLine if hasattr(t, "outLine") else t.get("outLine")
            if tid is not None:
                task_line_map[str(tid)] = {
                    "inLine": in_line_raw,
                    "outLine": out_line_raw,
                }
            sku_dicts = []
            for s in skus:
                if hasattr(s, "model_dump"):
                    sku_dicts.append(s.model_dump())
                elif hasattr(s, "dict"):
                    sku_dicts.append(s.dict())
                elif isinstance(s, dict):
                    sku_dicts.append(dict(s))
            task_skus_map[str(tid)] = sku_dicts

        current_stage = "freeze_unconfirmed"
        unconfirmed_task_ids = sorted(task_manager.get_unconfirmed_tasks().keys())
        frozen_tasks: Dict[int, Any] = {}
        for task_id in unconfirmed_task_ids:
            pending = task_manager.get_task(task_id)
            if pending is None or pending.aisle_id is None:
                continue
            aisle_id = int(pending.aisle_id)
            frozen_task = warehouse_service.get_task_for_aisle(aisle_id, task_id=task_id)
            if frozen_task is not None:
                frozen_tasks[aisle_id] = frozen_task

        current_stage = "execute_schedule"
        schedule_result, unsubmitted_outbound_tasks = warehouse_service.execute_schedule(
            tasks,
            frozen_tasks=frozen_tasks,
        )

        current_stage = "build_response"
        schedule_id = f"SCH-{uuid.uuid4().hex[:8].upper()}"
        timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

        aisle_assignments: List[Dict[str, Any]] = []
        recommended_by_aisle = {
            int(aid): task
            for aid, task in schedule_result.items()
            if task is not None
        }
        prioritized_executable = warehouse_service.get_prioritized_executable_tasks_by_aisle(
            recommended_tasks=recommended_by_aisle
        )
        match_fields = list(warehouse_service.core._get_outbound_match_features(None) or [])

        for aisle_id, assigned_task in schedule_result.items():
            if assigned_task is None:
                matched = [
                    warehouse_service._task_brief_dict(t)
                    for t in prioritized_executable.get(int(aisle_id), [])
                ]
                aisle_assignments.append(
                    {"aisleId": str(aisle_id), "assignedTask": None, "matchedTasks": matched}
                )
                continue

            positions = []
            task_skus = []
            for s in (assigned_task.skus or []):
                if isinstance(s, dict):
                    task_skus.append(dict(s))
                elif hasattr(s, "model_dump"):
                    task_skus.append(s.model_dump())
                elif hasattr(s, "dict"):
                    task_skus.append(s.dict())
                else:
                    task_skus.append({"skuId": getattr(s, "skuId", ""), "quantity": getattr(s, "quantity", 1)})

            if assigned_task.positions:
                for idx, pos in enumerate(assigned_task.positions):
                    sku_id = ""
                    quantity = 0
                    sku_attrs: Dict[str, Any] = {}
                    if idx < len(task_skus):
                        sku_dict = task_skus[idx] or {}
                        sku_id = sku_dict.get("skuId", "") or ""
                        quantity = sku_dict.get("quantity", 1) or 0
                        for field in match_fields:
                            if field in sku_dict:
                                sku_attrs[field] = sku_dict.get(field)

                    if not sku_id:
                        sku_id = (
                            getattr(pos, "upper_sku", None)
                            or getattr(pos, "lower_sku", None)
                            or getattr(pos, "sku", "")
                            or ""
                        )
                    if not quantity:
                        quantity = (
                            getattr(pos, "upper_quantity", 0)
                            or getattr(pos, "lower_quantity", 0)
                            or getattr(pos, "quantity", 0)
                        )

                    if match_fields and not sku_attrs:
                        if hasattr(pos, "is_double_layer") and pos.is_double_layer:
                            sku_attrs = getattr(pos, "upper_features", {}) or {}
                            if not sku_attrs:
                                sku_attrs = getattr(pos, "lower_features", {}) or {}
                        if not sku_attrs:
                            sku_attrs = getattr(pos, "features", {}) or {}

                    external_row = 2 * (pos.aisle - 1) + pos.row
                    payload = {
                        "row": external_row,
                        "column": pos.column,
                        "level": pos.level,
                        "skuId": sku_id,
                        "quantity": quantity,
                    }
                    for field in match_fields:
                        if field in sku_attrs:
                            payload[field] = sku_attrs.get(field)
                    positions.append(PositionInfo(**payload).model_dump())

            task_type = TaskType.OUTBOUND.value if assigned_task.task_type == "OUTBOUND" else TaskType.INBOUND.value
            plan_id = getattr(assigned_task, "plan_id", None)
            raw_group_idx = getattr(assigned_task, "group_idx", None)
            plan_index = (int(raw_group_idx) + 1) if raw_group_idx is not None else None
            if (plan_id is None or plan_index is None) and isinstance(getattr(assigned_task, "task_id", None), str):
                m = re.match(r"^OUTBOUND_PL(\d+)_GP(\d+)_", assigned_task.task_id)
                if m:
                    pl_num = int(m.group(1))
                    gp_num = int(m.group(2))
                    if plan_index is None:
                        plan_index = gp_num
                    if plan_id is None:
                        plan_id = f"LINE-{pl_num}"

            assigned_response = {
                "taskId": assigned_task.task_id,
                "taskType": task_type,
                "planId": plan_id,
                "planIndex": plan_index,
                "inLine": _line_ref_to_token(
                    warehouse_service.core,
                    getattr(assigned_task, "in_line", None),
                    inbound=True,
                    aisle=getattr(assigned_task, "assigned_aisle", None),
                ),
                "outLine": _line_ref_to_token(
                    warehouse_service.core,
                    getattr(assigned_task, "out_line", None),
                    inbound=False,
                    aisle=getattr(assigned_task, "assigned_aisle", None),
                ),
                "positions": positions if positions else None,
            }

            # Prefer echoing original request line refs for the same task.
            req_lines = task_line_map.get(str(assigned_task.task_id), {})
            if req_lines.get("inLine") is not None:
                assigned_response["inLine"] = req_lines.get("inLine")
            if req_lines.get("outLine") is not None:
                assigned_response["outLine"] = req_lines.get("outLine")
            # For non-request-generated tasks, try inventory instance metadata.
            if req_lines.get("inLine") is None:
                meta_in = _extract_line_meta_from_positions(
                    warehouse_service.core,
                    getattr(assigned_task, "positions", []) or [],
                    inbound=True,
                )
                if meta_in is not None:
                    assigned_response["inLine"] = meta_in
            if req_lines.get("outLine") is None:
                meta_out = _extract_line_meta_from_positions(
                    warehouse_service.core,
                    getattr(assigned_task, "positions", []) or [],
                    inbound=False,
                )
                if meta_out is not None:
                    assigned_response["outLine"] = meta_out

            matched = [
                warehouse_service._task_brief_dict(t)
                for t in prioritized_executable.get(int(aisle_id), [])
            ]
            aisle_assignments.append(
                {"aisleId": str(aisle_id), "assignedTask": assigned_response, "matchedTasks": matched}
            )
            existing_pending = task_manager.get_task(str(assigned_task.task_id))
            if existing_pending is None:
                task_manager.add_pending_task(
                    task_id=assigned_task.task_id,
                    task_type=assigned_task.task_type,
                    aisle_id=str(aisle_id),
                )

        forbidden_check = _build_aisle_forbidden_checks(warehouse_service.core, aisle_assignments, task_skus_map)
        if not forbidden_check.get("passed", True):
            warehouse_service.restore_state(service_state)
            task_manager.restore_state(task_manager_state)
            return fail(
                message="命中禁配规则，调度失败。",
                http_status=400,
                data={
                    "reason": "本次请求命中 aisle_forbidden 禁配规则，调度结果未生效。",
                    "checks": {
                        "aisle_forbidden": forbidden_check,
                    },
                    "timestamp": timestamp,
                },
            )

        return ok(
            status_code="SUCCESS",
            message="调度成功。",
            data={
                "scheduleId": schedule_id,
                "timestamp": timestamp,
                "unconfirmedTaskIds": unconfirmed_task_ids,
                "aisleAssignments": aisle_assignments,
                "unsubmittedOutboundTasks": unsubmitted_outbound_tasks,
                "executableTasksByAisle": {
                    str(aisle): [warehouse_service._task_brief_dict(t) for t in tasks]
                    for aisle, tasks in prioritized_executable.items()
                },
                "checks": {
                    "aisle_forbidden": forbidden_check,
                },
            },
        )

    except ValueError as e:
        if service_state is not None and task_manager_state is not None:
            warehouse_service.restore_state(service_state)
            task_manager.restore_state(task_manager_state)
        return fail(
            message="请求参数校验失败。",
            http_status=400,
            data={
                "stage": current_stage,
                "detail": str(e),
                "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )
    except Exception as e:
        if service_state is not None and task_manager_state is not None:
            warehouse_service.restore_state(service_state)
            task_manager.restore_state(task_manager_state)
        tb_lines = traceback.format_exc().splitlines()
        return fail(
            message="调度执行失败。",
            http_status=500,
            data={
                "stage": current_stage,
                "exception_type": type(e).__name__,
                "exception_message": str(e),
                "request_summary": {
                    "aisle_status_count": len(request.aisleStatus or []),
                    "inventory_count": len(request.inventory or []),
                    "task_count": len(request.tasks or []),
                    "task_ids": [
                        (t.taskId if hasattr(t, "taskId") else t.get("taskId"))
                        for t in (request.tasks or [])
                    ],
                },
                "traceback_tail": tb_lines[-12:],
                "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )
