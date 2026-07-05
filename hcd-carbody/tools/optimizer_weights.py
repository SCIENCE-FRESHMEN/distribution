"""
Grid search for optimizer score weights.

Supports:
- parallel trials (--jobs)
- parallel configs inside each trial (--parallel-configs)
- two optimization configs:
    ("proposed", "proposed", "optimization")
    ("baseline", "baseline", "optimization")

Weights are injected via environment variables (no warehouse.json rewrite):
  OPT_LR_BALANCE_WEIGHT
  OPT_MAKESPAN_WEIGHT
  OPT_BALANCE_WEIGHT
  OPT_PRODUCTION_LINE_AVG_TIME_WEIGHT
  OPT_PRODUCTION_LINE_BALANCE_WEIGHT
  OPT_AISLE_DISPERSION_WEIGHT
  OPT_INBOUND_WAIT_WEIGHT
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import re
import subprocess
import sys
import os
from pathlib import Path
from typing import Dict, Any, Iterable, List, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed


WEIGHT_KEYS = [
    "lr_balance_weight",
    "makespan_weight",
    "balance_weight",
    "production_line_avg_time_weight",
    "production_line_balance_weight",
    "aisle_dispersion_weight",
    "inbound_wait_weight",
]


WEIGHT_ENV_MAP = {
    "lr_balance_weight": "OPT_LR_BALANCE_WEIGHT",
    "makespan_weight": "OPT_MAKESPAN_WEIGHT",
    "balance_weight": "OPT_BALANCE_WEIGHT",
    "production_line_avg_time_weight": "OPT_PRODUCTION_LINE_AVG_TIME_WEIGHT",
    "production_line_balance_weight": "OPT_PRODUCTION_LINE_BALANCE_WEIGHT",
    "aisle_dispersion_weight": "OPT_AISLE_DISPERSION_WEIGHT",
    "inbound_wait_weight": "OPT_INBOUND_WAIT_WEIGHT",
}


DEFAULT_GRID: Dict[str, List[Any]] = {
    "lr_balance_weight": [0.1, 0.0],
    "makespan_weight": [0.001, 0.01, 0.05],
    "balance_weight": [0.1],
    "production_line_avg_time_weight": [0.001, 0.01, 0.05],
    "production_line_balance_weight": [0.0],
    "aisle_dispersion_weight": [0.0],
    "inbound_wait_weight": [0, 0.0001],
}


CONFIGS: List[Tuple[str, str, str]] = [
    ("proposed", "proposed", "optimization"),
    ("baseline", "baseline", "optimization"),
]


def load_grid(path: str | None) -> Dict[str, List[Any]]:
    if not path:
        return dict(DEFAULT_GRID)
    p = Path(path)
    obj = json.loads(p.read_text(encoding="utf-8"))
    grid: Dict[str, List[Any]] = {}
    for k in WEIGHT_KEYS:
        vals = obj.get(k)
        if isinstance(vals, list) and vals:
            grid[k] = vals
        else:
            grid[k] = list(DEFAULT_GRID[k])
    return grid


def iter_param_combinations(grid: Dict[str, List[Any]]) -> Iterable[Dict[str, Any]]:
    value_lists = [grid[k] for k in WEIGHT_KEYS]
    for values in itertools.product(*value_lists):
        yield {k: v for k, v in zip(WEIGHT_KEYS, values)}


def decode_bytes(raw: bytes) -> str:
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except Exception:
            pass
    return raw.decode("utf-8", errors="ignore")


def parse_avg_completion_hours_from_log(log_path: Path) -> float | None:
    """Average of per-day last outbound completion time (hours)."""
    if not log_path.exists():
        return None
    text = decode_bytes(log_path.read_bytes())
    lines = text.splitlines()

    day_header = re.compile(r"第\s*(\d+)\s*天汇总")
    outbound_line = re.compile(r"第\s*\d+\s*个\s*出库任务.*?起止\s*[\d.]+s~([\d.]+)s")
    end_by_day: Dict[int, float] = {}
    current_day: int | None = None

    for ln in lines:
        m_day = day_header.search(ln)
        if m_day:
            current_day = int(m_day.group(1))
            continue
        if current_day is None:
            continue
        m_out = outbound_line.search(ln)
        if m_out:
            end_s = float(m_out.group(1))
            prev = end_by_day.get(current_day, 0.0)
            if end_s > prev:
                end_by_day[current_day] = end_s

    vals = [v / 3600.0 for v in end_by_day.values() if v > 0]
    if not vals:
        return None
    return sum(vals) / len(vals)


def run_one(
    weights: Dict[str, Any],
    config: Tuple[str, str, str],
    trial_id: int,
    logs_dir: Path,
    python_exe: str,
) -> Tuple[float | None, Path]:
    allocation, position, scheduler = config
    log_file = logs_dir / f"trial{trial_id:04d}-{allocation[:4]}-{position[:4]}-{scheduler[:3]}.log"

    env = os.environ.copy()
    for k, env_name in WEIGHT_ENV_MAP.items():
        env[env_name] = str(weights[k])

    cmd = [
        python_exe,
        "run.py",
        "--inbound-allocation-strategy",
        allocation,
        "--inbound-position-strategy",
        position,
        "--scheduler-type",
        scheduler,
        "--lr-balance-weight",
        str(weights["lr_balance_weight"]),
        "--makespan-weight",
        str(weights["makespan_weight"]),
        "--balance-weight",
        str(weights["balance_weight"]),
        "--production-line-avg-time-weight",
        str(weights["production_line_avg_time_weight"]),
        "--production-line-balance-weight",
        str(weights["production_line_balance_weight"]),
        "--aisle-dispersion-weight",
        str(weights["aisle_dispersion_weight"]),
        "--inbound-wait-weight",
        str(weights["inbound_wait_weight"]),
        "--log-file",
        str(log_file),
    ]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=False)
    if proc.returncode != 0:
        print(f"[ERROR] trial {trial_id} {allocation}-{position}-{scheduler} failed: {proc.returncode}")
        print(decode_bytes(proc.stdout)[-1200:])
        print(decode_bytes(proc.stderr)[-1200:])
        return None, log_file
    return parse_avg_completion_hours_from_log(log_file), log_file


def run_trial(
    trial_id: int,
    weights: Dict[str, Any],
    logs_dir: Path,
    python_exe: str,
    parallel_configs: bool = False,
) -> Dict[str, Any]:
    row: Dict[str, Any] = {"trial_id": trial_id, **weights}
    vals: List[float] = []

    if parallel_configs and len(CONFIGS) > 1:
        with ThreadPoolExecutor(max_workers=len(CONFIGS)) as ex:
            fut_map = {ex.submit(run_one, weights, cfg, trial_id, logs_dir, python_exe): cfg for cfg in CONFIGS}
            for fut in as_completed(fut_map):
                cfg = fut_map[fut]
                avg_h, log_path = fut.result()
                key = f"{cfg[0]}_{cfg[1]}_{cfg[2]}"
                row[f"avg_completion_{key}_hours"] = avg_h
                row[f"log_{key}"] = str(log_path)
                if avg_h is not None:
                    vals.append(avg_h)
    else:
        for cfg in CONFIGS:
            avg_h, log_path = run_one(weights, cfg, trial_id, logs_dir, python_exe)
            key = f"{cfg[0]}_{cfg[1]}_{cfg[2]}"
            row[f"avg_completion_{key}_hours"] = avg_h
            row[f"log_{key}"] = str(log_path)
            if avg_h is not None:
                vals.append(avg_h)

    row["avg_completion_mean_hours"] = (sum(vals) / len(vals)) if vals else None
    return row


def flush_rows_csv(rows: List[Dict[str, Any]], out_csv: Path) -> None:
    if not rows:
        return
    with out_csv.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def load_existing_rows(out_csv: Path) -> List[Dict[str, Any]]:
    if not out_csv.exists():
        return []
    rows: List[Dict[str, Any]] = []
    with out_csv.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for r in reader:
            row = dict(r)
            try:
                row["trial_id"] = int(str(row.get("trial_id", "")).strip())
            except Exception:
                continue
            rows.append(row)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Grid search for optimizer weight params")
    parser.add_argument("--grid-json", type=str, default=None, help="JSON file: {param: [values,...]}")
    parser.add_argument("--max-trials", type=int, default=0, help="0 means run all combinations")
    parser.add_argument("--output-csv", type=str, default="logs/grid_search_optimizer_weights_results.csv")
    parser.add_argument("--logs-dir", type=str, default="logs/grid_search_optimizer_weights")
    parser.add_argument("--python", type=str, default=sys.executable)
    parser.add_argument("--jobs", type=int, default=8, help="parallel trials to run")
    parser.add_argument("--parallel-configs", action="store_true", help="run both configs in parallel inside each trial")
    parser.add_argument("--resume", action="store_true", help="resume from existing output-csv by trial_id")
    args = parser.parse_args()

    grid = load_grid(args.grid_json)
    logs_dir = Path(args.logs_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)
    out_csv = Path(args.output_csv)
    out_csv.parent.mkdir(parents=True, exist_ok=True)

    all_params = list(iter_param_combinations(grid))
    if args.max_trials and args.max_trials > 0:
        all_params = all_params[: args.max_trials]
    print(f"[INFO] total parameter combinations: {len(all_params)}")
    print(f"[INFO] parallel jobs: {max(1, int(args.jobs))}")

    rows: List[Dict[str, Any]] = []
    completed_trial_ids = set()
    if args.resume:
        rows = load_existing_rows(out_csv)
        completed_trial_ids = {int(r["trial_id"]) for r in rows if "trial_id" in r}
        if completed_trial_ids:
            print(f"[INFO] resume enabled, loaded existing trials: {len(completed_trial_ids)}")

    indexed_params = list(enumerate(all_params, start=1))
    pending = [(i, p) for i, p in indexed_params if i not in completed_trial_ids]
    if completed_trial_ids:
        print(f"[INFO] pending trials: {len(pending)} / total {len(all_params)}")

    jobs = max(1, int(args.jobs))
    if jobs == 1:
        for i, weights in pending:
            print(f"[INFO] trial {i}/{len(all_params)} params={weights}")
            row = run_trial(i, weights, logs_dir, args.python, args.parallel_configs)
            rows.append(row)
            rows.sort(key=lambda r: int(r.get("trial_id", 0)))
            flush_rows_csv(rows, out_csv)
    else:
        with ThreadPoolExecutor(max_workers=jobs) as ex:
            fut_map = {}
            for i, weights in pending:
                print(f"[INFO] submit trial {i}/{len(all_params)} params={weights}")
                fut = ex.submit(run_trial, i, weights, logs_dir, args.python, args.parallel_configs)
                fut_map[fut] = i
            done_cnt = 0
            for fut in as_completed(fut_map):
                trial_id = fut_map[fut]
                try:
                    row = fut.result()
                    rows.append(row)
                    done_cnt += 1
                    print(f"[INFO] completed trial {trial_id} ({done_cnt}/{len(all_params)})")
                    rows.sort(key=lambda r: int(r.get("trial_id", 0)))
                    flush_rows_csv(rows, out_csv)
                except Exception as e:
                    done_cnt += 1
                    print(f"[ERROR] trial {trial_id} failed: {e}")

    valid_rows = [r for r in rows if r.get("avg_completion_mean_hours") not in (None, "")]
    if valid_rows:
        def _to_float(v):
            try:
                return float(v)
            except Exception:
                return 1e18
        best = min(valid_rows, key=lambda r: _to_float(r["avg_completion_mean_hours"]))
        print("[INFO] best trial by mean completion time:")
        print(json.dumps(best, ensure_ascii=False, indent=2))
    print(f"[INFO] results saved: {out_csv}")


if __name__ == "__main__":
    main()
