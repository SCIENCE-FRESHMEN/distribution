"""
可视化脚本：解析日志并输出配对率、每日完成任务等图表。
"""

import argparse
import re
from pathlib import Path
import matplotlib.pyplot as plt
import numpy as np

if plt is not None:
    try:
        plt.style.use("seaborn-v0_8-whitegrid")
    except OSError:
        plt.style.use("seaborn-whitegrid")
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
DAYS_FILTER = set()

# 为每个策略指定固定颜色，所有图保持一致（仿照示例：灰/浅蓝/深蓝）
COLORS = {
    "base-base-heu": "#c0c0c0",   # grey
    "base-base-opt": "#000000",   # black
    "prop-prop-heu": "#a6c8ff",   # light blue
    "prop-prop-opt": "#0066cc",   # deep blue
}

OUTPUT_DIR = Path("visualization/compare")


def read_log_text(file_path: Path) -> str:
    """Read log text with common Windows/terminal encoding fallbacks.

    Args:
        file_path (Path): 需要读取、写入或检查的文件路径。

    Returns:
        str: 当前处理得到的文本标识、格式化结果或诊断信息。
    """
    raw = file_path.read_bytes()
    for encoding in ("utf-8", "utf-8-sig", "utf-16", "utf-16-le", "gbk"):
        try:
            return raw.decode(encoding)
        except Exception:
            continue
    return raw.decode("utf-8", errors="ignore")


# 覆盖乱码标签辅助函数，统一改用正确的中文标签。
def _is_outbound_task(task: dict) -> bool:
    """根据输入状态和业务规则返回布尔判断结果。

    输入：task（dict）
    输出：bool

    Args:
        task (dict): 当前处理的任务记录。

    Returns:
        bool: 条件满足、处理成功或校验通过时为 ``True``，否则为 ``False``。
    """
    task_id = str(task.get("id", "")).upper()
    task_type = str(task.get("type", ""))
    return task_id.startswith("OUT") or "出库" in task_type


def _is_inbound_task(task: dict) -> bool:
    """根据输入状态和业务规则返回布尔判断结果。

    输入：task（dict）
    输出：bool

    Args:
        task (dict): 当前处理的任务记录。

    Returns:
        bool: 条件满足、处理成功或校验通过时为 ``True``，否则为 ``False``。
    """
    task_id = str(task.get("id", "")).upper()
    task_type = str(task.get("type", ""))
    return task_id.startswith("IN") or "入库" in task_type


def _match_task_type(task: dict, task_type_label: str) -> bool:
    """执行 `_match_task_type` 对应的模块处理步骤，并返回该步骤产生的结果。

    输入：task（dict）、task_type_label（str）
    输出：bool

    Args:
        task (dict): 当前处理的任务记录。
        task_type_label (str): 供当前处理流程使用的 `task_type_label` 值。

    Returns:
        bool: 条件满足、处理成功或校验通过时为 ``True``，否则为 ``False``。
    """
    if task_type_label == "出库":
        return _is_outbound_task(task)
    if task_type_label == "入库":
        return _is_inbound_task(task)
    return str(task.get("type", "")) == task_type_label


def save_fig(filename: str) -> None:
    """将当前处理结果按约定格式写入目标文件或状态载体。

    输入：filename（str）
    输出：None

    Args:
        filename (str): 输入或输出文件名。

    Returns:
        None: 不返回业务数据；处理结果写入实例状态、传入对象或外部响应。
    """
    if plt is None:
        print("[WARN] matplotlib unavailable; skip figure output")
        return
    out_path = OUTPUT_DIR / filename
    out_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(out_path, dpi=300, bbox_inches="tight")
    print(f"[INFO] saved {out_path}")

PAIRING_RATE_TYPES = {
    "slot": "货位配对率",
    "beam_without_solo": "梁配对率(不含solo)",
    "beam_with_solo": "梁配对率(含solo)",
}


def _get_opt_only_strategy_data(all_data: dict) -> dict:
    """仅返回以 -opt 结尾的策略数据，用于统计 opt 日志。

    Args:
        all_data (dict): 按策略汇总的完整仿真或统计数据。

    Returns:
        dict: 按函数约定字段组织的计算结果映射。
    """
    return {strategy_file: data for strategy_file, data in all_data.items() if str(strategy_file).endswith("-opt")}


def parse_log_file(file_path: Path):
    """解析单个日志文件，返回结构化数据。

    Args:
        file_path (Path): 需要读取、写入或检查的文件路径。

    Returns:
        Any: 当前处理流程产生的结果；具体结构由函数摘要说明。
    """
    data = {
        "daily_summary": {},
        "pairing_rates": {},
        "pairing_start_by_day": {},
        "completion_times": [],
        "tasks_completed_per_day": {},
        "relocation_counts": [],
        "relocation_counts_by_day": {},
        "relocation_intervals_by_day": {},
        "total_relocations": 0,
        "task_completion_details": {},
        "aisle_busy_times": {},
        "invalid_idle_by_day": {},
        "production_lines": {},
        "optimization_solve_times": [],
        "optimization_solve_time_buckets": {"0-1s": 0, "1-2s": 0, "2s以上": 0},
    }

    content = read_log_text(file_path)
    if "全部天汇总" in content:
        content_for_days = content.split("全部天汇总", 1)[1]
    else:
        content_for_days = content

    production_line_pattern = r"产线(PL\d+):\s*(\d+)组"
    for line, count in re.findall(production_line_pattern, content_for_days):
        data["production_lines"][line] = int(count)

    current_day = None
    for line in content.splitlines():
        day_match = re.search(r"\[DAY\s+(\d+)", line)
        if day_match:
            current_day = int(day_match.group(1))
        if current_day is None or not line.startswith("[") or line.startswith("[DAY"):
            continue
        relocation_match = re.search(r"\[[^\]]+\]\s+\D*(\d+)\D+([\d.]+)s\D+([\d.]+)s", line)
        if relocation_match:
            data["relocation_intervals_by_day"].setdefault(current_day, []).append(
                {
                    "aisle": int(relocation_match.group(1)),
                    "start": float(relocation_match.group(2)),
                    "end": float(relocation_match.group(3)),
                }
            )

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
        relocation_intervals = []
        for match in re.finditer(
            r"\[移库占用\]\s+巷道\s+(\d+)\s+在时间段\s+([\d.]+)s\s+到\s+([\d.]+)s",
            block,
        ):
            relocation_intervals.append(
                {
                    "aisle": int(match.group(1)),
                    "start": float(match.group(2)),
                    "end": float(match.group(3)),
                }
            )
        data["relocation_intervals_by_day"].setdefault(day, []).extend(relocation_intervals)
        if not data["relocation_intervals_by_day"].get(day):
            fallback_intervals = []
            for match in re.finditer(r"\[[^\]]+\]\s+\D*(\d+)\D+([\d.]+)s\D+([\d.]+)s", block):
                fallback_intervals.append(
                    {
                        "aisle": int(match.group(1)),
                        "start": float(match.group(2)),
                        "end": float(match.group(3)),
                    }
                )
            if fallback_intervals:
                data["relocation_intervals_by_day"][day] = fallback_intervals

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

    invalid_idle_pattern = r"\[DAY\s+(\d+)\s+INVALID_IDLE\]\s+aisle=(\d+)\s+time=([\d.]+)\s+ratio=([\d.]+)"
    for day_str, aisle_str, time_str, ratio_str in re.findall(invalid_idle_pattern, content):
        day = int(day_str)
        aisle = int(aisle_str)
        data["invalid_idle_by_day"].setdefault(day, {})[aisle] = {
            "time": float(time_str),
            "ratio": float(ratio_str),
        }

    optimize_time_pattern = r"\[优化器\]调度完成，耗时([\d.]+)秒"
    for solve_time in re.findall(optimize_time_pattern, content):
        solve_seconds = float(solve_time)
        data["optimization_solve_times"].append(solve_seconds)
        if solve_seconds < 1.0:
            data["optimization_solve_time_buckets"]["0-1s"] += 1
        elif solve_seconds < 2.0:
            data["optimization_solve_time_buckets"]["1-2s"] += 1
        else:
            data["optimization_solve_time_buckets"]["2s以上"] += 1

    for day, tasks in data["task_completion_details"].items():
        outbound_count = sum(1 for t in tasks if _is_outbound_task(t))
        inbound_count = sum(1 for t in tasks if _is_inbound_task(t))
        data["tasks_completed_per_day"][day] = {
            "outbound": outbound_count,
            "inbound": inbound_count,
            "total": outbound_count + inbound_count,
        }

    data["aisle_busy_times"] = calculate_aisle_busy_times(
        data["task_completion_details"],
        data["relocation_intervals_by_day"],
    )
    for day, aisle_idle in data["invalid_idle_by_day"].items():
        if day not in data["aisle_busy_times"]:
            continue
        day_stats = data["aisle_busy_times"][day]
        numeric_aisles = [k for k in day_stats.keys() if isinstance(k, (int, float))]
        for aisle, idle_stats in aisle_idle.items():
            if aisle not in day_stats:
                continue
            day_stats[aisle]["invalid_idle_time"] = idle_stats["time"]
            day_stats[aisle]["invalid_idle_ratio"] = idle_stats["ratio"]
        if numeric_aisles:
            avg_invalid_idle = sum(day_stats[aisle].get("invalid_idle_time", 0.0) for aisle in numeric_aisles) / len(numeric_aisles)
            avg_invalid_ratio = sum(day_stats[aisle].get("invalid_idle_ratio", 0.0) for aisle in numeric_aisles) / len(numeric_aisles)
            if "avg" in day_stats:
                day_stats["avg"]["invalid_idle_time"] = avg_invalid_idle
                day_stats["avg"]["invalid_idle_ratio"] = avg_invalid_ratio

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
# 解析每个日期块内的配对率；横轴使用相对该日开始的分钟数。
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

