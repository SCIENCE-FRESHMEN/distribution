"""
并发运行多组策略配置，并将每组输出分别写入日志文件。

默认同时启动 4 个线程，对 4 组策略同时执行：
    1. proposed-proposed-heuristic
    2. proposed-proposed-optimization
    3. baseline-baseline-heuristic
    4. baseline-baseline-optimization
"""

import argparse
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


PRINT_LOCK = threading.Lock()

# 配置参数（要跑的策略组合：巷道选择策略，货位选择策略，调度策略）
CONFIGS = [
    ("proposed", "proposed", "heuristic"),
    ("proposed", "proposed", "optimization"),
    ("baseline", "baseline", "heuristic"),
    ("baseline", "baseline", "optimization"),
]

# 参数缩写，映射到日志文件名
ABBREVIATIONS = {
    "baseline": "base",
    "proposed": "prop",
    "heuristic": "heu",
    "optimization": "opt",
}


def build_log_filename(allocation: str, position: str, scheduler: str) -> str:
    """构造下游调用所需的对象、请求载荷或配置结果。

    输入：allocation（str）、position（str）、scheduler（str）
    输出：str

    Args:
        allocation (str): 供当前处理流程使用的 `allocation` 值。
        position (str): 供当前处理流程使用的 `position` 值。
        scheduler (str): 供当前处理流程使用的 `scheduler` 值。

    Returns:
        str: 当前处理得到的文本标识、格式化结果或诊断信息。
    """
    allocation_abbr = ABBREVIATIONS.get(allocation, allocation)
    position_abbr = ABBREVIATIONS.get(position, position)
    scheduler_abbr = ABBREVIATIONS.get(scheduler, scheduler)
    return f"{allocation_abbr}-{position_abbr}-{scheduler_abbr}.txt"


def run_config(
    index: int,
    total: int,
    allocation: str,
    position: str,
    scheduler: str,
    logs_dir: Path,
    warehouse_config: str | None = None,
    inbound_config: str | None = None,
    plan_config: str | None = None,
) -> dict:
    """执行 `run_config` 对应的模块处理步骤，并返回该步骤产生的结果。

    输入：index（int）、total（int）、allocation（str）、position（str）、scheduler（str）、logs_dir（Path）、warehouse_config（str | None，可选参数）、inbound_config（str | None，可选参数）、plan_config（str | None，可选参数）
    输出：dict

    Args:
        index (int): 供当前处理流程使用的 `index` 值。
        total (int): 供当前处理流程使用的 `total` 值。
        allocation (str): 供当前处理流程使用的 `allocation` 值。
        position (str): 供当前处理流程使用的 `position` 值。
        scheduler (str): 供当前处理流程使用的 `scheduler` 值。
        logs_dir (Path): 供当前处理流程使用的 `logs_dir` 值。
        warehouse_config (str | None，可选): 供当前处理流程使用的 `warehouse_config` 值。
        inbound_config (str | None，可选): 供当前处理流程使用的 `inbound_config` 值。
        plan_config (str | None，可选): 供当前处理流程使用的 `plan_config` 值。

    Returns:
        dict: 按函数约定字段组织的计算结果映射。
    """
    label = f"{allocation}-{position}-{scheduler}"
    log_file = logs_dir / build_log_filename(allocation, position, scheduler)
    cmd = [
        sys.executable,
        "run.py",
        "--inbound-allocation-strategy",
        allocation,
        "--inbound-position-strategy",
        position,
        "--scheduler-type",
        scheduler,
    ]
    if warehouse_config:
        cmd.extend(["--warehouse-config", warehouse_config])
    if inbound_config:
        cmd.extend(["--inbound-config", inbound_config])
    if plan_config:
        cmd.extend(["--plan-config", plan_config])

    with PRINT_LOCK:
        print(f"[INFO] Starting {index}/{total}: {label}")
        print(f"[INFO] Logging to: {log_file}")

    try:
        # 强制子进程使用 UTF-8 输出，避免按二进制块读取时切断多字节字符导致乱码。
        with log_file.open("w", encoding="utf-8") as f:
            child_env = os.environ.copy()
            child_env["PYTHONIOENCODING"] = "utf-8"
            child_env["PYTHONUTF8"] = "1"
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=child_env,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                f.write(line)
            return_code = proc.wait()
    except Exception as e:
        with PRINT_LOCK:
            print(f"[ERROR] Failed to run {label}: {e}")
        return {
            "label": label,
            "log_file": str(log_file),
            "return_code": -1,
            "error": str(e),
        }

    with PRINT_LOCK:
        if return_code == 0:
            print(f"[INFO] Finished: {label}")
        else:
            print(f"[ERROR] Failed: {label}, exit code={return_code}")

    return {
        "label": label,
        "log_file": str(log_file),
        "return_code": return_code,
    }


