"""
Run simulations and save terminal output to log files.

Usage:
    python run_compare.py
    python run_compare.py --jobs 4

By default it runs four combinations (proposed/baseline x heuristic/optimization)
and writes logs to logs/{allocation}-{position}-{scheduler}.txt, e.g.:
    logs/prop-prop-heu.txt
    logs/prop-prop-opt.txt
    logs/base-base-heu.txt
    logs/base-base-opt.txt
"""

import argparse
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Tuple

_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1F]')


def sanitize_filename(name: str) -> str:
    """Sanitize for Windows/NTFS-safe filenames."""
    name = _INVALID_FILENAME_CHARS.sub("_", name).strip(" .")
    return name or "log"


def _decode_bytes(raw: bytes) -> str:
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except Exception:
            pass
    return raw.decode("utf-8", errors="ignore")


def _run_one(
    idx: int,
    total: int,
    cfg: Tuple[str, str, str],
    logs_dir: Path,
    abbreviations: dict,
) -> Tuple[int, Tuple[str, str, str], Path, int, str | None]:
    allocation, position, scheduler = cfg
    allocation_abbr = abbreviations.get(allocation, allocation)
    position_abbr = abbreviations.get(position, position)
    scheduler_abbr = abbreviations.get(scheduler, scheduler)

    log_filename = sanitize_filename(f"{allocation_abbr}-{position_abbr}-{scheduler_abbr}.txt")
    log_file = logs_dir / log_filename

    # run.py internally also logs to --log-file; make it unique per config when parallel.
    internal_log = logs_dir / sanitize_filename(f"run-{allocation_abbr}-{position_abbr}-{scheduler_abbr}.log")

    cmd = [
        "python", "run.py",
        "--inbound-allocation-strategy", allocation,
        "--inbound-position-strategy", position,
        "--scheduler-type", scheduler,
        "--log-file", str(internal_log),
    ]

    print(f"[INFO] submit {idx}/{total}: {allocation}-{position}-{scheduler} -> {log_file}")
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=False)
        text = _decode_bytes(proc.stdout or b"")
        log_file.write_text(text, encoding="utf-8", errors="ignore")
        return idx, cfg, log_file, proc.returncode, None
    except Exception as e:
        return idx, cfg, log_file, -1, str(e)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run compare configs in parallel")
    parser.add_argument("--jobs", type=int, default=0, help="parallel worker count; 0 means auto")
    parser.add_argument(
        "--keep-internal-logs",
        action="store_true",
        help="keep temporary logs like logs/run-*.log (default: delete after all runs)",
    )
    args = parser.parse_args()

    configs = [
        ("proposed", "proposed", "heuristic"),
        ("proposed", "proposed", "optimization"),
        ("baseline", "baseline", "heuristic"),
        ("baseline", "baseline", "optimization"),
    ]

    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)

    abbreviations = {
        "baseline": "base",
        "proposed": "prop",
        "heuristic": "heu",
        "optimization": "opt",
    }

    auto_jobs = min(len(configs), max(1, (os.cpu_count() or 4)))
    jobs = auto_jobs if args.jobs <= 0 else max(1, args.jobs)
    print(f"[INFO] total configs: {len(configs)}, parallel jobs: {jobs}")

    failures = 0
    internal_logs: list[Path] = []
    with ThreadPoolExecutor(max_workers=jobs) as ex:
        futures = {
            ex.submit(_run_one, i, len(configs), cfg, logs_dir, abbreviations): (i, cfg)
            for i, cfg in enumerate(configs, 1)
        }
        for fut in as_completed(futures):
            idx, cfg = futures[fut]
            i, cfg2, log_file, code, err = fut.result()
            allocation, position, scheduler = cfg2
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

    if failures:
        print(f"[INFO] all configs finished, failures={failures}")
    else:
        print("[INFO] all configs finished, all success")

    if not args.keep_internal_logs:
        removed = 0
        for p in set(internal_logs):
            try:
                if p.exists():
                    p.unlink()
                    removed += 1
            except Exception as e:
                print(f"[WARN] failed to remove temp log {p}: {e}")
        if removed:
            print(f"[INFO] removed {removed} temporary run-*.log files")


if __name__ == "__main__":
    main()