# 若缺少日初配对率，则使用当天最早一条记录补齐。
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

    Args:
        data (dict): 当前处理的数据字典。
        days_filter (set): 供当前处理流程使用的 `days_filter` 值。

    Returns:
        Any: 当前处理流程产生的结果；具体结构由函数摘要说明。
    """
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
    if "relocation_intervals_by_day" in data and isinstance(data["relocation_intervals_by_day"], dict):
        data["relocation_intervals_by_day"] = {
            d: v for d, v in data["relocation_intervals_by_day"].items() if d in days_filter
        }
    data["aisle_busy_times"] = calculate_aisle_busy_times(
        data.get("task_completion_details", {}),
        data.get("relocation_intervals_by_day", {}),
    )

    return data




def _merge_intervals(intervals):
    """执行 `_merge_intervals` 对应的模块处理步骤，并返回该步骤产生的结果。

    Args:
        intervals (Any): 供当前处理流程使用的 `intervals` 值。

    Returns:
        Any: 当前处理流程产生的结果；具体结构由函数摘要说明。
    """
    if not intervals:
        return []
    ordered = sorted((float(start), float(end)) for start, end in intervals if end > start)
    if not ordered:
        return []
    merged = [[ordered[0][0], ordered[0][1]]]
    for start, end in ordered[1:]:
        if start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(start, end) for start, end in merged]


def _sum_intervals(intervals):
    """执行 `_sum_intervals` 对应的模块处理步骤，并返回该步骤产生的结果。

    Args:
        intervals (Any): 供当前处理流程使用的 `intervals` 值。

    Returns:
        Any: 当前处理流程产生的结果；具体结构由函数摘要说明。
    """
    return sum(end - start for start, end in _merge_intervals(intervals))


def _intersection_duration(intervals_a, intervals_b):
    """执行 `_intersection_duration` 对应的模块处理步骤，并返回该步骤产生的结果。

    Args:
        intervals_a (Any): 供当前处理流程使用的 `intervals_a` 值。
        intervals_b (Any): 供当前处理流程使用的 `intervals_b` 值。

    Returns:
        Any: 当前处理流程产生的结果；具体结构由函数摘要说明。
    """
    merged_a = _merge_intervals(intervals_a)
    merged_b = _merge_intervals(intervals_b)
    i = 0
    j = 0
    overlap = 0.0
    while i < len(merged_a) and j < len(merged_b):
        start = max(merged_a[i][0], merged_b[j][0])
        end = min(merged_a[i][1], merged_b[j][1])
        if end > start:
            overlap += end - start
        if merged_a[i][1] <= merged_b[j][1]:
            i += 1
        else:
            j += 1
    return overlap


def calculate_aisle_busy_times(task_details, relocation_intervals_by_day=None):
    """Compute per-day aisle busy times from inbound/outbound tasks only.

    Args:
        task_details (Any): 供当前处理流程使用的 `task_details` 值。
        relocation_intervals_by_day (Any，可选): 供当前处理流程使用的 `relocation_intervals_by_day` 值。

    Returns:
        Any: 当前处理流程产生的结果；具体结构由函数摘要说明。
    """
    if not task_details:
        return {}
    day_stats: dict = {}
    all_days = sorted(task_details.keys())
    for day in all_days:
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
        for aisle in sorted(aisle_tasks.keys()):
            tasks = aisle_tasks.get(aisle, [])
            valid_tasks = [t for t in tasks if t["end_time"] <= simulation_end_time]

            inbound_time = sum(t["duration"] for t in valid_tasks if t["type"] == "入库")
            outbound_time = sum(t["duration"] for t in valid_tasks if t["type"] == "出库")
            task_intervals = [(t["start_time"], t["end_time"]) for t in valid_tasks]
            task_busy_time = _sum_intervals(task_intervals)
            relocation_time = 0.0
            relocation_overlap_time = 0.0
            total_time = task_busy_time
            utilization = total_time / simulation_end_time * 100 if simulation_end_time else 0

            aisle_stats[aisle] = {
                "inbound_time": inbound_time,
                "outbound_time": outbound_time,
                "task_busy_time": task_busy_time,
                "relocation_time": relocation_time,
                "relocation_overlap_time": relocation_overlap_time,
                "total_time": total_time,
                "utilization": utilization,
                "simulation_end_time": simulation_end_time,
                "day": day,
            }

        if aisle_stats:
            divisor = len(aisle_stats)
            avg_inbound = sum(v["inbound_time"] for v in aisle_stats.values()) / divisor
            avg_outbound = sum(v["outbound_time"] for v in aisle_stats.values()) / divisor
            avg_task_busy = sum(v["task_busy_time"] for v in aisle_stats.values()) / divisor
            avg_total = sum(v["total_time"] for v in aisle_stats.values()) / divisor
            avg_util = avg_total / simulation_end_time * 100 if simulation_end_time else 0
            aisle_stats["avg"] = {
                "inbound_time": avg_inbound,
                "outbound_time": avg_outbound,
                "task_busy_time": avg_task_busy,
                "relocation_time": 0.0,
                "relocation_overlap_time": 0.0,
                "total_time": avg_total,
                "utilization": avg_util,
                "simulation_end_time": simulation_end_time,
                "day": day,
            }
        day_stats[day] = aisle_stats

    return day_stats


def _calculate_aisle_load_balance_stats(aisle_busy_times):
    """Calculate load-balance metrics from per-aisle total occupied time.

    Args:
        aisle_busy_times (Any): 供当前处理流程使用的 `aisle_busy_times` 值。

    Returns:
        Any: 当前处理流程产生的结果；具体结构由函数摘要说明。
    """
    numeric_aisles = [k for k in aisle_busy_times.keys() if isinstance(k, (int, float))]
    loads = [aisle_busy_times[a]["total_time"] for a in numeric_aisles if "total_time" in aisle_busy_times[a]]
    if not loads:
        return None

    avg_load = sum(loads) / len(loads)
    if avg_load <= 0:
        return {
            "avg_load": avg_load,
            "max_deviation_ratio": 0.0,
            "within_10pct": True,
            "deviation_ratios": [0.0 for _ in loads],
        }

    deviation_ratios = [abs(v - avg_load) / avg_load for v in loads]
    max_deviation_ratio = max(deviation_ratios) if deviation_ratios else 0.0
    return {
        "avg_load": avg_load,
        "max_deviation_ratio": max_deviation_ratio,
        "within_10pct": max_deviation_ratio <= 0.10,
        "deviation_ratios": deviation_ratios,
    }


def _calculate_aisle_task_count_balance_stats(day_tasks):
    """Calculate balance stats from per-aisle total task counts.

    Args:
        day_tasks (Any): 用于当前计算的任务列表。

    Returns:
        Any: 当前处理流程产生的结果；具体结构由函数摘要说明。
    """
    counts_by_aisle = {}
    for task in day_tasks or []:
        aisle = task.get("aisle")
        if aisle is None:
            continue
        counts_by_aisle[aisle] = counts_by_aisle.get(aisle, 0) + 1

    counts = list(counts_by_aisle.values())
    if not counts:
        return None

    avg_count = sum(counts) / len(counts)
    if avg_count <= 0:
        return {
            "avg_count": avg_count,
            "max_deviation_count": 0.0,
            "max_deviation_ratio": 0.0,
            "within_10pct": True,
        }

    deviation_counts = [abs(v - avg_count) for v in counts]
    deviation_ratios = [abs(v - avg_count) / avg_count for v in counts]
    max_deviation_count = max(deviation_counts) if deviation_counts else 0.0
    max_deviation_ratio = max(deviation_ratios) if deviation_ratios else 0.0
    return {
        "avg_count": avg_count,
        "max_deviation_count": max_deviation_count,
        "max_deviation_ratio": max_deviation_ratio,
        "within_10pct": max_deviation_ratio <= 0.10,
    }


def _filter_plot_days(days_list):
    """Keep all days in charts; exclusion is shown only in legend averages.

    Args:
        days_list (Any): 供当前处理流程使用的 `days_list` 值。

    Returns:
        Any: 当前处理流程产生的结果；具体结构由函数摘要说明。
    """
    return list(days_list)


def _format_dual_avg_label(strategy_name, day_value_pairs, formatter):
    """将内部数据格式化为日志、展示或接口输出所需的形式。

    Args:
        strategy_name (Any): 供当前处理流程使用的 `strategy_name` 值。
        day_value_pairs (Any): 供当前处理流程使用的 `day_value_pairs` 值。
        formatter (Any): 供当前处理流程使用的 `formatter` 值。

    Returns:
        Any: 当前处理流程产生的结果；具体结构由函数摘要说明。
    """
    values_all = [value for day, value in day_value_pairs if isinstance(value, (int, float)) and value > 0]
    if not values_all:
        return strategy_name

    values_excl_day1 = [
        value for day, value in day_value_pairs
        if day != 1 and isinstance(value, (int, float)) and value > 0
    ]
    avg_all = sum(values_all) / len(values_all)
    avg_excl_day1 = (sum(values_excl_day1) / len(values_excl_day1)) if values_excl_day1 else avg_all
    return f"{strategy_name}: {formatter(avg_all)}({formatter(avg_excl_day1)})"


def load_all_data(log_dir="logs", days_filter=None):
    """加载配置、文件或既有状态，并转换为当前模块可消费的数据。

    Args:
        log_dir (Any，可选): 日志输入目录。
        days_filter (Any，可选): 供当前处理流程使用的 `days_filter` 值。

    Returns:
        Any: 当前处理流程产生的结果；具体结构由函数摘要说明。
    """
    all_data = {}
    log_path = Path(log_dir)
    effective_days_filter = DAYS_FILTER if days_filter is None else set(days_filter)
    for strategy_file, strategy_name in STRATEGIES.items():
        file_path = log_path / f"{strategy_file}.txt"
        if file_path.exists():
            print(f"正在解析 {strategy_file}.txt...")
            parsed = parse_log_file(file_path)
            all_data[strategy_file] = _filter_days(parsed, effective_days_filter)
        else:
            print(f"警告: 找不到文件 {file_path}")
    return all_data


def plot_pairing_rates_over_time(all_data, rate_type="beam_with_solo"):
    """执行 `plot_pairing_rates_over_time` 对应的模块处理步骤，并返回该步骤产生的结果。

    Args:
        all_data (Any): 按策略汇总的完整仿真或统计数据。
        rate_type (Any，可选): 供当前处理流程使用的 `rate_type` 值。

    Returns:
        None: 通过实例状态、队列或外部副作用完成处理。
    """
    plt.figure(figsize=(12, 8))
    for strategy_file, data in all_data.items():
        times = sorted([t for t in data["pairing_rates"].keys() if isinstance(t, (int, float))])
        rates = [data["pairing_rates"][t][rate_type] for t in times]
        # 平均值基于所有记录（含非数字键）
        all_rates = [
            v[rate_type]
            for v in data.get("pairing_rates", {}).values()
            if isinstance(v, dict) and rate_type in v
        ]
        if all_rates:
            avg_rate = sum(all_rates) / len(all_rates)
            legend_label = f"{STRATEGIES[strategy_file]}: {avg_rate*100:.1f}%"
        else:
            legend_label = STRATEGIES[strategy_file]
        plt.plot(
            times,
            rates,
            label=legend_label,
            marker="o",
            markersize=4,
            color=COLORS.get(strategy_file),
        )
    plt.xlabel("时间 (分钟)")
    plt.ylabel(PAIRING_RATE_TYPES[rate_type])
    plt.title(f"{PAIRING_RATE_TYPES[rate_type]} 随时间变化")
    plt.legend()
    plt.grid(True, axis="y", linestyle="--", alpha=0.4)
    plt.tight_layout()
    save_fig("pairing_rates.png")
    plt.show()


def plot_pairing_start_by_day(all_data):
    """按天分组展示每日开始配对率（含solo）。

    Args:
        all_data (Any): 按策略汇总的完整仿真或统计数据。

    Returns:
        None: 通过实例状态、队列或外部副作用完成处理。
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
    days_list = _filter_plot_days(days_list)
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
        legend_label = _format_dual_avg_label(
            strategy_name,
            list(zip(days_list, heights)),
            lambda x: f"{x:.2f}%",
        )
        offsets = x + (idx_strategy - (len(strategies) - 1) / 2) * bar_width
        bars = plt.bar(offsets, heights, width=bar_width, label=legend_label, color=COLORS.get(strategy_file))
        for bar in bars:
            h = bar.get_height()
            if h > 0:
                plt.text(bar.get_x() + bar.get_width() / 2, h + 0.5, f"{h:.1f}%", ha="center", va="bottom", fontsize=10)

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

    Args:
        all_data (Any): 按策略汇总的完整仿真或统计数据。

    Returns:
        None: 通过实例状态、队列或外部副作用完成处理。
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
    days_list = _filter_plot_days(days_list)
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
                plt.text(bar.get_x() + bar.get_width() / 2, h + 0.3, lbl, ha="center", va="bottom", fontsize=10)

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

    Args:
        all_data (Any): 按策略汇总的完整仿真或统计数据。

    Returns:
        None: 通过实例状态、队列或外部副作用完成处理。
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
    days_list = _filter_plot_days(days_list)
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
            outbound_tasks = [t for t in day_tasks.get(day, []) if _is_outbound_task(t)]
            heights.append(max((t["end_time"] for t in outbound_tasks), default=0) / 3600.0)
        legend_label = _format_dual_avg_label(
            strategy_name,
            list(zip(days_list, heights)),
            lambda x: f"{x:.2f}h",
        )
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
                plt.text(bar.get_x() + bar.get_width() / 2, h + 0.02, f"{h:.2f}", ha="center", va="bottom", fontsize=10)

    plt.xticks(x, [f"Day {d}" for d in days_list])
    plt.xlabel("天数")
    plt.ylabel("最后出库任务完成时间 (h)")
    title = "每日最后出库任务完成时间"
    plt.grid(True, axis="y", linestyle="--", alpha=0.4)
    plt.legend(frameon=False, ncol=len(strategies), loc="upper center", bbox_to_anchor=(0.5, 1.1))
    plt.subplots_adjust(bottom=0.12)
    plt.figtext(0.5, 0.005, title, ha="center", fontsize=14)
    save_fig("completion_times.png")
    plt.show()


