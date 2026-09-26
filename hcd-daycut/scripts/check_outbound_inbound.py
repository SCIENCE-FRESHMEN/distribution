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
from typing import Any, Dict, List, Optional, Tuple, cast
import pytz

import pandas as pd


def to_ts(val: Any) -> Optional[pd.Timestamp]:
    """将单元格时间转换为带北京时间时区的时间戳。

    Args:
        val: Excel/CSV 单元格中的时间值或原始字符串。

    Returns:
        带 ``Asia/Shanghai`` 时区的时间；空值或无法解析时返回 ``None``。
    """
    try:
        china_tz = pytz.timezone('Asia/Shanghai')
        dt = val if isinstance(val, pd.Timestamp) else pd.to_datetime(val, errors="coerce")
        # ``to_datetime`` 对列表等非标量输入可能返回索引对象；表格单元格只接受单个时间。
        if not isinstance(dt, pd.Timestamp) or pd.isna(dt):
            return None
        if dt.tzinfo is None:
            dt = china_tz.localize(dt)
        else:
            dt = dt.tz_convert(china_tz)
        # pytz.localize 的静态返回类型是 datetime；统一封装为 pandas Timestamp。
        return cast(pd.Timestamp, pd.Timestamp(dt))
    except Exception:
        return None


def _base_sku(code_raw) -> str:
    """取 '/' 前的部分作为 SKU 基码，去空白；非字符串则转成字符串后处理。

    Args:
        code_raw (Any): 供当前处理流程使用的 `code_raw` 值。

    Returns:
        str: 当前处理得到的文本标识、格式化结果或诊断信息。
    """
    if isinstance(code_raw, str):
        return code_raw.split("/")[0].strip()
    if pd.isna(code_raw):
        return ""
    return str(code_raw).split("/")[0].strip()


def load_inbound_table(path: Path, arrival_col: str, beam_cols: List[str]) -> Dict[str, List[pd.Timestamp]]:
    """加载配置、文件或既有状态，并转换为当前模块可消费的数据。

    输入：path（Path）、arrival_col（str）、beam_cols（List[str]）
    输出：Dict[str, List[pd.Timestamp]]

    Args:
        path (Path): 需要读取、写入或检查的文件路径。
        arrival_col (str): 供当前处理流程使用的 `arrival_col` 值。
        beam_cols (List[str]): 供当前处理流程使用的 `beam_cols` 值。

    Returns:
        Dict[str, List[pd.Timestamp]]: 当前处理得到的文本标识、格式化结果或诊断信息。
    """
    df = pd.read_excel(path) if path.suffix.lower() in [".xlsx", ".xls"] else pd.read_csv(path)
    arrivals: Dict[str, List[pd.Timestamp]] = {}
    for _, row in df.iterrows():
        ts = to_ts(row.get(arrival_col))
        if ts is None:
            continue
        for col in beam_cols:
            base = _base_sku(row.get(col))
            if base:
                arrivals.setdefault(base, []).append(ts)
    for k in arrivals:
        arrivals[k].sort()
    return arrivals


def load_outbound_table(path: Path, creation_col: str, sku_cols: List[str]) -> List[Tuple[pd.Timestamp, List[str], int]]:
    """返回按创建时间排序的任务列表: (creation_time, sku_list, row_idx)

    Args:
        path (Path): 需要读取、写入或检查的文件路径。
        creation_col (str): 供当前处理流程使用的 `creation_col` 值。
        sku_cols (List[str]): 供当前处理流程使用的 `sku_cols` 值。

    Returns:
        List[Tuple[pd.Timestamp, List[str], int]]: 当前处理得到的文本标识、格式化结果或诊断信息。
    """
    df = pd.read_excel(path) if path.suffix.lower() in [".xlsx", ".xls"] else pd.read_csv(path)
    df[creation_col] = df[creation_col].ffill()
    tasks: List[Tuple[pd.Timestamp, List[str], int]] = []
    for idx, row in df.iterrows():
        ct = to_ts(row.get(creation_col))
        if ct is None:
            continue
        skus: List[str] = []
        for col in sku_cols:
            base = _base_sku(row.get(col))
            if base:
                skus.append(base)
        # pandas 的索引可以是任意 Hashable；这里需要的是 Excel 展示行号，
        # 由枚举计数生成，不能直接对原始索引做加一运算。
        tasks.append((ct, skus, len(tasks) + 1))
    tasks.sort(key=lambda x: x[0])
    return tasks


def check_consistency(inbound: Dict[str, List[pd.Timestamp]], outbound_tasks: List[Tuple[pd.Timestamp, List[str], int]], prefer_latest: bool = True) -> List[str]:
    """全局匹配：按出库时间顺序遍历，每个 SKU 使用“最晚但不晚于创建时间”的入库（prefer_latest=True），
    或最早可用的入库（False）。同一 SKU 的入库之间可交换，选最新可用不会让可行解变差。

    Args:
        inbound (Dict[str, List[pd.Timestamp]]): 供当前处理流程使用的 `inbound` 值。
        outbound_tasks (List[Tuple[pd.Timestamp, List[str], int]]): 用于当前计算的任务列表。
        prefer_latest (bool，可选): 供当前处理流程使用的 `prefer_latest` 值。

    Returns:
        List[str]: 当前处理得到的文本标识、格式化结果或诊断信息。
    """
    from bisect import bisect_right
    avail: Dict[str, List[pd.Timestamp]] = {k: sorted(v) for k, v in inbound.items()}

    def fmt_ts(ts: Optional[pd.Timestamp]) -> str:
        """将待展示的入库或出库时间格式化为北京时间文本。"""
        try:
            if ts is None:
                return ""
            return ts.tz_convert(pytz.timezone('Asia/Shanghai')).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return str(ts)

    issues: List[str] = []
    for ct, skus, row_idx in outbound_tasks:
        for sku in skus:
            arrivals = avail.get(sku, [])
            if not arrivals:
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
    """作为脚本入口，解析运行参数并按既定顺序调用本文件的主要处理流程。

    输入：无显式业务输入；依赖实例字段或模块配置。

    Args:
        None: 无显式业务参数；使用实例状态或模块配置。

    Returns:
        None: 通过实例状态、队列或外部副作用完成处理。
    """
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
