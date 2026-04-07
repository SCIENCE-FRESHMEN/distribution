"""
Pydantic 数据模型定义
基于 API接口字段说明文档 定义所有请求/响应模型
"""

from datetime import datetime
from typing import List, Optional, Dict, Any, Union, Union
import json
from pathlib import Path
from enum import Enum
from pydantic import BaseModel, Field, model_validator
try:
    import json5  # type: ignore
except Exception:
    json5 = None


_MATCH_FIELDS: Optional[List[str]] = None


def _load_match_fields() -> List[str]:
    global _MATCH_FIELDS
    if _MATCH_FIELDS is not None:
        return _MATCH_FIELDS
    config_path = Path(__file__).resolve().parents[1] / "config" / "warehouse.json"
    try:
        text = config_path.read_text(encoding="utf-8")
        if json5 is not None:
            cfg = json5.loads(text)
        else:
            cfg = json.loads(text)
        _MATCH_FIELDS = list(cfg.get("match_fields", []) or [])
    except Exception:
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
            _MATCH_FIELDS = list(cfg.get("match_fields", []) or [])
        except Exception:
            _MATCH_FIELDS = []
    return _MATCH_FIELDS


class SkuAttrsMixin(BaseModel):
    """SKU附加属性校验混入"""

    model_config = {
        "extra": "allow",
    }

    @model_validator(mode="after")
    def _validate_match_fields(self):
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


