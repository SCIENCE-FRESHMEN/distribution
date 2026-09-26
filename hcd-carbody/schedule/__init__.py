"""导出调度器实现供仿真和接口服务使用。"""

# ==========================================================================
# 模块导出：调度器工厂与公共类型
# ==========================================================================

"""


"""

from .optimizer import OptimizationScheduler
from .heuristic import HeuristicScheduler

# 提供基于类型字符串的调度器工厂与映射
SCHEDULER_MAP = {
    'heuristic': HeuristicScheduler,
    'optimization': OptimizationScheduler,
}

def get_scheduler(scheduler_type: str):
    """获取scheduler相关逻辑。

    Args:
        scheduler_type: 用于本函数处理的 `scheduler_type` 参数。

    Returns:
        处理结果；具体类型由调用上下文决定。
    """
    return SCHEDULER_MAP.get((scheduler_type or 'heuristic').lower(), HeuristicScheduler)

__all__ = ['OptimizationScheduler', 'HeuristicScheduler', 'get_scheduler', 'SCHEDULER_MAP']

