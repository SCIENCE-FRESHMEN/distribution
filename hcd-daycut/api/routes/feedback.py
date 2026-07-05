"""
任务执行反馈接口路由
"""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from ..models import ApiResponse, TaskFeedbackRequest, TaskAdjustRequest, TaskStatus
from ..services.warehouse_service import WarehouseService, get_warehouse_service
from ..state import TaskStateManager, get_task_state_manager

router = APIRouter(prefix="/task", tags=["任务反馈"])



def _failed_adjust_response(task_id: str, reason: str) -> JSONResponse:
    status_code = 400
    if "位置冲突" in reason:
        status_code = 409
    payload = ApiResponse(
        status="FAILED",
        message=f"任务调整失败: {task_id}",
        data={"taskId": task_id, "reason": reason},
    )
    return JSONResponse(status_code=status_code, content=payload.model_dump())

def _build_feedback_position_payload(task_obj):
    frozen_positions = getattr(task_obj, "_api_positions", None)
    if frozen_positions:
        return [dict(position) for position in frozen_positions]

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
            task_skus.append(
                {
                    "skuId": getattr(s, "skuId", "") or "",
                    "quantity": getattr(s, "quantity", 1) or 1,
                }
            )

    is_outbound_task = getattr(task_obj, "task_type", "") == "OUTBOUND"

    def append_position_payload(pos, shelf, sku_id, quantity):
        positions.append(
            {
                "row": 2 * (pos.aisle - 1) + pos.row,
                "column": pos.column,
                "level": pos.level,
                "shelf": shelf,
                "skuId": sku_id,
                "quantity": quantity,
            }
        )

    for idx, pos in enumerate(getattr(task_obj, "positions", None) or []):
        task_sku_dict = task_skus[idx] if idx < len(task_skus) else {}

        if getattr(pos, "is_double_layer", False):
            if is_outbound_task:
                if getattr(pos, "upper_quantity", 0) > 0 and getattr(pos, "upper_sku", None):
                    append_position_payload(
                        pos,
                        "UPPER",
                        getattr(pos, "upper_sku", "") or "",
                        getattr(pos, "upper_quantity", 0) or 0,
                    )
                if getattr(pos, "lower_quantity", 0) > 0 and getattr(pos, "lower_sku", None):
                    append_position_payload(
                        pos,
                        "LOWER",
                        getattr(pos, "lower_sku", "") or "",
                        getattr(pos, "lower_quantity", 0) or 0,
                    )
            else:
                has_upper_space = getattr(pos, "upper_quantity", 0) == 0
                has_lower_space = getattr(pos, "lower_quantity", 0) == 0
                if len(task_skus) >= 2 and len(getattr(task_obj, "positions", None) or []) == 1 and has_upper_space and has_lower_space:
                    first_sku = task_skus[0]
                    second_sku = task_skus[1]
                    append_position_payload(
                        pos,
                        "UPPER",
                        first_sku.get("skuId", "") or "",
                        first_sku.get("quantity", 0) or 1,
                    )
                    append_position_payload(
                        pos,
                        "LOWER",
                        second_sku.get("skuId", "") or "",
                        second_sku.get("quantity", 0) or 1,
                    )
                else:
                    sku_id = task_sku_dict.get("skuId", "") or ""
                    if sku_id:
                        quantity = task_sku_dict.get("quantity", 0) or 1
                        if has_upper_space and has_lower_space:
                            append_position_payload(pos, "UPPER", sku_id, quantity)
                        elif has_upper_space:
                            append_position_payload(pos, "UPPER", sku_id, quantity)
                        elif has_lower_space:
                            append_position_payload(pos, "LOWER", sku_id, quantity)
                    else:
                        append_position_payload(pos, None, "", 0)
        else:
            if is_outbound_task:
                sku_id = getattr(pos, "sku", "") or ""
                quantity = getattr(pos, "quantity", 0) or 0
            else:
                sku_id = task_sku_dict.get("skuId", "") or ""
                if sku_id:
                    quantity = task_sku_dict.get("quantity", 0) or 1
            append_position_payload(pos, None, sku_id, quantity)
    return positions


