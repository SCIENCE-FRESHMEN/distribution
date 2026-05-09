from pathlib import Path

import visualize_results as vr


ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "logs" / "compare_10_aisles"
REPORT_PATH = ROOT / "docs" / "simulation_10_aisles_daily_metrics.md"

STRATEGIES = [
    ("base-base-heu", "Baseline"),
    ("base-base-opt", "Baseline + Scheduling"),
    ("prop-prop-heu", "Allocation+"),
    ("prop-prop-opt", "Allocation+ + Scheduling"),
]


def get_day_end_pairing(data: dict, day: int) -> dict:
    day_times = [
        t for t in data.get("pairing_rates", {})
        if isinstance(t, (int, float)) and int(t // 1440) + 1 == day
    ]
    if not day_times:
        return {"slot": 0.0, "beam_without_solo": 0.0, "beam_with_solo": 0.0}
    return data["pairing_rates"][max(day_times)]


def get_day_last_outbound_time(data: dict, day: int) -> float:
    day_stats = data.get("aisle_busy_times", {}).get(day, {})
    if not day_stats:
        return 0.0
    return max(
        entry.get("simulation_end_time", 0.0)
        for entry in day_stats.values()
        if isinstance(entry, dict)
    )


def get_day_avg_utilization(data: dict, day: int) -> float:
    return data.get("aisle_busy_times", {}).get(day, {}).get("avg", {}).get("utilization", 0.0)


def get_day_total_tasks(data: dict, day: int) -> int:
    return data.get("tasks_completed_per_day", {}).get(day, {}).get("total", 0)


def get_day_relocations(data: dict, day: int) -> int:
    return data.get("relocation_counts_by_day", {}).get(day, 0)


def build_strategy_section(strategy_key: str, label: str, data: dict) -> str:
    days = sorted(data.get("tasks_completed_per_day", {}).keys())
    rows = []
    for day in days:
        start_pairing = data.get("pairing_start_by_day", {}).get(
            day, {"slot": 0.0, "beam_without_solo": 0.0, "beam_with_solo": 0.0}
        )
        end_pairing = get_day_end_pairing(data, day)
        rows.append(
            "| {day} | {tasks} | {relocs} | {last_out:.1f} | {util:.1f}% | {start_slot:.2f}% | {start_beam:.2f}% | {end_slot:.2f}% | {end_beam:.2f}% |".format(
                day=day,
                tasks=get_day_total_tasks(data, day),
                relocs=get_day_relocations(data, day),
                last_out=get_day_last_outbound_time(data, day),
                util=get_day_avg_utilization(data, day),
                start_slot=start_pairing["slot"] * 100,
                start_beam=start_pairing["beam_with_solo"] * 100,
                end_slot=end_pairing["slot"] * 100,
                end_beam=end_pairing["beam_with_solo"] * 100,
            )
        )

    return (
        f"## {label}\n\n"
        "| Day | 完成任务数 | 移库数 | 最后出库完成时间(s) | 平均巷道利用率 | 开始货位配对率 | 开始梁配对率(含solo) | 结束货位配对率 | 结束梁配对率(含solo) |\n"
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |\n"
        + "\n".join(rows)
        + "\n"
    )


def main() -> None:
    all_data = {}
    for strategy_key, _ in STRATEGIES:
        parsed = vr.parse_log_file(LOG_DIR / f"{strategy_key}.txt")
        all_data[strategy_key] = vr._filter_days(parsed, {1, 2, 3, 4, 5})

    sections = []
    for strategy_key, label in STRATEGIES:
        sections.append(build_strategy_section(strategy_key, label, all_data[strategy_key]))

    report = (
        "# 10巷道仿真每日指标明细\n\n"
        "## 为什么配对率是 0\n\n"
        "- 这不是汇总脚本算错，原始日志里每天的开始/结束配对率本身就是 `0.00%`。\n"
        "- 根因在于当前 [sku_config.json](C:/Users/Jerry/Desktop/各种项目/十堰wms/正式开始/code/shiyan/simulation/data/sku_config.json) 里的 `sku_pairs` 只定义了极少数 SKU 对，主要还是示例 SKU。\n"
        "- 但实际仿真数据 `inbound_task_config.json` / `production_plan_config.json` 里使用的是大量真实 SKU；这些 SKU 基本不在 `sku_pairs` 里，所以 [inventory.py](C:/Users/Jerry/Desktop/各种项目/十堰wms/正式开始/code/shiyan/simulation/inventory.py) 的 `get_pairing_stats()` 无法把它们识别为“可配对”。\n"
        "- 日志中出现的 `货位配对率: 0/0`、`梁配对率: 0/N` 正是这个配置失配的直接结果。\n"
        "- 如果你要看“真实有意义”的配对率，下一步应该先补全真实 SKU 的配对关系，而不是继续比较当前这组 0 值。\n\n"
        "## 每日指标\n\n"
        + "\n\n".join(sections)
        + "\n"
    )

    REPORT_PATH.write_text(report, encoding="utf-8")
    print(f"[INFO] wrote {REPORT_PATH}")


if __name__ == "__main__":
    main()
