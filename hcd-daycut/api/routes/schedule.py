"""
混合调度接口路由
"""

"""混合调度路由。

本层负责 HTTP 模型校验和响应转换；候选生成、资源预占与位置冻结均委托给
``WarehouseService``，以保证 mixed、任务查询和反馈使用同一状态语义。
"""

import logging
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
logger = logging.getLogger("api.business")


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
    task_manager: TaskStateManager = Depends(get_task_state_manager),
) -> ApiResponse | JSONResponse:
    """混合调度接口

    为当前可执行的入库和出库任务进行统一调度，返回巷道分配结果。

    注意：`assignedTask` 是推荐结果；可执行性以 `executableTasksByAisle` 为准。

    Args:
        request (MixedScheduleRequest): 本次 HTTP 请求对应的 Pydantic 请求对象。
        warehouse_service (WarehouseService，可选): API 服务实例，负责同步状态、生成调度结果和处理反馈。
        task_manager (TaskStateManager，可选): 任务状态管理器，用于读取和更新 API 任务状态。

    Returns:
        ApiResponse | JSONResponse: 封装处理结果或错误信息的 HTTP 响应。
    """
    invalid_result = warehouse_service.find_invalid_skus(request.tasks, task_types=["INBOUND"])
    if invalid_result["invalidSkus"]:
        logger.warning(
            "event=mixed_schedule_rejected reason=invalid_bom_sku task_ids=%s invalid_skus=%s",
            [str(getattr(task, "taskId", "")) for task in request.tasks],
            invalid_result["invalidSkus"],
        )
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

    duplicate_task_ids = warehouse_service.find_duplicate_task_ids(request.tasks)
    if duplicate_task_ids:
        logger.warning("event=mixed_schedule_rejected reason=duplicate_request_task_ids task_ids=%s", duplicate_task_ids)
        return JSONResponse(
            status_code=400,
            content={
                "status": "FAILED",
                "message": "存在重复的taskId，无法执行调度。",
                "data": {
                    "duplicateTaskIds": duplicate_task_ids,
                    "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                }
            }
        )

    conflicting_task_ids = warehouse_service.find_existing_task_id_conflicts(request.tasks)
    if conflicting_task_ids:
        logger.warning("event=mixed_schedule_rejected reason=active_task_ids task_ids=%s", conflicting_task_ids)
        return JSONResponse(
            status_code=400,
            content={
                "status": "FAILED",
                "message": "存在已在系统中排队或执行的taskId，无法重复提交。",
                "data": {
                    "conflictingTaskIds": conflicting_task_ids,
                    "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                }
            }
        )

    try:
        if not warehouse_service.apply_inline_schedule_plan(request, task_manager=task_manager):
            logger.warning("event=mixed_schedule_rejected reason=production_plan_sync_failed")
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
        logger.warning("event=mixed_schedule_rejected reason=production_plan_validation_error error=%s", str(e))
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
        schedule_result, _matched_preview = warehouse_service.execute_schedule_with_preview(tasks)

        # 4. 构建响应
        schedule_id = f"SCH-{uuid.uuid4().hex[:8].upper()}"
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        aisle_assignments: List[AisleAssignmentResponse] = []
        def build_task_response(task_obj) -> AssignedTaskResponse:
            """构建任务响应对象

            Args:
                task_obj (Any): 当前处理的任务对象。

            Returns:
                AssignedTaskResponse: 本次接口调用得到的响应对象。
            """
            frozen_positions = warehouse_service.get_task_api_positions(task_obj)
            positions = [PositionInfo(**payload) for payload in frozen_positions] if frozen_positions else []

            task_type = TaskType.OUTBOUND if getattr(task_obj, "task_type", "") == "OUTBOUND" else TaskType.INBOUND
            public_plan_index = getattr(task_obj, "plan_index_public", None)
            if public_plan_index is None:
                core_group_idx = getattr(task_obj, "group_idx", None)
                public_plan_index = int(core_group_idx) + 1 if core_group_idx is not None else None
            return AssignedTaskResponse(
                taskId=getattr(task_obj, "task_id", ""),
                taskType=task_type,
                planId=getattr(task_obj, "plan_id", None),
                planIndex=public_plan_index,
                positions=positions if positions else None,
            )

        prioritized_executable_by_aisle = warehouse_service.get_prioritized_executable_tasks_by_aisle()
        executable_aisle_by_task_id = {}
        for executable_aisle, executable_tasks in prioritized_executable_by_aisle.items():
            for executable_task in executable_tasks or []:
                task_id = getattr(executable_task, "task_id", None)
                if task_id and task_id not in executable_aisle_by_task_id:
                    executable_aisle_by_task_id[task_id] = int(executable_aisle)

        for aisle_id, assigned_task in schedule_result.items():
            executable_tasks = list(prioritized_executable_by_aisle.get(int(aisle_id), []) or [])
            assigned_candidate = None
            if executable_tasks:
                assigned_candidate = executable_tasks[0]
            elif assigned_task is not None:
                assigned_task_id = getattr(assigned_task, "task_id", None)
                fixed_aisle = executable_aisle_by_task_id.get(assigned_task_id)
                if fixed_aisle is None or fixed_aisle == int(aisle_id):
                    assigned_candidate = assigned_task

            if assigned_candidate is None:
                matched_resp = []
                for t in executable_tasks:
                    matched_resp.append(build_task_response(t))
                aisle_assignments.append(AisleAssignmentResponse(
                    aisleId=str(aisle_id),
                    assignedTask=None,
                    matchedTasks=matched_resp or None,
                ))
            else:
                # assigned_response 在下方通过 build_task_response(assigned_task) 构造。
                assigned_response = build_task_response(assigned_candidate)

                matched_resp = []
                seen = set()
                for t in executable_tasks:
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

        executable_tasks_by_aisle = warehouse_service.get_executable_tasks_by_aisle()
        executable_payload = {}
        for aisle, tasks_for_aisle in executable_tasks_by_aisle.items():
            executable_payload[str(aisle)] = [build_task_response(t) for t in (tasks_for_aisle or [])]

        schedule_data = MixedScheduleResponse(
            scheduleId=schedule_id,
            timestamp=timestamp,
            aisleAssignments=aisle_assignments,
            executableTasksByAisle=executable_payload,
            unsubmittedOutboundTasks=warehouse_service.get_last_unsubmitted_outbound_tasks() or None,
        )
        schedule_payload = schedule_data.model_dump()
        normalization_notices = warehouse_service.get_last_schedule_notices()
        if normalization_notices:
            schedule_payload["normalizationNotices"] = normalization_notices
        assigned_summary = [
            {
                "aisleId": assignment.aisleId,
                "taskId": assignment.assignedTask.taskId,
                "taskType": assignment.assignedTask.taskType.value,
                "positions": [position.model_dump() for position in (assignment.assignedTask.positions or [])],
            }
            for assignment in aisle_assignments
            if assignment.assignedTask is not None
        ]
        logger.info(
            "event=mixed_schedule_success schedule_id=%s request_task_ids=%s assigned=%s unsubmitted_outbound=%s",
            schedule_id,
            [str(getattr(task, "taskId", "")) for task in request.tasks],
            assigned_summary,
            [str(task.get("taskId", "")) for task in (schedule_payload.get("unsubmittedOutboundTasks") or []) if isinstance(task, dict)],
        )
        return ApiResponse(
            status="SUCCESS",
            message="调度成功",
            data=schedule_payload
        )

    except ValueError as e:
        logger.warning(
            "event=mixed_schedule_validation_error task_ids=%s error=%s",
            [str(getattr(task, "taskId", "")) for task in request.tasks],
            str(e),
        )
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
    except Exception as e:
        logger.exception(
            "event=mixed_schedule_error task_ids=%s error=%s",
            [str(getattr(task, "taskId", "")) for task in request.tasks],
            str(e),
        )
        raise HTTPException(
            status_code=500,
            detail=f"调度执行失败: {str(e)}"
        )

