"""
API路由模块
"""

from .schedule import router as schedule_router
from .feedback import router as feedback_router
from .inbound import router as inbound_router
from .bom import router as bom_router

__all__ = [
    "schedule_router",
    "feedback_router", 
    "inbound_router",
    "bom_router"
]

