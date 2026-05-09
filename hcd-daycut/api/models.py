"""
Pydantic 数据模型定义
基于 API接口字段说明文档 定义所有请求/响应模型
"""

from datetime import datetime
from typing import List, Optional, Dict, Any
import json
from pathlib import Path
from enum import Enum
from pydantic import BaseModel, Field

try:
    from pydantic import model_validator
except ImportError:
    model_validator = None
    from pydantic import root_validator
else:
    root_validator = None

if not hasattr(BaseModel, "model_dump"):
    BaseModel.model_dump = BaseModel.dict

from config_loader import load_jsonc


_MATCH_FIELDS: Optional[List[str]] = None


def _load_match_fields() -> List[str]:
    global _MATCH_FIELDS
    if _MATCH_FIELDS is not None:
        return _MATCH_FIELDS
    config_path = Path(__file__).resolve().parents[1] / "config" / "warehouse.json"
    try:
        cfg = load_jsonc(config_path)
        _MATCH_FIELDS = list(cfg.get("match_fields", []) or [])
    except Exception:
        _MATCH_FIELDS = []
    return _MATCH_FIELDS


class SkuAttrsMixin(BaseModel):
    """SKU附加属性校验混入"""

    if model_validator is not None:
        model_config = {
            "extra": "allow",
        }
    else:
        class Config:
            extra = "allow"

    def _check_match_fields(self):
        match_fields = _load_match_fields()
        if not match_fields:
            return self
        sku_id = getattr(self, "skuId", None)
        quantity = getattr(self, "quantity", None)
        if not sku_id or (quantity is not None and quantity <= 0):
            return self
        missing = [field for field in match_fields if getattr(self, field, None) is None]
        if missing:
            raise ValueError(f"缺少SKU附加属性: {', '.join(missing)}")
        return self

    if model_validator is not None:
        @model_validator(mode="after")
        def _validate_match_fields(self):
            return self._check_match_fields()
    else:
        @root_validator(skip_on_failure=True)
        def _validate_match_fields(cls, values):
            instance = cls.construct(**values)
            instance._check_match_fields()
            return values


# ============================================================
# 枚举类型定义
# ============================================================

class TaskType(str, Enum):
    """任务类型枚举"""
    INBOUND = "INBOUND"           # 入库货位分配
    INBOUND_AISLE = "INBOUND_AISLE"  # 入库巷道选择
    OUTBOUND = "OUTBOUND"         # 出库


class TaskStatus(str, Enum):
    """任务状态枚举"""
    PENDING = "PENDING"           # 待执行
    EXECUTING = "EXECUTING"       # 执行中
    COMPLETED = "COMPLETED"       # 已完成
    FAILED = "FAILED"             # 失败


class ScheduleStatus(str, Enum):
    """调度状态枚举"""
    SUCCESS = "SUCCESS"           # 成功
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"  # 部分成功
    FAILED = "FAILED"             # 失败


class OperationType(str, Enum):
    """操作类型枚举"""
    ADD = "ADD"                   # 新增
    UPDATE = "UPDATE"             # 更新


class BankType(str, Enum):
    """库区类型枚举"""
    LEFT = "LEFT"                 # 左库
    RIGHT = "RIGHT"               # 右库


class ShelfPosition(str, Enum):
    """货架位置枚举"""
    UPPER = "UPPER"               # 上层
    LOWER = "LOWER"               # 下层


class BeamSide(str, Enum):
    """单梁所在侧"""
    LEFT = "LEFT"
    RIGHT = "RIGHT"


class UnavailableReason(str, Enum):
    """巷道不可用原因"""
    MAINTENANCE = "MAINTENANCE"   # 维护
    ERROR = "ERROR"               # 故障
    OCCUPIED = "OCCUPIED"         # 被占用


# ============================================================
# 基础数据模型
# ============================================================

class SkuInfo(SkuAttrsMixin):
    """SKU信息"""
    skuId: str = Field(..., description="货物ID")
    quantity: int = Field(..., ge=0, description="货物数量（0表示空货位，1表示有货）")
    beamSide: Optional[BeamSide] = Field(None, description="单梁左右位置，仅单梁入库时使用")

    model_config = {
        "extra": "allow",
        "json_schema_extra": {
            "example": {
                "skuId": "2801021-H19H0",
                "quantity": 1,
                "version": "00"
            }
        }
    }


