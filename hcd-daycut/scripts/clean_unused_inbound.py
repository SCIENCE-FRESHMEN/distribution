"""
清理未使用的入库记录。

根据出库任务中实际使用的纵梁，清理入库表中未被使用的记录。
对于双梁入库记录，如果只有一个纵梁被使用，则只保留使用过的纵梁信息；
如果两个纵梁都没有被使用，则删除整条记录。
"""

import datetime
from pathlib import Path
from typing import Dict, List, Tuple
import pandas as pd
import pytz


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


def load_inbound_table_with_details(path: Path, arrival_col: str, beam_cols: List[str]) -> List[Tuple[pd.Timestamp, List[str], dict]]:
    """
    加载完整的入库表，包含所有原始信息
    返回: [(arrival_time, [beam_codes], original_row_dict), ...]
    """
    df = pd.read_excel(path) if path.suffix.lower() in [".xlsx", ".xls"] else pd.read_csv(path)
    inbound_rows: List[Tuple[pd.Timestamp, List[str], dict]] = []
    for _, row in df.iterrows():
        ts = to_ts(row.get(arrival_col))
        beams: List[str] = []
        for col in beam_cols:
            base = _base_sku(row.get(col))
            if base:
                beams.append(base)
        inbound_rows.append((ts, beams, dict(row)))
    return inbound_rows


def load_outbound_table(path: Path, creation_col: str, sku_cols: List[str]) -> List[Tuple[pd.Timestamp, List[str], int]]:
    """
    返回按创建时间排序的任务列表: (creation_time, sku_list, row_idx)
    """
    df = pd.read_excel(path) if path.suffix.lower() in [".xlsx", ".xls"] else pd.read_csv(path)
    # 添加缺失值处理，对时间列进行前向填充
    df[creation_col] = df[creation_col].fillna(method='ffill')
    tasks: List[Tuple[pd.Timestamp, List[str], int]] = []
    for idx, row in df.iterrows():
        ct = to_ts(row.get(creation_col))
        skus: List[str] = []
        for col in sku_cols:
            base = _base_sku(row.get(col))
            if base:
                skus.append(base)
        tasks.append((ct, skus, idx + 1))
    tasks.sort(key=lambda x: x[0])
    return tasks


def simulate_check_and_track_usage(inbound_rows: List[Tuple[pd.Timestamp, List[str], dict]],
                                   outbound_tasks: List[Tuple[pd.Timestamp, List[str], int]],
                                   prefer_latest: bool = False) -> List[Tuple[int, List[int], pd.Timestamp]]:
    """
    完全模拟原始验证过程，跟踪每个出库任务实际使用的入库记录
    prefer_latest=False 时，为每个 SKU 选择"最早但不晚于创建时间"的入库记录（正向遍历出库任务）。
    返回: [(inbound_row_index, [used_beam_indices], usage_time), ...]（顺序按处理出库任务的遍历顺序）
    """
    from bisect import bisect_right
    # 构建 inbound 字典，记录每个 SKU 的所有入库时间和对应行信息
    inbound_records: Dict[str, List[Tuple[pd.Timestamp, int, int]]] = {}  # {sku: [(time, row_idx, beam_idx)]}
    
    for row_idx, (arrival_time, beams, _) in enumerate(inbound_rows):
        for beam_idx, beam in enumerate(beams):
            if beam not in inbound_records:
                inbound_records[beam] = []
            inbound_records[beam].append((arrival_time, row_idx, beam_idx))
    
    # 对每个SKU的记录按时间排序（从早到晚）
    for beam in inbound_records:
        inbound_records[beam].sort(key=lambda x: x[0])
    
    # 记录实际使用的入库记录
    actual_usage: List[Tuple[int, List[int], pd.Timestamp]] = []  # [(row_idx, [beam_indices], usage_time)]
    
    # 按顺序处理出库任务，模拟 check_consistency 的逻辑
    for ct, skus, _ in outbound_tasks:
        used_in_this_task: Dict[int, List[int]] = {}  # {row_idx: [beam_indices]}
        
        for sku in skus:
            records = inbound_records.get(sku, [])
            if not records or pd.isna(ct):
                continue

            if prefer_latest:
                # 选择不晚于 ct 的最后一条
                times = [r[0] for r in records]
                idx = bisect_right(times, ct) - 1
            else:
                # 选择不晚于 ct 的第一条（即最早但不晚于创建时间的记录）
                times = [r[0] for r in records]
                idx = bisect_right(times, ct) - 1

            # 确保索引有效并且时间不晚于出库任务创建时间
            if 0 <= idx < len(records) and records[idx][0] <= ct:
                arrival_time, row_idx, beam_idx = records[idx]
                if row_idx not in used_in_this_task:
                    used_in_this_task[row_idx] = []
                used_in_this_task[row_idx].append(beam_idx)
                # 消费掉这条记录
                records.pop(idx)
                inbound_records[sku] = records
        
        # 将本次任务的使用情况加入总使用记录
        for row_idx, beam_indices in used_in_this_task.items():
            actual_usage.append((row_idx, beam_indices, ct))
    
    return actual_usage


def fmt_ts(ts: pd.Timestamp) -> str:
    try:
        if pd.isna(ts):
            return ""
        return ts.tz_convert(pytz.timezone('Asia/Shanghai')).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)


