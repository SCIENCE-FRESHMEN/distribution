"""
混合调度接口路由
"""

import uuid
from datetime import datetime
from typing import List
from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import JSONResponse

from ..models import (
    MixedScheduleRequest,
    MixedScheduleResponse,
    AisleAssignmentResponse,
    AssignedTaskResponse,
    PositionInfo,
    ApiResponse,
    TaskType,
    ShelfPosition,
)
from ..state import get_task_state_manager, TaskStateManager
from ..services.warehouse_service import get_warehouse_service, WarehouseService

router = APIRouter(prefix="/schedule", tags=["调度"])


@router.post(
    "/mixed",
    response_model=ApiResponse,
    responses={
        200: {
            "description": "调度成功",
            "content": {"application/json": {"example": {
                "status": "SUCCESS",
                "message": "调度成功",
                "data": {
                    "scheduleId": "SCH-A1B2C3D4",
                    "timestamp": "2026-01-15T10:00:05Z",
                    "aisleAssignments": [
                        {
                            "aisleId": "1",
                            "assignedTask": {
                                "taskId": "OUTBOUND-R1-001",
                                "taskType": "OUTBOUND",
                                "planId": "PLAN-LINE1",
                                "planIndex": 1,
                                "positions": [{
                                    "row": 1, "column": 5, "level": 2,
                                    "shelf": "UPPER",
                                    "skuId": "2801021-H19H0", "quantity": 1
                                }]
                            }
                        },
                        {"aisleId": "2", "assignedTask": None}
                    ]
                }
            }}}
        },
        409: {
            "description": "存在未确认的任务",
            "content": {"application/json": {"example": {
                "status": "FAILED",
                "message": "存在未确认的任务，请等待反馈后再请求调度。",
                "data": {
                    "unconfirmed_tasks": ["OUTBOUND-R1-001"],
                    "timestamp": "2026-01-15T10:00:05Z"
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
                "message": "调度执行失败: ...",
                "data": None
            }}}
        },
    },
)
async def mixed_schedule(
    request: MixedScheduleRequest,
    warehouse_service: WarehouseService = Depends(get_warehouse_service),
    task_manager: TaskStateManager = Depends(get_task_state_manager)
) -> ApiResponse:
    """
    混合调度接口
    
    为当前可执行的入库和出库任务进行统一调度，返回巷道分配结果。
    
    注意：如果有未确认的任务（未收到EXECUTING反馈），将拒绝新的调度请求。
    """
    # 检查是否有未确认的任务
    if task_manager.has_unconfirmed_tasks():
        unconfirmed = list(task_manager.get_unconfirmed_tasks().keys())
        return JSONResponse(
            status_code=409,
            content={
                "status": "FAILED",
                "message": "存在未确认的任务，请等待反馈后再请求调度。",
                "data": {
                    "unconfirmed_tasks": unconfirmed,
                    "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
                }
            }
        )
    
    try:
        # 1. 同步外部状态到warehouse_core
        warehouse_service.sync_aisle_status(request.aisleStatus)
        warehouse_service.sync_inventory(request.inventory)
        
        # 2. 转换任务列表
        tasks = warehouse_service.convert_schedule_tasks(request.tasks)
        
        # 3. 执行调度决策
        schedule_result = warehouse_service.execute_schedule(tasks)
        
        # 4. 构建响应
        schedule_id = f"SCH-{uuid.uuid4().hex[:8].upper()}"
        timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        
        aisle_assignments: List[AisleAssignmentResponse] = []
        match_fields = list(getattr(warehouse_service.core, "match_fields", []) or [])
        
        for aisle_id, assigned_task in schedule_result.items():
            if assigned_task is None:
                aisle_assignments.append(AisleAssignmentResponse(
                    aisleId=str(aisle_id),
                    assignedTask=None
                ))
            else:
                # 构建分配的任务响应
                positions = []
                
                # 从任务的skus字段获取SKU信息
                task_skus = []
                for s in (assigned_task.skus or []):
                    if isinstance(s, dict):
                        task_skus.append(dict(s))
                    elif hasattr(s, "model_dump"):
                        task_skus.append(s.model_dump())
                    elif hasattr(s, "dict"):
                        task_skus.append(s.dict())
                    else:
                        task_skus.append({
                            "skuId": getattr(s, "skuId", ""),
                            "quantity": getattr(s, "quantity", 1),
                        })
                
                if assigned_task.positions:
                    for idx, pos in enumerate(assigned_task.positions):
                        shelf = None
                        if hasattr(pos, 'is_double_layer') and pos.is_double_layer:
                            # 根据库存确定shelf
                            if pos.upper_quantity > 0:
                                shelf = ShelfPosition.UPPER
                            elif pos.lower_quantity > 0:
                                shelf = ShelfPosition.LOWER
                        
                        # 优先使用任务中的SKU信息，如果没有则使用货位信息
                        sku_id = ""
                        quantity = 0
                        sku_attrs = {}
                        if idx < len(task_skus):
                            sku_dict = task_skus[idx] or {}
                            sku_id = sku_dict.get("skuId", "") or ""
                            quantity = sku_dict.get("quantity", 1) or 0
                            for field in match_fields:
                                if field in sku_dict:
                                    sku_attrs[field] = sku_dict.get(field)
                        if not sku_id:
                            sku_id = getattr(pos, 'upper_sku', None) or getattr(pos, 'lower_sku', None) or getattr(pos, 'sku', '') or ''
                        if not quantity:
                            quantity = getattr(pos, 'upper_quantity', 0) or getattr(pos, 'lower_quantity', 0) or getattr(pos, 'quantity', 0)
                        if match_fields and not sku_attrs:
                            if hasattr(pos, "is_double_layer") and pos.is_double_layer:
                                if shelf == ShelfPosition.UPPER:
                                    sku_attrs = getattr(pos, "upper_attrs", {}) or {}
                                elif shelf == ShelfPosition.LOWER:
                                    sku_attrs = getattr(pos, "lower_attrs", {}) or {}
                            if not sku_attrs:
                                sku_attrs = getattr(pos, "sku_attrs", {}) or {}
                        
                        # 转换内部 row 为外部 row: 外部 row = 2 * (aisle - 1) + 内部 row
                        external_row = 2 * (pos.aisle - 1) + pos.row
                        
                        position_payload = {
                            "row": external_row,
                            "column": pos.column,
                            "level": pos.level,
                            "shelf": shelf,
                            "skuId": sku_id,
                            "quantity": quantity,
                        }
                        for field in match_fields:
                            if field in sku_attrs:
                                position_payload[field] = sku_attrs.get(field)
                        positions.append(PositionInfo(**position_payload))
                
                task_type = TaskType.OUTBOUND if assigned_task.task_type == "OUTBOUND" else TaskType.INBOUND
                
                assigned_response = AssignedTaskResponse(
                    taskId=assigned_task.task_id,
                    taskType=task_type,
                    planId=getattr(assigned_task, 'plan_id', None),
                    planIndex=getattr(assigned_task, 'group_idx', None),
                    positions=positions if positions else None
                )
                
                aisle_assignments.append(AisleAssignmentResponse(
                    aisleId=str(aisle_id),
                    assignedTask=assigned_response
                ))
                
                # 将任务添加到待反馈队列
                task_manager.add_pending_task(
                    task_id=assigned_task.task_id,
                    task_type=assigned_task.task_type,
                    aisle_id=str(aisle_id)
                )
        
        schedule_data = MixedScheduleResponse(
            scheduleId=schedule_id,
            timestamp=timestamp,
            aisleAssignments=aisle_assignments
        )
        return ApiResponse(
            status="SUCCESS",
            message="调度成功",
            data=schedule_data.model_dump()
        )
        
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"调度执行失败: {str(e)}"
        )

