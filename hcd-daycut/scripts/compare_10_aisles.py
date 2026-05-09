import argparse
import subprocess
import sys
from pathlib import Path

import visualize_results as vr


ROOT = Path(__file__).resolve().parents[1]
RUN_PY = ROOT / "run.py"
DEFAULT_WAREHOUSE_CONFIG = ROOT / "config" / "warehouse_10_aisles.json"
DEFAULT_LOG_DIR = ROOT / "logs" / "compare_10_aisles"
DEFAULT_OUT_DIR = ROOT / "visualization" / "compare_10_aisles"
DEFAULT_REPORT_PATH = ROOT / "docs" / "simulation_10_aisles_strategy_report.md"

STRATEGIES = [
    ("base-base-heu", "baseline", "baseline", "heuristic"),
    ("base-base-opt", "baseline", "baseline", "optimization"),
    ("prop-prop-heu", "proposed", "proposed", "heuristic"),
    ("prop-prop-opt", "proposed", "proposed", "optimization"),
]

LABELS = {
    "base-base-heu": "Baseline",
    "base-base-opt": "Baseline + Scheduling",
    "prop-prop-heu": "Allocation+",
    "prop-prop-opt": "Allocation+ + Scheduling",
}


def run_one(log_name: str, allocation: str, position: str, scheduler: str, args) -> None:
    cmd = [
        sys.executable,
        str(RUN_PY),
        "--warehouse-config",
        str(args.warehouse_config),
        "--random-seed",
        str(args.random_seed),
        "--real-time-days",
        str(args.real_time_days),
        "--cutoff-hour",
        str(args.cutoff_hour),
        "--inbound-allocation-strategy",
        allocation,
        "--inbound-position-strategy",
        position,
        "--scheduler-type",
        scheduler,
    ]
    if args.max_simulation_time is not None:
        cmd.extend(["--max-simulation-time", str(args.max_simulation_time)])

    print(f"[RUN] {' '.join(cmd)}")
    result = subprocess.run(
        cmd,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    args.log_dir.mkdir(parents=True, exist_ok=True)
    log_path = args.log_dir / f"{log_name}.txt"
    log_path.write_text(result.stdout + ("\n" + result.stderr if result.stderr else ""), encoding="utf-8")
    if result.returncode != 0:
        raise RuntimeError(f"{log_name} failed with exit code {result.returncode}; see {log_path}")


def summarize(data: dict) -> dict:
    total_tasks = sum(v.get("total", 0) for v in data.get("tasks_completed_per_day", {}).values())
    relocations = data.get("total_relocations", 0)

    start_rates = list(data.get("pairing_start_by_day", {}).values())
    start_slot_avg = sum(v["slot"] for v in start_rates) / len(start_rates) if start_rates else 0.0
    start_beam_avg = sum(v["beam_with_solo"] for v in start_rates) / len(start_rates) if start_rates else 0.0

    numeric_times = [t for t in data.get("pairing_rates", {}) if isinstance(t, (int, float))]
    if numeric_times:
        final_pairing = data["pairing_rates"][max(numeric_times)]
        final_slot = final_pairing["slot"]
        final_beam = final_pairing["beam_with_solo"]
    else:
        final_slot = 0.0
        final_beam = 0.0

    outbound_completion = []
    for day_stats in data.get("aisle_busy_times", {}).values():
        if not day_stats:
            continue
        simulation_end_time = max(
            entry.get("simulation_end_time", 0.0)
            for entry in day_stats.values()
            if isinstance(entry, dict)
        )
        if simulation_end_time:
            outbound_completion.append(simulation_end_time)
    avg_last_outbound = sum(outbound_completion) / len(outbound_completion) if outbound_completion else 0.0

    avg_util = []
    for day_stats in data.get("aisle_busy_times", {}).values():
        if "avg" in day_stats:
            avg_util.append(day_stats["avg"].get("utilization", 0.0))
    avg_utilization = sum(avg_util) / len(avg_util) if avg_util else 0.0

    return {
        "total_tasks": total_tasks,
        "relocations": relocations,
        "start_slot_avg": start_slot_avg,
        "start_beam_avg": start_beam_avg,
        "final_slot": final_slot,
        "final_beam": final_beam,
        "avg_last_outbound": avg_last_outbound,
        "avg_utilization": avg_utilization,
    }


def build_report(all_data: dict, args) -> str:
    summary_rows = []
    summary_map = {}
    for strategy_key, _, _, _ in STRATEGIES:
        summary = summarize(all_data[strategy_key])
        summary_map[strategy_key] = summary
        summary_rows.append(
            "| {label} | {tasks} | {relocs} | {last_out:.1f} | {util:.1f}% | {start_slot:.2f}% | {start_beam:.2f}% | {final_slot:.2f}% | {final_beam:.2f}% |".format(
                label=LABELS[strategy_key],
                tasks=summary["total_tasks"],
                relocs=summary["relocations"],
                last_out=summary["avg_last_outbound"],
                util=summary["avg_utilization"],
                start_slot=summary["start_slot_avg"] * 100,
                start_beam=summary["start_beam_avg"] * 100,
                final_slot=summary["final_slot"] * 100,
                final_beam=summary["final_beam"] * 100,
            )
        )

    best_completion = min(summary_map.items(), key=lambda item: (item[1]["avg_last_outbound"], item[1]["relocations"]))[0]
    best_relocation = min(summary_map.items(), key=lambda item: (item[1]["relocations"], item[1]["avg_last_outbound"]))[0]
    best_pairing = max(summary_map.items(), key=lambda item: (item[1]["final_beam"], -item[1]["avg_last_outbound"]))[0]

    return (
        "# 10巷道不同策略仿真对比报告\n\n"
        "## 实验摘要\n\n"
        f"- 目标：比较 10 个巷道下 4 组策略组合的仿真效果。\n"
        f"- 数据窗口：按切日逻辑运行 {args.real_time_days} 天，`cutoff_hour={args.cutoff_hour}`。\n"
        f"- 随机种子：`{args.random_seed}`。\n"
        f"- 仓库配置：`{args.warehouse_config.relative_to(ROOT)}`。\n"
        f"- 日志目录：`{args.log_dir.relative_to(ROOT)}`。\n"
        f"- 图表目录：`{args.out_dir.relative_to(ROOT)}`。\n\n"
        "## 仿真配置\n\n"
        "```bash\n"
        f"{sys.executable} run.py --warehouse-config {args.warehouse_config} --random-seed {args.random_seed} "
        f"--real-time-days {args.real_time_days} --cutoff-hour {args.cutoff_hour} "
        "--inbound-allocation-strategy <strategy> --inbound-position-strategy <strategy> --scheduler-type <strategy>\n"
        "```\n\n"
        "- 仓库结构：10 巷道、3 条产线、2 行、3 列、18 层、双层货位。\n"
        "- 10 巷道配置沿用默认 5 巷道参数，并将原禁用位模式对称复制到 6-10 巷道。\n"
        "- 数据源：`simulation/data/inbound_task_config.json`、`simulation/data/production_plan_config.json`。\n\n"
        "## 结果汇总\n\n"
        "| 策略 | 完成任务总数 | 移库总数 | 平均最后出库完成时间(s) | 平均巷道利用率 | 平均开始货位配对率 | 平均开始梁配对率(含solo) | 最终货位配对率 | 最终梁配对率(含solo) |\n"
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |\n"
        + "\n".join(summary_rows)
        + "\n\n## 结论\n\n"
        f"- 完工时间最优：`{LABELS[best_completion]}`。\n"
        f"- 移库成本最优：`{LABELS[best_relocation]}`。\n"
        f"- 最终配对率最优：`{LABELS[best_pairing]}`。\n"
        "- 如果目标是吞吐与配对率兼顾，优先看同时优化入库与调度的组合；如果目标是减少移库，则优先看移库总数更低的组合。\n\n"
        "## 说明与局限\n\n"
        "- 本次对比固定随机种子，结果可复现，但仍建议后续增加多随机种子重复实验。\n"
        "- 当前比较基于仓库默认 JSON 数据，结论代表该数据窗口下的相对效果，不直接等于线上长期平均表现。\n"
        "- 图表由 `visualize_results.py` 生成，可进一步查看每日趋势与巷道利用分布。\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run 10-aisle comparison and generate report.")
    parser.add_argument("--warehouse-config", type=Path, default=DEFAULT_WAREHOUSE_CONFIG)
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--report-path", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--random-seed", type=int, default=42)
    parser.add_argument("--real-time-days", type=int, default=5)
    parser.add_argument("--cutoff-hour", type=int, default=4)
    parser.add_argument("--max-simulation-time", type=float, default=None)
    parser.add_argument("--skip-run", action="store_true")
    args = parser.parse_args()

    args.warehouse_config = args.warehouse_config.resolve()
    args.log_dir = args.log_dir.resolve()
    args.out_dir = args.out_dir.resolve()
    args.report_path = args.report_path.resolve()

    if not args.skip_run:
        for strategy_key, allocation, position, scheduler in STRATEGIES:
            run_one(strategy_key, allocation, position, scheduler, args)

    days_filter = set(range(1, args.real_time_days + 1))
    all_data = {}
    for strategy_key, _, _, _ in STRATEGIES:
        parsed = vr.parse_log_file(args.log_dir / f"{strategy_key}.txt")
        all_data[strategy_key] = vr._filter_days(parsed, days_filter)

    vr.OUTPUT_DIR = args.out_dir
    vr.print_summary_statistics(all_data)
    vr.print_aisle_busy_times(all_data)
    vr.print_aisle_used_time_std(all_data)
    vr.print_aisle_task_counts(all_data)
    vr.print_relocation_counts_by_day(all_data)
    if vr.plt is not None:
        vr.plot_pairing_rates_over_time(all_data, "beam_with_solo")
        vr.plot_pairing_start_by_day(all_data)
        vr.plot_tasks_completed_per_day(all_data)
        vr.plot_completion_times(all_data)
        vr.plot_relocation_counts_by_day(all_data)
        vr.plot_avg_utilization_by_day(all_data)
    else:
        print("[WARN] matplotlib unavailable; skip chart generation")

    args.report_path.parent.mkdir(parents=True, exist_ok=True)
    args.report_path.write_text(build_report(all_data, args), encoding="utf-8")
    print(f"[INFO] report written to {args.report_path}")


if __name__ == "__main__":
    main()
