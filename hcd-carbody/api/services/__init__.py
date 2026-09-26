"""
服务层模块
"""

# ==========================================================================
# 模块导出：API 服务层公共对象
# ==========================================================================

from .warehouse_service import WarehouseService, get_warehouse_service

__all__ = ["WarehouseService", "get_warehouse_service"]

