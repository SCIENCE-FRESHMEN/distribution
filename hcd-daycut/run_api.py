#!/usr/bin/env python
"""
仓库调度系统 API 启动脚本

使用方法：
    python run_api.py [--host HOST] [--port PORT] [--reload]

示例：
    python run_api.py                    # 默认启动 (0.0.0.0:8000)
    python run_api.py --port 8080        # 指定端口
    python run_api.py --reload           # 开发模式（热重载）
"""

import argparse
import uvicorn

from api.logging_setup import create_uvicorn_log_config


def main():
    """作为脚本入口，解析运行参数并按既定顺序调用本文件的主要处理流程。

    输入：无显式业务输入；依赖实例字段或模块配置。

    Args:
        None: 无显式业务参数；使用实例状态或模块配置。

    Returns:
        None: 通过实例状态、队列或外部副作用完成处理。
    """
    parser = argparse.ArgumentParser(description="仓库调度系统 API 服务")
    # Uvicorn 绑定的监听地址；0.0.0.0 允许局域网或容器外部访问。
    parser.add_argument("--host", default="0.0.0.0", help="监听地址 (默认: 0.0.0.0)")
    # Uvicorn 监听端口。
    parser.add_argument("--port", type=int, default=8000, help="监听端口 (默认: 8000)")
    # 开发时监控源码变更并自动重启服务进程。
    parser.add_argument("--reload", action="store_true", help="开发模式（热重载）")
    # 服务工作进程数量；多进程时需注意各进程内存状态互不共享。
    parser.add_argument("--workers", type=int, default=1, help="工作进程数 (默认: 1)")

    args = parser.parse_args()

    print("=" * 60)
    print("仓库调度系统 API 服务")
    print("=" * 60)
    print(f"地址: http://{args.host}:{args.port}")
    print(f"文档: http://{args.host}:{args.port}/docs")
    print(f"模式: {'开发' if args.reload else '生产'}")
    print("=" * 60)

    uvicorn.run(
        "api.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        workers=args.workers if not args.reload else 1,
        log_level="info",
        log_config=create_uvicorn_log_config(),
    )


if __name__ == "__main__":
    main()