class DockAvailabilityRequest(BaseModel):
    """巷道内口位可用性（可选）"""
    direction: str = Field(..., description="方向：INBOUND/OUTBOUND（兼容 IN/OUT）")
    lineRef: Union[int, str] = Field(..., description="口位标识，支持数字或LxCy")
    isAvailable: bool = Field(..., description="该口位是否可用")
    reason: Optional[str] = Field(None, description="不可用原因")

    model_config = {
        "extra": "allow",
        "json_schema_extra": {
            "example": {
                "direction": "OUTBOUND",
                "lineRef": "L2C17",
                "isAvailable": False,
                "reason": "MAINTENANCE",
            }
        },
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
    inLine: Optional[Union[int, str]] = Field(None, description="入库线（可选，支持数字或LxCy）")
    outLine: Optional[Union[int, str]] = Field(None, description="出库线（可选，支持数字或LxCy）")
    skus: List[SkuInfo] = Field(..., description="货物列表")
    inboundUrgent: Optional[bool] = Field(False, description="是否紧急入库")

    model_config = {
        "json_schema_extra": {
            "example": {
                "taskId": "OUTBOUND-R1-001",
                "taskType": "OUTBOUND",
                "planId": "PLAN-LINE1-20260121",
                "planIndex": 1,
                "outLine": "L1C17",
                "skus": [
                    {
                        "skuId": "1RAT000001",
                        "quantity": 1,
                        "features": {
                            "color": "W1",
                            "skid_type": "0",
                            "skid_state": "1"
                        }
                    }
                ]
            }
        }
    }


class AisleStatusRequest(BaseModel):
    """巷道状态信息"""
    aisleId: str = Field(..., description="巷道ID")
    isAvailable: bool = Field(..., description="是否可用")
    unavailableReason: Optional[str] = Field(None, description="不可用原因")
    exitCongestion: List[ExitCongestion] = Field(..., description="各产线拥堵状态")
    dockAvailability: Optional[List[DockAvailabilityRequest]] = Field(
        None, description="巷道+口位可用性（可选）"
    )
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

    model_config = {
        "json_schema_extra": {
            "example": {
                "tasks": [
                    {
                        "taskId": "OUTBOUND-PL1-G1-1RAT000001",
                        "taskType": "OUTBOUND",
                        "planId": "PLAN-LINE1-20260121",
                        "planIndex": 1,
                        "outLine": "L1C17",
                        "skus": [
                            {
                                "skuId": "1RAT000001",
                                "quantity": 1,
                                "features": {
                                    "color": "W1",
                                    "skid_type": "0",
                                    "skid_state": "1"
                                }
                            }
                        ]
                    },
                    {
                        "taskId": "INBOUND-1RAT000012",
                        "taskType": "INBOUND",
                        "targetAisle": "1",
                        "inLine": "L4C1",
                        "outLine": "L1C17",
                        "skus": [
                            {
                                "skuId": "1RAT000012",
                                "quantity": 1,
                                "features": {
                                    "color": "W1",
                                    "skid_type": "0",
                                    "skid_state": "1"
                                }
                            }
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
                    "column": 2,
                    "level": 1,
                    "positions": [
                        {
                            "skuId": "1RAT000001",
                            "quantity": 1,
                            "features": {
                                "color": "W1",
                                "skid_type": "0",
                                "skid_state": "1"
                            }
                        }
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
                "taskId": "OUTBOUND-R1-001",
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

    model_config = {
        "json_schema_extra": {
            "example": {
                "aisleId": "1",
                "assignedTask": {
                    "taskId": "OUTBOUND-R1-001",
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
    """混合调度响应"""
    scheduleId: str = Field(..., description="调度ID")
    status: ScheduleStatus = Field(..., description="调度状态")
    timestamp: str = Field(..., description="时间戳")
    aisleAssignments: List[AisleAssignmentResponse] = Field(..., description="巷道分配结果")

    model_config = {
        "json_schema_extra": {
            "example": {
                "scheduleId": "SCH-A1B2C3D4",
                "status": "SUCCESS",
                "timestamp": "2026-01-15T10:00:05Z",
                "aisleAssignments": [
                    {
                        "aisleId": "1",
                        "assignedTask": {
                            "taskId": "OUTBOUND-R1-001",
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
                "taskId": "OUTBOUND-R1-001",
                "taskType": "OUTBOUND",
                "status": "COMPLETED",
                "startTime": "2026-01-15T10:00:00Z",
                "failureReason": None
            }
        }
    }


class TaskFeedbackResponse(BaseModel):
    """任务执行反馈响应"""
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
    inLine: Union[int, str] = Field(..., description="入库口/入口线，支持数字或LxCy")
    outLine: Union[int, str] = Field(..., description="出库口/出口线，支持数字或LxCy")
    skus: List[SkuInfo] = Field(..., description="货物列表")

    model_config = {
        "json_schema_extra": {
            "example": {
                "taskId": "INBOUND-TEST-001",
                "inLine": "L4C1",
                "outLine": "L1C17",
                "skus": [
                    {
                        "skuId": "1RAT000012",
                        "quantity": 1,
                        "features": {
                            "color": "W1",
                            "skid_type": "0",
                            "skid_state": "1"
                        }
                    }
                ]
            }
        }
    }


class InboundAllocateRequest(BaseModel):
    """入库分配请求"""
    tasks: List[InboundTaskRequest] = Field(..., description="任务列表")

    model_config = {
        "json_schema_extra": {
            "example": {
                "tasks": [
                    {
                        "taskId": "INBOUND-TEST-001",
                        "inLine": "L4C1",
                        "outLine": "L1C17",
                        "skus": [
                            {
                                "skuId": "1RAT000012",
                                "quantity": 1,
                                "features": {
                                    "color": "W1",
                                    "skid_type": "0",
                                    "skid_state": "1"
                                }
                            }
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
                        "planId": "PLAN-LINE1-20260121",
                        "lineId": "LINE-1",
                        "planIndex": [
                            {
                                "requiredSkus": [
                                    [
                                        {
                                            "skuId": "1RAT000001",
                                            "quantity": 1,
                                            "features": {
                                                "color": "W1",
                                                "skid_type": "0",
                                                "skid_state": "1"
                                            }
                                        }
                                    ],
                                    [
                                        {
                                            "skuId": "1RAT000002",
                                            "quantity": 1,
                                            "features": {
                                                "color": "W1",
                                                "skid_type": "0",
                                                "skid_state": "1"
                                            }
                                        }
                                    ]
                                ]
                            }
                        ]
                    },
                    {
                        "planId": "PLAN-LINE2-20260121",
                        "lineId": "2",
                        "planIndex": [
                            {
                                "requiredSkus": [
                                    [
                                        {
                                            "skuId": "1RAK000005",
                                            "quantity": 1,
                                            "features": {
                                                "color": "W1",
                                                "skid_type": "0",
                                                "skid_state": "1"
                                            }
                                        }
                                    ]
                                ]
                            }
                        ]
                    }
                ]
            }
        }
    }


class ProductionPlanResponse(BaseModel):
    """生产计划响应"""
    status: ScheduleStatus = Field(..., description="操作结果状态")

    model_config = {
        "json_schema_extra": {
            "example": {
                "status": "SUCCESS"
            }
        }
    }


# ============================================================
# 通用响应模型
# ============================================================

class ErrorResponse(BaseModel):
    """错误响应"""
    error: str = Field(..., description="错误信息")
    detail: Optional[str] = Field(None, description="详细信息")
    timestamp: str = Field(..., description="时间戳")

    model_config = {
        "json_schema_extra": {
            "example": {
                "error": "存在未确认的任务",
                "detail": "请等待反馈后再请求调度。未确认任务: ['OUTBOUND-R1-001']",
                "timestamp": "2026-01-15T10:00:10Z"
            }
        }
    }


class HealthResponse(BaseModel):
    """健康检查响应"""
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
    """BOM 配置更新响应"""
    status: ScheduleStatus = Field(..., description="更新状态")
    message: Optional[str] = Field(None, description="更新结果消息")
    timestamp: str = Field(..., description="时间戳")

    model_config = {
        "json_schema_extra": {
            "example": {
                "status": "SUCCESS",
                "message": "SKU配置更新成功",
                "timestamp": "2026-01-21T10:00:00Z"
            }
        }
    }

