from dataclasses import dataclass
from simulation.task_data import TaskData
from copy import deepcopy


# 事件类型常量
EVENT_TASK_COMPLETE = 'task_complete'
EVENT_INBOUND_UNASSIGNED = 'inbound_unassigned'           # 入库任务待分配巷道
EVENT_INBOUND_ARRIVAL_AT_AISLE = 'inbound_arrival_at_aisle'  # 入库任务到达指定巷道入口
EVENT_CONGESTION_CLEAR = 'congestion_clear'               # 某巷道*产线拥堵结束
EVENT_CRANE_AVAILABLE = 'crane_available'                 # 磁力吊可用


@dataclass
class Event:
    time: float
    event_id: str
    event_type: str
    task: TaskData

    def __lt__(self, other):
        return self.time < other.time

    def __repr__(self):
        return f"Event({self.time:.2f}, {self.event_id}, {self.event_type},{self.task.assigned_aisle if self.task.assigned_aisle else ''},{self.task.task_id if self.task else None})"

    def copy(self):
        return Event(self.time, self.event_id, self.event_type, deepcopy(self.task))