class PositionInfo(SkuAttrsMixin):
    """货位坐标信息"""
    row: int = Field(..., ge=1, description="行数")
    column: int = Field(..., ge=1, description="列数")
    level: int = Field(..., ge=1, description="层数")
    shelf: Optional[ShelfPosition] = Field(None, description="货架位置")
    skuId: Optional[str] = Field(None, description="货物ID")
    quantity: Optional[int] = Field(None, ge=0, description="货物数量")

    model_config = {
        "extra": "allow",
        "json_schema_extra": {
            "example": {
                "row": 1,
                "column": 5,
                "level": 2,
                "shelf": "UPPER",
                "skuId": "2801021-H19H0",
                "quantity": 1
            }
        }
    }


class ExitCongestion(BaseModel):
    """出口拥堵状态"""
    lineId: str = Field(..., description="产线ID")
    isCongested: bool = Field(..., description="是否拥堵")

    model_config = {
        "json_schema_extra": {
            "example": {
                "lineId": "1",
                "isCongested": False
            }
        }
    }


# ============================================================
# 混合调度接口模型
# ============================================================

class ScheduleTaskRequest(BaseModel):
    """混合调度请求中的任务信息"""
    taskId: str = Field(..., description="任务ID")
    taskType: TaskType = Field(..., description="任务类型")
    targetAisle: Optional[str] = Field(None, description="目标巷道(入库必填)")
    planId: Optional[str] = Field(None, description="计划ID(出库必填)")
    planIndex: Optional[int] = Field(None, ge=1, description="计划下第几组(出库必填)")
    skus: List[SkuInfo] = Field(..., description="货物列表(最多2个)")
    inboundUrgent: Optional[bool] = Field(False, description="是否紧急入库")

    model_config = {
        "json_schema_extra": {
            "example": {
                "taskId": "OUTBOUND_PL1_GP1_2801021-H19H0_2801037-H19H0_20260121101500_001",
                "taskType": "OUTBOUND",
                "planId": "PLAN-LINE1",
                "planIndex": 1,
                "skus": [
                    {"skuId": "2801021-H19H0", "quantity": 1},
                    {"skuId": "2801037-H19H0", "quantity": 1}
                ]
            }
        }
    }

    def _check_single_beam_requirements(self):
        if self.taskType == TaskType.INBOUND and len(self.skus) == 1 and self.skus[0].quantity > 0:
            if self.skus[0].beamSide is None:
                raise ValueError("single inbound SKU requires beamSide")
        return self

    if model_validator is not None:
        @model_validator(mode="after")
        def _validate_single_beam_requirements(self):
            return self._check_single_beam_requirements()
    else:
        @root_validator(skip_on_failure=True)
        def _validate_single_beam_requirements(cls, values):
            task_type = values.get("taskType")
            skus = values.get("skus") or []
            if task_type == TaskType.INBOUND and len(skus) == 1 and getattr(skus[0], "quantity", 0) > 0:
                if getattr(skus[0], "beamSide", None) is None:
                    raise ValueError("single inbound SKU requires beamSide")
            return values

class AisleStatusRequest(BaseModel):
    """巷道状态信息"""
    aisleId: str = Field(..., description="巷道ID")
    isAvailable: bool = Field(..., description="是否可用")
    unavailableReason: Optional[str] = Field(None, description="不可用原因")
    exitCongestion: List[ExitCongestion] = Field(..., description="各产线拥堵状态")
    bank: BankType = Field(..., description="所属库区")

    model_config = {
        "json_schema_extra": {
            "example": {
                "aisleId": "1",
                "isAvailable": True,
                "unavailableReason": None,
                "exitCongestion": [
                    {"lineId": "1", "isCongested": False},
                    {"lineId": "2", "isCongested": False},
                    {"lineId": "3", "isCongested": False}
                ],
                "bank": "LEFT"
            }
        }
    }