def _plot_daily_hourly_throughput(all_data, task_type_label: str, out_name: str, y_label: str, title: str):
    """执行 `_plot_daily_hourly_throughput` 对应的模块处理步骤，并返回该步骤产生的结果。

    Args:
        all_data (Any): 按策略汇总的完整仿真或统计数据。
        task_type_label (str): 供当前处理流程使用的 `task_type_label` 值。
        out_name (str): 供当前处理流程使用的 `out_name` 值。
        y_label (str): 供当前处理流程使用的 `y_label` 值。
        title (str): 供当前处理流程使用的 `title` 值。

    Returns:
        None: 通过实例状态、队列或外部副作用完成处理。
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
    days_list = _filter_plot_days(days_list)
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
            tasks = [t for t in day_tasks.get(day, []) if _match_task_type(t, task_type_label)]
            count = len(tasks)
            last_hours = max((t["end_time"] for t in tasks), default=0) / 3600.0
            throughput = (count / last_hours) if (count > 0 and last_hours > 0) else 0.0
            heights.append(throughput)
        legend_label = _format_dual_avg_label(
            strategy_name,
            list(zip(days_list, heights)),
            lambda x: f"{x:.2f}",
        )
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
                plt.text(bar.get_x() + bar.get_width() / 2, h + 0.02, f"{h:.2f}", ha="center", va="bottom", fontsize=10)

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
    """执行 `plot_outbound_hourly_throughput` 对应的模块处理步骤，并返回该步骤产生的结果。

    Args:
        all_data (Any): 按策略汇总的完整仿真或统计数据。

    Returns:
        None: 通过实例状态、队列或外部副作用完成处理。
    """
    _plot_daily_hourly_throughput(
        all_data=all_data,
        task_type_label="出库",
        out_name="outbound_hourly_throughput.png",
        y_label="日均出库每小时节拍 (任务数/小时)",
        title="每日平均出库每小时节拍",
    )


def plot_inbound_hourly_throughput(all_data):
    """执行 `plot_inbound_hourly_throughput` 对应的模块处理步骤，并返回该步骤产生的结果。

    Args:
        all_data (Any): 按策略汇总的完整仿真或统计数据。

    Returns:
        None: 通过实例状态、队列或外部副作用完成处理。
    """
    _plot_daily_hourly_throughput(
        all_data=all_data,
        task_type_label="入库",
        out_name="inbound_hourly_throughput.png",
        y_label="日均入库每小时节拍 (任务数/小时)",
        title="每日平均入库每小时节拍",
    )


def _collect_task_durations(task_completion_details):
    """遍历相关状态集合，汇总当前流程需要的对象或占用信息。

    Args:
        task_completion_details (Any): 供当前处理流程使用的 `task_completion_details` 值。

    Returns:
        Any: 当前处理流程产生的结果；具体结构由函数摘要说明。
    """
    inbound_durations = []
    outbound_durations = []
    all_durations = []

    for day_tasks in (task_completion_details or {}).values():
        for task in day_tasks:
            duration = task.get("duration")
            if duration is None:
                continue
            all_durations.append(duration)
            if _is_inbound_task(task):
                inbound_durations.append(duration)
            elif _is_outbound_task(task):
                outbound_durations.append(duration)

    return inbound_durations, outbound_durations, all_durations


def _safe_avg(values):
    """执行 `_safe_avg` 对应的模块处理步骤，并返回该步骤产生的结果。

    Args:
        values (Any): 待解析、格式化或计算的原始值集合。

    Returns:
        Any: 当前处理流程产生的结果；具体结构由函数摘要说明。
    """
    return (sum(values) / len(values)) if values else 0.0


def plot_avg_task_duration_by_strategy(all_data):
    """Plot average inbound/outbound/all-task duration for each strategy.

    Args:
        all_data (Any): 按策略汇总的完整仿真或统计数据。

    Returns:
        None: 通过实例状态、队列或外部副作用完成处理。
    """
    from matplotlib.patches import Patch
    plt.figure(figsize=(12, 7))

    strategy_items = list(STRATEGIES.items())
    x = np.arange(len(strategy_items))
    width = 0.22

    inbound_vals = []
    outbound_vals = []
    all_vals = []

    for strategy_file, _strategy_name in strategy_items:
        data = all_data.get(strategy_file, {})
        inbound_durations, outbound_durations, all_durations = _collect_task_durations(
            data.get("task_completion_details", {})
        )
        inbound_vals.append(_safe_avg(inbound_durations))
        outbound_vals.append(_safe_avg(outbound_durations))
        all_vals.append(_safe_avg(all_durations))

    series = [
        ("入库任务", inbound_vals, "#8ecae6", -width),
        ("出库任务", outbound_vals, "#219ebc", 0.0),
        ("全部任务", all_vals, "#023047", width),
    ]

    max_height = max(inbound_vals + outbound_vals + all_vals, default=0.0)
    text_offset = max(max_height * 0.015, 0.1)

    for label, values, color, offset in series:
        bars = plt.bar(x + offset, values, width=width, label=label, color=color)
        for bar, value in zip(bars, values):
            if value > 0:
                plt.text(
                    bar.get_x() + bar.get_width() / 2,
                    value + text_offset,
                    f"{value:.2f}s",
                    ha="center",
                    va="bottom",
                    fontsize=10,
                )

    plt.xticks(x, [strategy_name for _, strategy_name in strategy_items])
    plt.ylabel("平均任务耗时 (s)")
    plt.grid(True, axis="y", linestyle="--", alpha=0.4)
    plt.legend(frameon=False, ncol=3, loc="upper center", bbox_to_anchor=(0.5, 1.1))
    plt.subplots_adjust(bottom=0.12)
    plt.figtext(0.5, 0.005, "各策略入库/出库/全部任务平均耗时", ha="center", fontsize=14)
    save_fig("avg_task_duration_by_strategy.png")
    plt.show()

def plot_relocation_counts_by_day(all_data):
    """按天分组展示移库数量。

    Args:
        all_data (Any): 按策略汇总的完整仿真或统计数据。

    Returns:
        None: 通过实例状态、队列或外部副作用完成处理。
    """
    plt.figure(figsize=(12, 7))
    strategies = list(STRATEGIES.items())
    if DAYS_FILTER:
        days_list = sorted(DAYS_FILTER)
    else:
        day_set = set()
        for data in all_data.values():
            # 只包含有任务的天数（有task_completion_details或tasks_completed_per_day数据）
            day_set |= set(data.get("relocation_counts_by_day", {}).keys())
        days_list = sorted(day_set)

    # 过滤掉没有任何任务的天数
    valid_days = []
    for day in days_list:
        has_tasks = False
        for data in all_data.values():
            task_details = data.get("task_completion_details", {})
            tasks_per_day = data.get("tasks_completed_per_day", {})
            if day in task_details and len(task_details[day]) > 0:
                has_tasks = True
                break
            if day in tasks_per_day and tasks_per_day[day].get("total", 0) > 0:
                has_tasks = True
                break
        if has_tasks:
            valid_days.append(day)

    days_list = _filter_plot_days(valid_days)

    if not days_list:
        print("无按天移库数据，跳过图表")
        return

    x = np.arange(len(days_list))
    total_width = 0.75
    bar_width = total_width / len(strategies)

    for idx_strategy, (strategy_file, strategy_name) in enumerate(strategies):
        reloc_day = all_data.get(strategy_file, {}).get("relocation_counts_by_day", {})
        heights = [reloc_day.get(day, 0) for day in days_list]
        legend_label = _format_dual_avg_label(
            strategy_name,
            list(zip(days_list, heights)),
            lambda x: f"{x:.2f}",
        )
        offsets = x + (idx_strategy - (len(strategies) - 1) / 2) * bar_width
        bars = plt.bar(offsets, heights, width=bar_width, label=legend_label, color=COLORS.get(strategy_file))
        for bar in bars:
            h = bar.get_height()
            if h > 0:
                plt.text(bar.get_x() + bar.get_width() / 2, h + 0.1, f"{h}", ha="center", va="bottom", fontsize=10)

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

    Args:
        all_data (Any): 按策略汇总的完整仿真或统计数据。

    Returns:
        None: 通过实例状态、队列或外部副作用完成处理。
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
    days_list = _filter_plot_days(days_list)
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
        legend_label = _format_dual_avg_label(
            strategy_name,
            list(zip(days_list, heights)),
            lambda x: f"{x:.2f}%",
        )
        offsets = x + (idx_strategy - (len(strategies) - 1) / 2) * bar_width
        bars = plt.bar(offsets, heights, width=bar_width, label=legend_label, color=COLORS.get(strategy_file))
        for bar in bars:
            h = bar.get_height()
            if h > 0:
                plt.text(bar.get_x() + bar.get_width() / 2, h + 0.5, f"{h:.1f}%", ha="center", va="bottom", fontsize=10)

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


def plot_avg_invalid_idle_by_day(all_data):
    """Plot daily average invalid-idle time per aisle.

    Args:
        all_data (Any): 按策略汇总的完整仿真或统计数据。

    Returns:
        None: 通过实例状态、队列或外部副作用完成处理。
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
    days_list = _filter_plot_days(days_list)
    if not days_list:
        print("无无效等待数据，跳过图表")
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
            heights.append(avg_entry.get("invalid_idle_time", 0))
        legend_label = _format_dual_avg_label(
            strategy_name,
            list(zip(days_list, heights)),
            lambda x: f"{x:.2f}s",
        )
        offsets = x + (idx_strategy - (len(strategies) - 1) / 2) * bar_width
        bars = plt.bar(offsets, heights, width=bar_width, label=legend_label, color=COLORS.get(strategy_file))
        for bar in bars:
            h = bar.get_height()
            if h > 0:
                plt.text(bar.get_x() + bar.get_width() / 2, h + max(heights) * 0.01 if max(heights, default=0) > 0 else 0.1, f"{h:.1f}s", ha="center", va="bottom", fontsize=10)

    plt.xticks(x, [f"Day {d}" for d in days_list])
    plt.xlabel("天数")
    plt.ylabel("平均无效等待时间 (s)")
    title = "每日平均巷道无效等待时间"
    plt.grid(True, axis="y", linestyle="--", alpha=0.4)
    plt.legend(frameon=False, ncol=len(strategies), loc="upper center", bbox_to_anchor=(0.5, 1.1))
    plt.subplots_adjust(bottom=0.12)
    plt.figtext(0.5, 0.005, title, ha="center", fontsize=14)
    save_fig("avg_invalid_idle_by_day.png")
    plt.show()


