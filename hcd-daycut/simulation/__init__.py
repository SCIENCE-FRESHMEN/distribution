"""
Simulation package.

Keep this module lightweight: importing `simulation.*` submodules should not eagerly import the whole
warehouse core, otherwise it is easy to create circular imports (e.g. estimators -> position -> package
__init__ -> warehouse_core -> estimators).
"""

__all__ = [
    "InventoryPosition",
    "TaskData",
    "InventoryManager",
    "MetricsCalculator",
    "WarehouseCore",
]

