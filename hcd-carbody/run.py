"""
仓库仿真主程序 - 事件驱动版本
此程序模拟仓库中的入库和出库操作，使用事件驱动方式处理任务调度
整个系统包括入库任务分配、出库任务处理、库存管理和移库操作等功能
"""


import random
import heapq
import pytz
import datetime
import json
import os
import sys
import atexit
from typing import Optional, Dict, List, Tuple
from simulation import WarehouseCore
from simulation.warehouse_core import load_warehouse_config
from simulation.task_data import TaskData
from simulation.event import Event, EVENT_INBOUND_UNASSIGNED, EVENT_INBOUND_ARRIVAL_AT_AISLE
from simulation.task_data import TASK_TYPE_INBOUND_UNASSIGNED, TASK_TYPE_INBOUND, TASK_TYPE_OUTBOUND



_original_stdout = sys.stdout
_original_stderr = sys.stderr


def _restore_stdio(log_file: Optional[object]):
    """恢复标准输入输出流到原始状态，关闭日志文件句柄
    
    此函数在程序退出时被调用，确保标准输出和错误输出恢复到原始状态，
    并且关闭日志文件句柄以释放资源。
    
    Args:
        log_file: 日志文件对象，如果为None则只恢复stdio
    """
    try:
        sys.stdout = _original_stdout
        sys.stderr = _original_stderr
    except Exception:
        pass
    if log_file is not None:
        try:
            log_file.flush()
        except Exception:
            pass
        try:
            log_file.close()
        except Exception:
            pass

# 输出流复制类，同时向控制台和日志文件输出内容
class _TeeStream:
    def __init__(self, *streams):
        """初始化TeeStream对象，接收多个输出流
        
        Args:
            *streams: 可变数量的输出流对象
        """
        self.streams = streams

    def flush(self):
        """刷新所有输出流的缓冲区"""
        for stream in self.streams:
            try:
                stream.flush()
            except Exception:
                pass

    def close(self):
        """关闭所有输出流"""
        for stream in self.streams:
            try:
                stream.close()
            except Exception:
                pass


    def write(self, data):
        """将数据写入所有输出流并刷新
        
        Args:
            data: 要写入的数据字符串
            
        Returns:
            写入的数据长度
        """
        for stream in self.streams:
            stream.write(data)
            stream.flush()
        return len(data)


def configure_log_output(log_path: Optional[str]) -> Optional[object]:
    """配置日志输出到指定文件，同时保持控制台输出
    
    此函数将stdout和stderr重定向到既输出到控制台又输出到日志文件的流，
    便于调试和问题追踪。
    
    Args:
        log_path: 日志文件路径，如果为None则不输出日志
        
    Returns:
        日志文件对象，如果未创建则返回None
    """
    if not log_path:
        return None
    log_dir = os.path.dirname(log_path)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
    log_file = open(log_path, "a", encoding="utf-8")
    sys.stdout = _TeeStream(sys.stdout, log_file)
    sys.stderr = _TeeStream(sys.stderr, log_file)
    atexit.register(_restore_stdio, log_file)
    print(f"[INFO] logging to {log_path}")
    return log_file



