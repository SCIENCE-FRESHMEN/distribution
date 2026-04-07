"""Inbound allocation API routes."""

import uuid
from typing import Any, Dict, List

from fastapi import APIRouter, Depends

from ..models import InboundAllocateRequest
from ..response import fail, ok
from ..services.warehouse_service import WarehouseService, get_warehouse_service
from ..state import TaskStateManager, get_task_state_manager

router = APIRouter(prefix="/inbound", tags=["inbound"])


def _normalize_sku_features(core: Any, skus: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for s in skus or []:
        if not isinstance(s, dict):
            continue
        feats = s.get("features") if isinstance(s.get("features"), dict) else {}
        merged = dict(feats)
        # top-level fallback fields also allowed
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
            if fkey in feats and str(feats[fkey]).strip() in set(str(x).strip() for x in blocked_vals):
                violated.append({
                    "feature": fkey,
                    "value": feats[fkey],
                    "blocked_values": sorted([str(x) for x in blocked_vals]),
                })

    return {
        "taskId": task_id,
        "aisleId": str(aisle_id),
        "checked_count": checked_count,
        "passed": len(violated) == 0,
        "violated": violated,
    }


@router.post("/allocate")
async def allocate_inbound(
    request: InboundAllocateRequest,
    warehouse_service: WarehouseService = Depends(get_warehouse_service),
    task_manager: TaskStateManager = Depends(get_task_state_manager),
):
    """Recommend aisle for inbound tasks only."""
    _ = task_manager  # keep dependency for future flow consistency
    try:
        allocation_id = f"ALLOC-{uuid.uuid4().hex[:8].upper()}"
        assignments: List[Dict[str, Any]] = []
        checks: List[Dict[str, Any]] = []

        core = warehouse_service.core

        for task in request.tasks:
            skus = []
            for sku in task.skus:
                if hasattr(sku, "model_dump"):
                    skus.append(sku.model_dump())
                else:
                    skus.append(sku.dict())

            recommended_aisle = warehouse_service.allocate_inbound_aisle(
                task_id=task.taskId,
                skus=skus,
                in_line=getattr(task, "inLine", None),
                out_line=getattr(task, "outLine", None),
                production_line=getattr(task, "productionLine", None),
            )
            rec_aisle_str = str(recommended_aisle)
            assignments.append({"taskId": task.taskId, "recommendedAisle": rec_aisle_str})

            checks.append(_evaluate_forbidden(core, int(recommended_aisle), skus, task.taskId))

        violations = [x for x in checks if not x.get("passed", True)]
        code = 0 if not violations else 1001
        message = "分配成功" if not violations else "存在违反 aisle_forbidden 规则的分配结果"

        return ok(
            status_code=("PARTIAL_SUCCESS" if code else "SUCCESS"),
            message=message,
            data={
                "allocationId": allocation_id,
                "assignments": assignments,
                "checks": {
                    "aisle_forbidden": {
                        "rule": "driven by config/warehouse.json aisle_forbidden",
                        "checked_count": len(checks),
                        "passed": len(violations) == 0,
                        "violations": violations,
                    }
                },
            },
        )

    except Exception as e:
        return fail(message="入库分配失败", http_status=500, data={"detail": str(e)})
