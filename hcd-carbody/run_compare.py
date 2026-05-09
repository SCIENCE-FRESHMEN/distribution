"""
仓库仿真对比运行脚本 - 用于批量运行不同策略组合的仿真并保存日志

此脚本可以并行运行多种策略组合的仓库仿真，包括不同的入库分配策略、入库位置策略和调度器类型，
并将每种组合的运行结果保存到单独的日志文件中。

使用方法:
    python run_compare.py
    python run_compare.py --jobs 4

默认情况下，脚本会运行四种策略组合（proposed/baseline x heuristic/optimization）
并将日志保存到 logs/{allocation}-{position}-{scheduler}.txt，例如:
    logs/prop-prop-heu.txt
    logs/prop-prop-opt.txt
    logs/base-base-heu.txt
    logs/base-base-opt.txt
"""

import argparse
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Tuple

# 定义Windows文件名非法字符的正则表达式
_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1F]')


def sanitize_filename(name: str) -> str:
    """为Windows/NTFS安全的文件名进行清理和替换
    
    将文件名中的非法字符替换为下划线，确保文件名在Windows系统上可以正常创建
    
    Args:
        name: 原始文件名
        
    Returns:
        清理后的文件名
    """
    name = _INVALID_FILENAME_CHARS.sub("_", name).strip(" .")
    return name or "log"


def _decode_bytes(raw: bytes) -> str:
    """尝试使用不同编码解码字节串，优先尝试UTF-8和GBK编码
    
    Args:
        raw: 原始字节串
        
    Returns:
        解码后的字符串
    """
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except Exception:
            pass
    return raw.decode("utf-8", errors="ignore")


def _run_one(
    idx: int,
    total: int,
    cfg: Tuple[str, str, str],
    logs_dir: Path,
    abbreviations: dict,
) -> Tuple[int, Tuple[str, str, str], Path, int, str | None]:
    """运行单个仿真配置
    
    Args:
        idx: 当前配置的索引
        total: 总配置数
        cfg: 配置元组，包含(allocation, position, scheduler)
        logs_dir: 日志目录路径
        abbreviations: 策略名称缩写映射表
        
    Returns:
        包含运行结果的元组(idx, 配置, 日志文件路径, 返回码, 错误信息)
    """
    allocation, position, scheduler = cfg
    # 获取策略名称的缩写
    allocation_abbr = abbreviations.get(allocation, allocation)
    position_abbr = abbreviations.get(position, position)
    scheduler_abbr = abbreviations.get(scheduler, scheduler)

    # 构造日志文件名
    log_filename = sanitize_filename(f"{allocation_abbr}-{position_abbr}-{scheduler_abbr}.txt")
    log_file = logs_dir / log_filename

    # run.py内部也会记录到--log-file；并行运行时为每个配置创建唯一日志
    internal_log = logs_dir / sanitize_filename(f"run-{allocation_abbr}-{position_abbr}-{scheduler_abbr}.log")

    # 构造运行run.py的命令
    cmd = [
        "python", "run.py",
        "--inbound-allocation-strategy", allocation,      # 入库巷道分配策略
        "--inbound-position-strategy", position,          # 入库货位分配策略
        "--scheduler-type", scheduler,                    # 调度器类型
        "--log-file", str(internal_log),                  # 内部日志文件路径
    ]

    print(f"[INFO] submit {idx}/{total}: {allocation}-{position}-{scheduler} -> {log_file}")
    try:
        # 执行子进程并捕获输出
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=False)
        # 解码输出文本
        text = _decode_bytes(proc.stdout or b"")
        # 将输出写入日志文件
        log_file.write_text(text, encoding="utf-8", errors="ignore")
        return idx, cfg, log_file, proc.returncode, None
    except Exception as e:
        # 返回错误信息
        return idx, cfg, log_file, -1, str(e)


