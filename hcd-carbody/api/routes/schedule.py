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
from ..state import TaskStateManager, get_task_state_manager

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


@router.post("/mixed")
async def mixed_schedule(
    request: MixedScheduleRequest,
    warehouse_service: WarehouseService = Depends(get_warehouse_service),
    task_manager: TaskStateManager = Depends(get_task_state_manager),
):
    """Unified inbound/outbound scheduling endpoint."""
    if task_manager.has_unconfirmed_tasks():
        unconfirmed = list(task_manager.get_unconfirmed_tasks().keys())
        return fail(
            message="存在未确认的已派发任务，请先确认后再请求新调度",
            http_status=409,
            data={
                "unconfirmed_tasks": unconfirmed,
                "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )

    current_stage = "init"
    try:
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

        current_stage = "execute_schedule"
        schedule_result = warehouse_service.execute_schedule(tasks)

        current_stage = "build_response"
        schedule_id = f"SCH-{uuid.uuid4().hex[:8].upper()}"
        timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

        aisle_assignments: List[Dict[str, Any]] = []
        match_fields = list(warehouse_service.core._get_outbound_match_features(None) or [])

        for aisle_id, assigned_task in schedule_result.items():
            if assigned_task is None:
                aisle_assignments.append({"aisleId": str(aisle_id), "assignedTask": None})
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
            plan_index = getattr(assigned_task, "group_idx", None)
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

            aisle_assignments.append({"aisleId": str(aisle_id), "assignedTask": assigned_response})
            task_manager.add_pending_task(task_id=assigned_task.task_id, task_type=assigned_task.task_type, aisle_id=str(aisle_id))

        forbidden_check = _build_aisle_forbidden_checks(warehouse_service.core, aisle_assignments, task_skus_map)
        code = 0 if forbidden_check.get("passed", True) else 1001
        msg = "调度成功" if code == 0 else "存在违反 aisle_forbidden 规则的分配结果"

        return ok(
            status_code=("PARTIAL_SUCCESS" if code else "SUCCESS"),
            message=msg,
            data={
                "scheduleId": schedule_id,
                "timestamp": timestamp,
                "aisleAssignments": aisle_assignments,
                "checks": {
                    "aisle_forbidden": forbidden_check,
                },
            },
        )

    except Exception as e:
        tb_lines = traceback.format_exc().splitlines()
        return fail(
            message="调度执行失败",
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
