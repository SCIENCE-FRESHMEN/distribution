import argparse
import copy
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from simulation.warehouse_core import load_warehouse_config


DEFAULT_TASK_MULTIPLIER = 2
TARGET_NUM_AISLES = 8
STANDARD_AISLE_COUNT = 6
STANDARD_AISLE_DIMENSIONS = {"rows": 2, "columns": 17, "levels": 5}
SPECIAL_AISLE_DIMENSIONS = {"rows": 2, "columns": 11, "levels": 5}
DEFAULT_LOGS_SUBDIR = "compare_2x_8_aisles"

_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1F]')


def sanitize_filename(name: str) -> str:
    name = _INVALID_FILENAME_CHARS.sub("_", name).strip(" .")
    return name or "log"


def _decode_bytes(raw: bytes) -> str:
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except Exception:
            pass
    return raw.decode("utf-8", errors="ignore")


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def dump_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def build_aisle_dimensions() -> dict[str, dict[str, int]]:
    dims: dict[str, dict[str, int]] = {}
    for aisle in range(1, TARGET_NUM_AISLES + 1):
        if aisle <= STANDARD_AISLE_COUNT:
            dims[str(aisle)] = dict(STANDARD_AISLE_DIMENSIONS)
        else:
            dims[str(aisle)] = dict(SPECIAL_AISLE_DIMENSIONS)
    return dims


def build_disabled_positions(
    aisle_dimensions: dict[str, dict[str, int]],
    template_rows: tuple[int, ...] = (1,),
    edge_columns_only: bool = True,
) -> list[str]:
    positions: list[str] = []
    for aisle_str, dims in aisle_dimensions.items():
        aisle = int(aisle_str)
        rows = int(dims["rows"])
        columns = int(dims["columns"])
        levels = int(dims["levels"])
        target_rows = [row for row in template_rows if 1 <= row <= rows]
        target_columns = [1, columns] if edge_columns_only else list(range(1, columns + 1))
        for row in target_rows:
            for column in target_columns:
                for level in range(1, levels + 1):
                    positions.append(f"{aisle}-{row}-{column:02d}-{level:02d}")
    return positions


def expand_warehouse_config(base_config: dict[str, Any], task_multiplier: int) -> dict[str, Any]:
    expanded = copy.deepcopy(base_config)
    aisle_dimensions = build_aisle_dimensions()

    expanded["num_aisles"] = TARGET_NUM_AISLES
    expanded["num_rows"] = STANDARD_AISLE_DIMENSIONS["rows"]
    expanded["num_columns"] = STANDARD_AISLE_DIMENSIONS["columns"]
    expanded["num_levels"] = STANDARD_AISLE_DIMENSIONS["levels"]
    expanded["aisle_dimensions"] = aisle_dimensions
    expanded["aisle_production_line_mapping"] = {
        str(aisle): [1, 2, 3, 4] for aisle in range(1, TARGET_NUM_AISLES + 1)
    }
    expanded["aisle_forbidden"] = {
        str(aisle): ({"skid_type": ["1", 1]} if aisle <= STANDARD_AISLE_COUNT else {})
        for aisle in range(1, TARGET_NUM_AISLES + 1)
    }
    expanded["outbound_match_features"] = {
        str(aisle): ["rfid"] for aisle in range(1, TARGET_NUM_AISLES + 1)
    }

    total_positions = sum(
        int(dims["rows"]) * int(dims["columns"]) * int(dims["levels"])
        for dims in aisle_dimensions.values()
    )
    expanded["total_positions"] = total_positions
    expanded["max_beams"] = total_positions
    expanded["initial_inventory_count"] = int(base_config.get("initial_inventory_count", 0)) * task_multiplier
    expanded["disabled_positions"] = build_disabled_positions(aisle_dimensions)
    return expanded


def duplicate_sku_ids(value: Any, suffix: str) -> Any:
    if isinstance(value, dict):
        duplicated: dict[str, Any] = {}
        for key, item in value.items():
            if key == "skuId" and isinstance(item, str):
                duplicated[key] = f"{item}{suffix}"
            else:
                duplicated[key] = duplicate_sku_ids(item, suffix)
        return duplicated
    if isinstance(value, list):
        return [duplicate_sku_ids(item, suffix) for item in value]
    return value