def _build_success_data(warehouse_service: WarehouseService, task_id: str):
    data = {"taskId": task_id}
    running_task = warehouse_service.core.running_tasks.get(task_id)
    if running_task is not None:
        assigned_aisle = getattr(running_task, "assigned_aisle", None)
        if assigned_aisle is not None:
            data["aisleId"] = str(assigned_aisle)
        data["positions"] = _build_feedback_position_payload(running_task)
    return data


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
    task_manager: TaskStateManager = Depends(get_task_state_manager),
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
            # 通知warehouse_core任务正在执行
            feedback_data = {
                "taskId": task_id,
                "taskType": request.taskType.value,
                "status": "EXECUTING",
                "startTime": request.startTime,
            }
            if request.aisleId is not None:
                feedback_data["aisleId"] = request.aisleId
            if request.positions is not None:
                feedback_data["positions"] = [position.model_dump() for position in request.positions]
            if not warehouse_service.apply_feedback(feedback_data):
                reason = warehouse_service.get_last_feedback_error() or "任务反馈处理失败"
                return ApiResponse(
                    status="FAILED",
                    message=f"反馈处理失败: {task_id}",
                    data={"taskId": task_id, "reason": reason},
                )
            # 确认任务开始执行 - warehouse侧成功后再清理未确认状态
            if pending_task:
                task_manager.confirm_task(task_id)
                print(f"[API] 任务 {task_id} 已确认开始执行")
            notice = warehouse_service.get_last_feedback_notice()
            if notice:
                return ApiResponse(
                    status="SUCCESS",
                    message=notice,
                    data=_build_success_data(warehouse_service, task_id),
                )
            return ApiResponse(
                status="SUCCESS",
                message="反馈处理成功",
                data=_build_success_data(warehouse_service, task_id),
            )

        if status == TaskStatus.COMPLETED:
            # 通知warehouse_core任务已完成
            feedback_data = {
                "taskId": task_id,
                "taskType": request.taskType.value,
                "status": "COMPLETED",
                "startTime": request.startTime,
            }
            if not warehouse_service.apply_feedback(feedback_data):
                reason = warehouse_service.get_last_feedback_error()
                return ApiResponse(
                    status="FAILED",
                    message=f"反馈处理失败: {task_id}",
                    data={"taskId": task_id, "reason": reason} if reason else {"taskId": task_id},
                )
            # 标记任务完成
            if pending_task:
                task_manager.complete_task(task_id)
                print(f"[API] 任务 {task_id} 已完成")
            
        elif status == TaskStatus.FAILED:
            # 通知warehouse_core任务失败
            feedback_data = {
                "taskId": task_id,
                "taskType": request.taskType.value,
                "status": "FAILED",
                "startTime": request.startTime,
                "reason": request.failureReason,
            }
            if not warehouse_service.apply_feedback(feedback_data):
                reason = warehouse_service.get_last_feedback_error()
                return ApiResponse(
                    status="FAILED",
                    message=f"反馈处理失败: {task_id}",
                    data={"taskId": task_id, "reason": reason} if reason else {"taskId": task_id},
                )
            # 标记任务失败
            if pending_task:
                task_manager.fail_task(task_id)
                print(f"[API] 任务 {task_id} 执行失败: {request.failureReason}")
        
        return ApiResponse(status="SUCCESS", message="反馈处理成功", data=None)

    except Exception as exc:
        print(f"[API] 处理任务反馈失败: {exc}")
        return ApiResponse(status="FAILED", message=f"反馈处理失败: {str(exc)}", data=None)


