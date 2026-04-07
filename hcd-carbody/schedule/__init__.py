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
    return SCHEDULER_MAP.get((scheduler_type or 'heuristic').lower(), HeuristicScheduler)

__all__ = ['OptimizationScheduler', 'HeuristicScheduler', 'get_scheduler', 'SCHEDULER_MAP']