def expand_inbound_config(base_config: dict[str, Any], task_multiplier: int) -> dict[str, Any]:
    inbound_records = base_config.get("inbound_records", []) or []
    expanded_records: list[Any] = []
    for record in inbound_records:
        for copy_idx in range(1, task_multiplier + 1):
            expanded_records.append(duplicate_sku_ids(record, f"__copy{copy_idx}"))
    return {"inbound_records": expanded_records}


def expand_outbound_config(base_config: dict[str, Any], task_multiplier: int) -> dict[str, Any]:
    production_plan = base_config.get("production_plan", {}) or {}
    creation_times = base_config.get("creation_times", {}) or {}

    expanded_plan: dict[str, list[Any]] = {}
    expanded_creation_times: dict[str, list[Any]] = {}

    for line, groups in production_plan.items():
        line_key = str(line)
        line_creation_times = list(creation_times.get(line_key, creation_times.get(int(line) if str(line).isdigit() else line, [])) or [])
        expanded_groups: list[Any] = []
        expanded_times: list[Any] = []

        for group_idx, group in enumerate(groups or []):
            creation_time = line_creation_times[group_idx] if group_idx < len(line_creation_times) else None
            for copy_idx in range(1, task_multiplier + 1):
                expanded_groups.append(duplicate_sku_ids(group, f"__copy{copy_idx}"))
                if creation_time is not None:
                    expanded_times.append(creation_time)

        expanded_plan[line_key] = expanded_groups
        if expanded_times:
            expanded_creation_times[line_key] = expanded_times

    expanded = {"production_plan": expanded_plan}
    if expanded_creation_times:
        expanded["creation_times"] = expanded_creation_times
    return expanded


def write_expanded_configs(
    warehouse_src: Path,
    inbound_src: Path,
    outbound_src: Path,
    output_dir: Path,
    task_multiplier: int,
) -> tuple[Path, Path, Path]:
    warehouse_cfg = expand_warehouse_config(load_warehouse_config(str(warehouse_src)), task_multiplier)
    inbound_cfg = expand_inbound_config(load_json(inbound_src), task_multiplier)
    outbound_cfg = expand_outbound_config(load_json(outbound_src), task_multiplier)

    suffix = f"{task_multiplier}x_{TARGET_NUM_AISLES}_aisles"
    warehouse_out = output_dir / f"warehouse.{suffix}.json"
    inbound_out = output_dir / f"inbound_task_config.{suffix}.json"
    outbound_out = output_dir / f"outbound_task_config.{suffix}.json"
    dump_json(warehouse_out, warehouse_cfg)
    dump_json(inbound_out, inbound_cfg)
    dump_json(outbound_out, outbound_cfg)
    return warehouse_out, inbound_out, outbound_out


def run_one(
    idx: int,
    total: int,
    cfg: tuple[str, str, str],
    logs_dir: Path,
    inbound_config: Path,
    outbound_config: Path,
    abbreviations: dict[str, str],
) -> tuple[int, tuple[str, str, str], Path, int, str | None]:
    allocation, position, scheduler = cfg
    allocation_abbr = abbreviations.get(allocation, allocation)
    position_abbr = abbreviations.get(position, position)
    scheduler_abbr = abbreviations.get(scheduler, scheduler)

    log_filename = sanitize_filename(f"{allocation_abbr}-{position_abbr}-{scheduler_abbr}.txt")
    log_file = logs_dir / log_filename
    internal_log = logs_dir / sanitize_filename(f"run-{allocation_abbr}-{position_abbr}-{scheduler_abbr}.log")

    cmd = [
        sys.executable,
        "run.py",
        "--inbound-config",
        str(inbound_config),
        "--outbound-config",
        str(outbound_config),
        "--inbound-allocation-strategy",
        allocation,
        "--inbound-position-strategy",
        position,
        "--scheduler-type",
        scheduler,
        "--log-file",
        str(internal_log),
    ]

    print(f"[INFO] submit {idx}/{total}: {allocation}-{position}-{scheduler} -> {log_file}")
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=False)
        text = _decode_bytes(proc.stdout or b"")
        log_file.write_text(text, encoding="utf-8", errors="ignore")
        return idx, cfg, log_file, proc.returncode, None
    except Exception as exc:
        return idx, cfg, log_file, -1, str(exc)


