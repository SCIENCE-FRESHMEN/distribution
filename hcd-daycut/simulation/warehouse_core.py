"""
仓库仿真核心模块
整合库存管理、任务生成、时间计算等功能
"""

import random
import heapq
import json
from pathlib import Path
from copy import deepcopy
from collections.abc import Mapping
from typing import Dict, List, Tuple, Optional, Any
from config_loader import load_jsonc, resolve_runtime_path
from .task_data import TaskData
from .inventory import InventoryManager
from .metrics import MetricsCalculator
from estimate.time_estimator import TimeEstimator
from .position import InventoryPosition
from .event import (
    Event,
    EVENT_INBOUND_UNASSIGNED,
    EVENT_INBOUND_ARRIVAL_AT_AISLE,
    EVENT_TASK_COMPLETE,
    EVENT_CONGESTION_CLEAR,
    EVENT_CRANE_AVAILABLE,
)
from .task_data import (
    TaskData,
    AisleScheduleRecord,
    TASK_TYPE_INBOUND,
    TASK_TYPE_OUTBOUND,
    TASK_TYPE_INBOUND_UNASSIGNED,
)
from simulation.config_bulider.sku_config_builder import SKUConfigBuilder
from simulation.config_bulider.inbound_task_config_builder import InboundConfigBuilder

from schedule import get_scheduler


def _get_task_field(task_info: Any, field_name: str, default: Any = None) -> Any:
    """兼容读取 API 映射任务和仿真任务对象的字段。

    Args:
        task_info: API 请求传入的映射对象，或核心内部使用的 ``TaskData``/临时任务对象。
        field_name: 要读取的字段名。
        default: 任务为空或字段不存在时使用的默认值。

    Returns:
        字段值或 ``default``。``Mapping`` 分支使用键访问，避免对字典进行属性访问。
    """
    if isinstance(task_info, Mapping):
        return task_info.get(field_name, default)
    return getattr(task_info, field_name, default)


def load_warehouse_config(path: Optional[str]) -> dict:
    """Load warehouse initialization config from json if present.

    Args:
        path (Optional[str]): 需要读取、写入或检查的文件路径。

    Returns:
        dict: 按函数约定字段组织的计算结果映射。
    """
    if not path:
        return {}
    # 部署包优先读取 exe 同级可编辑配置；旧包未外置时回退到 _internal/config。
    p = resolve_runtime_path(path)
    if not p.exists():
        return {}
    try:
        return load_jsonc(p)
    except Exception:
        return {}


