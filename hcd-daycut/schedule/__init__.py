"""调度策略包。根据配置名称向仓库核心返回可用的调度器实现。"""

# 调度包对外只暴露调度器工厂和两种实现，调用方不需要依赖内部模块路径。
from .optimizer import OptimizationScheduler
from .heuristic import HeuristicScheduler

# 提供基于类型字符串的调度器工厂与映射
SCHEDULER_MAP = {
    'heuristic': HeuristicScheduler,
    'optimization': OptimizationScheduler,
}

def get_scheduler(scheduler_type: str):
    """读取并返回指定条件下的状态、对象或计算结果，不主动改变业务状态。

    输入：scheduler_type（str）

    Args:
        scheduler_type (str): 供当前处理流程使用的 `scheduler_type` 值。

    Returns:
        Any: 当前处理流程产生的结果；具体结构由函数摘要说明。
    """
    return SCHEDULER_MAP.get((scheduler_type or 'heuristic').lower(), HeuristicScheduler)

__all__ = ['OptimizationScheduler', 'HeuristicScheduler', 'get_scheduler', 'SCHEDULER_MAP']

