"""
运行每日仿真并自动将终端输出保存到日志文件。

使用方式：
    python run_daily.py

默认会遍历 simulation/data/daily 目录下的所有配置文件，并运行仿真，
并将输出写入 logs/daily_run_YYYYMMDD_HHMMSS.txt。
"""

import subprocess
import sys
import datetime
from pathlib import Path
import re
import argparse
from typing import Any, Optional, Iterable
from dataclasses import dataclass

# 文件内日期筛选配置（可选）。
# 设置 SELECT_DATES 可覆盖命令行日期，例如 ["20251012", "20251017"]。
# 当 SELECT_DATES 为空时，可通过 START_DATE/END_DATE 指定日期范围。
SELECT_DATES = None
START_DATE = "20251012"
END_DATE = "20251030"
# 泊松参数使用 lambda = 1 / POISSON_X；日志后缀记录 POISSON_X。
# e.g. 50, 70, 100, 120
POISSON_X = 50

# ============================================================================
# 辅助函数：日期筛选与日志文件命名
# ============================================================================
def get_date_and_tag(filename):
    """Extract date and optional tag from filename.
    Example:
      inbound_task_config_20251012.json -> ("20251012", "default")
      inbound_task_config_20251012_v2.json -> ("20251012", "v2")

    Args:
        filename (Any): 输入或输出文件名。

    Returns:
        Any: 当前处理流程产生的结果；具体结构由函数摘要说明。
    """
    stem = Path(filename).stem
    match = re.search(r"(\d{8})(?:[_-]([A-Za-z0-9]+))?$", stem)
    if not match:
        return None, None
    date_str = match.group(1)
    tag = match.group(2) or "default"
    return date_str, tag

def _parse_date_list(dates_value: Optional[str]) -> Optional[set]:
    """解析原始输入，提取本模块后续判断所需的结构化字段。

    输入：dates_value（Optional[str]）
    输出：Optional[set]

    Args:
        dates_value (Optional[str]): 供当前处理流程使用的 `dates_value` 值。

    Returns:
        Optional[set]: 当前处理流程产生的结果；具体结构由函数摘要说明。
    """
    if not dates_value:
        return None
    parts = re.split(r"[,\s]+", dates_value.strip())
    return {p for p in parts if p}

def _filter_dates(all_dates: Iterable[str], dates_value: Optional[str],
                  start_date: Optional[str], end_date: Optional[str]) -> list:
    """执行 `_filter_dates` 对应的模块处理步骤，并返回该步骤产生的结果。

    输入：all_dates（Iterable[str]）、dates_value（Optional[str]）、start_date（Optional[str]）、end_date（Optional[str]）
    输出：list

    Args:
        all_dates (Iterable[str]): 供当前处理流程使用的 `all_dates` 值。
        dates_value (Optional[str]): 供当前处理流程使用的 `dates_value` 值。
        start_date (Optional[str]): 供当前处理流程使用的 `start_date` 值。
        end_date (Optional[str]): 供当前处理流程使用的 `end_date` 值。

    Returns:
        list: 符合当前筛选或计算条件的结果集合。
    """
    date_set = _parse_date_list(dates_value)
    filtered = []
    for d in sorted(all_dates):
        if date_set is not None and d not in date_set:
            continue
        if start_date and d < start_date:
            continue
        if end_date and d > end_date:
            continue
        filtered.append(d)
    return filtered

def _format_lambda_suffix(value: Optional[float]) -> str:
    """将内部数据格式化为日志、展示或接口输出所需的形式。

    输入：value（Optional[float]）
    输出：str

    Args:
        value (Optional[float]): 待解析、格式化或计算的原始值。

    Returns:
        str: 当前处理得到的文本标识、格式化结果或诊断信息。
    """
    if value is None:
        return ""
    text = f"{value}".replace(".", "p")
    return f"-lam{text}"


@dataclass
class RunningJob:
    label: str
    cmd: list
    log_file: Path
    proc: subprocess.Popen
    file_handle: Any

