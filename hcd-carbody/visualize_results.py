"""
可视化脚本：解析日志并输出每日完成任务/完成时间等图表。
"""

import argparse
import re
from pathlib import Path
import matplotlib.pyplot as plt
import numpy as np


if plt is not None:
    plt.style.use("seaborn-v0_8-whitegrid")
    plt.rcParams.update(
        {
            "font.sans-serif": ["SimHei", "Arial Unicode MS", "DejaVu Sans"],
            "axes.unicode_minus": False,
            "font.size": 12,
            "axes.titlesize": 16,
            "axes.labelsize": 13,
            "xtick.labelsize": 11,
            "ytick.labelsize": 11,
            "legend.fontsize": 11,
        }
    )

# 需要展示的策略及显示名称
STRATEGIES = {
    "base-base-heu": "Baseline",
    "base-base-opt": "[+ Scheduling]",
    "prop-prop-heu": "[+Allocation]",
    "prop-prop-opt": "[+(Allocation,Scheduling)]",
}

# 仅使用的天数；设为空集合表示不筛选
DAYS_FILTER = {}

# 为每个策略指定固定颜色，所有图保持一致（仿照示例：灰/浅蓝/深蓝）
COLORS = {
    "base-base-heu": "#c0c0c0",   # grey
    "base-base-opt": "#000000",   # black
    "prop-prop-heu": "#a6c8ff",   # light blue
    "prop-prop-opt": "#0066cc",   # deep blue
}

OUTPUT_DIR = Path("visualization/compare")


def save_fig(filename: str) -> None:
    out_path = OUTPUT_DIR / filename
    out_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(out_path, dpi=300, bbox_inches="tight")
    print(f"[INFO] saved {out_path}")


