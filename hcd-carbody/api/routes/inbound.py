"""Inbound allocation API routes."""

import logging
import uuid
from typing import Any, Dict, List

from fastapi import APIRouter, Depends

from ..models import InboundAllocateRequest
from ..response import fail, ok
from ..services.warehouse_service import WarehouseService, get_warehouse_service
from ..state import TaskStateManager, get_task_state_manager

router = APIRouter(prefix="/inbound", tags=["inbound"])
logger = logging.getLogger("api.business")


# ==========================================================================
# 辅助函数：SKU 属性归一化与禁配校验
# ==========================================================================
def _normalize_sku_features(core: Any, skus: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """合并嵌套和顶层特征，并转换为核心统一的特征键和值。

    Args:
        core: WarehouseCore，用于执行特征别名归一化。
        skus: API 入库任务的 SKU 条目，允许 ``features`` 和顶层扩展字段并存。

    Returns:
        List[Dict[str, str]]: 每个有效 SKU 对应一组非空标准化特征。
    """
    rows: List[Dict[str, str]] = []
    for s in skus or []:
        if not isinstance(s, dict):
            continue
        raw_features = s.get("features")
        feats: Dict[str, Any] = raw_features if isinstance(raw_features, dict) else {}
        merged = dict(feats)
        # 允许使用 SKU 字典顶层字段作为特征回退值。
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
    """核验本次入库 SKU 是否命中目标巷道的特征禁配规则。

    Args:
        core: 仓库核心，提供 ``aisle_forbidden`` 和特征键归一化方法。
        aisle_id: 本次分配得到的内部巷道号。
        skus: 当前入库任务的 SKU 与属性。
        task_id: 用于返回诊断结果的任务标识。

    Returns:
        Dict[str, Any]: 包含已检查规则数、通过标志和违反项的诊断对象。
    """
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


# ==========================================================================
# 主函数：入库巷道和货位推荐 API 路由
# ==========================================================================
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
        if violations:
            logger.warning(
                "event=inbound_allocate_rejected allocation_id=%s task_ids=%s reason=aisle_forbidden violations=%s",
                allocation_id,
                [str(task.taskId) for task in request.tasks],
                violations,
            )
            return fail(
                message="命中禁配规则，入库分配失败。",
                http_status=400,
                data={
                    "reason": "本次请求命中 aisle_forbidden 禁配规则，分配结果未生效。",
                    "allocationId": allocation_id,
                    "checks": {
                        "aisle_forbidden": {
                            "rule": "driven by config/warehouse.json aisle_forbidden",
                            "checked_count": len(checks),
                            "passed": False,
                            "violations": violations,
                        }
                    },
                },
            )

        logger.info(
            "event=inbound_allocate_success allocation_id=%s assignments=%s",
            allocation_id,
            assignments,
        )
        return ok(
            status_code="SUCCESS",
            message="分配成功",
            data={
                "allocationId": allocation_id,
                "assignments": assignments,
                "checks": {
                    "aisle_forbidden": {
                        "rule": "driven by config/warehouse.json aisle_forbidden",
                        "checked_count": len(checks),
                        "passed": True,
                        "violations": [],
                    }
                },
            },
        )

    except Exception as e:
        logger.exception("event=inbound_allocate_error task_ids=%s", [str(task.taskId) for task in request.tasks])
        return fail(message="入库分配失败", http_status=500, data={"detail": str(e)})
