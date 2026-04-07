"""
Visualize daily runs produced by run_daily.py.

Reads logs under logs/daily/<YYYYMMDD[_tag]>/<config>.txt and compares:
- Overall outbound completion ratio
- Start pairing rate (beam with solo)
- End time (hours)

Usage:
  python visualize_daily_results.py
  python visualize_daily_results.py --dates 20251012,20251017
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Dict, Optional, Tuple

import matplotlib.pyplot as plt
import numpy as np
import csv


STRATEGIES = {
    "base-base-heu": "Baseline",
    "base-base-opt": "[+ Scheduling]",
    "prop-prop-heu": "[+ Allocation]",
    "prop-prop-opt": "[+ Allocation + Scheduling]",
}

COLORS = {
    "base-base-heu": "#c0c0c0",
    "base-base-opt": "#000000",
    "prop-prop-heu": "#a6c8ff",
    "prop-prop-opt": "#0066cc",
}

OUTPUT_DIR = Path("visualization/daily")


def save_fig(filename: str) -> None:
    out_path = OUTPUT_DIR / Path(filename).name
    out_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(out_path, dpi=300, bbox_inches="tight")
    print(f"[INFO] saved {out_path}")

SELECT_DATES = []
START_DATE = "20251012"
END_DATE = "20251030"
# Match run_daily suffix: "-lam{POISSON_X}".
POISSON_X = 100  # [50,70,100,120]


def _extract_date_dir(dir_name: str) -> Optional[str]:
    match = re.search(r"(\d{8})", dir_name)
    return match.group(1) if match else None


def _parse_completion(log_text: str) -> Dict[int, Tuple[int, int]]:
    results: Dict[int, Tuple[int, int]] = {}
    patterns = [
        r"\[INFO\]\[DAY\s+\d+\]\s*产线(\d+)\s+当日计划已完成：(\d+)/(\d+)\s+组",
        r"\[WARN\]\[DAY\s+\d+\]\s*产线(\d+)\s+当日计划未完成：已完成\s+(\d+)/(\d+)\s+组",
    ]
    for pat in patterns:
        for m in re.finditer(pat, log_text):
            pl = int(m.group(1))
            done = int(m.group(2))
            total = int(m.group(3))
            results[pl] = (done, total)
    return results


def _parse_pairing_end(log_text: str) -> Dict[str, float]:
    # Example:
    # [DAY 1 结束] 货位配对率: 67/81 = 82.72%; 梁配对率(不含solo): 134/274 = 48.91%; 梁配对率(含solo): 198/274 = 72.26%
    pat = (
        r"\[DAY\s+\d+\s+结束\]\s+货位配对率:\s+\d+/\d+\s+=\s+([\d.]+)%.*?"
        r"梁配对率\(不含solo\):\s+\d+/\d+\s+=\s+([\d.]+)%.*?"
        r"梁配对率\(含solo\):\s+\d+/\d+\s+=\s+([\d.]+)%"
    )
    m = re.search(pat, log_text)
    if not m:
        return {}
    return {
        "slot": float(m.group(1)) / 100.0,
        "beam_without_solo": float(m.group(2)) / 100.0,
        "beam_with_solo": float(m.group(3)) / 100.0,
    }


def _parse_pairing_start(log_text: str) -> Dict[str, float]:
    # Example:
    # [DAY 1 开始] 货位配对率: 67/81 = 82.72%; 梁配对率(不含solo): 134/274 = 48.91%; 梁配对率(含solo): 198/274 = 72.26%
    pat_new = (
        r"\[DAY\s+\d+\s+开始\]\s+货位配对率:\s+\d+/\d+\s+=\s+([\d.]+)%.*?"
        r"梁配对率\(不含solo\):\s+\d+/\d+\s+=\s+([\d.]+)%.*?"
        r"梁配对率\(含solo\):\s+\d+/\d+\s+=\s+([\d.]+)%"
    )
    m = re.search(pat_new, log_text)
    if m:
        return {
            "slot": float(m.group(1)) / 100.0,
            "beam_without_solo": float(m.group(2)) / 100.0,
            "beam_with_solo": float(m.group(3)) / 100.0,
        }
    pat_old = (
        r"\[DAY\s+\d+\s+开始\]\s+配对率\(旧，不含solo\):\s*([\d.]+)%.*?"
        r"梁配对率\(不含solo\):\s*([\d.]+)%.*?"
        r"梁配对率\(含solo\):\s*([\d.]+)%"
    )
    m = re.search(pat_old, log_text)
    if not m:
        return {}
    return {
        "slot": float(m.group(1)) / 100.0,
        "beam_without_solo": float(m.group(2)) / 100.0,
        "beam_with_solo": float(m.group(3)) / 100.0,
    }


def _parse_end_hours(log_text: str) -> Optional[float]:
    m = re.search(r"\[DAY\s+\d+\]\s+日内结束，当前时间\s+([\d.]+)\s+小时", log_text)
    if not m:
        return None
    return float(m.group(1))


def _parse_relocation_count(log_text: str) -> Optional[int]:
    matches = re.findall(r"移库数量:\s*(\d+)", log_text)
    if not matches:
        return None
    return int(matches[-1])

def _parse_relocation_intervals(log_text: str):
    pattern = r"\[移库占用\]\s+巷道\s+(\d+)\s+在时间\s+([\d.]+)s\s+到\s+([\d.]+)s"
    intervals = []
    for m in re.finditer(pattern, log_text):
        intervals.append(
            {
                "aisle": int(m.group(1)),
                "start": float(m.group(2)),
                "end": float(m.group(3)),
            }
        )
    return intervals


def _parse_task_details(log_text: str):
    pattern = (
        r"第\s*(\d+)\s*个(入库|出库)任务\s+([\w\-]+)\s+完成"
        r"\s*(?:\(巷道\s+(\d+)\))?.*?起止\s+([\d.]+)s~([\d.]+)s"
    )
    tasks = []
    for m in re.finditer(pattern, log_text):
        aisle = int(m.group(4)) if m.group(4) else None
        tasks.append(
            {
                "type": m.group(2),
                "id": m.group(3),
                "aisle": aisle,
                "start": float(m.group(5)),
                "end": float(m.group(6)),
            }
        )
    return tasks

def _find_overlaps(tasks):
    overlaps = []
    aisle_map: Dict[int, list] = {}
    for t in tasks:
        aisle = t["aisle"]
        if aisle is None:
            continue
        aisle_map.setdefault(aisle, []).append(t)
    for aisle, items in aisle_map.items():
        seen = set()
        unique_items = []
        for t in items:
            key = (t["start"], t["end"], t["type"], t.get("id"))
            if key in seen:
                continue
            seen.add(key)
            unique_items.append(t)
        items = sorted(unique_items, key=lambda x: x["start"])
        prev = None
        for curr in items:
            if prev and curr["start"] < prev["end"]:
                overlaps.append((aisle, prev, curr))
            if prev is None or curr["end"] > prev["end"]:
                prev = curr
    return overlaps


def _calculate_avg_utilization(tasks) -> Optional[float]:
    if not tasks:
        return None
    outbound_end = [t["end"] for t in tasks if t["type"] == "出库"]
    simulation_end = max(outbound_end) if outbound_end else max(t["end"] for t in tasks)
    if simulation_end <= 0:
        return None
    aisle_intervals: Dict[int, list] = {}
    for t in tasks:
        if t["end"] > simulation_end:
            continue
        if t["aisle"] is None:
            continue
        aisle_intervals.setdefault(t["aisle"], []).append((t["start"], t["end"]))
    if not aisle_intervals:
        return None
    utilizations = []
    for aisle, intervals in aisle_intervals.items():
        intervals = sorted(intervals, key=lambda x: x[0])
        merged = []
        for s, e in intervals:
            if not merged or s > merged[-1][1]:
                merged.append([s, e])
            else:
                merged[-1][1] = max(merged[-1][1], e)
        busy_time = sum(e - s for s, e in merged)
        utilizations.append((busy_time / simulation_end) * 100.0)
    return sum(utilizations) / len(utilizations)


def _calculate_used_time_std(tasks) -> Optional[float]:
    if not tasks:
        return None
    outbound_end = [t["end"] for t in tasks if t["type"] == "出库"]
    simulation_end = max(outbound_end) if outbound_end else max(t["end"] for t in tasks)
    if simulation_end <= 0:
        return None
    aisle_intervals: Dict[int, list] = {}
    for t in tasks:
        if t["end"] > simulation_end:
            continue
        if t["aisle"] is None:
            continue
        aisle_intervals.setdefault(t["aisle"], []).append((t["start"], t["end"]))
    if not aisle_intervals:
        return None
    used_times = []
    for intervals in aisle_intervals.values():
        intervals = sorted(intervals, key=lambda x: x[0])
        merged = []
        for s, e in intervals:
            if not merged or s > merged[-1][1]:
                merged.append([s, e])
            else:
                merged[-1][1] = max(merged[-1][1], e)
        busy_time = sum(e - s for s, e in merged)
        used_times.append(busy_time)
    mean_val = sum(used_times) / len(used_times)
    variance = sum((v - mean_val) ** 2 for v in used_times) / len(used_times)
    return variance ** 0.5


def _count_tasks_by_aisle(tasks):
    inbound_counts: Dict[int, int] = {}
    outbound_counts: Dict[int, int] = {}
    for t in tasks:
        aisle = t["aisle"]
        if aisle is None:
            continue
        if t["type"] == "入库":
            inbound_counts[aisle] = inbound_counts.get(aisle, 0) + 1
        elif t["type"] == "出库":
            outbound_counts[aisle] = outbound_counts.get(aisle, 0) + 1
    total_counts = {a: inbound_counts.get(a, 0) + outbound_counts.get(a, 0)
                    for a in set(inbound_counts) | set(outbound_counts)}
    return inbound_counts, outbound_counts, total_counts


def parse_log_file(path: Path) -> Dict[str, object]:
    text = path.read_text(encoding="utf-8")
    completion = _parse_completion(text)
    pairing = _parse_pairing_end(text)
    pairing_start = _parse_pairing_start(text)
    end_hours = _parse_end_hours(text)
    relocation_count = _parse_relocation_count(text)
    relocation_intervals = _parse_relocation_intervals(text)
    tasks = _parse_task_details(text)
    overlaps = _find_overlaps(tasks)
    avg_utilization = _calculate_avg_utilization(tasks)
    used_time_std = _calculate_used_time_std(tasks)
    inbound_counts, outbound_counts, total_counts = _count_tasks_by_aisle(tasks)

    total_done = sum(v[0] for v in completion.values()) if completion else 0
    total_groups = sum(v[1] for v in completion.values()) if completion else 0
    completion_ratio = (total_done / total_groups) if total_groups else None

    return {
        "completion": completion,
        "completion_ratio": completion_ratio,
        "pairing": pairing,
        "pairing_start": pairing_start,
        "end_hours": end_hours,
        "relocation_count": relocation_count,
        "relocation_intervals": relocation_intervals,
        "avg_utilization": avg_utilization,
        "used_time_std": used_time_std,
        "aisle_inbound_counts": inbound_counts,
        "aisle_outbound_counts": outbound_counts,
        "aisle_total_counts": total_counts,
        "overlaps": overlaps,
    }


def _format_lambda_suffix(value: Optional[float]) -> str:
    if value is None:
        return ""
    text = f"{value}".replace(".", "p")
    return f"-lam{text}"


def load_daily_results(log_root: Path, dates_filter: Optional[set], lambda_suffix: str) -> Dict[str, Dict[str, Dict[str, object]]]:
    all_data: Dict[str, Dict[str, Dict[str, object]]] = {}
    for date_dir in sorted(log_root.iterdir()):
        if not date_dir.is_dir():
            continue
        date_key = _extract_date_dir(date_dir.name) or date_dir.name
        if dates_filter and date_key not in dates_filter:
            continue
        all_data.setdefault(date_key, {})
        for strategy in STRATEGIES.keys():
            log_path = date_dir / f"{strategy}{lambda_suffix}.txt"
            if not log_path.exists():
                continue
            all_data[date_key][strategy] = parse_log_file(log_path)
    return all_data


def _plot_grouped_bars(dates, values_by_strategy, ylabel, title, out_name, legend_labels=None, strategies=None):
    plt.figure(figsize=(12, 7))
    strategies = strategies or list(STRATEGIES.keys())
    x = np.arange(len(dates))
    total_width = 0.75
    bar_width = total_width / len(strategies)

    for idx, strategy in enumerate(strategies):
        values = values_by_strategy.get(strategy, [None] * len(dates))
        heights = [v if v is not None else 0 for v in values]
        offsets = x + (idx - (len(strategies) - 1) / 2) * bar_width
        label = (
            legend_labels.get(strategy, STRATEGIES[strategy])
            if legend_labels is not None
            else STRATEGIES[strategy]
        )
        bars = plt.bar(
            offsets,
            heights,
            width=bar_width,
            label=label,
            color=COLORS.get(strategy),
        )
        for bar, v in zip(bars, values):
            if v is None:
                plt.text(
                    bar.get_x() + bar.get_width() / 2,
                    0.02,
                    "NA",
                    ha="center",
                    va="bottom",
                    fontsize=9,
                )
            else:
                plt.text(
                    bar.get_x() + bar.get_width() / 2,
                    bar.get_height() + 0.01,
                    f"{v:.2f}" if v <= 1 else f"{v:.1f}",
                    ha="center",
                    va="bottom",
                    fontsize=9,
                )

    plt.xticks(x, dates)
    plt.xlabel("Date")
    plt.ylabel(ylabel)
    plt.title(title)
    plt.grid(True, axis="y", linestyle="--", alpha=0.4)
    plt.legend(frameon=False, ncol=2, loc="upper center", bbox_to_anchor=(0.5, 1.1))
    plt.tight_layout()
    save_fig(out_name)
    plt.show()


def print_daily_aisle_stats(all_data):
    print("\n=== 各日期巷道入/出库次数与利用率方差 ===")
    for date in sorted(all_data.keys()):
        print(f"\n[Date {date}]")
        per_strategy = all_data.get(date, {})
        for strategy, info in per_strategy.items():
            inbound_counts = info.get("aisle_inbound_counts", {})
            outbound_counts = info.get("aisle_outbound_counts", {})
            total_counts = info.get("aisle_total_counts", {})
            used_time_std = info.get("used_time_std")
            if not inbound_counts and not outbound_counts:
                continue
            fmt_in = {k: f"{inbound_counts.get(k, 0)}次" for k in sorted(total_counts.keys())}
            fmt_out = {k: f"{outbound_counts.get(k, 0)}次" for k in sorted(total_counts.keys())}
            fmt_total = {k: f"{total_counts.get(k, 0)}次" for k in sorted(total_counts.keys())}
            print(f"{STRATEGIES.get(strategy, strategy)}:")
            print(f"  巷道入库次数: {fmt_in}")
            print(f"  巷道出库次数: {fmt_out}")
            print(f"  巷道总次数: {fmt_total}")
            if isinstance(used_time_std, (int, float)):
                print(f"  巷道使用时间标准差: {used_time_std:.2f}s")


def main():
    parser = argparse.ArgumentParser(description="Visualize daily logs produced by run_daily.")
    parser.add_argument("--log-root", default="logs/daily", help="Root directory for daily logs.")
    parser.add_argument("--out-dir", default="visualization/daily", help="Output directory for visualizations.")
    parser.add_argument("--dates", help="Comma/space separated list of YYYYMMDD to include.")
    parser.add_argument("--poisson-x", type=float, help="Match log suffix -lamX (X used in run_daily).")
    args = parser.parse_args()

    if SELECT_DATES:
        dates_filter = set(SELECT_DATES)
    elif START_DATE or END_DATE:
        dates_filter = {"__RANGE__"}
    elif args.dates:
        dates_filter = {d for d in re.split(r"[,\s]+", args.dates.strip()) if d}
    else:
        dates_filter = None

    lambda_value = POISSON_X if POISSON_X is not None else args.poisson_x
    lambda_suffix = _format_lambda_suffix(lambda_value)

    log_root = Path(args.log_root)
    if not log_root.exists():
        print(f"[ERROR] Log root not found: {log_root}")
        return

    if dates_filter == {"__RANGE__"}:
        dates_filter = None
        all_data = load_daily_results(log_root, dates_filter, lambda_suffix)
        all_dates = sorted(all_data.keys())
        filtered_dates = []
        for d in all_dates:
            if START_DATE and d < START_DATE:
                continue
            if END_DATE and d > END_DATE:
                continue
            filtered_dates.append(d)
        all_data = {d: all_data[d] for d in filtered_dates}
    else:
        all_data = load_daily_results(log_root, dates_filter, lambda_suffix)
    if not all_data:
        print("[WARN] No daily logs found.")
        return

    # export relocation intervals
    relocation_rows = []
    for date, per_strategy in all_data.items():
        for strategy, info in per_strategy.items():
            for entry in info.get("relocation_intervals") or []:
                relocation_rows.append(
                    {
                        "date": date,
                        "strategy": strategy,
                        "aisle": entry["aisle"],
                        "start": entry["start"],
                        "end": entry["end"],
                    }
                )
    if relocation_rows:
        csv_path = Path("daily_relocation_intervals.csv")
        with csv_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["date", "strategy", "aisle", "start", "end"])
            writer.writeheader()
            writer.writerows(relocation_rows)
        print(f"[INFO] Relocation intervals saved to {csv_path}")

    # warn overlaps
    for date, per_strategy in all_data.items():
        for strategy, info in per_strategy.items():
            overlaps = info.get("overlaps") or []
            if overlaps:
                print(
                    f"[WARN] {date} {strategy}: {len(overlaps)} overlaps in same aisle"
                )
                for aisle, prev, curr in overlaps[:5]:
                    print(
                        f"  aisle {aisle}: {prev['type']} {prev['start']:.2f}-{prev['end']:.2f} "
                        f"overlaps {curr['type']} {curr['start']:.2f}-{curr['end']:.2f}"
                    )
                if len(overlaps) > 5:
                    print("  ...")
    print_daily_aisle_stats(all_data)

    dates = sorted(all_data.keys())
    completion_vals = {s: [] for s in STRATEGIES.keys()}
    pairing_vals = None
    pairing_start_vals = {s: [] for s in STRATEGIES.keys()}
    end_hours_vals = {s: [] for s in STRATEGIES.keys()}
    relocation_vals = {s: [] for s in STRATEGIES.keys()}
    utilization_vals = {s: [] for s in STRATEGIES.keys()}

    for date in dates:
        for strategy in STRATEGIES.keys():
            info = all_data.get(date, {}).get(strategy)
            if not info:
                completion_vals[strategy].append(None)
                if pairing_vals is not None:
                    pairing_vals[strategy].append(None)
                pairing_start_vals[strategy].append(None)
                end_hours_vals[strategy].append(None)
                relocation_vals[strategy].append(None)
                utilization_vals[strategy].append(None)
                continue
            completion_vals[strategy].append(info.get("completion_ratio"))
            if pairing_vals is not None:
                pairing_vals[strategy].append(info.get("pairing", {}).get("beam_with_solo"))
            pairing_start_vals[strategy].append(info.get("pairing_start", {}).get("beam_with_solo"))
            end_hours_vals[strategy].append(info.get("end_hours"))
            reloc = info.get("relocation_count")
            relocation_vals[strategy].append(int(reloc) if reloc is not None else None)
            utilization_vals[strategy].append(info.get("avg_utilization"))

    _plot_grouped_bars(
        dates,
        completion_vals,
        ylabel="Completion Ratio",
        title="Outbound Completion Ratio by Date",
        out_name="daily_completion_ratio.png",
    )
    pairing_start_labels = {}
    for strategy, values in pairing_start_vals.items():
        nums = [v for v in values if isinstance(v, (int, float))]
        if nums:
            avg = sum(nums) / len(nums)
            pairing_start_labels[strategy] = f"{STRATEGIES[strategy]}: {avg*100:.2f}%"
        else:
            pairing_start_labels[strategy] = STRATEGIES[strategy]
    _plot_grouped_bars(
        dates,
        pairing_start_vals,
        ylabel="Start Beam Pairing Rate (with solo)",
        title="Start Pairing Rate by Date",
        out_name="daily_pairing_rate_start.png",
        legend_labels=pairing_start_labels,
        strategies=["base-base-heu", "prop-prop-opt"],
    )
    end_time_labels = {}
    for strategy, values in end_hours_vals.items():
        nums = [v for v in values if isinstance(v, (int, float))]
        if nums:
            avg = sum(nums) / len(nums)
            end_time_labels[strategy] = f"{STRATEGIES[strategy]}: {avg:.2f}h"
        else:
            end_time_labels[strategy] = STRATEGIES[strategy]
    _plot_grouped_bars(
        dates,
        end_hours_vals,
        ylabel="End Time (hours)",
        title="End Time by Date",
        out_name="daily_end_time.png",
        legend_labels=end_time_labels,
    )
    relocation_labels = {}
    for strategy, values in relocation_vals.items():
        nums = [v for v in values if isinstance(v, (int, float))]
        if nums:
            avg = sum(nums) / len(nums)
            relocation_labels[strategy] = f"{STRATEGIES[strategy]}: {avg:.2f}"
        else:
            relocation_labels[strategy] = STRATEGIES[strategy]
    _plot_grouped_bars(
        dates,
        relocation_vals,
        ylabel="Relocation Count",
        title="Relocation Count by Date",
        out_name="daily_relocation_count.png",
        legend_labels=relocation_labels,
    )
    utilization_labels = {}
    for strategy, values in utilization_vals.items():
        nums = [v for v in values if isinstance(v, (int, float))]
        if nums:
            avg = sum(nums) / len(nums)
            utilization_labels[strategy] = f"{STRATEGIES[strategy]}: {avg:.2f}%"
        else:
            utilization_labels[strategy] = STRATEGIES[strategy]
    _plot_grouped_bars(
        dates,
        utilization_vals,
        ylabel="Avg Aisle Utilization (%)",
        title="Avg Aisle Utilization by Date",
        out_name="daily_avg_utilization.png",
        legend_labels=utilization_labels,
    )


if __name__ == "__main__":
    main()