def parse_log_file(file_path: Path):
    """解析单个日志文件，返回结构化数据。"""
    data = {
        "daily_summary": {},
        "pairing_rates": {},
        "pairing_start_by_day": {},
        "completion_times": [],
        "tasks_completed_per_day": {},
        "relocation_counts": [],
        "relocation_counts_by_day": {},
        "total_relocations": 0,
        "task_completion_details": {},
        "aisle_busy_times": {},
        "production_lines": {},
    }

    content = file_path.read_text(encoding="utf-8")
    if "全部天汇总" in content:
        content_for_days = content.split("全部天汇总", 1)[1]
    else:
        content_for_days = content

    production_line_pattern = r"产线(PL\d+):\s*(\d+)组"
    for line, count in re.findall(production_line_pattern, content_for_days):
        data["production_lines"][line] = int(count)

    day_blocks = re.findall(
        r"第\s*(\d+)\s*天汇总\s*-+\s*(.*?)\s*(?=第\s*\d+\s*天汇总\s*-+|\Z)",
        content_for_days,
        re.DOTALL,
    )
    for day_str, block in day_blocks:
        day = int(day_str)
        data["daily_summary"][day] = {}
        relocation_match = re.search(r"移库数量:\s*(\d+)", block)
        if relocation_match:
            reloc = int(relocation_match.group(1))
            data["relocation_counts"].append(reloc)
            data["relocation_counts_by_day"][day] = reloc

    all_relocations = re.findall(r"移库数量:\s*(\d+)", content)
    if all_relocations:
        data["total_relocations"] = int(all_relocations[-1])

    task_completion_pattern = (
        r"第\s*(\d+)\s*个?\s*(入库|出库)任务\s+([\w\-]+)\s+完成"
        r"\s*(?:\(巷道\s+(\d+)\))?.*?起止\s+([\d.]+)s~([\d.]+)s"
    )
    for day, block in ((int(d), b) for d, b in day_blocks):
        matches = list(re.finditer(task_completion_pattern, block))
        for m in matches:
            task_index = int(m.group(1))
            task_type = m.group(2)
            task_id = m.group(3)
            aisle = int(m.group(4)) if m.group(4) else None
            start_time = float(m.group(5))
            end_time = float(m.group(6))
            data["task_completion_details"].setdefault(day, []).append(
                {
                    "index": task_index,
                    "type": task_type,
                    "id": task_id,
                    "aisle": aisle,
                    "start_time": start_time,
                    "end_time": end_time,
                    "duration": end_time - start_time,
                }
            )

    for day, tasks in data["task_completion_details"].items():
        outbound_count = sum(1 for t in tasks if "出库" in t["type"])
        inbound_count = sum(1 for t in tasks if "入库" in t["type"])
        data["tasks_completed_per_day"][day] = {
            "outbound": outbound_count,
            "inbound": inbound_count,
            "total": outbound_count + inbound_count,
        }

    data["aisle_busy_times"] = calculate_aisle_busy_times(data["task_completion_details"])

    start_end_pattern = (
        r"\[DAY (\d+) (开始|结束)\] 货位配对率: (\d+)/(\d+) = ([\d.]+)%; "
        r"梁配对率\(不含solo\): (\d+)/(\d+) = ([\d.]+)%; "
        r"梁配对率\(含solo\): (\d+)/(\d+) = ([\d.]+)%"
    )
    for m in re.findall(start_end_pattern, content):
        day = int(m[0])
        point = m[1]
        time_min = (day - 1) * 1440 if point == "开始" else day * 1440 - 1
        pairing_entry = {
            "slot": float(m[4]) / 100,
            "beam_without_solo": float(m[7]) / 100,
            "beam_with_solo": float(m[10]) / 100,
        }
        data["pairing_rates"][time_min] = pairing_entry
        if time_min % 1440 == 0:
            data["pairing_start_by_day"][day] = pairing_entry

    start_pattern_alt = (
        r"\[DAY (\d+) 开始\] 配对率\(旧，不含solo\):\s*([\d.]+)%.*?"
        r"梁配对率\(不含solo\):\s*([\d.]+)%.*?"
        r"梁配对率\(含solo\):\s*([\d.]+)%"
    )
    for m in re.findall(start_pattern_alt, content):
        day = int(m[0])
        pairing_entry = {
            "slot": float(m[1]) / 100,
            "beam_without_solo": float(m[2]) / 100,
            "beam_with_solo": float(m[3]) / 100,
        }
        data["pairing_start_by_day"][day] = pairing_entry
        time_min = (day - 1) * 1440
        if time_min not in data["pairing_rates"]:
            data["pairing_rates"][time_min] = pairing_entry

    pairing_pattern = (
        r"\[配对率 ([\d.]+)min\] 货位: (\d+)/(\d+) = ([\d.]+)%; "
        r"梁\(不含solo\): (\d+)/(\d+) = ([\d.]+)%; "
        r"梁\(含solo\): (\d+)/(\d+) = ([\d.]+)%"
    )
    # Parse pairing rates inside each day block; time axis is day-offset minutes.
    for day, block in ((int(d), b) for d, b in day_blocks):
        offset = (day - 1) * 1440.0
        for m in re.findall(pairing_pattern, block):
            time_raw = float(m[0])
            time_min = offset + time_raw
            data["pairing_rates"][time_min] = {
                "slot": float(m[3]) / 100,
                "beam_without_solo": float(m[6]) / 100,
                "beam_with_solo": float(m[9]) / 100,
            }

    # Fill start pairing rates when missing using earliest record of the day.
    if day_blocks:
        for day, _ in ((int(d), b) for d, b in day_blocks):
            if day in data["pairing_start_by_day"]:
                continue
            day_times = [
                t for t in data.get("pairing_rates", {}).keys()
                if isinstance(t, (int, float)) and int(t // 1440) + 1 == day
            ]
            if not day_times:
                continue
            earliest = min(day_times)
            data["pairing_start_by_day"][day] = data["pairing_rates"][earliest]

    return data


def _filter_days(data: dict, days_filter: set):
    """按天过滤数据，并重算相关汇总。"""
    if not days_filter:
        return data

    # 按天的字典
    for key in ["daily_summary", "tasks_completed_per_day", "relocation_counts_by_day", "task_completion_details", "pairing_start_by_day"]:
        if key in data and isinstance(data[key], dict):
            data[key] = {d: v for d, v in data[key].items() if d in days_filter}

    # pairing_rates（按分钟）：推算 day = floor(t/1440)+1
    pr_filtered = {}
    for t, val in data.get("pairing_rates", {}).items():
        if isinstance(t, (int, float)):
            day = int(t // 1440) + 1
            if day in days_filter:
                pr_filtered[t] = val
        else:
            pr_filtered[t] = val
    data["pairing_rates"] = pr_filtered

    # relocation_counts 重新按天汇总
    if "relocation_counts_by_day" in data:
        days_sorted = sorted(data["relocation_counts_by_day"].keys())
        data["relocation_counts"] = [data["relocation_counts_by_day"][d] for d in days_sorted]
        data["total_relocations"] = sum(data["relocation_counts"])

    # 过滤后重算巷道忙碌时间
    data["aisle_busy_times"] = calculate_aisle_busy_times(data.get("task_completion_details", {}))

    return data


def calculate_aisle_busy_times(task_details):
    """计算每一天的巷道忙碌时间，返回 {day: {aisle: stats}}。"""
    if not task_details:
        return {}

    day_stats: dict = {}
    for day in sorted(task_details.keys()):
        day_tasks = task_details.get(day, [])
        aisle_tasks = {}
        for task in day_tasks:
            aisle = task["aisle"]
            if aisle is None:
                continue
            aisle_tasks.setdefault(aisle, []).append(task)

        if not aisle_tasks:
            continue

        all_outbound = [t for tasks in aisle_tasks.values() for t in tasks if t["type"] == "出库"]
        if all_outbound:
            simulation_end_time = max(t["end_time"] for t in all_outbound)
        else:
            simulation_end_time = max((t["end_time"] for tasks in aisle_tasks.values() for t in tasks), default=0)

        aisle_stats = {}
        for aisle, tasks in aisle_tasks.items():
            valid_tasks = [t for t in tasks if t["end_time"] <= simulation_end_time]
            inbound_time = sum(t["duration"] for t in valid_tasks if t["type"] == "入库")
            outbound_time = sum(t["duration"] for t in valid_tasks if t["type"] == "出库")
            total_time = inbound_time + outbound_time
            utilization = total_time / simulation_end_time * 100 if simulation_end_time else 0
            aisle_stats[aisle] = {
                "inbound_time": inbound_time,
                "outbound_time": outbound_time,
                "total_time": total_time,
                "utilization": utilization,
                "simulation_end_time": simulation_end_time,
                "day": day,
            }
        # 平均
        if aisle_stats:
            avg_inbound = sum(v["inbound_time"] for v in aisle_stats.values()) / len(aisle_stats)
            avg_outbound = sum(v["outbound_time"] for v in aisle_stats.values()) / len(aisle_stats)
            avg_total = sum(v["total_time"] for v in aisle_stats.values()) / len(aisle_stats)
            avg_util = avg_total / simulation_end_time * 100 if simulation_end_time else 0
            aisle_stats["avg"] = {
                "inbound_time": avg_inbound,
                "outbound_time": avg_outbound,
                "total_time": avg_total,
                "utilization": avg_util,
                "simulation_end_time": simulation_end_time,
                "day": day,
            }
        day_stats[day] = aisle_stats

    return day_stats


def load_all_data(log_dir="logs"):
    all_data = {}
    log_path = Path(log_dir)
    for strategy_file, strategy_name in STRATEGIES.items():
        file_path = log_path / f"{strategy_file}.txt"
        if file_path.exists():
            print(f"正在解析 {strategy_file}.txt...")
            parsed = parse_log_file(file_path)
            all_data[strategy_file] = _filter_days(parsed, DAYS_FILTER)
        else:
            print(f"警告: 找不到文件 {file_path}")
    return all_data


def plot_pairing_start_by_day(all_data):
    """按天分组展示每日开始配对率（含solo）。"""
    plt.figure(figsize=(12, 7))
    strategies = list(STRATEGIES.items())
    if DAYS_FILTER:
        days_list = sorted(DAYS_FILTER)
    else:
        day_set = set()
        for data in all_data.values():
            day_set |= set(data.get("pairing_start_by_day", {}).keys())
        days_list = sorted(day_set)
    if not days_list:
        print("无每日开始配对率数据，跳过图表")
        return

    x = np.arange(len(days_list))
    total_width = 0.75
    bar_width = total_width / len(strategies)

    for idx_strategy, (strategy_file, strategy_name) in enumerate(strategies):
        data = all_data.get(strategy_file, {})
        per_day = data.get("pairing_start_by_day", {})
        heights = []
        for day in days_list:
            val = per_day.get(day, {}).get("beam_with_solo", 0)
            heights.append(val * 100)
        valid = [v for v in heights if v > 0]
        if valid:
            avg_val = sum(valid) / len(valid)
            legend_label = f"{strategy_name}: {avg_val:.2f}%"
        else:
            legend_label = strategy_name
        offsets = x + (idx_strategy - (len(strategies) - 1) / 2) * bar_width
        bars = plt.bar(offsets, heights, width=bar_width, label=legend_label, color=COLORS.get(strategy_file))
        for bar in bars:
            h = bar.get_height()
            if h > 0:
                plt.text(bar.get_x() + bar.get_width() / 2, h + 0.5, f"{h:.1f}%", ha="center", va="bottom", fontsize=10, rotation=90)

    plt.xticks(x, [f"Day {d}" for d in days_list])
    plt.xlabel("天数")
    plt.ylabel("开始梁配对率(含solo) (%)")
    title = "每日开始配对率"
    plt.grid(True, axis="y", linestyle="--", alpha=0.4)
    plt.legend(frameon=False, ncol=len(strategies), loc="upper center", bbox_to_anchor=(0.5, 1.1))
    plt.subplots_adjust(bottom=0.12)
    plt.figtext(0.5, 0.005, title, ha="center", fontsize=14)
    save_fig("pairing_start_by_day.png")
    plt.show()


def plot_tasks_completed_per_day(all_data):
    """按天分组的柱状图：同一天的三个策略并排对比。"""
    plt.figure(figsize=(12, 7))
    strategies = list(STRATEGIES.items())
    if DAYS_FILTER:
        days_list = sorted(DAYS_FILTER)
    else:
        day_set = set()
        for data in all_data.values():
            day_set |= set(data.get("tasks_completed_per_day", {}).keys())
        days_list = sorted(day_set)
    if not days_list:
        print("无任务完成数据，跳过任务数图表")
        return

    x = np.arange(len(days_list))
    total_width = 0.75
    bar_width = total_width / len(strategies)

    for idx_strategy, (strategy_file, strategy_name) in enumerate(strategies):
        data = all_data.get(strategy_file, {})
        per_day = data.get("tasks_completed_per_day", {})
        heights = []
        labels = []
        for day in days_list:
            total = per_day.get(day, {}).get("total", 0)
            out_c = per_day.get(day, {}).get("outbound", 0)
            in_c = per_day.get(day, {}).get("inbound", 0)
            heights.append(total)
            labels.append(f"{out_c}+{in_c}")
        offsets = x + (idx_strategy - (len(strategies) - 1) / 2) * bar_width
        bars = plt.bar(
            offsets,
            heights,
            width=bar_width,
            label=strategy_name,
            color=COLORS.get(strategy_file),
        )
        for bar, lbl in zip(bars, labels):
            h = bar.get_height()
            if h > 0:
                plt.text(bar.get_x() + bar.get_width() / 2, h + 0.3, lbl, ha="center", va="bottom", fontsize=10, rotation=90)

    plt.xticks(x, [f"Day {d}" for d in days_list])
    plt.xlabel("天数")
    plt.ylabel("完成任务数（出库+入库）")
    title = "每日完成任务数"
    plt.grid(True, axis="y", linestyle="--", alpha=0.4)
    plt.legend(frameon=False, ncol=len(strategies), loc="upper center", bbox_to_anchor=(0.5, 1.1))
    plt.subplots_adjust(bottom=0.12)
    plt.figtext(0.5, 0.005, title, ha="center", fontsize=14)
    save_fig("tasks_completed_per_day.png")
    plt.show()


def plot_completion_times(all_data):
    """按天分组展示每日最后出库完成时间。"""
    plt.figure(figsize=(12, 7))
    strategies = list(STRATEGIES.items())
    if DAYS_FILTER:
        days_list = sorted(DAYS_FILTER)
    else:
        day_set = set()
        for data in all_data.values():
            day_set |= set(data.get("task_completion_details", {}).keys())
        days_list = sorted(day_set)
    if not days_list:
        print("无出库任务完成时间数据，跳过图表")
        return

    x = np.arange(len(days_list))
    total_width = 0.75
    bar_width = total_width / len(strategies)

    for idx_strategy, (strategy_file, strategy_name) in enumerate(strategies):
        data = all_data.get(strategy_file, {})
        day_tasks = data.get("task_completion_details", {})
        heights = []
        for day in days_list:
            outbound_tasks = [t for t in day_tasks.get(day, []) if t["type"] == "出库"]
            heights.append(max((t["end_time"] for t in outbound_tasks), default=0) / 3600.0)
        valid_heights = [h for h in heights if h > 0]
        if valid_heights:
            avg_val = sum(valid_heights) / len(valid_heights)
            legend_label = f"{strategy_name}: {avg_val:.2f}小时"
        else:
            legend_label = strategy_name
        offsets = x + (idx_strategy - (len(strategies) - 1) / 2) * bar_width
        bars = plt.bar(
            offsets,
            heights,
            width=bar_width,
            label=legend_label,
            color=COLORS.get(strategy_file),
        )
        for bar in bars:
            h = bar.get_height()
            if h > 0:
                plt.text(bar.get_x() + bar.get_width() / 2, h + 0.02, f"{h:.2f}", ha="center", va="bottom", fontsize=10, rotation=90)

    plt.xticks(x, [f"Day {d}" for d in days_list])
    plt.xlabel("天数")
    plt.ylabel("最后出库任务完成时间 (小时)")
    title = "每日最后出库任务完成时间 (小时)"
    plt.grid(True, axis="y", linestyle="--", alpha=0.4)
    plt.legend(frameon=False, ncol=len(strategies), loc="upper center", bbox_to_anchor=(0.5, 1.1))
    plt.subplots_adjust(bottom=0.12)
    plt.figtext(0.5, 0.005, title, ha="center", fontsize=14)
    save_fig("completion_times.png")
    plt.show()


def _plot_daily_hourly_throughput(all_data, task_type_label: str, out_name: str, y_label: str, title: str):
    """Daily average hourly throughput = completed task count / last completion time (hours)."""
    plt.figure(figsize=(12, 7))
    strategies = list(STRATEGIES.items())
    if DAYS_FILTER:
        days_list = sorted(DAYS_FILTER)
    else:
        day_set = set()
        for data in all_data.values():
            day_set |= set(data.get("task_completion_details", {}).keys())
        days_list = sorted(day_set)
    if not days_list:
        print("No task completion data, skip throughput chart")
        return

    x = np.arange(len(days_list))
    total_width = 0.75
    bar_width = total_width / len(strategies)

    for idx_strategy, (strategy_file, strategy_name) in enumerate(strategies):
        data = all_data.get(strategy_file, {})
        day_tasks = data.get("task_completion_details", {})
        heights = []
        for day in days_list:
            tasks = [t for t in day_tasks.get(day, []) if t["type"] == task_type_label]
            cnt = len(tasks)
            last_h = max((t["end_time"] for t in tasks), default=0) / 3600.0
            rhythm = (cnt / last_h) if (cnt > 0 and last_h > 0) else 0.0
            heights.append(rhythm)
        valid_heights = [h for h in heights if h > 0]
        if valid_heights:
            avg_val = sum(valid_heights) / len(valid_heights)
            legend_label = f"{strategy_name}: {avg_val:.2f}"
        else:
            legend_label = strategy_name
        offsets = x + (idx_strategy - (len(strategies) - 1) / 2) * bar_width
        bars = plt.bar(
            offsets,
            heights,
            width=bar_width,
            label=legend_label,
            color=COLORS.get(strategy_file),
        )
        for bar in bars:
            h = bar.get_height()
            if h > 0:
                plt.text(bar.get_x() + bar.get_width() / 2, h + 0.02, f"{h:.2f}", ha="center", va="bottom", fontsize=10, rotation=90)

    plt.xticks(x, [f"Day {d}" for d in days_list])
    plt.xlabel("天数")
    plt.ylabel(y_label)
    plt.grid(True, axis="y", linestyle="--", alpha=0.4)
    plt.legend(frameon=False, ncol=len(strategies), loc="upper center", bbox_to_anchor=(0.5, 1.1))
    plt.subplots_adjust(bottom=0.12)
    plt.figtext(0.5, 0.005, title, ha="center", fontsize=14)
    save_fig(out_name)
    plt.show()


def plot_outbound_hourly_throughput(all_data):
    _plot_daily_hourly_throughput(
        all_data=all_data,
        task_type_label="出库",
        out_name="outbound_hourly_throughput.png",
        y_label="日均出库每小时节拍 (任务数/小时)",
        title="每日平均出库每小时节拍",
    )


def plot_inbound_hourly_throughput(all_data):
    _plot_daily_hourly_throughput(
        all_data=all_data,
        task_type_label="入库",
        out_name="inbound_hourly_throughput.png",
        y_label="日均入库每小时节拍 (任务数/小时)",
        title="每日平均入库每小时节拍",
    )


def plot_relocation_counts_by_day(all_data):
    """按天分组展示移库数量。"""
    plt.figure(figsize=(12, 7))
    strategies = list(STRATEGIES.items())
    if DAYS_FILTER:
        days_list = sorted(DAYS_FILTER)
    else:
        day_set = set()
        for data in all_data.values():
            day_set |= set(data.get("relocation_counts_by_day", {}).keys())
        days_list = sorted(day_set)
    if not days_list:
        print("无按天移库数据，跳过图表")
        return

    x = np.arange(len(days_list))
    total_width = 0.75
    bar_width = total_width / len(strategies)

    for idx_strategy, (strategy_file, strategy_name) in enumerate(strategies):
        reloc_day = all_data.get(strategy_file, {}).get("relocation_counts_by_day", {})
        heights = [reloc_day.get(day, 0) for day in days_list]
        avg_val = sum(heights) / len(heights) if heights else 0
        legend_label = f"{strategy_name}: {avg_val:.2f}"
        offsets = x + (idx_strategy - (len(strategies) - 1) / 2) * bar_width
        bars = plt.bar(offsets, heights, width=bar_width, label=legend_label, color=COLORS.get(strategy_file))
        for bar in bars:
            h = bar.get_height()
            if h > 0:
                plt.text(bar.get_x() + bar.get_width() / 2, h + 0.1, f"{h}", ha="center", va="bottom", fontsize=10, rotation=90)

    plt.xticks(x, [f"Day {d}" for d in days_list])
    plt.xlabel("天数")
    plt.ylabel("移库数量")
    title = "按天移库数量"
    plt.grid(True, axis="y", linestyle="--", alpha=0.4)
    plt.legend(frameon=False, ncol=len(strategies), loc="upper center", bbox_to_anchor=(0.5, 1.1))
    plt.subplots_adjust(bottom=0.12)
    plt.figtext(0.5, 0.005, title, ha="center", fontsize=14)
    save_fig("relocation_counts_by_day.png")
    plt.show()


def plot_avg_utilization_by_day(all_data):
    """按天分组展示平均巷道总占用率（来自每天下的 avg 条目）。"""
    plt.figure(figsize=(12, 7))
    strategies = list(STRATEGIES.items())
    if DAYS_FILTER:
        days_list = sorted(DAYS_FILTER)
    else:
        day_set = set()
        for data in all_data.values():
            day_set |= set(data.get("aisle_busy_times", {}).keys())
        days_list = sorted(day_set)
    if not days_list:
        print("无占用率数据，跳过图表")
        return

    x = np.arange(len(days_list))
    total_width = 0.75
    bar_width = total_width / len(strategies)

    for idx_strategy, (strategy_file, strategy_name) in enumerate(strategies):
        day_stats = all_data.get(strategy_file, {}).get("aisle_busy_times", {})
        heights = []
        for day in days_list:
            stats = day_stats.get(day, {})
            avg_entry = stats.get("avg", {})
            heights.append(avg_entry.get("utilization", 0))
        valid_heights = [h for h in heights if h > 0]
        if valid_heights:
            avg_val = sum(valid_heights) / len(valid_heights)
            legend_label = f"{strategy_name}: {avg_val:.2f}%"
        else:
            legend_label = strategy_name
        offsets = x + (idx_strategy - (len(strategies) - 1) / 2) * bar_width
        bars = plt.bar(offsets, heights, width=bar_width, label=legend_label, color=COLORS.get(strategy_file))
        for bar in bars:
            h = bar.get_height()
            if h > 0:
                plt.text(bar.get_x() + bar.get_width() / 2, h + 0.5, f"{h:.1f}%", ha="center", va="bottom", fontsize=10, rotation=90)

    plt.xticks(x, [f"Day {d}" for d in days_list])
    plt.xlabel("天数")
    plt.ylabel("平均巷道总占用率 (%)")
    title = "每日平均巷道总占用率"
    plt.grid(True, axis="y", linestyle="--", alpha=0.4)
    plt.legend(frameon=False, ncol=len(strategies), loc="upper center", bbox_to_anchor=(0.5, 1.1))
    plt.subplots_adjust(bottom=0.12)
    plt.figtext(0.5, 0.005, title, ha="center", fontsize=14)
    save_fig("avg_utilization_by_day.png")
    plt.show()

def print_summary_statistics(all_data):
    print("\n=== Summary ===")
    print(f"{'Strategy':<30} {'Total Tasks':<12}")
    print("-" * 48)
    for strategy_file, data in all_data.items():
        total_tasks = sum(v.get("total", 0) for v in data.get("tasks_completed_per_day", {}).values())
        print(f"{STRATEGIES[strategy_file]:<30} {total_tasks:<12}")


def print_aisle_busy_times(all_data):
    """打印每个策略下各巷道的忙碌时间。"""
    print("\n=== 巷道任务时间与占用率 ===")
    for strategy_file, data in all_data.items():
        print(f"\n{STRATEGIES[strategy_file]}:")
        all_days_stats = data.get("aisle_busy_times", {})
        if not all_days_stats:
            print("  无巷道忙碌时间数据")
            continue

        for day in sorted(all_days_stats.keys()):
            aisle_busy_times = all_days_stats[day]
            if not aisle_busy_times:
                continue
            numeric_aisles = sorted([k for k in aisle_busy_times.keys() if isinstance(k, (int, float))])
            sorted_aisles = numeric_aisles + (["avg"] if "avg" in aisle_busy_times else [])
            simulation_end_time = max(stats["simulation_end_time"] for stats in aisle_busy_times.values())
            print(f"  [Day {day}] 最后一个出库任务完成时间: {simulation_end_time/3600:.2f}小时")

            inbound_data = {}
            outbound_data = {}
            total_data = {}
            for aisle in sorted_aisles:
                stats = aisle_busy_times[aisle]
                inbound_time = stats["inbound_time"]
                outbound_time = stats["outbound_time"]
                total_time = stats["total_time"]
                inbound_pct = inbound_time / simulation_end_time * 100 if simulation_end_time else 0
                outbound_pct = outbound_time / simulation_end_time * 100 if simulation_end_time else 0
                total_pct = total_time / simulation_end_time * 100 if simulation_end_time else 0
                inbound_data[aisle] = f"{inbound_time:.1f}s ({inbound_pct:.1f}%)"
                outbound_data[aisle] = f"{outbound_time:.1f}s ({outbound_pct:.1f}%)"
                total_data[aisle] = f"{total_time:.1f}s ({total_pct:.1f}%)"

            print("    巷道入库耗时:", inbound_data)
            print("    巷道出库耗时:", outbound_data)
            print("    巷道总耗时:", total_data)


def print_aisle_used_time_std(all_data):
    """打印各策略的巷道使用时间标准差（按天）与均值。"""
    print("\n=== 巷道使用时间标准差 ===")
    for strategy_file, data in all_data.items():
        all_days_stats = data.get("aisle_busy_times", {})
        if not all_days_stats:
            continue
        per_day_std = []
        print(f"\n{STRATEGIES[strategy_file]}:")
        for day in sorted(all_days_stats.keys()):
            aisle_busy_times = all_days_stats[day]
            numeric_aisles = [k for k in aisle_busy_times.keys() if isinstance(k, (int, float))]
            used_times = [aisle_busy_times[a]["total_time"] for a in numeric_aisles]
            if not used_times:
                continue
            mean_val = sum(used_times) / len(used_times)
            variance = sum((v - mean_val) ** 2 for v in used_times) / len(used_times)
            std_dev = variance ** 0.5
            per_day_std.append(std_dev)
            print(f"  Day {day}: {std_dev:.2f}s")
        if per_day_std:
            avg_std = sum(per_day_std) / len(per_day_std)
            print(f"  平均标准差: {avg_std:.2f}s")


def print_aisle_task_counts(all_data):
    """打印每个策略下各巷道的入/出库任务次数。"""
    print("\n=== 各巷道入/出库任务次数 ===")
    for strategy_file, data in all_data.items():
        print(f"\n{STRATEGIES[strategy_file]}:")
        task_details = data.get("task_completion_details", {})
        aisle_counts = {}
        for day_tasks in task_details.values():
            for task in day_tasks:
                aisle = task["aisle"]
                if aisle is None:
                    continue
                task_type = task["type"]
                aisle_counts.setdefault(aisle, {"inbound": 0, "outbound": 0})
                if task_type == "入库":
                    aisle_counts[aisle]["inbound"] += 1
                elif task_type == "出库":
                    aisle_counts[aisle]["outbound"] += 1

        if not aisle_counts:
            print("  无巷道任务数据")
            continue

        sorted_aisles = sorted(aisle_counts.keys())
        inbound_data = {}
        outbound_data = {}
        total_data = {}
        for aisle in sorted_aisles:
            counts = aisle_counts[aisle]
            inbound_count = counts["inbound"]
            outbound_count = counts["outbound"]
            total_count = inbound_count + outbound_count
            inbound_data[aisle] = f"{inbound_count}次"
            outbound_data[aisle] = f"{outbound_count}次"
            total_data[aisle] = f"{total_count}次"

        print("  巷道入库次数:", inbound_data)
        print("  巷道出库次数:", outbound_data)
        print("  巷道总次数:", total_data)


def main():
    parser = argparse.ArgumentParser(description="可视化分析仓储策略仿真结果")
    parser.add_argument("--log-dir", default="logs", help="日志文件目录")
    parser.add_argument("--out-dir", default="visualization/compare", help="可视化输出目录")
    args = parser.parse_args()

    global OUTPUT_DIR
    OUTPUT_DIR = Path(args.out_dir)

    all_data = load_all_data(args.log_dir)
    if not all_data:
        print("错误: 没有找到有效的日志数据")
        return

    print_summary_statistics(all_data)
    print_aisle_busy_times(all_data)
    print_aisle_used_time_std(all_data)
    print_aisle_task_counts(all_data)
    plot_tasks_completed_per_day(all_data)
    plot_completion_times(all_data)
    plot_outbound_hourly_throughput(all_data)
    plot_inbound_hourly_throughput(all_data)
    plot_avg_utilization_by_day(all_data)
    print("\n图表已生成并保存到当前目录")


if __name__ == "__main__":
    main()
