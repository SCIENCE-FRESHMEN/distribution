"""
可视化脚本：解析日志并输出每日完成任务/完成时间等图表。

此脚本用于分析和可视化仓库仿真结果，包括任务完成数量、配对率、
巷道利用率等指标的图表展示。支持多种策略的对比分析，并将结果
导出为图片和CSV文件。
"""

import argparse
import re
import csv
from pathlib import Path
import matplotlib.pyplot as plt
import numpy as np

# 设置matplotlib样式和字体配置
if plt is not None:
    plt.style.use("seaborn-v0_8-whitegrid")
    plt.rcParams.update(
        {
            "font.sans-serif": ["SimHei", "Arial Unicode MS", "DejaVu Sans"],  # 设置中文字体
            "axes.unicode_minus": False,  # 正确显示负号
            "font.size": 12,              # 基础字体大小
            "axes.titlesize": 16,         # 标题字体大小
            "axes.labelsize": 13,         # 坐标轴标签字体大小
            "xtick.labelsize": 11,        # x轴刻度标签字体大小
            "ytick.labelsize": 11,        # y轴刻度标签字体大小
            "legend.fontsize": 11,        # 图例字体大小
        }
    )

# 需要展示的策略及显示名称
STRATEGIES = {
    "base-base-heu": "Baseline",                           # 基线策略
    "base-base-opt": "[+ Scheduling]",                     # 增加调度优化的策略
    "prop-prop-heu": "[+Allocation]",                      # 增加分配策略的策略
    "prop-prop-opt": "[+(Allocation,Scheduling)]",         # 同时增加分配策略和调度优化的策略
}

# 仅使用的天数；设为空集合表示不筛选
DAYS_FILTER = {}

# 为每个策略指定固定颜色，所有图保持一致（仿照示例：灰/浅蓝/深蓝）
COLORS = {
    "base-base-heu": "#c0c0c0",   # grey  - 基线策略使用灰色
    "base-base-opt": "#000000",   # black - 增加调度优化使用黑色
    "prop-prop-heu": "#a6c8ff",   # light blue - 增加分配策略使用浅蓝色
    "prop-prop-opt": "#0066cc",   # deep blue  - 同时增加分配和调度优化使用深蓝色
}

# 输出目录路径
OUTPUT_DIR = Path("visualization/compare")


def save_fig(filename: str) -> None:
    """保存当前图形到指定文件路径
    
    Args:
        filename: 要保存的文件名
    """
    out_path = OUTPUT_DIR / filename
    out_path.parent.mkdir(parents=True, exist_ok=True)  # 确保输出目录存在
    plt.savefig(out_path, dpi=300, bbox_inches="tight")  # 保存图片，300 DPI，紧凑布局
    print(f"[INFO] saved {out_path}")  # 输出保存成功的提示信息


