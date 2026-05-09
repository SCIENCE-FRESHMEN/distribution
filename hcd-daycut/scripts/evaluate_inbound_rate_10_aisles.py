import argparse
import math
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List


ROOT = Path(__file__).resolve().parents[1]
RUN_PY = ROOT / "run.py"
DEFAULT_WAREHOUSE_CONFIG = ROOT / "config" / "warehouse_10_aisles.json"
DEFAULT_LOG_DIR = ROOT / "logs" / "inbound_rate_10_aisles"
DEFAULT_REPORT_PATH = ROOT / "docs" / "simulation_10_aisles_inbound_rate_report.md"

DEFAULT_LAMBDAS = [1 / 150.0, 1 / 100.0, 1 / 75.0]

DAY_INBOUND_PATTERN = re.compile(r"\[DAY (\d+)\] 入库任务数: (\d+)")
DAY_END_TIME_PATTERN = re.compile(r"\[DAY (\d+)\] 日内结束，当前时间 ([\d.]+) 小时")
DAY_END_PAIRING_PATTERN = re.compile(
    r"\[DAY (\d+) 结束\] 货位配对率: (\d+)/(\d+) = ([\d.]+)%; "
    r"梁配对率\(不含solo\): (\d+)/(\d+) = ([\d.]+)%; "
    r"梁配对率\(含solo\): (\d+)/(\d+) = ([\d.]+)%"
)
DAY_SUMMARY_PATTERN = re.compile(
    r"第\s*(\d+)\s*天汇总\s*-+\s*(.*?)(?=第\s*\d+\s*天汇总\s*-+|全部天汇总|\Z)",
    re.DOTALL,
)
INBOUND_DONE_PATTERN = re.compile(r"第\s*\d+\s*个入库任务.*?完成(?:\s*\(巷道\s*(\d+)\))?.*?起止\s*([\d.]+)s~([\d.]+)s")
OUTBOUND_DONE_PATTERN = re.compile(r"第\s*\d+\s*个出库任务.*?完成(?:\s*\(巷道\s*(\d+)\))?.*?起止\s*([\d.]+)s~([\d.]+)s")
RELOCATION_PATTERN = re.compile(r"移库数量:\s*(\d+)")


@dataclass
class RateProfile:
    lambda_rate: float
    avg_interval_s: float
    avg_tasks_per_hour: float
    avg_tasks_per_day: float


@dataclass
class DayMetrics:
    day: int
    inbound_planned: int
    inbound_completed: int
    outbound_completed: int
    relocations: int
    last_outbound_completion_s: float
    avg_aisle_utilization_pct: float
    end_slot_pairing_pct: float
    end_beam_pairing_pct: float


def describe_rate(lambda_rate: float) -> RateProfile:
    if lambda_rate <= 0:
        return RateProfile(lambda_rate=lambda_rate, avg_interval_s=math.inf, avg_tasks_per_hour=0.0, avg_tasks_per_day=0.0)
    return RateProfile(
        lambda_rate=lambda_rate,
        avg_interval_s=1.0 / lambda_rate,
        avg_tasks_per_hour=lambda_rate * 3600.0,
        avg_tasks_per_day=lambda_rate * 86400.0,
    )


def scenario_name(lambda_rate: float) -> str:
    return f"lambda_{lambda_rate:.6f}".replace(".", "_")


