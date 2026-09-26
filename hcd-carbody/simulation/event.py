"""定义事件驱动仿真使用的事件类型和事件对象。"""

from dataclasses import dataclass
from simulation.task_data import TaskData
from copy import deepcopy


# 事件类型常量
EVENT_TASK_COMPLETE = 'task_complete'
EVENT_INBOUND_UNASSIGNED = 'inbound_unassigned'           # 入库任务待分配巷道
EVENT_INBOUND_ARRIVAL_AT_AISLE = 'inbound_arrival_at_aisle'  # 入库任务到达指定巷道入口
EVENT_CONGESTION_CLEAR = 'congestion_clear'               # 某巷道*产线拥堵结束
EVENT_CRANE_AVAILABLE = 'crane_available'                 # 磁力吊可用


# ==========================================================================
# 数据定义：事件队列元素、比较规则和副本生成
# ==========================================================================
@dataclass
class Event:
    time: float
    event_id: str
    event_type: str
    task: TaskData

    def __lt__(self, other):
        """执行 lt 对应的业务处理。

        Args:
            self: 当前对象实例。
            other: 用于本函数处理的 `other` 参数。

        Returns:
            处理结果；具体类型由调用上下文决定。
        """
        return self.time < other.time

    def __repr__(self):
        """执行 repr 对应的业务处理。

        Args:
            self: 当前对象实例。

        Returns:
            处理结果；具体类型由调用上下文决定。
        """
        return f"Event({self.time:.2f}, {self.event_id}, {self.event_type},{self.task.assigned_aisle if self.task.assigned_aisle else ''},{self.task.task_id if self.task else None})"

    def copy(self):
        """执行 copy 对应的业务处理。

        Args:
            self: 当前对象实例。

        Returns:
            处理结果；具体类型由调用上下文决定。
        """
        return Event(self.time, self.event_id, self.event_type, deepcopy(self.task))