# ============================================================================
# 主流程：逐日构造命令并启动仿真子进程
# ============================================================================
def main():
    """作为脚本入口，解析运行参数并按既定顺序调用本文件的主要处理流程。

    输入：无显式业务输入；依赖实例字段或模块配置。

    Args:
        None: 无显式业务参数；使用实例状态或模块配置。

    Returns:
        None: 通过实例状态、队列或外部副作用完成处理。
    """
    parser = argparse.ArgumentParser(description="Run daily configs in batch.")
    # 仅运行逗号或空格分隔的日期集合；传入后优先级高于起止日期。
    parser.add_argument(
        "--dates",
        help="Comma/space separated list of YYYYMMDD to run (e.g. 20251012,20251013)",
    )
    # 批处理日期范围的包含起点，仅在未指定 --dates 时参与筛选。
    parser.add_argument(
        "--start-date",
        help="Start date YYYYMMDD (inclusive). Ignored if --dates is set.",
    )
    # 批处理日期范围的包含终点，仅在未指定 --dates 时参与筛选。
    parser.add_argument(
        "--end-date",
        help="End date YYYYMMDD (inclusive). Ignored if --dates is set.",
    )
    args = parser.parse_args()
    # 获取daily目录下的所有配置文件
    daily_dir = Path("simulation/data/daily")

    if not daily_dir.exists():
        print(f"[ERROR] 目录 {daily_dir} 不存在")
        return

    # 获取所有inbound配置文件
    inbound_files = list(daily_dir.glob("inbound_task_config_*.json"))
    # 获取所有plan配置文件
    plan_files = list(daily_dir.glob("production_plan_config_*.json"))

    # 按日期匹配inbound和plan配置
    date_configs = {}

    for inbound_file in inbound_files:
        date_str, tag = get_date_and_tag(str(inbound_file))
        if date_str:
            if date_str not in date_configs:
                date_configs[date_str] = {"inbound": {}, "plan": {}}
            date_configs[date_str]["inbound"][tag] = str(inbound_file)

    for plan_file in plan_files:
        date_str, tag = get_date_and_tag(str(plan_file))
        if date_str:
            if date_str not in date_configs:
                date_configs[date_str] = {"inbound": {}, "plan": {}}
            date_configs[date_str]["plan"][tag] = str(plan_file)

    print(f"[INFO] 找到 {len(date_configs)} 个日期的配置文件")
    print(f"[INFO] 日期列表: {sorted(date_configs.keys())}")

    all_dates = sorted(date_configs.keys())
    dates_value = ",".join(SELECT_DATES) if SELECT_DATES else args.dates
    start_date = None if SELECT_DATES else (START_DATE or args.start_date)
    end_date = None if SELECT_DATES else (END_DATE or args.end_date)
    selected_dates = _filter_dates(all_dates, dates_value, start_date, end_date)
    if dates_value or start_date or end_date:
        print(f"[INFO] : {selected_dates}")
    if not selected_dates:
        print("[WARN] No matching dates to run.")
        return

    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)

    # 运行配置
    run_configs = [
        ("proposed", "proposed", "heuristic"),
        ("proposed", "proposed", "optimization"),
        ("baseline", "baseline", "heuristic"),
        ("baseline", "baseline", "optimization"),
    ]
    # 生成日志文件名
    abbreviations = {
        "baseline": "base",
        "proposed": "prop",
        "heuristic": "heu",
        "optimization": "opt",
    }

    # 按日期运行仿真
    successful_runs = 0
    failed_runs = 0

    for date_str in selected_dates:
        configs = date_configs[date_str]
        inbound_map = configs.get("inbound", {})
        plan_map = configs.get("plan", {})
        tags = sorted(set(inbound_map.keys()) & set(plan_map.keys()))

        if not tags:
            print(f"[WARNING] Missing config pairs for date {date_str}, skip")
            if not inbound_map:
                print("  - Missing inbound configs")
            if not plan_map:
                print("  - Missing plan configs")
            missing_inbound = sorted(set(plan_map.keys()) - set(inbound_map.keys()))
            missing_plan = sorted(set(inbound_map.keys()) - set(plan_map.keys()))
            if missing_inbound:
                print(f"  - Missing inbound tags: {missing_inbound}")
            if missing_plan:
                print(f"  - Missing plan tags: {missing_plan}")
            continue

        for tag in tags:
            inbound_path = inbound_map[tag]
            plan_path = plan_map[tag]
            tag_suffix = "" if tag == "default" else f"_{tag}"
            day_folder_name = f"{date_str}{tag_suffix}"
            day_log_dir = logs_dir / "daily" / day_folder_name
            day_log_dir.mkdir(parents=True, exist_ok=True)

            print("\n" + "=" * 80)
            print(f"[INFO] Running date {date_str}{tag_suffix}..")
            print("=" * 80)

            running_jobs = []
            for i, (allocation, position, scheduler) in enumerate(run_configs, 1):
                print(f"[INFO] Config {i}/4: {allocation}-{position}-{scheduler}")

                allocation_abbr = abbreviations.get(allocation, allocation)
                position_abbr = abbreviations.get(position, position)
                scheduler_abbr = abbreviations.get(scheduler, scheduler)
                label = f"{allocation_abbr}-{position_abbr}-{scheduler_abbr}"
                lambda_suffix = _format_lambda_suffix(POISSON_X)
                log_filename = f"{label}{lambda_suffix}.txt"
                log_file = day_log_dir / log_filename

                cmd = [
                    "python", "run.py",
                    "--date-str", date_str,
                    "--no-cutoff",
                    "--inbound-config", inbound_path,
                    "--plan-config", plan_path,
                    "--inbound-allocation-strategy", allocation,
                    "--inbound-position-strategy", position,
                    "--scheduler-type", scheduler,
                ]
                if POISSON_X is not None:
                    cmd.extend(["--inbound-rate-lambda", str(1 / POISSON_X)])

                print("[INFO] Command: " + " ".join(cmd))
                print(f"[INFO] Log file: {log_file}")

                try:
                    fh = open(log_file, "w", encoding="utf-8")
                    proc = subprocess.Popen(
                        cmd,
                        stdout=fh,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="gbk",
                        errors="replace",
                    )
                    running_jobs.append(
                        RunningJob(
                            label=label,
                            cmd=cmd,
                            log_file=log_file,
                            proc=proc,
                            file_handle=fh,
                        )
                    )
                except Exception as e:
                    print(f"[ERROR] Failed to start {label}: {e}")
                    failed_runs += 1

# 等待本批次的 4 个配置并行执行完成。
            for job in running_jobs:
                try:
                    return_code = job.proc.wait()
                    if return_code == 0:
                        print(f"[INFO] Date {date_str}{tag_suffix} config {job.label} completed")
                        successful_runs += 1
                    else:
                        print(
                            f"[ERROR] Date {date_str}{tag_suffix} config {job.label} failed "
                            f"(exit={return_code}), log={job.log_file}"
                        )
                        failed_runs += 1
                except Exception as e:
                    print(f"[ERROR] Failed while waiting {job.label}: {e}")
                    failed_runs += 1
                finally:
                    try:
                        job.file_handle.close()
                    except Exception:
                        pass

    print(f"\n{'='*60}")
    print("[INFO] 所有日期仿真运行完成")
    print(f"[INFO] 成功: {successful_runs}, 失败: {failed_runs}")
    print(f"[INFO] 总计: {successful_runs + failed_runs} 天")
    print(f"[INFO] 成功率: {successful_runs/(successful_runs + failed_runs)*100:.2f}%" if (successful_runs + failed_runs) > 0 else "[INFO] 成功率: 0%")

if __name__ == "__main__":
    main()
