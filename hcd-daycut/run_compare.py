"""
并发运行多组策略配置，并将每组输出分别写入日志文件。

默认同时启动 4 个线程，对 4 组策略同时执行：
    1. proposed-proposed-heuristic
    2. proposed-proposed-optimization
    3. baseline-baseline-heuristic
    4. baseline-baseline-optimization
"""

import argparse
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


PRINT_LOCK = threading.Lock()

CONFIGS = [
    ("proposed", "proposed", "heuristic"),
    ("proposed", "proposed", "optimization"),
    ("baseline", "baseline", "heuristic"),
    ("baseline", "baseline", "optimization"),
]

ABBREVIATIONS = {
    "baseline": "base",
    "proposed": "prop",
    "heuristic": "heu",
    "optimization": "opt",
}


def build_log_filename(allocation: str, position: str, scheduler: str) -> str:
    allocation_abbr = ABBREVIATIONS.get(allocation, allocation)
    position_abbr = ABBREVIATIONS.get(position, position)
    scheduler_abbr = ABBREVIATIONS.get(scheduler, scheduler)
    return f"{allocation_abbr}-{position_abbr}-{scheduler_abbr}.txt"


def run_config(index: int, total: int, allocation: str, position: str, scheduler: str, logs_dir: Path) -> dict:
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

    with PRINT_LOCK:
        print(f"[INFO] Starting {index}/{total}: {label}")
        print(f"[INFO] Logging to: {log_file}")

    try:
        with log_file.open("w", encoding="utf-8") as f:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
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


def main(max_workers: int = 4) -> int:
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)

    max_workers = max(1, min(max_workers, len(CONFIGS)))
    print(f"[INFO] Running {len(CONFIGS)} configs with {max_workers} worker threads")

    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(run_config, i, len(CONFIGS), allocation, position, scheduler, logs_dir)
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
    parser.add_argument("--max-workers", type=int, default=4, help="Number of worker threads, default is 4")
    args = parser.parse_args()
    raise SystemExit(main(max_workers=args.max_workers))