class WarehouseCore:
    """仓库仿真核心类"""

    # 类变量，用于存储入库记录，确保只加载一次
    _inbound_records = None
    _inbound_records_loaded = False

    def __init__(self, num_aisles: int = 5, num_production_lines: int = 3,
                 num_rows: int = 2, num_columns: int = 3, num_levels: int = 18,
                 total_positions: int = 1000, max_beams: int = 980,
                 initial_inventory_ratio: float = 0.3, random_seed: Optional[int] = None,
                 use_double_layer: bool = True,
                 aisle_production_line_mapping: Optional[Dict[int, List[int]]] = None,
                 scheduler_type: str = 'heuristic',
                 inbound_aisle_strategy: Optional[str] = None,
                 inbound_allocation_strategy: Optional[str] = None,
                 initial_inventory_count: int = 250,
                 transport_delay_s: Optional[float] = 30.0,
                 blockage_time: float = 15.0,
                 magnetic_crane_time: float = 40.0,
                 relocation_delay_s: float = 80.0,
                 config_path: Optional[str] = "config/warehouse.json",
                 sku_config_path: Optional[str] = None):
        """Args:
            num_aisles: 巷道数量
            num_production_lines: 产线数量
            num_rows, num_columns, num_levels: 仓位行/列/层
            total_positions: 仓位总数
            max_beams: 最大梁数
            initial_inventory_ratio: 初始库存占比
            random_seed: 随机种子
            use_double_layer: 是否启用双层货位
            aisle_production_line_mapping: 巷道-产线映射配置 {aisle: [可服务的产线列表]}，None表示所有巷道可服务所有产线
            initial_inventory_count: 初始库存总量（默认提取250条入库记录）
            transport_delay_s: 入库运输延迟（秒）
            blockage_time: 拥堵时间（秒）
            magnetic_crane_time: 磁力吊工作时间（秒）
            relocation_delay_s: 移库延迟（秒）
            config_path: 仓库几何/拥堵/运输等配置 JSON 路径
            sku_config_path: SKU/BOM 配置路径覆盖值。``None`` 时读取 ``warehouse.json``
                的 ``sku_config_path``，仍缺失时使用仿真默认文件。

        Returns:
            None: 通过实例状态、队列或外部副作用完成处理。
        """
        # 配置文件来源；保留路径以便快照/诊断时区分 API 配置和仿真配置。
        self.config_path = config_path
        # cfg 是仓库几何、设备参数和评分权重的唯一配置字典。
        cfg = load_warehouse_config(config_path)

        # 仓库几何与货位容量。InventoryManager 用这些值创建 position_map 中的全部货位。
        self.num_aisles = int(cfg.get("num_aisles", num_aisles))
        self.num_rows = int(cfg.get("num_rows", num_rows))
        self.num_columns = int(cfg.get("num_columns", num_columns))
        self.num_levels = int(cfg.get("num_levels", num_levels))
        # 纵梁库的双排、双层结构固定。货位总数由几何参数推导，最大梁数为每个物理
        # 货位的上下两层容量，避免在配置中重复维护三个可能彼此矛盾的结构参数。
        self.use_double_layer = True
        self.total_positions = self.num_aisles * self.num_rows * self.num_columns * self.num_levels
        self.max_beams = self.total_positions * 2
        # 货位列号的确定性择优模式：1=最小列，2=中间列，3=最大列。仅在其他
        # 可行性与路径成本相同或已完成比较后参与并列消解，不替代出入库口距离计算。
        raw_column_preference_mode = int(cfg.get("position_column_preference_mode", 3))
        self.position_column_preference_mode = (
            raw_column_preference_mode if raw_column_preference_mode in (1, 2, 3) else 3
        )
        # SKU 配对/出库校验必须一致的属性字段；空列表表示只按 skuId 匹配。
        self.match_fields = list(cfg.get("match_fields", []))

        # 生产计划与初始库存规模；aisles 是后续所有队列和统计字典的统一键集合。
        self.num_production_lines = int(cfg.get("num_production_lines", num_production_lines))
        self.initial_inventory_ratio = float(cfg.get("initial_inventory_ratio", initial_inventory_ratio))
        self.initial_inventory_count = int(cfg.get("initial_inventory_count", initial_inventory_count))
        self.aisles = list(range(1, self.num_aisles + 1))

        # 仅影响随机生成任务、候选采样等随机路径；固定后相同输入可重复仿真。
        random_seed = cfg.get("random_seed", random_seed)
        if random_seed is not None:
            random.seed(random_seed)
            print(f"设置随机种子: {random_seed}")

        # 设备、出库规则和优化评分参数统一由 warehouse.json 管理。API 与仿真均由
        # 此处读取同一份配置；cfg.get 的第二个参数仅用于兼容旧配置缺少字段时启动，
        # 不是命令行或构造参数覆盖入口。
        self.use_magnetic_crane = bool(cfg.get("use_magnetic_crane", True))
        self.outbound_congestion_time = float(cfg.get("outbound_congestion_time", 0.0))
        self.lr_balance_weight = float(cfg.get("lr_balance_weight", 0.0))

        # 优化器评分权重。仅在 scheduler_type 为 optimization 时参与候选方案评分；
        # 每个权重的业务含义和推荐取值在 warehouse.json 的同名字段注释中维护。
        self.makespan_weight = float(cfg.get("makespan_weight", 0.0))
        self.balance_weight = float(cfg.get("balance_weight", 0.0))
        self.production_line_avg_time_weight = float(cfg.get("production_line_avg_time_weight", 0.0))
        self.production_line_balance_weight = float(cfg.get("production_line_balance_weight", 0.0))
        self.aisle_dispersion_weight = float(cfg.get("aisle_dispersion_weight", 0.0))
        self.inbound_wait_weight = float(cfg.get("inbound_wait_weight", 0.0))
        self.outbound_choice_bonus_weight = float(cfg.get("outbound_choice_bonus_weight", 0.0))

        # FIFO 开关控制同 SKU 且属性匹配的多个候选货位是否按入库时间排序。
        self.outbound_fifo_enabled = bool(cfg.get("outbound_fifo_enabled", False))

        # 左右库按巷道序号中点划分，供左右库存均衡指标和左右梁分布策略共同使用。
        mid_point = len(self.aisles) // 2
        self.left_aisles = self.aisles[:mid_point]
        self.right_aisles = self.aisles[mid_point:]

        # 巷道-产线映射配置
        cfg_mapping = cfg.get("aisle_production_line_mapping", None)
        if cfg_mapping is not None:
            try:
                aisle_production_line_mapping = {int(k): [int(vv) for vv in v] for k, v in cfg_mapping.items()}
            except Exception:
                aisle_production_line_mapping = cfg_mapping
        if aisle_production_line_mapping is None:
            # 默认：所有巷道可服务所有产线
            self.aisle_production_line_mapping = {
                aisle: list(range(1, self.num_production_lines + 1))
                for aisle in self.aisles
            }
        else:
            self.aisle_production_line_mapping = aisle_production_line_mapping

        # API 通过显式覆盖使用 ``config/sku_config.json``；命令行仿真未传覆盖值时
        # 继续读取原有 ``simulation/data/sku_config.json``，两者的 BOM 不互相覆盖。
        self.sku_config_path = str(resolve_runtime_path(
            sku_config_path or cfg.get("sku_config_path", "simulation/data/sku_config.json")
        ))
        self.config_data = SKUConfigBuilder.load_json(self.sku_config_path)

        # SKU 类型表用于初始库存生成和统计；SKU 到产线表用于未显式传产线时的回退推断。
        self.sku_types = self.config_data["sku_types"]
        self.sku_to_production_line = self.config_data["sku_to_production_line"]

        # {skuA: skuB} 方向性配对关系；双梁直出要求两根梁满足此关系及 match_fields。
        self.sku_pairs = self.config_data["sku_pairs"]

        # 不参与 BOM 配对的 SKU 集合；分配器按单梁规则选择一侧货位。
        self.sku_solo = self.config_data["sku_solo"]

        # 加载入库任务配置，只在首次实例化时加载
        if not WarehouseCore._inbound_records_loaded:
            try:
                inbound_config_path = resolve_runtime_path(
                    cfg.get("inbound_config_path", "simulation/data/inbound_task_config.json")
                )
                inbound_config = InboundConfigBuilder.load_json(str(inbound_config_path))
                WarehouseCore._inbound_records = inbound_config.inbound_records
                WarehouseCore._inbound_records_loaded = True
                print("已加载入库任务配置文件")
            except FileNotFoundError:
                print("警告: 未找到入库任务配置文件，将使用默认的随机生成方式")
                WarehouseCore._inbound_records = []
                WarehouseCore._inbound_records_loaded = True
            except Exception as e:
                print(f"警告: 加载入库任务配置时出错: {e}，将使用默认的随机生成方式")
                WarehouseCore._inbound_records = []
                WarehouseCore._inbound_records_loaded = True

        self.inbound_records = WarehouseCore._inbound_records

        # 禁用货位 ID 列表；InventoryManager 建位时保留位置但禁止入库分配和库存写入。
        disabled_positions = list(cfg.get("disabled_positions", []))

        # 库存模块是货位、SKU 索引和库存数量的权威来源；所有真实入/出库在此同步索引。
        self.inventory_manager = InventoryManager(
            num_aisles=self.num_aisles,
            num_rows=self.num_rows,
            num_columns=self.num_columns,
            num_levels=self.num_levels,
            total_positions=self.total_positions,
            max_beams=self.max_beams,
            sku_types=self.sku_types,
            sku_pairs=self.sku_pairs,
            sku_solo=self.sku_solo,
            initial_inventory_ratio=self.initial_inventory_ratio,
            use_double_layer=self.use_double_layer,
            disabled_positions=disabled_positions,
            match_fields=self.match_fields,
        )
        self.metrics_calculator = MetricsCalculator(
            self.aisles, self.sku_types,
            left_aisles=self.left_aisles,
            right_aisles=self.right_aisles,
            lr_balance_weight=self.lr_balance_weight
        )
        # 时间估算器在本系统中以配置参数计算路径/任务时间；此处不加载外部训练模型。
        estimator_config_path = resolve_runtime_path(
            cfg.get("estimator_config_path", "config/time_estimator.json")
        )
        self.time_estimator = TimeEstimator(load_model=False, config_path=str(estimator_config_path))

        # 出库拥堵持续时间（秒）；任务完成后可继续保持巷道-产线不可调度至该时刻。
        self.blockage_time = float(cfg.get("blockage_time", blockage_time))
        # 磁力吊占用时间（秒）；启用磁力吊时与出库完成时间共同约束设备可用时刻。
        self.magnetic_crane_time = float(cfg.get("magnetic_crane_time", magnetic_crane_time))
        # 入库从任务生成到到达目标巷道入口的运输延迟（秒）。
        self.transport_delay_s = float(cfg.get("transport_delay_s", transport_delay_s if transport_delay_s is not None else 30.0))

        # 已执行调度轮次数，用于仿真报告和策略调用频次统计。
        self.total_rounds = 0
        # 按任务类型递增的内部任务 ID 计数器，避免自动生成任务与外部任务重名。
        self.task_id_counter = {TASK_TYPE_INBOUND: 0, TASK_TYPE_OUTBOUND: 0}
        # 已规划移库操作数；每个移库计划只在创建时递增一次，供指标统计使用。
        self._relocation_count = 0

        # 生产计划：{产线号: [生产组, ...]}。生产组是“可同时推进的一组需求”，其中每项
        # 是一条出库任务的 SKU 列表，通常为 [单梁 SKU] 或 [双梁 SKU1, 双梁 SKU2]：
        # {1: [
        #     [["A1", "A2"], ["B1"]],  # 第 1 组：一条双梁任务和一条单梁任务
        #     [["C1", "C2"]],           # 第 2 组：一条双梁任务
        # ]}。
        # API 的 plans[].planIndex[].requiredSkus 会在 WarehouseService 中转换为该内部格式。
        self.production_plan = {pl: [] for pl in range(1, self.num_production_lines + 1)}
        # 与 production_plan 同索引的属性矩阵：{字段名: {产线: [组 -> [SKU 属性]]}}。
        self.production_plan_attrs: Dict[str, Dict[int, List]] = {}
        # 每条产线当前可推进的零基组号；仅当前组的出库任务可成为可执行候选。
        self.production_line_current_group = {pl: 0 for pl in range(1, self.num_production_lines + 1)}
        # 当前组已完成任务 ID；集合满足整组要求时 mark_outbound_completed 推进组号。
        self.production_line_completed_tasks = {pl: set() for pl in range(1, self.num_production_lines + 1)}
        # 每条产线各组完成时刻，用于生产线平均时间和进度均衡统计。
        self.production_line_group_completion_times = {pl: [] for pl in range(1, self.num_production_lines + 1)}

        # 堵塞状态管理：{(aisle, production_line): {'blocked': bool, 'unblock_time': float}}
        self.blockage_status = {}
        for aisle in self.aisles:
            for pl in range(1, num_production_lines + 1):
                self.blockage_status[(aisle, pl)] = {'blocked': False, 'unblock_time': 0.0}

        # 入库巷道分配器；set_inbound_strategies 创建后供 allocate_inbound_aisle 调用。
        self.inbound_aisle_allocator = None
        # 入库货位分配器；返回与任务 SKU 顺序对应的落位位置列表。
        self.inbound_position_allocator = None

        # 根据传入的策略字符串进行初始化配置
        # 说明：参数 inbound_aisle_strategy 表示入库巷道分配策略；
        # 参数 inbound_allocation_strategy 表示入库货位分配策略
        if inbound_aisle_strategy or inbound_allocation_strategy:
            # 兼容已有的内部配置函数签名
            self.set_inbound_strategies(
                inbound_allocation_strategy=inbound_aisle_strategy,
                inbound_position_strategy=inbound_allocation_strategy,
            )

        # 调度器读取本 Core 的库存、队列和设备状态；策略变更后仍复用该状态入口。
        self.scheduler_type = cfg.get("scheduler_type", scheduler_type)
        scheduler_class = get_scheduler(self.scheduler_type)
        self.scheduler = scheduler_class(self)
        # 透传货位分配器（稍后策略变更时会再次同步）
        self.scheduler.position_allocator = self.inbound_position_allocator

        # 运行态由 Core 统一维护。四个集合的职责不可互换：
        # - pending_inbound_by_aisle：已到入库口、尚未启动的 FIFO/入口队列；
        # - pending_outbound_queue：已生成、尚未启动的生产组出库任务；
        # - running_tasks：已占用巷道/设备、等待完成或反馈的任务；
        # - relocation_reserved_positions：移库规划锁定的位置 -> 任务 ID，防止
        #   后续分配把同一位置再次用作入库或移库目标。
        self.running_tasks: Dict[str, TaskData] = {}
        # 每个巷道最近完成/运行任务的终点货位；时间估算以它作为下一任务路径起点。
        self.current_position_by_aisle: Dict[int, Optional[InventoryPosition]] = {
            aisle: None for aisle in self.aisles
        }
        # 已完成任务历史；生产组推进、指标计算和 API 查询均从此读取。
        self.completed_tasks: List[TaskData] = []
        self.pending_inbound_by_aisle: Dict[int, List[TaskData]] = {aisle: [] for aisle in self.aisles}
        # 按需生成的出库任务池。
        self.pending_outbound_queue: List[TaskData] = []
        # {(产线, 巷道, 左右侧): 可再次使用的仿真时刻}，磁力吊出库据此串行化。
        self.crane_available_times: Dict[tuple, float] = {}
        # 事件驱动仿真相关
        # 优先队列（最小堆）。
        self.event_queue = []
        # 当前仿真时间。
        self.current_time = 0.0
        # 全局任务状态映射；key=task_id，value 为 pending/assigned/processing/completed 等内部状态。
        self.task_status = {}
        # 反馈控制：如果需要真实反馈再标记完成，则阻止估计提前完成
        self.require_feedback_completion = False
        # 已收到外部完成反馈的任务 ID；仅 require_feedback_completion 为真时参与完成放行。
        self.feedback_received = set()
        # 已到预计完成时刻、但仍等待外部反馈的任务 ID，防止重复派发或重复完成。
        self.awaiting_feedback = set()
        # 每巷道移库占用区间 [(start, end, task_id), ...]，调度前用于排除该时间段。
        self.relocation_busy_intervals = {aisle: [] for aisle in self.aisles}
        # 单次移库操作的预估持续时间（秒）。
        self.relocation_delay_s = float(cfg.get("relocation_delay_s", relocation_delay_s))
        # {task_id: ready_time}；只有到达该时刻后关联出库任务才可启动。
        self.relocation_task_ready_time = {}
        # 正在等待移库完成的出库任务 ID 集合。
        self.relocation_task_ids = set()
        # {aisle: [移库操作字典]}；到达计划时刻后由 _apply_relocation_ops 写回真实库存。
        self.relocation_ops_by_aisle = {aisle: [] for aisle in self.aisles}
        # {position_id: task_id}；移库源/目标位置的虚拟锁，避免与后续分配发生重叠。
        self.relocation_reserved_positions = {}

    # ========================================================================
    # 主流程：核心初始化与事件状态机
    # ========================================================================
    def get_position_column_preference_key(self, aisle: int, column: int) -> Tuple[float, int]:
        """返回货位列号的可排序偏好键，供各货位分配器统一使用。

        Args:
            aisle: 所在巷道号。纵梁库各巷道列数一致，保留该参数以与车身库接口一致。
            column: 候选货位的列号。

        Returns:
            tuple[float, int]: 值越小越优。模式 1 返回小列号优先键；模式 2 返回
            到中间列的距离并以较小列号消除对称并列；模式 3 返回大列号优先键。
        """
        del aisle  # 纵梁库使用统一的 num_columns，但调用方仍需明确传入所属巷道。
        normalized_column = int(column)
        if self.position_column_preference_mode == 1:
            return (float(normalized_column), normalized_column)
        if self.position_column_preference_mode == 2:
            middle_column = (self.num_columns + 1) / 2.0
            return (abs(normalized_column - middle_column), normalized_column)
        return (-float(normalized_column), -normalized_column)

    def initialize(self, populate_initial_inventory: bool = True):
        """初始化货位、指标基线和仿真运行状态。

        Args:
            populate_initial_inventory: 是否按 ``initial_inventory_ratio`` 生成仿真随机初始库存。
                API 服务传 False，只建立空货位和索引，真实库存由后续库存同步或任务反馈写入。

        Returns:
            None: 成功后 ``inventory_manager`` 已建立货位、索引和禁用位，运行队列和
            生产组状态可用于接收事件。

        """
        # API 初始化只建立仓库结构。暂时置零可复用 InventoryManager 的统一建位流程，
        # 同时确保不会把仿真随机库存带入外部系统的真实库存视图。
        original_inventory_ratio = self.inventory_manager.initial_inventory_ratio
        if not populate_initial_inventory:
            self.inventory_manager.initial_inventory_ratio = 0.0
        self.inventory_manager.initialize()
        self.inventory_manager.initial_inventory_ratio = original_inventory_ratio
        self.inventory_manager.print_distribution()

        # 初始库存均衡度，仅用于观察。
        # balance = self.metrics_calculator.calculate_distribution_balance(
        #     self.inventory_manager.current_inventory
        # )
        # print(f"  初始综合均衡度: {balance:.3f}")

        # 打印配置信息
        # print(f"\n仓库配置:")
        # print(f"  使用磁力吊: {self.use_magnetic_crane}")
        # if self.use_magnetic_crane:
        #     print(f"  磁力吊工作时间: {self.magnetic_crane_time}秒")
        # print(f"  出库口拥堵时间: {self.outbound_congestion_time}秒")
        # print(f"  左右均衡权重: {self.lr_balance_weight}")

    def on_event(self, event: Optional[Event], current_time: float,
                 simulation_mode: bool = False) -> List[Event]:
        """处理事件并返回由当前状态转换产生的后续事件。

        Args:
            event: 事件对象；传入 ``None`` 时仅尝试为当前空闲巷道派发 pending 任务。
            current_time: 当前时间
            simulation_mode: 是否在仿真模式（如 ``calculate_schedule_times``）；
                仿真预览不会调用 ``decide_for_idle_aisles`` 修改实时派发节奏。

        事件流为：未分配入库 -> 到达巷道入口 -> pending 入库队列 -> 任务启动
        -> 任务完成。完成入库时才调用 ``InventoryManager.add_inventory`` 写入真实
        库存；完成出库时才扣减真实库存。事件处理中还会释放运行态及移库预留，
        并按需要创建新的完成/派发事件。

        局部变量：``etype`` 是当前事件类型；``task`` 是关联任务；``new_events`` 是
        本次状态转换新生成且同时写入堆队列的事件；``aisle``、``production_line``、
        ``skus`` 是完成分支的任务上下文；``inventory_added_successfully`` 防止入库
        只部分落账；``block_until`` 是移库对指定出库完成事件施加的延后时间。


        Returns:
            List[Event]: 当前事件产生并已压入 ``event_queue`` 的后续事件。入库未
            分配事件通常返回一个到达入口事件；任务完成事件可返回拥堵解除或被移库
            延后的完成事件；没有后续状态转换时返回空列表。
        """
        # current_time 是本次事件的仿真开始时间，先应用截止该时刻的移库操作。
        self.current_time = current_time
        self._apply_relocation_ops(current_time)
        if not simulation_mode:
            print("[warehouse_core]处理前的event_queue:")
            event_queue_sorted = sorted(self.event_queue, key=lambda e: (e.time, e.event_id))
            for ev in event_queue_sorted:
                print(f"  {ev}")
        if not event:
            new_events: List[Event] = []
            # 完成后尝试派发（非仿真模式）
            if not simulation_mode:
                dispatched = self.decide_for_idle_aisles(current_time)
                for ev in dispatched:
                    new_events.append(ev)
                    heapq.heappush(self.event_queue, ev)
            return new_events

        # 当前事件只允许执行一次。按 event_id 删除而不是弹堆顶，是为了兼容外部反馈直接构造的完成事件和移库把同一任务完成时刻向后延迟的重排场景。
        self.event_queue = [ev for ev in self.event_queue if ev.event_id != event.event_id]

        # 决定下方状态机分支的事件常量。
        etype = event.event_type
        # 事件携带的 TaskData，队列、库存和资源更新均围绕它进行。
        task = event.task
        # 返回给调用方且已推入 event_queue 的后续事件集合。
        new_events: List[Event] = []
        # 事件类型分支
        # 入库未分配（未达巷道）
        if etype == EVENT_INBOUND_UNASSIGNED:
            # 外部到货先进入该分支；这里只生成 transport_delay 后的入口事件，
            # 尚不占用货位或巷道资源。
            ev = self.allocate_inbound_aisle(task, current_time)
            if ev:
                new_events.append(ev)
                heapq.heappush(self.event_queue, ev)
        # 任务到达巷道入口后进入 pending 队列；调度器随后从每个入库口的队首选择可启动任务。
        elif etype == EVENT_INBOUND_ARRIVAL_AT_AISLE:
            # 到达后进入按巷道保存的 pending 队列。调度器随后从每个入库口的
            # 队首选择可启动任务，避免同一入口的后续任务越过前序任务。
            aisle = task.assigned_aisle
            # 检查aisle是否为元组或其他非整数类型，如果是则提取整数部分
            if isinstance(aisle, tuple):
                print(f"警告: 任务 {task.task_id} 的 assigned_aisle 是元组 {aisle}，尝试提取整数部分")
                # 尝试获取元组的第一个元素作为巷道号
                aisle = aisle[0] if len(aisle) > 0 else random.choice(self.aisles)
            elif not isinstance(aisle, int):
                print(f"警告: 任务 {task.task_id} 的 assigned_aisle 类型不正确: {type(aisle)}，值为: {aisle}")
                aisle = int(aisle) if aisle is not None else random.choice(self.aisles)

            # 最后的安全检查，确保aisle是有效的整数
            if aisle not in self.pending_inbound_by_aisle:
                print(f"警告: 任务 {task.task_id} 的 assigned_aisle {aisle} 不在有效巷道列表中，使用随机巷道")
                aisle = random.choice(self.aisles)

            self.pending_inbound_by_aisle[aisle].append(task)
            # 任务到达后尝试为空闲巷道派发（非仿真模式）
            if not simulation_mode:
                dispatched = self.decide_for_idle_aisles(current_time)
                for ev in dispatched:
                    new_events.append(ev)
                    heapq.heappush(self.event_queue, ev)
        # 出入库任务完成事件
        elif etype == EVENT_TASK_COMPLETE:
            # 真实状态提交点：从 running 移除后，入库写入库存、出库扣减库存；
            # 预览/派发阶段只计算 positions 和资源，不应提前改变真实库存。
            # 用于 running、预留、反馈和事件 ID 的统一关联键。
            task_id = task.task_id
            # 完成事件只能由已分配巷道的任务触发；以 0 作为无效哨兵值保留原有
            # 容错路径，同时避免将 Optional 巷道写入状态字典。
            # 实际占用的巷道。
            aisle = int(task.assigned_aisle or 0)
            # 用于磁力吊与生产组状态的资源维度。
            production_line = task.production_line
            # 真实入库/出库时需要逐条写入或扣减的 SKU 需求。
            skus = task.skus
            if task.task_type == TASK_TYPE_OUTBOUND and production_line == 1:
                # 移库尚在执行时不能让关联出库先扣库存。这里重建同一个 COMPLETE事件而不改 running_tasks，使任务在移库结束前仍持续占用巷道资源。
                block_until = self._get_relocation_active_until(current_time)
                if block_until is not None and block_until > current_time:
                    try:
                        rec = task.task_record or {}
                        old_delivery = rec.get('delivery_time')
                        if old_delivery is not None:
                            delta = block_until - old_delivery
                            if delta > 0:
                                rec['delivery_time'] = block_until
                                if 'un_congested_time' in rec:
                                    rec['un_congested_time'] = rec['un_congested_time'] + delta
                                if 'crane_finish_time' in rec:
                                    rec['crane_finish_time'] = rec['crane_finish_time'] + delta
                                task.task_record = rec
                    except Exception:
                        pass
                    ev_id = f"{EVENT_TASK_COMPLETE}_{task_id}"
                    ev = Event(block_until, ev_id, EVENT_TASK_COMPLETE, task)
                    new_events.append(ev)
                    heapq.heappush(self.event_queue, ev)
                    return new_events
            # 如果需要真实反馈且尚未收到，则等待反馈，不提前完成
            if getattr(self, "require_feedback_completion", False) and task_id not in getattr(self, "feedback_received", set()):
                # API 反馈模式下，时间估算到点并不等同于物理完成；任务继续保留在
                # running_tasks，并通过 awaiting_feedback 等待外部 COMPLETED 反馈。
                self.awaiting_feedback.add(task_id)
                return new_events
            # 完成或收到允许完成的反馈后才释放巷道的运行占用。
            if task_id in self.running_tasks:
                del self.running_tasks[task_id]
            # 入库任务完成
            if task.task_type == TASK_TYPE_INBOUND:
                # 入库 positions 在分配/调整阶段已冻结；此处按同一规则解析上下层，
                # 再调用 InventoryManager 写入权威库存和索引。
                if getattr(task, 'positions', None):
                    sku_entries = [s for s in skus if isinstance(s, dict)]
                    sku_ids = [s.get('skuId') for s in sku_entries if s.get('skuId') is not None]
                    inventory_added_successfully = False
                    try:
                        non_null_idx = 0
                        # positions 已在分配或调整阶段冻结。完成事件只按既定映射
                        # 落账，不能在此重新选择空位，否则会与预占状态脱节。
                        for sku_entry in sku_entries:
                            sku_id = sku_entry.get('skuId')
                            if sku_id is None:
                                continue
                            sku_attrs = {k: sku_entry.get(k) for k in self.match_fields} if self.match_fields else {}
                            sku_attrs["_inbound_time"] = current_time
                            # 天然配对双梁可复用一个双层货位；因此第二个 SKU 复用
                            # 最后一个位置，具体上下层由下方的层位解析规则决定。
                            pos = task.positions[min(non_null_idx, len(task.positions)-1)]
                            try:
                                # 如果是双层货位且任务有多个SKU，将SKU分配到不同层
                                layer = None
                                if pos.is_double_layer and len(sku_ids) > 1:
                                    # 根据分配算法返回的位置决定放置在哪一层
                                    if len(task.positions) == 2:
                                        # 双梁情况：有两个位置
                                        if task.positions[0] == task.positions[1]:
                                            # 同一个位置：根据行号决定上下层
                                            # row=1: sku1放上层，sku2放下层
                                            # row=2: sku1放下层，sku2放上层
                                            if pos.row == 1:
                                                layer = 'upper' if non_null_idx == 0 else 'lower'
                                            elif pos.row == 2:
                                                layer = 'lower' if non_null_idx == 0 else 'upper'
                                        else:
                                            # 不同位置：根据配对逻辑决定层
                                            # 如果是配对货位（已有配对SKU），放在下层
                                            # 如果是空货位，放在上层
                                            if ((pos.upper_sku is not None and pos.upper_sku != '') and
                                                (pos.lower_sku is None or pos.lower_sku == '')):
                                                # 上层已有SKU，下层为空，放在下层
                                                layer = 'lower'
                                            elif ((pos.upper_sku is None or pos.upper_sku == '') and
                                                  (pos.lower_sku is None or pos.lower_sku == '')):
                                                # 上下层都为空，放在上层
                                                layer = 'upper'
                                            else:
                                                # 下层有梁/上下层都有梁
                                                print(f"[warehouse_core]警告: 位置 {pos.get_position_id()} 下层有梁/上下层都有梁")
                                                break
                                    else:
                                        # 默认情况：第一个SKU放上层，第二个SKU放下层
                                        layer = 'upper' if non_null_idx == 0 else 'lower'
                                elif pos.is_double_layer and len(sku_ids) == 1:
                                    # 单个SKU放入双层货位，检查哪一层是空的
                                    if pos.upper_quantity == 0:
                                        layer = 'upper'
                                    elif pos.lower_quantity == 0:
                                        layer = 'lower'
                                    else:
                                        # 两层都满了，无法放入
                                        raise ValueError(f"位置 {pos.get_position_id()} 的上下层都已有货物")

                                self.inventory_manager.add_inventory(pos, sku_id, 1, layer, attrs=sku_attrs)
                                inventory_added_successfully = True
                                non_null_idx += 1
                            except Exception as e:
                                print(f"[warehouse_core]add_inventory error:{pos} {sku_id}, 错误: {str(e)}")
                                try:
                                    actual_pos = self.inventory_manager.position_map.get(pos.get_position_id())
                                    print(f"[warehouse_core] position_map snapshot: {actual_pos}")
                                except Exception:
                                    pass
                                inventory_added_successfully = False
                                # 如果任何一个SKU添加失败，则整个任务失败
                                break

                        # 输出当前仓库中梁的详细信息
                        beam_details = self._get_beam_details()
                        if inventory_added_successfully:
                            if beam_details:
                                # 获取并移除总梁数
                                total_beams = beam_details.pop('total_beams', 0)
                                # 还有其他SKU信息
                                if beam_details:
                                    beam_info = ", ".join([f"{sku}: {qty}" for sku, qty in beam_details.items()])
                                    print(f"[INFO] 入库任务 {task_id} 完成，仓库中梁详情: 'beam_info暂不输出' (总计: {total_beams})")
                                else:
                                    print(f"[INFO] 入库任务 {task_id} 完成，仓库中梁详情: 暂无梁库存 (总计: {total_beams})")
                            else:
                                print(f"[INFO] 入库任务 {task_id} 完成，仓库中梁详情: 暂无梁库存")
                        else:
                            print(f"[ERROR] 入库任务 {task_id} 部分或全部库存添加失败")
                    except Exception as e:
                        print(f"[ERROR] 处理入库任务 {task_id} 时发生异常: {e}")
                        inventory_added_successfully = False

                    # 只有在库存成功添加后才更新巷道位置
                    if inventory_added_successfully:
                        # 更新当前巷道位置为该任务最后一个位置
                        if getattr(task, 'positions', None):
                            self.current_position_by_aisle[aisle] = task.positions[-1]
                else:
                    print(f"[WARN] 入库任务 {task_id} 没有指定货位信息")
            # 出库任务完成
            elif task.task_type == TASK_TYPE_OUTBOUND:
                # 磁力吊/拥堵处理
                if self.use_magnetic_crane:
                    side = 'left' if aisle in self.left_aisles else 'right'
                    crane_key = (production_line, side)
                    crane_start_time = max(current_time, self.crane_available_times.get(crane_key, 0.0))
                    crane_finish_time = crane_start_time + self.magnetic_crane_time + self.outbound_congestion_time
                    self.crane_available_times[crane_key] = crane_finish_time
                    # 更新堵塞状态
                    self.update_blockage_status(aisle, production_line, blocked=True, unblock_time=crane_finish_time)
                    # 回填记录
                    if getattr(task, 'task_record', None) is not None:
                        task.task_record['crane_start_time'] = crane_start_time
                        task.task_record['un_congested_time'] = crane_start_time + self.magnetic_crane_time
                        task.task_record['crane_finish_time'] = crane_finish_time
                    ev_task = TaskData(task_id=task_id, task_type=TASK_TYPE_OUTBOUND, task_name=task_id, skus=skus, production_line=production_line, assigned_aisle=aisle)
                    ev_id = f"{EVENT_CONGESTION_CLEAR}_{task_id}"
                    ev_obj = Event(crane_finish_time, ev_id, EVENT_CONGESTION_CLEAR, ev_task)
                    new_events.append(ev_obj)
                    heapq.heappush(self.event_queue, ev_obj)
                else:
                    # 仅拥堵时间
                    outbound_finish_time = current_time + self.outbound_congestion_time
                    if self.outbound_congestion_time > 0:
                        self.update_blockage_status(aisle, production_line, blocked=True, unblock_time=outbound_finish_time)
                        # 回填记录
                        if getattr(task, 'task_record', None) is not None:
                            task.task_record['un_congested_time'] = outbound_finish_time
                            task.task_record['crane_finish_time'] = outbound_finish_time
                        ev_task = TaskData(task_id=task_id, task_type=TASK_TYPE_OUTBOUND, task_name=task_id, skus=skus, production_line=production_line, assigned_aisle=aisle)
                        ev_id = f"{EVENT_CONGESTION_CLEAR}_{task_id}"
                        ev_obj = Event(outbound_finish_time, ev_id, EVENT_CONGESTION_CLEAR, ev_task)
                        new_events.append(ev_obj)
                        heapq.heappush(self.event_queue, ev_obj)
                    else:
                        self.update_blockage_status(aisle, production_line, blocked=False, unblock_time=0.0)
                # 更新当前巷道位置（出库：最后位置）
                if getattr(task, 'positions', None):
                    last_pos = task.positions[-1]
                    # 复制一个位置用于记录当前位置（不影响库存位置对象）
                    try:
                        cp = InventoryPosition(
                            aisle=last_pos.aisle,
                            row=last_pos.row,
                            column=self.time_estimator.dock_out_col,
                            level=self.time_estimator.dock_map_out.get(production_line, 1),
                            is_double_layer=last_pos.is_double_layer,
                            sku=last_pos.sku,
                            quantity=last_pos.quantity,
                            upper_sku=last_pos.upper_sku,
                            upper_quantity=last_pos.upper_quantity,
                            lower_sku=last_pos.lower_sku,
                            lower_quantity=last_pos.lower_quantity,
                        )
                    except Exception:
                        cp = last_pos
                    self.current_position_by_aisle[aisle] = cp
            self.relocation_task_ids.discard(task_id)
            self._release_reserved_positions_for_task(task_id)
            # 记录完成
            self.completed_tasks.append(task)
            # 完成后尝试派发（非仿真模式）
            if not simulation_mode:
                dispatched = self.decide_for_idle_aisles(current_time)
                for ev in dispatched:
                    new_events.append(ev)
                    heapq.heappush(self.event_queue, ev)
        # 拥堵解除事件（磁力吊或拥堵时间结束）
        elif etype == EVENT_CONGESTION_CLEAR:
            task_id = task.task_id
            # 拥堵事件只能由已派发任务产生；0 仅作为异常数据的无效巷道哨兵，
            # 避免 Optional[int] 直接流入按巷道维护的状态字典。
            aisle = int(task.assigned_aisle or 0)
            production_line = task.production_line
            if production_line is not None:
                self.mark_outbound_completed(production_line, task, current_time)
            self.update_blockage_status(aisle, production_line, blocked=False, unblock_time=0.0)
            # 拥堵解除后尝试派发（非仿真模式）
            if not simulation_mode:
                dispatched = self.decide_for_idle_aisles(current_time)
                for ev in dispatched:
                    new_events.append(ev)
                    heapq.heappush(self.event_queue, ev)
        return new_events


    # ========================================================================
    # 阶段处理：巷道派发与可执行任务判断
    # ========================================================================
    def get_busy_aisles(self, current_time: float) -> set:
        """Return aisles currently occupied by running tasks or relocation blocks.

        Args:
            current_time (float): 当前仿真时间，单位秒。

        Returns:
            set: 符合当前处理条件的结果列表或集合。
        """
        busy_aisles = set()
        for task in (self.running_tasks or {}).values():
            aisle = getattr(task, "assigned_aisle", None)
            if aisle:
                busy_aisles.add(aisle)
        for aisle in self.aisles:
            if self._is_aisle_relocation_busy(aisle, current_time):
                busy_aisles.add(aisle)
        return busy_aisles

    def _build_scheduler_task_pools(
        self,
        running_task_ids: Optional[set] = None,
        finished_task_ids: Optional[set] = None,
    ) -> Tuple[List[TaskData], List[TaskData]]:
        """Build inbound/outbound candidate pools without mutating the live queues.

        Args:
            running_task_ids (Optional[set]): 已在运行中的任务 ID 集合，用于生成任务时去重。
            finished_task_ids (Optional[set]): 已完成任务 ID 集合，用于阻止生产计划重复生成旧任务。

        Returns:
            Tuple[List[TaskData], List[TaskData]]: 由函数摘要中所列字段组成的多值结果。
        """
        # 获取任务 ID 集合
        if running_task_ids is None:
            running_task_ids = set(self.running_tasks.keys())
        if finished_task_ids is None:
            finished_task_ids = {task.task_id for task in self.completed_tasks}

        # 生成出库任务候选池，限制每条产线最多 2 个任务，避免过度堆积。
        outbound_candidates = self.generate_outbound_tasks(
            max_tasks_per_line=2,
            running_task_ids=running_task_ids,
            finished_task_ids=finished_task_ids,
        )
        outbound_tasks: List[TaskData] = list(self.pending_outbound_queue)

        # 避免重复添加已存在的任务
        existing_ids = {task.task_id for task in outbound_tasks}
        for task in outbound_candidates:
            if task.task_id not in existing_ids:
                outbound_tasks.append(task)
                existing_ids.add(task.task_id)

        # 生成入库任务候选池，按巷道分组后取每个巷道的队首任务，避免同一入口的后续任务越过前序任务。
        inbound_tasks: List[TaskData] = []
        for aisle in self.aisles:
            line_buckets = {}
            for task in self.pending_inbound_by_aisle[aisle]:
                line = getattr(task, "in_line", 1)
                if line not in line_buckets:
                    line_buckets[line] = task
            inbound_tasks.extend(line_buckets.values())

        return inbound_tasks, outbound_tasks

    def _is_dispatchable_sequence_task(self, aisle: int, task_info: TaskData, current_time: float) -> bool:
        """判断指定巷道是否可执行指定任务。

        Args:
            aisle (int): 目标巷道编号。
            task_info (TaskData): 待分配或待计算的任务对象/字典。
            current_time (float): 当前仿真时间，单位秒。

        Returns:
            bool: 处理成功、条件成立或校验通过时为 ``True``，否则为 ``False``。
        """
        task_id = task_info.task_id
        task_type = task_info.task_type
        production_line = task_info.production_line

        # 入库任务仅在指定时间段内可执行
        if current_time < getattr(self, "inbound_only_seconds", 0.0) and task_type == TASK_TYPE_OUTBOUND:
            return False

        # 出库任务仅在指定时间段内可执行
        if task_type == TASK_TYPE_OUTBOUND and production_line is not None:
            if not self._is_task_relocation_ready(task_id, current_time):
                return False
            if task_id in {task.task_id for task in self.completed_tasks}:
                return False

            sku_ids = []
            for sku_entry in (task_info.skus or []):
                if isinstance(sku_entry, dict):
                    sku_id = sku_entry.get("skuId")
                else:
                    sku_id = getattr(sku_entry, "skuId", None)
                if sku_id:
                    sku_ids.append(sku_id)

            # 出库任务的 SKU 配对检查：如果任务包含两个 SKU，则必须在同一货位上配对成功才能执行。
            if len(sku_ids) == 2:
                sku1, sku2 = sku_ids
                attrs_map = self._task_attrs_map(task_info)
                attrs1 = attrs_map.get(sku1, {})
                attrs2 = attrs_map.get(sku2, {})
                paired_position, _, _ = self._check_sku_pairing_status(
                    sku1, sku2, attrs1, attrs2, task_id=task_id
                )
                if paired_position is None:
                    return False

            if self.check_blockage(aisle, production_line, current_time=current_time):
                return False
            if not self.can_start_outbound_task(task_id, production_line, getattr(task_info, "group_idx", None)):
                return False

        return True

    def get_ready_task_counts_by_aisle(self, current_time: float) -> Dict[int, int]:
        """获取指定时间段内每条产线可执行的任务数量.

        Args:
            current_time (float): 当前仿真时间，单位秒。

        Returns:
            Dict[int, int]: 按函数定义字段组织的结果映射。
        """
        ready_counts = {aisle: 0 for aisle in self.aisles}
        busy_aisles = self.get_busy_aisles(current_time)
        running_ids = set(self.running_tasks.keys())
        finished_ids = {task.task_id for task in self.completed_tasks}
        inbound_tasks, outbound_tasks = self._build_scheduler_task_pools(running_ids, finished_ids)

        random_state = random.getstate()
        # 调度器可能会在内部使用随机数生成器，导致外部调用的随机状态被污染。为避免这种情况，我们在调用调度器前保存当前随机状态，并在调用后恢复它。
        try:
            aisle_task_sequences = self.scheduler.solve(
                inbound_tasks=deepcopy(inbound_tasks),
                outbound_tasks=deepcopy(outbound_tasks),
                running_tasks=deepcopy(self.running_tasks),
                current_time=current_time,
            )
        except Exception:
            random.setstate(random_state)
            return ready_counts
        finally:
            random.setstate(random_state)

        for aisle in self.aisles:
            if aisle in busy_aisles:
                continue
            sequence = list(aisle_task_sequences.get(aisle, []) or [])
            if not sequence:
                continue
            if self._is_dispatchable_sequence_task(aisle, sequence[0], current_time):
                ready_counts[aisle] = 1

        return ready_counts


    def _build_io_port_disabled_positions(self, dock_levels_by_col: Dict[int, Any]) -> List[str]:
        """构造下游调用所需的对象、请求载荷或配置结果。

        输入：dock_levels_by_col（Dict[int, Any]）
        输出：List[str]

        Args:
            dock_levels_by_col (Dict[int, Any]): 按列号配置的出入库口层位映射。

        Returns:
            List[str]: 符合当前处理条件的结果列表或集合。
        """
        disabled = []
        for col, levels in dock_levels_by_col.items():
            if col < 1 or col > self.num_columns:
                continue
            try:
                levels_iter = [int(l) for l in levels]
            except Exception:
                continue
            for aisle in range(1, self.num_aisles + 1):
                for row in range(1, self.num_rows + 1):
                    for level in levels_iter:
                        if level < 1 or level > self.num_levels:
                            continue
                        disabled.append(f"{aisle:01d}-{row:01d}-{col:02d}-{level:02d}")
        return disabled

    # ========================================================================
    # 配置入口：入库策略、初始库存和生产计划
    # ========================================================================
    def set_inbound_aisle_allocator(self, allocator: Any):
        """设置入库任务的巷道分配策略

        Args:
            allocator: 实现了allocate(task_info, inventory_positions)方法的对象

        Returns:
            None: 通过实例状态、队列或外部副作用完成处理。
        """
        self.inbound_aisle_allocator = allocator
        print(f"设置入库巷道分配策略: {allocator.__class__.__name__}")

    def set_inbound_position_allocator(self, allocator: Any):
        """设置入库任务的货位分配策略

        Args:
            allocator: 实现了allocate(available_positions, task_info)方法的对象

        Returns:
            None: 通过实例状态、队列或外部副作用完成处理。
        """
        self.inbound_position_allocator = allocator
        print(f"设置入库货位分配策略: {allocator.__class__.__name__}")
        # 同步到调度器
        if hasattr(self, 'scheduler') and self.scheduler is not None:
            self.scheduler.position_allocator = self.inbound_position_allocator

    def set_inbound_strategies(self, inbound_allocation_strategy: Optional[str], inbound_position_strategy: Optional[str]):
        """根据策略字符串在Core内部配置入库巷道/货位分配器

        Args:
            inbound_allocation_strategy (Optional[str]): 入库巷道分配策略名称，用于选择分配器实现。
            inbound_position_strategy (Optional[str]): 入库货位选择策略名称，用于选择货位分配器实现。

        Returns:
            None: 更新 ``inbound_aisle_allocator``、``inbound_position_allocator``，并将
            货位分配器同步给已创建的调度器。
        """
        # 巷道分配策略
        if inbound_allocation_strategy:
            from allocation.proposed_strategy import ProposedAisleAllocator
            from allocation.baseline_strategy import BaselineAisleAllocator
            if inbound_allocation_strategy == 'proposed':
                allocator = ProposedAisleAllocator(self)
                # 统一接口
                self.set_inbound_aisle_allocator(allocator)
                print(f"使用提出策略进行入库巷道分配")
            else:
                # 基线策略
                allocator = BaselineAisleAllocator(self)
                self.set_inbound_aisle_allocator(allocator)
                print(f"使用基线策略({inbound_allocation_strategy})进行入库巷道分配")
        # 货位分配策略
        if inbound_position_strategy:
            if inbound_position_strategy == 'proposed':
                from allocation.proposed_strategy import ProposedPositionAllocator
                allocator = ProposedPositionAllocator(self)
                self.set_inbound_position_allocator(allocator)
                print(f"使用提出策略进行入库货位分配")
            else:
                from allocation.baseline_strategy import BaselinePositionAllocator
                allocator = BaselinePositionAllocator(self)
                self.set_inbound_position_allocator(allocator)
                print(f"使用基线策略({inbound_position_strategy})进行入库货位分配")

    # ===================== 新增：供内部其他文件调用的函数（非API） =====================
    def initialize_core(self, production_plan: Dict[Any, Any], initial_inventory: Optional[dict] = None, initial_inventory_count: int = 250):
        """用于被外部仿真器调用的核心初始化：库存、生产计划与运行态重置

        Args:
            production_plan (Dict[int, List[List[List[str]]]]): 按产线、生产组和任务组织的生产计划。
            initial_inventory (Optional[dict]): 可选的初始库存快照；提供时优先按该快照构造仓库状态。
            initial_inventory_count (int): 未传初始快照时用于从历史入库记录初始化的记录数量上限。

        Returns:
            None: 重置库存、生产组、队列和资源状态；初始库存来源由 ``initial_inventory``
            与历史入库记录分支决定。
        """
        # 初始化库存
        self.inventory_manager.initialize()
        if initial_inventory:
            # 允许外部直接写入初始库存（可选）
            for aisle, sku_qty in initial_inventory.items():
                for sku_id, qty in sku_qty.items():
                    # 这里不指定具体货位，假设库存管理器支持该接口或由外部已分配
                    # 初始快照只给出巷道/SKU 数量时，使用库存管理器的正式写入接口
                    # 选择可写货位；不再调用未定义的动态方法。
                    for _ in range(int(qty or 0)):
                        candidates = [
                            pos for pos in self.inventory_manager.inventory_positions
                            if pos.aisle == int(aisle) and pos.is_empty()
                        ]
                        if not candidates:
                            break
                        self.inventory_manager.add_inventory(candidates[0], str(sku_id), 1)
        else:
            # 如果没有提供initial_inventory，则通过读取inbound_task_fig前N组入库任务来进行初始化
            self.inventory_manager.initialize_from_inbound_tasks(
                self.inbound_records,
                self.aisles,
                self.inbound_position_allocator,
                self.inbound_aisle_allocator,
                initial_inventory_count
            )
        # 打印配对统计信息

        try:
            pairing = self.inventory_manager.get_pairing_stats()
            print(f"  成功配对货位(配对总数): {pairing['matched_pairs']}")
            print(f"  solo货位: {pairing['solo_upper']}")
            print(f"  总计双层货位 {pairing['double_slots']}个, 已占用 {pairing['filled_slots']}个")
            print(f"  潜在配对数: {pairing['potential_pairs']}")
            matched_pairs = pairing['matched_pairs']
            potential_pairs = pairing['potential_pairs']
            total_pairs = matched_pairs + potential_pairs
            success_rate = matched_pairs / total_pairs if total_pairs > 0 else 0
            total_goods = pairing.get('total_goods', 0)
            paired_beams = pairing.get('paired_beams', matched_pairs * 2)
            paired_beams_including_solo = pairing.get(
                'paired_beams_including_solo',
                paired_beams + pairing.get('solo_beams', pairing.get('solo_upper', 0)),
            )
            beam_match_rate = pairing.get(
                'beam_match_rate',
                paired_beams / total_goods if total_goods else 0,
            )
            beam_match_rate_including_solo = pairing.get(
                'beam_match_rate_including_solo',
                paired_beams_including_solo / total_goods if total_goods else 0,
            )
            print(f"  匹配率(旧): {matched_pairs}/{total_pairs} = {success_rate:.2%}")
            print(
                f"  匹配率(新,不含solo): {paired_beams}/{total_goods} = {beam_match_rate:.2%}"
            )
            print(
                f"  匹配率(新,含solo): {paired_beams_including_solo}/{total_goods} = {beam_match_rate_including_solo:.2%}"
            )
            print(f"  总梁数: {total_goods}")

            # 打印各巷道配对货位数和货物数
            print("  各巷道统计:")
            for aisle, count in pairing['matched_pairs_by_aisle'].items():
                aisle_goods = pairing['goods_by_aisle'][aisle]
                print(f" 巷道{aisle}: {count}个配对货位/{aisle_goods}个货物")
        except Exception as e:
            print(f"[WARN] 获取配对统计信息时出错: {e}")
        self.set_production_plan(production_plan)
        # 重置运行态
        self.running_tasks.clear()
        self.completed_tasks.clear()
        self.pending_inbound_by_aisle = {aisle: [] for aisle in self.aisles}
        self.pending_outbound_queue.clear()
        self.crane_available_times.clear()
        self.event_queue.clear()
        self.current_time = 0.0
        self.current_position_by_aisle = {aisle: None for aisle in self.aisles}
        # 重置堵塞状态
        for aisle in self.aisles:
            for pl in range(1, self.num_production_lines + 1):
                self.blockage_status[(aisle, pl)] = {'blocked': False, 'unblock_time': 0.0}
        # 重置生产计划跟踪变量
        for pl in range(1, self.num_production_lines + 1):
            self.production_line_current_group[pl] = 0
            self.production_line_completed_tasks[pl] = set()
            self.production_line_group_completion_times[pl] = []

    # ========================================================================
    # 外部状态反馈与入库到达处理
    # ========================================================================
    def apply_task_feedback(self, feedback: dict):
        """接收外部真实任务完成信息，更新状态并记录
        预期字段：taskId, taskType, status, durationSeconds/startTime/endTime, aisleId,
                 sourcePosition/targetPosition {aisleId/column/row/level/shelf}, reason

        Args:
            feedback (dict): 外部执行反馈数据，包含任务状态、位置或异常信息。

        Returns:
            None: 记录反馈、刷新任务状态和巷道当前位置；若对应任务正在等待反馈，
            立即触发该任务的完成状态转换。
        """
        if not hasattr(self, "external_feedback"):
            self.external_feedback: list = []
        self.external_feedback.append(feedback)

        task_id = feedback.get("taskId")
        if task_id:
            self.task_status[task_id] = feedback.get("status", "COMPLETED").upper()
            self.feedback_received.add(task_id)

        # 更新巷道当前位置（优先用目标位，退化用源位）
        pos_info = feedback.get("targetPosition") or feedback.get("sourcePosition") or {}
        aisle = feedback.get("aisleId") or pos_info.get("aisleId")
        try:
            if aisle and pos_info:
                pos = InventoryPosition(
                    aisle=int(aisle),
                    row=int(pos_info.get("row", 0) or 0),
                    column=int(pos_info.get("column", 0) or 0),
                    level=int(pos_info.get("level", 0) or 0),
                    is_double_layer=True if pos_info.get("shelf") else False,
                )
                self.current_position_by_aisle[int(aisle)] = pos
        except Exception:
            pass

        # 如果该任务在等待反馈，立即触发完成逻辑（使用当前时间作为完成时间）
        if task_id and task_id in getattr(self, "awaiting_feedback", set()):
            # 尝试从 running 中取出任务并调度完成事件
            task_obj = self.running_tasks.get(task_id)
            if task_obj:
                ev = Event(self.current_time, f"FEEDBACK_COMPLETE_{task_id}", EVENT_TASK_COMPLETE, task_obj)
                self.on_event(ev, self.current_time, simulation_mode=True)

    def allocate_inbound_aisle(self, task_or_stub, current_time: float) -> Event:
        """将未指定巷道的入库请求转为“到达巷道入口”事件。

        调用来源是 ``EVENT_INBOUND_UNASSIGNED`` 分支。输入为带 ``skus`` 和可选
        ``in_line`` 的任务对象；方法按 BOM/SKU 可服务产线、``match_fields``、库存及
        模拟占位调用入库巷道分配器，返回携带新 ``TaskData`` 的
        ``EVENT_INBOUND_ARRIVAL_AT_AISLE``。
        本方法只确定巷道并生成事件，不写真实库存，也不把任务直接加入运行队列。

        Args:
            task_or_stub (Any): 未分配入库任务或仅含 SKU 的临时任务对象。
            current_time (float): 当前仿真时间，单位秒。

        Returns:
            Event: 当前处理流程产生的结果，具体结构见函数摘要。
        """
        # 入库只携带物料和入口线，不绑定具体生产计划产线。SKU 配置中记录的
        # 可服务产线只用于筛掉无法出库到目标产线的巷道，不能写入任务 production_line。
        skus = task_or_stub.skus
        in_line = getattr(task_or_stub, "in_line", 1)

        # 与 skus 顺序对齐：每项是该 SKU 可由哪些生产线消耗。双梁的两项可不同；
        # 分配器会要求目标巷道分别至少服务每项中的一条产线。
        eligible_production_lines = []
        for sku_entry in skus:
            sku_id = sku_entry.get("skuId") if isinstance(sku_entry, dict) else None
            configured_lines = self.sku_to_production_line.get(sku_id, []) if sku_id else []
            if not isinstance(configured_lines, (list, tuple, set)):
                configured_lines = [configured_lines]
            eligible_production_lines.append([
                int(line) for line in configured_lines if line is not None and str(line).strip()
            ])

        # 分配器可读取 pending/running 的模拟入库状态。明确传入任务类型，保证
        # ProposedAisleAllocator 不读取任务 production_line，而按每根 SKU 的可达范围过滤。
        assigned_aisle = None
        if self.inbound_aisle_allocator is not None:
            assigned_aisle = self.inbound_aisle_allocator.allocate(
                {
                    'skus': skus,
                    'task_type': TASK_TYPE_INBOUND,
                    'in_line': in_line,
                    'eligible_production_lines': eligible_production_lines,
                },
                self.inventory_manager.inventory_positions,
            )

        # 分配器未给出结果时，仍按 SKU 可服务产线回退。无配置 SKU 时退回全部巷道，
        # 以保持旧配置兼容，并由日志暴露缺少的 SKU 映射。
        if assigned_aisle is None:
            fallback_aisles = list(self.aisles)
            for configured_lines in eligible_production_lines:
                if not configured_lines:
                    continue
                serviceable_aisles = [
                    aisle for aisle, aisle_lines in self.aisle_production_line_mapping.items()
                    if set(configured_lines) & {int(line) for line in aisle_lines}
                ]
                fallback_aisles = list(set(fallback_aisles) & set(serviceable_aisles))
            assigned_aisle = random.choice(fallback_aisles or self.aisles)
        # 生成任务ID（含入库线标记）
        self.task_id_counter[TASK_TYPE_INBOUND] += 1
        task_id = f"IN_IL{int(in_line)}_{self.task_id_counter[TASK_TYPE_INBOUND]:05d}"
        # 返回到达入口事件
        arrival_time = current_time + self.transport_delay_s
        inbound_task = TaskData(
            task_id=task_id,
            task_type=TASK_TYPE_INBOUND,
            task_name=task_id,
            skus=skus,
            # 入库任务不属于生产计划产线；0 仅是 TaskData 的兼容占位值。
            production_line=0,
            in_line=in_line,
            assigned_aisle=assigned_aisle,
        )
        event_id = f"{EVENT_INBOUND_ARRIVAL_AT_AISLE}_{task_id}"
        return Event(arrival_time, event_id, EVENT_INBOUND_ARRIVAL_AT_AISLE, inbound_task)


    def _task_attrs_map(self, task: TaskData) -> Dict[str, Dict[str, Any]]:
        """提取任务SKU的额外属性（按skuId分组）。

        Args:
            task (TaskData): 当前处理的 TaskData 任务对象。

        Returns:
            Dict[str, Dict[str, Any]]: 按函数定义字段组织的结果映射。
        """
        match_fields = self.match_fields or []
        if not match_fields:
            return {}
        attrs_map: Dict[str, Dict[str, Any]] = {}
        for entry in (getattr(task, "skus", None) or []):
            if not isinstance(entry, dict):
                continue
            sku_id = entry.get("skuId")
            if not sku_id:
                continue
            attrs = {}
            for field in match_fields:
                if field in entry:
                    attrs[field] = entry.get(field)
            if attrs:
                attrs_map[sku_id] = attrs
        return attrs_map

    def _format_task_skus(self, task: TaskData) -> str:
        """格式化任务SKU（包含额外属性）用于日志输出。

        Args:
            task (TaskData): 当前处理的 TaskData 任务对象。

        Returns:
            str: 当前处理得到的文本标识、状态或诊断信息。
        """
        match_fields = self.match_fields or []
        entries = []
        for entry in (getattr(task, "skus", None) or []):
            if isinstance(entry, dict):
                sku_val = entry.get("skuId")
                sku_str = "None" if sku_val is None else str(sku_val)
                attrs = []
                for field in match_fields:
                    if field in entry:
                        attrs.append(f"{field}={entry.get(field)}")
                if attrs:
                    sku_str = f"{sku_str}({', '.join(attrs)})"
                entries.append(sku_str)
            else:
                entries.append(str(entry))
        return f" SKUs=[{', '.join(entries)}]" if entries else ""

    def _get_empty_slots_by_aisle(self) -> Dict[int, int]:
        """统计各巷道当前仍可继续入库的位置数。

        Returns:
            Dict[int, int]: 按函数定义字段组织的结果映射。
        """
        empty_slots_by_aisle = {aisle: 0 for aisle in self.aisles}
        for pos in self.inventory_manager.inventory_positions:
            try:
                if pos.is_empty():
                    empty_slots_by_aisle[pos.aisle] = empty_slots_by_aisle.get(pos.aisle, 0) + 1
            except Exception:
                continue
        return empty_slots_by_aisle

    def _get_position_sku_quantity(self, pos: InventoryPosition, sku_id: str) -> int:
        """统计单个货位中指定 SKU 的数量。

        Args:
            pos (InventoryPosition): 当前处理的 InventoryPosition 货位对象。
            sku_id (str): 当前处理的 SKU 标识。

        Returns:
            int: 当前计算得到的数量、索引、时间或编号。
        """
        qty = 0
        try:
            if getattr(pos, "is_double_layer", False):
                if getattr(pos, "upper_sku", None) == sku_id:
                    qty += int(getattr(pos, "upper_quantity", 0) or 0)
                if getattr(pos, "lower_sku", None) == sku_id:
                    qty += int(getattr(pos, "lower_quantity", 0) or 0)
            else:
                if getattr(pos, "sku", None) == sku_id:
                    qty += int(getattr(pos, "quantity", 0) or 0)
        except Exception:
            return 0
        return qty

    def get_inbound_allocation_context(self, task_info: Any, inventory_positions: Optional[List[Any]] = None) -> Dict[str, Any]:
        """构造入库分配阶段的日志上下文。

        Args:
            task_info (Any): 待分配或待计算的任务对象/字典。
            inventory_positions (Optional[List[Any]]): 当前仓库货位列表；函数会在自身说明中明确是否修改。

        Returns:
            Dict[str, Any]: 按函数定义字段组织的结果映射。
        """
        positions = inventory_positions if inventory_positions is not None else self.inventory_manager.inventory_positions
        # 该上下文既被 API 字典任务也被 TaskData 调用；统一经 helper 读取，避免
        # 把 ``dict`` 静态推断为具有 ``skus`` 属性的对象。
        skus_data = _get_task_field(task_info, "skus", []) or []

        sku_ids: List[str] = []
        for entry in skus_data or []:
            if isinstance(entry, dict):
                sku_id = entry.get("skuId")
            else:
                sku_id = getattr(entry, "skuId", None)
            if sku_id and sku_id not in sku_ids:
                sku_ids.append(sku_id)

        sku_qty_by_aisle: Dict[str, Dict[int, int]] = {}
        mate_qty_by_aisle: Dict[str, Dict[int, int]] = {}
        solo_skus: List[str] = []

        for sku_id in sku_ids:
            sku_qty_by_aisle[sku_id] = {aisle: 0 for aisle in self.aisles}
            mate = self.sku_pairs.get(sku_id)
            is_solo = sku_id in self.sku_solo or not mate or mate == sku_id
            if is_solo:
                solo_skus.append(sku_id)
            else:
                mate_qty_by_aisle[sku_id] = {aisle: 0 for aisle in self.aisles}

        for pos in positions or []:
            aisle = getattr(pos, "aisle", None)
            if aisle not in self.aisles:
                continue
            for sku_id in sku_ids:
                sku_qty_by_aisle[sku_id][aisle] += self._get_position_sku_quantity(pos, sku_id)
                mate = self.sku_pairs.get(sku_id)
                if sku_id in mate_qty_by_aisle and mate:
                    mate_qty_by_aisle[sku_id][aisle] += self._get_position_sku_quantity(pos, mate)

        return {
            "sku_qty_by_aisle": sku_qty_by_aisle,
            "mate_qty_by_aisle": mate_qty_by_aisle,
            "solo_skus": solo_skus,
        }

    def _get_task_qty_by_aisle(
        self,
        task: Optional[TaskData]
    ) -> Tuple[Dict[str, int], Dict[int, int]]:
        """统计当前任务的SKU需求，以及各巷道相关库存总量。

        Args:
            task (Optional[TaskData]): 当前处理的 TaskData 任务对象。

        Returns:
            Tuple[Dict[str, int], Dict[int, int]]: 由函数摘要中所列字段组成的多值结果。
        """
        demand_by_sku: Dict[str, int] = {}
        qty_by_aisle = {aisle: 0 for aisle in self.aisles}
        if task is None:
            return demand_by_sku, qty_by_aisle
        for sku_entry in (getattr(task, "skus", None) or []):
            sku_id = sku_entry.get("skuId") if isinstance(sku_entry, dict) else None
            qty = sku_entry.get("quantity", 1) if isinstance(sku_entry, dict) else 1
            if sku_id:
                demand_by_sku[sku_id] = demand_by_sku.get(sku_id, 0) + int(qty or 1)

        for aisle in self.aisles:
            aisle_inventory = self.inventory_manager.current_inventory.get(aisle, {})
            qty_by_aisle[aisle] = sum(aisle_inventory.get(sku, 0) for sku in demand_by_sku)
        return demand_by_sku, qty_by_aisle

    def _get_task_paired_positions_by_aisle(self, task: TaskData) -> Tuple[Dict[int, int], Dict[int, List[str]]]:
        """统计当前任务在各巷道中已配对可直接出库的位置数。

        Args:
            task (TaskData): 当前处理的 TaskData 任务对象。

        Returns:
            Tuple[Dict[int, int], Dict[int, List[str]]]: 由函数摘要中所列字段组成的多值结果。
        """
        paired_positions_by_aisle = {aisle: 0 for aisle in self.aisles}
        paired_position_ids_by_aisle = {aisle: [] for aisle in self.aisles}
        sku_ids = task.get_sku_ids() if task else []
        if len(sku_ids) != 2:
            return paired_positions_by_aisle, paired_position_ids_by_aisle

        attrs_map = self._task_attrs_map(task)
        sku1, sku2 = sku_ids
        attrs1 = attrs_map.get(sku1, {})
        attrs2 = attrs_map.get(sku2, {})
        for pos in self.inventory_manager.inventory_positions:
            try:
                if not getattr(pos, "is_double_layer", False):
                    continue
                if not pos.matches_pair(sku1, attrs1, sku2, attrs2, self.match_fields):
                    continue
                if not (
                    getattr(pos, "upper_quantity", 0) > 0
                    and getattr(pos, "lower_quantity", 0) > 0
                ):
                    continue
                aisle = pos.aisle
                paired_positions_by_aisle[aisle] += 1
                paired_position_ids_by_aisle[aisle].append(pos.get_position_id())
            except Exception:
                continue
        return paired_positions_by_aisle, paired_position_ids_by_aisle

    def decide_for_idle_aisles(self, current_time: float) -> List[Event]:
        """为所有空闲巷道决策下一任务（入/出库），返回产生的任务完成事件列表

        Args:
            current_time (float): 当前仿真时间，单位秒。

        Returns:
            List[Event]: 为本次由空闲变为运行中的任务生成的完成事件列表。事件已由
            调用方压入事件队列；没有可启动任务或全部巷道忙碌时返回空列表。
        """
        self.check_and_relocate_inventory()
        events: List[Event] = []
        # 阶段一：running 任务和进行中的移库都独占巷道。后续只对未出现在
        # busy_aisles 的巷道调用调度器结果，避免同巷道并发启动两个任务。
        busy_aisles = set()
        for t in self.running_tasks.values():
            try:
                aisle = t.assigned_aisle
                if aisle:
                    busy_aisles.add(aisle)
            except Exception:
                pass
        # 移库占用也视为巷道忙碌
        for aisle in self.aisles:
            if self._is_aisle_relocation_busy(aisle, current_time):
                busy_aisles.add(aisle)
        # 阶段二：根据生产组推进状态补齐可释放的出库候选。running/completed ID
        # 传给生成器，用于阻止已执行或已完成任务再次被放回 pending 队列。
        running_ids = set(self.running_tasks.keys())
        finished_ids = set([t.task_id for t in self.completed_tasks])
        outbound_candidates = self.generate_outbound_tasks(max_tasks_per_line=2, running_task_ids=running_ids, finished_task_ids=finished_ids)
        # 合并到队列（去重）
        existing_ids = set([t.task_id for t in self.pending_outbound_queue])
        for t in outbound_candidates:
            if t.task_id not in existing_ids:
                self.pending_outbound_queue.append(t)

        # 阶段三：入库按“巷道 + 入库线”取队首。循环不直接取整个队列，是为了
        # 保持同一入库线 FIFO，同时允许不同入库线在调度器中参与同一轮比较。
        inbound_tasks: List[TaskData] = []
        for a in self.aisles:
            line_buckets = {}
            for t in self.pending_inbound_by_aisle[a]:
                line = getattr(t, "in_line", 1)
                if line not in line_buckets:
                    # 保持队列顺序，遇到第一个即为该线路队首
                    line_buckets[line] = t
            inbound_tasks.extend(line_buckets.values())
        if inbound_tasks:
            pending_sizes = {a: len(self.pending_inbound_by_aisle[a]) for a in self.aisles}
            print(f"[DEBUG][core] inbound pending sizes: {pending_sizes}")
        outbound_tasks: List[TaskData] = list(self.pending_outbound_queue)

        # 使用已实例化的调度器
        aisle_task_sequences = self.scheduler.solve(
            inbound_tasks=inbound_tasks,
            outbound_tasks=outbound_tasks,
            running_tasks=self.running_tasks,
            current_time=current_time,
        )
        # 阶段四：仅消费每条巷道序列的第一个任务。调度器可能返回多任务序列，
        # 但当前事件轮次每个空闲巷道最多启动一个任务，后续任务等待下一次派发。
        for aisle in self.aisles:
            if aisle in busy_aisles:
                continue
            sequence = aisle_task_sequences.get(aisle, [])
            if not sequence:
                continue
            task_info = sequence[0]

            task_id = task_info.task_id
            task_type = task_info.task_type
            production_line = task_info.production_line

            # 如果是出库任务，检查是否可以开始
            if task_type == TASK_TYPE_OUTBOUND and production_line is not None:
                # 出库启动前依次检查移库完成、重复完成、同位直接配对和出口拥堵；
                # 任一条件不满足都保持任务在 pending 中，不删除也不扣减库存。
                if not self._is_task_relocation_ready(task_id, current_time):
                    continue
                if task_id in {t.task_id for t in self.completed_tasks}:
                    continue
                sku_ids = []
                for s in (task_info.skus or []):
                    if isinstance(s, dict):
                        sid = s.get('skuId')
                    else:
                        sid = getattr(s, 'skuId', None)
                    if sid:
                        sku_ids.append(sid)
                if len(sku_ids) == 2:
                    sku1, sku2 = sku_ids
                    attrs_map = self._task_attrs_map(task_info)
                    attrs1 = attrs_map.get(sku1, {})
                    attrs2 = attrs_map.get(sku2, {})
                    paired_position, sku1_positions, sku2_positions = self._check_sku_pairing_status(
                        sku1, sku2, attrs1, attrs2, task_id=task_id
                    )
                    if paired_position is None:
                        self._perform_relocation_if_needed(
                            task_id, production_line, sku1, sku2, sku1_positions, sku2_positions, attrs1, attrs2
                        )
                        continue
                if self.check_blockage(aisle, production_line, current_time=current_time):
                    continue
                # 检查产线组的顺序约束（前面的组是否完成）
                if not self.can_start_outbound_task(task_id, production_line, getattr(task_info, "group_idx", None)):
                    continue

            task_info.task_record = self.generate_task_record(task_info, current_time)

            # 添加到运行任务字典
            self.running_tasks[task_id] = task_info

            # 实时打印任务（含额外属性）
            sku_info = self._format_task_skus(task_info)
            pos_info = ""
            if getattr(task_info, "positions", None):
                pos_ids = [p.get_position_id() for p in task_info.positions]
                pos_info = f" positions={pos_ids}"
            if task_type == TASK_TYPE_INBOUND:
                print(f"[dispatch][inbound_context] empty_slots_by_aisle={self._get_empty_slots_by_aisle()}")
            elif task_type == TASK_TYPE_OUTBOUND:
                task_demand, task_qty_by_aisle = self._get_task_qty_by_aisle(task_info)
                task_paired_positions_by_aisle, task_paired_position_ids_by_aisle = (
                    self._get_task_paired_positions_by_aisle(task_info)
                )
                print(
                    f"[dispatch][outbound_context] current_task_demand={task_demand} "
                    f"current_task_qty_by_aisle={task_qty_by_aisle} "
                    f"task_paired_positions_by_aisle={task_paired_positions_by_aisle} "
                    f"task_paired_position_ids_by_aisle={task_paired_position_ids_by_aisle}"
                )
            print(
                f"[dispatch] {task_type} task={task_id} aisle={aisle} pl={production_line}{sku_info}{pos_info}"
            )

            # 从等待队列中移除已开工的任务
            if task_type == TASK_TYPE_OUTBOUND:
                self.pending_outbound_queue = [t for t in self.pending_outbound_queue if t.task_id != task_id]
                # 出库：日志打印后再扣减库存，保证上下文反映派发前快照
                sku_ids_list = [s.get('skuId', None) for s in task_info.skus if 'skuId']
                for idx, sku in enumerate(sku_ids_list):
                    pos = task_info.positions[min(idx, len(task_info.positions) - 1)]
                    try:
                        self.inventory_manager.remove_inventory(pos, sku, 1)
                    except Exception:
                        print(f"[DEBUG] 扣减库存失败: {sku}")
            else:
                # 入库：在该巷道的等待队列中删除对应task_id
                self.pending_inbound_by_aisle[aisle] = [t for t in self.pending_inbound_by_aisle[aisle] if t.task_id != task_id]

            ev_id = f"{EVENT_TASK_COMPLETE}_{task_info.task_id}"
            delivery_time = task_info.task_record.get('delivery_time', current_time)
            ev = Event(float(delivery_time or current_time), ev_id, EVENT_TASK_COMPLETE, task_info)
            events.append(ev)

        if not events:
            print(f"[DEBUG] 没有事件生成，当前时间: {self.current_time:.2f}s")
            try:
                idle_aisles = [a for a in self.aisles if a not in busy_aisles]
                pending_sizes = {a: len(self.pending_inbound_by_aisle.get(a, [])) for a in self.aisles}
                seq_total = sum(len(seq) for seq in aisle_task_sequences.values())
                print(
                    f"[DEBUG][dispatch] idle_aisles={idle_aisles} busy_aisles={sorted(busy_aisles)} "
                    f"seq_total={seq_total} inbound_tasks={len(inbound_tasks)} outbound_tasks={len(outbound_tasks)} "
                    f"pending_outbound={len(self.pending_outbound_queue)} pending_inbound_sizes={pending_sizes}"
                )
                for aisle in idle_aisles:
                    seq = aisle_task_sequences.get(aisle, [])
                    if not seq:
                        print(f"[DEBUG][dispatch] aisle {aisle}: scheduler empty")
                        continue
                    task_info = seq[0]
                    if task_info.task_type == TASK_TYPE_OUTBOUND and task_info.production_line is not None:
                        if self.check_blockage(aisle, task_info.production_line, current_time=current_time):
                            print(f"[DEBUG][dispatch] aisle {aisle}: outbound blocked pl={task_info.production_line}")
                            continue
                        if not self.can_start_outbound_task(
                            task_info.task_id,
                            task_info.production_line,
                            getattr(task_info, "group_idx", None),
                        ):
                            print(f"[DEBUG][dispatch] aisle {aisle}: outbound order blocked task={task_info.task_id}")
                            continue
                    print(
                        f"[DEBUG][dispatch] aisle {aisle}: candidate ready task={task_info.task_id} "
                        f"type={task_info.task_type}"
                    )
            except Exception as e:
                print(f"[DEBUG][dispatch] 生成空派发摘要失败: {e}")


        # 临时调度日志（当前保持关闭）。
        # with open(f'log_{self.scheduler_type}.txt', 'a') as f:
        #     f.write(f"当前时间: {self.current_time:.2f}s, 事件: {events}\n")
        #     f.write(f"aisle_task_sequences: {aisle_task_sequences}\n")

        return events

    def set_production_plan(self, production_plan: Any) -> None:
        """设置当日生产计划

        Args:
            production_plan: {production_line: [
                [['A1', 'A2'], ['A3', 'A4']],  第 1 组，包含 2 个任务
                [['A1', 'A2'], ['A3', 'A4']],  第 2 组，包含 2 个任务
                ...
            ]}

        Returns:
            None: 通过实例状态、队列或外部副作用完成处理。
        """
        plan_attrs = None
        if isinstance(production_plan, dict) and "production_plan" in production_plan:
            plan_attrs = production_plan.get("production_plan_attrs")
            plan_versions = production_plan.get("production_plan_versions")
            # 兼容旧字段 production_plan_versions -> attrs["version"]
            if plan_attrs is None and plan_versions is not None:
                plan_attrs = {"version": plan_versions}
            production_plan = production_plan.get("production_plan", {})
        self.production_plan = production_plan
        if plan_attrs is not None:
            self.production_plan_attrs = {k: {int(kk): vv for kk, vv in v.items()} for k, v in plan_attrs.items()}
        # 重置进度
        for pl in range(1, self.num_production_lines + 1):
            self.production_line_current_group[pl] = 0
            self.production_line_completed_tasks[pl] = set()
            self.production_line_group_completion_times[pl] = []
        print(f"\n设置生产计划:")
        for pl, groups in production_plan.items():
            print(f"  产线{pl}: {len(groups)}组，每组{len(groups[0]) if groups else 0}个task")

    def update_blockage_status(self, aisle: int, production_line: int,
                               blocked: bool, unblock_time: float = 0.0):
        """更新堵塞状态

        Args:
            aisle: 巷道号
            production_line: 产线号
            blocked: 是否堵塞
            unblock_time: 预估解除堵塞的时间（仿真时间）

        Returns:
            None: 通过实例状态、队列或外部副作用完成处理。
        """
        self.blockage_status[(aisle, production_line)] = {
            'blocked': blocked,
            'unblock_time': unblock_time
        }

    def check_blockage(self, aisle: int, production_line: int, current_time: float = 0.0) -> bool:
        """检查某个巷道*产线是否堵塞

        Args:
            aisle: 巷道号
            production_line: 产线号
            current_time: 当前仿真时间

        Returns:
            是否堵塞
        """
        status = self.blockage_status.get((aisle, production_line), {'blocked': False, 'unblock_time': 0.0})
        if status['blocked']:
            # 如果已经到了解除时间，自动解除堵塞
            if current_time >= status['unblock_time']:
                self.update_blockage_status(aisle, production_line, False, 0.0)
                return False
            return True
        return False

    def can_start_outbound_task(self, task_id: str, production_line: int, group_idx: Optional[int] = None) -> bool:
        """检查出库任务是否可以开始（考虑产线组的顺序约束）

        Args:
            task_id: 任务ID（格式：OUTBOUND_PL{pl}_GP{group}_{sku1}_{sku2}）
            production_line: 产线号
            group_idx: 可选的 core 内部 0-based 组索引；API planIndex 会先转换后传入

        Returns:
            是否可以开始该任务
        """
        if production_line is None:
            return True

        if group_idx is not None:
            try:
                task_group_idx = int(group_idx)
            except (TypeError, ValueError):
                return True
        else:
            # 从task_id中提取组号
            try:
                parts = task_id.split('_')
                # 格式：OUTBOUND_PL{pl}_GP{group}_{sku1}_{sku2}
                if len(parts) >= 3 and parts[0] == TASK_TYPE_OUTBOUND and parts[1].startswith('PL') and parts[2].startswith('GP'):
                    # 去掉 'GP'
                    task_group_number = int(parts[2][2:])
                    task_group_idx = task_group_number - 1
                else:
                    # 无法解析，允许开始
                    return True
            except (ValueError, IndexError):
                # 解析失败，允许开始
                return True

        # 检查是否超出生产计划范围
        if production_line not in self.production_plan:
            return True
        if task_group_idx >= len(self.production_plan[production_line]):
            # 超出计划范围，不能开始
            return False

        # 检查是否是当前组
        current_group_idx = self.production_line_current_group.get(production_line, 0)
        if task_group_idx != current_group_idx:
            # 只允许当前组任务开始；public 1-based planIndex 已在API层转换为 core 0-based
            return False

        return True

    def mark_outbound_completed(self, production_line: int, task: TaskData, completion_time: float = 0.0):
        """标记某个出库任务完成（用于跟踪生产计划进度）

        Args:
            production_line: 产线号
            task_id: 完成的任务ID（格式：OUT_GROUP_{production_line}_{group_number}_{sku1}_{sku2}）
            completion_time: 完成时间（绝对仿真时间）


        Returns:
            None: 不返回业务数据；处理结果通过实例状态、队列或传入对象体现。
        """
        # 从task_id中提取组号（group_number = group_idx + 1）
        # 优先使用任务上记录的 group_idx（生成时写入），否则解析 task_id
        if hasattr(task, "group_idx"):
            task_group_idx = getattr(task, "group_idx", None)
        else:
            task_group_idx = None
        task_id = task.task_id
        if task_group_idx is None:
            try:
                parts = task_id.split('_')
                if len(parts) >= 3 and parts[0] == TASK_TYPE_OUTBOUND and parts[1].startswith('PL') and parts[2].startswith('GP'):
                    # 去掉 'GP'
                    task_group_number = int(parts[2][2:])
                    task_group_idx = task_group_number - 1
                else:
                    print(f"警告：无法解析任务ID格式: {task_id}")
                    return
            except (ValueError, IndexError) as e:
                print(f"警告：解析任务ID时出错: {task_id}, 错误: {e}")
                return

        # 检查任务所属的组是否有效
        if production_line not in self.production_plan:
            return True
        if task_group_idx >= len(self.production_plan[production_line]):
            return True

        # 标记task完成（使用任务所属的组，而不是current_group）
        self.production_line_completed_tasks[production_line].add(task_id)

        # 获取任务所属的组
        task_group = self.production_plan[production_line][task_group_idx]

        expected_task_count = len(task_group)
        generated_group_task_ids = [
            f"{TASK_TYPE_OUTBOUND}_PL{production_line}_GP{task_group_idx+1}_{'_'.join(task_skus)}"
            for task_skus in task_group
        ]

        # 仿真内部默认使用标准生成的 task_id，保持历史组推进逻辑不变。
        # 只有在任务 ID 明显不是内部标准格式时，才回退到“按系统中真实存在的同组任务”
        # 做完成判断，以兼容 API 侧自定义 taskId 的场景。
        if task_id in generated_group_task_ids:
            all_task_ids_in_group = generated_group_task_ids
        else:
            known_group_task_ids = {task_id}

            for pending_task in list(getattr(self, "pending_outbound_queue", []) or []):
                if getattr(pending_task, "production_line", None) != production_line:
                    continue
                if getattr(pending_task, "group_idx", None) == task_group_idx and getattr(pending_task, "task_id", None):
                    known_group_task_ids.add(pending_task.task_id)

            for running_task in list((getattr(self, "running_tasks", {}) or {}).values()):
                if getattr(running_task, "task_type", None) != TASK_TYPE_OUTBOUND:
                    continue
                if getattr(running_task, "production_line", None) != production_line:
                    continue
                if getattr(running_task, "group_idx", None) == task_group_idx and getattr(running_task, "task_id", None):
                    known_group_task_ids.add(running_task.task_id)

            for completed_task in list(getattr(self, "completed_tasks", []) or []):
                if getattr(completed_task, "task_type", None) != TASK_TYPE_OUTBOUND:
                    continue
                if getattr(completed_task, "production_line", None) != production_line:
                    continue
                if getattr(completed_task, "group_idx", None) == task_group_idx and getattr(completed_task, "task_id", None):
                    known_group_task_ids.add(completed_task.task_id)

            if len(known_group_task_ids) == expected_task_count:
                all_task_ids_in_group = sorted(known_group_task_ids)
            else:
                all_task_ids_in_group = generated_group_task_ids

        # 检查该组是否全部完成
        if all(tid in self.production_line_completed_tasks[production_line] for tid in all_task_ids_in_group):
            # 该组完成，记录完成时间
            self.production_line_group_completion_times[production_line].append({
                'group_idx': task_group_idx,
                'completion_time': completion_time
            })

            # 更新current_group到已完成的组的下一组
            # 注意：可能跨越多个组（如果之前的组也都完成了）
            if task_group_idx >= self.production_line_current_group[production_line]:
                self.production_line_current_group[production_line] = task_group_idx + 1
                # print(f"  产线{production_line}第{task_group_idx+1}组完成（时间：{completion_time:.2f}秒），进入第{task_group_idx+2}组")

            # 只保留未完成组的任务
            for tid in all_task_ids_in_group:
                self.production_line_completed_tasks[production_line].discard(tid)

    def _generate_tasks_for_group(self, production_line: int, group_idx: int,
                                 max_tasks: int, running_task_ids: set,
                                 finished_task_ids: set) -> List[TaskData]:
        """为指定产线的指定组生成出库任务（辅助方法）

        Args:
            production_line (int): 任务所属生产线编号。
            group_idx (int): 生产组从零开始的内部索引。
            max_tasks (int): 当前处理的任务集合或任务队列。
            running_task_ids (set): 已在运行中的任务 ID 集合，用于生成任务时去重。
            finished_task_ids (set): 已完成任务 ID 集合，用于阻止生产计划重复生成旧任务。

        Returns:
            List[TaskData]: 符合当前处理条件的结果列表或集合。
        """
        tasks = []
        group = self.production_plan[production_line][group_idx]
        # 额外属性（按字段名分组）
        attrs_group = {}
        for field, by_line in (self.production_plan_attrs or {}).items():
            line_groups = by_line.get(production_line, [])
            attrs_group[field] = line_groups[group_idx] if group_idx < len(line_groups) else []
        completed_task_ids = self.production_line_completed_tasks[production_line]

        tasks_generated = 0
        for task_idx, task_skus in enumerate(group):
            if tasks_generated >= max_tasks:
                break

            # 生成任务ID
            if len(task_skus) == 1:
                # 单梁任务
                task_id = f"{TASK_TYPE_OUTBOUND}_PL{production_line}_GP{group_idx+1}_{task_skus[0]}"
            else:
                # 双梁任务
                task_id = f"{TASK_TYPE_OUTBOUND}_PL{production_line}_GP{group_idx+1}_{'_'.join(task_skus)}"

            # 检查是否已经在运行中或已完成
            if task_id in running_task_ids or task_id in finished_task_ids:
                continue

            # 检查这个task是否已在当前组的完成列表中（仅对当前组检查）
            if group_idx == self.production_line_current_group[production_line]:
                if task_id in completed_task_ids:
                    continue

            # 检查库存 - 支持单梁和双梁任务
            all_skus_available = False
            if len(task_skus) == 1:
                # 单梁任务：只需检查是否有该SKU的库存
                sku = task_skus[0]
                total_qty = sum(self.inventory_manager.current_inventory[aisle].get(sku, 0)
                              for aisle in self.aisles)
                if total_qty > 0:
                    all_skus_available = True
            else:
                # 双梁任务：放宽为总量充足即可（由调度/移库决定具体配对位置）
                need = {}
                for sku in task_skus:
                    need[sku] = need.get(sku, 0) + 1
                enough = True
                debug_need = []
                for sku, req in need.items():
                    total_qty = sum(self.inventory_manager.current_inventory[aisle].get(sku, 0) for aisle in self.aisles)
                    debug_need.append((sku, req, total_qty))
                    if total_qty < req:
                        enough = False
                if not enough:
                    print(f"[DEBUG][generate_outbound] 库存不足，跳过 {task_id} 需求 {debug_need}")
                all_skus_available = enough

            if not all_skus_available:
                continue

            # 创建出库任务（新版字段）
            skus_payload = []
            for idx, sku in enumerate(task_skus):
                entry = {'skuId': sku, 'quantity': 1}
                for field, group_attrs in attrs_group.items():
                    task_attrs = []
                    if group_attrs and task_idx < len(group_attrs):
                        task_attrs = group_attrs[task_idx]
                    if task_attrs and idx < len(task_attrs):
                        entry[field] = task_attrs[idx]
                skus_payload.append(entry)
            task = TaskData(
                task_id=task_id,
                task_type=TASK_TYPE_OUTBOUND,
                task_name=task_id,
                production_line=production_line,
                skus=skus_payload
            )
            # 记录组索引，便于后续完成时准确推进 current_group
            task.group_idx = group_idx
            tasks.append(task)
            tasks_generated += 1

        return tasks

    def generate_outbound_tasks(self, max_tasks_per_line: int = 2,
                               running_task_ids: Optional[set[str]] = None,
                               finished_task_ids: Optional[set[str]] = None) -> List[TaskData]:
        """生成出库任务（基于生产计划）

        从生产计划中直接取task，每个task可以是单梁或双梁任务
        每次生成当前组的所有task（通常是2个task）
        避免生成正在运行的任务
        如果当前组的所有任务都在running或finished，则自动生成下一组

        Args:
            max_tasks_per_line: 每个产线最多生成的任务数（默认2，对应一组的2个task）
            running_task_ids: 正在运行的任务ID集合，用于避免重复生成
            finished_task_ids: 已完成的任务ID集合，用于判断是否可以进入下一组

        Returns:
            出库任务列表
        """
        if running_task_ids is None:
            running_task_ids = set()
        if finished_task_ids is None:
            finished_task_ids = set()

        outbound_tasks = []

        for production_line in range(1, self.num_production_lines + 1):
            # 获取当前组索引
            current_group_idx = self.production_line_current_group[production_line]
            # 如果该产线没有计划，跳过
            if production_line not in self.production_plan:
                continue
            if current_group_idx >= len(self.production_plan[production_line]):
                # 该产线所有组都已完成
                continue

            # 检查当前组的所有task是否都在running或finished中
            current_group = self.production_plan[production_line][current_group_idx]
            current_group_task_ids = []
            for task_idx, task_skus in enumerate(current_group):
                if len(task_skus) == 1:
                    # 单梁任务
                    task_id = f"{TASK_TYPE_OUTBOUND}_PL{production_line}_GP{current_group_idx+1}_{task_skus[0]}"
                else:
                    # 双梁任务
                    task_id = f"{TASK_TYPE_OUTBOUND}_PL{production_line}_GP{current_group_idx+1}_{'_'.join(task_skus)}"
                current_group_task_ids.append(task_id)

            all_tasks_busy = all(tid in running_task_ids or tid in finished_task_ids for tid in current_group_task_ids)

            if all_tasks_busy:
                #当前组任务都在处理中，生成下一组的任务
                next_group_idx = current_group_idx + 1
                if next_group_idx < len(self.production_plan[production_line]):
                    tasks = self._generate_tasks_for_group(
                        production_line, next_group_idx, max_tasks_per_line,
                        running_task_ids, finished_task_ids
                    )
                    outbound_tasks.extend(tasks)
            else:
                # 仅生成当前组的任务，跳过已在运行或已完成的任务
                tasks = self._generate_tasks_for_group(
                    production_line, current_group_idx, max_tasks_per_line,
                    running_task_ids, finished_task_ids
                )
                outbound_tasks.extend(tasks)

        return outbound_tasks

    # ========================================================================
    # 阶段处理：调度方案仿真与评分
    # ========================================================================
    def calculate_schedule_times(self, aisle_task_sequences: Dict[int, List[TaskData]]) -> Tuple[float, dict]:
        """事件驱动仿真：根据当前状态与调度器顺序推进事件队列，返回完工时间与详情
        不需要重复分配task，就是把self.event_queue和aisle_task_sequences执行完就行
        执行过程中可能需要添加task_complete与拥堵状态更新的event，直到event空结束

        Args:
            aisle_task_sequences (Dict[int, List[TaskData]]): 调度器为各巷道计算出的候选任务序列。

        Returns:
            Tuple[float, dict]: 由函数摘要中所列字段组成的多值结果。
        """
        start_time = self.current_time
        scheduled_task_ids = {
            task.task_id
            for task_sequence in (aisle_task_sequences or {}).values()
            for task in task_sequence
            if getattr(task, "task_id", None)
        }
        completed_before = len(self.completed_tasks)

        # 追踪每个巷道已经分配到第几个任务（索引）
        aisle_task_index = {aisle: 0 for aisle in aisle_task_sequences.keys()}
        tasks_num = sum(len(task_sequence) for task_sequence in aisle_task_sequences.values())

        def try_dispatch_tasks_from_sequences():
            """为当前空闲巷道派发其候选序列中的下一任务。

            函数读取 ``running_tasks`` 和移库预留判断巷道是否忙碌，读取
            ``aisle_task_index`` 取得每条巷道尚未派发的任务，并在任务可执行时
            创建后续事件、推进对应索引。返回本轮新创建的事件列表。
            """
            # 获取当前忙碌的巷道
            busy_aisles = set()
            for t in self.running_tasks.values():
                if t.assigned_aisle:
                    busy_aisles.add(t.assigned_aisle)
            # 移库占用也视为巷道忙碌
            for aisle in self.aisles:
                if self._is_aisle_relocation_busy(aisle, self.current_time):
                    busy_aisles.add(aisle)

            new_events = []
            for aisle, task_sequence in aisle_task_sequences.items():
                if aisle in busy_aisles:
                    # 巷道忙碌，跳过
                    continue

                # 获取当前应该分配的任务索引
                task_idx = aisle_task_index[aisle]
                if task_idx >= len(task_sequence):
                    # 该巷道的所有任务都已分配
                    continue

                # 取当前索引的任务
                task_info = task_sequence[task_idx]
                task_id = task_info.task_id
                task_type = task_info.task_type
                production_line = task_info.production_line
                # 仿真模式下也尊重入库预热期：在预热期间不要开始出库任务
                if self.current_time < getattr(self, 'inbound_only_seconds', 0.0) and task_type == TASK_TYPE_OUTBOUND:
                    try:
                        print(f"[warmup-core-sim] 在仿真评分期间当前时间 {self.current_time:.2f}s < {self.inbound_only_seconds:.2f}s，跳过出库任务 {task_id} 在巷道 {aisle}")
                    except Exception:
                        pass
                    continue

                # 如果是出库任务，检查是否可以开始
                if task_type == TASK_TYPE_OUTBOUND and production_line is not None:
                    if not self._is_task_relocation_ready(task_id, self.current_time):
                        continue
                    if task_id in {t.task_id for t in self.completed_tasks}:
                        continue
                    sku_ids = []
                    for s in (task_info.skus or []):
                        if isinstance(s, dict):
                            sid = s.get('skuId')
                        else:
                            sid = getattr(s, 'skuId', None)
                        if sid:
                            sku_ids.append(sid)
                    if len(sku_ids) == 2:
                        sku1, sku2 = sku_ids
                        attrs_map = self._task_attrs_map(task_info)
                        attrs1 = attrs_map.get(sku1, {})
                        attrs2 = attrs_map.get(sku2, {})
                        paired_position, sku1_positions, sku2_positions = self._check_sku_pairing_status(
                            sku1, sku2, attrs1, attrs2, task_id=task_id
                        )
                        if paired_position is None:
                            self._perform_relocation_if_needed(
                                task_id, production_line, sku1, sku2, sku1_positions, sku2_positions, attrs1, attrs2
                            )
                            continue
                    if self.check_blockage(aisle, production_line, current_time=self.current_time):
                        # 被拥堵阻塞，暂不开始
                        continue
                    # 检查产线组的顺序约束（前面的组是否完成）
                    if not self.can_start_outbound_task(task_id, production_line, getattr(task_info, "group_idx", None)):
                        # 前面的组还没完成，不能开始
                        continue

                # 生成任务记录
                task_info.task_record = self.generate_task_record(task_info, self.current_time)

                # 添加到运行任务
                self.running_tasks[task_id] = task_info
                # 标记巷道为忙碌
                busy_aisles.add(aisle)

                # 实时打印任务（含额外属性）
                sku_info = self._format_task_skus(task_info)
                pos_info = ""
                if getattr(task_info, "positions", None):
                    pos_ids = [p.get_position_id() for p in task_info.positions]
                    pos_info = f" positions={pos_ids}"
                print(
                    f"[dispatch] {task_type} task={task_id} aisle={aisle} pl={production_line}{sku_info}{pos_info}"
                )

                # 从等待队列中移除（如果存在）
                if task_type == TASK_TYPE_OUTBOUND:
                    self.pending_outbound_queue = [t for t in self.pending_outbound_queue if t.task_id != task_id]
                    # 出库：立即扣减库存
                    sku_ids_list = [s.get('skuId', None) for s in task_info.skus if 'skuId']
                    for idx, sku in enumerate(sku_ids_list):
                        if task_info.positions and len(task_info.positions) > idx:
                            pos = task_info.positions[idx]
                            try:
                                self.inventory_manager.remove_inventory(pos, sku, 1)
                            except Exception:
                                pass
                # 入库任务
                else:
                    # 确保只从该任务被分配到的巷道的等待队列中移除
                    self.pending_inbound_by_aisle[aisle] = [t for t in self.pending_inbound_by_aisle[aisle] if t.task_id != task_id]

                # 创建任务完成事件
                ev_id = f"{EVENT_TASK_COMPLETE}_{task_info.task_id}"
                delivery_time = task_info.task_record.get('delivery_time', self.current_time)
                ev = Event(float(delivery_time or self.current_time), ev_id, EVENT_TASK_COMPLETE, task_info)
                new_events.append(ev)

                # 更新该巷道的任务索引
                aisle_task_index[aisle] += 1

            return new_events

        # 初始分配：为空闲巷道分配第一个任务
        initial_events = try_dispatch_tasks_from_sequences()
        for ev in initial_events:
            heapq.heappush(self.event_queue, ev)

        last_time = self.current_time

        # 事件循环（仿真模式：不会重新调度，只执行预定的任务序列）
        while self.event_queue or self.running_tasks:
            if not self.event_queue:
                next_reloc_time = self._get_next_relocation_op_time()
                if next_reloc_time is not None:
                    self.current_time = max(self.current_time, next_reloc_time)
                    self._apply_relocation_ops(self.current_time)
                    continue
                # 如果没有事件但仍有运行任务，说明所有任务都完成了
                # 在仿真模式下不会重新派发任务
                break

            next_reloc_time = self._get_next_relocation_op_time()
            if next_reloc_time is not None and next_reloc_time < self.event_queue[0].time:
                self.current_time = max(self.current_time, next_reloc_time)
                self._apply_relocation_ops(self.current_time)
                continue

            ev = heapq.heappop(self.event_queue)
            self.current_time = max(self.current_time, ev.time)
            last_time = self.current_time

            # 处理事件，将返回的新事件添加到队列（仿真模式：不会调用decide_for_idle_aisles）
            new_events = self.on_event(ev, self.current_time, simulation_mode=True)
            for new_ev in new_events:
                heapq.heappush(self.event_queue, new_ev)

            # 事件处理后，尝试从aisle_task_sequences中分配新任务
            dispatch_events = try_dispatch_tasks_from_sequences()
            for new_ev in dispatch_events:
                heapq.heappush(self.event_queue, new_ev)

        # 统计结果
        aisle_schedules = {aisle: [] for aisle in self.aisles}
        aisle_completion_times = {aisle: 0.0 for aisle in self.aisles}
        production_line_times = {
            pl: {'delivery_time': 0.0, 'crane_finish_time': 0.0}
            for pl in range(1, self.num_production_lines + 1)
        }
        inbound_wait_time = 0.0

        newly_completed_tasks = self.completed_tasks[completed_before:]
        relevant_completed_tasks = [
            t for t in newly_completed_tasks
            if getattr(t, "task_id", None) in scheduled_task_ids
        ]

        for t in relevant_completed_tasks:
            rec = t.task_record or {}
            pl = t.production_line
            aisle = t.assigned_aisle
            # ``task_record`` 允许在中断任务上不含完整时间字段；统计时将缺失值
            # 视为本轮起点，避免不完整记录阻断整个方案评分。
            delivery_time = float(rec.get('delivery_time', start_time) or start_time)
            crane_finish_time = float(rec.get('crane_finish_time', 0.0) or 0.0)
            if aisle in aisle_schedules:
                aisle_schedules[aisle].append(rec)
                aisle_completion_times[aisle] = max(aisle_completion_times[aisle], delivery_time)
            if t.task_type == TASK_TYPE_OUTBOUND:
                if pl not in production_line_times:
                    production_line_times[pl] = {'delivery_time': 0.0, 'crane_finish_time': 0.0}
                production_line_times[pl]['delivery_time'] = max(
                    production_line_times[pl]['delivery_time'], delivery_time
                )
                production_line_times[pl]['crane_finish_time'] = max(
                    production_line_times[pl]['crane_finish_time'], crane_finish_time
                )
            elif t.task_type == TASK_TYPE_INBOUND:
                try:
                    st = rec.get('start_time', 0.0)
                    if st is not None:
                        inbound_wait_time += max(0.0, st - start_time)
                except Exception:
                    pass

        if relevant_completed_tasks:
            makespan = max(aisle_completion_times.values()) - start_time
        else:
            makespan = 0.0
        detailed_schedule = {
            'aisle_schedules': aisle_schedules,
            'production_line_times': production_line_times,
            'aisle_completion_times': aisle_completion_times,
            'inbound_wait_time': inbound_wait_time,
        }
        return makespan, detailed_schedule

    def get_current_balance(self) -> float:
        """获取当前库存均衡度

        Returns:
            float: 当前计算得到的时间、评分或比例数值。
        """
        return self.metrics_calculator.calculate_distribution_balance(
            self.inventory_manager.current_inventory
        )

    def calculate_comprehensive_score(self, makespan: float, detailed_schedule: dict,
                                     balance_before: float,
                                     start_time: float,
                                     makespan_weight: Optional[float] = None,
                                     balance_weight: Optional[float] = None,
                                     production_line_avg_time_weight: Optional[float] = None,
                                     production_line_balance_weight: Optional[float] = None,
                                     aisle_dispersion_weight: Optional[float] = None,
                                     inbound_wait_weight: Optional[float] = None) -> Tuple[float, dict]:
        """计算综合评分（越小越好）

        综合考虑：
        1. makespan（当前轮所派的任务完工时间）
        2. 库存均衡度变化（如果均衡度变差则惩罚）
        3. 产线平均完成时间（各产线完成最后一个任务的平均时间，越小越好）

        Args:
            makespan: 调度方案的makespan
            detailed_schedule: calculate_schedule_times返回的详细调度信息
            balance_before: 调度前的库存均衡度
            start_time: 本轮评分起始时刻，用于将产线完成时刻换算为持续时间。
            makespan_weight: 可选的本次覆盖权重；未传时读取 ``warehouse.json`` 的
                ``makespan_weight``。
            balance_weight: 可选的库存均衡惩罚覆盖权重；未传时读取配置。
            production_line_avg_time_weight: 可选的产线平均完成时间覆盖权重；未传时读取配置。
            production_line_balance_weight: 可选的产线推进均衡覆盖权重；未传时读取配置。
            aisle_dispersion_weight: 可选的巷道负载分散覆盖权重；未传时读取配置。
            inbound_wait_weight: 可选的入库等待时间覆盖权重；未传时读取配置。

        Returns:
            Tuple[float, dict]: 总评分及其组成项、实际生效权重和原始指标明细。
        """
        # 所有默认评分权重都来自 WarehouseCore 初始化时加载的 warehouse.json。
        # 显式传入非 None 参数只用于单次试验，不会回写全局配置。
        makespan_weight = self.makespan_weight if makespan_weight is None else makespan_weight
        balance_weight = self.balance_weight if balance_weight is None else balance_weight
        production_line_avg_time_weight = (
            self.production_line_avg_time_weight
            if production_line_avg_time_weight is None
            else production_line_avg_time_weight
        )
        production_line_balance_weight = (
            self.production_line_balance_weight
            if production_line_balance_weight is None
            else production_line_balance_weight
        )
        aisle_dispersion_weight = (
            self.aisle_dispersion_weight
            if aisle_dispersion_weight is None
            else aisle_dispersion_weight
        )
        inbound_wait_weight = self.inbound_wait_weight if inbound_wait_weight is None else inbound_wait_weight

        # 1. 当前轮所派的任务完工时间部分
        makespan_score = makespan * makespan_weight

        # 2. 库存均衡度变化（均衡度变差则惩罚）
        balance_after = self.get_current_balance()
        # 正值表示变好，负值表示变差。
        balance_change = balance_after - balance_before
        # 如果变差，则惩罚；如果变好，不奖励（保持 0）；放大到可比尺度。
        balance_penalty = max(0, -balance_change) * balance_weight * 1000

        # 3. 产线平均完成时间
        production_line_times = detailed_schedule['production_line_times']

        pl_completion_times = [
            production_line_times[pl]['crane_finish_time'] - start_time
            for pl in range(1, self.num_production_lines + 1)
            if production_line_times[pl]['crane_finish_time'] > 0
        ]
        avg_production_line_time = (
            sum(pl_completion_times) / len(pl_completion_times)
            if pl_completion_times else 0.0
        )
        production_line_avg_score = avg_production_line_time * production_line_avg_time_weight if pl_completion_times else 0.0

        # 4. 产线进度平衡（按比例，忽略计划为0的产线）
        line_progress = []
        line_totals = []
        for pl in range(1, self.num_production_lines + 1):
            total_groups = len(self.production_plan.get(pl, []))
            if total_groups <= 0:
                continue
            finished_tasks = 0
            for aisle, records in detailed_schedule['aisle_schedules'].items():
                for rec in records:
                    task_obj = rec.get('task') if isinstance(rec, dict) else None
                    if task_obj is None:
                        continue
                    if getattr(task_obj, "production_line", None) == pl and getattr(task_obj, "task_type", None) == TASK_TYPE_OUTBOUND:
                        finished_tasks += 1
            line_progress.append(finished_tasks / total_groups)
            line_totals.append(total_groups)
        balance_variance = 0.0
        # 空或单巷道调度时仍供评分明细安全读取。
        mean_ct = 0.0
        if len(line_progress) > 1:
            mean_prog = sum(line_progress) / len(line_progress)
            balance_variance = sum((p - mean_prog) ** 2 for p in line_progress) / len(line_progress)
        production_line_balance_penalty = balance_variance * production_line_balance_weight

        # 5. 巷道分散度（巷道任务数的方差）
        aisle_counts = [len(tasks) for tasks in detailed_schedule['aisle_schedules'].values() if tasks]
        aisle_dispersion_penalty = 0.0
        if len(aisle_counts) > 1:
            mean_ct = sum(aisle_counts) / len(aisle_counts)
            aisle_dispersion_penalty = (sum((c - mean_ct) ** 2 for c in aisle_counts) / len(aisle_counts)) * aisle_dispersion_weight

        # 6. 入库等待时间惩罚（入库等待越久越差）
        inbound_wait = detailed_schedule.get('inbound_wait_time', 0.0) or 0.0
        inbound_wait_penalty = inbound_wait * inbound_wait_weight

        # 综合score
        total_score = (makespan_score + balance_penalty + production_line_avg_score +
                       production_line_balance_penalty + aisle_dispersion_penalty +
                       inbound_wait_penalty)

        details = {
            'score_weights': {
                'makespan_weight': makespan_weight,
                'balance_weight': balance_weight,
                'production_line_avg_time_weight': production_line_avg_time_weight,
                'production_line_balance_weight': production_line_balance_weight,
                'aisle_dispersion_weight': aisle_dispersion_weight,
                'inbound_wait_weight': inbound_wait_weight,
            },
            'total_score': total_score,
            'makespan_score': makespan_score,
            'balance_penalty': balance_penalty,
            'production_line_avg_score': production_line_avg_score,
            'balance_before': balance_before,
            'balance_after': balance_after,
            'balance_change': balance_change,
            'balance_raw_penalty': max(0, -balance_change) * 1000,
            'avg_production_line_time': avg_production_line_time,
            'production_line_balance_penalty': production_line_balance_penalty,
            'production_line_balance_raw': balance_variance,
            'aisle_dispersion_penalty': aisle_dispersion_penalty,
            'aisle_dispersion_raw': (
                sum((c - mean_ct) ** 2 for c in aisle_counts) / len(aisle_counts)
                if len(aisle_counts) > 1 else 0.0
            ),
            'aisle_task_counts': aisle_counts,
            'line_progress': line_progress,
            'line_totals': line_totals,
            'inbound_wait_time': inbound_wait,
            'inbound_wait_penalty': inbound_wait_penalty,
        }

        return total_score, details

    def generate_task_record(self, task_info: TaskData, current_time: float) -> AisleScheduleRecord:
        """生成任务的时间记录

        Args:
            task_info: 任务信息（TaskData对象）
            current_time: 当前时间

        Returns:
            AisleScheduleRecord格式的字典，包含：
            - start_time: 任务开始时间
            - duration: 任务持续时间
            - delivery_time: 货物到达出库口/入库完成的时间
            - un_congested_time: 出库拥堵解除时间（若适用）
            - crane_finish_time: 磁力吊及拥堵完全结束时间（若适用）
        """
        aisle = int(task_info.assigned_aisle or 0)
        task_type = task_info.task_type

        # 获取当前巷道位置
        current_position = self.current_position_by_aisle.get(aisle)

        # 使用时间估算器计算任务持续时间
        if task_type == TASK_TYPE_OUTBOUND:
            # 出库任务
            duration = self.time_estimator.estimate_outbound_time(
                source_position=task_info.positions,
                skus=task_info.skus,
                production_line=task_info.production_line,
                current_position=current_position
            )
        else:
            # 入库任务
            duration = self.time_estimator.estimate_inbound_time(
                target_position=task_info.positions,
                skus=task_info.skus,
                in_line=getattr(task_info, "in_line", 1),
                current_position=current_position
            )
        # 若巷道正处于移库占用，则推迟到占用结束后再开始
        relocation_delay = self._get_relocation_delay_until_free(aisle, current_time)
        start_time = current_time + relocation_delay
        delivery_time = start_time + duration

        # 初始化记录
        record = {
            'start_time': start_time,
            'duration': duration,
            'delivery_time': delivery_time,
        }

        # 如果是出库任务，需要考虑拥堵和磁力吊时间
        if task_type == TASK_TYPE_OUTBOUND:
            # 拥堵时间
            un_congested_time = delivery_time + self.outbound_congestion_time
            record['un_congested_time'] = un_congested_time

            # 磁力吊时间（如果启用）
            if self.use_magnetic_crane:
                crane_finish_time = un_congested_time + self.magnetic_crane_time
                record['crane_finish_time'] = crane_finish_time
            else:
                record['crane_finish_time'] = un_congested_time

        return AisleScheduleRecord(**record)

    def get_sol_score(self, aisle_task_sequences: Dict[int, List[TaskData]],
                      makespan_weight: Optional[float] = None,
                      balance_weight: Optional[float] = None,
                      production_line_avg_time_weight: Optional[float] = None,
                      production_line_balance_weight: Optional[float] = None,
                      aisle_dispersion_weight: Optional[float] = None,
                      inbound_wait_weight: Optional[float] = None) -> Tuple[float, dict]:
        """计算给定调度方案的评分（不修改任何内部状态）

        该函数通过深拷贝所有相关状态，在副本上进行仿真，确保不修改原始对象的任何属性。

        Args:
            aisle_task_sequences: 巷道任务序列 {aisle: [task_info, ...]}
            makespan_weight: makespan权重（越小越好）
            balance_weight: 均衡度变化权重（库存均衡度变差的惩罚）
            production_line_avg_time_weight: 产线平均完成时间权重（越小越好）

        Returns:
            (综合score, 详细信息字典)

        详细信息字典包含：
            - total_score: 综合评分（越小越好）
            - makespan: 完工时间
            - makespan_score: makespan评分部分
            - balance_penalty: 库存均衡度变差惩罚
            - production_line_avg_score: 产线平均完成时间评分
            - balance_before: 调度前的库存均衡度
            - balance_after: 调度后的库存均衡度
            - balance_change: 均衡度变化
            - avg_production_line_time: 产线平均完成时间
            - aisle_schedules: 各巷道详细调度
            - production_line_times: 各产线时间统计
            - aisle_completion_times: 各巷道完成时间
        """
        makespan_weight = self.makespan_weight if makespan_weight is None else makespan_weight
        balance_weight = self.balance_weight if balance_weight is None else balance_weight
        production_line_avg_time_weight = (
            self.production_line_avg_time_weight
            if production_line_avg_time_weight is None
            else production_line_avg_time_weight
        )
        production_line_balance_weight = (
            self.production_line_balance_weight
            if production_line_balance_weight is None
            else production_line_balance_weight
        )
        aisle_dispersion_weight = (
            self.aisle_dispersion_weight
            if aisle_dispersion_weight is None
            else aisle_dispersion_weight
        )
        inbound_wait_weight = self.inbound_wait_weight if inbound_wait_weight is None else inbound_wait_weight

        # 为了保证评分期间不会修改主对象状态，构造一个完全独立的仿真副本并在其上执行评分。
        # 1) 构造副本并注入当前状态快照；
        # 2) 将传入的 aisle_task_sequences 中的 TaskData.positions 映射到副本的 InventoryPosition；
        # 3) 在副本上运行 calculate_schedule_times 和 calculate_comprehensive_score，返回结果。

        # 构造副本并恢复状态（传入 aisle_task_sequences 以便只对涉及的位置深拷贝，节省时间和空间）
        sim_core = self.clone_for_simulation(aisle_task_sequences=aisle_task_sequences)

        # 记录调度前的库存均衡度与起始时间（基于副本）
        balance_before = sim_core.get_current_balance()
        start_time = sim_core.current_time

        # 将 aisle_task_sequences 映射到副本对应的 InventoryPosition（避免引用回主对象）
        mapped_sequences: Dict[int, List[TaskData]] = {}
        for aisle, seq in (aisle_task_sequences or {}).items():
            mapped_seq: List[TaskData] = []
            for task in seq:
                tcopy: TaskData = deepcopy(task)
                new_positions = []
                for p in getattr(tcopy, 'positions', []) or []:
                    try:
                        pid = p.get_position_id()
                        new_p = sim_core.inventory_manager.position_map.get(pid)
                        if new_p is not None:
                            new_positions.append(new_p)
                        else:
                            # 无法在副本中找到对应位置时，保留原引用以避免失败（但这通常不应发生）
                            new_positions.append(p)
                    except Exception:
                        new_positions.append(p)
                tcopy.positions = new_positions
                # 确保positions列表长度与原始任务一致
                if len(new_positions) != len(getattr(task, 'positions', [])):
                    print(f"[DEBUG] Positions长度不匹配: 原始={len(getattr(task, 'positions', []))}, 复制后={len(new_positions)}")
                mapped_seq.append(tcopy)
            mapped_sequences[aisle] = mapped_seq

        # 在副本上运行仿真评分（副本内的状态会被修改，但不会影响主对象）
        import builtins
        orig_print = builtins.print
        try:
            # 在评分期间临时屏蔽所有 print 输出，避免副本中大量打印干扰主流程输出
            builtins.print = lambda *a, **k: None
            makespan, detailed_schedule = sim_core.calculate_schedule_times(mapped_sequences)

            # 计算综合评分（在副本上计算，使用副本的 balance_before/start_time）
            total_score, score_details = sim_core.calculate_comprehensive_score(
                    makespan,
                    detailed_schedule,
                    start_time=start_time,
                    balance_before=balance_before,
                    makespan_weight=makespan_weight,
                    balance_weight=balance_weight,
                    production_line_avg_time_weight=production_line_avg_time_weight,
                    production_line_balance_weight=production_line_balance_weight,
                    aisle_dispersion_weight=aisle_dispersion_weight,
                    inbound_wait_weight=inbound_wait_weight
                )
        finally:
            # 恢复内建 print
            builtins.print = orig_print

        result_details = {
            **score_details,
            'makespan': makespan,
            'aisle_schedules': detailed_schedule['aisle_schedules'],
            'production_line_times': detailed_schedule['production_line_times'],
            'aisle_completion_times': detailed_schedule['aisle_completion_times']
        }

        return total_score, result_details

    @staticmethod
    def format_score_breakdown(
        score_details: dict,
        extra_terms: Optional[List[Tuple[str, float]]] = None,
        extra_penalty: Optional[float] = None,
        extra_penalty_label: str = "extra_penalty",
    ) -> List[str]:
        """Format score details for readable optimization logs.

        Args:
            score_details (dict): 调度评分的基础指标及其原始数值。
            extra_terms (Optional[List[Tuple[str, float]]]): 需要附加到评分明细中的 ``(名称, 数值)`` 项。
            extra_penalty (Optional[float]): 需要单独计入总分的惩罚数值。
            extra_penalty_label (str): 附加惩罚在评分明细中的显示名称。

        Returns:
            List[str]: 符合当前处理条件的结果列表或集合。
        """
        if not score_details:
            return ["  [score] no details"]

        weights = score_details.get('score_weights', {})
        total_score = float(score_details.get('total_score', 0.0))
        extra_terms = list(extra_terms or [])
        if extra_penalty:
            extra_terms.append((extra_penalty_label, extra_penalty))
        final_score = total_score + sum(value for _, value in extra_terms)

        def _f(value: Any) -> str:
            """把评分明细值统一转换为日志可读文本。

            数值保留四位小数，非数值（例如缺失标记）保持原始字符串形式，供
            下方评分公式日志拼接使用。
            """
            if isinstance(value, (int, float)):
                return f"{float(value):.4f}"
            return str(value)

        parts = [
            f"{_f(weights.get('makespan_weight', 0.0))}*{_f(score_details.get('makespan'))}={_f(score_details.get('makespan_score', 0.0))}(makespan)",
            f"{_f(weights.get('balance_weight', 0.0))}*{_f(score_details.get('balance_raw_penalty', 0.0))}={_f(score_details.get('balance_penalty', 0.0))}(balance_penalty)",
            f"{_f(weights.get('production_line_avg_time_weight', 0.0))}*{_f(score_details.get('avg_production_line_time', 0.0))}={_f(score_details.get('production_line_avg_score', 0.0))}(production_line_avg)",
            f"{_f(weights.get('production_line_balance_weight', 0.0))}*{_f(score_details.get('production_line_balance_raw', 0.0))}={_f(score_details.get('production_line_balance_penalty', 0.0))}(production_line_balance)",
            f"{_f(weights.get('aisle_dispersion_weight', 0.0))}*{_f(score_details.get('aisle_dispersion_raw', 0.0))}={_f(score_details.get('aisle_dispersion_penalty', 0.0))}(aisle_dispersion)",
            f"{_f(weights.get('inbound_wait_weight', 0.0))}*{_f(score_details.get('inbound_wait_time', 0.0))}={_f(score_details.get('inbound_wait_penalty', 0.0))}(inbound_wait)",
        ]
        for label, value in extra_terms:
            if value:
                parts.append(f"{label}={_f(value)}({label})")

        formula = " + ".join(parts)
        return [f"  [score] total={_f(final_score)} = {formula}"]

    # ========================================================================
    # 辅助函数：仿真快照、库存统计与状态恢复
    # ========================================================================
    def _save_simulation_state(self, affected_position_ids: Optional[set[str]] = None) -> dict:
        """保存优化候选试算前的完整运行态快照。

        快照包含事件队列、pending/running 队列、生产组状态和深拷贝货位列表。
        ``affected_position_ids`` 保留为选择性复制扩展入口；当前实现为保证候选
        评分互不污染，仍复制全部 ``inventory_positions``。配对的恢复方法会重建
        位置和 SKU 索引，不能只恢复 ``current_inventory`` 汇总值。

        Args:
            affected_position_ids (set): 快照恢复后需要重新建立索引或预留状态的货位 ID 集合。

        Returns:
            dict: 按函数定义字段组织的结果映射。
        """
        """保存仿真状态的快照（用于get_sol_score）

        Args:
            affected_position_ids: 可选，需要深拷贝的位置ID集合。
                                   如果提供，只对这些位置深拷贝，其他位置浅拷贝以节省时间和空间。
                                   如果为None，则对所有位置深拷贝（保持原有行为）。
        """
        # 库存位置：根据 affected_position_ids 进行选择性深拷贝
        # inventory_positions_copy = None
        # if affected_position_ids is not None:
        #     # 只对涉及的位置深拷贝，其他位置浅拷贝
        #     inventory_positions_copy = self.inventory_manager.inventory_positions.copy()
        #     for idx, p in enumerate(self.inventory_manager.inventory_positions):
        #         pid = p.get_position_id()
        #         if pid in affected_position_ids:
        #             inventory_positions_copy[idx] = deepcopy(p)
        # else:
        #     # 全部深拷贝（原有行为）
        inventory_positions_copy = deepcopy(self.inventory_manager.inventory_positions)

        return {
            # 时间与事件
            'current_time': self.current_time,
            'event_queue': deepcopy(self.event_queue),

            # 任务管理
            'running_tasks': deepcopy(self.running_tasks),
            'completed_tasks': deepcopy(self.completed_tasks),
            'pending_inbound_by_aisle': deepcopy(self.pending_inbound_by_aisle),
            'pending_outbound_queue': deepcopy(self.pending_outbound_queue),
            'task_status': deepcopy(self.task_status),

            # 巷道状态
            'crane_available_times': deepcopy(self.crane_available_times),
            'blockage_status': deepcopy(self.blockage_status),
            'current_position_by_aisle': deepcopy(self.current_position_by_aisle),
            'relocation_task_ids': deepcopy(self.relocation_task_ids),

            # 生产计划相关
            'production_plan': deepcopy(self.production_plan),
            'production_line_current_group': deepcopy(self.production_line_current_group),
            'production_line_completed_tasks': deepcopy(self.production_line_completed_tasks),
            'production_line_group_completion_times': deepcopy(self.production_line_group_completion_times),

            # 统计数据
            'total_rounds': self.total_rounds,
            'task_id_counter': deepcopy(self.task_id_counter),

            # 库存状态
            'inventory_state': deepcopy(self.inventory_manager.current_inventory),
            'inventory_positions': inventory_positions_copy,

            # 其他运行时属性（如果存在）
            'inbound_only_seconds': getattr(self, 'inbound_only_seconds', 0.0),
        }

    def _restore_simulation_state(self, saved_state: dict):
        """恢复评分预览前保存的运行态和库存快照。

        该方法由优化调度器的候选方案试算使用。替换 ``inventory_positions`` 后，
        必须重建 ``position_map`` 和 ``sku_position_index``，否则任务携带的位置
        主键会指向旧对象，导致库存写入或出库匹配读取错误的状态。

        Args:
            saved_state (dict): 由 ``_save_simulation_state`` 创建的完整运行态快照。

        Returns:
            None: 用快照替换运行时状态，并重建货位主键和 SKU 索引。
        """
        # 时间与事件
        self.current_time = saved_state['current_time']
        self.event_queue = saved_state['event_queue']

        # 任务管理
        self.running_tasks = saved_state['running_tasks']
        self.completed_tasks = saved_state['completed_tasks']
        self.pending_inbound_by_aisle = saved_state['pending_inbound_by_aisle']
        self.pending_outbound_queue = saved_state['pending_outbound_queue']
        self.task_status = saved_state['task_status']

        # 巷道状态
        self.crane_available_times = saved_state['crane_available_times']
        self.blockage_status = saved_state['blockage_status']
        self.current_position_by_aisle = saved_state['current_position_by_aisle']
        self.relocation_task_ids = saved_state.get('relocation_task_ids', set())

        # 生产计划相关
        self.production_plan = saved_state['production_plan']
        self.production_line_current_group = saved_state['production_line_current_group']
        self.production_line_completed_tasks = saved_state['production_line_completed_tasks']
        self.production_line_group_completion_times = saved_state['production_line_group_completion_times']

        # 统计数据
        self.total_rounds = saved_state['total_rounds']
        self.task_id_counter = saved_state['task_id_counter']

        # 库存状态
        # 先恢复权威库存和位置对象，再恢复两个派生索引；不能复用快照前的索引。
        self.inventory_manager.current_inventory = saved_state['inventory_state']
        self.inventory_manager.inventory_positions = saved_state['inventory_positions']

        # 其他运行时属性
        if 'inbound_only_seconds' in saved_state:
            self.inbound_only_seconds = saved_state['inbound_only_seconds']

        # 当我们替换了 inventory_positions 列表时，需要重建 position_map 与 sku_position_index
        try:
            pm = {}
            sku_index = {sku: [] for sku in self.inventory_manager.sku_types}
            for p in self.inventory_manager.inventory_positions:
                # position_map 保证任务主键取回当前快照对象；sku_position_index
                # 供配对、出库和库存查询按 SKU 快速定位货位。
                pid = p.get_position_id()
                pm[pid] = p
                if p.is_double_layer:
                    upper_sku = p.upper_sku
                    lower_sku = p.lower_sku
                    if upper_sku:
                        sku_index.setdefault(upper_sku, []).append(p)
                    if lower_sku:
                        sku_index.setdefault(lower_sku, []).append(p)
                else:
                    sku = p.sku
                    if sku:
                        sku_index.setdefault(sku, []).append(p)
            self.inventory_manager.position_map = pm
            self.inventory_manager.sku_position_index = sku_index
        except Exception:
            pass

    def clone_for_simulation(self, aisle_task_sequences: Optional[Dict[int, List[TaskData]]] = None) -> 'WarehouseCore':
        """为仿真评分构造一个独立的 WarehouseCore 副本并注入当前状态。

        返回的副本在内存上与主对象完全隔离，后续在副本上运行的任何修改都不会影响主对象。

        Args:
            aisle_task_sequences: 可选，巷道任务序列。如果提供，只对涉及的库存位置进行深拷贝以节省时间和空间。


        Returns:
            'WarehouseCore': 当前处理流程产生的结果，具体结构见函数摘要。
        """
        # 使用与当前对象相同的构造参数创建新实例（尽量保持配置一致）
        try:
            initial_inv_ratio = getattr(self.inventory_manager, 'initial_inventory_ratio', 0.3)
        except Exception:
            initial_inv_ratio = 0.3

        sim_core = WarehouseCore(
            num_aisles=self.num_aisles,
            num_production_lines=self.num_production_lines,
            initial_inventory_ratio=initial_inv_ratio,
            random_seed=None,
            aisle_production_line_mapping=deepcopy(self.aisle_production_line_mapping),
            scheduler_type=self.scheduler_type,
            inbound_aisle_strategy=None,
            inbound_allocation_strategy=None,
            initial_inventory_count=self.initial_inventory_count,
            config_path=self.config_path,
            sku_config_path=self.sku_config_path,
        )

        # 复制仿真参数（如果被修改过）
        sim_core.blockage_time = self.blockage_time
        sim_core.magnetic_crane_time = self.magnetic_crane_time
        sim_core.transport_delay_s = self.transport_delay_s

        # 从 aisle_task_sequences 中提取涉及的 position_ids（用于选择性深拷贝）
        affected_position_ids = None
        if aisle_task_sequences is not None:
            affected_position_ids = set()
            for aisle, seq in aisle_task_sequences.items():
                for task in seq:
                    for p in getattr(task, 'positions', []) or []:
                        try:
                            affected_position_ids.add(p.get_position_id())
                        except Exception:
                            pass

        # 将当前状态深拷贝并注入到副本（使用已有的 save/restore 工具）
        saved_state = self._save_simulation_state(affected_position_ids=affected_position_ids)

        sim_core._restore_simulation_state(saved_state)


        # 复制 allocator 引用（策略对象直接引用，不深拷贝）
        sim_core.inbound_aisle_allocator = self.inbound_aisle_allocator
        sim_core.inbound_position_allocator = self.inbound_position_allocator

        # 重新绑定/初始化副本的 scheduler，确保其内部引用指向 sim_core
        try:
            scheduler_class = get_scheduler(self.scheduler_type)
            sim_core.scheduler = scheduler_class(sim_core)
            sim_core.scheduler.position_allocator = sim_core.inbound_position_allocator
        except Exception:
            # 如果无法重新初始化调度器则保持现状（副本已有一个调度器实例）
            pass

        return sim_core

    def _get_total_beams(self) -> int:
        """获取仓库中梁的总数

        Returns:
            int: 仓库中梁的总数

        """
        total_beams = 0
        # 遍历所有巷道和SKU，统计总数
        for aisle in self.inventory_manager.aisles:
            for sku in self.inventory_manager.sku_types:
                total_beams += self.inventory_manager.current_inventory[aisle].get(sku, 0)
        return total_beams

    def _get_beam_details(self) -> dict:
        """获取仓库中梁的详细信息，包括每种SKU及其数量（数量为0的不包含在内）

        Returns:
            dict: 包含每种SKU及其数量的字典

        """
        beam_details = {}
        try:
            # 使用current_inventory进行统计
            for aisle in self.inventory_manager.aisles:
                for sku in self.inventory_manager.sku_types:
                    quantity = self.inventory_manager.current_inventory[aisle].get(sku, 0)
                    if quantity > 0:
                        if sku in beam_details:
                            beam_details[sku] += quantity
                        else:
                            beam_details[sku] = quantity

            # 计算总梁数
            total_beams = sum(beam_details.values())
            beam_details['total_beams'] = total_beams
        except Exception as e:
            print(f"[DEBUG] 统计梁详情时出错: {e}")
            # 出错时回退到遍历货位的方式
            try:
                for position in self.inventory_manager.inventory_positions:
                    if position.is_double_layer:
                        if position.upper_sku and position.upper_quantity > 0:
                            beam_details[position.upper_sku] = beam_details.get(position.upper_sku, 0) + position.upper_quantity
                        if position.lower_sku and position.lower_quantity > 0:
                            beam_details[position.lower_sku] = beam_details.get(position.lower_sku, 0) + position.lower_quantity
                    else:
                        if position.sku and position.quantity > 0:
                            beam_details[position.sku] = beam_details.get(position.sku, 0) + position.quantity

                # 计算总梁数
                total_beams = sum(beam_details.values())
                beam_details['total_beams'] = total_beams
            except Exception as e2:
                print(f"[DEBUG] 回退统计方法也出错: {e2}")

        return beam_details

    def get_relocation_count(self) -> int:
        """获取累计移库数

        Returns:
            int: 当前计算得到的数量、索引、时间或编号。
        """
        return self._relocation_count

    # ========================================================================
    # 阶段处理：移库决策、资源预留与操作执行
    # ========================================================================
    def check_and_relocate_inventory(self) -> List[dict]:
        """检查当前组尚未执行的出库任务是否需要移库，并在必要时执行移库操作

        检查逻辑：
        1. 获取当前组尚未执行的出库任务（双梁任务）
        2. 检查事件队列中是否有与该出库任务相关的入库任务
        3. 如果没有相关入库任务，且库存中存在以下情况之一：
           - 有两个SKU但没有配对好（分别在不同位置）
           则需要进行移库操作

        Returns:
            执行移库操作的记录列表，每项包含：
            - task_id: 出库任务ID
            - task_skus: 出库任务的SKU列表
            - production_line: 产线号
            - reason: 移库原因 ('unpaired' 未配对 / 'missing_sku' 缺少SKU)
            - relocation_details: 移库操作详情

        """
        relocation_records = []
        completed_task_ids_all = {t.task_id for t in self.completed_tasks}

        for production_line in range(1, self.num_production_lines + 1):
            current_group_idx = self.production_line_current_group.get(production_line, 0)
            if current_group_idx >= len(self.production_plan.get(production_line, [])):
                continue

            current_group = self.production_plan[production_line][current_group_idx]
            completed_task_ids = self.production_line_completed_tasks[production_line]
            running_task_ids = set(self.running_tasks.keys())

            # 遍历当前组的每个task
            for task_idx, task_skus in enumerate(current_group):
                # 生成任务ID
                if len(task_skus) == 1:
                    task_id = f"{TASK_TYPE_OUTBOUND}_PL{production_line}_GP{current_group_idx+1}_{task_skus[0]}"
                else:
                    task_id = f"{TASK_TYPE_OUTBOUND}_PL{production_line}_GP{current_group_idx+1}_{'_'.join(task_skus)}"
                group_label = current_group_idx + 1
                def _log_relocation_skip(reason: str) -> None:
                    """记录当前生产组任务未进入移库扫描的具体原因。

                    日志包含产线、公开组号和任务 ID，用于区分已完成、运行中、
                    库存不足等跳过原因；不改变任务、库存或移库预留状态。
                    """
                    print(
                        f"[DEBUG][relocation-scan] skip pl={production_line} gp={group_label} "
                        f"task={task_id} reason={reason}"
                    )

                # 跳过已完成或正在运行的任务
                if task_id in completed_task_ids or task_id in running_task_ids:
                    if task_id in completed_task_ids:
                        _log_relocation_skip("already_completed_in_line")
                    else:
                        _log_relocation_skip("currently_running")
                    continue
                if task_id in completed_task_ids_all:
                    _log_relocation_skip("already_completed_global")
                    continue
                if self._is_task_pending_completion(task_id):
                    _log_relocation_skip("pending_task_complete_event")
                    continue
                if task_id in self.relocation_task_ids:
                    _log_relocation_skip("relocation_already_scheduled")
                    continue

                # 只处理双梁任务（需要两个SKU配对出库）
                if len(task_skus) != 2:
                    _log_relocation_skip("not_double_sku_task")
                    continue

                sku1, sku2 = task_skus[0], task_skus[1]
                attrs_list = self._build_attrs_for_task_from_plan(
                    production_line, current_group_idx, task_idx, task_skus
                )
                attrs1 = attrs_list[0] if len(attrs_list) > 0 else {}
                attrs2 = attrs_list[1] if len(attrs_list) > 1 else {}

                # 检查事件队列中是否有与该出库任务相关的入库任务
                has_related_inbound = self._has_related_inbound_in_queue(sku1, sku2)
                if has_related_inbound:
                    _log_relocation_skip(f"related_inbound_in_queue skus={sku1},{sku2}")
                    # 有相关入库任务，不需要移库
                    continue

                # 检查库存配对情况
                paired_position, sku1_positions, sku2_positions = self._check_sku_pairing_status(
                    sku1, sku2, attrs1, attrs2, task_id=task_id
                )

                if paired_position is not None:
                    _log_relocation_skip(
                        f"already_paired position={getattr(paired_position, 'position_id', None)}"
                    )
                    # 已有配对好的货位，不需要移库
                    continue

                # 判断移库原因和执行移库
                relocation_result = self._perform_relocation_if_needed(
                    task_id, production_line, sku1, sku2, sku1_positions, sku2_positions, attrs1, attrs2
                )

                if relocation_result is not None:
                    if relocation_result.get("relocation_details", {}).get("operations"):
                        print(
                            f"[DEBUG][relocation-scan] schedule pl={production_line} gp={group_label} "
                            f"task={task_id} reason={relocation_result.get('reason')}"
                        )
                        self.relocation_task_ids.add(task_id)
                    else:
                        print(
                            f"[DEBUG][relocation-scan] no-op pl={production_line} gp={group_label} "
                            f"task={task_id} result={relocation_result}"
                        )
                    relocation_records.append(relocation_result)
                else:
                    _log_relocation_skip(
                        f"perform_relocation_returned_none sku1_pos={len(sku1_positions)} sku2_pos={len(sku2_positions)}"
                    )

        return relocation_records

    def _has_related_inbound_in_queue(self, sku1: str, sku2: str) -> bool:
        """检查事件队列中是否有与指定SKU相关的入库任务

        Args:
            sku1: 第一个SKU
            sku2: 第二个SKU

        Returns:
            是否存在相关入库任务
        """
        for ev in self.event_queue:
            if ev.event_type in [EVENT_INBOUND_UNASSIGNED, EVENT_INBOUND_ARRIVAL_AT_AISLE]:
                if ev.task and ev.task.skus:
                    inbound_skus = [s.get('skuId') for s in ev.task.skus]
                    if sku1 in inbound_skus or sku2 in inbound_skus:
                        return True
        return False

    def _is_task_pending_completion(self, task_id: str) -> bool:
        """检查该出库任务是否已经进入 task_complete 事件队列但尚未拥堵清除。

        Args:
            task_id (str): 任务唯一标识，用于定位队列、冻结位置或反馈对象。

        Returns:
            bool: 处理成功、条件成立或校验通过时为 ``True``，否则为 ``False``。
        """
        if not task_id:
            return False
        for ev in self.event_queue:
            if ev.event_type == EVENT_TASK_COMPLETE and getattr(ev.task, "task_id", None) == task_id:
                return True
        return False

    def _build_attrs_for_task_from_plan(self, production_line: int, group_idx: int, task_idx: int, task_skus: List[str]) -> List[Dict[str, Any]]:
        """从生产计划属性中构造每个SKU的属性字典列表（按sku顺序）。

        Args:
            production_line (int): 任务所属生产线编号。
            group_idx (int): 生产组从零开始的内部索引。
            task_idx (int): 任务在生产组中的从零开始下标。
            task_skus (List[str]): 该生产组任务要求的 SKU 序列。

        Returns:
            List[Dict[str, Any]]: 符合当前处理条件的结果列表或集合。
        """
        attrs_per_sku = [{} for _ in task_skus]
        if not self.match_fields:
            return attrs_per_sku
        for field, by_line in (self.production_plan_attrs or {}).items():
            groups = by_line.get(production_line, [])
            if group_idx >= len(groups):
                continue
            tasks = groups[group_idx]
            if task_idx >= len(tasks):
                continue
            values = tasks[task_idx] or []
            for i in range(min(len(values), len(task_skus))):
                attrs_per_sku[i][field] = values[i]
        return attrs_per_sku

    def _check_sku_pairing_status(self, sku1: str, sku2: str,
                                  attrs1: Optional[Dict[str, Any]] = None,
                                  attrs2: Optional[Dict[str, Any]] = None,
                                  task_id: Optional[str] = None) -> Tuple[Optional[InventoryPosition], List[Tuple[str, InventoryPosition]], List[Tuple[str, InventoryPosition]]]:
        """检查两个SKU的配对状态

        Args:
            sku1: 第一个SKU
            sku2: 第二个SKU

        Returns:
            (paired_position, sku1_positions, sku2_positions)
            - paired_position: 已配对好的货位（如果存在），否则为None
            - sku1_positions: sku1所在位置列表，每项为 (层位, 货位对象)
            - sku2_positions: sku2所在位置列表，每项为 (层位, 货位对象)
        """

        paired_position = None
        sku1_positions = []
        sku2_positions = []

        match_fields = self.match_fields or []
        for pos in self.inventory_manager.inventory_positions:
            if self._is_position_reserved_for_other_task(pos, task_id):
                continue
            if pos.is_double_layer:
                # 检查是否已配对（两个SKU在同一货位的上下层且都有库存）
                if (
                    pos.matches_pair(sku1, attrs1 or {}, sku2, attrs2 or {}, match_fields)
                    and pos.upper_quantity > 0
                    and pos.lower_quantity > 0
                ):
                    paired_position = pos
                    break

                # 记录只有单个SKU的位置
                if pos.matches_sku(sku1, attrs1 or {}, match_fields, shelf='upper') and pos.upper_quantity > 0:
                    sku1_positions.append(('upper', pos))
                if pos.matches_sku(sku1, attrs1 or {}, match_fields, shelf='lower') and pos.lower_quantity > 0:
                    sku1_positions.append(('lower', pos))
                if pos.matches_sku(sku2, attrs2 or {}, match_fields, shelf='upper') and pos.upper_quantity > 0:
                    sku2_positions.append(('upper', pos))
                if pos.matches_sku(sku2, attrs2 or {}, match_fields, shelf='lower') and pos.lower_quantity > 0:
                    sku2_positions.append(('lower', pos))

        return paired_position, sku1_positions, sku2_positions

    def _perform_relocation_if_needed(self, task_id: str, production_line: int,
                                       sku1: str, sku2: str,
                                       sku1_positions: List[Tuple[str, InventoryPosition]],
                                       sku2_positions: List[Tuple[str, InventoryPosition]],
                                       attrs1: Optional[Dict[str, Any]] = None,
                                       attrs2: Optional[Dict[str, Any]] = None) -> Optional[dict]:
        """根据库存情况执行移库操作

        Args:
            task_id: 出库任务ID
            production_line: 产线号
            sku1: 第一个SKU
            sku2: 第二个SKU
            sku1_positions: sku1所在位置列表
            sku2_positions: sku2所在位置列表

        Returns:
            移库操作记录，如果无法移库则返回None
        """
        if task_id:
            pending_ready = self.relocation_task_ready_time.get(task_id)
            if pending_ready is not None and self.current_time < pending_ready:
                return {
                    'task_id': task_id,
                    'task_skus': [sku1, sku2],
                    'production_line': production_line,
                    'reason': 'relocation_pending',
                    'relocation_details': {'status': 'pending'},
                }
        same_sku = sku1 == sku2
        if same_sku:
            # 相同SKU的出库需要至少两根可用梁
            # sku1_positions 与 sku2_positions 相同
            total_beams = len(sku1_positions)
            has_sku1 = has_sku2 = total_beams >= 2
        else:
            has_sku1 = len(sku1_positions) > 0
            has_sku2 = len(sku2_positions) > 0

        # 若涉及巷道正在执行任务，推迟移库（避免占用冲突）
        involved_aisles = {p.aisle for _, p in (sku1_positions + sku2_positions) if p is not None}
        if involved_aisles:
            if any(getattr(t, 'assigned_aisle', None) in involved_aisles for t in self.running_tasks.values()):
                return {
                    'task_id': task_id,
                    'task_skus': [sku1, sku2],
                    'production_line': production_line,
                    'reason': 'relocation_aisle_busy',
                    'relocation_details': {
                        'status': 'relocation_aisle_busy',
                        'sku1_positions': [(layer, pos.get_position_id()) for layer, pos in sku1_positions],
                        'sku2_positions': [(layer, pos.get_position_id()) for layer, pos in sku2_positions],
                    }
                }

        # 情况1：两个SKU都有，但分别在不同位置 -> 需要移库配对
        if has_sku1 and has_sku2:
            # 如果相同SKU，避免使用同一位置/同一层的同一根梁做“配对”
            if same_sku:
                distinct_positions = []
                seen_ids = set()
                for layer, pos in sku1_positions:
                    pid = (pos.get_position_id(), layer)
                    if pid not in seen_ids:
                        seen_ids.add(pid)
                        distinct_positions.append((layer, pos))
                if len(distinct_positions) < 2:
                    # 仍然认为缺少一根梁
                    return {
                        'task_id': task_id,
                        'task_skus': [sku1, sku2],
                        'production_line': production_line,
                        'reason': 'missing_sku',
                        'relocation_details': {
                            'existing_sku': sku1,
                            'missing_sku': sku2,
                            'existing_positions': [(layer, pos.get_position_id()) for layer, pos in distinct_positions],
                            'status': 'waiting_for_inbound'
                        }
                    }
                # 重用 distinct_positions 作为两个SKU的位置列表
                sku1_positions = sku2_positions = distinct_positions
            return self._relocate_to_pair(
                task_id,
                production_line,
                sku1,
                sku2,
                sku1_positions,
                sku2_positions,
                reason='unpaired',
                attrs1=attrs1,
                attrs2=attrs2,
            )

        # 情况2：只有一个SKU -> 记录但可能无法完成（需要入库补全）
        elif has_sku1 or has_sku2:
            existing_sku = sku1 if has_sku1 else sku2
            missing_sku = sku2 if has_sku1 else sku1
            existing_positions = sku1_positions if has_sku1 else sku2_positions

            # 尝试寻找是否有另一个SKU可以与现有SKU配对
            # （在这种情况下，实际上是等待入库，这里只记录状态）
            return {
                'task_id': task_id,
                'task_skus': [sku1, sku2],
                'production_line': production_line,
                'reason': 'missing_sku',
                'relocation_details': {
                    'existing_sku': existing_sku,
                    'missing_sku': missing_sku,
                    'existing_positions': [(layer, pos.get_position_id()) for layer, pos in existing_positions],
                    'status': 'waiting_for_inbound'
                }
            }

        # 情况3：两个SKU都没有 -> 无法出库
        else:
            return {
                'task_id': task_id,
                'task_skus': [sku1, sku2],
                'production_line': production_line,
                'reason': 'no_inventory',
                'relocation_details': {
                    'status': 'cannot_fulfill'
                }
            }

    def _relocate_to_pair(self, task_id: str, production_line: int,
                          sku1: str, sku2: str,
                          sku1_positions: List[Tuple[str, InventoryPosition]],
                          sku2_positions: List[Tuple[str, InventoryPosition]],
                          reason: str,
                          attrs1: Optional[Dict[str, Any]] = None,
                          attrs2: Optional[Dict[str, Any]] = None) -> Optional[dict]:
        """执行移库操作，将两个分散的SKU移动到同一货位形成配对

        策略：
        1. 优先将一个SKU移到另一个SKU所在的货位（如果目标货位有空层）
        2. 如果都没有空层，则将两个SKU都移到一个空的双层货位

        Args:
            task_id: 出库任务ID
            production_line: 产线号
            sku1: 第一个SKU
            sku2: 第二个SKU
            sku1_positions: sku1所在位置列表
            sku2_positions: sku2所在位置列表
            reason: 移库原因

        Returns:
            移库操作记录
        """
        relocation_details = {
            'from_positions': [],
            'to_position': None,
            'operations': [],
            'attrs': {
                'sku1': attrs1 or {},
                'sku2': attrs2 or {},
            }
        }
        relocation_aisles: set = set()

        # 当前正在执行的任务所占用的巷道，避免与之冲突
        busy_aisles = {
            t.assigned_aisle for t in self.running_tasks.values()
            if getattr(t, 'assigned_aisle', None) is not None
        }

        def _has_conflict(*positions):
            """判断候选移库来源或目标是否落在正在执行任务占用的巷道。

            从非空货位提取巷道号，与本次计算开始时的 ``busy_aisles`` 取交集；
            冲突时仅输出延后日志，由外层返回 deferred 结果，不创建移库操作。
            """
            aisles = {p.aisle for p in positions if p is not None}
            conflict = bool(busy_aisles & aisles)
            if conflict:
                print(f"[relocation skip] task {task_id}: aisles {aisles} busy with running tasks, defer relocation ({reason})")
            return conflict

        def _fmt(pos_list):
            """将候选 ``(layer, position)`` 列表转换为可序列化的位置摘要。"""
            return [(layer, pos.get_position_id()) for layer, pos in pos_list]

        def _defer_result(status_note):
            """构造未执行移库的统一返回对象。

            保留任务、SKU、产线和候选货位摘要，使调用方能够记录延后原因，
            并在后续事件或 API 查询中重新尝试，而不会提前修改真实库存。
            """
            return {
                'task_id': task_id,
                'task_skus': [sku1, sku2],
                'production_line': production_line,
                'reason': reason,
                'relocation_details': {
                    'status': status_note,
                    'sku1_positions': _fmt(sku1_positions),
                    'sku2_positions': _fmt(sku2_positions),
                }
            }

        # 同 SKU 配对时，两个来源必须是不同的梁（位置和层位不能完全相同）。
        if sku1 == sku2:
            distinct_beams = {(pos.get_position_id(), layer) for layer, pos in sku1_positions}
            if len(distinct_beams) < 2:
                return _defer_result('same_sku_not_enough_distinct_beams')

        # 新策略：优先利用已有货位的下层空位
        sku1_lower_empty = [(layer, pos) for layer, pos in sku1_positions if pos.can_place_sku('lower')]
        sku2_lower_empty = [(layer, pos) for layer, pos in sku2_positions if pos.can_place_sku('lower')]

        # 1) 若 sku1 下层空，则将 sku2 移到 sku1 下层
        if sku1_lower_empty and sku2_positions:
            # 优先选择与 sku2 已有位置同巷道的目标位，便于同巷道移库
            sku2_aisles = {p.aisle for _, p in sku2_positions}
            sku1_lower_empty = sorted(
                sku1_lower_empty,
                key=lambda lp: (lp[1].aisle not in sku2_aisles, lp[1].aisle, lp[1].column, lp[1].level)
            )
            target_pos = sku1_lower_empty[0][1]
            src_candidates = sku2_positions
            # 同 SKU 时，避免选择与目标同一货位的梁
            if sku1 == sku2:
                src_candidates = [(ly, p) for ly, p in sku2_positions if p.get_position_id() != target_pos.get_position_id()]
            if not src_candidates:
                return _defer_result('same_sku_only_one_position_available')
            # 优先选择与目标巷道相同的候选，便于同巷道移库
            src_candidates = sorted(
                src_candidates,
                key=lambda lp: (lp[1].aisle != target_pos.aisle, lp[1].aisle, lp[0])
            )
            src_layer2, src_pos2 = src_candidates[0]

            # 检查当前巷道的任务中是否包含与移库相关的SKU
            if self._has_running_task_with_conflicting_sku([sku1, sku2], {target_pos.aisle, src_pos2.aisle}):
                print(f"[移库跳过] 任务{task_id}: 巷道 {target_pos.aisle, src_pos2.aisle} 的运行任务中包含冲突SKU {sku1}/{sku2}，推迟移库")
                return _defer_result('conflicting_running_task')

            try:
                # 检查目标位置是否仍然可用（双重检查）
                if not target_pos.can_place_sku('lower'):
                    print(f"[移库跳过] 任务{task_id}: 目标位置 {target_pos.get_position_id()} 的下层已不可用，跳过移库")
                    return _defer_result('target_position_occupied')

                wait_offset = self._get_relocation_wait_offset({target_pos.aisle, src_pos2.aisle})
                ready_time = self.current_time + wait_offset + (
                    self.relocation_delay_s * (2 if target_pos.aisle != src_pos2.aisle else 1)
                )
                self.relocation_task_ready_time[task_id] = ready_time
                src_start = self.current_time + wait_offset
                src_end = src_start + self.relocation_delay_s
                if target_pos.aisle != src_pos2.aisle:
                    tgt_end = src_end + self.relocation_delay_s
                else:
                    tgt_end = src_end
                print(
                    f"[relocation-audit] schedule task={task_id} at t={self.current_time:.2f}s "
                    f"src_end={src_end:.2f}s tgt_end={tgt_end:.2f}s sku={sku2} "
                    f"from={src_pos2.get_position_id()} to={target_pos.get_position_id()}"
                )
                # 捕获移库SKU的属性（在移除前获取）
                moved_attrs = {}
                if src_layer2 == "upper":
                    moved_attrs = getattr(src_pos2, "upper_attrs", {}) or {}
                elif src_layer2 == "lower":
                    moved_attrs = getattr(src_pos2, "lower_attrs", {}) or {}
                try:
                    self.inventory_manager.remove_inventory(src_pos2, sku2, 1)
                    print(
                        f"[relocation-audit] apply t={self.current_time:.2f}s task={task_id} "
                        f"action=remove sku={sku2} pos={src_pos2.get_position_id()} result=ok"
                    )
                except Exception as e:
                    print(
                        f"[relocation-audit] apply t={self.current_time:.2f}s task={task_id} "
                        f"action=remove sku={sku2} pos={src_pos2.get_position_id()} result=fail err={e}"
                    )
                    return _defer_result('source_remove_failed')
                print(
                    f"[relocation-audit] move attrs task={task_id} sku={sku2} "
                    f"from={src_pos2.get_position_id()}:{src_layer2} attrs={moved_attrs}"
                )
                self._reserve_position(target_pos, task_id)
                self._schedule_relocation_ops(
                    target_pos.aisle,
                    tgt_end,
                    [{"action": "add", "sku": sku2, "pos_id": target_pos.get_position_id(), "layer": "lower", "task_id": task_id, "attrs": moved_attrs}],
                )

                # 分段占用：先占用移出巷道，再占用移入巷道，按等待偏移执行
                self._add_relocation_block({src_pos2.aisle}, duration_s=self.relocation_delay_s, start_offset=wait_offset)
                self._relocation_count += 1
                if target_pos.aisle != src_pos2.aisle:
                    self._add_relocation_block({target_pos.aisle}, duration_s=self.relocation_delay_s, start_offset=wait_offset + self.relocation_delay_s)
                    self._relocation_count += 1

                relocation_details['from_positions'].append({
                    'sku': sku2,
                    'position': src_pos2.get_position_id(),
                    'layer': src_layer2
                })
                relocation_details['to_position'] = target_pos.get_position_id()
                relocation_details['operations'].append({
                    'type': 'move',
                    'sku': sku2,
                    'from': f"{src_pos2.get_position_id()}:{src_layer2}",
                    'to': f"{target_pos.get_position_id()}:lower"
                })

                attrs_info = ""
                if attrs1 or attrs2:
                    attrs_info = f" attrs=({sku1}:{attrs1 or {}}, {sku2}:{attrs2 or {}})"
                print(f"[移库] 任务{task_id}: 将{sku2}从{src_pos2.get_position_id()}:{src_layer2}移到{target_pos.get_position_id()}:lower与{sku1}配对，用时{self.relocation_delay_s}s{attrs_info}")

                return {
                    'task_id': task_id,
                    'task_skus': [sku1, sku2],
                    'production_line': production_line,
                    'reason': reason,
                    'relocation_details': relocation_details
                }
            except Exception as e:
                print(f"[移库失败] 任务{task_id}: {e}")

        # 2) 若 sku2 下层空，则将 sku1 移到 sku2 下层
        if sku2_lower_empty and sku1_positions:
            sku1_aisles = {p.aisle for _, p in sku1_positions}
            sku2_lower_empty = sorted(
                sku2_lower_empty,
                key=lambda lp: (lp[1].aisle not in sku1_aisles, lp[1].aisle, lp[1].column, lp[1].level)
            )
            target_pos = sku2_lower_empty[0][1]
            src_candidates = sku1_positions
            if sku1 == sku2:
                src_candidates = [(ly, p) for ly, p in sku1_positions if p.get_position_id() != target_pos.get_position_id()]
            if not src_candidates:
                return _defer_result('same_sku_only_one_position_available')
            # 优先选择与目标巷道相同的候选，便于同巷道移库
            src_candidates = sorted(
                src_candidates,
                key=lambda lp: (lp[1].aisle != target_pos.aisle, lp[1].aisle, lp[0])
            )
            src_layer1, src_pos1 = src_candidates[0]

            # 检查当前巷道的任务中是否包含与移库相关的SKU
            if self._has_running_task_with_conflicting_sku([sku1, sku2], {target_pos.aisle, src_pos1.aisle}):
                print(f"[移库跳过] 任务{task_id}: 巷道 {target_pos.aisle, src_pos1.aisle} 的运行任务中包含冲突SKU {sku1}/{sku2}，推迟移库")
                return _defer_result('conflicting_running_task')

            try:
                # 检查目标位置是否仍然可用（双重检查）
                if not target_pos.can_place_sku('lower'):
                    print(f"[移库跳过] 任务{task_id}: 目标位置 {target_pos.get_position_id()} 的下层已不可用，跳过移库")
                    return _defer_result('target_position_occupied')

                wait_offset = self._get_relocation_wait_offset({target_pos.aisle, src_pos1.aisle})
                ready_time = self.current_time + wait_offset + (
                    self.relocation_delay_s * (2 if target_pos.aisle != src_pos1.aisle else 1)
                )
                self.relocation_task_ready_time[task_id] = ready_time
                src_start = self.current_time + wait_offset
                src_end = src_start + self.relocation_delay_s
                if target_pos.aisle != src_pos1.aisle:
                    tgt_end = src_end + self.relocation_delay_s
                else:
                    tgt_end = src_end
                print(
                    f"[relocation-audit] schedule task={task_id} at t={self.current_time:.2f}s "
                    f"src_end={src_end:.2f}s tgt_end={tgt_end:.2f}s sku={sku1} "
                    f"from={src_pos1.get_position_id()} to={target_pos.get_position_id()}"
                )
                # 捕获移库SKU的属性（在移除前获取）
                moved_attrs = {}
                if src_layer1 == "upper":
                    moved_attrs = getattr(src_pos1, "upper_attrs", {}) or {}
                elif src_layer1 == "lower":
                    moved_attrs = getattr(src_pos1, "lower_attrs", {}) or {}
                try:
                    self.inventory_manager.remove_inventory(src_pos1, sku1, 1)
                    print(
                        f"[relocation-audit] apply t={self.current_time:.2f}s task={task_id} "
                        f"action=remove sku={sku1} pos={src_pos1.get_position_id()} result=ok"
                    )
                except Exception as e:
                    print(
                        f"[relocation-audit] apply t={self.current_time:.2f}s task={task_id} "
                        f"action=remove sku={sku1} pos={src_pos1.get_position_id()} result=fail err={e}"
                    )
                    return _defer_result('source_remove_failed')
                print(
                    f"[relocation-audit] move attrs task={task_id} sku={sku1} "
                    f"from={src_pos1.get_position_id()}:{src_layer1} attrs={moved_attrs}"
                )
                self._reserve_position(target_pos, task_id)
                self._schedule_relocation_ops(
                    target_pos.aisle,
                    tgt_end,
                    [{"action": "add", "sku": sku1, "pos_id": target_pos.get_position_id(), "layer": "lower", "task_id": task_id, "attrs": moved_attrs}],
                )

                self._add_relocation_block({src_pos1.aisle}, duration_s=self.relocation_delay_s, start_offset=wait_offset)
                self._relocation_count += 1
                if target_pos.aisle != src_pos1.aisle:
                    self._add_relocation_block({target_pos.aisle}, duration_s=self.relocation_delay_s, start_offset=wait_offset + self.relocation_delay_s)
                    self._relocation_count += 1

                relocation_details['from_positions'].append({
                    'sku': sku1,
                    'position': src_pos1.get_position_id(),
                    'layer': src_layer1
                })
                relocation_details['to_position'] = target_pos.get_position_id()
                relocation_details['operations'].append({
                    'type': 'move',
                    'sku': sku1,
                    'from': f"{src_pos1.get_position_id()}:{src_layer1}",
                    'to': f"{target_pos.get_position_id()}:lower"
                })

                attrs_info = ""
                if attrs1 or attrs2:
                    attrs_info = f" attrs=({sku1}:{attrs1 or {}}, {sku2}:{attrs2 or {}})"
                print(f"[移库] 任务{task_id}: 将{sku1}从{src_pos1.get_position_id()}:{src_layer1}移到{target_pos.get_position_id()}:lower与{sku2}配对，用时{self.relocation_delay_s}s{attrs_info}")

                return {
                    'task_id': task_id,
                    'task_skus': [sku1, sku2],
                    'production_line': production_line,
                    'reason': reason,
                    'relocation_details': relocation_details
                }
            except Exception as e:
                print(f"[移库失败] 任务{task_id}: {e}")

        # # 3) 若双方下层都空（前两步已覆盖），默认将 sku2 移到 sku1 的下层
        # if sku1_lower_empty and sku2_lower_empty and sku2_positions:
        #     target_pos = sku1_lower_empty[0][1]
        #     src_layer2, src_pos2 = sku2_positions[0]

        #     # 检查当前巷道的任务中是否包含与移库相关的SKU
        #     if self._has_running_task_with_conflicting_sku([sku1, sku2], {target_pos.aisle, src_pos2.aisle}):
        #         print(f"[移库跳过] 任务{task_id}: 巷道 {target_pos.aisle, src_pos2.aisle} 的运行任务中包含冲突SKU {sku1}/{sku2}，推迟移库")
        #         return _defer_result('conflicting_running_task')

        #     try:
        #         # 检查目标位置是否仍然可用（双重检查）
        #         if not target_pos.can_place_sku('lower'):
        #             print(f"[移库跳过] 任务{task_id}: 目标位置 {target_pos.get_position_id()} 的下层已不可用，跳过移库")
        #             return _defer_result('target_position_occupied')

        #         wait_offset = self._get_relocation_wait_offset({target_pos.aisle, src_pos2.aisle})
        #         self.inventory_manager.remove_inventory(src_pos2, sku2, 1)
        #         self.inventory_manager.add_inventory(target_pos, sku2, 1, 'lower')

        #         self._relocation_count += 1
        #         self._add_relocation_block({src_pos2.aisle}, duration_s=self.relocation_delay_s, start_offset=wait_offset)
        #         if target_pos.aisle != src_pos2.aisle:
        #             self._add_relocation_block({target_pos.aisle}, duration_s=self.relocation_delay_s, start_offset=wait_offset + self.relocation_delay_s)

        #         relocation_details['from_positions'].append({
        #             'sku': sku2,
        #             'position': src_pos2.get_position_id(),
        #             'layer': src_layer2
        #         })
        #         relocation_details['to_position'] = target_pos.get_position_id()
        #         relocation_details['operations'].append({
        #             'type': 'move',
        #             'sku': sku2,
        #             'from': f"{src_pos2.get_position_id()}:{src_layer2}",
        #             'to': f"{target_pos.get_position_id()}:lower"
        #         })

        #         print(f"[移库] 任务{task_id}: 将{sku2}从{src_pos2.get_position_id()}:{src_layer2}移到{target_pos.get_position_id()}:lower与{sku1}配对，用时{self.relocation_delay_s}s")

        #         return {
        #             'task_id': task_id,
        #             'task_skus': [sku1, sku2],
        #             'production_line': production_line,
        #             'reason': reason,
        #             'relocation_details': relocation_details
        #         }
        #     except Exception as e:
        #         print(f"[移库失败] 任务{task_id}: {e}")

        # # 4) 下层都不空，再考虑同时移动到空位置
        # empty_positions = self.inventory_manager.get_empty_positions()
        # double_layer_empty = [p for p in empty_positions if p.is_double_layer]

        # # 选择两根源梁：需要确保来源不是同一层位（特别是 sku1==sku2 时）
        # def _pick_two_distinct(src_list1, src_list2):
        #     for layer1, pos1 in src_list1:
        #         for layer2, pos2 in src_list2:
        #             if (pos1 is not pos2) or (layer1 != layer2):
        #                 return (layer1, pos1), (layer2, pos2)
        #     return None, None

        # if double_layer_empty and sku1_positions and sku2_positions:
        #     src_pair = _pick_two_distinct(sku1_positions, sku2_positions)
        #     if src_pair[0] is None:
        #         return None  # 无法找到两根不同梁，放弃移库
        #     target_pos = double_layer_empty[0]
        #     (src_layer1, src_pos1), (src_layer2, src_pos2) = src_pair

        #     # 检查当前巷道的任务中是否包含与移库相关的SKU
        #     if self._has_running_task_with_conflicting_sku([sku1, sku2], {target_pos.aisle, src_pos1.aisle, src_pos2.aisle}):
        #         print(f"[移库跳过] 任务{task_id}: 巷道 {target_pos.aisle, src_pos1.aisle, src_pos2.aisle} 的运行任务中包含冲突SKU {sku1}/{sku2}，推迟移库")
        #         return _defer_result('conflicting_running_task')

        #     # 检查目标位置是否仍然可用（双重检查）
        #     if not (target_pos.can_place_sku('upper') and target_pos.can_place_sku('lower')):
        #         print(f"[移库跳过] 任务{task_id}: 目标位置 {target_pos.get_position_id()} 已不可用，跳过移库")
        #         return _defer_result('target_position_occupied')

        #     wait_offset = self._get_relocation_wait_offset({target_pos.aisle, src_pos1.aisle, src_pos2.aisle})

        #     try:
        #         # 移除两个SKU
        #         self.inventory_manager.remove_inventory(src_pos1, sku1, 1)
        #         self.inventory_manager.remove_inventory(src_pos2, sku2, 1)

        #         # 添加到新位置
        #         self.inventory_manager.add_inventory(target_pos, sku1, 1, 'upper')
        #         self.inventory_manager.add_inventory(target_pos, sku2, 1, 'lower')

        #         self._relocation_count += 2  # 两次移动操作
        #         self._add_relocation_block({src_pos1.aisle, src_pos2.aisle}, duration_s=self.relocation_delay_s, start_offset=wait_offset)
        #         if target_pos.aisle not in {src_pos1.aisle, src_pos2.aisle}:
        #             self._add_relocation_block({target_pos.aisle}, duration_s=self.relocation_delay_s, start_offset=wait_offset + self.relocation_delay_s)

        #         relocation_details['from_positions'] = [
        #             {'sku': sku1, 'position': src_pos1.get_position_id(), 'layer': src_layer1},
        #             {'sku': sku2, 'position': src_pos2.get_position_id(), 'layer': src_layer2}
        #         ]
        #         relocation_details['to_position'] = target_pos.get_position_id()
        #         relocation_details['operations'] = [
        #             {
        #                 'type': 'move',
        #                 'sku': sku1,
        #                 'from': f"{src_pos1.get_position_id()}:{src_layer1}",
        #                 'to': f"{target_pos.get_position_id()}:upper"
        #             },
        #             {
        #                 'type': 'move',
        #                 'sku': sku2,
        #                 'from': f"{src_pos2.get_position_id()}:{src_layer2}",
        #                 'to': f"{target_pos.get_position_id()}:lower"
        #             }
        #         ]

        #         print(f"[移库] 任务{task_id}: 将{sku1}和{sku2}移到空货位{target_pos.get_position_id()}配对，用时{self.relocation_delay_s}s")

        #         return {
        #             'task_id': task_id,
        #             'task_skus': [sku1, sku2],
        #             'production_line': production_line,
        #             'reason': reason,
        #             'relocation_details': relocation_details
        #         }
        #     except Exception as e:
        #         print(f"[移库失败] 任务{task_id}: {e}")

        # 无法执行移库
        return {
            'task_id': task_id,
            'task_skus': [sku1, sku2],
            'production_line': production_line,
            'reason': reason,
            'relocation_details': {
                'status': 'relocation_failed',
                'sku1_positions': [(layer, pos.get_position_id()) for layer, pos in sku1_positions],
                'sku2_positions': [(layer, pos.get_position_id()) for layer, pos in sku2_positions]
            }
        }

    def _add_relocation_block(self, aisles: set, duration_s: float, start_offset: float = 0.0):
        """标记给定巷道在指定时间段因移库占用，期间不派发新任务

        Args:
            aisles (set): 参与当前处理的巷道编号集合。
            duration_s (float): 移库或资源占用持续时间，单位秒。
            start_offset (float): 相对当前仿真时刻的开始偏移，单位秒。

        Returns:
            None: 合并并写入各巷道的移库忙碌区间，供后续派发和时间估算检查。
        """
        if not aisles or duration_s <= 0:
            return
        start_time = self.current_time + max(0.0, start_offset)
        end_time = start_time + duration_s
        for aisle in aisles:
            intervals = self.relocation_busy_intervals.setdefault(aisle, [])
            # 只合并与新段重叠的区间，保留相邻区间以便区分占用段
            new_s, new_e = start_time, end_time
            merged = []
            for s, e in intervals:
                if e <= new_s or s >= new_e:
                    merged.append((s, e))
                else:
                    new_s = min(new_s, s)
                    new_e = max(new_e, e)
            merged.append((new_s, new_e))
            merged.sort(key=lambda x: x[0])
            self.relocation_busy_intervals[aisle] = merged

            # 输出移库执行信息
            print(f"[移库占用] 巷道 {aisle} 在时间 {new_s:.1f}s 到 {new_e:.1f}s 之间执行移库操作")

    def _get_relocation_delay_until_free(self, aisle: int, proposed_start: float) -> float:
        """返回巷道因移库占用需要额外等待的时间（秒）。

        如果 proposed_start 落在某个移库占用区间内，则需要等待到该区间结束；
        否则返回 0。

        Args:
            aisle (int): 目标巷道编号。
            proposed_start (float): 当前任务或移库操作候选的开始时刻，单位秒。

        Returns:
            float: 当前计算得到的时间、评分或比例数值。
        """
        intervals = self.relocation_busy_intervals.get(aisle, [])
        # 清理过期区间
        intervals = [(s, e) for s, e in intervals if e > proposed_start]
        self.relocation_busy_intervals[aisle] = intervals

        delay = 0.0
        for s, e in intervals:
            if s <= proposed_start < e:
                delay = max(delay, e - proposed_start)
        return delay

    def _is_aisle_relocation_busy(self, aisle: int, current_time: float) -> bool:
        """检查巷道在当前时间是否因移库占用

        Args:
            aisle (int): 目标巷道编号。
            current_time (float): 当前仿真时间，单位秒。

        Returns:
            bool: 处理成功、条件成立或校验通过时为 ``True``，否则为 ``False``。
        """
        intervals = self.relocation_busy_intervals.get(aisle, [])
        # 清理过期的区间
        self.relocation_busy_intervals[aisle] = [(s, e) for s, e in intervals if e > current_time]
        return any(s <= current_time < e for s, e in self.relocation_busy_intervals[aisle])

    def _get_relocation_wait_offset(self, aisles: set) -> float:
        """计算移库需要等待的时间（若目标/来源巷道当前繁忙，则等待到空闲）

        Args:
            aisles (set): 参与当前处理的巷道编号集合。

        Returns:
            float: 当前计算得到的时间、评分或比例数值。
        """
        if not aisles:
            return 0.0
        # 先清理过期区间
        for a, intervals in list(self.relocation_busy_intervals.items()):
            self.relocation_busy_intervals[a] = [(s, e) for s, e in intervals if e > self.current_time]
        wait = 0.0
        for a in aisles:
            for s, e in self.relocation_busy_intervals.get(a, []):
                if self.current_time < s:
                    # 未来已有移库占用，等待到占用结束，避免重叠
                    wait = max(wait, e - self.current_time)
                elif s <= self.current_time < e:
                    wait = max(wait, e - self.current_time)
        # 若巷道上有正在运行的任务，也等待 relocation_delay_s
        running_busy = any(getattr(t, 'assigned_aisle', None) in aisles for t in self.running_tasks.values())
        if running_busy:
            wait = max(wait, self.relocation_delay_s)
        return wait

    def _get_relocation_active_until(self, current_time: float) -> Optional[float]:
        """读取并返回指定条件下的状态、对象或计算结果，不主动改变业务状态。

        输入：current_time（float）
        输出：Optional[float]

        Args:
            current_time (float): 当前仿真时间，单位秒。

        Returns:
            Optional[float]: 计算得到的结果；当前条件不成立或不存在可用对象时返回 ``None``。
        """
        max_end = None
        for a, intervals in list(self.relocation_busy_intervals.items()):
            new_intervals = []
            for s, e in intervals:
                if e <= current_time:
                    continue
                new_intervals.append((s, e))
                if s <= current_time < e:
                    if max_end is None or e > max_end:
                        max_end = e
            self.relocation_busy_intervals[a] = new_intervals
        return max_end

    def _reserve_position(self, pos: InventoryPosition, task_id: str) -> None:
        """为移库任务临时锁定目标位置，不修改库存。

        ``pos.reserved`` 负责让货位可用性判断立即失效，字典保存所有者任务 ID，
        供冲突检查、任务完成和失败/取消清理时精确释放。

        Args:
            pos (InventoryPosition): 当前处理的 InventoryPosition 货位对象。
            task_id (str): 任务唯一标识，用于定位队列、冻结位置或反馈对象。

        Returns:
            None: 不返回业务数据；处理结果通过实例状态、队列或传入对象体现。
        """
        pos_id = pos.get_position_id()
        pos.reserved = True
        self.relocation_reserved_positions[pos_id] = task_id

    def _is_position_reserved_for_other_task(self, pos: InventoryPosition, task_id: Optional[str] = None) -> bool:
        """判断移库预留是否属于其他任务。

        ``reserved`` 只表示位置被锁定，所有者任务号保存在
        relocation_reserved_positions；同一任务再次检查自己的位置时
        允许通过，其他任务则不能复用。

        Args:
            pos (InventoryPosition): 当前处理的 InventoryPosition 货位对象。
            task_id (Optional[str]): 任务唯一标识，用于定位队列、冻结位置或反馈对象。

        Returns:
            bool: 处理成功、条件成立或校验通过时为 ``True``，否则为 ``False``。
        """
        if not getattr(pos, "reserved", False):
            return False
        owner_task_id = self.relocation_reserved_positions.get(pos.get_position_id())
        return owner_task_id != task_id

    def _release_reserved_position(self, pos_id: str) -> None:
        """按坐标主键释放一项移库预留，并返回原所有者任务 ID。

        Args:
            pos_id (str): 当前对象的唯一标识。

        Returns:
            None: 不返回业务数据；处理结果通过实例状态、队列或传入对象体现。
        """
        task_id = self.relocation_reserved_positions.pop(pos_id, None)
        pos = self.inventory_manager.position_map.get(pos_id)
        if pos is not None:
            pos.reserved = False
        return task_id

    def _release_reserved_positions_for_task(self, task_id: str) -> None:
        """释放某移库任务登记的全部位置，供完成、失败或取消路径共同调用。

        Args:
            task_id (str): 任务唯一标识，用于定位队列、冻结位置或反馈对象。

        Returns:
            None: 不返回业务数据；处理结果通过实例状态、队列或传入对象体现。
        """
        if not task_id:
            return
        reserved_pos_ids = [
            pos_id
            for pos_id, owner_task_id in self.relocation_reserved_positions.items()
            if owner_task_id == task_id
        ]
        for pos_id in reserved_pos_ids:
            self._release_reserved_position(pos_id)

    def _schedule_relocation_ops(self, aisle: int, end_time: float, ops: List[dict]) -> None:
        """登记一批在指定完成时刻执行的移库库存操作。

        Args:
            aisle (int): 目标巷道编号。
            end_time (float): 这批移库操作可提交到真实库存的完成时刻。
            ops (List[dict]): 按执行顺序排列的 ``remove``/``add`` 操作；每项至少带
                SKU、货位 ID、可选层号和所属移库任务 ID。

        Returns:
            None: 将 ``(end_time, ops)`` 追加到该巷道的延迟操作队列；不立即修改库存。
        """
        # 同巷道可积累多批不同结束时刻的移库。事件推进统一由
        # _apply_relocation_ops 消费，避免规划阶段就改变真实库存。
        if aisle not in self.relocation_ops_by_aisle:
            self.relocation_ops_by_aisle[aisle] = []
        self.relocation_ops_by_aisle[aisle].append((end_time, ops))

    def _get_next_relocation_op_time(self) -> Optional[float]:
        """返回所有巷道下一批待执行移库操作的最早完成时刻。

        Returns:
            Optional[float]: 计算得到的结果；当前条件不成立或不存在可用对象时返回 ``None``。
        """
        next_time = None
        for entries in self.relocation_ops_by_aisle.values():
            for end_time, _ in entries:
                if next_time is None or end_time < next_time:
                    next_time = end_time
        return next_time

    def _apply_relocation_ops(self, current_time: float) -> None:
        """应用已经到达完成时刻的移库操作。

        remove 操作扣减源位置库存，add 操作写入目标位置并同步索引；
        目标写入失败时立即释放该位置预留，未到时刻的操作继续保留在
        relocation_ops_by_aisle，供下一次事件推进处理。

        Args:
            current_time (float): 当前仿真时间，单位秒。

        Returns:
            None: 不返回业务数据；处理结果通过实例状态、队列或传入对象体现。
        """
        # 逐巷道消费到期批次；使用列表副本遍历允许循环末尾把未到期批次安全回写。
        for aisle, entries in list(self.relocation_ops_by_aisle.items()):
            if not entries:
                continue
            pending = []
            for end_time, ops in entries:
                if end_time <= current_time:
                    # 一批操作按 remove/add 列表顺序提交。规划器负责把源位扣减排在
                    # 目标位写入前，保证同一批移库不会短时间复制出额外库存。
                    for op in ops:
                        action = op.get("action")
                        sku = op.get("sku")
                        pos_id = op.get("pos_id")
                        layer = op.get("layer")
                        task_id = op.get("task_id")
                        if not (action and sku and pos_id):
                            # 不完整操作不能安全落账；忽略该条，避免使用默认货位或 SKU
                            # 误修改库存。若它是 add，还会在 finally 中释放目标预留。
                            continue
                        pos = self.inventory_manager.position_map.get(pos_id)
                        if not pos:
                            print(f"[relocation-audit] apply t={current_time:.2f}s task={task_id} "
                                  f"action={action} sku={sku} pos={pos_id} result=missing_pos")
                            if action == "add":
                                self._release_reserved_position(pos_id)
                            continue
                        add_succeeded = False
                        try:
                            if action == "remove":
                                self.inventory_manager.remove_inventory(pos, sku, 1)
                            elif action == "add":
                                # add 的目标位在规划时已经 reserved；只有写入成功才保留
                                # 该占用，失败时 finally 释放，供后续分配重新使用。
                                attrs = op.get("attrs")
                                self.inventory_manager.add_inventory(pos, sku, 1, layer, attrs=attrs)
                                add_succeeded = True
                            print(f"[relocation-audit] apply t={current_time:.2f}s task={task_id} "
                                  f"action={action} sku={sku} pos={pos_id} result=ok")
                        except Exception as e:
                            print(f"[relocation-audit] apply t={current_time:.2f}s task={task_id} "
                                  f"action={action} sku={sku} pos={pos_id} result=fail err={e}")
                        finally:
                            if action == "add" and not add_succeeded:
                                self._release_reserved_position(pos_id)
                else:
                    pending.append((end_time, ops))
            self.relocation_ops_by_aisle[aisle] = pending

    def _is_task_relocation_ready(self, task_id: str, current_time: float) -> bool:
        """Return True if the task can start after relocation.

        Args:
            task_id (str): 任务唯一标识，用于定位队列、冻结位置或反馈对象。
            current_time (float): 当前仿真时间，单位秒。

        Returns:
            bool: 处理成功、条件成立或校验通过时为 ``True``，否则为 ``False``。
        """
        if not task_id:
            return True
        ready_time = self.relocation_task_ready_time.get(task_id)
        if ready_time is None:
            return True
        if current_time >= ready_time:
            self.relocation_task_ready_time.pop(task_id, None)
            return True
        return False

    def _has_running_task_with_conflicting_sku(self, skus: List[str], aisles: set) -> bool:
        """检查指定巷道中 running 任务是否正在处理目标 SKU。

        Args:
            skus (List[str]): 当前任务或计划中的 SKU 条目列表。
            aisles (set): 参与当前处理的巷道编号集合。

        Returns:
            bool: 处理成功、条件成立或校验通过时为 ``True``，否则为 ``False``。
        """
        # 去掉 None 和空字符串。
        target = {s for s in skus if s}
        if not target:
            return False
        for task in self.running_tasks.values():
            if getattr(task, 'assigned_aisle', None) not in aisles:
                continue
            task_skus = []
            if getattr(task, 'skus', None):
                for s in task.skus:
                    if isinstance(s, dict):
                        sku_id = s.get('skuId')
                    elif isinstance(s, str):
                        sku_id = s
                    else:
                        sku_id = getattr(s, 'skuId', None)
                    if sku_id:
                        task_skus.append(sku_id)
            if target.intersection(task_skus):
                return True
        return False
