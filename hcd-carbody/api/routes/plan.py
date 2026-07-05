"""Production plan routes."""

from fastapi import APIRouter, Depends

from ..models import OperationType, ProductionPlanRequest
from ..response import fail, ok
from ..services.warehouse_service import WarehouseService, get_warehouse_service

router = APIRouter(prefix="/plan", tags=["production-plan"])


@router.post("/production")
async def set_production_plan(
    request: ProductionPlanRequest,
    warehouse_service: WarehouseService = Depends(get_warehouse_service),
):
    try:
        is_update = request.operationType == OperationType.UPDATE
        success = warehouse_service.set_production_plan(
            request,
            update=is_update,
            reset_assigned=bool(getattr(request, "resetAssigned", False)),
        )
        if success:
            return ok(status_code="SUCCESS", message="生产计划更新成功。", data={"success": True})
        return fail(message="生产计划更新失败。", http_status=500, data={"success": False})
    except ValueError as e:
        return fail(message="生产计划参数校验失败。", http_status=400, data={"detail": str(e)})
    except Exception as e:
        return fail(message="生产计划更新失败。", http_status=500, data={"detail": str(e)})


@router.get("/production")
async def get_production_plan(warehouse_service: WarehouseService = Depends(get_warehouse_service)):
    """
    获取生产计划
    """

    try:
        return ok(status_code="SUCCESS", message="ok", data={"production_plan": warehouse_service.get_production_plan()})
    except Exception as e:
        return fail(message="获取生产计划失败。", http_status=500, data={"detail": str(e)})