def parse_log_file(file_path: Path):
    """解析单个日志文件，返回结构化数据
    
    从日志文件中提取各类统计信息，包括每日汇总、配对率、任务完成时间、
    移库数量、巷道忙碌时间等，并将其组织成易于处理的字典结构。
    
    Args:
        file_path: 日志文件路径
        
    Returns:
        包含解析后数据的字典
    """
    # 初始化返回数据结构
    data = {
        "daily_summary": {},           # 每日汇总信息
        "pairing_rates": {},          # 配对率信息（按时间点）
        "pairing_start_by_day": {},   # 每日开始时的配对率
        "completion_times": [],       # 完成时间列表
        "tasks_completed_per_day": {}, # 每日完成任务数
        "relocation_counts": [],      # 移库数量列表
        "relocation_counts_by_day": {}, # 按天的移库数量
        "total_relocations": 0,       # 总移库数量
        "task_completion_details": {}, # 任务完成详情
        "aisle_busy_times": {},       # 巷道忙碌时间
        "production_lines": {},       # 生产线信息
    }

    content = file_path.read_text(encoding="utf-8")
    if "全部天汇总" in content:
        content_for_days = content.split("全部天汇总", 1)[1]  # 只分析"全部天汇总"之后的内容
    else:
        content_for_days = content

    # 解析生产线完成情况
    production_line_pattern = r"产线(PL\d+):\s*(\d+)组"
    for line, count in re.findall(production_line_pattern, content_for_days):
        data["production_lines"][line] = int(count)

    # 解析每日汇总块
    day_blocks = re.findall(
        r"第\s*(\d+)\s*天汇总\s*-+\s*(.*?)\s*(?=第\s*\d+\s*天汇总\s*-+|\Z)",
        content_for_days,
        re.DOTALL,  # 使.能匹配换行符
    )
    for day_str, block in day_blocks:
        day = int(day_str)
        data["daily_summary"][day] = {}
        relocation_match = re.search(r"移库数量:\s*(\d+)", block)
        if relocation_match:
            reloc = int(relocation_match.group(1))
            data["relocation_counts"].append(reloc)
            data["relocation_counts_by_day"][day] = reloc

    # 解析总移库数量
    all_relocations = re.findall(r"移库数量:\s*(\d+)", content)
    if all_relocations:
        data["total_relocations"] = int(all_relocations[-1])

    # 解析任务完成详情
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
                    "index": task_index,           # 任务索引
                    "type": task_type,             # 任务类型（入库/出库）
                    "id": task_id,                 # 任务ID
                    "aisle": aisle,                # 巷道号
                    "start_time": start_time,      # 开始时间
                    "end_time": end_time,          # 结束时间
                    "duration": end_time - start_time,  # 持续时间
                }
            )

    # 计算每日完成的任务数（入库、出库、总计）
    for day, tasks in data["task_completion_details"].items():
        outbound_count = sum(1 for t in tasks if "出库" in t["type"])
        inbound_count = sum(1 for t in tasks if "入库" in t["type"])
        data["tasks_completed_per_day"][day] = {
            "outbound": outbound_count,  # 出库任务数
            "inbound": inbound_count,    # 入库任务数
            "total": outbound_count + inbound_count,  # 总任务数
        }

    # 计算巷道忙碌时间
    data["aisle_busy_times"] = calculate_aisle_busy_times(data["task_completion_details"])

    # 解析配对率信息
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
            "slot": float(m[4]) / 100,           # 货位配对率
            "beam_without_solo": float(m[7]) / 100,  # 梁配对率（不含solo）
            "beam_with_solo": float(m[10]) / 100,    # 梁配对率（含solo）
        }
        data["pairing_rates"][time_min] = pairing_entry
        if time_min % 1440 == 0:
            data["pairing_start_by_day"][day] = pairing_entry

    # 解析替代的配对率格式
    start_pattern_alt = (
        r"\[DAY (\d+) 开始\] 配对率\(旧，不含solo\):\s*([\d.]+)%.*?"
        r"梁配对率\(不含solo\):\s*([\d.]+)%.*?"
        r"梁配对率\(含solo\):\s*([\d.]+)%"
    )
    for m in re.findall(start_pattern_alt, content):
        day = int(m[0])
        pairing_entry = {
            "slot": float(m[1]) / 100,           # 货位配对率
            "beam_without_solo": float(m[2]) / 100,  # 梁配对率（不含solo）
            "beam_with_solo": float(m[3]) / 100,     # 梁配对率（含solo）
        }
        data["pairing_start_by_day"][day] = pairing_entry
        time_min = (day - 1) * 1440
        if time_min not in data["pairing_rates"]:
            data["pairing_rates"][time_min] = pairing_entry

    # 解析中间时刻的配对率
    pairing_pattern = (
        r"\[配对率 ([\d.]+)min\] 货位: (\d+)/(\d+) = ([\d.]+)%; "
        r"梁\(不含solo\): (\d+)/(\d+) = ([\d.]+)%; "
        r"梁\(含solo\): (\d+)/(\d+) = ([\d.]+)%"
    )
    # 解析每天块中的配对率；时间轴是按天偏移的分钟数
    for day, block in ((int(d), b) for d, b in day_blocks):
        offset = (day - 1) * 1440.0
        for m in re.findall(pairing_pattern, block):
            time_raw = float(m[0])
            time_min = offset + time_raw
            data["pairing_rates"][time_min] = {
                "slot": float(m[3]) / 100,           # 货位配对率
                "beam_without_solo": float(m[6]) / 100,  # 梁配对率（不含solo）
                "beam_with_solo": float(m[9]) / 100,     # 梁配对率（含solo）
            }

    # 如果缺少每日开始配对率，则使用当天最早的记录填充
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
    """按天过滤数据，并重算相关汇总。
    
    根据指定的天数过滤器，从数据中移除不需要的天的数据，并重新计算相关的汇总信息。
    
    Args:
        data: 包含原始数据的字典
        days_filter: 要保留的天数集合
        
    Returns:
        过滤后的数据字典
    """
    if not days_filter:
        return data

    # 按天的字典数据过滤
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
    """计算每一天的巷道忙碌时间，返回 {day: {aisle: stats}}。
    
    根据任务完成详情计算每个巷道在每天的忙碌时间，包括入库时间、出库时间、
    总时间、利用率等统计信息。
    
    Args:
        task_details: 任务完成详情字典，格式为 {day: [task_detail, ...]}
        
    Returns:
        巷道忙碌时间统计字典，格式为 {day: {aisle: stats, "avg": avg_stats}}
    """
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

        # 找到仿真结束时间（最后一个出库任务的结束时间）
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
                "inbound_time": inbound_time,        # 入库时间
                "outbound_time": outbound_time,      # 出库时间
                "total_time": total_time,            # 总时间
                "utilization": utilization,          # 利用率（百分比）
                "simulation_end_time": simulation_end_time,  # 仿真结束时间
                "day": day,                          # 天数
            }
        # 计算平均值
        if aisle_stats:
            avg_inbound = sum(v["inbound_time"] for v in aisle_stats.values()) / len(aisle_stats)
            avg_outbound = sum(v["outbound_time"] for v in aisle_stats.values()) / len(aisle_stats)
            avg_total = sum(v["total_time"] for v in aisle_stats.values()) / len(aisle_stats)
            avg_util = avg_total / simulation_end_time * 100 if simulation_end_time else 0
            aisle_stats["avg"] = {
                "inbound_time": avg_inbound,         # 平均入库时间
                "outbound_time": avg_outbound,       # 平均出库时间
                "total_time": avg_total,             # 平均总时间
                "utilization": avg_util,             # 平均利用率（百分比）
                "simulation_end_time": simulation_end_time,  # 仿真结束时间
                "day": day,                          # 天数
            }
        day_stats[day] = aisle_stats

    return day_stats


