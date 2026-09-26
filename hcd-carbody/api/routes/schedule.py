"""Mixed scheduling API routes."""

import logging
import traceback
import uuid
from datetime import datetime
from typing import Any, Dict, List
import re

from fastapi import APIRouter, Depends

from ..models import MixedScheduleRequest, PositionInfo, TaskType
from ..response import fail, ok
from ..services.warehouse_service import WarehouseService, get_warehouse_service
from ..state import PendingTaskStatus, TaskStateManager, get_task_state_manager

router = APIRouter(prefix="/schedule", tags=["schedule"])
logger = logging.getLogger("api.business")


# ==========================================================================
# 辅助函数：线路、SKU 属性和禁配校验归一化
# ==========================================================================
def _line_ref_to_token(core: Any, line_ref: Any, *, inbound: bool, aisle: Any = None) -> Any:
    """将线路编号或既有 LxCy 口坐标转换为统一的库口位置标记。

    Args:
        core: WarehouseCore，提供 TimeEstimator 及巷道异构尺寸信息。
        line_ref: 数字线路号、LxCy 标记或空值。
        inbound: True 读取入库口映射，False 读取出库口映射。
        aisle: 可选巷道号，用于异构巷道的库口坐标换算。

    Returns:
        Any: 标准 ``L{层}C{列}`` 标记；无法换算时保留原始非空值。
    """
    if line_ref is None:
        return None
    s = str(line_ref).strip()
    if not s:
        return None
    if re.fullmatch(r"[lL]\d+[cC]\d+", s):
        return s.upper()
    # numeric line id -> map to dock token L{level}C{col}
    try:
        line_num = int(s)
    except Exception:
        return s
    try:
        est = getattr(core, "time_estimator", None)
        if est is None:
            return s
        if inbound:
            col, level = est.resolve_inbound_dock(line_num, default_layer=1, aisle=aisle)
        else:
            col, level = est.resolve_outbound_dock(line_num, default_layer=1, aisle=aisle)
        return f"L{int(level)}C{int(col)}"
    except Exception:
        return s


def _extract_line_meta_from_positions(core: Any, positions: List[Any], *, inbound: bool) -> Any:
    """从已冻结货位提取入库线或出库线元数据。

    Args:
        core: 预留的上下文参数，保持与其他线路解析函数调用方式一致。
        positions: 已匹配位置，优先读取其专用 in_line/out_line 字段。
        inbound: True 提取入库线，False 提取出库线。

    Returns:
        Any: 首个有效线路元数据；找不到时返回 None。
    """
    if not positions:
        return None

    # 优先使用独立的货位级元数据，不把货位信息混入 features 字段。
    for pos in positions:
        if inbound:
            val = getattr(pos, "in_line", None)
            if val is not None and str(val).strip():
                return val
            for attr in ("upper_in_line", "lower_in_line"):
                v = getattr(pos, attr, None)
                if v is not None and str(v).strip():
                    return v
        else:
            val = getattr(pos, "out_line", None)
            if val is not None and str(val).strip():
                return val
            for attr in ("upper_out_line", "lower_out_line"):
                v = getattr(pos, attr, None)
                if v is not None and str(v).strip():
                    return v

    def _normalize_key(k: Any) -> str:
        """将特征键转换为忽略下划线和大小写的比较形式。

        Args:
            k: 原始特征键。

        Returns:
            str: 例如 ``out_line`` 转为 ``outline``。
        """
        return str(k).replace("_", "").strip().lower()

    def _pick_from_dict(d: Any) -> Any:
        """从位置特征字典中兼容读取 inLine/outLine 别名。

        Args:
            d: 单层或上下层货位附带的特征字典。

        Returns:
            Any: 首个有效线路值；字典不含目标字段时返回 None。
        """
        if not isinstance(d, dict):
            return None
        # 第一轮：按常见名称精确查找。
        for key in ("inLine", "in_line") if inbound else ("outLine", "out_line"):
            if key in d and d[key] is not None and str(d[key]).strip():
                return d[key]
        # 第二轮：对键名归一化后再查找。
        for k, v in d.items():
            if v is None or not str(v).strip():
                continue
            if _normalize_key(k) in ("inline" if inbound else "outline",):
                return v
        return None

    for pos in positions:
        for feat in (
            getattr(pos, "features", None),
            getattr(pos, "upper_features", None),
            getattr(pos, "lower_features", None),
        ):
            val = _pick_from_dict(feat)
            if val is not None:
                return val
    return None


