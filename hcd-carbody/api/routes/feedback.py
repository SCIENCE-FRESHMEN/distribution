"""Task feedback routes."""

import logging

from fastapi import APIRouter, Depends

from ..models import TaskAdjustRequest, TaskFeedbackRequest, TaskStatus
from ..response import fail, ok
from ..services.warehouse_service import WarehouseService, _get_or_create_warehouse_service, get_warehouse_service
from ..state import TaskStateManager, get_task_state_manager

router = APIRouter(prefix="/task", tags=["task-feedback"])
logger = logging.getLogger("api.business")


# ==========================================================================
# 主函数：任务反馈、待处理查询与入库调整 API 路由
# ==========================================================================
@router.post("/feedback")
async def task_feedback(
    request: TaskFeedbackRequest,
    warehouse_service: WarehouseService = Depends(get_warehouse_service),
    task_manager: TaskStateManager = Depends(get_task_state_manager),
):
    """
    任务执行反馈接口

    外部系统报告任务执行状态。

    状态说明：
    - EXECUTING: 任务正在执行中（收到此状态表示指令已成功传输）
    - COMPLETED: 任务已完成
    - FAILED: 任务执行失败
    """
    task_id = request.taskId
    pending_task = task_manager.get_task(task_id)

    try:
        feedback_payload = {
            "taskId": task_id,
            "taskType": request.taskType.value,
            "status": request.status.value,
            "startTime": request.startTime,
            "aisleId": request.aisleId,
            "positions": [pos.model_dump() for pos in (request.positions or [])],
            "reason": request.failureReason,
        }
        result = warehouse_service.apply_feedback(feedback_payload)
        if not result.get("success", False):
            logger.warning(
                "event=task_feedback_rejected task_id=%s task_type=%s status=%s aisle_id=%s reason=%s",
                task_id,
                request.taskType.value,
                request.status.value,
                request.aisleId,
                result.get("reason", "未知原因。"),
            )
            return fail(
                message="任务反馈被拒绝。",
                http_status=400,
                data={
                    "taskId": task_id,
                    "reason": result.get("reason", "未知原因。"),
                },
            )

        if request.status == TaskStatus.EXECUTING:
            if pending_task:
                if result.get("aisleId") is not None:
                    pending_task.aisle_id = str(result.get("aisleId"))
                task_manager.confirm_task(task_id)
        elif request.status == TaskStatus.COMPLETED:
            if pending_task:
                task_manager.complete_task(task_id)
        elif request.status == TaskStatus.FAILED:
            if pending_task:
                task_manager.fail_task(task_id)

        logger.info(
            "event=task_feedback_success task_id=%s task_type=%s status=%s aisle_id=%s positions=%s",
            task_id,
            request.taskType.value,
            request.status.value,
            result.get("aisleId"),
            result.get("positions") or [],
        )
        return ok(
            status_code="SUCCESS",
            message=result.get("message", "任务反馈处理成功。"),
            data={
                "taskId": task_id,
                "status": request.status.value,
                "aisleId": result.get("aisleId"),
                "positions": result.get("positions"),
                "reason": result.get("reason"),
            },
        )
    except ValueError as e:
        logger.warning("event=task_feedback_validation_error task_id=%s reason=%s", task_id, str(e))
        return fail(
            message="任务反馈参数校验失败。",
            http_status=400,
            data={"taskId": task_id, "reason": str(e)},
        )
    except Exception as e:
        logger.exception("event=task_feedback_error task_id=%s", task_id)
        return fail(
            message="任务反馈处理失败。",
            http_status=500,
            data={"detail": str(e), "taskId": task_id},
        )