def main(
    max_workers: int = 4,
    warehouse_config: str | None = None,
    inbound_config: str | None = None,
    plan_config: str | None = None,
    logs_subdir: str | None = None,
) -> int:
    """作为脚本入口，解析运行参数并按既定顺序调用本文件的主要处理流程。

    输入：max_workers（int，可选参数）、warehouse_config（str | None，可选参数）、inbound_config（str | None，可选参数）、plan_config（str | None，可选参数）、logs_subdir（str | None，可选参数）
    输出：int

    Args:
        max_workers (int，可选): 供当前处理流程使用的 `max_workers` 值。
            warehouse_config (str | None，可选): 供当前处理流程使用的 `warehouse_config` 值。
            inbound_config (str | None，可选): 供当前处理流程使用的 `inbound_config` 值。
            plan_config (str | None，可选): 供当前处理流程使用的 `plan_config` 值。
            logs_subdir (str | None，可选): 当前对比运行在 ``logs`` 下使用的独立子目录名称。

    Returns:
        int: 当前计算得到的数量、索引或编号。
    """
    logs_dir = Path("logs")
    # 对比运行默认不覆盖既有日志；调用方可用时间戳子目录隔离本轮四个策略的输出。
    if logs_subdir:
        logs_dir = logs_dir / Path(logs_subdir).name
    logs_dir.mkdir(exist_ok=True)

    max_workers = max(1, min(max_workers, len(CONFIGS)))
    print(f"[INFO] Running {len(CONFIGS)} configs with {max_workers} worker threads")

    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(
                run_config,
                i,
                len(CONFIGS),
                allocation,
                position,
                scheduler,
                logs_dir,
                warehouse_config,
                inbound_config,
                plan_config,
            )
            for i, (allocation, position, scheduler) in enumerate(CONFIGS, 1)
        ]
        for future in as_completed(futures):
            results.append(future.result())

    failed = [item for item in results if item.get("return_code") != 0]
    if failed:
        print("[ERROR] Some configs failed:")
        for item in failed:
            print(f"  - {item['label']} -> exit={item['return_code']}, log={item['log_file']}")
        return 1

    print("[INFO] All configs finished successfully")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run strategy comparisons in parallel")
    # 策略组合并行运行的最大工作线程数。
    parser.add_argument("--max-workers", type=int, default=4, help="Number of worker threads, default is 4")
    # 透传给 run.py 的仓库配置路径，统一比较场景的仓库约束。
    parser.add_argument("--warehouse-config", type=str, help="Warehouse config path passed through to run.py")
    # 透传给 run.py 的入库任务配置路径。
    parser.add_argument("--inbound-config", type=str, help="Inbound config path passed through to run.py")
    # 透传给 run.py 的生产计划配置路径。
    parser.add_argument("--plan-config", type=str, help="Production plan config path passed through to run.py")
    # 将四种策略日志写入 logs 下独立子目录，避免覆盖历史对比结果。
    parser.add_argument("--logs-subdir", type=str, help="Subdirectory under logs for this comparison run")
    args = parser.parse_args()
    raise SystemExit(
        main(
            max_workers=args.max_workers,
            warehouse_config=args.warehouse_config,
            inbound_config=args.inbound_config,
            plan_config=args.plan_config,
            logs_subdir=args.logs_subdir,
        )
    )
