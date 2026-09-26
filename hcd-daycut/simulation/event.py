"""离散事件仿真的统一事件定义。

``WarehouseCore`` 将事件放入按 ``time`` 排序的堆队列，并通过
``WarehouseCore.on_event`` 消费。事件只描述何时发生什么；库存、巷道状态和
任务队列的实际修改均由核心事件处理器完成。
"""

from dataclasses import dataclass
from simulation.task_data import TaskData
from copy import deepcopy


# 前两类通常由任务到达或分配结果产生；完成、清堵和磁力吊可用事件由前序
# 任务的时间估计继续派生。
EVENT_TASK_COMPLETE = 'task_complete'                     # 任务完成
EVENT_INBOUND_UNASSIGNED = 'inbound_unassigned'           # 入库任务待分配巷道
EVENT_INBOUND_ARRIVAL_AT_AISLE = 'inbound_arrival_at_aisle'  # 入库任务到达指定巷道入口
EVENT_CONGESTION_CLEAR = 'congestion_clear'               # 某巷道*产线拥堵结束
EVENT_CRANE_AVAILABLE = 'crane_available'                 # 磁力吊可用


@dataclass
class Event:
    """事件队列元素。

    ``event_id`` 用于日志和定位；``task`` 是关联任务快照。``__lt__`` 仅按
    发生时间比较，使 ``heapq`` 可直接作为仿真时间推进队列使用。
    """
    time: float
    event_id: str
    event_type: str
    task: TaskData

    def __lt__(self, other):
        """按发生时间排序事件，使 ``heapq`` 先弹出最早事件。"""
        return self.time < other.time

    def __repr__(self):
        """返回包含时间、事件类型、巷道和任务 ID 的调试文本。"""
        return f"Event({self.time:.2f}, {self.event_id}, {self.event_type},{self.task.assigned_aisle if self.task.assigned_aisle else ''},{self.task.task_id if self.task else None})"

    def copy(self):
        """复制事件及其任务快照，供候选方案试算隔离使用。

        Returns:
            时间和事件标识相同、但 ``task`` 已深拷贝的新事件。
        """
        return Event(self.time, self.event_id, self.event_type, deepcopy(self.task))