class WarehouseSimulation:
    """仓库仿真器 - 事件驱动版本，主要负责管理仿真流程和事件队列
    
    该类封装了仓库仿真的核心逻辑，包括事件队列管理、时间推进、任务处理等。
    使用事件驱动的方式来模拟仓库中的各种操作，如入库、出库、移库等。
    """
    
    def __init__(self, num_aisles: int = 5, num_production_lines: int = 3,
                 initial_inventory_ratio: float = 0.3, random_seed: Optional[int] = None,
                 use_magnetic_crane: bool = True, outbound_congestion_time: float = 10.0,
                 aisle_production_line_mapping: Optional[Dict[int, List[int]]] = None,
                 lr_balance_weight: float = 0.3,
                 inbound_aisle_strategy: Optional[str] = None,
                 inbound_allocation_strategy: Optional[str] = None,
                 inbound_aisle_allocator = None,
                 inbound_position_allocator = None,
                 inbound_rate_lambda: float = 1/85.0,  # 泊松到达率(1/秒)
                 transport_delay_s: Optional[float] = None,
                 scheduler_type: str = 'heuristic',
                 initial_inventory_count: int = 250,
                 track_skus: Optional[List[str]] = ["2801022-TG360"]):
        """
        初始化仓库仿真器参数

        Args:
            num_aisles: 巷道数量，代表仓库中的存储通道数
            num_production_lines: 产线数量，代表下游生产线的数量
            initial_inventory_ratio: 初始库存占比，决定仿真开始时的库存水平
            random_seed: 随机种子，用于保证仿真的可重现性
            use_magnetic_crane: 是否使用磁力吊（默认True），影响任务处理逻辑
            outbound_congestion_time: 出库口拥堵时间（秒），模拟出库时的等待时间
            aisle_production_line_mapping: 巷道-产线映射配置，定义哪些巷道服务于哪些产线
            lr_balance_weight: 左右均衡度权重（0-1），默认0.3，影响库存分布策略
            inbound_aisle_allocator: 入库任务的巷道分配策略（可选）
            inbound_position_allocator: 入库任务的货位分配策略（可选）
            inbound_rate_lambda: 入库任务到达的泊松分布参数（lambda值）
            transport_delay_s: 运输延迟时间（秒），影响任务执行时间
            scheduler_type: 调度器类型 ('heuristic' 或 'optimization')
            initial_inventory_count: 初始库存任务记录数量（仅当initial_inventory为None时有效）
            track_skus: 需要跟踪的SKU列表，用于监控特定产品的流动情况
        """
        # 仓库核心组件，处理具体的业务逻辑和状态管理
        self.warehouse_core = WarehouseCore(
            num_aisles=num_aisles,
            num_production_lines=num_production_lines,
            initial_inventory_ratio=initial_inventory_ratio,
            random_seed=random_seed,
            use_magnetic_crane=use_magnetic_crane,
            outbound_congestion_time=outbound_congestion_time,
            aisle_production_line_mapping=aisle_production_line_mapping,
            lr_balance_weight=lr_balance_weight,
            scheduler_type=scheduler_type,
            inbound_aisle_strategy=inbound_aisle_strategy,
            inbound_allocation_strategy=inbound_allocation_strategy,
            initial_inventory_count=initial_inventory_count,
        )
        if track_skus:
            try:
                self.warehouse_core.inventory_manager.set_sku_watchlist(track_skus)
                print(f"[INFO] 开启 SKU 跟踪: {track_skus}")
            except Exception:
                pass
        
        # 参数：入库泊松到达率与运输延迟（可覆盖Core默认）
        self.inbound_rate_lambda = inbound_rate_lambda
        if transport_delay_s is not None:
            self.warehouse_core.transport_delay_s = transport_delay_s

        # 事件驱动仿真相关（Simulation 仅做派发与推进）
        self.event_queue = []  # 优先队列（最小堆），存储待处理事件，按时间排序
        self.current_time = 0.0  # 当前仿真时间，随事件处理逐步推进
    
    def run_simulation(self, production_plan: Dict[int, List[List[str]]] = None,
                      max_simulation_time: float = 3600.0, initial_inventory: Optional[dict] = None,
                      initial_inventory_count: int = 250,
                      real_time_days: Optional[int] = None,
                      cutoff_hour: Optional[int] = None,
                      creation_times: Optional[Dict[int, List[float]]] = None):
        """事件驱动仿真 - 主仿真循环，处理入库出库任务，分天执行
        
        该方法将根据时间把任务分成多天执行，每天独立处理各自的入库出库任务，
        在每天内部按事件驱动方式进行仿真。支持真实时间数据和模拟数据两种模式。
        
        仿真流程：
        1. 将任务按时间分桶到不同的"天"
        2. 每天开始时初始化相关状态
        3. 生成当天的入库任务事件
        4. 循环处理事件直到当天结束或任务完成
        5. 汇总当天的执行结果
        6. 重复步骤2-5直到所有天完成
        
        Args:
            production_plan: 生产计划 {production_line: [['A1', 'A2', 'A3', 'A4'], ...]}
            max_simulation_time: 最大仿真时间（秒）
            initial_inventory: 初始库存字典（可选）
            initial_inventory_count: 初始库存任务记录数量
            real_time_days: 使用真实时间数据时，模拟几天（从最早记录的cutoff_hour切日开始）
            cutoff_hour: 切日小时，默认 6 点，6 点前归上一天
            creation_times: 与生产计划 group 对齐的创建时间列表
        """
        print("=" * 80)
        print("仓库仿真系统 - 事件驱动版本")
        print("=" * 80)

        # 兼容直接传入包含 production_plan/creation_times 的整体配置
        if creation_times is None and isinstance(production_plan, dict) \
                and "production_plan" in production_plan and "creation_times" in production_plan:
            creation_times = production_plan.get("creation_times", {})
            production_plan = production_plan.get("production_plan", {})
            
        # 使用明确的时区处理：时间戳视为 UTC 秒，统一转为东八区，避免本机时区影响
        china_tz = pytz.timezone('Asia/Shanghai')

        def to_local(ts: float) -> datetime.datetime:
            return datetime.datetime.fromtimestamp(ts, tz=pytz.utc).astimezone(china_tz)

        def calc_anchor(ts: float) -> datetime.datetime:
            dt = to_local(ts)
            anchor = dt.replace(hour=cutoff_hour, minute=0, second=0, microsecond=0)
            if dt.hour < cutoff_hour:
                anchor -= datetime.timedelta(days=1)
            return anchor

        def day_idx(ts: float, anchor_dt: datetime.datetime) -> int:
            dt = to_local(ts)
            return int((dt - anchor_dt).total_seconds() // (24*3600))

        # 按时间分桶入库/出库
        inbound_records_all = getattr(self.warehouse_core, "inbound_records", [])
        # 已用于初始化的入库任务不再参与日循环
        if initial_inventory is None and initial_inventory_count:
            inbound_records_all = inbound_records_all[initial_inventory_count:]
        creation_times = creation_times or {}
        # 归一化 key 为 int，避免字符串产线号导致取值失败
        creation_times = {int(k): v for k, v in creation_times.items()}
        # 同样归一化 production_plan key，后续分桶使用此版本
        production_plan = {int(k): v for k, v in (production_plan or {}).items()}
        ts_candidates = []
        for rec in inbound_records_all:
            if isinstance(rec, dict) and "arrival_time" in rec and rec["arrival_time"] is not None:
                ts_candidates.append(rec["arrival_time"])
        for _, cts in creation_times.items():
            ts_candidates += [ct for ct in cts if ct is not None]
        if cutoff_hour is None:
            inbound_by_day = {0: list(inbound_records_all)}
            merged_outbound: Dict[int, List[List[List[str]]]] = {
                pl: list(production_plan.get(pl, []))
                for pl in range(1, self.warehouse_core.num_production_lines + 1)
            }
            outbound_by_day = {0: merged_outbound}
            days_to_run = 1
            per_day_time_limit = max_simulation_time
            print("[INFO] cutoff_hour disabled; run all records as a single day")
        else:
            anchor_dt = calc_anchor(min(ts_candidates)) if ts_candidates else calc_anchor(datetime.datetime.now(china_tz).timestamp())
            anchor_ts = anchor_dt.timestamp()

            inbound_by_day = {}
            for rec in inbound_records_all:
                if isinstance(rec, dict) and "arrival_time" in rec and rec["arrival_time"] is not None:
                    d = day_idx(rec["arrival_time"], anchor_dt)
                else:
                    d = 0
                inbound_by_day.setdefault(d, []).append(rec)

            outbound_by_day = {}
            production_plan = production_plan or {}
            for line, groups in production_plan.items():
                cts = creation_times.get(line, [])
                for idx, group in enumerate(groups):
                    ct = cts[idx] if idx < len(cts) else None
                    ct_ts = ct if ct is not None else anchor_ts
                    d = day_idx(ct_ts, anchor_dt)
                    outbound_by_day.setdefault(d, {}).setdefault(line, []).append(group)

        if cutoff_hour is not None:
            max_day_idx = max(outbound_by_day.keys() | inbound_by_day.keys()) if (outbound_by_day or inbound_by_day) else 0
            days_to_run = max_day_idx + 1
            if real_time_days is not None:
                days_to_run = min(days_to_run, real_time_days)
                per_day_time_limit = 24 * 3600
            else:
                # 不按真实天数跑：合并所有天的任务到单日，直接受 max_simulation_time 控制
                days_to_run = 1
                per_day_time_limit = max_simulation_time
                merged_outbound = {pl: [] for pl in range(1, self.warehouse_core.num_production_lines + 1)}
                for d in outbound_by_day.values():
                    for pl, groups in d.items():
                        merged_outbound[pl].extend(groups)
                outbound_by_day = {0: merged_outbound}
                merged_inbound = []
                for records in inbound_by_day.values():
                    merged_inbound.extend(records)
                inbound_by_day = {0: merged_inbound}

            print(f"[INFO] 出库按天分桶概览（cutoff={cutoff_hour:02d}:00，最多展示前10天）:")
        for d in sorted(outbound_by_day.keys())[:10]:
            cnt1 = len(outbound_by_day[d].get(1, []))
            cnt2 = len(outbound_by_day[d].get(2, []))
            cnt3 = len(outbound_by_day[d].get(3, []))
            total = cnt1 + cnt2 + cnt3
            print(f"  Day{d+1}: 总组数 {total} | 产线1={cnt1}, 2={cnt2}, 3={cnt3}")
        if len(outbound_by_day) > 10:
            print(f"  ... 共 {len(outbound_by_day)} 天数据")

        # 删除第一次初始化，只在每天开始时进行初始化
        # self.warehouse_core.initialize_core(
        #     production_plan={},
        #     initial_inventory=initial_inventory,
        #     initial_inventory_count=initial_inventory_count
        # )
        
        # 初始化移库计数器和移库日志游标
        self._last_relocation_count = 0
        self._last_relocation_log_idx = 0

        report_interval = 15 * 60.0  # 15min
        total_plan_groups = {
            pl: sum(len(outbound_by_day.get(d, {}).get(pl, [])) for d in outbound_by_day)
            for pl in range(1, self.warehouse_core.num_production_lines + 1)
        }
        cumulative_completed = {pl: 0 for pl in range(1, self.warehouse_core.num_production_lines + 1)}
        daily_summaries: List[List[str]] = []

        for day in range(days_to_run):
            print(f"\n=== 第{day+1}天 ===")
            day_plan = outbound_by_day.get(day, {})
            day_plan_full = {pl: day_plan.get(pl, []) for pl in range(1, self.warehouse_core.num_production_lines + 1)}
            self.warehouse_core.set_production_plan(day_plan_full)
            if day == 0:
                self.warehouse_core.initialize_core(
                    production_plan=day_plan_full,
                    initial_inventory=initial_inventory,
                    initial_inventory_count=initial_inventory_count
                )
            day_inbound_records = inbound_by_day.get(day, [])

            # 打印当日入库/出库任务概览
            print(f"[DAY {day+1}] 入库任务数: {len(day_inbound_records)}")
            for i, rec in enumerate(day_inbound_records[:5]):
                if isinstance(rec, dict):
                    skus = rec.get('skus', [])
                    at = rec.get('arrival_time', '')
                else:
                    skus = rec
                    at = ''
                print(f"  入库#{i+1}: {skus} arrival_time={at}")
            if len(day_inbound_records) > 5:
                print(f"  ... 共 {len(day_inbound_records)} 条")

            print(f"[DAY {day+1}] 出库计划：")
            for pl, groups in day_plan_full.items():
                print(f"  产线{pl}: {len(groups)}组")
                for gi, grp in enumerate(groups[:3]):
                    print(f"    组{gi+1}: {grp}")
                if len(groups) > 3:
                    print(f"    ... 共 {len(groups)} 组")

            # 清理运行状态（库存不变）
            self.event_queue = []
            self.current_time = 0.0
            next_report_time = report_interval
            self.warehouse_core.pending_outbound_queue = []
            self.warehouse_core.pending_inbound_by_aisle = {aisle: [] for aisle in self.warehouse_core.aisles}
            self.warehouse_core.running_tasks.clear()
            # 清理跨日的完成记录/状态，避免上一天的任务ID影响当日生成
            self.warehouse_core.completed_tasks.clear()
            self.warehouse_core.task_status.clear()

            # 初始化pairing_logs
            pairing_logs = []

            # 当天入库：用泊松分布生成到达事件（horizon=24h）
            self._seed_inbound_for_records(day_inbound_records, horizon_s=24*3600)

            schedule_events = self.warehouse_core.on_event(None, self.current_time)
            for ev in schedule_events:
                heapq.heappush(self.event_queue, ev.copy())

            day_max_time = per_day_time_limit
            # 事件循环
            print("\n开始事件驱动仿真...")
            next_report_time = self.current_time + report_interval
            outbound_completed = False  # 出库完成后不再往调度器加新任务，但要跑完队列中已有任务
            while self.current_time < day_max_time:
                next_reloc_time = getattr(self.warehouse_core, "_get_next_relocation_op_time", lambda: None)()
                if next_reloc_time is not None:
                    next_event_time = self.event_queue[0].time if self.event_queue else None
                    if next_event_time is None or next_reloc_time < next_event_time:
                        self.current_time = max(self.current_time, next_reloc_time)
                        self.warehouse_core._apply_relocation_ops(self.current_time)
                        continue
                # 若事件队列为空但仍有未完成的当日任务，尝试再触发一次调度
                if not self.event_queue:
                    if not outbound_completed:
                        pending_exists = any(
                            self.warehouse_core.production_line_current_group.get(pl, 0) < len(groups)
                            for pl, groups in day_plan_full.items()
                        )
                        if pending_exists:
                            print(f"[DEBUG] 事件队列为空，但仍有未完成任务，尝试触发调度")
                            for pl, groups in day_plan_full.items():
                                current_group = self.warehouse_core.production_line_current_group.get(pl, 0)
                                print(f"[DEBUG] 产线{pl}: 当前组 {current_group}/{len(groups)}")
                            wait = max(self.warehouse_core.relocation_delay_s, 0.0)
                            refill_events = self.warehouse_core.on_event(None, self.current_time + 2 * wait)
                            print(f"[DEBUG] 调度生成了 {len(refill_events)} 个新事件")
                            for ev in refill_events:
                                heapq.heappush(self.event_queue, ev.copy())
                if not self.event_queue:
                    # 出库完成后尝试从 Core 内部队列补一下剩余入库事件，避免遗漏
                    if outbound_completed and getattr(self.warehouse_core, "event_queue", None):
                        inbound_left = [
                            ev for ev in self.warehouse_core.event_queue
                            if ev.event_type in (EVENT_INBOUND_UNASSIGNED, EVENT_INBOUND_ARRIVAL_AT_AISLE)
                        ]
                        for ev in inbound_left:
                            heapq.heappush(self.event_queue, ev.copy())
                        # 清掉已转移的入库事件，防止重复
                        self.warehouse_core.event_queue = [
                            ev for ev in self.warehouse_core.event_queue
                            if ev.event_type not in (EVENT_INBOUND_UNASSIGNED, EVENT_INBOUND_ARRIVAL_AT_AISLE)
                        ]

                    if not self.event_queue:
                        # 出库完成后只是不再加新任务，队列清空即可结束
                        if outbound_completed:
                            print("[DEBUG] 事件队列已清空，出库已完成，结束当天循环")
                        else:
                            # 仍然没有事件可处理，跳出
                            print("[DEBUG] 仍然没有事件可处理，跳出循环")
                        break

                # 取出下一个事件
                event = heapq.heappop(self.event_queue)
                self.current_time = event.time

                print(f"\n[时间 {self.current_time:.2f}s] 处理事件: {event}")

                # NOTE: 输出event_queue，前n个，按照时间升序，然后按event_id升序
                n=10
                event_queue_sorted = sorted(self.event_queue, key=lambda e: (e.time, e.event_id))[:10]
                if event_queue_sorted:
                    print("[run]未来即将到来的事件(前10，按时间升序):")
                    for ev in event_queue_sorted:
                        print(f"  {ev}")
                
                # 将事件通知Core，让Core更新并返回新事件
                core_events = self.warehouse_core.on_event(event, self.current_time)
                if outbound_completed:
                    core_events = [
                        ev for ev in core_events
                        if getattr(getattr(ev, "task", None), "task_type", None) != TASK_TYPE_OUTBOUND
                    ]
                print("[run]新添事件:")
                for ev in core_events:
                    print(f"  {ev}")
                    heapq.heappush(self.event_queue, ev.copy())

                # 定期间隔输出（已移除配对率统计，仅推进报告时间）
                if self.current_time >= next_report_time:
                    next_report_time += report_interval

                day_out_done = True
                for pl, groups in day_plan_full.items():
                    if self.warehouse_core.production_line_current_group.get(pl, 0) < len(groups):
                        day_out_done = False
                        break
                if day_out_done and not outbound_completed:
                    print(f"[DAY {day+1}] 当天出库完成，进入收尾阶段：不再向调度器添加新任务，继续消化队列中的事件")
                    outbound_completed = True
                    continue

            # 出库收尾后，若仍有未消化的入库相关事件或待处理入库队列，强制补处理一次，避免遗漏
            if outbound_completed:
                inbound_left = any(
                    ev.event_type in (EVENT_INBOUND_UNASSIGNED, EVENT_INBOUND_ARRIVAL_AT_AISLE)
                    for ev in self.event_queue
                )
                inbound_pending = any(self.warehouse_core.pending_inbound_by_aisle.get(a) for a in self.warehouse_core.pending_inbound_by_aisle)
                if inbound_left or inbound_pending:
                    print(f"[DAY {day+1}] 出库收尾后仍有入库事件/待处理入库，执行一次补处理")
                    self._flush_remaining_inbound(current_time=self.current_time)

            if outbound_completed:
                core_inbound_events = []
                if getattr(self.warehouse_core, "event_queue", None):
                    core_inbound_events = [
                        ev for ev in self.warehouse_core.event_queue
                        if ev.event_type in (EVENT_INBOUND_UNASSIGNED, EVENT_INBOUND_ARRIVAL_AT_AISLE)
                    ]
                    self.warehouse_core.event_queue = [
                        ev for ev in self.warehouse_core.event_queue
                        if ev.event_type not in (EVENT_INBOUND_UNASSIGNED, EVENT_INBOUND_ARRIVAL_AT_AISLE)
                    ]
                for ev in core_inbound_events:
                    heapq.heappush(self.event_queue, ev.copy())

                for aisle, pending in (self.warehouse_core.pending_inbound_by_aisle or {}).items():
                    try:
                        while pending:
                            task = pending.pop(0)
                            event_id = f"{EVENT_INBOUND_ARRIVAL_AT_AISLE}_{task.task_id}"
                            heapq.heappush(
                                self.event_queue,
                                Event(self.current_time, event_id, EVENT_INBOUND_ARRIVAL_AT_AISLE, task)
                            )
                    except Exception:
                        continue

                if self.event_queue:
                    print(f"[DAY {day+1}] 出库收尾后再次发现入库残留事件，兜底补处理")
                    self._flush_remaining_inbound(current_time=self.current_time)

            # 若事件耗尽但仍有未完成的产线，输出警告
            final_day_out_done = True
            for pl, groups in day_plan_full.items():
                current_group = self.warehouse_core.production_line_current_group.get(pl, 0)
                if current_group < len(groups):
                    final_day_out_done = False
                    print(f"[WARN][DAY {day+1}] 产线{pl} 当日计划未完成：已完成 {current_group}/{len(groups)} 组，事件已耗尽，可能缺库存或无可派任务")
                    print(f"[DEBUG] 产线{pl} 详细信息：计划组数={len(groups)}, 当前组索引={current_group}")
                    self._debug_outbound_blockers(pl, groups)
                else:
                    print(f"[INFO][DAY {day+1}] 产线{pl} 当日计划已完成：{len(groups)}/{len(groups)} 组")
            print(f"[DAY {day+1}] 日内结束，当前时间 {self.current_time/3600:.2f} 小时")
            # 记录当日完成的组数
            day_completed = {
                pl: self.warehouse_core.production_line_current_group.get(pl, 0)
                for pl in range(1, self.warehouse_core.num_production_lines + 1)
            }
            for pl, cnt in day_completed.items():
                cumulative_completed[pl] += cnt
            # 当日汇总
            lines = self._print_daily_summary(
                day_idx=day+1,
                day_completed=day_completed,
                cumulative_completed=cumulative_completed,
                total_plan_groups=total_plan_groups,
                day_time=self.current_time,
                pairing_logs=pairing_logs,
                return_lines=True,
            )
            for ln in lines:
                print(ln)
            daily_summaries.append(lines)

        # 当天循环结束

        # 输出最终结果（循环外，汇总全部天）
        if daily_summaries:
            print("\n" + "=" * 80)
            print("全部天汇总")
            print("=" * 80)
            for lines in daily_summaries:
                for ln in lines:
                    print(ln)
                print("-" * 60)
    
    def _print_daily_summary(self, day_idx: int, day_completed=None, cumulative_completed=None, total_plan_groups=None,
                             day_time=None, pairing_logs=None, return_lines: bool = False):
        """输出当日完成情况（参考总结果格式）
        
        Args:
            day_idx: 当前天的索引
            day_completed: 当天完成的任务数
            cumulative_completed: 累计完成的任务数
            total_plan_groups: 总计划组数
            day_time: 当天结束时的时间
            pairing_logs: 配对率记录
            return_lines: 是否返回行列表而不打印
            
        Returns:
            如果return_lines为True，则返回行列表
        """
        lines: List[str] = []
        lines.append("\n" + "-" * 60)
        lines.append(f"第 {day_idx} 天汇总")
        lines.append("-" * 60)
        t = day_time if day_time is not None else self.current_time
        lines.append(f"仿真时长: {t/60:.2f}分钟")

        lines.append(f"任务完成情况 (含起止/耗时，如有记录):")
        in_count, out_count = 0, 0
        for task in self.warehouse_core.completed_tasks:
            record = getattr(task, "task_record", {}) or {}
            start_t = record.get("start_time")
            end_t = record.get("delivery_time")
            duration = None
            if isinstance(start_t, (int, float)) and isinstance(end_t, (int, float)):
                duration = end_t - start_t
            span = ""
            if start_t is not None or end_t is not None:
                s = f"{start_t:.1f}s" if isinstance(start_t, (int, float)) else "?"
                e = f"{end_t:.1f}s" if isinstance(end_t, (int, float)) else "?"
                span = f"，起止 {s}~{e}"
                if duration is not None:
                    span += f"，耗时 {duration:.1f}s"
            if task.task_type == TASK_TYPE_INBOUND:
                in_count += 1
                aisle_info = f" (巷道 {task.assigned_aisle})" if getattr(task, 'assigned_aisle', None) else ""
                lines.append(f"第{in_count}个入库任务 {task.task_id} 完成{aisle_info}{span}")
            if task.task_type == TASK_TYPE_OUTBOUND:
                out_count += 1
                aisle_info = f" (巷道 {task.assigned_aisle})" if getattr(task, 'assigned_aisle', None) else ""
                lines.append(f"第{out_count}个出库任务 {task.task_id} 完成{aisle_info}{span}")

        lines.append(f"生产计划完成情况:")
        if total_plan_groups is None or cumulative_completed is None:
            for pl in range(1, self.warehouse_core.num_production_lines + 1):
                total_groups = len(self.warehouse_core.production_plan[pl])
                current_group_idx = self.warehouse_core.production_line_current_group[pl]
                completed_groups = current_group_idx
                if current_group_idx < total_groups:
                    lines.append(f"  产线{pl}: 已完成{completed_groups} 组，当前在第 {current_group_idx+1} 组，共 {total_groups} 组")
                else:
                    lines.append(f"  产线{pl}: 已完成全部 {total_groups} 组")
        else:
            for pl in sorted(total_plan_groups.keys()):
                tot = total_plan_groups.get(pl, 0)
                comp = cumulative_completed.get(pl, 0)
                today = (day_completed or {}).get(pl, 0)
                lines.append(f"  产线{pl}: 今日完成 {today} 组，累计 {comp}/{tot}")

        # 显示每日新增的移库数量而不是累计值
        daily_relocations = self.warehouse_core._relocation_count - getattr(self, '_last_relocation_count', 0)
        lines.append(f"移库数量: {daily_relocations} (新增)")
        # 移库占用明细（当日新增）
        new_logs = []
        if hasattr(self.warehouse_core, "relocation_log_messages"):
            msgs = self.warehouse_core.relocation_log_messages
            if self._last_relocation_log_idx < len(msgs):
                new_logs = msgs[self._last_relocation_log_idx:]
                self._last_relocation_log_idx = len(msgs)
        if new_logs:
            lines.append("移库占用记录:")
            lines.extend(new_logs)
        self._last_relocation_count = self.warehouse_core._relocation_count
        
        if pairing_logs:
            lines.append("配对率记录(15min间隔):")
            lines.extend(pairing_logs)

        if return_lines:
            return lines
        for ln in lines:
            print(ln)

    def _normalize_skus(self, sku_raw):
        """标准化SKU数据格式，确保每项都有正确的结构和数量信息
        
        此方法将不同格式的SKU数据转换为统一的格式，便于后续处理。
        支持字典和简单列表两种输入格式。
        
        Args:
            sku_raw: 原始SKU数据，可能是列表或字典格式
            
        Returns:
            标准化的SKU列表，每个元素都包含skuId、quantity和side等必要字段
        """
        skus = []
        for idx_slot, sku in enumerate(sku_raw or []):
            side = 'A' if idx_slot == 0 else 'B'
            if isinstance(sku, dict):
                sku_dict = dict(sku)
                sku_dict.setdefault('quantity', 1)
                sku_dict.setdefault('side', side)
                if 'skuId' not in sku_dict:
                    sku_dict['skuId'] = sku_dict.get('rfid') or sku_dict.get('RFID')
                skus.append(sku_dict)
            else:
                skus.append({'skuId': sku, 'quantity': 1, 'side': side})
        return skus
    def _seed_inbound_poisson(self, lambda_rate: float, horizon_s: float):
        """使用泊松过程生成入库事件，模拟真实的到达时间间隔
        
        Args:
            lambda_rate: 泊松分布的λ参数，表示单位时间内的平均到达率
            horizon_s: 时间范围（秒），在此范围内生成事件
        """
        t = 0.0
        idx_inbound_unassigned = 0

        # 跳过前initial_inventory_count条用于初始化库存的数据
        start_index = self.warehouse_core.initial_inventory_count
        if start_index >= len(self.warehouse_core.inbound_records):
            print(f"警告: initial_inventory_count ({start_index}) 大于或等于总入库记录数 ({len(self.warehouse_core.inbound_records)})，将不生成额外的入库任务")
            return

        for record in self.warehouse_core.inbound_records[start_index:]:
            dt = random.expovariate(lambda_rate) if lambda_rate > 0 else horizon_s
            t += dt
            if t >= horizon_s:
                break
            # 兼容带 arrival_time 的字典记录或原始列表记录，保留槽位信息(side)
            sku_raw = record.get('skus', []) if isinstance(record, dict) else record
            in_line = record.get('in_line', 1) if isinstance(record, dict) else 1
            production_line = record.get('production_line') if isinstance(record, dict) else None
            skus = self._normalize_skus(sku_raw)

            task = TaskData(
                task_id=f"IN_UNASSIGNED_{idx_inbound_unassigned:05d}",
                task_type=TASK_TYPE_INBOUND_UNASSIGNED,
                task_name=f"IN_UNASSIGNED_{idx_inbound_unassigned:05d}",
                skus=skus,
                in_line=in_line,
                production_line=production_line,
            )
            event_id = EVENT_INBOUND_UNASSIGNED + '_' + str(idx_inbound_unassigned)
            heapq.heappush(self.event_queue, Event(t, event_id, EVENT_INBOUND_UNASSIGNED, task))
            idx_inbound_unassigned += 1

    def _seed_inbound_from_records(self, horizon_s: float, real_time_days: Optional[int] = None,
                                   cutoff_hour: Optional[int] = 6) -> Tuple[bool, Optional[float], Optional[float]]:
        """若入库记录包含到达时间，则按真实到达时间生成事件。

        Args:
            horizon_s: 最大仿真时间
            real_time_days: 限制使用真实时间的天数（从最早记录的cutoff_hour切日开始）
            cutoff_hour: 切日小时
        Returns:
            (used_real, anchor_ts, max_used_ts)
        """
        records = getattr(self.warehouse_core, "inbound_records", [])
        if not records:
            return False, None, None
        start_index = getattr(self.warehouse_core, "initial_inventory_count", 0)
        records = records[start_index:]

        def parse_record(rec):
            # 支持 dict {'arrival_time': t, 'skus': [...], 'in_line': x, 'production_line': y } 或 (t, [...])
            if isinstance(rec, dict) and "arrival_time" in rec:
                return rec["arrival_time"], rec.get("skus", []), rec.get("in_line", 1), rec.get("production_line")
            if isinstance(rec, (list, tuple)) and len(rec) == 2 and isinstance(rec[0], (int, float)):
                return rec[0], rec[1], 1, None
            return None, None, 1, None

        tasks = []
        arrival_list = []
        for i, rec in enumerate(records):
            idx = start_index + i
            arrive, skus_raw, in_line, production_line = parse_record(rec)
            if arrive is None:
                break
            arrival_list.append((idx, arrive, skus_raw, in_line, production_line))

        if not arrival_list:
            return False, None, None

        # 确定day0起点（cutoff_hour前算前一天）
        import datetime
        # 使用明确的时区处理
        china_tz = pytz.timezone('Asia/Shanghai')
        first_ts = min(a for _, a, _, _, _ in arrival_list)
        # 使用带时区的时间处理，避免UTC和本地时间混淆
        first_dt = datetime.datetime.fromtimestamp(first_ts, tz=china_tz)
        anchor = first_dt.replace(hour=cutoff_hour, minute=0, second=0, microsecond=0)
        if first_dt.hour < cutoff_hour:
            anchor -= datetime.timedelta(days=1)
        anchor_ts = anchor.timestamp()

        def day_idx(ts: float) -> int:
            return int((ts - anchor_ts) // (24 * 3600))

        if real_time_days is not None:
            arrival_list = [item for item in arrival_list if day_idx(item[1]) < real_time_days]

        max_used_ts = None
        for idx, arrive_abs, skus_raw, in_line, production_line in arrival_list:
            arrive = arrive_abs - anchor_ts  # 以 day0 起点为0
            if horizon_s is not None and arrive >= horizon_s:
                continue
            max_used_ts = arrive_abs if (max_used_ts is None or arrive_abs > max_used_ts) else max_used_ts
            skus = self._normalize_skus(sku_raw)
            task = TaskData(
                task_id=f"IN_UNASSIGNED_{idx:05d}",
                task_type=TASK_TYPE_INBOUND_UNASSIGNED,
                task_name=f"IN_UNASSIGNED_{idx:05d}",
                skus=skus,
                in_line=in_line,
                production_line=production_line,
            )
            event_id = EVENT_INBOUND_UNASSIGNED + '_' + str(idx)
            tasks.append(Event(arrive, event_id, EVENT_INBOUND_UNASSIGNED, task))

        if not tasks:
            return False, anchor_ts, max_used_ts
        for ev in tasks:
            heapq.heappush(self.event_queue, ev)
        print(f"[INFO] 使用真实到达时间生成入库事件 {len(tasks)} 个")
        return True, anchor_ts, max_used_ts

    def _seed_inbound_for_records(self, records: List, horizon_s: float = 24*3600):
        """基于给定的入库记录列表，按泊松间隔生成到达事件（当天起点为0）。
        
        Args:
            records: 入库记录列表
            horizon_s: 时间范围（秒），在此范围内生成事件
        """
        t = 0.0
        idx_inbound_unassigned = 0
        for rec in records:
            if t >= horizon_s:
                break
            dt = random.expovariate(self.inbound_rate_lambda) if self.inbound_rate_lambda > 0 else horizon_s
            t += dt
            if t >= horizon_s:
                break
            # 兼容 dict 或 [skuA, skuB]，保留槽位(side)
            if isinstance(rec, dict):
                sku_raw = rec.get('skus', [])
                in_line = rec.get('in_line', 1)
                production_line = rec.get('production_line')
            else:
                sku_raw = rec
                in_line = 1
                production_line = None
            skus = self._normalize_skus(sku_raw)
            task = TaskData(
                task_id=f"IN_UNASSIGNED_{idx_inbound_unassigned:05d}",
                task_type=TASK_TYPE_INBOUND_UNASSIGNED,
                task_name=f"IN_UNASSIGNED_{idx_inbound_unassigned:05d}",
                skus=skus,
                in_line=in_line,
                production_line=production_line,
            )
            event_id = EVENT_INBOUND_UNASSIGNED + '_' + str(idx_inbound_unassigned)
            heapq.heappush(self.event_queue, Event(t, event_id, EVENT_INBOUND_UNASSIGNED, task))
            idx_inbound_unassigned += 1

    def _flush_remaining_inbound(self, current_time: float):
        """将队列中尚未到达的入库事件立即处理完（用于当日出库完成后加速结束当天）。
        
        此方法主要用于仿真收尾阶段，当出库任务完成后，将剩余的入库任务快速处理完毕，
        避免仿真因等待入库任务时间到达而停滞。
        
        Args:
            current_time: 当前仿真时间
        """
        # 将剩余事件统一拉平到 current_time 并处理到底（主要是入库到达→入库完成）
        while self.event_queue:
            ev = heapq.heappop(self.event_queue)
            ev.time = current_time
            new_events = self.warehouse_core.on_event(ev, current_time)
            for ne in new_events:
                heapq.heappush(self.event_queue, ne.copy())

    def _debug_outbound_blockers(self, production_line: int, day_groups: List):
        """当出库无法继续时，输出剩余任务的缺货情况
        
        此方法用于调试目的，当出库任务因为库存不足而无法完成时，
        分析具体缺少哪些SKU，帮助定位问题。
        
        Args:
            production_line: 产线编号
            day_groups: 当天该产线的出库任务组列表
        """
        current_idx = self.warehouse_core.production_line_current_group.get(production_line, 0)
        if current_idx >= len(day_groups):
            print(f"[DEBUG] 产线{production_line} 当前组索引({current_idx})已超过计划组数({len(day_groups)})")
            return
            
        print(f"[DEBUG] 产线{production_line} 当前组索引: {current_idx}，计划总组数: {len(day_groups)}")
        remaining = day_groups[current_idx:]
        snap = self.warehouse_core.inventory_manager.get_inventory_snapshot()
        dist = snap.get("sku_distribution", {})
        print(f"[DEBUG] 产线{production_line} 剩余 {len(remaining)} 组，库存分布中部分 SKU 数量：")
        for gi, group in enumerate(remaining, start=current_idx + 1):
            print(f"  组{gi}:")
            match_mode = self.warehouse_core._get_outbound_match_mode(production_line)
            feature_keys = self.warehouse_core._get_outbound_match_features(production_line)
            inv = self.warehouse_core.inventory_manager

            def _count_feature_qty(target_features: dict) -> int:
                if not target_features:
                    return 0
                total = 0
                for pos in inv.inventory_positions:
                    if pos.is_double_layer:
                        if pos.upper_quantity > 0 and inv._features_match(pos.upper_features, target_features, feature_keys):
                            total += pos.upper_quantity
                        if pos.lower_quantity > 0 and inv._features_match(pos.lower_features, target_features, feature_keys):
                            total += pos.lower_quantity
                    else:
                        if pos.quantity > 0 and inv._features_match(pos.features, target_features, feature_keys):
                            total += pos.quantity
                return total

            for ti, task_skus in enumerate(group):
                if match_mode == "features" and feature_keys:
                    need = {}
                    for sku_entry in task_skus:
                        feats = sku_entry.get('features') if isinstance(sku_entry, dict) else None
                        key = None
                        if feats:
                            key = tuple((k, feats.get(k)) for k in feature_keys)
                        need[key] = need.get(key, 0) + 1
                    lacks = {}
                    for key, req in need.items():
                        if key is None:
                            lacks[str(key)] = req
                            continue
                        features = {k: v for k, v in key}
                        total_qty = _count_feature_qty(features)
                        if total_qty < req:
                            label = self.warehouse_core._task_sku_label({'features': features})
                            lacks[label] = req - total_qty
                    if lacks:
                        print(f"    task {ti+1}: need {need}, lacks {lacks}")
                    else:
                        print(f"    task {ti+1}: need {need}, inventory ok")
                else:
                    need = {}
                    for sku_entry in task_skus:
                        if isinstance(sku_entry, dict):
                            sku = sku_entry.get('skuId') or sku_entry.get('rfid') or sku_entry.get('RFID')
                        else:
                            sku = sku_entry
                        need[sku] = need.get(sku, 0) + 1
                    lacks = {sku: need[sku] - dist.get(sku, 0) for sku in need if dist.get(sku, 0) < need[sku]}
                    if lacks:
                        print(f"    task {ti+1}: need {need}, lacks {lacks}")
                    else:
                        print(f"    task {ti+1}: need {need}, inventory ok (dist { {k: dist.get(k,0) for k in need} })")
    def _print_final_analysis(self, cumulative_completed=None, total_plan_groups=None):
        """输出最终分析
        
        此方法在仿真结束后提供完整的性能指标和统计数据，
        包括任务完成情况、生产计划执行情况、移库数量和库存均衡度等。
        
        Args:
            cumulative_completed: 累计完成任务数
            total_plan_groups: 总计划组数
        """
        print("\n" + "=" * 80)
        print("仿真结束 - 最终统计")
        print("=" * 80)
        
        print(f"\n仿真时长: {self.current_time/60:.2f}分钟")
        
        # 入库任务完成情况
        print(f"\n任务完成情况:")
        in_count, out_count = 0, 0
        for task in self.warehouse_core.completed_tasks:
            if task.task_type == TASK_TYPE_INBOUND:
                in_count += 1
                aisle_info = f" (巷道 {task.assigned_aisle})" if getattr(task, "assigned_aisle", None) else ""
                rec = getattr(task, "task_record", {}) or {}
                st, et = rec.get('start_time'), rec.get('delivery_time')
                dur = rec.get('duration')
                time_info = ""
                if st is not None and et is not None:
                    time_info = f"，起止 {st:.1f}s~{et:.1f}s，耗时 {et - st:.1f}s"
                elif dur is not None and st is not None:
                    time_info = f"，开始 {st:.1f}s，耗时 {dur:.1f}s"
                print(f"第{in_count}个入库任务 {task.task_id} 完成{aisle_info}{time_info}")
            if task.task_type == TASK_TYPE_OUTBOUND:
                out_count += 1
                aisle_info = f" (巷道 {task.assigned_aisle})" if getattr(task, "assigned_aisle", None) else ""
                rec = getattr(task, "task_record", {}) or {}
                st, et = rec.get('start_time'), rec.get('delivery_time')
                dur = rec.get('duration')
                time_info = ""
                if st is not None and et is not None:
                    time_info = f"，起止 {st:.1f}s~{et:.1f}s，耗时 {et - st:.1f}s"
                elif dur is not None and st is not None:
                    time_info = f"，开始 {st:.1f}s，耗时 {dur:.1f}s"
                print(f"第{out_count}个出库任务 {task.task_id} 完成{aisle_info}{time_info}")

        # 生产计划完成情况
        print(f"\n生产计划完成情况:")
        if cumulative_completed is None or total_plan_groups is None:
            for pl in range(1, self.warehouse_core.num_production_lines + 1):
                total_groups = len(self.warehouse_core.production_plan[pl])
                current_group_idx = self.warehouse_core.production_line_current_group[pl]
                completed_groups = current_group_idx
                if current_group_idx < total_groups:
                    print(f"  产线{pl}: 已完成 {completed_groups} 组，当前在第 {current_group_idx+1} 组，共 {total_groups} 组")
                else:
                    print(f"  产线{pl}: 已完成全部 {total_groups} 组")
        else:
            for pl in sorted(total_plan_groups.keys()):
                tot = total_plan_groups.get(pl, 0)
                comp = cumulative_completed.get(pl, 0)
                if comp < tot:
                    print(f"  产线{pl}: 已完成 {comp} 组，剩余 {tot - comp} 组，共 {tot} 组")
                else:
                    print(f"  产线{pl}: 已完成全部 {tot} 组")
        print(f"\n移库数量: {self.warehouse_core._relocation_count}")
        # 库存均衡度
        final_balance = self.warehouse_core.get_current_balance()
        print(f"\n最终库存均衡度: {final_balance:.3f}")


def main(random_seed: Optional[int] = 42, max_simulation_time: float = 3600.0, 
         use_magnetic_crane: Optional[bool] = None,
         outbound_congestion_time: Optional[float] = None, lr_balance_weight: Optional[float] = None,
         inbound_allocation_strategy: str = "baseline",
         inbound_position_strategy: str = 'baseline',
         scheduler_type: str = 'heuristic',
         makespan_weight: Optional[float] = None,
         balance_weight: Optional[float] = None,
         production_line_avg_time_weight: Optional[float] = None,
         production_line_balance_weight: Optional[float] = None,
         aisle_dispersion_weight: Optional[float] = None,
         inbound_wait_weight: Optional[float] = None,
         initial_inventory_count: Optional[int] = None,
         inbound_rate_lambda: float = 1/85.0,
         real_time_days: Optional[int] = None,
         cutoff_hour: int = 6,
         date_str: Optional[str] = None,
         inbound_config_path: Optional[str] = None,
         outbound_config_path: Optional[str] = None,
         plan_config_path: Optional[str] = None):
    """仓库仿真主函数（事件驱动版本）
    
    该函数是整个仓库仿真的入口点，负责加载配置、初始化仿真器、执行仿真流程。
    参数优先级：命令行参数 > config/warehouse.json > 代码默认值
    
    仿真系统支持多种策略配置，包括入库巷道分配策略、入库货位分配策略、
    调度策略等，可以根据需要选择不同的策略组合进行对比实验。
    
    Args:
        random_seed: 随机种子，None表示不设置
        max_simulation_time: 最大仿真时间（秒）
        use_magnetic_crane: 是否使用磁力吊（优先级：函数参数 > config/warehouse.json > 默认值True）
        outbound_congestion_time: 出库口拥堵时间（秒，优先级：函数参数 > config/warehouse.json > 默认值0.0）
        lr_balance_weight: 左右均衡度权重（0-1，优先级：函数参数 > config/warehouse.json > 默认值0.3）
        inbound_allocation_strategy: 入库巷道分配策略 ('proposed', 'baseline_random', 'baseline_round_robin', 'baseline_most_empty', None)
        inbound_position_strategy: 入库货位分配策略 ('proposed', 'first_available', 'lowest_level', 'nearest')
        scheduler_type: 调度器类型 ('heuristic' 或 'optimization')
        initial_inventory_count: 用于初始化库存的入库任务记录数（优先级：函数参数 > config/warehouse.json > 默认值200）
        cutoff_hour: 切日小时
        date_str: 日期字符串（如20251012），用于指定运行特定日期的仿真
        inbound_config_path: 入库配置文件路径，用于指定特定日期的配置
        outbound_config_path: 出库配置文件路径，用于指定特定日期的配置
        plan_config_path: 兼容旧参数，等同 outbound_config_path
    """
    # 入库策略交由 Core 内部配置
    
    cfg = load_warehouse_config("config/warehouse.json")
    def _resolve_param(value, key, fallback):
        """解析参数值，按优先级顺序：函数参数 > 配置文件 > 默认值
        
        这个辅助函数实现了参数的三级优先级解析机制，确保系统可以灵活地从
        多个来源获取配置参数，同时保持一致的行为。
        
        Args:
            value: 函数参数传入的值
            key: 配置文件中的键名
            fallback: 默认值
            
        Returns:
            实际使用的参数值
        """
        if value is not None:
            return value
        if key in cfg:
            return cfg[key]
        return fallback

    use_magnetic_crane = _resolve_param(use_magnetic_crane, "use_magnetic_crane", True)
    outbound_congestion_time = _resolve_param(outbound_congestion_time, "outbound_congestion_time", 0.0)
    lr_balance_weight = _resolve_param(lr_balance_weight, "lr_balance_weight", 0.3)
    makespan_weight = _resolve_param(makespan_weight, "makespan_weight", 0.3)
    balance_weight = _resolve_param(balance_weight, "balance_weight", 0.001)
    production_line_avg_time_weight = _resolve_param(production_line_avg_time_weight, "production_line_avg_time_weight", 0.5)
    production_line_balance_weight = _resolve_param(production_line_balance_weight, "production_line_balance_weight", 0.3)
    aisle_dispersion_weight = _resolve_param(aisle_dispersion_weight, "aisle_dispersion_weight", 0.3)
    inbound_wait_weight = _resolve_param(inbound_wait_weight, "inbound_wait_weight", 0.01)

    # 创建仿真器
    simulator = WarehouseSimulation(
        num_aisles=5,
        num_production_lines=3,
        initial_inventory_ratio=0,
        random_seed=random_seed,
        use_magnetic_crane=use_magnetic_crane,
        outbound_congestion_time=outbound_congestion_time,
        lr_balance_weight=lr_balance_weight,
        scheduler_type=scheduler_type,
        inbound_aisle_strategy=inbound_allocation_strategy,
        inbound_allocation_strategy=inbound_position_strategy,
        initial_inventory_count=initial_inventory_count or 250,
        inbound_rate_lambda=inbound_rate_lambda,
    )
    if initial_inventory_count is None:
        initial_inventory_count = simulator.warehouse_core.initial_inventory_count
    else:
        simulator.warehouse_core.initial_inventory_count = initial_inventory_count
    simulator.warehouse_core.makespan_weight = makespan_weight
    simulator.warehouse_core.balance_weight = balance_weight
    simulator.warehouse_core.production_line_avg_time_weight = production_line_avg_time_weight
    simulator.warehouse_core.production_line_balance_weight = production_line_balance_weight
    simulator.warehouse_core.aisle_dispersion_weight = aisle_dispersion_weight
    simulator.warehouse_core.inbound_wait_weight = inbound_wait_weight

    # 根据是否指定了特定日期配置来设置生产计划
    if outbound_config_path is None and plan_config_path:
        outbound_config_path = plan_config_path

    if inbound_config_path and outbound_config_path:
        if date_str:
            print(f"正在运行日期 {date_str} 的仿真..")
        else:
            print("正在运行自定义配置仿真..")
        print(f"使用入库配置: {inbound_config_path}")
        print(f"使用出库配置: {outbound_config_path}")

        with open(inbound_config_path, 'r', encoding='utf-8') as f:
            inbound_config = json.load(f)
        with open(outbound_config_path, 'r', encoding='utf-8') as f:
            outbound_config = json.load(f)

        # 设置入库记录
        simulator.warehouse_core.inbound_records = inbound_config.get('inbound_records', [])
        # 设置生产计划
        production_plan = outbound_config.get('production_plan', {})
        try:
            plan_counts = {str(k): len(v or []) for k, v in (production_plan or {}).items()}
            print(f"[DEBUG] outbound production_plan counts: {plan_counts}")
        except Exception:
            print("[DEBUG] outbound production_plan counts: <unavailable>")
        simulator.warehouse_core.set_production_plan(production_plan)

        # 获取创建时间
        creation_times = outbound_config.get('creation_times', {})

        # 自动计算初始库存数量：第一个出库任务开始前的入库数量
        first_outbound_time = None
        try:
            all_creation_times = []
            for times in (creation_times or {}).values():
                all_creation_times.extend([t for t in (times or []) if t is not None])
            if all_creation_times:
                first_outbound_time = min(all_creation_times)
        except Exception:
            first_outbound_time = None

        if first_outbound_time is not None:
            auto_initial_count = 0
            for rec in inbound_config.get('inbound_records', []) or []:
                if isinstance(rec, dict):
                    at = rec.get('arrival_time')
                    if at is not None and at < first_outbound_time:
                        auto_initial_count += 1
            initial_inventory_count = auto_initial_count
            simulator.warehouse_core.initial_inventory_count = auto_initial_count
            print(
                f"[INFO] initial_inventory_count auto={auto_initial_count} "
                f"(first_outbound_time={first_outbound_time})"
            )

    else:
        # 使用默认配置
        print("正在运行默认仿真...")
        with open("simulation/data/outbound_task_config.json", "r", encoding="utf-8") as f:
            outbound_config = json.load(f)
        production_plan = outbound_config.get("production_plan", {})
        creation_times = outbound_config.get("creation_times", {})
        try:
            plan_counts = {str(k): len(v or []) for k, v in (production_plan or {}).items()}
            print(f"[DEBUG] outbound production_plan counts: {plan_counts}")
        except Exception:
            print("[DEBUG] outbound production_plan counts: <unavailable>")

    # 运行仿真
    simulator.run_simulation(
        production_plan=production_plan,
        max_simulation_time=max_simulation_time,
        initial_inventory_count=initial_inventory_count,
        real_time_days=real_time_days,
        cutoff_hour=cutoff_hour,
        creation_times=creation_times,
    )


if __name__ == "__main__":
    # 使用说明：
    # 运行前请先激活conda环境: conda activate scip_env
    
    # 参数说明：
    # - use_magnetic_crane: 是否使用磁力吊（True/False）
    # - outbound_congestion_time: 出库口拥堵时间（秒）
    # - lr_balance_weight: 左右均衡度权重（0-1），不使用磁力吊时建议设为0
    # - scheduler_type: 调度器类型
    #   * 'heuristic': 启发式调度器（快速，适合大规模）
    #   * 'optimization': 随机优化调度器（较慢，质量更好，支持综合评分）
    # - num_iterations: 随机优化调度器的迭代次数（仅当scheduler_type='optimization'时有效）
    # - makespan_weight: makespan权重（仅当scheduler_type='optimization'时有效）
    # - balance_weight: 均衡度变化权重（仅当scheduler_type='optimization'时有效）
    # - production_line_avg_time_weight: 产线平均完成时间权重（仅当scheduler_type='optimization'时有效）
    # - inbound_allocation_strategy: 入库巷道分配策略
    #   * 'proposed': 使用提出策略
    #   * 'baseline': 使用基线策略
    # - inbound_position_strategy: 入库货位分配策略
    #   * 'proposed': 使用提出策略（综合考虑时间成本和层高）
    #   * 'baseline': 使用基线策略
    # - initial_inventory_count: 用于初始化库存的入库任务记录数
    # - cutoff_hour: 切日小时
    # - real_time_days: 使用真实时间数据时，模拟几天（从最早记录的cutoff_hour切日开始）
    # - date_str: 日期字符串（如20251012），用于指定运行特定日期的仿真
    # - inbound_config_path: 入库配置文件路径，用于指定特定日期的配置
    # - outbound_config_path: 出库配置文件路径，用于指定特定日期的配置
    # - plan_config_path: 兼容旧参数，等同 outbound_config_path

    
    # 未在json中定义的参数可以在下面修改，例如仿真天数，入库速率等。
    import argparse
    
    parser = argparse.ArgumentParser(description='仓库仿真系统')
    parser.add_argument('--random-seed', type=int, default=42, help='随机种子')
    parser.add_argument('--max-simulation-time', type=float, default=86400.0, help='最大仿真时间（秒）')
    parser.add_argument('--use-magnetic-crane', action='store_true', help='是否使用磁力吊')
    parser.add_argument('--no-magnetic-crane', dest='use_magnetic_crane', action='store_false', help='不使用磁力吊')
    parser.set_defaults(use_magnetic_crane=None)
    parser.add_argument('--outbound-congestion-time', type=float, default=None, help='出库口拥堵时间（秒，默认用 config/warehouse.json）')
    parser.add_argument('--lr-balance-weight', type=float, default=None, help='左右均衡度权重（默认用 config/warehouse.json）')
    parser.add_argument('--inbound-allocation-strategy', type=str, default='baseline', help='入库巷道分配策略, baseline or proposed')
    parser.add_argument('--inbound-position-strategy', type=str, default='baseline', help='入库货位分配策略, baseline or proposed')
    parser.add_argument('--scheduler-type', type=str, default='heuristic', help='调度器类型, optimization or heuristic') 
    parser.add_argument('--makespan-weight', type=float, default=None, help='makespan weight (optimization)')
    parser.add_argument('--balance-weight', type=float, default=None, help='inventory balance change weight (optimization)')
    parser.add_argument('--production-line-avg-time-weight', type=float, default=None, help='production line avg time weight (optimization)')
    parser.add_argument('--production-line-balance-weight', type=float, default=None, help='production line balance weight (optimization)')
    parser.add_argument('--aisle-dispersion-weight', type=float, default=None, help='aisle dispersion weight (optimization)')
    parser.add_argument('--inbound-wait-weight', type=float, default=None, help='inbound wait time weight (optimization)')
    parser.add_argument('--initial-inventory-count', type=int, default=None, help='用于初始化库存的入库任务记录数')
    parser.add_argument('--inbound-rate-lambda', type=float, default=1/60.0, help='(入库计划生成时)入库任务生成间隔的λ参数')
    parser.add_argument('--real-time-days', type=int, default=15, help='使用真实时间数据时，模拟几天')
    parser.add_argument('--cutoff-hour', type=int, default=4, help='切日小时')
    parser.add_argument('--no-cutoff', action='store_true', help='不分日，整段时间作为单日运行')
    parser.add_argument('--date-str', type=str, help='日期字符串（如20251012），用于指定运行特定日期的仿真')
    parser.add_argument('--inbound-config', type=str, help='入库配置文件路径')
    parser.add_argument('--outbound-config', type=str, help='出库配置文件路径')
    parser.add_argument('--plan-config', type=str, help='兼容旧参数，等同 --outbound-config')
    parser.add_argument('--log-file', type=str, default='logs/run.log', help='log file path (tee stdout/stderr)')
    
    args = parser.parse_args()
    configure_log_output(args.log_file)
    
    main(
        random_seed=args.random_seed, 
        max_simulation_time=args.max_simulation_time, # 最大仿真时间
        use_magnetic_crane=args.use_magnetic_crane,  # 是否使用磁力吊
        outbound_congestion_time=args.outbound_congestion_time,  # 出库口拥堵时间（秒）
        lr_balance_weight=args.lr_balance_weight,  # 左右均衡度权重
        inbound_allocation_strategy=args.inbound_allocation_strategy,  # 入库巷道分配策略
        inbound_position_strategy=args.inbound_position_strategy,  # 入库货位分配策略
        scheduler_type=args.scheduler_type,  # 调度器类型
        makespan_weight=args.makespan_weight,
        balance_weight=args.balance_weight,
        production_line_avg_time_weight=args.production_line_avg_time_weight,
        production_line_balance_weight=args.production_line_balance_weight,
        aisle_dispersion_weight=args.aisle_dispersion_weight,
        inbound_wait_weight=args.inbound_wait_weight,
        initial_inventory_count=args.initial_inventory_count,  # 用于初始化库存的入库任务记录数
        inbound_rate_lambda=args.inbound_rate_lambda,  # (入库计划生成时)入库任务生成间隔的λ参数
        real_time_days=args.real_time_days, #设置为None时可以使用max_simulation_time
        cutoff_hour=None if args.no_cutoff else args.cutoff_hour,
        date_str=args.date_str,
        inbound_config_path=args.inbound_config,
        outbound_config_path=args.outbound_config,
        plan_config_path=args.plan_config
    )