def load_all_data(log_dir="logs"):
    """加载所有日志文件的数据并解析成结构化数据
    
    遍历日志目录中的所有相关日志文件，解析每个文件并将结果组织成字典返回
    
    Args:
        log_dir: 日志文件所在的目录路径
        
    Returns:
        包含所有策略数据的字典，格式为 {strategy_file: parsed_data}
    """
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
    """按天分组展示每日开始配对率（含solo）。
    
    生成一个柱状图，显示每天开始时不同策略的配对率情况，便于比较不同策略的初始配对效果。
    
    Args:
        all_data: 包含所有策略数据的字典
    """
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
    """按天分组的柱状图：同一天的三个策略并排对比。
    
    生成一个柱状图，显示每天不同策略完成的任务数，包括入库和出库任务数量。
    
    Args:
        all_data: 包含所有策略数据的字典
    """
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
    """按天分组展示每日最后出库完成时间。
    
    生成一个柱状图，显示每天最后一个出库任务的完成时间，用于比较不同策略的效率。
    
    Args:
        all_data: 包含所有策略数据的字典
    """
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
    """计算并绘制日均每小时吞吐量图表（完成任务数/完成时间）。
    
    计算每天每小时的平均吞吐量（任务数/小时），用于评估不同策略的效率。
    计算公式为：完成任务数 / 最后完成时间（小时）
    
    Args:
        all_data: 包含所有策略数据的字典
        task_type_label: 任务类型标签（"入库" 或 "出库"）
        out_name: 输出文件名
        y_label: Y轴标签
        title: 图表标题
    """
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
            rhythm = (cnt / last_h) if (cnt > 0 and last_h > 0) else 0.0  # 吞吐量计算
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
    """绘制出库每小时吞吐量图表"""
    _plot_daily_hourly_throughput(
        all_data=all_data,
        task_type_label="出库",
        out_name="outbound_hourly_throughput.png",
        y_label="日均出库每小时节拍 (任务数/小时)",
        title="每日平均出库每小时节拍",
    )


def plot_inbound_hourly_throughput(all_data):
    """绘制入库每小时吞吐量图表"""
    _plot_daily_hourly_throughput(
        all_data=all_data,
        task_type_label="入库",
        out_name="inbound_hourly_throughput.png",
        y_label="日均入库每小时节拍 (任务数/小时)",
        title="每日平均入库每小时节拍",
    )


