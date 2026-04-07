"""
生产计划接口路由
"""

from typing import Dict, List
from fastapi import APIRouter, HTTPException, Depends

from ..models import (
    ProductionPlanRequest,
    ApiResponse,
    OperationType,
)
from ..services.warehouse_service import get_warehouse_service, WarehouseService

router = APIRouter(prefix="/plan", tags=["生产计划"])


@router.post(
    "/production",
    response_model=ApiResponse,
    responses={
        200: {
            "description": "设置成功",
            "content": {"application/json": {"example": {
                "status": "SUCCESS",
                "message": "生产计划设置成功",
                "data": None
            }}}
        },
        422: {
            "description": "请求参数验证失败",
            "content": {"application/json": {"example": {
                "status": "FAILED",
                "message": "请求参数验证失败",
                "data": {"errors": ["body -> plans: field required"]}
            }}}
        },
        500: {
            "description": "服务器内部错误",
            "content": {"application/json": {"example": {
                "status": "FAILED",
                "message": "设置生产计划失败: ...",
                "data": None
            }}}
        },
    },
)
async def set_production_plan(
    request: ProductionPlanRequest,
    warehouse_service: WarehouseService = Depends(get_warehouse_service)
) -> ApiResponse:
    """
    设置/更新生产计划接口
    
    - ADD: 新增生产计划
    - UPDATE: 更新（替换）现有生产计划
    """
    try:
        # 转换生产计划格式为warehouse_core需要的格式
        # warehouse_core格式: {production_line: [[['sku1', 'sku2'], ['sku3', 'sku4']], ...]}
        # 每个产线的计划是一个 groups 列表，每个 group 包含多个 task，每个 task 是一个 SKU 列表
        production_plan = {}
        match_fields = list(getattr(warehouse_service.core, "match_fields", []) or [])
        production_plan_attrs: Dict[str, Dict[int, List]] = {field: {} for field in match_fields}
        
        for plan in request.plans:
            line_id = int(plan.lineId.replace("LINE-", "")) if "LINE-" in plan.lineId else int(plan.lineId)
            
            groups = []
            attrs_by_field = {field: [] for field in match_fields}
            # 遍历 planIndex 数组，每个元素对应一个组
            for group in plan.planIndex:
                # group.requiredSkus 已经是二维数组，外层是任务列表，内层是每个任务的 SKU
                tasks_in_group = []
                group_attrs_by_field = {field: [] for field in match_fields}
                for task_skus in group.requiredSkus:
                    # 展开该任务的所有 SKU（根据 quantity 展开）
                    sku_list = []
                    task_attrs_by_field = {field: [] for field in match_fields}
                    for sku in task_skus:
                        for _ in range(sku.quantity):
                            sku_list.append(sku.skuId)
                            for field in match_fields:
                                task_attrs_by_field[field].append(getattr(sku, field, None))
                    tasks_in_group.append(sku_list)
                    for field in match_fields:
                        group_attrs_by_field[field].append(task_attrs_by_field[field])
                groups.append(tasks_in_group)
                for field in match_fields:
                    attrs_by_field[field].append(group_attrs_by_field[field])
            
            production_plan[line_id] = groups
            for field in match_fields:
                production_plan_attrs[field][line_id] = attrs_by_field[field]
        
        # 调用warehouse_service设置生产计划
        is_update = request.operationType == OperationType.UPDATE
        plan_payload = {
            "production_plan": production_plan,
            "production_plan_attrs": production_plan_attrs if match_fields else {},
        }
        success = warehouse_service.set_production_plan(plan_payload, update=is_update)
        
        if success:
            return ApiResponse(status="SUCCESS", message="生产计划设置成功", data=None)
        else:
            return ApiResponse(status="FAILED", message="生产计划设置失败", data=None)
            
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"设置生产计划失败: {str(e)}"
        )


@router.get("/production")
async def get_production_plan(
    warehouse_service: WarehouseService = Depends(get_warehouse_service)
):
    """
    获取当前生产计划（调试接口）
    """
    try:
        plan = warehouse_service.get_production_plan()
        return {
            "status": "SUCCESS",
            "message": "获取生产计划成功",
            "data": {"production_plan": plan}
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"获取生产计划失败: {str(e)}"
        )

