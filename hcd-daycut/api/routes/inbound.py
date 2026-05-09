"""
入库分配接口路由
"""

import uuid
from datetime import datetime, timezone
from typing import List
from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import JSONResponse

from ..models import (
    InboundAllocateRequest,
    InboundAllocateResponse,
    InboundAssignmentResponse,
    ApiResponse,
)
from ..state import get_task_state_manager, TaskStateManager
from ..services.warehouse_service import get_warehouse_service, WarehouseService

router = APIRouter(prefix="/inbound", tags=["入库分配"])


@router.post(
    "/allocate",
    response_model=ApiResponse,
    responses={
        200: {
            "description": "分配成功",
            "content": {"application/json": {"example": {
                "status": "SUCCESS",
                "message": "入库分配成功",
                "data": {
                    "allocationId": "ALLOC-E5F6G7H8",
                    "assignments": [
                        {"taskId": "INBOUND-TEST-001", "recommendedAisle": "2"}
                    ]
                }
            }}}
        },
        422: {
            "description": "请求参数验证失败",
            "content": {"application/json": {"example": {
                "status": "FAILED",
                "message": "请求参数验证失败",
                "data": {"errors": ["body -> tasks: field required"]}
            }}}
        },
        500: {
            "description": "服务器内部错误",
            "content": {"application/json": {"example": {
                "status": "FAILED",
                "message": "入库分配失败: ...",
                "data": None
            }}}
        },
    },
)
async def allocate_inbound(
    request: InboundAllocateRequest,
    warehouse_service: WarehouseService = Depends(get_warehouse_service),
    task_manager: TaskStateManager = Depends(get_task_state_manager)
) -> ApiResponse:
    """
    入库巷道分配接口
    
    为入库任务分配推荐的目标巷道。
    
    注意：此接口仅返回推荐巷道，不会将任务加入待反馈队列。
    实际的任务执行需要通过混合调度接口或直接调用。
    """
    try:
        invalid_result = warehouse_service.find_invalid_skus(request.tasks)
        if invalid_result["invalidSkus"]:
            return JSONResponse(
                status_code=400,
                content={
                    "status": "FAILED",
                    "message": "存在未维护在BOM中的SKU，无法执行入库分配。",
                    "data": {
                        **invalid_result,
                        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    }
                }
            )

        allocation_id = f"ALLOC-{uuid.uuid4().hex[:8].upper()}"
        assignments: List[InboundAssignmentResponse] = []
        
        for task in request.tasks:
            # 转换SKU信息
            skus = []
            for sku in task.skus:
                if hasattr(sku, "model_dump"):
                    skus.append(sku.model_dump())
                else:
                    skus.append(sku.dict())

            # 调用warehouse_service进行巷道分配
            recommended_aisle = warehouse_service.allocate_inbound_aisle(
                task_id=task.taskId,
                skus=skus
            )
            
            assignments.append(InboundAssignmentResponse(
                taskId=task.taskId,
                recommendedAisle=str(recommended_aisle)
            ))
        
        allocate_data = InboundAllocateResponse(
            allocationId=allocation_id,
            assignments=assignments
        )
        return ApiResponse(
            status="SUCCESS",
            message="入库分配成功",
            data=allocate_data.model_dump()
        )
        
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"入库分配失败: {str(e)}"
        )