class InventoryPositionRequest(BaseModel):
    """库存货位信息"""
    aisleId: str = Field(..., description="巷道ID")
    row: int = Field(..., ge=1, description="行数")
    column: int = Field(..., ge=1, description="列数")
    level: int = Field(..., ge=1, description="层数")
    shelf: Optional[ShelfPosition] = Field(None, description="货架位置")
    positions: Optional[List[SkuInfo]] = Field(None, description="货物信息")

    model_config = {
        "json_schema_extra": {
            "example": {
                "aisleId": "1",
                "row": 1,
                "column": 1,
                "level": 1,
                "shelf": "LOWER",
                "positions": [
                    {"skuId": "2801021-H19H0", "quantity": 1}
                ]
            }
        }
    }


class MixedScheduleRequest(BaseModel):
    """混合调度请求"""
    tasks: List[ScheduleTaskRequest] = Field(..., description="任务列表")
    aisleStatus: List[AisleStatusRequest] = Field(..., description="巷道状态列表")
    inventory: List[InventoryPositionRequest] = Field(..., description="库存信息")
    productionPlan: Optional["ProductionPlanRequest"] = Field(
        None,
        description="本次调度使用的内联生产计划；结构沿用原 ProductionPlanRequest",
    )
    productionLineCurrentGroup: Optional[Dict[str, int]] = Field(
        None,
        description="各产线 core 0-based 当前可执行组索引，例如 {'LINE-1': 0} 表示第1组可执行/已完成0组",
    )
    currentGroups: Optional[Any] = Field(
        None,
        description="各产线 public 1-based 当前可执行组号；支持 {'LINE-1': 1} 或 [{'lineId': 'LINE-1', 'currentGroup': 1}]，优先于 productionLineCurrentGroup",
    )
    operationType: Optional[OperationType] = Field(
        None,
        description="内联生产计划操作类型；当 plans 直接放在 mixed 根节点时使用",
    )
    planDate: Optional[str] = Field(
        None,
        description="内联生产计划日期；当 plans 直接放在 mixed 根节点时使用",
    )
    plans: Optional[List["ProductionPlanInfo"]] = Field(
        None,
        description="内联生产计划列表；当直接放在 mixed 根节点时沿用原 plans 结构",
    )
    model_config = {
        "json_schema_extra": {
            "example": {
                "productionPlan": {
                    "operationType": "UPDATE",
                    "planDate": "2026-01-21 09:00:00",
                    "plans": [
                        {
                            "planId": "PLAN-LINE1-20260121",
                            "lineId": "1",
                            "planIndex": [
                                {
                                    "requiredSkus": [
                                        [{"skuId": "2801021-H19H0", "quantity": 1}, {"skuId": "2801037-H19H0", "quantity": 1}]
                                    ]
                                }
                            ]
                        }
                    ]
                },
                "currentGroups": {"LINE-1": 1},
                "productionLineCurrentGroup": {"LINE-1": 0},
                "tasks": [
                    {
                        "taskId": "OUTBOUND_PL1_GP1_2801021-H19H0_2801037-H19H0_20260121101500_001",
                        "taskType": "OUTBOUND",
                        "planId": "PLAN-LINE1",
                        "planIndex": 1,
                        "skus": [
                            {"skuId": "2801021-H19H0", "quantity": 1},
                            {"skuId": "2801037-H19H0", "quantity": 1}
                        ]
                    }
                ],
                "aisleStatus": [
                    {
                        "aisleId": "1",
                        "isAvailable": True,
                        "unavailableReason": None,
                        "exitCongestion": [
                            {"lineId": "1", "isCongested": False},
                            {"lineId": "2", "isCongested": False},
                            {"lineId": "3", "isCongested": False}
                        ],
                        "bank": "LEFT"
                    },
                    {
                        "aisleId": "2",
                        "isAvailable": True,
                        "unavailableReason": None,
                        "exitCongestion": [
                            {"lineId": "1", "isCongested": False},
                            {"lineId": "2", "isCongested": False},
                            {"lineId": "3", "isCongested": False}
                        ],
                        "bank": "LEFT"
                    },
                    {
                        "aisleId": "3",
                        "isAvailable": True,
                        "unavailableReason": None,
                        "exitCongestion": [
                            {"lineId": "1", "isCongested": False},
                            {"lineId": "2", "isCongested": False},
                            {"lineId": "3", "isCongested": False}
                        ],
                        "bank": "RIGHT"
                    },
                ],
                "inventory": [{
                    "aisleId": "1",
                    "row": 1,
                    "column": 1,
                    "level": 1,
                    "shelf": "UPPER",
                    "positions": [
                        {"skuId": "2801021-H19H0", "quantity": 1}
                    ]
                }]
            }
        }
    }


