"""
优化权重参数搜索
输入：配置文件、日志文件、权重参数网格、日志文件列表
输出：CSV 文件，包含每个权重参数组合的性能指标
该模块用于在给定的优化权重参数网格上执行网格搜索，并输出性能指标。
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from visualize_results import DAYS_FILTER, _filter_days, parse_log_file


WEIGHT_KEYS = [
    "lr_balance_weight",
    "makespan_weight",
    "balance_weight",
    "production_line_avg_time_weight",
    "production_line_balance_weight",
    "aisle_dispersion_weight",
    "inbound_wait_weight",
    "outbound_choice_bonus_weight",
]

# 权重参数网格
DEFAULT_GRID: Dict[str, List[Any]] = {
    "lr_balance_weight": [0.05],
    "makespan_weight": [0.01, 0.05],
    "balance_weight": [0.1],
    "production_line_avg_time_weight": [0.01],
    "production_line_balance_weight": [0.01],
    "aisle_dispersion_weight": [0.5],
    "inbound_wait_weight": [-0.01, 0.01, 0.05, 0.1],
    "outbound_choice_bonus_weight": [1.0],

}

# 比较的配置
CONFIGS: List[Tuple[str, str, str]] = [
    ("proposed", "proposed", "optimization"),
    ("baseline", "baseline", "optimization"),
]


DAY_HEADER_RE = re.compile(r"===\s*第\s*(\d+)\s*天\s*===")
OUTBOUND_DONE_RE = re.compile(r"出库任务.*?起止\s*[\d.]+s~([\d.]+)s")
DAILY_RELOCATION_RE = re.compile(r"移库数量:\s*(\d+)\s*\(新增\)")
FINAL_BALANCE_RE = re.compile(r"最终库存均衡度:\s*([-\d.]+)")
PAIRING_RE = re.compile(r"\[DAY\s+\d+\s+结束\].*?梁配对率\(含solo\):\s*\d+/\d+\s*=\s*([\d.]+)%")
COMPLETION_PATTERNS = [
    re.compile(r"\[INFO\]\[DAY\s+\d+\]\s*产线(\d+)\s+当日计划已完成：\s*(\d+)/(\d+)\s*组"),
    re.compile(r"\[WARN\]\[DAY\s+\d+\]\s*产线(\d+)\s+当日计划未完成：已完成\s*(\d+)/(\d+)\s*组"),
]


def parse_completion_ratio(log_text: str) -> float | None:
    """解析原始输入，提取本模块后续判断所需的结构化字段。

    输入：log_text（str）
    输出：float | None

    Args:
        log_text (str): 供当前处理流程使用的 `log_text` 值。

    Returns:
        float | None: 当前处理流程产生的结果；具体结构由函数摘要说明。
    """
    total_done = 0
    total_groups = 0
    found = False
    for pattern in COMPLETION_PATTERNS:
        for match in pattern.finditer(log_text):
            _line_id = int(match.group(1))
            done = int(match.group(2))
            total = int(match.group(3))
            total_done += done
            total_groups += total
            found = True
    if not found:
        return None
    if total_groups <= 0:
        return None
    return total_done / total_groups


def load_grid(path: str | None) -> Dict[str, List[Any]]:
    """加载配置、文件或既有状态，并转换为当前模块可消费的数据。

    输入：path（str | None）
    输出：Dict[str, List[Any]]

    Args:
        path (str | None): 需要读取、写入或检查的文件路径。

    Returns:
        Dict[str, List[Any]]: 当前处理得到的文本标识、格式化结果或诊断信息。
    """
    if not path:
        return {k: list(v) for k, v in DEFAULT_GRID.items()}

    obj = json.loads(Path(path).read_text(encoding="utf-8"))
    grid: Dict[str, List[Any]] = {}
    for key in WEIGHT_KEYS:
        vals = obj.get(key)
        grid[key] = vals if isinstance(vals, list) and vals else list(DEFAULT_GRID[key])
    return grid


def iter_param_combinations(grid: Dict[str, List[Any]]) -> Iterable[Dict[str, Any]]:
    """执行 `iter_param_combinations` 对应的模块处理步骤，并返回该步骤产生的结果。

    输入：grid（Dict[str, List[Any]]）
    输出：Iterable[Dict[str, Any]]

    Args:
        grid (Dict[str, List[Any]]): 供当前处理流程使用的 `grid` 值。

    Returns:
        Iterable[Dict[str, Any]]: 当前处理得到的文本标识、格式化结果或诊断信息。
    """
    value_lists = [grid[k] for k in WEIGHT_KEYS]
    for values in itertools.product(*value_lists):
        yield {k: v for k, v in zip(WEIGHT_KEYS, values)}


def decode_bytes(raw: bytes) -> str:
    """执行 `decode_bytes` 对应的模块处理步骤，并返回该步骤产生的结果。

    输入：raw（bytes）
    输出：str

    Args:
        raw (bytes): 供当前处理流程使用的 `raw` 值。

    Returns:
        str: 当前处理得到的文本标识、格式化结果或诊断信息。
    """
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except Exception:
            pass
    return raw.decode("utf-8", errors="ignore")


def parse_log_metrics(log_path: Path) -> Dict[str, Any]:
    """解析原始输入，提取本模块后续判断所需的结构化字段。

    输入：log_path（Path）
    输出：Dict[str, Any]

    Args:
        log_path (Path): 供当前处理流程使用的 `log_path` 值。

    Returns:
        Dict[str, Any]: 当前处理得到的文本标识、格式化结果或诊断信息。
    """
    metrics: Dict[str, Any] = {
        "avg_completion_hours": None,
        "total_relocations": None,
        "final_balance": None,
        "last_pairing_with_solo_pct": None,
        "completion_ratio": None,
        "unfinished": False,
    }
    if not log_path.exists():
        return metrics

    text = decode_bytes(log_path.read_bytes())
    metrics["completion_ratio"] = parse_completion_ratio(text)
    metrics["unfinished"] = (
        metrics["completion_ratio"] is not None and metrics["completion_ratio"] < 1.0
    )
    parsed = parse_log_file(log_path)
    parsed = _filter_days(parsed, DAYS_FILTER) if DAYS_FILTER else parsed

    completion_vals: List[float] = []
    if not metrics["unfinished"]:
        for day in sorted(parsed.get("task_completion_details", {}).keys()):
            outbound_tasks = [
                t for t in parsed["task_completion_details"].get(day, [])
                if t.get("type") == "出库"
            ]
            end_time = max((t.get("end_time", 0.0) for t in outbound_tasks), default=0.0)
            if end_time > 0:
                completion_vals.append(end_time / 3600.0)

        if completion_vals:
            metrics["avg_completion_hours"] = sum(completion_vals) / len(completion_vals)

    metrics["total_relocations"] = parsed.get("total_relocations")

    m_balance = FINAL_BALANCE_RE.findall(text)
    if m_balance:
        metrics["final_balance"] = float(m_balance[-1])

    pairing_rates = parsed.get("pairing_rates", {})
    numeric_times = [t for t in pairing_rates.keys() if isinstance(t, (int, float))]
    if numeric_times:
        last_pairing = pairing_rates[max(numeric_times)]
        metrics["last_pairing_with_solo_pct"] = float(last_pairing.get("beam_with_solo", 0.0)) * 100.0

    return metrics


def run_one(
    weights: Dict[str, Any],
    config: Tuple[str, str, str],
    trial_id: int,
    logs_dir: Path,
    python_exe: str,
) -> Tuple[Dict[str, Any] | None, Path]:
    """执行 `run_one` 对应的模块处理步骤，并返回该步骤产生的结果。

    输入：weights（Dict[str, Any]）、config（Tuple[str, str, str]）、trial_id（int）、logs_dir（Path）、python_exe（str）
    输出：Tuple[Dict[str, Any] | None, Path]

    Args:
        weights (Dict[str, Any]): 供当前处理流程使用的 `weights` 值。
        config (Tuple[str, str, str]): 本次加载或构造使用的配置字典。
        trial_id (int): 当前对象的唯一标识。
        logs_dir (Path): 供当前处理流程使用的 `logs_dir` 值。
        python_exe (str): 供当前处理流程使用的 `python_exe` 值。

    Returns:
        Tuple[Dict[str, Any] | None, Path]: 当前处理得到的文本标识、格式化结果或诊断信息。
    """
    allocation, position, scheduler = config
    short_name = f"{allocation[:4]}-{position[:4]}-{scheduler[:3]}"
    log_file = logs_dir / f"trial{trial_id:04d}-{short_name}.log"

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
        "--outbound-choice-bonus-weight",
        str(weights["outbound_choice_bonus_weight"]),
    ]

    proc = subprocess.run(cmd, capture_output=True, text=False)
    stdout_text = decode_bytes(proc.stdout)
    stderr_text = decode_bytes(proc.stderr)
    log_text = stdout_text
    if stderr_text.strip():
        if log_text and not log_text.endswith("\n"):
            log_text += "\n"
        log_text += "\n[STDERR]\n" + stderr_text
    log_file.write_text(log_text, encoding="utf-8")

    if proc.returncode != 0:
        print(f"[ERROR] trial {trial_id} {allocation}-{position}-{scheduler} failed: {proc.returncode}")
        stdout_tail = stdout_text[-1200:]
        stderr_tail = stderr_text[-1200:]
        if stdout_tail.strip():
            print(stdout_tail)
        if stderr_tail.strip():
            print(stderr_tail)
        return None, log_file

    return parse_log_metrics(log_file), log_file


def run_trial(
    trial_id: int,
    weights: Dict[str, Any],
    logs_dir: Path,
    python_exe: str,
    parallel_configs: bool,
) -> Dict[str, Any]:
    """执行 `run_trial` 对应的模块处理步骤，并返回该步骤产生的结果。

    输入：trial_id（int）、weights（Dict[str, Any]）、logs_dir（Path）、python_exe（str）、parallel_configs（bool）
    输出：Dict[str, Any]

    Args:
        trial_id (int): 当前对象的唯一标识。
        weights (Dict[str, Any]): 供当前处理流程使用的 `weights` 值。
        logs_dir (Path): 供当前处理流程使用的 `logs_dir` 值。
        python_exe (str): 供当前处理流程使用的 `python_exe` 值。
        parallel_configs (bool): 供当前处理流程使用的 `parallel_configs` 值。

    Returns:
        Dict[str, Any]: 当前处理得到的文本标识、格式化结果或诊断信息。
    """
    row: Dict[str, Any] = {"trial_id": trial_id, **weights}

    has_unfinished = False
    avg_completion_vals: List[float] = []
    relocation_vals: List[float] = []
    pairing_vals: List[float] = []

    def _store_result(cfg: Tuple[str, str, str], metrics: Dict[str, Any] | None, log_path: Path) -> None:
        """执行 `_store_result` 对应的模块处理步骤，并返回该步骤产生的结果。

        输入：cfg（Tuple[str, str, str]）、metrics（Dict[str, Any] | None）、log_path（Path）
        输出：None

        """
        nonlocal has_unfinished
        key = f"{cfg[0]}_{cfg[1]}_{cfg[2]}"
        row[f"log_{key}"] = str(log_path)
        if metrics is None:
            row[f"avg_completion_{key}_hours"] = None
            row[f"relocations_{key}"] = None
            row[f"final_balance_{key}"] = None
            row[f"pairing_with_solo_{key}_pct"] = None
            return

        if metrics.get("unfinished"):
            has_unfinished = True
            row[f"avg_completion_{key}_hours"] = "未完成"
        else:
            row[f"avg_completion_{key}_hours"] = metrics["avg_completion_hours"]
        row[f"relocations_{key}"] = metrics["total_relocations"]
        row[f"final_balance_{key}"] = metrics["final_balance"]
        row[f"pairing_with_solo_{key}_pct"] = metrics["last_pairing_with_solo_pct"]

        if (not metrics.get("unfinished")) and metrics["avg_completion_hours"] is not None:
            avg_completion_vals.append(float(metrics["avg_completion_hours"]))
        if metrics["total_relocations"] is not None:
            relocation_vals.append(float(metrics["total_relocations"]))
        if metrics["last_pairing_with_solo_pct"] is not None:
            pairing_vals.append(float(metrics["last_pairing_with_solo_pct"]))

    if parallel_configs:
        with ThreadPoolExecutor(max_workers=len(CONFIGS)) as ex:
            fut_map = {
                ex.submit(run_one, weights, cfg, trial_id, logs_dir, python_exe): cfg
                for cfg in CONFIGS
            }
            for fut in as_completed(fut_map):
                cfg = fut_map[fut]
                metrics, log_path = fut.result()
                _store_result(cfg, metrics, log_path)
    else:
        for cfg in CONFIGS:
            metrics, log_path = run_one(weights, cfg, trial_id, logs_dir, python_exe)
            _store_result(cfg, metrics, log_path)

    row["avg_completion_mean_hours"] = (
        "未完成"
        if has_unfinished
        else (sum(avg_completion_vals) / len(avg_completion_vals) if avg_completion_vals else None)
    )
    row["relocations_mean"] = (
        sum(relocation_vals) / len(relocation_vals) if relocation_vals else None
    )
    row["pairing_with_solo_mean_pct"] = (
        sum(pairing_vals) / len(pairing_vals) if pairing_vals else None
    )
    return row


def flush_rows_csv(rows: List[Dict[str, Any]], out_csv: Path) -> None:
    """执行 `flush_rows_csv` 对应的模块处理步骤，并返回该步骤产生的结果。

    输入：rows（List[Dict[str, Any]]）、out_csv（Path）
    输出：None

    Args:
        rows (List[Dict[str, Any]]): 供当前处理流程使用的 `rows` 值。
        out_csv (Path): 供当前处理流程使用的 `out_csv` 值。

    Returns:
        None: 不返回业务数据；处理结果写入实例状态、传入对象或外部响应。
    """
    if not rows:
        return
    with out_csv.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def load_existing_rows(out_csv: Path) -> List[Dict[str, Any]]:
    """加载配置、文件或既有状态，并转换为当前模块可消费的数据。

    输入：out_csv（Path）
    输出：List[Dict[str, Any]]

    Args:
        out_csv (Path): 供当前处理流程使用的 `out_csv` 值。

    Returns:
        List[Dict[str, Any]]: 当前处理得到的文本标识、格式化结果或诊断信息。
    """
    if not out_csv.exists():
        return []
    rows: List[Dict[str, Any]] = []
    with out_csv.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                row["trial_id"] = int(str(row.get("trial_id", "")).strip())
            except Exception:
                continue
            rows.append(dict(row))
    return rows


def to_float_or_none(value: Any) -> float | None:
    """执行 `to_float_or_none` 对应的模块处理步骤，并返回该步骤产生的结果。

    输入：value（Any）
    输出：float | None

    Args:
        value (Any): 待解析、格式化或计算的原始值。

    Returns:
        float | None: 当前处理流程产生的结果；具体结构由函数摘要说明。
    """
    if value in (None, ""):
        return None
    try:
        return float(value)
    except Exception:
        return None


def print_best_row(rows: List[Dict[str, Any]], metric: str) -> None:
    """执行 `print_best_row` 对应的模块处理步骤，并返回该步骤产生的结果。

    输入：rows（List[Dict[str, Any]]）、metric（str）
    输出：None

    Args:
        rows (List[Dict[str, Any]]): 供当前处理流程使用的 `rows` 值。
        metric (str): 供当前处理流程使用的 `metric` 值。

    Returns:
        None: 不返回业务数据；处理结果写入实例状态、传入对象或外部响应。
    """
    valid_rows = [r for r in rows if to_float_or_none(r.get(metric)) is not None]
    if not valid_rows:
        print(f"[WARN] no valid rows for metric: {metric}")
        return

    best = min(valid_rows, key=lambda r: float(r[metric]))
    print(f"[INFO] best trial by {metric}:")
    print(json.dumps(best, ensure_ascii=False, indent=2))


def main() -> None:
    """作为脚本入口，解析运行参数并按既定顺序调用本文件的主要处理流程。

    输入：无显式业务输入；依赖实例字段或模块配置。
    输出：None

    Args:
        None: 无显式业务参数；使用实例状态或模块配置。

    Returns:
        None: 不返回业务数据；处理结果写入实例状态、传入对象或外部响应。
    """
    parser = argparse.ArgumentParser(description="Grid search for optimization weight params")
    # 自定义待搜索参数网格 JSON；未传入时使用脚本定义的默认网格。
    parser.add_argument("--grid-json", type=str, default=None, help="JSON file: {param: [values,...]}")
    # 限制尝试组合数；0 表示遍历全部参数笛卡尔积。
    parser.add_argument("--max-trials", type=int, default=0, help="0 means run all combinations")
    # 输出每组权重和指标结果的 CSV 文件路径。
    parser.add_argument("--output-csv", type=str, default="logs/opt_weight_search_results.csv")
    # 各次策略仿真的独立日志输出目录。
    parser.add_argument("--logs-dir", type=str, default="logs/opt_weight_search")
    # 子进程运行 run.py 时使用的 Python 解释器路径。
    parser.add_argument("--python", type=str, default=sys.executable)
    # 同时执行的参数组合数量。
    parser.add_argument("--jobs", type=int, default=8, help="parallel trials")
    # 每组参数内并行运行两个必测配置，缩短单次试验时间。
    parser.add_argument(
        "--parallel-configs",
        dest="parallel_configs",
        action="store_true",
        help="run the two required configs in parallel inside each trial",
    )
    # 将同一参数组合内的两个配置改为串行，适用于机器资源有限的情况。
    parser.add_argument(
        "--no-parallel-configs",
        dest="parallel_configs",
        action="store_false",
        help="run the two required configs sequentially inside each trial",
    )
    parser.set_defaults(parallel_configs=True)
    # 读取既有输出 CSV，跳过已经完成的参数组合。
    parser.add_argument("--resume", action="store_true", help="resume from existing output-csv")
    # 选择打印最优试验时使用的排序指标。
    parser.add_argument(
        "--rank-metric",
        type=str,
        default="avg_completion_mean_hours",
        choices=[
            "avg_completion_mean_hours",
            "relocations_mean",
        ],
        help="metric used to print the best trial",
    )
    args = parser.parse_args()

    grid = load_grid(args.grid_json)
    all_params = list(iter_param_combinations(grid))
    if args.max_trials and args.max_trials > 0:
        all_params = all_params[: args.max_trials]

    logs_dir = Path(args.logs_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)
    out_csv = Path(args.output_csv)
    out_csv.parent.mkdir(parents=True, exist_ok=True)

    print("[INFO] required configs:")
    for cfg in CONFIGS:
        print(f"  - {cfg[0]}-{cfg[1]}-{cfg[2]}")
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
                done_cnt += 1
                try:
                    row = fut.result()
                    rows.append(row)
                    print(f"[INFO] completed trial {trial_id} ({done_cnt}/{len(all_params)})")
                    rows.sort(key=lambda r: int(r.get("trial_id", 0)))
                    flush_rows_csv(rows, out_csv)
                except Exception as exc:
                    print(f"[ERROR] trial {trial_id} failed: {exc}")

    print_best_row(rows, args.rank_metric)
    print(f"[INFO] results saved: {out_csv}")


if __name__ == "__main__":
    main()