def plot_relocation_counts_by_day(all_data):
    """按天分组展示移库数量。
    
    生成一个柱状图，显示每天不同策略下的移库数量，用于比较不同策略的移库效率。
    
    Args:
        all_data: 包含所有策略数据的字典
    """
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
    """按天分组展示平均巷道总占用率（来自每天下的 avg 条目）。
    
    生成一个柱状图，显示每天不同策略下的平均巷道占用率，用于比较不同策略的设备利用率。
    
    Args:
        all_data: 包含所有策略数据的字典
    """
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
    """打印汇总统计信息，包括各策略的总任务数
    
    Args:
        all_data: 包含所有策略数据的字典
    """
    print("\n=== Summary ===")
    print(f"{'Strategy':<30} {'Total Tasks':<12}")
    print("-" * 48)
    for strategy_file, data in all_data.items():
        total_tasks = sum(v.get("total", 0) for v in data.get("tasks_completed_per_day", {}).values())
        print(f"{STRATEGIES[strategy_file]:<30} {total_tasks:<12}")


def print_aisle_busy_times(all_data):
    """打印每个策略下各巷道的忙碌时间。
    
    显示每个策略下每天各巷道的入库时间、出库时间、总时间及占用率的详细信息。
    
    Args:
        all_data: 包含所有策略数据的字典
    """
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
    """打印各策略的巷道使用时间标准差（按天）与均值。
    
    计算并显示每个策略下每天各巷道使用时间的标准差，用于评估设备负载均衡程度。
    
    Args:
        all_data: 包含所有策略数据的字典
    """
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
    """打印每个策略下各巷道的入/出库任务次数。
    
    统计并显示每个策略下各巷道处理的入库和出库任务次数，用于评估任务分配的均衡性。
    
    Args:
        all_data: 包含所有策略数据的字典
    """
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


def print_daily_aisle_avg_task_time(all_data):
    """
    按策略/按天/按巷道打印：
    1) 入库/出库总耗时、数量、平均单任务耗时
    2) 平均每小时数量：
       - 入库: count / (当天最后入库完成时间 / 3600)
       - 出库: count / (当天最后出库完成时间 / 3600)
    并导出 CSV 到输出目录。
    """
    print("\n=== Daily Aisle Avg Task Time (time/count/avg + per_hour_by_type_end) ===")
    inbound_label = "入库"
    outbound_label = "出库"
    csv_rows = []

    for strategy_file, data in all_data.items():
        print(f"\n[{STRATEGIES.get(strategy_file, strategy_file)}]:")

        task_details = data.get("task_completion_details", {}) or {}
        if not task_details:
            print("  no task completion data")
            continue

        for day in sorted(task_details.keys()):
            day_tasks = task_details.get(day, []) or []

            inbound_end_s = 0.0
            outbound_end_s = 0.0
            inbound_stats = {}
            outbound_stats = {}
            for task in day_tasks:
                aisle = task.get("aisle")
                if aisle is None:
                    continue
                t = str(task.get("type", ""))
                duration = float(task.get("duration", 0.0) or 0.0)
                end_time = float(task.get("end_time", 0.0) or 0.0)
                if t == inbound_label:
                    row = inbound_stats.setdefault(aisle, {"time_s": 0.0, "count": 0})
                    row["time_s"] += duration
                    row["count"] += 1
                    if end_time > inbound_end_s:
                        inbound_end_s = end_time
                elif t == outbound_label:
                    row = outbound_stats.setdefault(aisle, {"time_s": 0.0, "count": 0})
                    row["time_s"] += duration
                    row["count"] += 1
                    if end_time > outbound_end_s:
                        outbound_end_s = end_time

            inbound_end_h = inbound_end_s / 3600.0 if inbound_end_s > 0 else 0.0
            outbound_end_h = outbound_end_s / 3600.0 if outbound_end_s > 0 else 0.0

            def _build(stats_dict, end_h, direction):
                out = {}
                aisles = sorted(stats_dict.keys())
                for aisle in aisles:
                    cnt = int(stats_dict[aisle].get("count", 0))
                    time_s = float(stats_dict[aisle].get("time_s", 0.0))
                    avg_s = (time_s / cnt) if cnt > 0 else 0.0
                    per_hour = (cnt / end_h) if end_h > 0 else 0.0
                    out[aisle] = {
                        "total_time_s": round(time_s, 2),
                        "count": cnt,
                        "avg_task_s": round(avg_s, 2),
                        "per_hour": round(per_hour, 3),
                    }
                    csv_rows.append(
                        {
                            "strategy_key": strategy_file,
                            "strategy_name": STRATEGIES.get(strategy_file, strategy_file),
                            "day": day,
                            "aisle": aisle,
                            "direction": direction,
                            "total_time_s": round(time_s, 2),
                            "count": cnt,
                            "avg_task_s": round(avg_s, 2),
                            "day_end_s": round(end_h * 3600.0, 2),
                            "per_hour": round(per_hour, 3),
                        }
                    )
                return out

            inbound_out = _build(inbound_stats, inbound_end_h, "inbound")
            outbound_out = _build(outbound_stats, outbound_end_h, "outbound")

            print(
                f"  Day {day} "
                f"(inbound_end_s={round(inbound_end_s, 2)}, outbound_end_s={round(outbound_end_s, 2)}):"
            )
            print(f"    inbound:  {inbound_out}")
            print(f"    outbound: {outbound_out}")

    # 暂时关闭第一个CSV输出，避免与效率表重复导出。
    # out_csv = OUTPUT_DIR / "daily_aisle_avg_task_time.csv"
    # out_csv.parent.mkdir(parents=True, exist_ok=True)
    # with open(out_csv, "w", newline="", encoding="utf-8-sig") as f:
    #     writer = csv.DictWriter(
    #         f,
    #         fieldnames=[
    #             "strategy_key",
    #             "strategy_name",
    #             "day",
    #             "aisle",
    #             "direction",
    #             "total_time_s",
    #             "count",
    #             "avg_task_s",
    #             "day_end_s",
    #             "per_hour",
    #         ],
    #     )
    #     writer.writeheader()
    #     writer.writerows(csv_rows)
    # print(f"[INFO] saved {out_csv}")