def plot_aisle_used_time_std_by_day(all_data):
    """Show daily std of per-aisle busy time.

    Args:
        all_data (Any): 按策略汇总的完整仿真或统计数据。

    Returns:
        None: 通过实例状态、队列或外部副作用完成处理。
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
    days_list = _filter_plot_days(days_list)
    if not days_list:
        print("无巷道运行时间标准差数据，跳过图表")
        return

    x = np.arange(len(days_list))
    total_width = 0.75
    bar_width = total_width / len(strategies)

    for idx_strategy, (strategy_file, strategy_name) in enumerate(strategies):
        day_stats = all_data.get(strategy_file, {}).get("aisle_busy_times", {})
        heights = []
        for day in days_list:
            stats = day_stats.get(day, {})
            numeric_aisles = [k for k in stats.keys() if isinstance(k, (int, float))]
            used_times = [stats[a]["total_time"] for a in numeric_aisles]
            if not used_times:
                heights.append(0)
                continue
            mean_val = sum(used_times) / len(used_times)
            variance = sum((v - mean_val) ** 2 for v in used_times) / len(used_times)
            heights.append(variance ** 0.5)

        legend_label = _format_dual_avg_label(
            strategy_name,
            list(zip(days_list, heights)),
            lambda x: f"{x:.2f}s",
        )

        offsets = x + (idx_strategy - (len(strategies) - 1) / 2) * bar_width
        bars = plt.bar(offsets, heights, width=bar_width, label=legend_label, color=COLORS.get(strategy_file))
        for bar in bars:
            h = bar.get_height()
            if h > 0:
                plt.text(bar.get_x() + bar.get_width() / 2, h + 0.02, f"{h:.2f}", ha="center", va="bottom", fontsize=10)

    plt.xticks(x, [f"Day {d}" for d in days_list])
    plt.xlabel("天数")
    plt.ylabel("巷道运行时间标准差 (s)")
    title = "每日巷道运行时间标准差"
    plt.grid(True, axis="y", linestyle="--", alpha=0.4)
    plt.legend(frameon=False, ncol=len(strategies), loc="upper center", bbox_to_anchor=(0.5, 1.1))
    plt.subplots_adjust(bottom=0.12)
    plt.figtext(0.5, 0.005, title, ha="center", fontsize=14)
    save_fig("aisle_used_time_std_by_day.png")
    plt.show()


def plot_aisle_load_max_deviation_by_day(all_data):
    """Plot daily max relative deviation from average aisle load.

    Args:
        all_data (Any): 按策略汇总的完整仿真或统计数据。

    Returns:
        None: 通过实例状态、队列或外部副作用完成处理。
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
    days_list = _filter_plot_days(days_list)
    if not days_list:
        print("No aisle load-balance data, skip chart")
        return

    x = np.arange(len(days_list))
    total_width = 0.75
    bar_width = total_width / len(strategies)

    for idx_strategy, (strategy_file, strategy_name) in enumerate(strategies):
        day_stats = all_data.get(strategy_file, {}).get("aisle_busy_times", {})
        heights = []
        for day in days_list:
            stats = day_stats.get(day, {})
            balance_stats = _calculate_aisle_load_balance_stats(stats) if stats else None
            heights.append((balance_stats["max_deviation_ratio"] * 100.0) if balance_stats else 0.0)

        legend_label = _format_dual_avg_label(
            strategy_name,
            list(zip(days_list, heights)),
            lambda x: f"{x:.2f}%",
        )

        offsets = x + (idx_strategy - (len(strategies) - 1) / 2) * bar_width
        bars = plt.bar(offsets, heights, width=bar_width, label=legend_label, color=COLORS.get(strategy_file))
        for bar in bars:
            h = bar.get_height()
            if h > 0:
                plt.text(bar.get_x() + bar.get_width() / 2, h + 0.3, f"{h:.1f}%", ha="center", va="bottom", fontsize=10)

    plt.axhline(10.0, color="red", linestyle="--", linewidth=1.5, label="10% 阈值")
    plt.xticks(x, [f"Day {d}" for d in days_list])
    plt.xlabel("天数")
    plt.ylabel("相对平均负载的最大偏差 (%)")
    title = "每日巷道负载均衡最大偏差"
    plt.grid(True, axis="y", linestyle="--", alpha=0.4)
    plt.legend(frameon=False, ncol=min(len(strategies) + 1, 3), loc="upper center", bbox_to_anchor=(0.5, 1.12))
    plt.subplots_adjust(bottom=0.12)
    plt.figtext(0.5, 0.005, title, ha="center", fontsize=14)
    save_fig("aisle_load_max_deviation_by_day.png")
    plt.show()