class AssignedTaskResponse(BaseModel):
    """分配的任务详情"""
    taskId: str = Field(..., description="任务ID")
    taskType: TaskType = Field(..., description="任务类型")
    planId: Optional[str] = Field(None, description="计划ID")
    planIndex: Optional[int] = Field(None, description="计划下第几组")
    positions: Optional[List[PositionInfo]] = Field(None, description="货位坐标")

    model_config = {
        "json_schema_extra": {
            "example": {
                "taskId": "OUTBOUND_PL1_GP1_2801021-H19H0_2801037-H19H0_20260121101500_001",
                "taskType": "OUTBOUND",
                "planId": "PLAN-LINE1",
                "planIndex": 1,
                "positions": [
                    {
                        "row": 1,
                        "column": 5,
                        "level": 2,
                        "shelf": "UPPER",
                        "skuId": "2801021-H19H0",
                        "quantity": 1
                    }
                ]
            }
        }
    }


class AisleAssignmentResponse(BaseModel):
    """巷道分配结果"""
    aisleId: str = Field(..., description="巷道ID")
    assignedTask: Optional[AssignedTaskResponse] = Field(None, description="分配的任务")
    matchedTasks: Optional[List[AssignedTaskResponse]] = Field(
        None,
        description="本次请求中匹配到该巷道的任务列表（用于预匹配/排队展示，不代表立即下发执行）",
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "aisleId": "1",
                "assignedTask": {
                    "taskId": "OUTBOUND_PL1_GP1_2801021-H19H0_2801037-H19H0_20260121101500_001",
                    "taskType": "OUTBOUND",
                    "planId": "PLAN-LINE1",
                    "planIndex": 1,
                    "positions": [
                        {
                            "row": 1,
                            "column": 5,
                            "level": 2,
                            "shelf": "UPPER",
                            "skuId": "2801021-H19H0",
                            "quantity": 1
                        }
                    ]
                }
            }
        }
    }


class MixedScheduleResponse(BaseModel):
    """混合调度响应（data 部分）"""
    scheduleId: str = Field(..., description="调度ID")
    timestamp: str = Field(..., description="时间戳")
    aisleAssignments: List[AisleAssignmentResponse] = Field(..., description="巷道分配结果")

    model_config = {
        "json_schema_extra": {
            "example": {
                "scheduleId": "SCH-A1B2C3D4",
                "timestamp": "2026-01-15T10:00:05Z",
                "aisleAssignments": [
                    {
                        "aisleId": "1",
                        "assignedTask": {
                            "taskId": "OUTBOUND_PL1_GP1_2801021-H19H0_2801037-H19H0_20260121101500_001",
                            "taskType": "OUTBOUND",
                            "planId": "PLAN-LINE1",
                            "planIndex": 1,
                            "positions": [
                                {
                                    "row": 1,
                                    "column": 5,
                                    "level": 2,
                                    "shelf": "UPPER",
                                    "skuId": "2801021-H19H0",
                                    "quantity": 1
                                }
                            ]
                        }
                    }
                ]
            }
        }
    }


# ============================================================
# 任务执行反馈接口模型
# ============================================================

