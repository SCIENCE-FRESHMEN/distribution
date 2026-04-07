"""
Grid search for proposed allocator/position hyperparameters.

It runs:
    ("proposed", "proposed", "heuristic")
    ("proposed", "proposed", "optimization")

for each parameter combination, then computes average daily completion time
(same metric logic as visualize_results.plot_completion_times).
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, Any, Iterable, List, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from visualize_results import parse_log_file


CONFIGS: List[Tuple[str, str, str]] = [
    ("proposed", "proposed", "heuristic"),
    ("proposed", "proposed", "optimization"),
]


PARAM_ENV_MAP = {
    "future_n": "PROPOSED_FUTURE_N",
    "future_weight": "PROPOSED_FUTURE_WEIGHT",
    "future_tail_weight": "PROPOSED_FUTURE_TAIL_WEIGHT",
    "feature_weight": "PROPOSED_FEATURE_WEIGHT",
    "pending_weight": "PROPOSED_PENDING_WEIGHT",
    "recent_weight": "PROPOSED_RECENT_WEIGHT",
    "capacity_ratio_weight": "PROPOSED_CAPACITY_RATIO_WEIGHT",
    "recent_select_window": "PROPOSED_RECENT_SELECT_WINDOW",
    "w_in": "PROPOSED_W_IN",
    "w_out": "PROPOSED_W_OUT",
}


# Default is a single-point grid (current values). Replace with your ranges,
# or pass --grid-json to load ranges dynamically.
DEFAULT_GRID: Dict[str, List[Any]] = {
  "future_n": [4],
  "future_weight": [1.0, 4.0],
  "future_tail_weight": [0.0],
  "feature_weight": [1.0],
  "pending_weight": [8.0, 12.0],
  "recent_weight": [4.0, 8.0],
  "capacity_ratio_weight": [20.0, 30.0],
  "recent_select_window": [4],
  "w_in": [1.0],
  "w_out": [3.0, 5.0],
}



def load_grid(path: str | None) -> Dict[str, List[Any]]:
    if not path:
        return DEFAULT_GRID
    p = Path(path)
    obj = json.loads(p.read_text(encoding="utf-8"))
    grid: Dict[str, List[Any]] = {}
    for k, vals in obj.items():
        if k not in PARAM_ENV_MAP:
            continue
        if isinstance(vals, list) and vals:
            grid[k] = vals
    for k, vals in DEFAULT_GRID.items():
        if k not in grid:
            grid[k] = vals
    return grid


def iter_param_combinations(grid: Dict[str, List[Any]]) -> Iterable[Dict[str, Any]]:
    keys = list(PARAM_ENV_MAP.keys())
    value_lists = [grid.get(k, DEFAULT_GRID[k]) for k in keys]
    for values in itertools.product(*value_lists):
        yield {k: v for k, v in zip(keys, values)}


def avg_completion_hours(parsed: Dict[str, Any]) -> float | None:
    day_tasks = parsed.get("task_completion_details", {}) or {}
    days = sorted(day_tasks.keys())
    if not days:
        return None
    heights = []
    for day in days:
        outbound_tasks = [t for t in day_tasks.get(day, []) if t.get("type") == "出库"]
        heights.append(max((t.get("end_time", 0) for t in outbound_tasks), default=0) / 3600.0)
    valid = [h for h in heights if h > 0]
    if not valid:
        return None
    return sum(valid) / len(valid)


def run_one(
    params: Dict[str, Any],
    config: Tuple[str, str, str],
    trial_id: int,
    logs_dir: Path,
    python_exe: str,
) -> Tuple[float | None, Path]:
    allocation, position, scheduler = config
    log_file = logs_dir / f"trial{trial_id:04d}-{allocation[:4]}-{position[:4]}-{scheduler[:3]}.log"

    env = os.environ.copy()
    for k, env_name in PARAM_ENV_MAP.items():
        env[env_name] = str(params[k])

    cmd = [
        python_exe,
        "run.py",
        "--inbound-allocation-strategy",
        allocation,
        "--inbound-position-strategy",
        position,
        "--scheduler-type",
        scheduler,
        "--log-file",
        str(log_file),
    ]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=False)
    try:
        stdout_text = proc.stdout.decode("utf-8")
    except Exception:
        try:
            stdout_text = proc.stdout.decode("gbk")
        except Exception:
            stdout_text = proc.stdout.decode("utf-8", errors="ignore")
    try:
        stderr_text = proc.stderr.decode("utf-8")
    except Exception:
        try:
            stderr_text = proc.stderr.decode("gbk")
        except Exception:
            stderr_text = proc.stderr.decode("utf-8", errors="ignore")
    if proc.returncode != 0:
        print(f"[ERROR] trial {trial_id} {scheduler} failed with code {proc.returncode}")
        print(stdout_text[-2000:])
        print(stderr_text[-2000:])
        return None, log_file

    parsed = parse_log_file(log_file)
    return avg_completion_hours(parsed), log_file


def run_trial(
    trial_id: int,
    params: Dict[str, Any],
    logs_dir: Path,
    python_exe: str,
    parallel_configs: bool = False,
) -> Dict[str, Any]:
    row: Dict[str, Any] = {"trial_id": trial_id, **params}
    per_cfg = []
    if parallel_configs and len(CONFIGS) > 1:
        with ThreadPoolExecutor(max_workers=len(CONFIGS)) as ex:
            fut_map = {ex.submit(run_one, params, cfg, trial_id, logs_dir, python_exe): cfg for cfg in CONFIGS}
            for fut in as_completed(fut_map):
                cfg = fut_map[fut]
                avg_h, log_path = fut.result()
                key = cfg[2]  # heuristic / optimization
                row[f"avg_completion_{key}_hours"] = avg_h
                row[f"log_{key}"] = str(log_path)
                if avg_h is not None:
                    per_cfg.append(avg_h)
    else:
        for cfg in CONFIGS:
            avg_h, log_path = run_one(params, cfg, trial_id, logs_dir, python_exe)
            key = cfg[2]  # heuristic / optimization
            row[f"avg_completion_{key}_hours"] = avg_h
            row[f"log_{key}"] = str(log_path)
            if avg_h is not None:
                per_cfg.append(avg_h)
    row["avg_completion_mean_hours"] = (sum(per_cfg) / len(per_cfg)) if per_cfg else None
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
            for k in ("avg_completion_heuristic_hours", "avg_completion_optimization_hours", "avg_completion_mean_hours"):
                v = row.get(k, None)
                if v is None or str(v).strip() == "":
                    row[k] = None
                else:
                    try:
                        row[k] = float(v)
                    except Exception:
                        row[k] = None
            rows.append(row)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Grid search for proposed strategy params")
    parser.add_argument("--grid-json", type=str, default=None, help="JSON file: {param: [values,...]}")
    parser.add_argument("--max-trials", type=int, default=0, help="0 means run all combinations")
    parser.add_argument("--output-csv", type=str, default="logs/grid_search_proposed_future_results.csv")
    parser.add_argument("--logs-dir", type=str, default="logs/grid_search_future")
    parser.add_argument("--python", type=str, default=sys.executable)
    parser.add_argument("--jobs", type=int, default=8, help="parallel trials to run")
    parser.add_argument("--parallel-configs", action="store_true", help="run heuristic/optimization in parallel inside each trial")
    parser.add_argument("--resume", action="store_true", help="resume from existing output-csv by skipping completed trial_id")
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
        for i, params in pending:
            print(f"[INFO] trial {i}/{len(all_params)} params={params}")
            row = run_trial(i, params, logs_dir, args.python, args.parallel_configs)
            rows.append(row)
            rows.sort(key=lambda r: int(r.get("trial_id", 0)))
            flush_rows_csv(rows, out_csv)
    else:
        with ThreadPoolExecutor(max_workers=jobs) as ex:
            future_map = {}
            for i, params in pending:
                print(f"[INFO] submit trial {i}/{len(all_params)} params={params}")
                fut = ex.submit(run_trial, i, params, logs_dir, args.python, args.parallel_configs)
                future_map[fut] = i
            done_cnt = 0
            for fut in as_completed(future_map):
                trial_id = future_map[fut]
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

    valid_rows = [r for r in rows if r.get("avg_completion_mean_hours") is not None]
    if valid_rows:
        best = min(valid_rows, key=lambda r: r["avg_completion_mean_hours"])
        print("[INFO] best trial by mean completion time:")
        print(json.dumps(best, ensure_ascii=False, indent=2))
    print(f"[INFO] results saved: {out_csv}")


if __name__ == "__main__":
    main()