def plot_aisle_task_count_max_deviation_by_day(all_data):
    """Plot daily max relative deviation from average aisle total task counts.

    Args:
        all_data (Any): 按策略汇总的完整仿真或统计数据。

    Returns:
        None: 通过实例状态、队列或外部副作用完成处理。
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
    days_list = _filter_plot_days(days_list)
    if not days_list:
        print("No aisle task-count balance data, skip chart")
        return

    x = np.arange(len(days_list))
    total_width = 0.75
    bar_width = total_width / len(strategies)

    for idx_strategy, (strategy_file, strategy_name) in enumerate(strategies):
        day_tasks = all_data.get(strategy_file, {}).get("task_completion_details", {})
        heights = []
        for day in days_list:
            balance_stats = _calculate_aisle_task_count_balance_stats(day_tasks.get(day, []))
            heights.append((balance_stats["max_deviation_ratio"] * 100.0) if balance_stats else 0.0)

        legend_label = _format_dual_avg_label(
            strategy_name,
            list(zip(days_list, heights)),
            lambda x: f"{x:.2f}%",
        )
        offsets = x + (idx_strategy - (len(strategies) - 1) / 2) * bar_width
        bars = plt.bar(offsets, heights, width=bar_width, label=legend_label, color=COLORS.get(strategy_file))
        for bar in bars:
            h = bar.get_height()
            if h > 0:
                plt.text(bar.get_x() + bar.get_width() / 2, h + 0.3, f"{h:.1f}%", ha="center", va="bottom", fontsize=10)

    plt.axhline(10.0, color="red", linestyle="--", linewidth=1.5, label="10% 阈值")
    plt.xticks(x, [f"Day {d}" for d in days_list])
    plt.xlabel("天数")
    plt.ylabel("巷道任务数量相对平均值的最大偏差 (%)")
    title = "每日巷道任务数量最大偏差"
    plt.grid(True, axis="y", linestyle="--", alpha=0.4)
    plt.legend(frameon=False, ncol=min(len(strategies) + 1, 3), loc="upper center", bbox_to_anchor=(0.5, 1.12))
    plt.subplots_adjust(bottom=0.12)
    plt.figtext(0.5, 0.005, title, ha="center", fontsize=14)
    save_fig("aisle_task_count_max_deviation_by_day.png")
    plt.show()


def plot_aisle_occupancy_breakdown_by_day(all_data):
    """按天展示平均巷道任务占用、移库占用和总占用率。

    Args:
        all_data (Any): 按策略汇总的完整仿真或统计数据。

    Returns:
        None: 通过实例状态、队列或外部副作用完成处理。
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
    days_list = _filter_plot_days(days_list)
    if not days_list:
        print("无巷道占用分解数据，跳过图表")
        return

    x = np.arange(len(days_list))
    total_width = 0.75
    bar_width = total_width / len(strategies)

    for idx_strategy, (strategy_file, strategy_name) in enumerate(strategies):
        day_stats = all_data.get(strategy_file, {}).get("aisle_busy_times", {})
        task_vals = []
        reloc_vals = []
        total_vals = []
        for day in days_list:
            stats = day_stats.get(day, {})
            avg_entry = stats.get("avg", {})
            sim_end = avg_entry.get("simulation_end_time", 0) or 0
            task_busy = avg_entry.get("task_busy_time", 0) or 0
            relocation = avg_entry.get("relocation_time", 0) or 0
            total = avg_entry.get("total_time", 0) or 0
            if sim_end > 0:
                task_vals.append(task_busy / sim_end * 100)
                reloc_vals.append(relocation / sim_end * 100)
                total_vals.append(total / sim_end * 100)
            else:
                task_vals.append(0)
                reloc_vals.append(0)
                total_vals.append(0)

        legend_label = _format_dual_avg_label(
            strategy_name,
            list(zip(days_list, total_vals)),
            lambda x: f"{x:.2f}%",
        )

        offsets = x + (idx_strategy - (len(strategies) - 1) / 2) * bar_width
        base_color = COLORS.get(strategy_file)
        bars_task = plt.bar(
            offsets,
            task_vals,
            width=bar_width,
            label=legend_label,
            color=base_color,
            alpha=0.85,
        )
        plt.bar(
            offsets,
            reloc_vals,
            width=bar_width,
            bottom=task_vals,
            color=base_color,
            alpha=0.35,
            hatch="//",
        )
        for bar, total, task, reloc in zip(bars_task, total_vals, task_vals, reloc_vals):
            if total > 0:
                plt.text(
                    bar.get_x() + bar.get_width() / 2,
                    task + reloc + 0.5,
                    f"{total:.1f}%",
                    ha="center",
                    va="bottom",
                    fontsize=9,
                )

    from matplotlib.patches import Patch
    style_legend = [
        Patch(facecolor="#666666", alpha=0.85, label="任务占用"),
        Patch(facecolor="#666666", alpha=0.35, hatch="//", label="移库占用"),
    ]

    plt.xticks(x, [f"Day {d}" for d in days_list])
    plt.xlabel("天数")
    plt.ylabel("平均巷道占用率 (%)")
    title = "每日平均巷道占用分解"
    plt.grid(True, axis="y", linestyle="--", alpha=0.4)
    leg1 = plt.legend(frameon=False, ncol=len(strategies), loc="upper center", bbox_to_anchor=(0.5, 1.14))
    plt.gca().add_artist(leg1)
    plt.legend(handles=style_legend, frameon=False, ncol=2, loc="upper center", bbox_to_anchor=(0.5, 1.06))
    plt.subplots_adjust(bottom=0.12, top=0.82)
    plt.figtext(0.5, 0.005, title, ha="center", fontsize=14)
    save_fig("aisle_occupancy_breakdown_by_day.png")
    plt.show()

