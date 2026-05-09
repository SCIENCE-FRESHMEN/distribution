"""
全局状态管理模块
管理待反馈任务队列和系统状态
"""

import asyncio
from datetime import datetime, timedelta
from typing import Dict, Optional, Set
from dataclasses import dataclass, field
from enum import Enum
import threading


class PendingTaskStatus(str, Enum):
    """待反馈任务状态"""
    PENDING = "PENDING"           # 已发送，等待EXECUTING反馈
    CONFIRMED = "CONFIRMED"       # 已收到EXECUTING反馈，确认传输成功
    COMPLETED = "COMPLETED"       # 任务已完成
    FAILED = "FAILED"             # 任务失败
    TIMEOUT = "TIMEOUT"           # 超时未收到反馈


@dataclass
class PendingTask:
    """待反馈任务信息"""
    task_id: str
    task_type: str
    aisle_id: Optional[str]
    status: PendingTaskStatus
    created_at: datetime
    confirmed_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    timeout_seconds: float = 60.0  # 默认60秒超时
    
    def is_timeout(self) -> bool:
        """检查是否超时"""
        if self.status != PendingTaskStatus.PENDING:
            return False
        elapsed = (datetime.utcnow() - self.created_at).total_seconds()
        return elapsed > self.timeout_seconds


class TaskStateManager:
    """任务状态管理器"""
    
    def __init__(self, default_timeout: float = 60.0):
        self.default_timeout = default_timeout
        self._pending_tasks: Dict[str, PendingTask] = {}
        self._lock = threading.RLock()
        self._confirmed_tasks: Set[str] = set()
        
    def add_pending_task(self, task_id: str, task_type: str, 
                         aisle_id: Optional[str] = None,
                         timeout_seconds: Optional[float] = None) -> PendingTask:
        """添加待反馈任务"""
        with self._lock:
            existing = self._pending_tasks.get(task_id)
            # Idempotent guard: avoid downgrading CONFIRMED task back to PENDING.
            if existing is not None and existing.status in (
                PendingTaskStatus.PENDING,
                PendingTaskStatus.CONFIRMED,
            ):
                if aisle_id is not None:
                    existing.aisle_id = aisle_id
                return existing

            task = PendingTask(
                task_id=task_id,
                task_type=task_type,
                aisle_id=aisle_id,
                status=PendingTaskStatus.PENDING,
                created_at=datetime.utcnow(),
                timeout_seconds=timeout_seconds or self.default_timeout
            )
            self._pending_tasks[task_id] = task
            return task
    
    def confirm_task(self, task_id: str) -> bool:
        """确认任务已开始执行（收到EXECUTING反馈）"""
        with self._lock:
            if task_id in self._pending_tasks:
                task = self._pending_tasks[task_id]
                task.status = PendingTaskStatus.CONFIRMED
                task.confirmed_at = datetime.utcnow()
                self._confirmed_tasks.add(task_id)
                return True
            return False
    
    def complete_task(self, task_id: str) -> bool:
        """标记任务完成"""
        with self._lock:
            if task_id in self._pending_tasks:
                task = self._pending_tasks[task_id]
                task.status = PendingTaskStatus.COMPLETED
                task.completed_at = datetime.utcnow()
                self._confirmed_tasks.discard(task_id)
                # 从pending中移除已完成的任务
                del self._pending_tasks[task_id]
                return True
            return False
    
    def fail_task(self, task_id: str) -> bool:
        """标记任务失败"""
        with self._lock:
            if task_id in self._pending_tasks:
                task = self._pending_tasks[task_id]
                task.status = PendingTaskStatus.FAILED
                task.completed_at = datetime.utcnow()
                self._confirmed_tasks.discard(task_id)
                del self._pending_tasks[task_id]
                return True
            return False
    
    def get_task(self, task_id: str) -> Optional[PendingTask]:
        """获取任务信息"""
        with self._lock:
            return self._pending_tasks.get(task_id)
    
    def has_unconfirmed_tasks(self) -> bool:
        """检查是否有未确认的任务"""
        with self._lock:
            return any(
                task.status == PendingTaskStatus.PENDING
                for task in self._pending_tasks.values()
            )
    
    def get_unconfirmed_tasks(self) -> Dict[str, PendingTask]:
        """获取所有未确认的任务"""
        with self._lock:
            return {
                task_id: task 
                for task_id, task in self._pending_tasks.items()
                if task.status == PendingTaskStatus.PENDING
            }
    
    def get_all_pending_tasks(self) -> Dict[str, PendingTask]:
        """获取所有待处理任务（包括已确认但未完成的）"""
        with self._lock:
            return self._pending_tasks.copy()
    
    def check_and_timeout_tasks(self) -> list:
        """检查并标记超时的任务，返回超时的任务ID列表"""
        with self._lock:
            timeout_tasks = []
            for task_id, task in list(self._pending_tasks.items()):
                if task.is_timeout():
                    task.status = PendingTaskStatus.TIMEOUT
                    timeout_tasks.append(task_id)
            return timeout_tasks
    
    def can_accept_new_task(self, aisle_id: Optional[str] = None) -> bool:
        """
        检查是否可以接受新任务
        如果指定了巷道，检查该巷道是否有未确认的任务
        如果未指定巷道，检查是否有任何未确认的任务
        """
        with self._lock:
            if aisle_id:
                # 检查指定巷道
                for task in self._pending_tasks.values():
                    if task.aisle_id == aisle_id and task.status == PendingTaskStatus.PENDING:
                        return False
                return True
            else:
                # 全局检查
                return not self.has_unconfirmed_tasks()
    
    def is_task_confirmed(self, task_id: str) -> bool:
        """检查任务是否已确认"""
        with self._lock:
            return task_id in self._confirmed_tasks
    
    def clear_all(self):
        """清除所有状态"""
        with self._lock:
            self._pending_tasks.clear()
            self._confirmed_tasks.clear()


# 全局单例实例
_task_state_manager: Optional[TaskStateManager] = None


def get_task_state_manager() -> TaskStateManager:
    """获取全局任务状态管理器实例"""
    global _task_state_manager
    if _task_state_manager is None:
        _task_state_manager = TaskStateManager()
    return _task_state_manager


def reset_task_state_manager():
    """重置全局任务状态管理器（用于测试）"""
    global _task_state_manager
    if _task_state_manager is not None:
        _task_state_manager.clear_all()
    _task_state_manager = TaskStateManager()