@router.get("/pending")
async def get_pending_tasks(
    task_manager: TaskStateManager = Depends(get_task_state_manager),
    warehouse_service: WarehouseService = Depends(get_warehouse_service),
):
    """
    获取所有待处理任务（调试接口）

    返回当前所有待反馈确认的任务列表。
    """
    merged_tasks = []
    seen_task_ids = set()
    pending = task_manager.get_all_pending_tasks()
    service_pending = warehouse_service.get_pending_tasks()
    inbound_pending = service_pending.get("inbound", {}) if isinstance(service_pending, dict) else {}
    outbound_pending = service_pending.get("outbound", []) if isinstance(service_pending, dict) else []
    running_tasks = warehouse_service.get_running_tasks()

    for task in running_tasks.values():
        task_id = str(getattr(task, "task_id", "") or "")
        if not task_id or task_id in seen_task_ids:
            continue
        merged_tasks.append(
            {
                "task_id": task_id,
                "task_type": getattr(task, "task_type", None),
                "aisle_id": str(warehouse_service._get_task_effective_aisle(task)) if warehouse_service._get_task_effective_aisle(task) is not None else None,
                "status": "EXECUTING",
                "created_at": None,
                "confirmed_at": None,
                "is_timeout": False,
                "source": "running_tasks",
            }
        )
        seen_task_ids.add(task_id)

    for aisle_id, tasks in (inbound_pending or {}).items():
        for task in tasks or []:
            task_id = str(getattr(task, "task_id", "") or "")
            if not task_id or task_id in seen_task_ids:
                continue
            merged_tasks.append(
                {
                    "task_id": task_id,
                    "task_type": getattr(task, "task_type", None),
                    "aisle_id": str(aisle_id),
                    "status": "PENDING",
                    "created_at": None,
                    "confirmed_at": None,
                    "is_timeout": False,
                    "source": "pending_inbound_queue",
                }
            )
            seen_task_ids.add(task_id)

    for task in outbound_pending or []:
        task_id = str(getattr(task, "task_id", "") or "")
        if not task_id or task_id in seen_task_ids:
            continue
        merged_tasks.append(
            {
                "task_id": task_id,
                "task_type": getattr(task, "task_type", None),
                "aisle_id": str(warehouse_service._get_task_effective_aisle(task)) if warehouse_service._get_task_effective_aisle(task) is not None else None,
                "status": "PENDING",
                "created_at": None,
                "confirmed_at": None,
                "is_timeout": False,
                "source": "pending_outbound_queue",
            }
        )
        seen_task_ids.add(task_id)

    for task in pending.values():
        task_id = str(task.task_id)
        if not task_id or task_id in seen_task_ids:
            continue
        merged_tasks.append(
            {
                "task_id": task.task_id,
                "task_type": task.task_type,
                "aisle_id": task.aisle_id,
                "status": task.status.value,
                "created_at": task.created_at.isoformat(),
                "confirmed_at": task.confirmed_at.isoformat() if task.confirmed_at else None,
                "is_timeout": task.is_timeout(),
                "source": "task_manager",
            }
        )
        seen_task_ids.add(task_id)

    return ok(
        status_code="SUCCESS",
        message="ok",
        data={
            "count": len(merged_tasks),
            "tasks": merged_tasks,
        },
    )


@router.get("/unconfirmed")
async def get_unconfirmed_tasks(
    task_manager: TaskStateManager = Depends(get_task_state_manager),
    warehouse_service: WarehouseService = Depends(get_warehouse_service),
):
    """
    获取未确认的任务（调试接口）

    返回已发送但尚未收到EXECUTING反馈的任务列表。
    """
    grouped = warehouse_service.get_tasks_by_aisle_for_api()
    tasks_by_aisle = {
        str(aisle): {
            "can_executing": [warehouse_service._task_brief_dict(task) for task in payload.get("can_executing", [])],
            "pending": [warehouse_service._task_brief_dict(task) for task in payload.get("pending", [])],
            "running": [warehouse_service._task_brief_dict(task) for task in payload.get("running", [])],
        }
        for aisle, payload in grouped.items()
    }
    can_accept_executing = any(bool(payload.get("can_executing")) for payload in grouped.values())
    return ok(
        status_code="SUCCESS",
        message="ok",
        data={
            "tasksByAisle": tasks_by_aisle,
            "canAcceptExecuting": can_accept_executing,
        },
    )


@router.post("/adjust")
async def task_adjust(
    request: TaskAdjustRequest,
    warehouse_service: WarehouseService = Depends(get_warehouse_service),
):
    """执行 任务 adjust 对应的业务处理。

    Args:
        request: 接口请求对象。
        warehouse_service: 用于本函数处理的 `warehouse_service` 参数。

    Returns:
        处理结果；具体类型由调用上下文决定。
    """
    if request.taskType.value != "INBOUND":
        return fail(
            message="任务调整被拒绝。",
            http_status=400,
            data={"taskId": request.taskId, "reason": "当前仅支持 INBOUND 任务调整。"},
        )
    try:
        parsed_positions = warehouse_service._normalize_feedback_positions(
            [pos.model_dump() for pos in (request.positions or [])],
            warehouse_service._to_int_or_none(request.aisleId),
        ) if request.positions else []
        sku_rows = [sku.model_dump() if hasattr(sku, "model_dump") else sku.dict() for sku in (request.skus or [])] if request.skus is not None else None
        result = warehouse_service.adjust_inbound_task(
            task_id=request.taskId,
            aisle_id=warehouse_service._to_int_or_none(request.aisleId),
            in_line=request.inLine,
            skus=sku_rows,
            positions=parsed_positions,
        )
        if not result.get("success", False):
            return fail(
                message="任务调整失败。",
                http_status=400,
                data={"taskId": request.taskId, "reason": result.get("reason", "未知原因。")},
            )
        return ok(
            status_code="SUCCESS",
            message="任务调整成功。",
            data={
                "taskId": request.taskId,
                "aisleId": result.get("aisleId"),
                "positions": result.get("positions"),
                "reason": None,
            },
        )
    except ValueError as exc:
        return fail(
            message="任务调整参数校验失败。",
            http_status=400,
            data={"taskId": request.taskId, "reason": str(exc)},
        )
    except Exception as exc:
        return fail(
            message="任务调整失败。",
            http_status=500,
            data={"taskId": request.taskId, "detail": str(exc)},
        )