class TaskFeedbackRequest(BaseModel):
    """任务执行反馈请求"""
    taskId: str = Field(..., description="任务ID")
    taskType: TaskType = Field(..., description="任务类型")
    status: TaskStatus = Field(..., description="任务状态")
    startTime: str = Field(..., description="开始时间(ISO 8601)")
    failureReason: Optional[str] = Field(None, description="失败原因")

    model_config = {
        "json_schema_extra": {
            "example": {
                "taskId": "OUTBOUND_PL1_GP1_2801021-H19H0_2801037-H19H0_20260121101500_001",
                "taskType": "OUTBOUND",
                "status": "COMPLETED",
                "startTime": "2026-01-15T10:00:00Z",
                "failureReason": None
            }
        }
    }

    def _check_failure_reason(self):
        if self.status == TaskStatus.FAILED and not (self.failureReason or "").strip():
            raise ValueError("failureReason is required when status is FAILED")
        return self

    if model_validator is not None:
        @model_validator(mode="after")
        def _validate_failure_reason(self):
            return self._check_failure_reason()
    else:
        @root_validator(skip_on_failure=True)
        def _validate_failure_reason(cls, values):
            if values.get("status") == TaskStatus.FAILED and not (values.get("failureReason") or "").strip():
                raise ValueError("failureReason is required when status is FAILED")
            return values


class TaskFeedbackResponse(BaseModel):
    """任务执行反馈响应（已弃用，仅做内部参考）

    实际返回格式为 ApiResponse，此接口 data 为 null。
    """
    status: ScheduleStatus = Field(..., description="反馈结果状态")

    model_config = {
        "json_schema_extra": {
            "example": {
                "status": "SUCCESS"
            }
        }
    }


# ============================================================
# 入库分配接口模型
# ============================================================

class InboundTaskRequest(BaseModel):
    """入库任务请求"""
    taskId: str = Field(..., description="任务ID")
    skus: List[SkuInfo] = Field(..., description="货物列表")

    model_config = {
        "json_schema_extra": {
            "example": {
                "taskId": "INBOUND-TEST-001",
                "skus": [
                    {"skuId": "2801021-H19H0", "quantity": 1},
                    {"skuId": "2801037-H19H0", "quantity": 1}
                ]
            }
        }
    }

    def _check_single_beam_requirements(self):
        if len(self.skus) == 1 and self.skus[0].quantity > 0:
            if self.skus[0].beamSide is None:
                raise ValueError("single inbound SKU requires beamSide")
        return self

    if model_validator is not None:
        @model_validator(mode="after")
        def _validate_single_beam_requirements(self):
            return self._check_single_beam_requirements()
    else:
        @root_validator(skip_on_failure=True)
        def _validate_single_beam_requirements(cls, values):
            skus = values.get("skus") or []
            if len(skus) == 1 and getattr(skus[0], "quantity", 0) > 0:
                if getattr(skus[0], "beamSide", None) is None:
                    raise ValueError("single inbound SKU requires beamSide")
            return values

class InboundAllocateRequest(BaseModel):
    """入库分配请求"""
    tasks: List[InboundTaskRequest] = Field(..., description="任务列表")

    model_config = {
        "json_schema_extra": {
            "example": {
                "tasks": [
                    {
                        "taskId": "INBOUND-TEST-001",
                        "skus": [
                            {"skuId": "2801021-H19H0", "quantity": 1},
                            {"skuId": "2801037-H19H0", "quantity": 1}
                        ]
                    }
                ]
            }
        }
    }


class InboundAssignmentResponse(BaseModel):
    """入库分配结果"""
    taskId: str = Field(..., description="任务ID")
    recommendedAisle: str = Field(..., description="推荐巷道ID")

    model_config = {
        "json_schema_extra": {
            "example": {
                "taskId": "INBOUND-TEST-001",
                "recommendedAisle": "2"
            }
        }
    }


class InboundAllocateResponse(BaseModel):
    """入库分配响应"""
    allocationId: str = Field(..., description="分配ID")
    assignments: List[InboundAssignmentResponse] = Field(..., description="分配结果")

    model_config = {
        "json_schema_extra": {
            "example": {
                "allocationId": "ALLOC-E5F6G7H8",
                "assignments": [
                    {
                        "taskId": "INBOUND-TEST-001",
                        "recommendedAisle": "2"
                    }
                ]
            }
        }
    }


# ============================================================
# 生产计划接口模型
# ============================================================

class RequiredSku(SkuAttrsMixin):
    """需求SKU信息"""
    skuId: str = Field(..., description="货物ID")
    quantity: int = Field(..., ge=1, description="需求数量")

    model_config = {
        "extra": "allow",
        "json_schema_extra": {
            "example": {
                "skuId": "2801021-H19H0",
                "quantity": 1
            }
        }
    }


