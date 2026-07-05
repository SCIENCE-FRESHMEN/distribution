"""
校验出库所用纵梁是否都已入库，且入库时间早于出库创建时间。

输入均为表格（Excel/CSV）：
1) allocation 表：包含列 纵梁A、纵梁B、到达时间（列名见配置常量）
   - 保留纵梁码完整字符串（不截断"//"后缀）
2) production_plan 表：包含列 零件号（左主/左副/右主/右副）及 创建时间（列名见配置常量）

逻辑：按创建时间排序出库任务；对每个纵梁码，消费最早且不晚于使用时间的入库记录，
若不存在则报告缺失。

运行：直接执行本文件，会使用配置常量中的路径与列名。
"""

import datetime
from pathlib import Path
from typing import Dict, List, Tuple
import pytz

import pandas as pd


def to_ts(val) -> pd.Timestamp:
    """将时间值转为带时区的 Timestamp（北京时间），失败返回 NaT"""
    try:
        china_tz = pytz.timezone('Asia/Shanghai')
        if isinstance(val, pd.Timestamp):
            dt = val
        else:
            dt = pd.to_datetime(val, errors='coerce')
        if pd.isna(dt):
            return pd.NaT
        if dt.tzinfo is None:
            dt = china_tz.localize(dt)
        else:
            dt = dt.tz_convert(china_tz)
        return dt
    except Exception:
        return pd.NaT


def _base_sku(code_raw) -> str:
    """取 '/' 前的部分作为 SKU 基码，去空白；非字符串则转成字符串后处理。"""
    if isinstance(code_raw, str):
        return code_raw.split("/")[0].strip()
    if pd.isna(code_raw):
        return ""
    return str(code_raw).split("/")[0].strip()


def load_inbound_table(path: Path, arrival_col: str, beam_cols: List[str]) -> Dict[str, List[pd.Timestamp]]:
    df = pd.read_excel(path) if path.suffix.lower() in [".xlsx", ".xls"] else pd.read_csv(path)
    arrivals: Dict[str, List[pd.Timestamp]] = {}
    for _, row in df.iterrows():
        ts = to_ts(row.get(arrival_col))
        if pd.isna(ts):
            continue
        for col in beam_cols:
            base = _base_sku(row.get(col))
            if base:
                arrivals.setdefault(base, []).append(ts)
    for k in arrivals:
        arrivals[k].sort()
    return arrivals


def load_outbound_table(path: Path, creation_col: str, sku_cols: List[str]) -> List[Tuple[pd.Timestamp, List[str], int]]:
    """
    返回按创建时间排序的任务列表: (creation_time, sku_list, row_idx)
    """
    df = pd.read_excel(path) if path.suffix.lower() in [".xlsx", ".xls"] else pd.read_csv(path)
    df[creation_col] = df[creation_col].ffill()
    tasks: List[Tuple[pd.Timestamp, List[str], int]] = []
    for idx, row in df.iterrows():
        ct = to_ts(row.get(creation_col))
        if pd.isna(ct):
            continue
        skus: List[str] = []
        for col in sku_cols:
            base = _base_sku(row.get(col))
            if base:
                skus.append(base)
        tasks.append((ct, skus, idx + 1))
    tasks.sort(key=lambda x: x[0])
    return tasks


def check_consistency(inbound: Dict[str, List[pd.Timestamp]], outbound_tasks: List[Tuple[pd.Timestamp, List[str], int]], prefer_latest: bool = True) -> List[str]:
    """
    全局匹配：按出库时间顺序遍历，每个 SKU 使用“最晚但不晚于创建时间”的入库（prefer_latest=True），
    或最早可用的入库（False）。同一 SKU 的入库之间可交换，选最新可用不会让可行解变差。
    """
    from bisect import bisect_right
    avail: Dict[str, List[pd.Timestamp]] = {k: sorted(v) for k, v in inbound.items()}

    def fmt_ts(ts: pd.Timestamp) -> str:
        try:
            if pd.isna(ts):
                return ""
            return ts.tz_convert(pytz.timezone('Asia/Shanghai')).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return str(ts)

    issues: List[str] = []
    for ct, skus, row_idx in outbound_tasks:
        for sku in skus:
            arrivals = avail.get(sku, [])
            if not arrivals or pd.isna(ct):
                issues.append(
                    f"[MISSING] SKU {sku} 需要于 {fmt_ts(ct)} (plan row {row_idx}) ，但无任何入库记录"
                )
                continue

            if prefer_latest:
                idx = bisect_right(arrivals, ct) - 1  # 最新的不晚于 ct
            else:
                idx = 0  # 最早可用

            if 0 <= idx < len(arrivals) and arrivals[idx] <= ct:
                arrivals.pop(idx)  # 消费该入库
                avail[sku] = arrivals
                continue

            # 无可用入库，输出缺失信息
            if arrivals:
                remaining = len(arrivals)
                earliest = fmt_ts(arrivals[0])
                latest = fmt_ts(arrivals[-1])
                issues.append(
                    f"[MISSING] SKU {sku} 需要于 {fmt_ts(ct)} (plan row {row_idx}) "
                    f"，剩余可用入库 {remaining} 条，最早 {earliest}，最晚 {latest}，但均晚于创建时间"
                )
    return issues


def main():
    # ===== 配置：如需调整路径/列名，修改下方常量 =====
    inbound_table = Path("simulation/data/daily/inbound_20251026.xlsx")
    outbound_table = Path("simulation/data/daily/production_plan_20251026.xlsx")
    arrival_col = "到达时间"
    beam_cols = ["纵梁A", "纵梁B"]
    creation_col = "开始时间"
    outbound_sku_cols = ["零件号（左主）", "零件号（左副）", "零件号（右主）", "零件号（右副）"]
    # ===============================================

    inbound = load_inbound_table(inbound_table, arrival_col=arrival_col, beam_cols=beam_cols)
    outbound_tasks = load_outbound_table(outbound_table, creation_col=creation_col, sku_cols=outbound_sku_cols)

    issues = check_consistency(inbound, outbound_tasks, prefer_latest=True)

    print(f"Inbound unique beams: {len(inbound)}")
    print(f"Outbound tasks: {len(outbound_tasks)}")
    if issues:
        print(f"\nFound {len(issues)} issues:")
        for msg in issues:
            print(msg)
    else:
        print("\nAll outbound tasks have required beams with prior inbound arrivals.")


if __name__ == "__main__":
    main()