def plot_optimization_solve_time_buckets_opt_only(all_data):
    """绘制仅包含 opt 日志的优化器求解耗时分桶柱状图。

    Args:
        all_data (Any): 按策略汇总的完整仿真或统计数据。

    Returns:
        None: 通过实例状态、队列或外部副作用完成处理。
    """
    opt_data = _get_opt_only_strategy_data(all_data)
    if not opt_data:
        print("无 opt 日志数据，跳过优化器求解耗时柱状图")
        return

    buckets = ["0-1s", "1-2s", "2s以上"]
    strategy_labels = []
    strategy_names = []
    bucket_counts_by_strategy = []
    for strategy_file in opt_data.keys():
        strategy_data = opt_data[strategy_file]
        strategy_buckets = strategy_data.get("optimization_solve_time_buckets", {})
        solve_times = strategy_data.get("optimization_solve_times", [])
        base_name = STRATEGIES.get(strategy_file, strategy_file)
        if solve_times:
            avg_solve = sum(solve_times) / len(solve_times)
            strategy_names.append(f"{base_name}：{avg_solve:.1f}s")
        else:
            strategy_names.append(base_name)
        bucket_counts_by_strategy.append([strategy_buckets.get(bucket, 0) for bucket in buckets])

    if not any(count > 0 for counts in bucket_counts_by_strategy for count in counts):
        print("无优化器求解耗时记录，跳过优化器求解耗时柱状图")
        return

    strategy_display_names = []
    for strategy_file in opt_data.keys():
        strategy_data = opt_data[strategy_file]
        solve_times = strategy_data.get("optimization_solve_times", [])
        base_name = STRATEGIES.get(strategy_file, strategy_file)
        if solve_times:
            avg_solve = sum(solve_times) / len(solve_times)
            strategy_display_names.append(f"{base_name}: {avg_solve:.3f}s")
        else:
            strategy_display_names.append(base_name)

    plt.figure(figsize=(10, 5))
    x = np.arange(len(buckets))
    strategy_files = list(opt_data.keys())
    width = 0.8 / max(1, len(strategy_files))
    for idx, (strategy_file, strategy_name, counts) in enumerate(zip(strategy_files, strategy_display_names, bucket_counts_by_strategy)):
        offsets = x + (idx - (len(strategy_files) - 1) / 2) * width
        bars = plt.bar(
            offsets,
            counts,
            width=width,
            label=strategy_name,
            color=COLORS.get(strategy_file, "#0066cc"),
        )
        for bar, count in zip(bars, counts):
            if count > 0:
                plt.text(bar.get_x() + bar.get_width() / 2, count + 0.1, str(count), ha="center", va="bottom", fontsize=8)

    plt.xticks(x, buckets)
    plt.ylabel("次数")
    plt.title("优化器求解耗时分桶")
    plt.grid(True, axis="y", linestyle="--", alpha=0.4)
    plt.legend(frameon=False, ncol=len(strategy_display_names), loc="upper center", bbox_to_anchor=(0.5, 1.08))
    plt.figtext(0.5, 0.02, "时间", ha="center", fontsize=10)
    plt.tight_layout()
    save_fig("optimization_solve_time_buckets_opt_only.png")
    plt.show()


def print_summary_statistics(all_data):
    """执行 `print_summary_statistics` 对应的模块处理步骤，并返回该步骤产生的结果。

    Args:
        all_data (Any): 按策略汇总的完整仿真或统计数据。

    Returns:
        None: 通过实例状态、队列或外部副作用完成处理。
    """
    print("\n=== Summary ===")
    print(f"{'Strategy':<30} {'Total Tasks':<12} {'Relocations':<12} {'Start Slot Avg':<14} {'Start Beam Avg':<14} {'Final Slot Rate':<15} {'Final Beam Rate (solo)':<22}")
    print("-" * 125)
    for strategy_file, data in all_data.items():
        total_tasks = sum(v.get("total", 0) for v in data.get("tasks_completed_per_day", {}).values())
        relocation_count = data.get("total_relocations", 0)
        start_rates = data.get("pairing_start_by_day", {})
        if start_rates:
            start_slot_avg = sum(v["slot"] for v in start_rates.values()) / len(start_rates)
            start_beam_avg = sum(v["beam_with_solo"] for v in start_rates.values()) / len(start_rates)
        else:
            start_slot_avg = 0
            start_beam_avg = 0
        times = [t for t in data.get("pairing_rates", {}).keys() if isinstance(t, (int, float))]
        if times:
            last_time = max(times)
            last_pairing = data["pairing_rates"][last_time]
            slot_rate = last_pairing["slot"]
            beam_with_solo_rate = last_pairing["beam_with_solo"]
        else:
            slot_rate = beam_with_solo_rate = 0
        solve_times = data.get("optimization_solve_times", [])
        solve_buckets = data.get("optimization_solve_time_buckets", {"0-1s": 0, "1-2s": 0, "2s以上": 0})
        if solve_times:
            avg_solve = sum(solve_times) / len(solve_times)
            print(f"  优化器求解耗时: {len(solve_times)} 次，平均 {avg_solve:.3f}s")
            print(f"    分桶: 0-1s={solve_buckets['0-1s']} 1-2s={solve_buckets['1-2s']} 2s以上={solve_buckets['2s以上']}")
        else:
            print("  优化器求解耗时: 无记录")

        print(
            f"{STRATEGIES[strategy_file]:<30} {total_tasks:<12} {relocation_count:<12} "
            f"{start_slot_avg*100:>12.2f}% {start_beam_avg*100:>12.2f}% "
            f"{slot_rate*100:>13.2f}% {beam_with_solo_rate*100:>19.2f}%"
        )


def print_aisle_busy_times(all_data):
    """打印每个策略下各巷道的忙碌时间。

    Args:
        all_data (Any): 按策略汇总的完整仿真或统计数据。

    Returns:
        None: 通过实例状态、队列或外部副作用完成处理。
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
            print(f"  [Day {day}] 最后一个出库任务完成时间: {simulation_end_time:.1f}s")

            inbound_data = {}
            outbound_data = {}
            relocation_data = {}
            task_busy_data = {}
            overlap_data = {}
            invalid_idle_data = {}
            total_data = {}
            for aisle in sorted_aisles:
                stats = aisle_busy_times[aisle]
                inbound_time = stats["inbound_time"]
                outbound_time = stats["outbound_time"]
                relocation_time = stats.get("relocation_time", 0)
                task_busy_time = stats.get("task_busy_time", inbound_time + outbound_time)
                overlap_time = stats.get("relocation_overlap_time", 0)
                invalid_idle_time = stats.get("invalid_idle_time", 0)
                total_time = stats["total_time"]
                inbound_pct = inbound_time / simulation_end_time * 100 if simulation_end_time else 0
                outbound_pct = outbound_time / simulation_end_time * 100 if simulation_end_time else 0
                relocation_pct = relocation_time / simulation_end_time * 100 if simulation_end_time else 0
                task_busy_pct = task_busy_time / simulation_end_time * 100 if simulation_end_time else 0
                overlap_pct = overlap_time / simulation_end_time * 100 if simulation_end_time else 0
                invalid_idle_pct = invalid_idle_time / simulation_end_time * 100 if simulation_end_time else 0
                total_pct = total_time / simulation_end_time * 100 if simulation_end_time else 0
                inbound_data[aisle] = f"{inbound_time:.1f}s ({inbound_pct:.1f}%)"
                outbound_data[aisle] = f"{outbound_time:.1f}s ({outbound_pct:.1f}%)"
                relocation_data[aisle] = f"{relocation_time:.1f}s ({relocation_pct:.1f}%)"
                task_busy_data[aisle] = f"{task_busy_time:.1f}s ({task_busy_pct:.1f}%)"
                overlap_data[aisle] = f"{overlap_time:.1f}s ({overlap_pct:.1f}%)"
                invalid_idle_data[aisle] = f"{invalid_idle_time:.1f}s ({invalid_idle_pct:.1f}%)"
                total_data[aisle] = f"{total_time:.1f}s ({total_pct:.1f}%)"

            print("    巷道入库耗时:", inbound_data)
            print("    巷道出库耗时:", outbound_data)
            print("    巷道任务占用:", task_busy_data)
            print("    巷道无效等待:", invalid_idle_data)
            print("    巷道总耗时:", total_data)


def print_aisle_used_time_std(all_data):
    """打印各策略的巷道使用时间标准差（按天）与均值。

    Args:
        all_data (Any): 按策略汇总的完整仿真或统计数据。

    Returns:
        None: 通过实例状态、队列或外部副作用完成处理。
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