def _normalize_sku_features(core: Any, skus: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """将 API SKU 条目的嵌套/顶层特征合并为标准化键值对。

    Args:
        core: WarehouseCore，用于执行键别名归一化。
        skus: 一个任务的 SKU 条目列表。

    Returns:
        List[Dict[str, str]]: 与有效 SKU 对应的标准化属性集合。
    """
    rows: List[Dict[str, str]] = []
    for s in skus or []:
        if not isinstance(s, dict):
            continue
        raw_features = s.get("features")
        feats: Dict[str, Any] = raw_features if isinstance(raw_features, dict) else {}
        merged = dict(feats)
        for k, v in s.items():
            if k in ("skuId", "quantity", "features"):
                continue
            if k not in merged and v is not None:
                merged[k] = v

        norm: Dict[str, str] = {}
        for k, v in merged.items():
            if v is None:
                continue
            try:
                ck = core._canonical_feature_key(k) if hasattr(core, "_canonical_feature_key") else str(k)
            except Exception:
                ck = str(k)
            sv = str(v).strip()
            if ck and sv:
                norm[str(ck)] = sv
        if norm:
            rows.append(norm)
    return rows


def _evaluate_forbidden(core: Any, aisle_id: int, skus: List[Dict[str, Any]], task_id: str) -> Dict[str, Any]:
    """执行 evaluate forbidden 对应的业务处理。

    Args:
        core: 用于本函数处理的 `core` 参数。
        aisle_id: 目标巷道编号。
        skus: 用于本函数处理的 `skus` 参数。
        task_id: 任务唯一标识。

    Returns:
        Dict[str, Any]: 处理后的结果。
    """
    rules = (getattr(core, "aisle_forbidden", {}) or {}).get(int(aisle_id), {})
    if not rules:
        return {
            "taskId": task_id,
            "aisleId": str(aisle_id),
            "checked_count": 0,
            "passed": True,
            "violated": [],
        }

    feat_rows = _normalize_sku_features(core, skus)
    violated: List[Dict[str, Any]] = []
    checked_count = 0
    for feats in feat_rows:
        for fkey, blocked_vals in rules.items():
            checked_count += 1
            blocked = set(str(x).strip() for x in blocked_vals)
            if fkey in feats and str(feats[fkey]).strip() in blocked:
                violated.append(
                    {
                        "feature": fkey,
                        "value": feats[fkey],
                        "blocked_values": sorted([str(x) for x in blocked_vals]),
                    }
                )

    return {
        "taskId": task_id,
        "aisleId": str(aisle_id),
        "checked_count": checked_count,
        "passed": len(violated) == 0,
        "violated": violated,
    }


def _build_aisle_forbidden_checks(
    core: Any,
    aisle_assignments: List[Dict[str, Any]],
    task_skus_map: Dict[str, List[Dict[str, Any]]],
    ) -> Dict[str, Any]:
    """构建巷道 forbidden checks相关逻辑。

    Args:
        core: 用于本函数处理的 `core` 参数。
        aisle_assignments: 用于本函数处理的 `aisle_assignments` 参数。
        task_skus_map: 用于本函数处理的 `task_skus_map` 参数。

    Returns:
        Dict[str, Any]: 处理后的结果。
    """
    checked: List[Dict[str, Any]] = []
    violations: List[Dict[str, Any]] = []
    for row in aisle_assignments:
        task = row.get("assignedTask")
        if not task:
            continue
        if str(task.get("taskType")) != TaskType.INBOUND.value:
            continue
        task_id = str(task.get("taskId"))
        aisle_value = row.get("aisleId")
        if aisle_value is None:
            continue
        aisle_id = int(aisle_value)
        skus = task_skus_map.get(task_id, [])
        item = _evaluate_forbidden(core, aisle_id, skus, task_id)
        checked.append(item)
        if not item.get("passed", True):
            violations.append(item)

    return {
        "rule": "driven by config/warehouse.json aisle_forbidden",
        "checked_count": len(checked),
        "passed": len(violations) == 0,
        "violations": violations,
    }


def _find_duplicate_task_ids(tasks: List[Any]) -> List[str]:
    """查找duplicate 任务 ids相关逻辑。

    Args:
        tasks: 待处理的任务集合。

    Returns:
        List[str]: 处理后的结果。
    """
    seen = set()
    duplicates = set()
    for task in tasks or []:
        task_id = task.taskId if hasattr(task, "taskId") else task.get("taskId")
        if not task_id:
            continue
        task_id = str(task_id)
        if task_id in seen:
            duplicates.add(task_id)
        seen.add(task_id)
    return sorted(duplicates)


def _request_task_value(task: Any, field_name: str) -> Any:
    """兼容 Pydantic 请求对象和字典任务的字段读取。"""
    if isinstance(task, dict):
        return task.get(field_name)
    return getattr(task, field_name, None)


# ==========================================================================
# 主函数：混合调度 API 路由
# ==========================================================================
@router.post("/mixed")
async def mixed_schedule(
    request: MixedScheduleRequest,
    warehouse_service: WarehouseService = Depends(get_warehouse_service),
    task_manager: TaskStateManager = Depends(get_task_state_manager),
):
    """Unified inbound/outbound scheduling endpoint."""
    current_stage = "init"
    service_state = None
    task_manager_state = None
    try:
        duplicate_task_ids = _find_duplicate_task_ids(request.tasks or [])
        if duplicate_task_ids:
            logger.warning("event=mixed_schedule_rejected reason=duplicate_request_task_ids task_ids=%s", duplicate_task_ids)
            return fail(
                message="存在重复的taskId，无法执行调度。",
                http_status=400,
                data={"duplicateTaskIds": duplicate_task_ids},
            )

        active_task_ids = warehouse_service.get_active_task_ids() | set(task_manager.get_all_pending_tasks().keys())
        duplicate_active_ids = sorted(
            {
                str(_request_task_value(task, "taskId"))
                for task in (request.tasks or [])
                if str(_request_task_value(task, "taskId")) in active_task_ids
            }
        )
        if duplicate_active_ids:
            logger.warning("event=mixed_schedule_rejected reason=active_task_ids task_ids=%s", duplicate_active_ids)
            return fail(
                message="存在已下发或执行中的taskId，无法重复提交。",
                http_status=400,
                data={"duplicateTaskIds": duplicate_active_ids},
            )

        service_state = warehouse_service.save_state()
        task_manager_state = task_manager.save_state()

        current_stage = "sync_production_context"
        ignored_plan_ids: List[str] = []
        if request.productionPlan is not None:
            operation_type = getattr(request.productionPlan, "operationType", None)
            op_value = str(getattr(operation_type, "value", operation_type or "")).upper()
            is_update = op_value == "UPDATE"
            reset_assigned = bool(getattr(request.productionPlan, "resetAssigned", False))
            plan_result = warehouse_service.set_production_plan(
                request.productionPlan,
                update=is_update,
                reset_assigned=reset_assigned,
                current_groups=request.currentGroups,
                legacy_current_groups=request.productionLineCurrentGroup,
            )
            ignored_plan_ids = list(plan_result.get("ignoredPlanIds", []) or [])
            if is_update and reset_assigned:
                task_manager.clear_all()
        elif request.currentGroups is not None or request.productionLineCurrentGroup is not None:
            warehouse_service.set_current_groups(
                current_groups=request.currentGroups,
                legacy_current_groups=request.productionLineCurrentGroup,
            )

        current_stage = "sync_aisle_status"
        warehouse_service.sync_aisle_status(request.aisleStatus)

        current_stage = "sync_inventory"
        warehouse_service.sync_inventory(request.inventory)

        current_stage = "convert_schedule_tasks"
        tasks = warehouse_service.convert_schedule_tasks(request.tasks)

        task_skus_map: Dict[str, List[Dict[str, Any]]] = {}
        task_line_map: Dict[str, Dict[str, Any]] = {}
        for t in (request.tasks or []):
            tid = _request_task_value(t, "taskId")
            skus = _request_task_value(t, "skus") or []
            in_line_raw = _request_task_value(t, "inLine")
            out_line_raw = _request_task_value(t, "outLine")
            if tid is not None:
                task_line_map[str(tid)] = {
                    "inLine": in_line_raw,
                    "outLine": out_line_raw,
                }
            sku_dicts = []
            for s in skus:
                if hasattr(s, "model_dump"):
                    sku_dicts.append(s.model_dump())
                elif hasattr(s, "dict"):
                    sku_dicts.append(s.dict())
                elif isinstance(s, dict):
                    sku_dicts.append(dict(s))
            task_skus_map[str(tid)] = sku_dicts

        current_stage = "freeze_unconfirmed"
        unconfirmed_task_ids = sorted(task_manager.get_unconfirmed_tasks().keys())
        frozen_tasks: Dict[int, Any] = {}
        for task_id in unconfirmed_task_ids:
            pending = task_manager.get_task(task_id)
            if pending is None or pending.aisle_id is None:
                continue
            aisle_id = int(pending.aisle_id)
            frozen_task = warehouse_service.get_task_for_aisle(aisle_id, task_id=task_id)
            if frozen_task is not None:
                frozen_tasks[aisle_id] = frozen_task

        current_stage = "execute_schedule"
        schedule_result, unsubmitted_outbound_tasks = warehouse_service.execute_schedule(
            tasks,
            frozen_tasks=frozen_tasks,
        )

        current_stage = "build_response"
        schedule_id = f"SCH-{uuid.uuid4().hex[:8].upper()}"
        timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

        aisle_assignments: List[Dict[str, Any]] = []
        recommended_by_aisle = {
            int(aid): task
            for aid, task in schedule_result.items()
            if task is not None
        }
        prioritized_executable = warehouse_service.get_prioritized_executable_tasks_by_aisle(
            recommended_tasks=recommended_by_aisle
        )
        match_fields = list(warehouse_service.core._get_outbound_match_features(None) or [])

        for aisle_id, assigned_task in schedule_result.items():
            if assigned_task is None:
                matched = [
                    warehouse_service._task_brief_dict(t)
                    for t in prioritized_executable.get(int(aisle_id), [])
                ]
                aisle_assignments.append(
                    {"aisleId": str(aisle_id), "assignedTask": None, "matchedTasks": matched}
                )
                continue

            positions = []
            task_skus = []
            resolved_outbound_entries = []
            for s in (assigned_task.skus or []):
                if isinstance(s, dict):
                    task_skus.append(dict(s))
                elif hasattr(s, "model_dump"):
                    task_skus.append(s.model_dump())
                elif hasattr(s, "dict"):
                    task_skus.append(s.dict())
                else:
                    task_skus.append({"skuId": getattr(s, "skuId", ""), "quantity": getattr(s, "quantity", 1)})
            if assigned_task.task_type == "OUTBOUND":
                try:
                    resolved_outbound_entries = warehouse_service._resolve_outbound_inventory_removal_entries(assigned_task)
                except Exception:
                    resolved_outbound_entries = []

            if assigned_task.positions:
                for idx, pos in enumerate(assigned_task.positions):
                    sku_id = ""
                    quantity = 0
                    sku_attrs: Dict[str, Any] = {}
                    resolved_entry = resolved_outbound_entries[idx] if idx < len(resolved_outbound_entries) else None
                    if idx < len(task_skus):
                        sku_dict = task_skus[idx] or {}
                        sku_id = sku_dict.get("skuId", "") or ""
                        quantity = sku_dict.get("quantity", 1) or 0
                        for field in match_fields:
                            if field in sku_dict:
                                sku_attrs[field] = sku_dict.get(field)

                    if not sku_id:
                        sku_id = (
                            getattr(pos, "upper_sku", None)
                            or getattr(pos, "lower_sku", None)
                            or getattr(pos, "sku", "")
                            or ""
                        )
                    if not quantity:
                        quantity = (
                            getattr(pos, "upper_quantity", 0)
                            or getattr(pos, "lower_quantity", 0)
                            or getattr(pos, "quantity", 0)
                        )
                    if resolved_entry is not None:
                        resolved_sku = str(resolved_entry.get("skuId") or "").strip()
                        if resolved_sku:
                            sku_id = resolved_sku
                        resolved_features = resolved_entry.get("features")
                        if isinstance(resolved_features, dict) and resolved_features:
                            sku_attrs = dict(resolved_features)

                    if match_fields and not sku_attrs:
                        if hasattr(pos, "is_double_layer") and pos.is_double_layer:
                            sku_attrs = getattr(pos, "upper_features", {}) or {}
                            if not sku_attrs:
                                sku_attrs = getattr(pos, "lower_features", {}) or {}
                        if not sku_attrs:
                            sku_attrs = getattr(pos, "features", {}) or {}

                    external_row = 2 * (pos.aisle - 1) + pos.row
                    payload = {
                        "row": external_row,
                        "column": pos.column,
                        "level": pos.level,
                        "skuId": sku_id,
                        "quantity": quantity,
                    }
                    for field in match_fields:
                        if field in sku_attrs:
                            payload[field] = sku_attrs.get(field)
                    positions.append(PositionInfo(**payload).model_dump())

            task_type = TaskType.OUTBOUND.value if assigned_task.task_type == "OUTBOUND" else TaskType.INBOUND.value
            plan_id = getattr(assigned_task, "plan_id", None)
            raw_group_idx = getattr(assigned_task, "group_idx", None)
            plan_index = (int(raw_group_idx) + 1) if raw_group_idx is not None else None
            if (plan_id is None or plan_index is None) and isinstance(getattr(assigned_task, "task_id", None), str):
                m = re.match(r"^OUTBOUND_PL(\d+)_GP(\d+)_", assigned_task.task_id)
                if m:
                    pl_num = int(m.group(1))
                    gp_num = int(m.group(2))
                    if plan_index is None:
                        plan_index = gp_num
                    if plan_id is None:
                        plan_id = f"LINE-{pl_num}"

            assigned_response = {
                "taskId": assigned_task.task_id,
                "taskType": task_type,
                "planId": plan_id,
                "planIndex": plan_index,
                "inLine": _line_ref_to_token(
                    warehouse_service.core,
                    getattr(assigned_task, "in_line", None),
                    inbound=True,
                    aisle=getattr(assigned_task, "assigned_aisle", None),
                ),
                "outLine": _line_ref_to_token(
                    warehouse_service.core,
                    getattr(assigned_task, "out_line", None),
                    inbound=False,
                    aisle=getattr(assigned_task, "assigned_aisle", None),
                ),
                "positions": positions if positions else None,
            }

            # 同一任务存在原始请求产线标识时，优先沿用该标识返回。
            req_lines = task_line_map.get(str(assigned_task.task_id), {})
            if req_lines.get("inLine") is not None:
                assigned_response["inLine"] = req_lines.get("inLine")
            if req_lines.get("outLine") is not None:
                assigned_response["outLine"] = req_lines.get("outLine")
            # 对于不是由本次请求生成的任务，尝试从库存实例元数据中获取产线信息。
            if req_lines.get("inLine") is None:
                meta_in = _extract_line_meta_from_positions(
                    warehouse_service.core,
                    getattr(assigned_task, "positions", []) or [],
                    inbound=True,
                )
                if meta_in is not None:
                    assigned_response["inLine"] = meta_in
            if req_lines.get("outLine") is None:
                meta_out = _extract_line_meta_from_positions(
                    warehouse_service.core,
                    getattr(assigned_task, "positions", []) or [],
                    inbound=False,
                )
                if meta_out is not None:
                    assigned_response["outLine"] = meta_out

            matched = [
                warehouse_service._task_brief_dict(t)
                for t in prioritized_executable.get(int(aisle_id), [])
            ]
            aisle_assignments.append(
                {"aisleId": str(aisle_id), "assignedTask": assigned_response, "matchedTasks": matched}
            )
            existing_pending = task_manager.get_task(str(assigned_task.task_id))
            if existing_pending is None:
                task_manager.add_pending_task(
                    task_id=assigned_task.task_id,
                    task_type=assigned_task.task_type,
                    aisle_id=str(aisle_id),
                )

        forbidden_check = _build_aisle_forbidden_checks(warehouse_service.core, aisle_assignments, task_skus_map)
        if not forbidden_check.get("passed", True):
            warehouse_service.restore_state(service_state)
            task_manager.restore_state(task_manager_state)
            logger.warning(
                "event=mixed_schedule_rejected schedule_id=%s reason=aisle_forbidden violations=%s",
                schedule_id,
                forbidden_check.get("violations", []),
            )
            return fail(
                message="命中禁配规则，调度失败。",
                http_status=400,
                data={
                    "reason": "本次请求命中 aisle_forbidden 禁配规则，调度结果未生效。",
                    "checks": {
                        "aisle_forbidden": forbidden_check,
                    },
                    "timestamp": timestamp,
                },
            )

        assigned_summary = [
            {
                "aisleId": assignment["aisleId"],
                "taskId": assignment["assignedTask"]["taskId"],
                "taskType": assignment["assignedTask"]["taskType"],
                "positions": assignment["assignedTask"].get("positions") or [],
            }
            for assignment in aisle_assignments
            if assignment.get("assignedTask") is not None
        ]
        logger.info(
            "event=mixed_schedule_success schedule_id=%s request_task_ids=%s assigned=%s unsubmitted_outbound=%s unconfirmed=%s",
            schedule_id,
            [str(_request_task_value(task, "taskId")) for task in (request.tasks or [])],
            assigned_summary,
            [str(task.get("taskId", "")) for task in (unsubmitted_outbound_tasks or []) if isinstance(task, dict)],
            unconfirmed_task_ids,
        )
        return ok(
            status_code="SUCCESS",
            message="调度成功。",
            data={
                "scheduleId": schedule_id,
                "timestamp": timestamp,
                "ignoredPlanIds": ignored_plan_ids,
                "unconfirmedTaskIds": unconfirmed_task_ids,
                "aisleAssignments": aisle_assignments,
                "unsubmittedOutboundTasks": unsubmitted_outbound_tasks,
                "executableTasksByAisle": {
                    str(aisle): [warehouse_service._task_brief_dict(t) for t in tasks]
                    for aisle, tasks in prioritized_executable.items()
                },
                "checks": {
                    "aisle_forbidden": forbidden_check,
                },
            },
        )

    except ValueError as e:
        if service_state is not None and task_manager_state is not None:
            warehouse_service.restore_state(service_state)
            task_manager.restore_state(task_manager_state)
        logger.warning(
            "event=mixed_schedule_validation_error stage=%s task_ids=%s reason=%s",
            current_stage,
            [str(_request_task_value(task, "taskId")) for task in (request.tasks or [])],
            str(e),
        )
        return fail(
            message="请求参数校验失败。",
            http_status=400,
            data={
                "stage": current_stage,
                "detail": str(e),
                "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )
    except Exception as e:
        if service_state is not None and task_manager_state is not None:
            warehouse_service.restore_state(service_state)
            task_manager.restore_state(task_manager_state)
        tb_lines = traceback.format_exc().splitlines()
        logger.exception(
            "event=mixed_schedule_error stage=%s task_ids=%s error=%s",
            current_stage,
            [str(_request_task_value(task, "taskId")) for task in (request.tasks or [])],
            str(e),
        )
        return fail(
            message="调度执行失败。",
            http_status=500,
            data={
                "stage": current_stage,
                "exception_type": type(e).__name__,
                "exception_message": str(e),
                "request_summary": {
                    "aisle_status_count": len(request.aisleStatus or []),
                    "inventory_count": len(request.inventory or []),
                    "task_count": len(request.tasks or []),
                    "task_ids": [
                        _request_task_value(t, "taskId")
                        for t in (request.tasks or [])
                    ],
                },
                "traceback_tail": tb_lines[-12:],
                "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )
