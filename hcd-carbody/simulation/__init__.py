"""导出仿真基础对象，并延迟加载核心以避免包级循环导入。"""

# ==========================================================================
# 模块导出：仿真核心公共类型
# ==========================================================================

from typing import TYPE_CHECKING, Any

from .position import InventoryPosition
from .task_data import TaskData
from .inventory import InventoryManager
from .metrics import MetricsCalculator

if TYPE_CHECKING:
    from .warehouse_core import WarehouseCore


def __getattr__(name: str) -> Any:
    """按需导出 WarehouseCore，避免 position/inventory 导入时提前加载调度器。"""
    if name == "WarehouseCore":
        from .warehouse_core import WarehouseCore
        return WarehouseCore
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = ['InventoryPosition', 'TaskData', 'InventoryManager', 'MetricsCalculator', 'WarehouseCore']

