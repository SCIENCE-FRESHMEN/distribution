"""
FastAPI应用入口
仓库调度系统API服务
"""

from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .routes import schedule_router, feedback_router, inbound_router, plan_router
from .services.warehouse_service import init_warehouse_service, get_warehouse_service
from .state import reset_task_state_manager
from .response import ok, fail

# API版本
API_VERSION = "1.0.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时初始化
    print("[API] 初始化仓库调度服务...")
    init_warehouse_service()
    reset_task_state_manager()
    print("[API] 服务初始化完成")
    
    yield
    
    # 关闭时清理
    print("[API] 关闭仓库调度服务...")


# 创建FastAPI应用
app = FastAPI(
    title="仓库调度系统 API",
    description="""
## 仓库调度系统 API 服务

提供以下功能接口：

### 1. 混合调度 (/api/v1/schedule/mixed)
- 为入库和出库任务进行统一调度
- 返回巷道分配结果

### 2. 任务执行反馈 (/api/v1/task/feedback)
- 接收外部系统的任务执行状态反馈
- 状态包括：EXECUTING（执行中）、COMPLETED（已完成）、FAILED（失败）

### 3. 入库分配 (/api/v1/inbound/allocate)
- 为入库任务分配推荐的目标巷道

### 4. 生产计划 (/api/v1/plan/production)
- 设置或更新当日生产计划

### 常用测试数据 (Examples)
为了方便测试，可以使用以下真实的 SKU ID（基于库存）：
- **产线1 配对 SKU**: `2801021-H19H0` (左), `2801037-H19H0` (右)
- **产线2 配对 SKU**: `2801022-H17F4` (左), `2801038-H17F4` (右)
- **巷道 ID**: `1`, `2`, `3`, `4`, `5`
- **产线 ID**: `1`, `2`, `3`

### 注意事项
- 每次发出调度指令后，必须等待外部系统返回 **EXECUTING** 状态的反馈
- 只有收到确认后才能处理下一个调度请求（否则返回 409 冲突）
""",
    version=API_VERSION,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# 添加CORS中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# 注册路由
app.include_router(schedule_router, prefix="/api/v1")
app.include_router(feedback_router, prefix="/api/v1")
app.include_router(inbound_router, prefix="/api/v1")
app.include_router(plan_router, prefix="/api/v1")


# ============================================================
# 全局异常处理
# ============================================================

@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    return fail(
        message=str(exc.detail),
        http_status=exc.status_code,
        data={"timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")},
        status_code="FAILED",
    )


@app.exception_handler(Exception)
async def general_exception_handler(request, exc):
    return fail(
        message="内部服务器错误",
        http_status=500,
        data={
            "detail": str(exc),
            "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        status_code="FAILED",
    )


@app.get("/", tags=["基础"])
async def root():
    return ok(
        status_code="SUCCESS",
        message="ok",
        data={
            "service": "仓库调度系统 API",
            "version": API_VERSION,
            "docs": "/docs",
            "redoc": "/redoc",
        },
    )


@app.get("/api/v1/status", tags=["基础"])
async def system_status():
    try:
        service = get_warehouse_service()
        return ok(
            status_code="SUCCESS",
            message="ok",
            data={
                "service_status": "running",
                "current_time": service._get_current_time(),
                "running_tasks_count": len(service.get_running_tasks()),
                "completed_tasks_count": len(service.get_completed_tasks()),
                "aisle_status": service.get_aisle_status(),
                "inventory_summary": service.get_inventory_summary(),
                "inventory": service.get_full_inventory(),
            },
        )
    except Exception as e:
        return fail(
            message="error",
            http_status=500,
            data={"detail": str(e)},
            status_code="FAILED",
        )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "api.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info",
    )
