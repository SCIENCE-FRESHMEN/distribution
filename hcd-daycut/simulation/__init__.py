"""仿真核心包的公共导出入口。

本文件通过惰性导出对外提供核心类，避免导入任意 ``simulation.*`` 子模块时立即
加载 ``warehouse_core`` 而形成循环导入。类型检查阶段仍显式声明各名称，确保
``__all__`` 与实际可访问的公共接口保持一致。
"""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .inventory import InventoryManager
    from .metrics import MetricsCalculator
    from .position import InventoryPosition
    from .task_data import TaskData
    from .warehouse_core import WarehouseCore

__all__ = [
    "InventoryPosition",
    "TaskData",
    "InventoryManager",
    "MetricsCalculator",
    "WarehouseCore",
]

_PUBLIC_CLASS_MODULES = {
    "InventoryPosition": (".position", "InventoryPosition"),
    "TaskData": (".task_data", "TaskData"),
    "InventoryManager": (".inventory", "InventoryManager"),
    "MetricsCalculator": (".metrics", "MetricsCalculator"),
    "WarehouseCore": (".warehouse_core", "WarehouseCore"),
}


def __getattr__(name: str) -> Any:
    """按需加载包级公开类，兼顾公共导出与循环导入保护。

    Args:
        name: 调用方从 ``simulation`` 包读取的属性名称。

    Returns:
        对应子模块中定义的公共类对象。

    Raises:
        AttributeError: ``name`` 不是本包声明的公共类时抛出。
    """
    module_info = _PUBLIC_CLASS_MODULES.get(name)
    if module_info is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_name, class_name = module_info
    value = getattr(import_module(module_name, __name__), class_name)
    globals()[name] = value  # 缓存已解析的类，后续访问无需重复导入。
    return value