def clean_inbound_records(inbound_rows: List[Tuple[pd.Timestamp, List[str], dict]],
                         actual_usage: List[Tuple[int, List[int], pd.Timestamp]],
                         beam_cols: List[str]) -> List[dict]:
    """
    根据实际使用情况清理入库记录
    
    Args:
        inbound_rows: 入库数据 [(arrival_time, [beams], original_row_dict)]
        actual_usage: 实际使用情况 [(inbound_row_index, [used_beam_indices], usage_time)]
        beam_cols: 纵梁列名列表
    
    Returns:
        清理后的行数据列表
    """
    # 统计每个入库行被使用的beam以及使用时间
    used_beams_per_row: Dict[int, List[int]] = {}
    usage_times_per_beam: Dict[int, Dict[int, pd.Timestamp]] = {}  # {row_idx: {beam_idx: usage_time}}
    
    for row_idx, beam_indices, usage_time in actual_usage:
        if row_idx not in used_beams_per_row:
            used_beams_per_row[row_idx] = []
            usage_times_per_beam[row_idx] = {}
            
        for beam_idx in beam_indices:
            if beam_idx not in used_beams_per_row[row_idx]:
                used_beams_per_row[row_idx].append(beam_idx)
                usage_times_per_beam[row_idx][beam_idx] = usage_time
    
    # 记录处理统计信息
    total_rows = len(inbound_rows)
    removed_rows = 0
    partial_cleaned_rows = 0
    
    # 创建一个新数据列表用于存储结果
    cleaned_rows = []
    
    for idx, (arrival_time, beams, original_row) in enumerate(inbound_rows):
        used_beam_indices = used_beams_per_row.get(idx, [])
        
        # 如果至少有一个纵梁需要保留
        if used_beam_indices:
            new_row = original_row.copy()
            # 如果只需要保留一个纵梁，则清空另一个
            if len(beams) == 2:
                if len(used_beam_indices) == 1:
                    # 只有一个纵梁被使用，清空未使用的那个
                    unused_index = 1 - used_beam_indices[0]  # 0->1, 1->0
                    new_row[beam_cols[unused_index]] = ""
                    partial_cleaned_rows += 1
                # 如果两个都被使用，保持原样
                # 如果都不被使用（理论上不可能走到这里），也保持原样
            
            # 添加使用时间列
            for i in range(len(beams)):
                if i in usage_times_per_beam.get(idx, {}):
                    usage_time = usage_times_per_beam[idx][i]
                    new_row[f"{beam_cols[i]}使用时间"] = fmt_ts(usage_time)
                else:
                    new_row[f"{beam_cols[i]}使用时间"] = ""
            
            cleaned_rows.append(new_row)
        else:
            # 所有纵梁都不需要保留，整行删除
            removed_rows += 1
    
    print(f"原始记录数: {total_rows}")
    print(f"删除整行记录数: {removed_rows}")
    print(f"部分清理记录数(只保留一个纵梁): {partial_cleaned_rows}")
    print(f"保留记录数: {len(cleaned_rows)}")
    
    return cleaned_rows


def main():
    # ===== 配置：如需调整路径/列名，修改下方常量 =====
    # 使用绝对路径或相对于当前工作目录的路径
    inbound_table = Path("simulation/data/aisle_allocation2.xlsx")
    outbound_table = Path("simulation/data/production_plan.xlsx")
    
    # 确保路径存在
    if not inbound_table.exists():
        print(f"警告: 入库表文件不存在: {inbound_table}")
        return
    
    if not outbound_table.exists():
        print(f"警告: 出库表文件不存在: {outbound_table}")
        return
        
    arrival_col = "到达时间"  # 入库记录中的到货时间列名
    beam_cols = ["纵梁A", "纵梁B"]  # 入库记录中的纵梁代码列名
    creation_col = "开始时间"  # 出库任务中的创建时间列名
    outbound_sku_cols = ["零件号（左主）", "零件号（左副）", "零件号（右主）", "零件号（右副）"]  # 出库任务中的SKU列名
    # ===============================================
    
    # 加载数据
    print("正在加载入库数据...")
    inbound_rows = load_inbound_table_with_details(inbound_table, arrival_col=arrival_col, beam_cols=beam_cols)
    print(f"加载入库记录 {len(inbound_rows)} 条")
    
    print("正在加载出库数据...")
    outbound_tasks = load_outbound_table(outbound_table, creation_col=creation_col, sku_cols=outbound_sku_cols)
    print(f"加载出库任务 {len(outbound_tasks)} 个")
    
    # 模拟验证过程并跟踪实际使用情况
    print("正在分析实际使用的纵梁（正向遍历，选用最早但不晚于创建时间的入库记录）...")
    actual_usage = simulate_check_and_track_usage(inbound_rows, outbound_tasks, prefer_latest=False)
    print(f"发现实际使用的入库记录数: {len(actual_usage)}")
    
    # 清理入库记录
    print("正在清理未使用的入库记录...")
    cleaned_rows = clean_inbound_records(inbound_rows, actual_usage, beam_cols)
    
    # 保存结果
    output_file = inbound_table.parent / f"{inbound_table.stem}_cleaned{inbound_table.suffix}"
    
    if cleaned_rows:
        df_cleaned = pd.DataFrame(cleaned_rows)
        if output_file.suffix.lower() in [".xlsx", ".xls"]:
            df_cleaned.to_excel(output_file, index=False)
        else:
            df_cleaned.to_csv(output_file, index=False)
        
        print(f"清理完成！结果已保存至: {output_file}")
    else:
        print("没有需要保留的记录")


if __name__ == "__main__":
    main()