def print_aisle_load_balance(all_data):
    """Print max relative deviation from average aisle load by day.

    Args:
        all_data (Any): 按策略汇总的完整仿真或统计数据。

    Returns:
        None: 通过实例状态、队列或外部副作用完成处理。
    """
    print("\n=== 巷道负载均衡偏差 ===")
    print("指标: max(|L_i - L_avg| / L_avg), 其中 L_i 为巷道总占用时间")
    print("目标: 最大偏差率 <= 10%")
    for strategy_file, data in all_data.items():
        all_days_stats = data.get("aisle_busy_times", {})
        if not all_days_stats:
            continue
        per_day_dev = []
        print(f"\n{STRATEGIES[strategy_file]}:")
        for day in sorted(all_days_stats.keys()):
            balance_stats = _calculate_aisle_load_balance_stats(all_days_stats[day])
            if not balance_stats:
                continue
            dev_pct = balance_stats["max_deviation_ratio"] * 100.0
            per_day_dev.append(dev_pct)
            status = "PASS" if balance_stats["within_10pct"] else "FAIL"
            print(f"  Day {day}: {dev_pct:.2f}% ({status})")
        if per_day_dev:
            avg_dev = sum(per_day_dev) / len(per_day_dev)
            print(f"  平均最大偏差率: {avg_dev:.2f}%")


def print_aisle_task_count_balance(all_data):
    """Print max relative deviation from average aisle total task counts by day.

    Args:
        all_data (Any): 按策略汇总的完整仿真或统计数据。

    Returns:
        None: 通过实例状态、队列或外部副作用完成处理。
    """
    print("\n=== 巷道任务次数均衡偏差 ===")
    print("指标: max(|N_i - N_avg| / N_avg), 其中 N_i 为巷道总任务次数(入库+出库)，输出为偏差率而非原始次数")
    print("目标: 最大偏差率 <= 10%")
    for strategy_file, data in all_data.items():
        all_day_tasks = data.get("task_completion_details", {})
        if not all_day_tasks:
            continue
        per_day_dev = []
        print(f"\n{STRATEGIES[strategy_file]}:")
        for day in sorted(all_day_tasks.keys()):
            balance_stats = _calculate_aisle_task_count_balance_stats(all_day_tasks[day])
            if not balance_stats:
                continue
            dev_pct = balance_stats["max_deviation_ratio"] * 100.0
            per_day_dev.append((day, dev_pct))
            status = "PASS" if balance_stats["within_10pct"] else "FAIL"
            print(f"  Day {day}: {dev_pct:.2f}% ({status})")
        if per_day_dev:
            values = [v for _, v in per_day_dev]
            values_excl_day1 = [v for d, v in per_day_dev if d != 1]
            avg_all = sum(values) / len(values)
            avg_excl = (sum(values_excl_day1) / len(values_excl_day1)) if values_excl_day1 else avg_all
            print(f"  平均最大偏差率: {avg_all:.2f}% ({avg_excl:.2f}%)")


def print_aisle_task_counts(all_data):
    """打印每个策略下各巷道的入/出库任务次数。

    Args:
        all_data (Any): 按策略汇总的完整仿真或统计数据。

    Returns:
        None: 通过实例状态、队列或外部副作用完成处理。
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
                if _is_inbound_task(task):
                    aisle_counts[aisle]["inbound"] += 1
                elif _is_outbound_task(task):
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


def print_relocation_counts_by_day(all_data):
    """打印每个策略下按天的移库数量。

    Args:
        all_data (Any): 按策略汇总的完整仿真或统计数据。

    Returns:
        None: 通过实例状态、队列或外部副作用完成处理。
    """
    print("\n=== 各策略按天移库数量 ===")

    # 收集所有有任务的天数
    days_with_tasks = set()
    for data in all_data.values():
        task_details = data.get("task_completion_details", {})
        tasks_per_day = data.get("tasks_completed_per_day", {})
        for day, tasks in task_details.items():
            if len(tasks) > 0:
                days_with_tasks.add(day)
        for day, info in tasks_per_day.items():
            if info.get("total", 0) > 0:
                days_with_tasks.add(day)

    for strategy_file, data in all_data.items():
        print(f"\n{STRATEGIES[strategy_file]}:")
        reloc_by_day = data.get("relocation_counts_by_day", {})
        if not reloc_by_day:
            print("  无移库数据")
            continue

        # 只保留有任务的天数
        filtered_reloc = {day: count for day, count in reloc_by_day.items() if day in days_with_tasks}

        if not filtered_reloc:
            print("  无移库数据（已过滤无任务天数）")
            continue

        days = sorted(filtered_reloc.keys())
        info = {day: f"{filtered_reloc[day]}次" for day in days}
        print("  移库数量(天):", info)


def parse_days_filter(days_text: str):
    """解析原始输入，提取本模块后续判断所需的结构化字段。

    输入：days_text（str）

    Args:
        days_text (str): 供当前处理流程使用的 `days_text` 值。

    Returns:
        Any: 当前处理流程产生的结果；具体结构由函数摘要说明。
    """
    if not days_text:
        return set()
    return {int(part.strip()) for part in days_text.split(",") if part.strip()}


def main():
    """作为脚本入口，解析运行参数并按既定顺序调用本文件的主要处理流程。

    输入：无显式业务输入；依赖实例字段或模块配置。

    Args:
        None: 无显式业务参数；使用实例状态或模块配置。

    Returns:
        None: 通过实例状态、队列或外部副作用完成处理。
    """
    if plt is None:
        print("错误: matplotlib 未安装，无法生成可视化图表。")
        return

    parser = argparse.ArgumentParser(description="可视化分析仓储策略仿真结果")
    # 选择配对率口径，决定后续汇总时是否将单梁任务计入分母和分子。
    parser.add_argument(
        "--rate-type",
        choices=["slot", "beam_without_solo", "beam_with_solo"],
        default="beam_with_solo",
        help="选择配对率类型进行可视化",
    )
    # 输入策略日志的根目录，脚本会递归读取其中符合命名规则的结果文件。
    parser.add_argument("--log-dir", default="logs", help="日志文件目录")
    # 图表、汇总文件和策略对比结果的输出目录。
    parser.add_argument("--out-dir", default="visualization/compare", help="可视化输出目录")
    args = parser.parse_args()

    global OUTPUT_DIR
    OUTPUT_DIR = Path(args.out_dir)

    all_data = load_all_data(args.log_dir)
    if not all_data:
        print("错误: 没有找到有效的日志数据")
        return

    print_summary_statistics(all_data)
    plot_optimization_solve_time_buckets_opt_only(all_data)
    print_aisle_busy_times(all_data)
    print_aisle_used_time_std(all_data)
    print_aisle_load_balance(all_data)
    print_aisle_task_count_balance(all_data)
    print_aisle_task_counts(all_data)
    print_relocation_counts_by_day(all_data)
    plot_pairing_rates_over_time(all_data, args.rate_type)
    plot_pairing_start_by_day(all_data)
    plot_tasks_completed_per_day(all_data)
    plot_completion_times(all_data)
    plot_outbound_hourly_throughput(all_data)
    plot_inbound_hourly_throughput(all_data)
    plot_avg_task_duration_by_strategy(all_data)
    plot_relocation_counts_by_day(all_data)
    plot_avg_utilization_by_day(all_data)
    plot_avg_invalid_idle_by_day(all_data)
    plot_aisle_used_time_std_by_day(all_data)
    plot_aisle_load_max_deviation_by_day(all_data)
    plot_aisle_task_count_max_deviation_by_day(all_data)
    print("\n图表已生成并保存到当前目录")


if __name__ == "__main__":
    main()
