"""
任务执行反馈接口路由
"""

from fastapi import APIRouter, HTTPException, Depends

from ..models import (
    TaskFeedbackRequest,
    ApiResponse,
    TaskStatus,
)
from ..state import get_task_state_manager, TaskStateManager, PendingTaskStatus
from ..services.warehouse_service import get_warehouse_service, WarehouseService

router = APIRouter(prefix="/task", tags=["任务反馈"])


@router.post(
    "/feedback",
    response_model=ApiResponse,
    responses={
        200: {
            "description": "反馈处理成功",
            "content": {"application/json": {"example": {
                "status": "SUCCESS",
                "message": "反馈处理成功",
                "data": None
            }}}
        },
        422: {
            "description": "请求参数验证失败",
            "content": {"application/json": {"example": {
                "status": "FAILED",
                "message": "请求参数验证失败",
                "data": {"errors": ["body -> taskId: field required"]}
            }}}
        },
        500: {
            "description": "服务器内部错误",
            "content": {"application/json": {"example": {
                "status": "FAILED",
                "message": "内部服务器错误: ...",
                "data": None
            }}}
        },
    },
)
async def task_feedback(
    request: TaskFeedbackRequest,
    warehouse_service: WarehouseService = Depends(get_warehouse_service),
    task_manager: TaskStateManager = Depends(get_task_state_manager)
) -> ApiResponse:
    """
    任务执行反馈接口
    
    外部系统报告任务执行状态。
    
    状态说明：
    - EXECUTING: 任务正在执行中（收到此状态表示指令已成功传输）
    - COMPLETED: 任务已完成
    - FAILED: 任务执行失败
    """
    task_id = request.taskId
    status = request.status
    
    # 获取任务信息（可能在pending中，也可能不在）
    pending_task = task_manager.get_task(task_id)
    
    try:
        # 根据状态处理
        if status == TaskStatus.EXECUTING:
            # 确认任务开始执行 - 这是关键的确认点
            if pending_task:
                task_manager.confirm_task(task_id)
                print(f"[API] 任务 {task_id} 已确认开始执行")
            
            # 通知warehouse_core任务正在执行
            feedback_data = {
                "taskId": task_id,
                "taskType": request.taskType.value,
                "status": "EXECUTING",
                "startTime": request.startTime,
            }
            warehouse_service.apply_feedback(feedback_data)
            
        elif status == TaskStatus.COMPLETED:
            # 标记任务完成
            if pending_task:
                task_manager.complete_task(task_id)
                print(f"[API] 任务 {task_id} 已完成")
            
            # 通知warehouse_core任务已完成
            feedback_data = {
                "taskId": task_id,
                "taskType": request.taskType.value,
                "status": "COMPLETED",
                "startTime": request.startTime,
            }
            warehouse_service.apply_feedback(feedback_data)
            
        elif status == TaskStatus.FAILED:
            # 标记任务失败
            if pending_task:
                task_manager.fail_task(task_id)
                print(f"[API] 任务 {task_id} 执行失败: {request.failureReason}")
            
            # 通知warehouse_core任务失败
            feedback_data = {
                "taskId": task_id,
                "taskType": request.taskType.value,
                "status": "FAILED",
                "startTime": request.startTime,
                "reason": request.failureReason,
            }
            warehouse_service.apply_feedback(feedback_data)
        
        return ApiResponse(status="SUCCESS", message="反馈处理成功", data=None)
        
    except Exception as e:
        print(f"[API] 处理任务反馈失败: {e}")
        return ApiResponse(status="FAILED", message=f"反馈处理失败: {str(e)}", data=None)


@router.get("/pending")
async def get_pending_tasks(
    task_manager: TaskStateManager = Depends(get_task_state_manager)
):
    """
    获取所有待处理任务（调试接口）
    
    返回当前所有待反馈确认的任务列表。
    """
    pending = task_manager.get_all_pending_tasks()
    return {
        "status": "SUCCESS",
        "message": "获取待处理任务成功",
        "data": {
            "count": len(pending),
            "tasks": [
                {
                    "task_id": task.task_id,
                    "task_type": task.task_type,
                    "aisle_id": task.aisle_id,
                    "status": task.status.value,
                    "created_at": task.created_at.isoformat(),
                    "confirmed_at": task.confirmed_at.isoformat() if task.confirmed_at else None,
                    "is_timeout": task.is_timeout()
                }
                for task in pending.values()
            ]
        }
    }


@router.get("/unconfirmed")
async def get_unconfirmed_tasks(
    task_manager: TaskStateManager = Depends(get_task_state_manager)
):
    """
    获取未确认的任务（调试接口）
    
    返回已发送但尚未收到EXECUTING反馈的任务列表。
    """
    unconfirmed = task_manager.get_unconfirmed_tasks()
    return {
        "status": "SUCCESS",
        "message": "获取未确认任务成功",
        "data": {
            "count": len(unconfirmed),
            "tasks": list(unconfirmed.keys()),
            "can_accept_new_task": not bool(unconfirmed)
        }
    }