@router.get("/pending")
async def get_pending_tasks(
    task_manager: TaskStateManager = Depends(get_task_state_manager),
    warehouse_service: WarehouseService = Depends(get_warehouse_service),
):
    """
    获取当前任务状态（调试接口）。

    除 TaskStateManager 中的待反馈任务外，也返回 warehouse_service 中真实存在的
    入库/出库 pending 队列及 running 任务，避免遗漏出库任务。
    """
    pending = task_manager.get_all_pending_tasks()
    merged_tasks = {}

    for task in pending.values():
        merged_tasks[task.task_id] = {
            "task_id": task.task_id,
            "task_type": task.task_type,
            "aisle_id": task.aisle_id,
            "status": task.status.value,
            "created_at": task.created_at.isoformat(),
            "confirmed_at": task.confirmed_at.isoformat() if task.confirmed_at else None,
            "is_timeout": task.is_timeout(),
            "source": "task_manager",
        }

    warehouse_pending = warehouse_service.get_pending_tasks()
    running_tasks = warehouse_service.get_running_tasks()

    for aisle, tasks in (warehouse_pending.get("inbound") or {}).items():
        for task in tasks or []:
            task_id = getattr(task, "task_id", "")
            merged_tasks[task_id] = {
                "task_id": task_id,
                "task_type": getattr(task, "task_type", ""),
                "aisle_id": str(aisle),
                "status": "PENDING_EXECUTION",
                "created_at": None,
                "confirmed_at": None,
                "is_timeout": False,
                "source": "pending_inbound_queue",
            }

    for task in (warehouse_pending.get("outbound") or []):
        task_id = getattr(task, "task_id", "")
        positions = getattr(task, "positions", None) or []
        aisle = getattr(positions[0], "aisle", None) if positions else None
        merged_tasks[task_id] = {
            "task_id": task_id,
            "task_type": getattr(task, "task_type", ""),
            "aisle_id": str(aisle) if aisle is not None else None,
            "status": "PENDING_EXECUTION",
            "created_at": None,
            "confirmed_at": None,
            "is_timeout": False,
            "source": "pending_outbound_queue",
        }

    for task in running_tasks.values():
        task_id = getattr(task, "task_id", "")
        aisle = getattr(task, "assigned_aisle", None)
        merged_tasks[task_id] = {
            "task_id": task_id,
            "task_type": getattr(task, "task_type", ""),
            "aisle_id": str(aisle) if aisle is not None else None,
            "status": "RUNNING",
            "created_at": None,
            "confirmed_at": None,
            "is_timeout": False,
            "source": "running_tasks",
        }

    return {
        "status": "SUCCESS",
        "message": "获取待处理任务成功",
        "data": {
            "count": len(merged_tasks),
            "tasks": list(merged_tasks.values()),
        },
    }


@router.get("/unconfirmed")
async def get_unconfirmed_tasks(
    task_manager: TaskStateManager = Depends(get_task_state_manager),
    warehouse_service: WarehouseService = Depends(get_warehouse_service),
):
    """
    获取可 EXECUTING 的任务列表（兼容旧路径）。
    若某巷道存在当前推荐任务，则该任务在该巷道列表中排第一。
    """
    grouped = warehouse_service.get_tasks_by_aisle_for_api()
    by_aisle = {}

    def _build_task_data(task, aisle: int):
        return {
            "taskId": getattr(task, "task_id", ""),
            "taskType": getattr(task, "task_type", ""),
            "aisleId": str(aisle),
            "positions": _build_feedback_position_payload(task) or None,
        }

    total_can_executing = 0
    for aisle, payload in grouped.items():
        can_executing = [_build_task_data(task, int(aisle)) for task in (payload.get("can_executing") or [])]
        pending = [_build_task_data(task, int(aisle)) for task in (payload.get("pending") or [])]
        running = [_build_task_data(task, int(aisle)) for task in (payload.get("running") or [])]
        total_can_executing += len(can_executing)
        by_aisle[str(aisle)] = {
            "can_executing": can_executing,
            "pending": pending,
            "running": running,
        }

    return {
        "status": "SUCCESS",
        "message": "获取可执行任务成功",
        "data": {
            "tasksByAisle": by_aisle,
            "canAcceptExecuting": bool(total_can_executing),
        },
    }


@router.post("/adjust", response_model=ApiResponse)
async def adjust_task(
    request: TaskAdjustRequest,
    warehouse_service: WarehouseService = Depends(get_warehouse_service),
) -> ApiResponse:
    """独立任务调整接口：调整入库任务的巷道/SKU/位置。"""
    if request.taskType.value != "INBOUND":
        return ApiResponse(
            status="FAILED",
            message=f"任务调整失败: {request.taskId}",
            data={"taskId": request.taskId, "reason": "当前仅支持INBOUND任务调整"},
        )

    skus_payload = None
    if request.skus is not None:
        skus_payload = [s.model_dump() if hasattr(s, "model_dump") else s.dict() for s in request.skus]
    positions_payload = None
    if request.positions is not None:
        positions_payload = [p.model_dump() if hasattr(p, "model_dump") else p.dict() for p in request.positions]

    ok, reason, task = warehouse_service.adjust_inbound_task(
        task_id=request.taskId,
        target_aisle=int(request.aisleId) if request.aisleId is not None else None,
        skus=skus_payload,
        positions_data=positions_payload,
    )
    if not ok or task is None:
        return _failed_adjust_response(request.taskId, reason or "调整失败")

    return ApiResponse(
        status="SUCCESS",
        message="任务调整成功",
        data={
            "taskId": request.taskId,
            "aisleId": str(getattr(task, "assigned_aisle", "")),
            "positions": _build_feedback_position_payload(task),
        },
    )