class PlanGroup(BaseModel):
    """计划下的一组出库任务"""
    requiredSkus: List[List[RequiredSku]] = Field(..., description="该组的任务列表，每个任务包含多个SKU")

    model_config = {
        "json_schema_extra": {
            "example": {
                "requiredSkus": [
                    [{"skuId": "2801021-H19H0", "quantity": 1}, {"skuId": "2801037-H19H0", "quantity": 1}],
                    [{"skuId": "2801021-H19H0", "quantity": 1}, {"skuId": "2801037-H19H0", "quantity": 1}]
                ]
            }
        }
    }


class ProductionPlanInfo(BaseModel):
    """生产计划信息"""
    planId: str = Field(..., description="计划ID")
    lineId: str = Field(..., description="产线ID")
    planIndex: List[PlanGroup] = Field(..., description="计划下的出库组列表，数组索引+1 即为组号")

    model_config = {
        "json_schema_extra": {
            "example": {
                "planId": "PLAN-LINE1",
                "lineId": "1",
                "planIndex": [
                    {
                        "requiredSkus": [
                            [{"skuId": "2801021-H19H0", "quantity": 1}, {"skuId": "2801037-H19H0", "quantity": 1}],
                            [{"skuId": "2801021-H19H0", "quantity": 1}, {"skuId": "2801037-H19H0", "quantity": 1}]
                        ]
                    }
                ]
            }
        }
    }


class ProductionPlanRequest(BaseModel):
    """生产计划请求"""
    operationType: OperationType = Field(..., description="操作类型：ADD（新增计划）或 UPDATE（更新计划，替换原有计划）")
    planDate: str = Field(..., description="生产计划的日期，格式：YYYY-MM-DD HH:mm:ss")
    plans: List[ProductionPlanInfo] = Field(..., description="当日各产线的生产计划列表")

    model_config = {
        "json_schema_extra": {
            "example": {
                "operationType": "ADD",
                "planDate": "2026-01-20 10:00:00",
                "plans": [
                    {
                        "planId": "PLAN-LINE1",
                        "lineId": "1",
                        "planIndex": [
                            {
                                "requiredSkus": [
                                    [{"skuId": "2801021-H19H0", "quantity": 1}, {"skuId": "2801037-H19H0", "quantity": 1}],
                                    [{"skuId": "2801021-H19H0", "quantity": 1}, {"skuId": "2801037-H19H0", "quantity": 1}]
                                ]
                            },
                            {
                                "requiredSkus": [
                                    [{"skuId": "2801021-H19H0", "quantity": 1}, {"skuId": "2801037-H19H0", "quantity": 1}]
                                ]
                            }
                        ]
                    },
                    {
                        "planId": "PLAN-LINE2",
                        "lineId": "2",
                        "planIndex": [
                            {
                                "requiredSkus": [
                                    [{"skuId": "2801022-H17F4", "quantity": 1}, {"skuId": "2801038-H17F4", "quantity": 1}]
                                ]
                            }
                        ]
                    }
                ]
            }
        }
    }


class ProductionPlanResponse(BaseModel):
    """生产计划响应（已弃用，仅做内部参考）

    实际返回格式为 ApiResponse，此接口 data 为 null。
    """
    status: ScheduleStatus = Field(..., description="操作结果状态")

    model_config = {
        "json_schema_extra": {
            "example": {
                "status": "SUCCESS"
            }
        }
    }


try:
    MixedScheduleRequest.model_rebuild()
except AttributeError:
    MixedScheduleRequest.update_forward_refs(
        ProductionPlanRequest=ProductionPlanRequest,
        ProductionPlanInfo=ProductionPlanInfo,
    )


# ============================================================
# 通用响应模型
# ============================================================