def export_proposed_opt_daily_efficiency_csv(all_data):
    """
    仅导出 proposed+opt（prop-prop-opt）的每日效率表到同一个 CSV。
    结构按"每个巷道一行"，入库/出库作为列，并附加每日汇总行。
    """
    strategy_key = "prop-prop-opt"
    data = all_data.get(strategy_key)
    if not data:
        print(f"[WARN] strategy '{strategy_key}' not found, skip efficiency csv export")
        return

    inbound_label = "入库"
    outbound_label = "出库"
    task_details = data.get("task_completion_details", {}) or {}
    busy_by_day = data.get("aisle_busy_times", {}) or {}
    if not task_details:
        print(f"[WARN] strategy '{strategy_key}' has no task details, skip efficiency csv export")
        return

    rows = []
    for day in sorted(task_details.keys()):
        day_tasks = task_details.get(day, []) or []
        day_busy = busy_by_day.get(day, {}) or {}

        day_end_s = 0.0
        if isinstance(day_busy.get("avg"), dict):
            day_end_s = float(day_busy["avg"].get("simulation_end_time", 0.0) or 0.0)
        if day_end_s <= 0:
            for t in day_tasks:
                day_end_s = max(day_end_s, float(t.get("end_time", 0.0) or 0.0))
        inbound_end_s = max(
            (float(t.get("end_time", 0.0) or 0.0) for t in day_tasks if str(t.get("type", "")) == inbound_label),
            default=0.0,
        )

        aisle_stats = {}
        for task in day_tasks:
            aisle = task.get("aisle")
            if aisle is None:
                continue
            task_type = str(task.get("type", ""))
            duration = float(task.get("duration", 0.0) or 0.0)
            st = aisle_stats.setdefault(
                aisle,
                {"inbound_count": 0, "outbound_count": 0, "total_count": 0, "work_time_s": 0.0},
            )
            st["work_time_s"] += duration
            st["total_count"] += 1
            if task_type == inbound_label:
                st["inbound_count"] += 1
            elif task_type == outbound_label:
                st["outbound_count"] += 1

        for aisle in sorted(k for k in day_busy.keys() if isinstance(k, (int, float))):
            aisle_stats.setdefault(
                aisle,
                {"inbound_count": 0, "outbound_count": 0, "total_count": 0, "work_time_s": 0.0},
            )

        day_total_inbound = 0
        day_total_outbound = 0
        day_total_count = 0
        day_idle_time_s = 0.0
        day_loss_jph = 0.0
        day_reach_jph = 0.0
        day_end_h = day_end_s / 3600.0 if day_end_s > 0 else 0.0
        inbound_end_h = inbound_end_s / 3600.0 if inbound_end_s > 0 else 0.0

        for aisle in sorted(aisle_stats.keys()):
            st = aisle_stats[aisle]
            inbound_count = int(st["inbound_count"])
            outbound_count = int(st["outbound_count"])
            total_count = int(st["total_count"])
            inbound_jph = (inbound_count / inbound_end_h) if inbound_end_h > 0 else 0.0
            outbound_jph = (outbound_count / day_end_h) if day_end_h > 0 else 0.0
            total_jph = inbound_jph + outbound_jph
            work_time_s = float(st["work_time_s"])
            idle_time_s = max(day_end_s - work_time_s, 0.0)
            avg_eff = (work_time_s / total_count) if total_count > 0 else 0.0  # s/task
            theoretical_reach_jph = (3600.0 / avg_eff) if avg_eff > 0 else 0.0
            theoretical_loss_jph = (
                theoretical_reach_jph * idle_time_s / day_end_s
                if day_end_s > 0
                else 0.0
            )

            day_total_inbound += inbound_count
            day_total_outbound += outbound_count
            day_total_count += total_count
            day_idle_time_s += idle_time_s
            day_loss_jph += theoretical_loss_jph
            day_reach_jph += theoretical_reach_jph

            rows.append(
                {
                    "strategy_key": strategy_key,
                    "strategy_name": STRATEGIES.get(strategy_key, strategy_key),
                    "day": day,
                    "aisle": aisle,
                    "row_type": "aisle",
                    "machine_name": f"{int(aisle)}号堆垛机",
                    "inbound_jph": round(inbound_jph, 2),
                    "outbound_jph": round(outbound_jph, 2),
                    "total_jph": round(total_jph, 2),
                    "work_time_s": round(work_time_s, 2),
                    "idle_time_s": round(idle_time_s, 2),
                    "avg_efficiency_s_per_task": round(avg_eff, 2),
                    "theoretical_loss_jph": round(theoretical_loss_jph, 2),
                    "theoretical_reach_jph": round(theoretical_reach_jph, 2),
                }
            )

        rows.append(
            {
                "strategy_key": strategy_key,
                "strategy_name": STRATEGIES.get(strategy_key, strategy_key),
                "day": day,
                "aisle": "",
                "row_type": "summary",
                "machine_name": "汇总",
                "inbound_jph": round((day_total_inbound / inbound_end_h), 2) if inbound_end_h > 0 else 0.0,
                "outbound_jph": round((day_total_outbound / day_end_h), 2) if day_end_h > 0 else 0.0,
                "total_jph": round(
                    ((day_total_inbound / inbound_end_h) if inbound_end_h > 0 else 0.0)
                    + ((day_total_outbound / day_end_h) if day_end_h > 0 else 0.0),
                    2,
                ),
                "work_time_s": "",
                "idle_time_s": round(day_idle_time_s, 2),
                "avg_efficiency_s_per_task": "",
                "theoretical_loss_jph": round(day_loss_jph, 2),
                "theoretical_reach_jph": round(day_reach_jph, 2),
            }
        )

    out_csv = OUTPUT_DIR / "proposed_opt_daily_efficiency_table.csv"
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(out_csv, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "strategy_key",
                "strategy_name",
                "day",
                "aisle",
                "row_type",
                "machine_name",
                "inbound_jph",
                "outbound_jph",
                "total_jph",
                "work_time_s",
                "idle_time_s",
                "avg_efficiency_s_per_task",
                "theoretical_loss_jph",
                "theoretical_reach_jph",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)
    print(f"[INFO] saved {out_csv}")

def main():
    """主函数，解析命令行参数并执行可视化分析"""
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
    print_daily_aisle_avg_task_time(all_data)
    print_aisle_used_time_std(all_data)
    print_aisle_task_counts(all_data)
    export_proposed_opt_daily_efficiency_csv(all_data)
    plot_tasks_completed_per_day(all_data)
    plot_completion_times(all_data)
    plot_outbound_hourly_throughput(all_data)
    plot_inbound_hourly_throughput(all_data)
    plot_avg_utilization_by_day(all_data)
    print("\n图表已生成并保存到当前目录")


if __name__ == "__main__":
    main()