def main() -> None:
    parser = argparse.ArgumentParser(description="扩展仓库巷道并按指定倍数复制出入库任务后，批量运行四种策略对比")
    parser.add_argument("--jobs", type=int, default=0, help="并行任务数，0表示自动")
    parser.add_argument("--keep-internal-logs", action="store_true", help="保留 run.py 生成的内部日志")
    parser.add_argument("--logs-subdir", type=str, default=DEFAULT_LOGS_SUBDIR, help="输出日志子目录名")
    parser.add_argument("--warehouse-config", type=str, default="config/warehouse.json", help="源仓库配置路径")
    parser.add_argument("--inbound-config", type=str, default="simulation/data/inbound_task_config.json", help="源入库配置路径")
    parser.add_argument("--outbound-config", type=str, default="simulation/data/outbound_task_config.json", help="源出库配置路径")
    parser.add_argument("--task-multiplier", type=int, default=DEFAULT_TASK_MULTIPLIER, help="任务复制倍数，默认2")
    args = parser.parse_args()

    if args.task_multiplier < 1:
        raise ValueError("--task-multiplier 必须大于等于 1")

    configs = [
        ("proposed", "proposed", "heuristic"),
        ("proposed", "proposed", "optimization"),
        ("baseline", "baseline", "heuristic"),
        ("baseline", "baseline", "optimization"),
    ]
    abbreviations = {
        "baseline": "base",
        "proposed": "prop",
        "heuristic": "heu",
        "optimization": "opt",
    }

    logs_dir = Path("logs") / sanitize_filename(args.logs_subdir)
    generated_dir = logs_dir / "generated_configs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    warehouse_src = Path(args.warehouse_config)
    inbound_src = Path(args.inbound_config)
    outbound_src = Path(args.outbound_config)
    warehouse_generated, inbound_generated, outbound_generated = write_expanded_configs(
        warehouse_src=warehouse_src,
        inbound_src=inbound_src,
        outbound_src=outbound_src,
        output_dir=generated_dir,
        task_multiplier=args.task_multiplier,
    )

    print(f"[INFO] generated warehouse config: {warehouse_generated}")
    print(f"[INFO] generated inbound config: {inbound_generated}")
    print(f"[INFO] generated outbound config: {outbound_generated}")

    original_warehouse_text = warehouse_src.read_text(encoding="utf-8")
    generated_warehouse_text = warehouse_generated.read_text(encoding="utf-8")

    auto_jobs = min(len(configs), max(1, (os.cpu_count() or 4)))
    jobs = auto_jobs if args.jobs <= 0 else max(1, args.jobs)
    print(f"[INFO] total configs: {len(configs)}, parallel jobs: {jobs}")

    failures = 0
    internal_logs: list[Path] = []

    try:
        warehouse_src.write_text(generated_warehouse_text, encoding="utf-8")
        with ThreadPoolExecutor(max_workers=jobs) as executor:
            futures = {
                executor.submit(
                    run_one,
                    idx,
                    len(configs),
                    cfg,
                    logs_dir,
                    inbound_generated,
                    outbound_generated,
                    abbreviations,
                ): (idx, cfg)
                for idx, cfg in enumerate(configs, 1)
            }
            for future in as_completed(futures):
                _, _ = futures[future]
                i, cfg, log_file, code, err = future.result()
                allocation, position, scheduler = cfg
                name = f"{allocation}-{position}-{scheduler}"
                if code == 0:
                    print(f"[INFO] done {i}/{len(configs)}: {name}, log={log_file}")
                else:
                    failures += 1
                    if err:
                        print(f"[ERROR] failed {i}/{len(configs)}: {name}, err={err}")
                    else:
                        print(f"[ERROR] failed {i}/{len(configs)}: {name}, returncode={code}, log={log_file}")

                allocation_abbr = abbreviations.get(allocation, allocation)
                position_abbr = abbreviations.get(position, position)
                scheduler_abbr = abbreviations.get(scheduler, scheduler)
                internal_logs.append(logs_dir / sanitize_filename(f"run-{allocation_abbr}-{position_abbr}-{scheduler_abbr}.log"))
    finally:
        warehouse_src.write_text(original_warehouse_text, encoding="utf-8")
        print(f"[INFO] restored warehouse config: {warehouse_src}")

    if failures:
        print(f"[INFO] all configs finished, failures={failures}")
    else:
        print("[INFO] all configs finished, all success")

    if not args.keep_internal_logs:
        removed = 0
        for path in set(internal_logs):
            try:
                if path.exists():
                    path.unlink()
                    removed += 1
            except Exception as exc:
                print(f"[WARN] failed to remove temp log {path}: {exc}")
        if removed:
            print(f"[INFO] removed {removed} temporary run-*.log files")


if __name__ == "__main__":
    main()
