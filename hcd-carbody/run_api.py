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


def main():
    parser = argparse.ArgumentParser(description="仓库调度系统 API 服务")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址 (默认: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="监听端口 (默认: 8000)")
    parser.add_argument("--reload", action="store_true", help="开发模式（热重载）")
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
    )


if __name__ == "__main__":
    main()