def main() -> None:
    """主函数，解析命令行参数并运行对比实验"""
    parser = argparse.ArgumentParser(description="并行运行多种配置对比实验")
    parser.add_argument("--jobs", type=int, default=0, help="并行工作进程数；0表示自动检测")
    parser.add_argument("--logs-subdir", type=str, default="", help="输出日志子目录名")
    parser.add_argument(
        "--keep-internal-logs",
        action="store_true",
        help="保留临时日志文件如 logs/run-*.log (默认: 运行结束后删除)",
    )
    args = parser.parse_args()

    # 定义要测试的策略组合
    # 每个元组包含: (入库巷道分配策略, 入库货位分配策略, 调度器类型)
    configs = [
        ("proposed", "proposed", "heuristic"),      # 提出的方法 + 提出的方法 + 启发式调度
        ("proposed", "proposed", "optimization"),   # 提出的方法 + 提出的方法 + 优化调度
        ("baseline", "baseline", "heuristic"),      # 基线方法 + 基线方法 + 启发式调度
        ("baseline", "baseline", "optimization"),   # 基线方法 + 基线方法 + 优化调度
    ]

    # 创建日志目录
    logs_dir = Path("logs")
    if args.logs_subdir:
        logs_dir = logs_dir / sanitize_filename(args.logs_subdir)
    logs_dir.mkdir(parents=True, exist_ok=True)

    # 定义策略名称的缩写映射，用于构建日志文件名
    abbreviations = {
        "baseline": "base",          # 基线方法的缩写
        "proposed": "prop",          # 提出方法的缩写
        "heuristic": "heu",          # 启发式调度的缩写
        "optimization": "opt",       # 优化调度的缩写
    }

    # 自动计算并行任务数：取配置总数和CPU核心数的最小值
    auto_jobs = min(len(configs), max(1, (os.cpu_count() or 4)))
    # 如果用户设置了--jobs参数且大于0，则使用用户指定的值，否则使用自动检测的值
    jobs = auto_jobs if args.jobs <= 0 else max(1, args.jobs)
    print(f"[INFO] total configs: {len(configs)}, parallel jobs: {jobs}")

    failures = 0
    internal_logs: list[Path] = []
    
    # 使用线程池并行执行所有配置的仿真
    with ThreadPoolExecutor(max_workers=jobs) as ex:
        # 提交所有任务到线程池
        futures = {
            ex.submit(_run_one, i, len(configs), cfg, logs_dir, abbreviations): (i, cfg)
            for i, cfg in enumerate(configs, 1)
        }
        # 等待任务完成并处理结果
        for fut in as_completed(futures):
            idx, cfg = futures[fut]
            i, cfg2, log_file, code, err = fut.result()
            allocation, position, scheduler = cfg2
            name = f"{allocation}-{position}-{scheduler}"
            
            # 根据返回码判断任务是否成功
            if code == 0:
                print(f"[INFO] done {i}/{len(configs)}: {name}, log={log_file}")
            else:
                failures += 1
                if err:
                    print(f"[ERROR] failed {i}/{len(configs)}: {name}, err={err}")
                else:
                    print(f"[ERROR] failed {i}/{len(configs)}: {name}, returncode={code}, log={log_file}")
            
            # 记录内部日志文件路径，用于后续清理
            allocation_abbr = abbreviations.get(allocation, allocation)
            position_abbr = abbreviations.get(position, position)
            scheduler_abbr = abbreviations.get(scheduler, scheduler)
            internal_logs.append(logs_dir / sanitize_filename(f"run-{allocation_abbr}-{position_abbr}-{scheduler_abbr}.log"))

    # 输出最终统计信息
    if failures:
        print(f"[INFO] all configs finished, failures={failures}")
    else:
        print("[INFO] all configs finished, all success")

    # 如果不需要保留内部日志，则删除临时日志文件
    if not args.keep_internal_logs:
        removed = 0
        # 删除去重后的内部日志文件
        for p in set(internal_logs):
            try:
                if p.exists():
                    p.unlink()  # 删除文件
                    removed += 1
            except Exception as e:
                print(f"[WARN] failed to remove temp log {p}: {e}")
        if removed:
            print(f"[INFO] removed {removed} temporary run-*.log files")


if __name__ == "__main__":
    main()