class ApiResponse(BaseModel):
    """统一 API 响应格式

    所有接口均使用此格式返回，包括成功、失败、参数校验错误等。
    """
    status: str = Field(..., description="状态: SUCCESS / FAILED")
    message: str = Field("", description="描述信息")
    data: Optional[Any] = Field(None, description="业务数据，具体结构因接口而异；无业务数据时为 null")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "summary": "成功（含 data）",
                    "value": {
                        "status": "SUCCESS",
                        "message": "调度成功",
                        "data": {
                            "scheduleId": "SCH-A1B2C3D4",
                            "timestamp": "2026-01-15T10:00:05Z",
                            "aisleAssignments": []
                        }
                    }
                },
                {
                    "summary": "成功（无 data）",
                    "value": {
                        "status": "SUCCESS",
                        "message": "反馈处理成功",
                        "data": None
                    }
                },
                {
                    "summary": "失败",
                    "value": {
                        "status": "FAILED",
                        "message": "存在未确认的任务",
                        "data": None
                    }
                },
                {
                    "summary": "参数校验失败 (422)",
                    "value": {
                        "status": "FAILED",
                        "message": "请求参数验证失败",
                        "data": {"errors": ["body -> tasks: field required"]}
                    }
                }
            ]
        }
    }


class ErrorResponse(BaseModel):
    """错误响应（与 ApiResponse 结构一致）"""
    status: str = Field("FAILED", description="状态")
    message: str = Field(..., description="错误信息")
    data: Optional[Any] = Field(None, description="附加数据")

    model_config = {
        "json_schema_extra": {
            "example": {
                "status": "FAILED",
                "message": "存在未确认的任务，请等待反馈后再请求调度。",
                "data": {
                    "unconfirmed_tasks": ["OUTBOUND-R1-001"],
                    "timestamp": "2026-01-15T10:00:05Z"
                }
            }
        }
    }


class HealthResponse(BaseModel):
    """健康检查响应（已弃用，仅做内部参考）

    实际返回格式为 ApiResponse，健康信息放在 data 字段中。
    """
    status: str = Field(..., description="服务状态")
    version: str = Field(..., description="版本号")
    timestamp: str = Field(..., description="时间戳")

    model_config = {
        "json_schema_extra": {
            "example": {
                "status": "healthy",
                "version": "1.0.0",
                "timestamp": "2026-01-15T10:00:00Z"
            }
        }
    }


# ============================================================
# BOM 配置接口模型
# ============================================================

class BomConfigData(BaseModel):
    """BOM 配置数据"""
    sku_types: List[str] = Field(..., description="SKU类型列表")
    sku_pairs: Dict[str, str] = Field(..., description="SKU配对关系，key和value互为配对")
    sku_solo: Dict[str, bool] = Field(..., description="单独存放的SKU，需要独立货位")
    sku_to_production_line: Dict[str, List[str]] = Field(..., description="SKU到产线的映射关系")

    model_config = {
        "json_schema_extra": {
            "example": {
                "sku_types": [
                    "2801021-H19H0",
                    "2801037-H19H0",
                    "2801021-H22F0"
                ],
                "sku_pairs": {
                    "2801021-H19H0": "2801037-H19H0",
                    "2801037-H19H0": "2801021-H19H0",
                    "2801021-H22F0": "2801021-H22F0"
                },
                "sku_solo": {
                    "2801021-H22F0": True
                },
                "sku_to_production_line": {
                    "2801021-H19H0": ["1", "2", "3"],
                    "2801037-H19H0": ["1", "2", "3"],
                    "2801021-H22F0": ["1", "2", "3"]
                }
            }
        }
    }


class BomUpdateRequest(BaseModel):
    """BOM 配置更新请求"""
    config: BomConfigData = Field(..., description="完整的SKU配置数据")

    model_config = {
        "json_schema_extra": {
            "example": {
                "config": {
                    "sku_types": [
                        "2801021-H19H0",
                        "2801037-H19H0"
                    ],
                    "sku_pairs": {
                        "2801021-H19H0": "2801037-H19H0",
                        "2801037-H19H0": "2801021-H19H0"
                    },
                    "sku_solo": {},
                    "sku_to_production_line": {
                        "2801021-H19H0": ["1", "2", "3"],
                        "2801037-H19H0": ["1", "2", "3"]
                    }
                }
            }
        }
    }


class BomUpdateResponse(BaseModel):
    """BOM 配置更新响应（data 部分）"""
    timestamp: str = Field(..., description="时间戳")

    model_config = {
        "json_schema_extra": {
            "example": {
                "timestamp": "2026-01-21T10:00:00Z"
            }
        }
    }

