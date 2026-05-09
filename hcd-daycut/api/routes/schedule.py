"""
混合调度接口路由
"""

import uuid
from datetime import datetime, timezone
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
                    "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                }
            }
        )

    invalid_result = warehouse_service.find_invalid_skus(request.tasks, task_types=["INBOUND"])
    if invalid_result["invalidSkus"]:
        return JSONResponse(
            status_code=400,
            content={
                "status": "FAILED",
                "message": "存在未维护在BOM中的SKU，无法执行调度。",
                "data": {
                    **invalid_result,
                    "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                }
            }
        )

    try:
        if not warehouse_service.apply_inline_schedule_plan(request):
            return JSONResponse(
                status_code=400,
                content={
                    "status": "FAILED",
                    "message": "生产计划同步失败",
                    "data": {
                        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    }
                }
            )
    except ValueError as e:
        return JSONResponse(
            status_code=400,
            content={
                "status": "FAILED",
                "message": str(e),
                "data": {
                    "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
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
        schedule_result, matched_preview = warehouse_service.execute_schedule_with_preview(tasks)
        
        # 4. 构建响应
        schedule_id = f"SCH-{uuid.uuid4().hex[:8].upper()}"
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        
        aisle_assignments: List[AisleAssignmentResponse] = []
        match_fields = list(getattr(warehouse_service.core, "match_fields", []) or [])

        def build_position(shelf: ShelfPosition, sku_id: str, quantity: int, sku_attrs: dict, pos) -> PositionInfo:
            """构建单个position对象"""
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
            return PositionInfo(**position_payload)

        def build_task_response(task_obj) -> AssignedTaskResponse:
            """把 TaskData 转成接口返回的 AssignedTaskResponse。"""
            positions = []

            task_skus = []
            for s in (getattr(task_obj, "skus", None) or []):
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

            if getattr(task_obj, "positions", None):
                is_outbound_task = getattr(task_obj, "task_type", "") == "OUTBOUND"

                for idx, pos in enumerate(task_obj.positions):
                    is_double_layer = hasattr(pos, 'is_double_layer') and pos.is_double_layer
                    has_upper = is_double_layer and getattr(pos, 'upper_quantity', 0) > 0
                    has_lower = is_double_layer and getattr(pos, 'lower_quantity', 0) > 0

                    task_sku_dict = task_skus[idx] if idx < len(task_skus) else {}

                    if is_outbound_task and has_upper and has_lower:
                        upper_sku_id = getattr(pos, 'upper_sku', '') or ''
                        upper_quantity = getattr(pos, 'upper_quantity', 0)
                        upper_attrs = dict(getattr(pos, "upper_attrs", {}) or {})
                        for field in match_fields:
                            if field in task_sku_dict:
                                upper_attrs[field] = task_sku_dict.get(field)
                        positions.append(build_position(ShelfPosition.UPPER, upper_sku_id, upper_quantity, upper_attrs, pos))

                        lower_sku_id = getattr(pos, 'lower_sku', '') or ''
                        lower_quantity = getattr(pos, 'lower_quantity', 0)
                        lower_attrs = dict(getattr(pos, "lower_attrs", {}) or {})
                        for field in match_fields:
                            if field in task_sku_dict:
                                lower_attrs[field] = task_sku_dict.get(field)
                        positions.append(build_position(ShelfPosition.LOWER, lower_sku_id, lower_quantity, lower_attrs, pos))

                    elif is_outbound_task and (has_upper or has_lower):
                        if has_upper:
                            shelf = ShelfPosition.UPPER
                            sku_id = getattr(pos, 'upper_sku', '') or ''
                            quantity = getattr(pos, 'upper_quantity', 0)
                            sku_attrs = dict(getattr(pos, "upper_attrs", {}) or {})
                        else:  # has_lower
                            shelf = ShelfPosition.LOWER
                            sku_id = getattr(pos, 'lower_sku', '') or ''
                            quantity = getattr(pos, 'lower_quantity', 0)
                            sku_attrs = dict(getattr(pos, "lower_attrs", {}) or {})

                        for field in match_fields:
                            if field in task_sku_dict:
                                sku_attrs[field] = task_sku_dict.get(field)

                        positions.append(build_position(shelf, sku_id, quantity, sku_attrs, pos))

                    elif not is_outbound_task:
                        if is_double_layer:
                            has_upper_space = getattr(pos, 'upper_quantity', 0) == 0
                            has_lower_space = getattr(pos, 'lower_quantity', 0) == 0

                            if has_upper_space and has_lower_space:
                                sku_id = task_sku_dict.get("skuId", "") or ''
                                quantity = task_sku_dict.get("quantity", 0) or 1
                                sku_attrs = dict(getattr(pos, "upper_attrs", {}) or {})
                                for field in match_fields:
                                    if field in task_sku_dict:
                                        sku_attrs[field] = task_sku_dict.get(field)
                                if sku_id:
                                    positions.append(build_position(ShelfPosition.UPPER, sku_id, quantity, sku_attrs, pos))

                            elif has_upper_space:
                                sku_id = task_sku_dict.get("skuId", "") or ''
                                quantity = task_sku_dict.get("quantity", 0) or 1
                                sku_attrs = dict(getattr(pos, "upper_attrs", {}) or {})
                                for field in match_fields:
                                    if field in task_sku_dict:
                                        sku_attrs[field] = task_sku_dict.get(field)
                                if sku_id:
                                    positions.append(build_position(ShelfPosition.UPPER, sku_id, quantity, sku_attrs, pos))

                            elif has_lower_space:
                                sku_id = task_sku_dict.get("skuId", "") or ''
                                quantity = task_sku_dict.get("quantity", 0) or 1
                                sku_attrs = dict(getattr(pos, "lower_attrs", {}) or {})
                                for field in match_fields:
                                    if field in task_sku_dict:
                                        sku_attrs[field] = task_sku_dict.get(field)
                                if sku_id:
                                    positions.append(build_position(ShelfPosition.LOWER, sku_id, quantity, sku_attrs, pos))

                        else:
                            sku_id = getattr(pos, 'sku', '') or task_sku_dict.get("skuId", "") or ''
                            quantity = getattr(pos, 'quantity', 0) or task_sku_dict.get("quantity", 0) or 1
                            sku_attrs = dict(getattr(pos, "sku_attrs", {}) or {})

                            for field in match_fields:
                                if field in task_sku_dict:
                                    sku_attrs[field] = task_sku_dict.get(field)

                            if sku_id:
                                positions.append(build_position(None, sku_id, quantity, sku_attrs, pos))

            task_type = TaskType.OUTBOUND if getattr(task_obj, "task_type", "") == "OUTBOUND" else TaskType.INBOUND
            public_plan_index = getattr(task_obj, "plan_index_public", None)
            if public_plan_index is None:
                core_group_idx = getattr(task_obj, "group_idx", None)
                public_plan_index = int(core_group_idx) + 1 if core_group_idx is not None else None
            return AssignedTaskResponse(
                taskId=getattr(task_obj, "task_id", ""),
                taskType=task_type,
                planId=getattr(task_obj, 'plan_id', None),
                planIndex=public_plan_index,
                positions=positions if positions else None,
            )

        for aisle_id, assigned_task in schedule_result.items():
            if assigned_task is None:
                matched_resp = []
                for t in (matched_preview.get(int(aisle_id), []) or []):
                    matched_resp.append(build_task_response(t))
                aisle_assignments.append(AisleAssignmentResponse(
                    aisleId=str(aisle_id),
                    assignedTask=None,
                    matchedTasks=matched_resp or None,
                ))
            else:
                # assigned_response built via build_task_response(assigned_task) below
                assigned_response = build_task_response(assigned_task)

                matched_resp = []
                seen = set()
                for t in (matched_preview.get(int(aisle_id), []) or []):
                    tid = getattr(t, "task_id", "")
                    if not tid or tid in seen:
                        continue
                    matched_resp.append(build_task_response(t))
                    seen.add(tid)
                
                aisle_assignments.append(AisleAssignmentResponse(
                    aisleId=str(aisle_id),
                    assignedTask=assigned_response,
                    matchedTasks=matched_resp or None,
                ))
                
                # 将任务添加到待反馈队列
                if assigned_task.task_id not in (warehouse_service.core.running_tasks or {}):
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

