"""
BOM配置接口路由
"""

from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Depends

from ..models import (
    BomUpdateRequest,
    ApiResponse,
)
from ..services.warehouse_service import get_warehouse_service, WarehouseService

router = APIRouter(prefix="/bom", tags=["BOM配置"])


@router.post(
    "/update",
    response_model=ApiResponse,
    responses={
        200: {
            "description": "更新成功",
            "content": {"application/json": {"example": {
                "status": "SUCCESS",
                "message": "SKU配置更新成功",
                "data": {"timestamp": "2026-01-21T10:00:00Z"}
            }}}
        },
        400: {
            "description": "配置数据验证失败",
            "content": {"application/json": {"example": {
                "status": "FAILED",
                "message": "配置数据验证失败: ...",
                "data": None
            }}}
        },
        422: {
            "description": "请求参数验证失败",
            "content": {"application/json": {"example": {
                "status": "FAILED",
                "message": "请求参数验证失败",
                "data": {"errors": ["body -> config: field required"]}
            }}}
        },
        500: {
            "description": "服务器内部错误",
            "content": {"application/json": {"example": {
                "status": "FAILED",
                "message": "更新 BOM 配置失败: ...",
                "data": None
            }}}
        },
    },
)
async def update_bom_config(
    request: BomUpdateRequest,
    warehouse_service: WarehouseService = Depends(get_warehouse_service)
) -> ApiResponse:
    """
    更新 BOM 配置（SKU 配置）
    
    此接口用于更新系统的 SKU 配置信息，包括：
    - sku_types: SKU 类型列表
    - sku_pairs: SKU 配对关系（可放在同一货位的上下两层）
    - sku_solo: 需要单独存放的 SKU
    - sku_to_production_line: SKU 到产线的映射关系
    
    **注意事项：**
    - 配置更新会立即生效
    - 不会影响现有库存和任务状态
    - 建议在系统空闲时更新
    - 必须提供完整的配置数据
    """
    try:
        # 转换配置数据格式
        if hasattr(request.config, "model_dump"):
            config_data = request.config.model_dump()
        else:
            config_data = request.config.dict()
        
        # 调用 warehouse_service 更新配置
        success = warehouse_service.update_sku_config(config_data)
        
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if success:
            return ApiResponse(
                status="SUCCESS",
                message="SKU配置更新成功",
                data={"timestamp": timestamp}
            )
        else:
            return ApiResponse(
                status="FAILED",
                message="SKU配置更新失败",
                data={"timestamp": timestamp}
            )
            
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=f"配置数据验证失败: {str(e)}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"更新 BOM 配置失败: {str(e)}"
        )


@router.get("/config")
async def get_bom_config(
    warehouse_service: WarehouseService = Depends(get_warehouse_service)
):
    """
    获取当前 BOM 配置（调试接口）
    """
    try:
        config = {
            "sku_types": warehouse_service.core.sku_types,
            "sku_pairs": warehouse_service.core.sku_pairs,
            "sku_solo": warehouse_service.core.sku_solo,
            "sku_to_production_line": warehouse_service.core.sku_to_production_line
        }
        return {
            "status": "SUCCESS",
            "message": "获取BOM配置成功",
            "data": {"config": config}
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"获取 BOM 配置失败: {str(e)}"
        )

