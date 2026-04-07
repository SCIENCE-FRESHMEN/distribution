"""
Production plan routes.
"""

from fastapi import APIRouter, Depends

from ..models import ProductionPlanRequest, OperationType
from ..services.warehouse_service import get_warehouse_service, WarehouseService
from ..response import ok, fail

router = APIRouter(prefix="/plan", tags=["production-plan"])


@router.post("/production")
async def set_production_plan(
    request: ProductionPlanRequest,
    warehouse_service: WarehouseService = Depends(get_warehouse_service),
):
    try:
        production_plan = {}

        for plan in request.plans:
            line_id = int(plan.lineId.replace("LINE-", "")) if "LINE-" in plan.lineId else int(plan.lineId)
            match_fields = list(warehouse_service.core._get_outbound_match_features(line_id) or [])
            feature_fields = [f for f in match_fields if str(f).lower() != "rfid"]

            groups = []
            for group in plan.planIndex:
                tasks_in_group = []
                for task_skus in group.requiredSkus:
                    sku_list = []
                    for sku in task_skus:
                        for _ in range(sku.quantity):
                            sku_entry = {"skuId": sku.skuId}
                            if feature_fields:
                                features = {
                                    field: getattr(sku, field, None)
                                    for field in feature_fields
                                    if getattr(sku, field, None) is not None
                                }
                                if features:
                                    sku_entry["features"] = features
                            sku_list.append(sku_entry)
                    tasks_in_group.append(sku_list)
                groups.append(tasks_in_group)

            production_plan[line_id] = groups

        is_update = request.operationType == OperationType.UPDATE
        success = warehouse_service.set_production_plan({"production_plan": production_plan}, update=is_update)

        if success:
            return ok(status_code="SUCCESS", message="设置生产计划成功", data={"success": True})
        return fail(message="", http_status=500, data={"success": False})

    except Exception as e:
        return fail(message="设置生产计划失败", http_status=500, data={"detail": str(e)})


@router.get("/production")
async def get_production_plan(warehouse_service: WarehouseService = Depends(get_warehouse_service)):
    """
    获取生产计划
    """

    try:
        return ok(status_code="SUCCESS", message="ok", data={"production_plan": warehouse_service.get_production_plan()})
    except Exception as e:
        return fail(message="获取生产计划失败", http_status=500, data={"detail": str(e)})
