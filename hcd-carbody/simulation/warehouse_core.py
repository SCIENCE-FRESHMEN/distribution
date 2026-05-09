"""
仓库仿真核心模块
整合库存管理、任务生成、时间计算等功能
"""

import random
import heapq
import json
import re
import os
try:
    import json5  # type: ignore
except Exception:
    json5 = None
from pathlib import Path
from copy import deepcopy
from typing import Dict, List, Tuple, Optional, Any
from .task_data import TaskData
from .inventory import InventoryManager
from .metrics import MetricsCalculator
from estimate.time_estimator import TimeEstimator, load_time_estimator_config
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

from schedule import get_scheduler


def load_warehouse_config(path: Optional[str]) -> dict:
    """从JSON/JSON5文件加载仓库初始化配置（如果存在）"""
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    text = p.read_text(encoding="utf-8")
    try:
        # 优先使用JSON5（支持注释、尾随逗号等特性）
        if json5 is not None:
            return json5.loads(text)
        return json.loads(text)
    except Exception:
        # JSON5解析失败时，使用严格JSON解析器作为备选
        try:
            return json.loads(text)
        except Exception:
            return {}


class WarehouseCore:
    """仓库仿真核心类，负责管理仓库的仿真运行状态和调度逻辑"""
    
    # 类变量，用于存储入库记录，确保只加载一次
    _inbound_records = None
    _inbound_records_loaded = False
    
    def __init__(self, num_aisles: int = 5, num_production_lines: int = 3,
                 num_rows: int = 2, num_columns: int = 3, num_levels: int = 18,
                 total_positions: int = 1000, max_beams: int = 980,
                 initial_inventory_ratio: float = 0.3, random_seed: Optional[int] = None,
                 use_double_layer: bool = True,
                 use_magnetic_crane: bool = True, outbound_congestion_time: float = 10.0,
                 aisle_production_line_mapping: Optional[Dict[int, List[int]]] = None,
                 lr_balance_weight: float = 0,
                 scheduler_type: str = 'heuristic',
                 inbound_aisle_strategy: Optional[str] = None,
                 inbound_allocation_strategy: Optional[str] = None,
                 initial_inventory_count: int = 250,
                 transport_delay_s: Optional[float] = 30.0,
                 blockage_time: float = 15.0,
                 magnetic_crane_time: float = 40.0,
                 relocation_delay_s: float = 80.0,
                 config_path: Optional[str] = "config/warehouse.json"):
        """
        仓库核心初始化方法，设置各种参数和配置项
        Args:
            num_aisles: 巷道数量
            num_production_lines: 产线数量
            num_rows, num_columns, num_levels: 仓位行/列/层
            total_positions: 仓位总数
            max_beams: 最大梁数
            initial_inventory_ratio: 初始库存占比
            random_seed: 随机种子
            use_double_layer: 是否启用双层货位
            use_magnetic_crane: 是否使用磁力吊（默认True）
            outbound_congestion_time: 出库口拥堵时间（秒），即磁力吊资源到位后的拥堵时间
            aisle_production_line_mapping: 巷道-产线映射配置 {aisle: [可服务的产线列表]}，None表示所有巷道可服务所有产线
            lr_balance_weight: 左右均衡度权重（0-1），默认0.3，如果不使用磁力吊建议设为0
            initial_inventory_count: 初始库存总量（默认提取250条入库记录）
            transport_delay_s: 入库运输延迟（秒）
            blockage_time: 拥堵时间（秒）
            magnetic_crane_time: 磁力吊工作时间（秒）
            relocation_delay_s: 移库延迟（秒）
            config_path: 仓库几何/拥堵/运输等配置 JSON 路径
        """
        cfg = load_warehouse_config(config_path)

        # 从配置文件加载基本仓库参数
        self.num_aisles = int(cfg.get("num_aisles", num_aisles))
        self.num_rows = int(cfg.get("num_rows", num_rows))
        self.num_columns = int(cfg.get("num_columns", num_columns))
        self.num_levels = int(cfg.get("num_levels", num_levels))
        self.total_positions = int(cfg.get("total_positions", total_positions))
        self.max_beams = int(cfg.get("max_beams", max_beams))
        self.use_double_layer = bool(cfg.get("use_double_layer", use_double_layer))

        self.num_production_lines = int(cfg.get("num_production_lines", num_production_lines))
        self.initial_inventory_ratio = float(cfg.get("initial_inventory_ratio", initial_inventory_ratio))
        self.initial_inventory_count = int(cfg.get("initial_inventory_count", initial_inventory_count))
        self.aisles = list(range(1, self.num_aisles + 1))
        # 加载巷道维度配置，支持不同巷道具有不同的行列层数
        self.aisle_dimensions = self._normalize_aisle_dimensions(cfg.get("aisle_dimensions", {}))
        # 加载特征别名配置，用于规范化不同名称的特征
        self.feature_alias = self._normalize_feature_alias(cfg.get("feature_alias", {}))
        # 加载巷道禁止规则，指定某些巷道不能存放特定特征的货物
        self.aisle_forbidden = self._normalize_aisle_forbidden(cfg.get("aisle_forbidden", {}))
        # 加载禁用巷道列表
        self.disabled_aisles = self._normalize_disabled_aisles(cfg.get("disabled_aisles", []))
        # 活跃巷道列表（非禁用巷道）
        self.active_aisles = [a for a in self.aisles if a not in self.disabled_aisles]
        # 如果配置了巷道维度，则重新计算总仓位数
        if self.aisle_dimensions and "total_positions" not in cfg:
            self.total_positions = sum(
                int(self.aisle_dimensions.get(a, {}).get("rows", self.num_rows))
                * int(self.aisle_dimensions.get(a, {}).get("columns", self.num_columns))
                * int(self.aisle_dimensions.get(a, {}).get("levels", self.num_levels))
                for a in self.aisles
            )
        
        # 设置随机种子
        random_seed = cfg.get("random_seed", random_seed)
        if random_seed is not None:
            random.seed(random_seed)
            print(f"设置随机种子: {random_seed}")
        
        # 磁力吊和拥堵配置
        self.use_magnetic_crane = bool(cfg.get("use_magnetic_crane", use_magnetic_crane))
        self.outbound_congestion_time = float(cfg.get("outbound_congestion_time", outbound_congestion_time))
        # 从环境变量或配置文件加载各种权重参数
        weight_env_map = {
            "lr_balance_weight": "OPT_LR_BALANCE_WEIGHT",
            "makespan_weight": "OPT_MAKESPAN_WEIGHT",
            "balance_weight": "OPT_BALANCE_WEIGHT",
            "production_line_avg_time_weight": "OPT_PRODUCTION_LINE_AVG_TIME_WEIGHT",
            "production_line_balance_weight": "OPT_PRODUCTION_LINE_BALANCE_WEIGHT",
            "aisle_dispersion_weight": "OPT_AISLE_DISPERSION_WEIGHT",
            "inbound_wait_weight": "OPT_INBOUND_WAIT_WEIGHT",
        }

        def _read_weight(key: str, default_val: float) -> float:
            env_key = weight_env_map.get(key)
            if env_key:
                raw = os.getenv(env_key)
                if raw is not None and str(raw).strip() != "":
                    try:
                        return float(raw)
                    except Exception:
                        print(f"[WARN] invalid env {env_key}={raw}, fallback to config/default")
            return float(cfg.get(key, default_val))

        # 权重参数
        self.lr_balance_weight = _read_weight("lr_balance_weight", lr_balance_weight)
        self.makespan_weight = _read_weight("makespan_weight", 0.3)
        self.balance_weight = _read_weight("balance_weight", 0.001)
        self.production_line_avg_time_weight = _read_weight("production_line_avg_time_weight", 0.5)
        self.production_line_balance_weight = _read_weight("production_line_balance_weight", 0.3)
        self.aisle_dispersion_weight = _read_weight("aisle_dispersion_weight", 0.3)
        self.inbound_wait_weight = _read_weight("inbound_wait_weight", 0.01)
        
        # 出库匹配特征配置
        cfg_match_features = cfg.get("outbound_match_features", {}) or {}
        # 按产线配置匹配特征: {production_line: [features]}
        if isinstance(cfg_match_features, dict):
            self.outbound_match_features_by_line = {
                int(k): [self._canonical_feature_key(x) for x in list(v or [])] for k, v in cfg_match_features.items()
            }
            # 如果未按产线指定，默认使用RFID
            self.outbound_match_features_default = ["rfid"]
        else:
            self.outbound_match_features_by_line = {}
            self.outbound_match_features_default = [self._canonical_feature_key(x) for x in list(cfg_match_features or ["rfid"])]
        
        # 左右库定义，用于左右均衡计算
        mid_point = len(self.aisles) // 2
        self.left_aisles = self.aisles[:mid_point]
        self.right_aisles = self.aisles[mid_point:]
        
        # 巷道-产线映射配置，定义哪些巷道可以服务哪些产线
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
        
        # SKU配置已移除，SKU按实际入库动态出现
        self.sku_types = []
        self.sku_to_production_line = {}
        
        # 加载入库任务配置，只在首次实例化时加载
        if not WarehouseCore._inbound_records_loaded:
            try:
                with open("simulation/data/inbound_task_config.json", "r", encoding="utf-8") as f:
                    inbound_config = json.load(f)
                WarehouseCore._inbound_records = inbound_config.get("inbound_records", [])
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
        
        # 加载时间估算器配置
        time_cfg = load_time_estimator_config("config/time_estimator.json")
        dock_map_in = time_cfg.get("dock_map_in", {})
        dock_map_out = time_cfg.get("dock_map_out", {})
        # 加载禁用位置列表
        disabled_positions = list(cfg.get("disabled_positions", []))
        
        # 创建子模块
        self.inventory_manager = InventoryManager(
            num_aisles=self.num_aisles,
            num_rows=self.num_rows,
            num_columns=self.num_columns,
            num_levels=self.num_levels,
            total_positions=self.total_positions,
            max_beams=self.max_beams,
            initial_inventory_ratio=self.initial_inventory_ratio,
            use_double_layer=self.use_double_layer,
            disabled_positions=disabled_positions,
            aisle_dimensions=self.aisle_dimensions,
        )
        # 创建指标计算器
        self.metrics_calculator = MetricsCalculator(
            self.aisles, self.sku_types, 
            left_aisles=self.left_aisles,
            right_aisles=self.right_aisles,
            lr_balance_weight=self.lr_balance_weight
        )
        # 创建时间估算器（注意：暂时不加载模型）
        self.time_estimator = TimeEstimator(load_model=False)
        
        # 仿真参数
        self.blockage_time = float(cfg.get("blockage_time", blockage_time))  # 拥堵时间（秒）
        self.magnetic_crane_time = float(cfg.get("magnetic_crane_time", magnetic_crane_time))  # 磁力吊工作时间（秒）
        self.transport_delay_s = float(cfg.get("transport_delay_s", transport_delay_s if transport_delay_s is not None else 30.0))  # 入库从分配巷道到达入口的运输延迟
        # 当所有候选巷道预计已满时，入库到达延迟的最大重试次数
        # 防止在仿真评分模式下的无限重试循环
        self.inbound_arrival_max_retries = int(cfg.get("inbound_arrival_max_retries", 12))
        
        # 统计数据
        self.total_rounds = 0
        self.task_id_counter = {TASK_TYPE_INBOUND: 0, TASK_TYPE_OUTBOUND: 0}  # 任务ID计数器
        self._relocation_count = 0  # 移库操作计数
        
        # 生产计划：每条产线当日要出的SKU组
        # 格式: {production_line: [['A1', 'A2', 'A3', 'A4'], ['A1', 'A2', 'A3', 'A4'], ...]}
        self.production_plan = {pl: [] for pl in range(1, self.num_production_lines + 1)}
        # 跟踪每条产线当前进行到第几组（应该是第几辆车的意思吧）
        self.production_line_current_group = {pl: 0 for pl in range(1, self.num_production_lines + 1)}
        # 跟踪每条产线当前组已完成的task_id集合
        self.production_line_completed_tasks = {pl: set() for pl in range(1, self.num_production_lines + 1)}
        # 记录每条产线每组完成的时间
        self.production_line_group_completion_times = {pl: [] for pl in range(1, self.num_production_lines + 1)}
        
        # 拥堵状态: {(aisle, out_line): {'blocked': bool, 'unblock_time': float}}
        self.blockage_status = {}
        # 获取出库线/产线列表
        out_lines = list(getattr(self.time_estimator, "dock_map_out", {}).keys())
        if not out_lines:
            out_lines = list(range(1, self.num_production_lines + 1))
        for aisle in self.aisles:
            for out_line in out_lines:
                self.blockage_status[(aisle, out_line)] = {'blocked': False, 'unblock_time': 0.0}
        # 分配策略（可选，用于入库任务的巷道分配和货位分配）
        self.inbound_aisle_allocator = None  # 需要外部设置
        self.inbound_position_allocator = None  # 需要外部设置，用于入库货位分配

        # 根据传入的策略字符串进行初始化配置
        # 说明：参数 inbound_aisle_strategy 表示入库巷道分配策略；
        # 参数 inbound_allocation_strategy 表示入库货位分配策略
        if inbound_aisle_strategy or inbound_allocation_strategy:
            # 兼容已有的内部配置函数签名
            self.set_inbound_strategies(
                inbound_allocation_strategy=inbound_aisle_strategy,
                inbound_position_strategy=inbound_allocation_strategy,
            )

        # 预创建调度器并与Core绑定
        self.scheduler_type = cfg.get("scheduler_type", scheduler_type)
        scheduler_class = get_scheduler(self.scheduler_type)
        self.scheduler = scheduler_class(self)
        # 透传货位分配器（稍后策略变更时会再次同步）
        self.scheduler.position_allocator = self.inbound_position_allocator

        # 运行态（新：由Core统一管理调度与事件产生）
        self.running_tasks: Dict[str, TaskData] = {}
        # 每个巷道的当前结束位置（上一任务的终点位置）
        self.current_position_by_aisle = {aisle: None for aisle in self.aisles}
        self.completed_tasks: List[TaskData] = []
        self.pending_inbound_by_aisle: Dict[int, List[TaskData]] = {aisle: [] for aisle in self.aisles}
        self.pending_outbound_queue: List[TaskData] = []  # 按需生成的出库任务池
        self.crane_available_times: Dict[tuple, float] = {}  # {(out_line, side): time}
        # 事件驱动仿真相关
        self.event_queue = []  # 优先队列（最小堆）
        self.current_time = 0.0  # 当前仿真时间
        # 全局任务状态映射，key=task_id, value in {'pending','assigned','processing','completed'}
        self.task_status = {}
        # 反馈控制：如果需要真实反馈再标记完成，则阻止估计提前完成
        self.require_feedback_completion = False
        self.feedback_received = set()
        self.awaiting_feedback = set()
        # 移库相关：记录每个巷道因移库占用到的时间
        self.relocation_busy_intervals = {aisle: [] for aisle in self.aisles}
        self.relocation_delay_s = float(cfg.get("relocation_delay_s", relocation_delay_s))
        self.relocation_task_ready_time = {}
        self.relocation_task_ids = set()
        self.relocation_ops_by_aisle = {aisle: [] for aisle in self.aisles}
        self.relocation_reserved_positions = {}
    
    def initialize(self):
        """初始化仓库状态，包括库存管理和均衡度计算"""
        self.inventory_manager.initialize()
        self.inventory_manager.print_distribution()
        
        balance = self.metrics_calculator.calculate_distribution_balance(
            self.inventory_manager.current_inventory
        )
        print(f"  初始综合均衡度: {balance:.3f}")
        
        # 打印配置信息
        print(f"\n仓库配置:")
        print(f"  使用磁力吊: {self.use_magnetic_crane}")
        if self.use_magnetic_crane:
            print(f"  磁力吊工作时间: {self.magnetic_crane_time}秒")
        print(f"  出库口拥堵时间: {self.outbound_congestion_time}秒")
        print(f"  左右均衡权重: {self.lr_balance_weight}")
    
    def _build_io_port_disabled_positions(self, dock_levels_by_col: Dict[int, Any]) -> List[str]:
        """根据码头级别构建禁用位置列表"""
        disabled = []
        for col, levels in dock_levels_by_col.items():
            try:
                levels_iter = [int(l) for l in levels]
            except Exception:
                continue
            for aisle in range(1, self.num_aisles + 1):
                dims = self.aisle_dimensions.get(aisle, {})
                aisle_rows = int(dims.get("rows", self.num_rows))
                aisle_columns = int(dims.get("columns", self.num_columns))
                aisle_levels = int(dims.get("levels", self.num_levels))
                if col < 1 or col > aisle_columns:
                    continue
                for row in range(1, aisle_rows + 1):
                    for level in levels_iter:
                        if level < 1 or level > aisle_levels:
                            continue
                        disabled.append(f"{aisle:01d}-{row:01d}-{col:02d}-{level:02d}")
        return disabled

    def _extract_dock_levels_by_col(
        self,
        dock_map_in: Dict[Any, Any],
        dock_map_out: Dict[Any, Any],
        dock_in_col: int,
        dock_out_col: int,
    ) -> Dict[int, List[int]]:
        """
        从时间估算器码头映射构建 {col: [levels...]} 映射
        支持旧版映射（line -> level）和新版映射（line -> {col, level}）
        """
        levels_by_col: Dict[int, set] = {}
        for default_col, dock_map in ((dock_in_col, dock_map_in), (dock_out_col, dock_map_out)):
            if not isinstance(dock_map, dict):
                continue
            for _, val in dock_map.items():
                if isinstance(val, dict):
                    try:
                        col = int(val.get("col", default_col))
                        level = int(val.get("level", 1))
                    except Exception:
                        continue
                else:
                    try:
                        col = int(default_col)
                        level = int(val)
                    except Exception:
                        continue
                levels_by_col.setdefault(col, set()).add(level)
        return {col: sorted(levels) for col, levels in levels_by_col.items()}

    def _normalize_aisle_dimensions(self, raw_dims: Any) -> Dict[int, Dict[str, int]]:
        """标准化巷道维度配置"""
        result: Dict[int, Dict[str, int]] = {}
        if not isinstance(raw_dims, dict):
            return result
        for aisle_raw, dims in raw_dims.items():
            try:
                aisle_id = int(aisle_raw)
            except Exception:
                continue
            if not isinstance(dims, dict):
                continue
            rows = int(dims.get("rows", self.num_rows))
            columns = int(dims.get("columns", self.num_columns))
            levels = int(dims.get("levels", self.num_levels))
            if rows <= 0 or columns <= 0 or levels <= 0:
                continue
            result[aisle_id] = {"rows": rows, "columns": columns, "levels": levels}
        return result

    @staticmethod
    def _normalize_text(v: Any) -> str:
        """标准化文本，去除前后空白"""
        return str(v).strip()

    def _normalize_feature_alias(self, raw_alias: Any) -> Dict[str, List[str]]:
        """
        标准化特征别名配置：
        {
          "skid_state": ["skid_state", "滑橇状态"],
          "skid_type": ["skid_type", "滑橇类型"]
        }
        """
        result: Dict[str, List[str]] = {}
        self._feature_alias_lookup = {}
        if not isinstance(raw_alias, dict):
            return result
        for canonical_raw, alias_vals in raw_alias.items():
            canonical = self._normalize_text(canonical_raw)
            if not canonical:
                continue
            aliases: List[str] = [canonical]
            if isinstance(alias_vals, (list, tuple, set)):
                for x in alias_vals:
                    sx = self._normalize_text(x)
                    if sx and sx not in aliases:
                        aliases.append(sx)
            elif alias_vals is not None:
                sx = self._normalize_text(alias_vals)
                if sx and sx not in aliases:
                    aliases.append(sx)
            result[canonical] = aliases
            for a in aliases:
                self._feature_alias_lookup[self._normalize_text(a).lower()] = canonical
        return result

    def _canonical_feature_key(self, key: Any) -> str:
        """获取规范化的特征键"""
        k = self._normalize_text(key)
        if not k:
            return k
        return self._feature_alias_lookup.get(k.lower(), k)

    def _normalize_feature_dict(self, feats: Any) -> Dict[str, Any]:
        """标准化特征字典"""
        normalized: Dict[str, Any] = {}
        if not isinstance(feats, dict):
            return normalized
        for k, v in feats.items():
            ck = self._canonical_feature_key(k)
            if not ck:
                continue
            if ck not in normalized and v is not None:
                normalized[ck] = v
        return normalized

    def _normalize_aisle_forbidden(self, raw_rules: Any) -> Dict[int, Dict[str, set]]:
        """
        标准化巷道禁止规则配置：
        {
          "1": {"color": "W1", "model": ["A", "B"]},
          "2": {"color": "R1"}
        }
        """
        result: Dict[int, Dict[str, set]] = {}
        if not isinstance(raw_rules, dict):
            return result
        for aisle_raw, rules in raw_rules.items():
            try:
                aisle = int(aisle_raw)
            except Exception:
                continue
            if not isinstance(rules, dict):
                continue
            rule_map: Dict[str, set] = {}
            for k, v in rules.items():
                key = self._canonical_feature_key(k)
                if not key:
                    continue
                if isinstance(v, (list, tuple, set)):
                    vals = {str(x).strip() for x in v if str(x).strip()}
                else:
                    sv = str(v).strip()
                    vals = {sv} if sv else set()
                if vals:
                    rule_map[key] = vals
            if rule_map:
                result[aisle] = rule_map
        return result

    def _normalize_disabled_aisles(self, raw: Any) -> set:
        """标准化禁用巷道列表"""
        result = set()
        if raw is None:
            return result
        if not isinstance(raw, (list, tuple, set)):
            return result
        for x in raw:
            try:
                a = int(x)
            except Exception:
                continue
            if a in self.aisles:
                result.add(a)
        return result

    def _is_aisle_enabled(self, aisle: int) -> bool:
        """检查巷道是否启用"""
        return int(aisle) not in self.disabled_aisles

    def _reassign_pending_inbound_from_disabled_aisles(self) -> None:
        """
        将禁用巷道的待处理入库任务移动到当前启用的、有效的巷道
        """
        for aisle in list(self.disabled_aisles):
            if aisle not in self.pending_inbound_by_aisle:
                continue
            pending = list(self.pending_inbound_by_aisle.get(aisle, []))
            if not pending:
                continue
            self.pending_inbound_by_aisle[aisle] = []
            for task in pending:
                production_line = getattr(task, "production_line", None)
                valid_aisles = self._get_valid_inbound_aisles(task, production_line)
                if valid_aisles:
                    new_aisle = min(valid_aisles, key=lambda a: len(self.pending_inbound_by_aisle.get(a, [])))
                    task.assigned_aisle = new_aisle
                    self.pending_inbound_by_aisle[new_aisle].append(task)
                else:
                    # 没有可用巷道；将任务保留在原始队列中
                    self.pending_inbound_by_aisle[aisle].append(task)

    def set_disabled_aisles(self, aisles: List[int]) -> None:
        """设置禁用巷道"""
        self.disabled_aisles = self._normalize_disabled_aisles(aisles)
        self.active_aisles = [a for a in self.aisles if a not in self.disabled_aisles]
        self._reassign_pending_inbound_from_disabled_aisles()

    def disable_aisle(self, aisle: int) -> None:
        """禁用巷道"""
        try:
            aisle = int(aisle)
        except Exception:
            return
        if aisle not in self.aisles:
            return
        if aisle in self.disabled_aisles:
            return
        self.disabled_aisles.add(aisle)
        self.active_aisles = [a for a in self.aisles if a not in self.disabled_aisles]
        self._reassign_pending_inbound_from_disabled_aisles()

    def enable_aisle(self, aisle: int) -> None:
        """启用巷道"""
        try:
            aisle = int(aisle)
        except Exception:
            return
        if aisle not in self.aisles:
            return
        if aisle in self.disabled_aisles:
            self.disabled_aisles.remove(aisle)
        self.active_aisles = [a for a in self.aisles if a not in self.disabled_aisles]

    def _is_task_forbidden_in_aisle(self, task_or_stub: Any, aisle: int) -> bool:
        """检查任务是否被禁止在指定巷道中执行"""
        rules = self.aisle_forbidden.get(int(aisle), {})
        if not rules:
            return False
        skus = getattr(task_or_stub, "skus", []) or []
        for sku_entry in skus:
            if not isinstance(sku_entry, dict):
                continue
            feats = self._normalize_feature_dict(sku_entry.get("features") or {})
            for fkey, blocked_vals in rules.items():
                if fkey in feats and str(feats.get(fkey)).strip() in blocked_vals:
                    return True
        return False

    def _get_valid_inbound_aisles(self, task_or_stub: Any, production_line: Optional[int]) -> List[int]:
        """获取有效的入库巷道列表"""
        if production_line is not None:
            base = [a for a in self.aisles if production_line in self.aisle_production_line_mapping.get(a, [])]
        else:
            base = list(self.aisles)
        base = [a for a in base if self._is_aisle_enabled(a)]
        valid = [a for a in base if not self._is_task_forbidden_in_aisle(task_or_stub, a)]
        return valid

    def _count_empty_positions_in_aisle(self, aisle: int) -> int:
        """统计巷道中的空仓位数量"""
        cnt = 0
        for p in self.inventory_manager.inventory_positions:
            if p.aisle != aisle:
                continue
            if getattr(p, "disabled", False):
                continue
            try:
                if p.is_empty():
                    cnt += 1
            except Exception:
                continue
        return cnt

    def _count_running_inbound_in_aisle(self, aisle: int) -> int:
        """统计巷道中正在运行的入库任务数量"""
        cnt = 0
        for t in self.running_tasks.values():
            try:
                if t.task_type == TASK_TYPE_INBOUND and int(t.assigned_aisle) == int(aisle):
                    cnt += 1
            except Exception:
                continue
        return cnt

    def _get_projected_free_slots(self, aisle: int) -> int:
        """
        预测空闲槽位数，考虑：
        当前空槽 - 待处理入库队列 - 正在运行的入库任务
        """
        empty_cnt = self._count_empty_positions_in_aisle(int(aisle))
        pending_cnt = len(self.pending_inbound_by_aisle.get(int(aisle), []))
        running_in_cnt = self._count_running_inbound_in_aisle(int(aisle))
        return int(empty_cnt - pending_cnt - running_in_cnt)

    def set_inbound_aisle_allocator(self, allocator: Any):
        """设置入库任务的巷道分配策略
        
        Args:
            allocator: 实现了allocate(task_info, inventory_positions)方法的对象
        """
        self.inbound_aisle_allocator = allocator
        print(f"设置入库巷道分配策略: {allocator.__class__.__name__}")
    
    def set_inbound_position_allocator(self, allocator: Any):
        """设置入库任务的货位分配策略
        
        Args:
            allocator: 实现了allocate(available_positions, task_info)方法的对象
        """
        self.inbound_position_allocator = allocator
        print(f"设置入库货位分配策略: {allocator.__class__.__name__}")
        # 同步到调度器
        if hasattr(self, 'scheduler') and self.scheduler is not None:
            self.scheduler.position_allocator = self.inbound_position_allocator
    
    def set_inbound_strategies(self, inbound_allocation_strategy: Optional[str], inbound_position_strategy: Optional[str]):
        """根据策略字符串在Core内部配置入库巷道/货位分配器"""
        # 巷道分配策略
        if inbound_allocation_strategy:
            from allocation.proposed_strategy import ProposedAisleAllocator
            from allocation.baseline_strategy import BaselineAisleAllocator
            if inbound_allocation_strategy == 'proposed':
                allocator = ProposedAisleAllocator(self)
                # 统一接口
                if not hasattr(allocator, 'allocate') and hasattr(allocator, 'allocate_random'):
                    allocator.allocate = allocator.allocate_random
                self.set_inbound_aisle_allocator(allocator)
                print(f"使用提出策略进行入库巷道分配")
            else:
                # 基线策略
                allocator = BaselineAisleAllocator(self)
                if not hasattr(allocator, 'allocate') and hasattr(allocator, 'allocate_random'):
                    allocator.allocate = allocator.allocate_random
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

    # ===================== 新增：对外统一接口 =====================
    def initialize_core(self, production_plan: Dict[int, List[List[List[str]]]], initial_inventory: Optional[dict] = None, initial_inventory_count: int = 250):
        """用于被外部仿真器调用的核心初始化：库存、生产计划与运行态重置"""
        # 初始化库存
        self.inventory_manager.initialize()
        if initial_inventory:
            # 允许外部直接写入初始库存（可选）
            for aisle, sku_qty in initial_inventory.items():
                for sku_id, qty in sku_qty.items():
                    # 这里不指定具体货位，假设库存管理器支持该接口或由外部已分配
                    try:
                        self.inventory_manager.add_to_any_position(int(aisle), sku_id, qty)  # 如果不存在则忽略
                    except Exception:
                        pass
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

    def apply_task_feedback(self, feedback: dict):
        """
        接收外部真实任务完成信息，更新状态并记录
        预期字段：taskId, taskType, status, durationSeconds/startTime/endTime, aisleId,
                 sourcePosition/targetPosition {aisleId/column/row/level/shelf}, reason
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
        """为未指定巷道的入库任务分配巷道，并返回到达巷道入口的事件(Event)
        接受 TaskData 或 dict({'skus': [...]})"""
        # 兼容两种输入，优先找到首个非空 skuId 决定产线
        skus = task_or_stub.skus
        production_line = getattr(task_or_stub, "production_line", None)
        in_line = getattr(task_or_stub, "in_line", 1)
        if production_line in (None, 0):
            for sku_entry in skus:
                if isinstance(sku_entry, dict):
                    sid = sku_entry.get('skuId')
                else:
                    sid = None
                if sid is not None:
                    production_line = self.sku_to_production_line.get(sid)
                    break
        skid_type = self._extract_task_feature_value(task_or_stub, ["skid_type", "滑橇类型", "skidType"])
        # 巷道分配
        assigned_aisle = None
        valid_aisles = self._get_valid_inbound_aisles(task_or_stub, production_line)
        valid_with_capacity = [a for a in valid_aisles if self._get_projected_free_slots(a) > 0]
        if self.inbound_aisle_allocator is not None:
            # Pass production_line to allocator if available.
            stub = type('InboundStub', (), {'skus': skus, 'production_line': production_line, 'in_line': in_line})()
            assigned_aisle = self.inbound_aisle_allocator.allocate(stub, self.inventory_manager.inventory_positions)
            if assigned_aisle is not None and assigned_aisle not in valid_aisles:
                print(f"[WARN] inbound aisle {assigned_aisle} violates aisle_forbidden, fallback to valid aisles")
                assigned_aisle = None
            elif assigned_aisle is not None and self._get_projected_free_slots(assigned_aisle) <= 0:
                print(f"[WARN] inbound aisle {assigned_aisle} projected full (empty-pending-running<=0), fallback to other aisles")
                assigned_aisle = None
        
        # 如果分配器返回None或未设置分配器，则使用默认策略
        if assigned_aisle is None:
            if valid_with_capacity:
                assigned_aisle = random.choice(valid_with_capacity)
            elif valid_aisles:
                assigned_aisle = random.choice(valid_aisles)
            else:
                print(f"[ERROR] No valid inbound aisle for task due to aisle_forbidden. Fallback to random aisle.")
                fallback_pool = self.active_aisles if self.active_aisles else self.aisles
                assigned_aisle = random.choice(fallback_pool)
        # 生成任务ID（含入库线标记）
        self.task_id_counter[TASK_TYPE_INBOUND] += 1
        try:
            in_line_num = int(in_line)
        except Exception:
            m = re.search(r"(\d+)", str(in_line))
            in_line_num = int(m.group(1)) if m else 1
        task_id = f"IN_IL{in_line_num}_{self.task_id_counter[TASK_TYPE_INBOUND]:05d}"
        # 返回到达入口事件
        arrival_time = current_time + self.transport_delay_s
        inbound_task = TaskData(
            task_id=task_id,
            task_type=TASK_TYPE_INBOUND,
            task_name=task_id,
            skus=skus,
            production_line=production_line,
            in_line=in_line,
            assigned_aisle=assigned_aisle,
        )
        if str(skid_type).strip() == "1":
            print(
                f"[SKID] inbound assign task={task_id} skid_type=1 "
                f"aisle={assigned_aisle} in_line={in_line} production_line={production_line}"
            )
        event_id = f"{EVENT_INBOUND_ARRIVAL_AT_AISLE}_{task_id}"
        return Event(arrival_time, event_id, EVENT_INBOUND_ARRIVAL_AT_AISLE, inbound_task)

    def on_event(self, event: Event, current_time: float, simulation_mode: bool = False) -> List[Event]:
        """处理外部上报事件，返回新产生的事件列表（由Core内部决策）
        
        Args:
            event: 事件对象
            current_time: 当前时间
            simulation_mode: 是否在仿真模式（如calculate_schedule_times中），仿真模式下不会调用decide_for_idle_aisles
        """
        # 更新当前时间
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

        # 从事件队列中移除当前处理的事件
        self.event_queue = [ev for ev in self.event_queue if ev.event_id != event.event_id]
        
        etype = event.event_type
        task = event.task
        new_events: List[Event] = []
        if etype == EVENT_INBOUND_UNASSIGNED:
            # 未分配巷道的入库任务：由Core分配巷道并返回到达事件
            ev = self.allocate_inbound_aisle(task, current_time)
            if ev:
                new_events.append(ev)
                heapq.heappush(self.event_queue, ev)
        elif etype == EVENT_INBOUND_ARRIVAL_AT_AISLE:
            # 入库任务到达巷道入口，加入等待队列
            skid_type = self._extract_task_feature_value(task, ["skid_type", "滑橇类型", "skidType"])
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
                fallback_pool = self.active_aisles if self.active_aisles else self.aisles
                aisle = random.choice(fallback_pool)
            # aisle_forbidden check at arrival-time (final safety guard)
            if self._is_task_forbidden_in_aisle(task, aisle):
                production_line = getattr(task, "production_line", None)
                valid_aisles = self._get_valid_inbound_aisles(task, production_line)
                if valid_aisles:
                    aisle = min(valid_aisles, key=lambda a: len(self.pending_inbound_by_aisle.get(a, [])))
                    task.assigned_aisle = aisle
                else:
                    print(f"[ERROR] Task {task.task_id} blocked by aisle_forbidden for all aisles, keep original aisle={aisle}")
            # Capacity guard at arrival-time:
            # if projected free slots <= 0 after considering pending/running, try reassign; otherwise delay retry.
            if self._get_projected_free_slots(aisle) <= 0:
                production_line = getattr(task, "production_line", None)
                valid_aisles = self._get_valid_inbound_aisles(task, production_line)
                candidates = [a for a in valid_aisles if self._get_projected_free_slots(a) > 0]
                if candidates:
                    aisle = min(candidates, key=lambda a: len(self.pending_inbound_by_aisle.get(a, [])))
                    task.assigned_aisle = aisle
                    print(f"[INFO] inbound {task.task_id} reassign due to projected full: -> aisle {aisle}")
                    if str(skid_type).strip() == "1":
                        print(f"[SKID] inbound reassign task={task.task_id} skid_type=1 aisle={aisle}")
                else:
                    retry_count = int(getattr(task, "_arrival_retry_count", 0)) + 1
                    setattr(task, "_arrival_retry_count", retry_count)
                    if retry_count > max(1, int(self.inbound_arrival_max_retries)):
                        print(
                            f"[WARN] inbound {task.task_id} delay retries exceeded "
                            f"({retry_count-1}/{self.inbound_arrival_max_retries}), stop retry loop"
                        )
                        if str(skid_type).strip() == "1":
                            print(
                                f"[SKID] inbound stop-retry task={task.task_id} "
                                f"skid_type=1 aisle={aisle}"
                            )
                        # In online mode, keep task pending so future task-complete events can still dispatch it.
                        if not simulation_mode:
                            task.pending_enter_time = float(current_time)
                            self.pending_inbound_by_aisle[aisle].append(task)
                        return new_events

                    retry_time = current_time + max(5.0, float(self.transport_delay_s))
                    retry_id = (
                        f"{EVENT_INBOUND_ARRIVAL_AT_AISLE}_{task.task_id}_"
                        f"retry_{retry_count}_{int(retry_time)}"
                    )
                    retry_ev = Event(retry_time, retry_id, EVENT_INBOUND_ARRIVAL_AT_AISLE, task)
                    new_events.append(retry_ev)
                    heapq.heappush(self.event_queue, retry_ev)
                    print(
                        f"[INFO] inbound {task.task_id} delayed: all valid aisles projected full, "
                        f"retry {retry_count}/{self.inbound_arrival_max_retries} at {retry_time:.1f}s"
                    )
                    if str(skid_type).strip() == "1":
                        print(
                            f"[SKID] inbound delayed task={task.task_id} skid_type=1 "
                            f"retry={retry_count}/{self.inbound_arrival_max_retries} retry_at={retry_time:.1f}s"
                        )
                    return new_events
            
            # 添加任务到待处理列表
            task.pending_enter_time = float(current_time)
            self.pending_inbound_by_aisle[aisle].append(task)
            if str(skid_type).strip() == "1":
                print(f"[SKID] inbound queued task={task.task_id} skid_type=1 aisle={aisle} pending_size={len(self.pending_inbound_by_aisle[aisle])}")
            # 任务到达后尝试为空闲巷道派发（非仿真模式）
            if not simulation_mode:
                dispatched = self.decide_for_idle_aisles(current_time, simulation_mode=simulation_mode)
                for ev in dispatched:
                    new_events.append(ev)
                    heapq.heappush(self.event_queue, ev)
        elif etype == EVENT_TASK_COMPLETE:
            # 任务完成，更新库存/生产计划/堵塞
            task_id = task.task_id
            aisle = task.assigned_aisle if task.assigned_aisle else None
            production_line = task.production_line
            out_line = getattr(task, "out_line", None) or production_line
            skus = task.skus
            if task.task_type == TASK_TYPE_OUTBOUND and production_line == 1:
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
                self.awaiting_feedback.add(task_id)
                return new_events
            # 从running移除
            if task_id in self.running_tasks:
                del self.running_tasks[task_id]
            if task.task_type == TASK_TYPE_INBOUND:
                # 入库完成：根据指定位置入库
                if getattr(task, 'positions', None):
                    sku_ids = [s.get('skuId') for s in skus if s.get('skuId') is not None]
                    inventory_added_successfully = False
                    try:
                        for idx, sku_id in enumerate(sku_ids):
                            pos = task.positions[min(idx, len(task.positions)-1)]
                            sku_features = None
                            try:
                                sku_entry = skus[idx] if idx < len(skus) else None
                                if isinstance(sku_entry, dict):
                                    sku_features = sku_entry.get('features')
                            except Exception:
                                sku_features = None
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
                                                layer = 'upper' if idx == 0 else 'lower'
                                            elif pos.row == 2:
                                                layer = 'lower' if idx == 0 else 'upper'
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
                                        layer = 'upper' if idx == 0 else 'lower'
                                elif pos.is_double_layer and len(sku_ids) == 1:
                                    # 单个SKU放入双层货位，检查哪一层是空的
                                    if pos.upper_quantity == 0:
                                        layer = 'upper'
                                    elif pos.lower_quantity == 0:
                                        layer = 'lower'
                                    else:
                                        # 两层都满了，无法放入
                                        raise ValueError(f"位置 {pos.get_position_id()} 的上下层都已有货物")
                                
                                self.inventory_manager.add_inventory(
                                    pos,
                                    sku_id,
                                    1,
                                    layer,
                                    features=sku_features,
                                    in_line=getattr(task, 'in_line', None),
                                    out_line=getattr(task, 'out_line', None),
                                )
                                inventory_added_successfully = True
                            except Exception as e:
                                print(f"[warehouse_core]add_inventory error:{pos} {sku_id}, 错误: {str(e)}")
                                try:
                                    actual_pos = self.inventory_manager.position_map.get(pos.get_position_id())
                                    print(f"[warehouse_core] position_map snapshot: {actual_pos}")
                                except Exception:
                                    pass
                                inventory_added_successfully = False
                                break  # 如果任何一个SKU添加失败，则整个任务失败
                        
                        # 输出当前仓库中梁的详细信息
                        beam_details = self._get_beam_details()
                        if inventory_added_successfully:
                            if beam_details:
                                total_beams = beam_details.pop('total_beams', 0)  # 获取并移除总梁数
                                if beam_details:  # 还有其他SKU信息
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
            elif task.task_type == TASK_TYPE_OUTBOUND:
                # 磁力吊/拥堵处理
                if self.use_magnetic_crane:
                    side = 'left' if aisle in self.left_aisles else 'right'
                    crane_key = (out_line, side)
                    crane_start_time = max(current_time, self.crane_available_times.get(crane_key, 0.0))
                    crane_finish_time = crane_start_time + self.magnetic_crane_time + self.outbound_congestion_time
                    self.crane_available_times[crane_key] = crane_finish_time
                    # 更新堵塞状态
                    self.update_blockage_status(aisle, out_line, blocked=True, unblock_time=crane_finish_time)
                    # 回填记录
                    if getattr(task, 'task_record', None) is not None:
                        task.task_record['crane_start_time'] = crane_start_time
                        task.task_record['un_congested_time'] = crane_start_time + self.magnetic_crane_time
                        task.task_record['crane_finish_time'] = crane_finish_time
                    ev_task = TaskData(task_id=task_id, task_type=TASK_TYPE_OUTBOUND, task_name=task_id, skus=skus, production_line=production_line, out_line=out_line, assigned_aisle=aisle)
                    ev_id = f"{EVENT_CONGESTION_CLEAR}_{task_id}"
                    ev_obj = Event(crane_finish_time, ev_id, EVENT_CONGESTION_CLEAR, ev_task)
                    new_events.append(ev_obj)
                    heapq.heappush(self.event_queue, ev_obj)
                else:
                    # 仅拥堵时间
                    outbound_finish_time = current_time + self.outbound_congestion_time
                    if self.outbound_congestion_time > 0:
                        self.update_blockage_status(aisle, out_line, blocked=True, unblock_time=outbound_finish_time)
                        # 回填记录
                        if getattr(task, 'task_record', None) is not None:
                            task.task_record['un_congested_time'] = outbound_finish_time
                            task.task_record['crane_finish_time'] = outbound_finish_time
                        ev_task = TaskData(task_id=task_id, task_type=TASK_TYPE_OUTBOUND, task_name=task_id, skus=skus, production_line=production_line, out_line=out_line, assigned_aisle=aisle)
                        ev_id = f"{EVENT_CONGESTION_CLEAR}_{task_id}"
                        ev_obj = Event(outbound_finish_time, ev_id, EVENT_CONGESTION_CLEAR, ev_task)
                        new_events.append(ev_obj)
                        heapq.heappush(self.event_queue, ev_obj)
                    else:
                        self.update_blockage_status(aisle, out_line, blocked=False, unblock_time=0.0)
                # 更新当前巷道位置（出库：最后位置）
                if getattr(task, 'positions', None):
                    last_pos = task.positions[-1]
                    # 复制一个位置用于记录当前位置（不影响库存位置对象）
                    try:
                        dock_col, dock_level = self.time_estimator.resolve_outbound_dock(
                            out_line,
                            default_layer=1,
                            aisle=aisle,
                        )
                        cp = InventoryPosition(
                            aisle=last_pos.aisle,
                            row=last_pos.row,
                            column=dock_col,
                            level=dock_level,
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
            # 记录完成
            self.completed_tasks.append(task)
            # 完成后尝试派发（非仿真模式）
            if not simulation_mode:
                dispatched = self.decide_for_idle_aisles(current_time, simulation_mode=simulation_mode)
                for ev in dispatched:
                    new_events.append(ev)
                    heapq.heappush(self.event_queue, ev)
        elif etype == EVENT_CONGESTION_CLEAR:
            task_id = task.task_id
            aisle = task.assigned_aisle
            production_line = task.production_line
            out_line = getattr(task, "out_line", None) or production_line
            if production_line is not None:
                self.mark_outbound_completed(production_line, task, current_time)
            self.update_blockage_status(aisle, out_line, blocked=False, unblock_time=0.0)
            # 拥堵解除后尝试派发（非仿真模式）
            if not simulation_mode:
                dispatched = self.decide_for_idle_aisles(current_time, simulation_mode=simulation_mode)
                for ev in dispatched:
                    new_events.append(ev)
                    heapq.heappush(self.event_queue, ev)
        return new_events

    def decide_for_idle_aisles(self, current_time: float, simulation_mode: bool = False) -> List[Event]:
        """为所有空闲巷道决策下一任务（入/出库），返回产生的任务完成事件列表"""
        self.check_and_relocate_inventory()
        events: List[Event] = []
        # 正在运行的巷道
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
            if not self._is_aisle_enabled(aisle):
                continue
            if self._is_aisle_relocation_busy(aisle, current_time):
                busy_aisles.add(aisle)
        # 预生成可用出库任务（基于当前运行与已完成）
        running_ids = set(self.running_tasks.keys())
        finished_ids = set([t.task_id for t in self.completed_tasks])
        outbound_candidates = self.generate_outbound_tasks(max_tasks_per_line=2, running_task_ids=running_ids, finished_task_ids=finished_ids)
        # 合并到队列（去重）
        existing_ids = set([t.task_id for t in self.pending_outbound_queue])
        for t in outbound_candidates:
            if t.task_id not in existing_ids:
                self.pending_outbound_queue.append(t)

        # 汇总待分配任务
        # 入库：按巷道 + 入库线分桶，各桶取队首，后续调度器再决定先后
        inbound_tasks: List[TaskData] = []
        for a in self.aisles:
            if not self._is_aisle_enabled(a):
                continue
            line_buckets = {}
            for t in self.pending_inbound_by_aisle[a]:
                line = getattr(t, "in_line", 1)
                if line not in line_buckets:
                    line_buckets[line] = t  # 保持队列顺序，遇到第一个即为该线路队首
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
        for aisle in self.aisles:
            if not self._is_aisle_enabled(aisle):
                continue
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
                    paired_position, sku1_positions, sku2_positions = self._check_sku_pairing_status(sku1, sku2)
                    if paired_position is None:
                        self._perform_relocation_if_needed(
                            task_id, production_line, sku1, sku2, sku1_positions, sku2_positions
                        )
                        continue
                match_mode = self._get_outbound_match_mode(production_line)
                if match_mode == "features":
                    self._log_outbound_feature_candidates(task_info)
                out_line = getattr(task_info, "out_line", None) or production_line
                if self.check_blockage(aisle, out_line, current_time=current_time):
                    continue
                # 检查产线组的顺序约束（前面的组是否完成）
                if not self.can_start_outbound_task(task_id, production_line):
                    continue

            task_info.task_record = self.generate_task_record(task_info, current_time)

            # 添加到运行任务字典
            self.running_tasks[task_id] = task_info

            # 从等待队列中移除已开工的任务
            if task_type == TASK_TYPE_OUTBOUND:
                self.pending_outbound_queue = [t for t in self.pending_outbound_queue if t.task_id != task_id]
                # Outbound: remove inventory for matched positions.
                match_mode = self._get_outbound_match_mode(production_line)
                feature_keys = self._get_outbound_match_features(production_line)
                feature_filters = self._extract_feature_filters_from_task(task_info, feature_keys)
                sku_ids_list = [
                    s.get('skuId') if isinstance(s, dict) else getattr(s, 'skuId', None)
                    for s in (task_info.skus or [])
                ]
                for idx, _ in enumerate(sku_ids_list):
                    pos = task_info.positions[min(idx, len(task_info.positions) - 1)]
                    try:
                        sku_to_remove = None
                        if match_mode == "features":
                            target_features = None
                            if feature_filters:
                                target_features = feature_filters[min(idx, len(feature_filters) - 1)]
                            if pos.is_double_layer:
                                if target_features and self.inventory_manager._features_match(pos.lower_features, target_features, feature_keys):
                                    sku_to_remove = pos.lower_sku
                                elif target_features and self.inventory_manager._features_match(pos.upper_features, target_features, feature_keys):
                                    sku_to_remove = pos.upper_sku
                            else:
                                if target_features and self.inventory_manager._features_match(pos.features, target_features, feature_keys):
                                    sku_to_remove = pos.sku
                            if sku_to_remove is None:
                                sku_to_remove = pos.sku or pos.lower_sku or pos.upper_sku
                        else:
                            sku_to_remove = sku_ids_list[idx]
                        if sku_to_remove:
                            self.inventory_manager.remove_inventory(pos, sku_to_remove, 1)
                        else:
                            print("[DEBUG] 扣减库存失败: 未找到可移除的SKU")
                    except Exception:
                        print(f"[DEBUG] 扣减库存失败: {sku_to_remove}")
            else:
                # 入库：在该巷道的等待队列中删除对应task_id
                self.pending_inbound_by_aisle[aisle] = [t for t in self.pending_inbound_by_aisle[aisle] if t.task_id != task_id]

            ev_id = f"{EVENT_TASK_COMPLETE}_{task_info.task_id}"
            ev = Event(task_info.task_record['delivery_time'], ev_id, EVENT_TASK_COMPLETE, task_info)
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
                        out_line = getattr(task_info, "out_line", None) or task_info.production_line
                        if self.check_blockage(aisle, out_line, current_time=current_time):
                            print(f"[DEBUG][dispatch] aisle {aisle}: outbound blocked pl={task_info.production_line}")
                            continue
                        if not self.can_start_outbound_task(task_info.task_id, task_info.production_line):
                            print(f"[DEBUG][dispatch] aisle {aisle}: outbound order blocked task={task_info.task_id}")
                            continue
                    print(
                        f"[DEBUG][dispatch] aisle {aisle}: candidate ready task={task_info.task_id} "
                        f"type={task_info.task_type}"
                    )
            except Exception as e:
                print(f"[DEBUG][dispatch] 生成空派发摘要失败: {e}")

        return events

    def set_production_plan(
        self,
        production_plan: Dict[int, List[List[List[str]]]],
        current_groups: Optional[Dict[int, int]] = None,
    ):
        """设置当日生产计划
        
        Args:
            production_plan: {production_line: [
                [['A1', 'A2'], ['A3', 'A4']],  # 第1组，包含2个task
                [['A1', 'A2'], ['A3', 'A4']],  # 第2组，包含2个task
                ...
            ]}
        """
        normalized_plan = {
            pl: list((production_plan or {}).get(pl, []) or [])
            for pl in range(1, self.num_production_lines + 1)
        }
        for pl, groups in (production_plan or {}).items():
            if pl not in normalized_plan:
                normalized_plan[pl] = list(groups or [])
        self.production_plan = normalized_plan
        # 检查出库匹配特征
        for pl, groups in self.production_plan.items():
            match_features = self._get_outbound_match_features(pl)
            required = [f for f in match_features if str(f).lower() != "rfid"]
            if not required:
                continue
            available_keys = set()
            for group in groups or []:
                for task_skus in group or []:
                    for sku_entry in task_skus or []:
                        if isinstance(sku_entry, dict):
                            feats = sku_entry.get("features") or {}
                            available_keys.update(feats.keys())
            missing = [f for f in required if f not in available_keys]
            if missing:
                print(
                    f"[ERROR] outbound_match_features for production_line {pl} contains "
                    f"unknown feature(s): {missing}. Simulation aborted."
                )
                
        # 重置进度
        for pl in range(1, self.num_production_lines + 1):
            group_count = len(self.production_plan.get(pl, []))
            current_idx = 0
            if current_groups is not None:
                try:
                    current_idx = int(current_groups.get(pl, 0) or 0)
                except Exception:
                    current_idx = 0
            self.production_line_current_group[pl] = max(0, min(current_idx, group_count))
            self.production_line_completed_tasks[pl] = set()
            self.production_line_group_completion_times[pl] = []
        print(f"\n设置生产计划:")
        for pl, groups in self.production_plan.items():
            print(f"  产线{pl}: {len(groups)}组，每组{len(groups[0]) if groups else 0}个task")
    
    def update_blockage_status(self, aisle: int, out_line: int,
                               blocked: bool, unblock_time: float = 0.0):
        """更新(aisle, out_line)的堵塞状态
        
        Args:
            aisle: aisle id
            out_line: outbound dock/level id
            blocked: blocked or not
            unblock_time: estimated unblock time (simulation time)
        """
        self.blockage_status[(aisle, out_line)] = {
            'blocked': blocked,
            'unblock_time': unblock_time
        }
    
    def check_blockage(self, aisle: int, out_line: int, current_time: float = 0.0) -> bool:
        """检查(aisle, out_line)是否被堵塞
        
        Args:
            aisle: aisle id
            out_line: outbound dock/level id
            current_time: current simulation time
            
        Returns:
            True if blocked
        """
        status = self.blockage_status.get((aisle, out_line), {'blocked': False, 'unblock_time': 0.0})
        if status['blocked']:
            # 如果已经到了解除时间，自动解除堵塞
            if current_time >= status['unblock_time']:
                self.update_blockage_status(aisle, out_line, False, 0.0)
                return False
            return True
        return False
    
    def can_start_outbound_task(self, task_id: str, production_line: int) -> bool:
        """检查出库任务是否可以开始（考虑产线组的顺序约束）
        
        Args:
            task_id: 任务ID（格式：OUTBOUND_PL{pl}_GP{group}_{sku1}_{sku2}）
            production_line: 产线号
            
        Returns:
            是否可以开始该任务
        """
        if production_line is None:
            return True
        
        # 从task_id中提取组号
        try:
            parts = task_id.split('_')
            # 格式：OUTBOUND_PL{pl}_GP{group}_{sku1}_{sku2}
            if len(parts) >= 3 and parts[0] == TASK_TYPE_OUTBOUND and parts[1].startswith('PL') and parts[2].startswith('GP'):
                task_group_number = int(parts[2][2:])  # 去掉 'GP'
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
        if task_group_idx > current_group_idx:
            # 前面的组还没完成，不能开始
            return False
        
        return True
    
    def mark_outbound_completed(self, production_line: int, task: TaskData, completion_time: float = 0.0):
        """标记某个出库任务完成（用于跟踪生产计划进度）
        
        Args:
            production_line: 产线号
            task_id: 完成的任务ID（格式：OUT_GROUP_{production_line}_{group_number}_{sku1}_{sku2}）
            completion_time: 完成时间（绝对仿真时间）
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
                    task_group_number = int(parts[2][2:])  # 去掉 'GP'
                    task_group_idx = task_group_number - 1
                else:
                    print(f"警告：无法解析任务ID格式: {task_id}")
                    return
            except (ValueError, IndexError) as e:
                print(f"警告：解析任务ID时出错: {task_id}, 错误: {e}")
                return
        
        # 检查任务所属的组是否有效
        if task_group_idx >= len(self.production_plan[production_line]):
            return True
        
        # 标记task完成（使用任务所属的组，而不是current_group）
        self.production_line_completed_tasks[production_line].add(task_id)
        
        # 获取任务所属的组
        task_group = self.production_plan[production_line][task_group_idx]
        
        # Build task IDs (supports features/dict).
        all_task_ids_in_group = []
        for task_skus in task_group:
            labels = [self._task_sku_label(s) for s in task_skus]
            if len(labels) == 1:
                sku_label = labels[0]
            else:
                sku_label = "_".join(labels)
            all_task_ids_in_group.append(
                f"{TASK_TYPE_OUTBOUND}_PL{production_line}_GP{task_group_idx+1}_{sku_label}"
            )
        
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
        """为指定产线的指定组生成出库任务（辅助方法）"""
        tasks = []
        group = self.production_plan[production_line][group_idx]
        completed_task_ids = self.production_line_completed_tasks[production_line]
        
        tasks_generated = 0
        for task_skus in group:
            if tasks_generated >= max_tasks:
                break
            
            # Build task ID (supports features/dict).
            labels = [self._task_sku_label(s) for s in task_skus]
            if len(labels) == 1:
                sku_label = labels[0]
            else:
                sku_label = "_".join(labels)
            task_id = f"{TASK_TYPE_OUTBOUND}_PL{production_line}_GP{group_idx+1}_{sku_label}"
            
            # 检查是否已经在运行中或已完成
            if task_id in running_task_ids or task_id in finished_task_ids:
                continue
            
            # 检查这个task是否已在当前组的完成列表中（仅对当前组检查）
            if group_idx == self.production_line_current_group[production_line]:
                if task_id in completed_task_ids:
                    continue
            
            # Inventory check for single/double tasks.
            all_skus_available = False
            match_features = self._get_outbound_match_features(production_line)
            feature_mode = self._get_outbound_match_mode(production_line) == "features" and bool(match_features)

            def _count_feature_qty(target_features: dict) -> int:
                if not target_features:
                    return 0
                total = 0
                for pos in self.inventory_manager.inventory_positions:
                    if pos.is_double_layer:
                        if pos.upper_quantity > 0 and self.inventory_manager._features_match(pos.upper_features, target_features, match_features):
                            total += pos.upper_quantity
                        if pos.lower_quantity > 0 and self.inventory_manager._features_match(pos.lower_features, target_features, match_features):
                            total += pos.lower_quantity
                    else:
                        if pos.quantity > 0 and self.inventory_manager._features_match(pos.features, target_features, match_features):
                            total += pos.quantity
                return total

            if feature_mode:
                need = {}
                for sku_entry in task_skus:
                    feats = sku_entry.get('features') if isinstance(sku_entry, dict) else None
                    key = None
                    if feats:
                        key = tuple((k, feats.get(k)) for k in match_features)
                    need[key] = need.get(key, 0) + 1
                enough = True
                debug_need = []
                for key, req in need.items():
                    if key is None:
                        enough = False
                        debug_need.append((key, req, 0))
                        continue
                    features = {k: v for k, v in key}
                    total_qty = _count_feature_qty(features)
                    debug_need.append((features, req, total_qty))
                    if total_qty < req:
                        enough = False
                if not enough:
                    print(f"[DEBUG][generate_outbound] 库存不足，跳过 {task_id} 需求 {debug_need}")
                all_skus_available = enough
            else:
                if len(task_skus) == 1:
                # 单梁任务：只需检查是否有该SKU的库存
                    sku_entry = task_skus[0]
                    if isinstance(sku_entry, dict):
                        sku = sku_entry.get('skuId') or sku_entry.get('rfid') or sku_entry.get('RFID')
                    else:
                        sku = sku_entry
                    if not sku:
                        continue
                    total_qty = sum(
                        self.inventory_manager.current_inventory[aisle].get(sku, 0)
                        for aisle in self.aisles
                    )
                    if total_qty > 0:
                        all_skus_available = True
                else:
                    # 双梁任务：放宽为总量充足即可（由调度/移库决定具体配对位置）
                    need = {}
                    for sku_entry in task_skus:
                        if isinstance(sku_entry, dict):
                            sku = sku_entry.get('skuId') or sku_entry.get('rfid') or sku_entry.get('RFID')
                        else:
                            sku = sku_entry
                        if not sku:
                            continue
                        need[sku] = need.get(sku, 0) + 1
                    enough = True
                    debug_need = []
                    for sku, req in need.items():
                        total_qty = sum(
                            self.inventory_manager.current_inventory[aisle].get(sku, 0)
                            for aisle in self.aisles
                        )
                        debug_need.append((sku, req, total_qty))
                        if total_qty < req:
                            enough = False
                    if not enough:
                        print(f"[DEBUG][generate_outbound] 库存不足，跳过 {task_id} 需求 {debug_need}")
                    all_skus_available = enough

            if not all_skus_available:
                continue
            
            # 创建出库任务（新版字段）
            task = TaskData(
                task_id=task_id,
                task_type=TASK_TYPE_OUTBOUND,
                task_name=task_id,
                production_line=production_line,
                out_line=None,
                skus=self._normalize_task_skus(task_skus)
            )
            # 记录组索引，便于后续完成时准确推进 current_group
            task.group_idx = group_idx
            tasks.append(task)
            tasks_generated += 1
        
        return tasks
    
    def generate_outbound_tasks(self, max_tasks_per_line: int = 2, 
                               running_task_ids: set = None,
                               finished_task_ids: set = None) -> List[TaskData]:
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
            if current_group_idx >= len(self.production_plan[production_line]):
                continue  # 该产线所有组都已完成
            
            # 检查当前组的所有task是否都在running或finished中
            current_group = self.production_plan[production_line][current_group_idx]
            current_group_task_ids = []
            for task_skus in current_group:
                labels = [self._task_sku_label(s) for s in task_skus]
                if len(labels) == 1:
                    sku_label = labels[0]
                else:
                    sku_label = "_".join(labels)
                task_id = f"{TASK_TYPE_OUTBOUND}_PL{production_line}_GP{current_group_idx+1}_{sku_label}"
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
    
    def calculate_schedule_times(self, aisle_task_sequences: Dict[int, List[TaskData]]) -> Tuple[float, dict]:
        """
        事件驱动仿真：根据当前状态与调度器顺序推进事件队列，返回完工时间与详情
        不需要重复分配task，就是把self.event_queue和aisle_task_sequences执行完就行
        执行过程中可能需要添加task_complete与拥堵状态更新的event，直到event空结束
        """
        start_time = self.current_time
        
        # 追踪每个巷道已经分配到第几个任务（索引）
        aisle_task_index = {aisle: 0 for aisle in aisle_task_sequences.keys()}
        tasks_num = sum(len(task_sequence) for task_sequence in aisle_task_sequences.values())
        
        def try_dispatch_tasks_from_sequences():
            """尝试从aisle_task_sequences中为空闲巷道分配下一个任务"""
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
                    continue  # 巷道忙碌，跳过
                
                # 获取当前应该分配的任务索引
                task_idx = aisle_task_index[aisle]
                if task_idx >= len(task_sequence):
                    continue  # 该巷道的所有任务都已分配
                
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
                        paired_position, sku1_positions, sku2_positions = self._check_sku_pairing_status(sku1, sku2)
                        if paired_position is None:
                            self._perform_relocation_if_needed(
                                task_id, production_line, sku1, sku2, sku1_positions, sku2_positions
                            )
                            continue
                    out_line = getattr(task_info, "out_line", None) or production_line
                    if self.check_blockage(aisle, out_line, current_time=self.current_time):
                        continue  # 被拥堵阻塞，暂不开始
                    # 检查产线组的顺序约束（前面的组是否完成）
                    if not self.can_start_outbound_task(task_id, production_line):
                        continue  # 前面的组还没完成，不能开始
                
                # 生成任务记录
                task_info.task_record = self.generate_task_record(task_info, self.current_time)
                
                # 添加到运行任务
                self.running_tasks[task_id] = task_info
                busy_aisles.add(aisle)  # 标记巷道为忙碌
                
                # 从等待队列中移除（如果存在）
                if task_type == TASK_TYPE_OUTBOUND:
                    self.pending_outbound_queue = [t for t in self.pending_outbound_queue if t.task_id != task_id]
                    # 出库：立即扣减库存
                    for idx, pos in enumerate(task_info.positions or []):
                        sku = self._resolve_outbound_sku(task_info, idx)
                        if not sku:
                            continue
                        try:
                            self.inventory_manager.remove_inventory(pos, sku, 1)
                        except Exception:
                            pass
                else:  # 入库任务
                    # 确保只从该任务被分配到的巷道的等待队列中移除
                    self.pending_inbound_by_aisle[aisle] = [t for t in self.pending_inbound_by_aisle[aisle] if t.task_id != task_id]
                
                # 创建任务完成事件
                ev_id = f"{EVENT_TASK_COMPLETE}_{task_info.task_id}"
                ev = Event(task_info.task_record['delivery_time'], ev_id, EVENT_TASK_COMPLETE, task_info)
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

        for t in self.completed_tasks[-tasks_num:]:
            rec = t.task_record or {}
            pl = t.production_line
            aisle = t.assigned_aisle
            if aisle in aisle_schedules:
                aisle_schedules[aisle].append(rec)
                aisle_completion_times[aisle] = max(aisle_completion_times[aisle], rec['delivery_time'])
            if t.task_type == TASK_TYPE_OUTBOUND:
                if rec['delivery_time'] is not None:
                    production_line_times[pl]['delivery_time'] = max(production_line_times[pl]['delivery_time'], rec['delivery_time'])
                if rec['crane_finish_time'] is not None:
                    production_line_times[pl]['crane_finish_time'] = max(production_line_times[pl]['crane_finish_time'], rec['crane_finish_time'])
            elif t.task_type == TASK_TYPE_INBOUND:
                try:
                    st = rec.get('start_time', None)
                    if st is None:
                        continue
                    enqueue_ts = getattr(t, 'pending_enter_time', None)
                    if enqueue_ts is None:
                        # Fallback for older tasks that do not carry pending timestamp.
                        enqueue_ts = getattr(t, 'assigned_time', None)
                    if enqueue_ts is None:
                        enqueue_ts = start_time
                    inbound_wait_time += max(0.0, float(st) - float(enqueue_ts))
                except Exception:
                    pass

        makespan = max(aisle_completion_times.values())-start_time if aisle_completion_times else last_time - start_time
        detailed_schedule = {
            'aisle_schedules': aisle_schedules,
            'production_line_times': production_line_times,
            'aisle_completion_times': aisle_completion_times,
            'inbound_wait_time': inbound_wait_time,
        }
        return makespan, detailed_schedule
    
    def get_current_balance(self) -> float:
        """获取当前库存均衡度"""
        return self.metrics_calculator.calculate_distribution_balance(
            self.inventory_manager.current_inventory
        )

    def _get_outbound_match_features(self, production_line: Optional[int]) -> List[str]:
        if production_line in (None, 0):
            return list(self.outbound_match_features_default or [])
        if self.outbound_match_features_by_line and production_line in self.outbound_match_features_by_line:
            return list(self.outbound_match_features_by_line.get(production_line, []) or [])
        return list(self.outbound_match_features_default or [])

    def _extract_feature_filters_from_task(self, task_info: TaskData, feature_keys: List[str]) -> List[dict]:
        feature_keys = [self._canonical_feature_key(k) for k in (feature_keys or [])]
        filters = []
        for s in (task_info.skus or []):
            if not isinstance(s, dict):
                continue
            feats = self._normalize_feature_dict(s.get("features"))
            if not feats and feature_keys:
                feats = {}
                for raw_k, raw_v in s.items():
                    ck = self._canonical_feature_key(raw_k)
                    if ck in feature_keys and raw_v is not None:
                        feats[ck] = raw_v
            if feats:
                filters.append(feats)
        return filters

    def _extract_task_feature_value(self, task_or_stub: Any, keys: List[str]) -> Optional[str]:
        canonical_keys = [self._canonical_feature_key(k) for k in (keys or [])]
        for s in (getattr(task_or_stub, "skus", None) or []):
            if not isinstance(s, dict):
                continue
            feats = self._normalize_feature_dict(s.get("features"))
            for k in canonical_keys:
                if k in feats and feats.get(k) is not None:
                    return str(feats.get(k)).strip()
            for k in canonical_keys:
                # fallback: top-level keys in sku dict can also use alias
                for raw_k, raw_v in s.items():
                    if self._canonical_feature_key(raw_k) == k and raw_v is not None:
                        return str(raw_v).strip()
        return None

    @staticmethod
    def _format_feature_label(features: dict) -> str:
        if not features:
            return "未知"
        if len(features) == 1:
            k, v = next(iter(features.items()))
            if str(k).lower() == "color":
                return str(v)
            return f"{k}={v}"
        return ";".join([f"{k}={v}" for k, v in features.items()])

    def _log_outbound_feature_candidates(self, task_info: TaskData) -> None:
        production_line = task_info.production_line
        feature_keys = self._get_outbound_match_features(production_line)
        filters = self._extract_feature_filters_from_task(task_info, feature_keys)
        if not filters:
            return
        for filt in filters:
            skus = set()
            total_qty = 0
            for pos in self.inventory_manager.inventory_positions:
                if pos.is_double_layer:
                    if pos.upper_quantity > 0 and self.inventory_manager._features_match(pos.upper_features, filt, feature_keys):
                        if pos.upper_sku:
                            skus.add(pos.upper_sku)
                        total_qty += pos.upper_quantity
                    if pos.lower_quantity > 0 and self.inventory_manager._features_match(pos.lower_features, filt, feature_keys):
                        if pos.lower_sku:
                            skus.add(pos.lower_sku)
                        total_qty += pos.lower_quantity
                else:
                    if pos.quantity > 0 and self.inventory_manager._features_match(pos.features, filt, feature_keys):
                        if pos.sku:
                            skus.add(pos.sku)
                        total_qty += pos.quantity
            label = self._format_feature_label(filt)
            sku_text = "，".join(sorted(skus)) if skus else "无"
            print(f"[INFO] 出库匹配 (qty={total_qty})")

    def _get_outbound_match_mode(self, production_line: Optional[int]) -> str:
        features = self._get_outbound_match_features(production_line)
        # If features list includes rfid, treat as RFID mode.
        for f in features:
            if str(f).lower() == "rfid":
                return "rfid"
        return "features" if features else "rfid"
    
    def calculate_comprehensive_score(self, makespan: float, detailed_schedule: dict,
                                     balance_before: float,
                                     start_time: float,
                                     makespan_weight: float = 0.0,
                                     balance_weight: float = 0.0001,
                                     production_line_avg_time_weight: float = 1,
                                     production_line_balance_weight: float = 1,
                                     aisle_dispersion_weight: float = 1,
                                     inbound_wait_weight: float = 0.01) -> Tuple[float, dict]:
        """计算综合评分（越小越好）
        
        综合考虑：
        1. makespan（完工时间）
        2. 库存均衡度变化（如果均衡度变差则惩罚）
        3. 产线平均完成时间（各产线完成最后一个任务的平均时间，越小越好）
        
        Args:
            makespan: 调度方案的makespan
            detailed_schedule: calculate_schedule_times返回的详细调度信息
            balance_before: 调度前的库存均衡度
            makespan_weight: makespan权重
            balance_weight: 均衡度变化权重
            production_line_avg_time_weight: 产线平均完成时间权重
            
        Returns:
            (综合score, 详细信息字典)
        """
        # 1. makespan部分
        makespan_score = makespan * makespan_weight
        
        # 2. 库存均衡度变化（均衡度变差则惩罚）
        balance_after = self.get_current_balance()
        balance_change = balance_after - balance_before  # 正值表示变好，负值表示变差
        # 如果变差，则惩罚；如果变好，不奖励（保持0）
        balance_penalty = max(0, -balance_change) * balance_weight * 1000  # 放大到可比尺度
        
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
        if len(line_progress) > 1:
            mean_prog = sum(line_progress) / len(line_progress)
            balance_variance = sum((p - mean_prog) ** 2 for p in line_progress) / len(line_progress)
        production_line_balance_penalty = balance_variance * production_line_balance_weight

        # 5. 巷道分散度（巷道任务数的方差，包含空巷道）
        aisle_counts = [len(tasks) for tasks in detailed_schedule['aisle_schedules'].values()]
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
            'total_score': total_score,
            'makespan_score': makespan_score,
            'balance_penalty': balance_penalty,
            'production_line_avg_score': production_line_avg_score,
            'balance_before': balance_before,
            'balance_after': balance_after,
            'balance_change': balance_change,
            'avg_production_line_time': avg_production_line_time,
            'production_line_balance_penalty': production_line_balance_penalty,
            'aisle_dispersion_penalty': aisle_dispersion_penalty,
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
        aisle = task_info.assigned_aisle
        task_type = task_info.task_type
        
        # 获取当前巷道位置
        current_position = self.current_position_by_aisle.get(aisle)
        
        # 使用时间估算器计算任务持续时间
        if task_type == TASK_TYPE_OUTBOUND:
            # 出库任务 
            duration = self.time_estimator.estimate_outbound_time(
                source_position=task_info.positions,
                skus=task_info.skus,
                production_line=(getattr(task_info, "out_line", None) or task_info.production_line),
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
    
    def _save_simulation_state(self, affected_position_ids: set = None) -> dict:
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
        """恢复仿真状态（用于get_sol_score）"""
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
        self.inventory_manager.current_inventory = saved_state['inventory_state']
        self.inventory_manager.inventory_positions = saved_state['inventory_positions']
        
        # 其他运行时属性
        if 'inbound_only_seconds' in saved_state:
            self.inbound_only_seconds = saved_state['inbound_only_seconds']
        
        # 当我们替换了 inventory_positions 列表时，需要重建 position_map 与 sku_position_index
        try:
            pm = {}
            sku_index = {}
            for p in self.inventory_manager.inventory_positions:
                pid = p.get_position_id()
                pm[pid] = p
                if p.is_double_layer:
                    if getattr(p, 'upper_sku', None):
                        sku_index.setdefault(p.upper_sku, []).append(p)
                    if getattr(p, 'lower_sku', None):
                        sku_index.setdefault(p.lower_sku, []).append(p)
                else:
                    if getattr(p, 'sku', None):
                        sku_index.setdefault(p.sku, []).append(p)
            self.inventory_manager.position_map = pm
            self.inventory_manager.sku_position_index = sku_index
        except Exception:
            pass

    def clone_for_simulation(self, aisle_task_sequences: Dict[int, List[TaskData]] = None) -> 'WarehouseCore':
        """为仿真评分构造一个独立的 WarehouseCore 副本并注入当前状态。

        返回的副本在内存上与主对象完全隔离，后续在副本上运行的任何修改都不会影响主对象。
        
        Args:
            aisle_task_sequences: 可选，巷道任务序列。如果提供，只对涉及的库存位置进行深拷贝以节省时间和空间。
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
            use_magnetic_crane=self.use_magnetic_crane,
            outbound_congestion_time=self.outbound_congestion_time,
            aisle_production_line_mapping=deepcopy(self.aisle_production_line_mapping),
            lr_balance_weight=self.lr_balance_weight,
            scheduler_type=self.scheduler_type,
            inbound_aisle_strategy=None,
            inbound_allocation_strategy=None,
            initial_inventory_count=self.initial_inventory_count,
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
        """
        获取仓库中梁的总数
        
        Returns:
            int: 仓库中梁的总数
        """
        total_beams = 0
        for aisle in self.inventory_manager.aisles:
            total_beams += sum(self.inventory_manager.current_inventory[aisle].values())
        return total_beams

    def _get_beam_details(self) -> dict:
        """
        获取仓库中梁的详细信息，包括每种SKU及其数量（数量为0的不包含在内）
        
        Returns:
            dict: 包含每种SKU及其数量的字典
        """
        beam_details = {}
        try:
            # 使用current_inventory进行统计
            for aisle in self.inventory_manager.aisles:
                for sku, quantity in self.inventory_manager.current_inventory[aisle].items():
                    if quantity > 0:
                        beam_details[sku] = beam_details.get(sku, 0) + quantity
            
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
        """获取累计移库数"""
        return self._relocation_count

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
            if self._get_outbound_match_mode(production_line) == "features":
                continue
            
            current_group = self.production_plan[production_line][current_group_idx]
            completed_task_ids = self.production_line_completed_tasks[production_line]
            running_task_ids = set(self.running_tasks.keys())
            
            # 遍历当前组的每个task
            for task_skus in current_group:
                # Build task ID (supports features/dict).
                labels = [self._task_sku_label(s) for s in task_skus]
                if len(labels) == 1:
                    sku_label = labels[0]
                else:
                    sku_label = "_".join(labels)
                task_id = f"{TASK_TYPE_OUTBOUND}_PL{production_line}_GP{current_group_idx+1}_{sku_label}"
                
                # 跳过已完成或正在运行的任务
                if task_id in completed_task_ids or task_id in running_task_ids:
                    continue
                if task_id in completed_task_ids_all:
                    continue
                if self._is_task_pending_completion(task_id):
                    continue
                if task_id in self.relocation_task_ids:
                    continue
                
                # 只处理双梁任务（需要两个SKU配对出库）
                if len(task_skus) != 2:
                    continue
                
                sku1, sku2 = task_skus[0], task_skus[1]
                
                # 检查事件队列中是否有与该出库任务相关的入库任务
                has_related_inbound = self._has_related_inbound_in_queue(sku1, sku2)
                if has_related_inbound:
                    continue  # 有相关入库任务，不需要移库
                
                # 检查库存配对情况
                paired_position, sku1_positions, sku2_positions = self._check_sku_pairing_status(sku1, sku2)
                
                if paired_position is not None:
                    continue  # 已有配对好的货位，不需要移库
                
                # 判断移库原因和执行移库
                relocation_result = self._perform_relocation_if_needed(
                    task_id, production_line, sku1, sku2, sku1_positions, sku2_positions
                )
                
                if relocation_result is not None:
                    if relocation_result.get("relocation_details", {}).get("operations"):
                        self.relocation_task_ids.add(task_id)
                    relocation_records.append(relocation_result)
        
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
        """检查该出库任务是否已经进入 task_complete 事件队列但尚未拥堵清除。"""
        if not task_id:
            return False
        for ev in self.event_queue:
            if ev.event_type == EVENT_TASK_COMPLETE and getattr(ev.task, "task_id", None) == task_id:
                return True
        return False

    def _check_sku_pairing_status(self, sku1: str, sku2: str) -> Tuple[Optional[InventoryPosition], List[Tuple[str, InventoryPosition]], List[Tuple[str, InventoryPosition]]]:
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
        if sku1 == "2801022-TG360":
            print("sku1:", sku1)

        paired_position = None
        sku1_positions = []
        sku2_positions = []
        
        for pos in self.inventory_manager.inventory_positions:
            if pos.is_double_layer:
                # 检查是否已配对（两个SKU在同一货位的上下层且都有库存）
                pos_skus = [pos.upper_sku, pos.lower_sku]
                if set(pos_skus) == {sku1, sku2}:
                    if pos.upper_quantity > 0 and pos.lower_quantity > 0:
                        paired_position = pos
                        break
                
                # 记录只有单个SKU的位置
                if pos.upper_sku == sku1 and pos.upper_quantity > 0:
                    sku1_positions.append(('upper', pos))
                if pos.lower_sku == sku1 and pos.lower_quantity > 0:
                    sku1_positions.append(('lower', pos))
                if pos.upper_sku == sku2 and pos.upper_quantity > 0:
                    sku2_positions.append(('upper', pos))
                if pos.lower_sku == sku2 and pos.lower_quantity > 0:
                    sku2_positions.append(('lower', pos))
        
        return paired_position, sku1_positions, sku2_positions

    def _perform_relocation_if_needed(self, task_id: str, production_line: int,
                                       sku1: str, sku2: str,
                                       sku1_positions: List[Tuple[str, InventoryPosition]],
                                       sku2_positions: List[Tuple[str, InventoryPosition]]) -> Optional[dict]:
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
            total_beams = len(sku1_positions)  # sku1_positions 与 sku2_positions 相同
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
            return self._relocate_to_pair(task_id, production_line, sku1, sku2, 
                                          sku1_positions, sku2_positions, reason='unpaired')
        
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
                          reason: str) -> Optional[dict]:
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
            'operations': []
        }
        relocation_aisles: set = set()

        # 当前正在执行的任务所占用的巷道，避免与之冲突
        busy_aisles = {
            t.assigned_aisle for t in self.running_tasks.values()
            if getattr(t, 'assigned_aisle', None) is not None
        }

        def _has_conflict(*positions):
            aisles = {p.aisle for p in positions if p is not None}
            conflict = bool(busy_aisles & aisles)
            if conflict:
                print(f"[relocation skip] task {task_id}: aisles {aisles} busy with running tasks, defer relocation ({reason})")
            return conflict

        def _fmt(pos_list):
            return [(layer, pos.get_position_id()) for layer, pos in pos_list]

        def _defer_result(status_note):
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

        # same SKU: require at least two distinct beams (position + layer) to proceed
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
                self._reserve_position(target_pos, task_id)
                self._schedule_relocation_ops(
                    target_pos.aisle,
                    tgt_end,
                    [{"action": "add", "sku": sku2, "pos_id": target_pos.get_position_id(), "layer": "lower", "task_id": task_id}],
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

                print(f"[移库] 任务{task_id}: 将{sku2}从{src_pos2.get_position_id()}:{src_layer2}移到{target_pos.get_position_id()}:lower与{sku1}配对，用时{self.relocation_delay_s}s")

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
                self._reserve_position(target_pos, task_id)
                self._schedule_relocation_ops(
                    target_pos.aisle,
                    tgt_end,
                    [{"action": "add", "sku": sku1, "pos_id": target_pos.get_position_id(), "layer": "lower", "task_id": task_id}],
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

                print(f"[移库] 任务{task_id}: 将{sku1}从{src_pos1.get_position_id()}:{src_layer1}移到{target_pos.get_position_id()}:lower与{sku2}配对，用时{self.relocation_delay_s}s")

                return {
                    'task_id': task_id,
                    'task_skus': [sku1, sku2],
                    'production_line': production_line,
                    'reason': reason,
                    'relocation_details': relocation_details
                }
            except Exception as e:
                print(f"[移库失败] 任务{task_id}: {e}")

        
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
        """标记给定巷道在指定时间段因移库占用，期间不派发新任务"""
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
        """检查巷道在当前时间是否因移库占用"""
        intervals = self.relocation_busy_intervals.get(aisle, [])
        # 清理过期的区间
        self.relocation_busy_intervals[aisle] = [(s, e) for s, e in intervals if e > current_time]
        return any(s <= current_time < e for s, e in self.relocation_busy_intervals[aisle])

    def _get_relocation_wait_offset(self, aisles: set) -> float:
        """计算移库需要等待的时间（若目标/来源巷道当前繁忙，则等待到空闲）"""
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
        pos_id = pos.get_position_id()
        pos.reserved = True
        self.relocation_reserved_positions[pos_id] = task_id

    def _release_reserved_position(self, pos_id: str) -> None:
        task_id = self.relocation_reserved_positions.pop(pos_id, None)
        pos = self.inventory_manager.position_map.get(pos_id)
        if pos is not None:
            pos.reserved = False
        return task_id

    def _schedule_relocation_ops(self, aisle: int, end_time: float, ops: List[dict]) -> None:
        if aisle not in self.relocation_ops_by_aisle:
            self.relocation_ops_by_aisle[aisle] = []
        self.relocation_ops_by_aisle[aisle].append((end_time, ops))

    def _get_next_relocation_op_time(self) -> Optional[float]:
        next_time = None
        for entries in self.relocation_ops_by_aisle.values():
            for end_time, _ in entries:
                if next_time is None or end_time < next_time:
                    next_time = end_time
        return next_time

    def _apply_relocation_ops(self, current_time: float) -> None:
        for aisle, entries in list(self.relocation_ops_by_aisle.items()):
            if not entries:
                continue
            pending = []
            for end_time, ops in entries:
                if end_time <= current_time:
                    for op in ops:
                        action = op.get("action")
                        sku = op.get("sku")
                        pos_id = op.get("pos_id")
                        layer = op.get("layer")
                        task_id = op.get("task_id")
                        if not (action and sku and pos_id):
                            continue
                        pos = self.inventory_manager.position_map.get(pos_id)
                        if not pos:
                            print(f"[relocation-audit] apply t={current_time:.2f}s task={task_id} "
                                  f"action={action} sku={sku} pos={pos_id} result=missing_pos")
                            if action == "add":
                                self._release_reserved_position(pos_id)
                            continue
                        try:
                            if action == "remove":
                                self.inventory_manager.remove_inventory(pos, sku, 1)
                            elif action == "add":
                                self.inventory_manager.add_inventory(pos, sku, 1, layer)
                            print(f"[relocation-audit] apply t={current_time:.2f}s task={task_id} "
                                  f"action={action} sku={sku} pos={pos_id} result=ok")
                        except Exception as e:
                            print(f"[relocation-audit] apply t={current_time:.2f}s task={task_id} "
                                  f"action={action} sku={sku} pos={pos_id} result=fail err={e}")
                        finally:
                            if action == "add":
                                self._release_reserved_position(pos_id)
                else:
                    pending.append((end_time, ops))
            self.relocation_ops_by_aisle[aisle] = pending

    def _is_task_relocation_ready(self, task_id: str, current_time: float) -> bool:
        """Return True if the task can start after relocation."""
        if not task_id:
            return True
        ready_time = self.relocation_task_ready_time.get(task_id)
        if ready_time is None:
            return True
        if current_time >= ready_time:
            self.relocation_task_ready_time.pop(task_id, None)
            return True
        return False

    def _resolve_outbound_sku(self, task_info: TaskData, idx: int) -> Optional[str]:
        try:
            sku_entry = task_info.skus[idx] if task_info.skus and idx < len(task_info.skus) else None
        except Exception:
            sku_entry = None
        sku_id = None
        if isinstance(sku_entry, dict):
            sku_id = sku_entry.get('skuId') or sku_entry.get('rfid') or sku_entry.get('RFID')
        elif sku_entry is not None:
            sku_id = getattr(sku_entry, 'skuId', None)
        match_mode = self._get_outbound_match_mode(getattr(task_info, "production_line", None))
        if sku_id and match_mode != 'features':
            return sku_id
        try:
            pos = task_info.positions[idx] if task_info.positions and idx < len(task_info.positions) else None
        except Exception:
            pos = None
        if pos is None:
            return None
        if getattr(pos, 'is_double_layer', False):
            if getattr(pos, 'upper_quantity', 0) > 0 and getattr(pos, 'upper_sku', None):
                return pos.upper_sku
            if getattr(pos, 'lower_quantity', 0) > 0 and getattr(pos, 'lower_sku', None):
                return pos.lower_sku
            return None
        return getattr(pos, 'sku', None)
    def _task_sku_label(self, sku_entry: Any) -> str:
        if isinstance(sku_entry, dict):
            sku_id = sku_entry.get('skuId') or sku_entry.get('rfid') or sku_entry.get('RFID')
            if sku_id:
                return str(sku_id)
            feats = sku_entry.get('features') or {}
            # Prefer actual feature keys on the entry to avoid misleading defaults.
            keys = sorted(feats.keys()) if feats else (self.outbound_match_features_default or [])
            parts = [f"{k}={feats.get(k)}" for k in keys]
            return "F[" + "|".join(parts) + "]"
        return str(sku_entry)

    def _normalize_task_skus(self, task_skus: List[Any]) -> List[dict]:
        skus_list = []
        for sku in task_skus:
            if isinstance(sku, dict):
                sku_dict = dict(sku)
                sku_dict.setdefault('quantity', 1)
                if 'skuId' not in sku_dict:
                    sku_dict['skuId'] = sku_dict.get('rfid') or sku_dict.get('RFID')
                skus_list.append(sku_dict)
            else:
                skus_list.append({'skuId': sku, 'quantity': 1})
        return skus_list


    def _has_running_task_with_conflicting_sku(self, skus: List[str], aisles: set) -> bool:
        target = {s for s in skus if s}  # 去掉 None/空
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