def run_one(args, lambda_rate: float) -> Path:
    name = scenario_name(lambda_rate)
    log_path = args.log_dir / f"{name}.txt"
    if args.skip_existing and log_path.exists():
        print(f"[SKIP] {name} -> {log_path}")
        return log_path

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
        "proposed",
        "--inbound-position-strategy",
        "proposed",
        "--scheduler-type",
        "optimization",
        "--inbound-rate-lambda",
        str(lambda_rate),
    ]
    if args.max_simulation_time is not None:
        cmd.extend(["--max-simulation-time", str(args.max_simulation_time)])

    args.log_dir.mkdir(parents=True, exist_ok=True)
    print(f"[RUN] {name}: {' '.join(cmd)}")
    with log_path.open("w", encoding="utf-8") as fh:
        process = subprocess.Popen(
            cmd,
            cwd=ROOT,
            stdout=fh,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return_code = process.wait()
    if return_code != 0:
        raise RuntimeError(f"{name} failed with exit code {return_code}; see {log_path}")
    return log_path


def _calc_avg_utilization(summary_block: str, simulation_end_s: float) -> float:
    if simulation_end_s <= 0:
        return 0.0

    aisle_totals: Dict[int, float] = {}
    for pattern in (INBOUND_DONE_PATTERN, OUTBOUND_DONE_PATTERN):
        for aisle_str, start_s, end_s in pattern.findall(summary_block):
            if not aisle_str:
                continue
            aisle = int(aisle_str)
            duration = float(end_s) - float(start_s)
            aisle_totals[aisle] = aisle_totals.get(aisle, 0.0) + duration

    if not aisle_totals:
        return 0.0
    avg_total_time = sum(aisle_totals.values()) / len(aisle_totals)
    return avg_total_time / simulation_end_s * 100.0


def parse_log(log_path: Path, days: List[int]) -> Dict[int, DayMetrics]:
    text = log_path.read_text(encoding="utf-8")

    inbound_by_day = {int(day): int(count) for day, count in DAY_INBOUND_PATTERN.findall(text)}
    end_time_by_day = {int(day): float(hours) * 3600.0 for day, hours in DAY_END_TIME_PATTERN.findall(text)}
    pairing_by_day = {
        int(day): {
            "slot": float(slot_pct),
            "beam": float(beam_solo_pct),
        }
        for day, _, _, slot_pct, _, _, _, _, _, beam_solo_pct in DAY_END_PAIRING_PATTERN.findall(text)
    }

    summary_blocks = {int(day): block for day, block in DAY_SUMMARY_PATTERN.findall(text)}

    metrics_by_day: Dict[int, DayMetrics] = {}
    for day in days:
        block = summary_blocks.get(day, "")
        inbound_completed = len(INBOUND_DONE_PATTERN.findall(block))
        outbound_matches = OUTBOUND_DONE_PATTERN.findall(block)
        outbound_completed = len(outbound_matches)
        last_outbound_completion_s = max((float(end_s) for _, _, end_s in outbound_matches), default=0.0)
        reloc_match = RELOCATION_PATTERN.search(block)
        simulation_end_s = end_time_by_day.get(day, 0.0)
        metrics_by_day[day] = DayMetrics(
            day=day,
            inbound_planned=inbound_by_day.get(day, 0),
            inbound_completed=inbound_completed,
            outbound_completed=outbound_completed,
            relocations=int(reloc_match.group(1)) if reloc_match else 0,
            last_outbound_completion_s=last_outbound_completion_s,
            avg_aisle_utilization_pct=_calc_avg_utilization(block, simulation_end_s),
            end_slot_pairing_pct=pairing_by_day.get(day, {}).get("slot", 0.0),
            end_beam_pairing_pct=pairing_by_day.get(day, {}).get("beam", 0.0),
        )
    return metrics_by_day


def summarize(metrics_by_day: Dict[int, DayMetrics]) -> Dict[str, float]:
    days = sorted(metrics_by_day)
    values = [metrics_by_day[day] for day in days]
    day_count = len(values) or 1
    return {
        "avg_inbound_planned": sum(v.inbound_planned for v in values) / day_count,
        "avg_inbound_completed": sum(v.inbound_completed for v in values) / day_count,
        "avg_outbound_completed": sum(v.outbound_completed for v in values) / day_count,
        "avg_relocations": sum(v.relocations for v in values) / day_count,
        "avg_last_outbound_completion_s": sum(v.last_outbound_completion_s for v in values) / day_count,
        "avg_aisle_utilization_pct": sum(v.avg_aisle_utilization_pct for v in values) / day_count,
        "avg_end_slot_pairing_pct": sum(v.end_slot_pairing_pct for v in values) / day_count,
        "avg_end_beam_pairing_pct": sum(v.end_beam_pairing_pct for v in values) / day_count,
    }


def build_report(results: List[dict], args) -> str:
    summary_rows = []
    detail_sections = []

    best_util = max(results, key=lambda item: item["summary"]["avg_aisle_utilization_pct"])

    for result in results:
        profile: RateProfile = result["profile"]
        summary = result["summary"]
        summary_rows.append(
            "| {lambda_rate:.6f} | {interval:.1f} | {per_hour:.1f} | {per_day:.1f} | {planned:.1f} | {util:.2f}% | {beam:.2f}% | {reloc:.1f} | {last_out:.1f} |".format(
                lambda_rate=profile.lambda_rate,
                interval=profile.avg_interval_s,
                per_hour=profile.avg_tasks_per_hour,
                per_day=profile.avg_tasks_per_day,
                planned=summary["avg_inbound_planned"],
                util=summary["avg_aisle_utilization_pct"],
                beam=summary["avg_end_beam_pairing_pct"],
                reloc=summary["avg_relocations"],
                last_out=summary["avg_last_outbound_completion_s"],
            )
        )

        day_rows = []
        for day in sorted(result["metrics_by_day"]):
            day = result["metrics_by_day"][day]
            day_rows.append(
                "| {day} | {in_planned} | {in_done} | {out_done} | {reloc} | {last_out:.1f} | {util:.2f}% | {slot:.2f}% | {beam:.2f}% |".format(
                    day=day.day,
                    in_planned=day.inbound_planned,
                    in_done=day.inbound_completed,
                    out_done=day.outbound_completed,
                    reloc=day.relocations,
                    last_out=day.last_outbound_completion_s,
                    util=day.avg_aisle_utilization_pct,
                    slot=day.end_slot_pairing_pct,
                    beam=day.end_beam_pairing_pct,
                )
            )
        detail_sections.append(
            "## λ = {lambda_rate:.6f}\n\n"
            "- 平均入库间隔: `{interval:.1f}s/单`\n"
            "- 平均入库速率: `{per_hour:.1f} 单/小时`，即 `{per_day:.1f} 单/天`\n"
            "- 平均计划入库数: `{planned:.1f} 单/天`\n\n"
            "| Day | 计划入库数 | 完成入库数 | 完成出库数 | 移库数 | 最后出库完成时间(s) | 平均巷道利用率 | 结束货位配对率 | 结束梁配对率(含solo) |\n"
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |\n"
            "{rows}\n".format(
                lambda_rate=profile.lambda_rate,
                interval=profile.avg_interval_s,
                per_hour=profile.avg_tasks_per_hour,
                per_day=profile.avg_tasks_per_day,
                planned=summary["avg_inbound_planned"],
                rows="\n".join(day_rows),
            )
        )

    return (
        "# 10 巷道入库速率敏感性评估\n\n"
        "## 实验配置\n\n"
        "- 仓库配置: `config/warehouse_10_aisles.json`\n"
        "- 策略固定为: `Allocation+ + Scheduling` (`proposed + proposed + optimization`)\n"
        f"- 运行天数: `{args.real_time_days}` 天\n"
        f"- 切日小时: `cutoff_hour={args.cutoff_hour}`\n"
        f"- 随机种子: `{args.random_seed}`\n"
        "- 入库速率口径:\n"
        "  `λ` 是泊松到达率，单位是 `单/秒`；平均入库间隔为 `1/λ` 秒，平均入库速率为 `λ*3600` 单/小时。\n\n"
        "## 汇总结论\n\n"
        f"- 本轮平均巷道利用率最高的是 `λ={best_util['profile'].lambda_rate:.6f}`，对应 `{best_util['summary']['avg_aisle_utilization_pct']:.2f}%`。\n"
        "- 文档中的“平均入库速率”同时给出理论平均值（由 λ 直接换算）和日志里的平均计划入库数，避免只看参数不看实际任务量。\n\n"
        "| λ(单/秒) | 平均入库间隔(s) | 平均入库速率(单/小时) | 平均入库速率(单/天) | 平均计划入库数(单/天) | 平均巷道利用率 | 平均结束梁配对率(含solo) | 平均移库数 | 平均最后出库完成时间(s) |\n"
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |\n"
        + "\n".join(summary_rows)
        + "\n\n## 每日结果\n\n"
        + "\n\n".join(detail_sections)
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate 10-aisle utilization under different inbound rates.")
    parser.add_argument("--warehouse-config", type=Path, default=DEFAULT_WAREHOUSE_CONFIG)
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--report-path", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--random-seed", type=int, default=42)
    parser.add_argument("--real-time-days", type=int, default=5)
    parser.add_argument("--cutoff-hour", type=int, default=4)
    parser.add_argument("--max-simulation-time", type=float, default=None)
    parser.add_argument("--lambdas", type=float, nargs="+", default=DEFAULT_LAMBDAS)
    parser.add_argument("--skip-run", action="store_true")
    parser.add_argument("--skip-existing", action="store_true")
    args = parser.parse_args()

    args.warehouse_config = args.warehouse_config.resolve()
    args.log_dir = args.log_dir.resolve()
    args.report_path = args.report_path.resolve()

    days = list(range(1, args.real_time_days + 1))
    results = []
    for lambda_rate in args.lambdas:
        log_path = args.log_dir / f"{scenario_name(lambda_rate)}.txt"
        if not args.skip_run:
            log_path = run_one(args, lambda_rate)
        profile = describe_rate(lambda_rate)
        metrics_by_day = parse_log(log_path, days)
        results.append(
            {
                "profile": profile,
                "log_path": log_path,
                "metrics_by_day": metrics_by_day,
                "summary": summarize(metrics_by_day),
            }
        )

    args.report_path.parent.mkdir(parents=True, exist_ok=True)
    args.report_path.write_text(build_report(results, args), encoding="utf-8")
    print(f"[INFO] wrote {args.report_path}")


if __name__ == "__main__":
    main()
