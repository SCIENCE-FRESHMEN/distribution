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
from typing import Optional, Iterable

# In-file date selection (optional).
# Set SELECT_DATES to override CLI, e.g. ["20251012", "20251017"].
# Or set START_DATE/END_DATE for a range when SELECT_DATES is empty.
SELECT_DATES = None
START_DATE = "20251012"
END_DATE = "20251030"
# Use lambda = 1 / POISSON_X. Log suffix uses POISSON_X.
POISSON_X = 100  # e.g. 50, 70, 100, 120

def get_date_and_tag(filename):
    """
    Extract date and optional tag from filename.
    Example:
      inbound_task_config_20251012.json -> ("20251012", "default")
      inbound_task_config_20251012_v2.json -> ("20251012", "v2")
    """
    stem = Path(filename).stem
    match = re.search(r"(\d{8})(?:[_-]([A-Za-z0-9]+))?$", stem)
    if not match:
        return None, None
    date_str = match.group(1)
    tag = match.group(2) or "default"
    return date_str, tag

def _parse_date_list(dates_value: Optional[str]) -> Optional[set]:
    if not dates_value:
        return None
    parts = re.split(r"[,\s]+", dates_value.strip())
    return {p for p in parts if p}

def _filter_dates(all_dates: Iterable[str], dates_value: Optional[str],
                  start_date: Optional[str], end_date: Optional[str]) -> list:
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
    if value is None:
        return ""
    text = f"{value}".replace(".", "p")
    return f"-lam{text}"

def main():
    parser = argparse.ArgumentParser(description="Run daily configs in batch.")
    parser.add_argument(
        "--dates",
        help="Comma/space separated list of YYYYMMDD to run (e.g. 20251012,20251013)",
    )
    parser.add_argument(
        "--start-date",
        help="Start date YYYYMMDD (inclusive). Ignored if --dates is set.",
    )
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

    run_configs = [
        ("proposed", "proposed", "heuristic"),
        ("proposed", "proposed", "optimization"),
        ("baseline", "baseline", "heuristic"),
        ("baseline", "baseline", "optimization"),
    ]
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

            for i, (allocation, position, scheduler) in enumerate(run_configs, 1):
                print(f"[INFO] Config {i}/4: {allocation}-{position}-{scheduler}")

                allocation_abbr = abbreviations.get(allocation, allocation)
                position_abbr = abbreviations.get(position, position)
                scheduler_abbr = abbreviations.get(scheduler, scheduler)
                lambda_suffix = _format_lambda_suffix(POISSON_X)
                log_filename = f"{allocation_abbr}-{position_abbr}-{scheduler_abbr}{lambda_suffix}.txt"
                log_file = day_log_dir / log_filename

                # Execute run.py
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
                    with open(log_file, "w", encoding="utf-8") as f:
                        # Write output to both terminal and file
                        proc = subprocess.Popen(
                            cmd,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,  # Merge stderr into stdout
                            text=True,
                            encoding="gbk",
                            errors="replace",
                        )
                        assert proc.stdout is not None
                        for line in proc.stdout:
                            sys.stdout.write(line)
                            f.write(line)
                        proc.wait()

                    print(f"[INFO] Date {date_str}{tag_suffix} config {allocation_abbr}-{position_abbr}-{scheduler_abbr} completed")
                    successful_runs += 1
                except Exception as e:
                    print(f"[ERROR] Failed date {date_str}{tag_suffix} config {allocation}-{position}-{scheduler}: {e}")
                    failed_runs += 1

    print(f"\n{'='*60}")
    print("[INFO] 所有日期仿真运行完成")
    print(f"[INFO] 成功: {successful_runs}, 失败: {failed_runs}")
    print(f"[INFO] 总计: {successful_runs + failed_runs} 天")
    print(f"[INFO] 成功率: {successful_runs/(successful_runs + failed_runs)*100:.2f}%" if (successful_runs + failed_runs) > 0 else "[INFO] 成功率: 0%")

if __name__ == "__main__":
    main()
