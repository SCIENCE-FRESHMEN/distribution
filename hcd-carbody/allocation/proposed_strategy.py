from typing import List, Dict, Optional, Any, Set
from collections import deque
import os
from simulation.task_data import TaskData
from simulation.position import InventoryPosition
from estimate.time_estimator import load_time_estimator_config


# ==========================================
# 1. 巷道分配器 (ProposedAisleAllocator)
# ==========================================
class ProposedAisleAllocator:
    """
    提出的巷道分配策略核心：
    - 全局混存：不同产线可进入同一巷道，提高空间利用率。
    - 局部均衡：同一产线的货物尽可能均匀分布在不同巷道，避免集中。
    - 计划驱动：SKU与产线的关系、任务紧急度均从 production_plan 动态获取。
    """
    def __init__(self, warehouse_core):
        """
        初始化巷道分配器
        
        Args:
            warehouse_core: 仓库核心组件，提供仓库的基本信息和状态
        """
        self.warehouse_core = warehouse_core
        self.aisles = warehouse_core.aisles  # 巷道列表
        self._decision_count = 0  # 决策计数器
        aisle_cnt = max(1, len(self.aisles))  # 巷道数量，至少为1
        
        def _env_int(name: str, default: int) -> int:
            """
            从环境变量获取整数值，如果不存在则返回默认值
            
            Args:
                name: 环境变量名称
                default: 默认值
                
            Returns:
                环境变量对应的整数值
            """
            try:
                return int(os.getenv(name, str(default)))
            except Exception:
                return default

        def _env_float(name: str, default: float) -> float:
            """
            从环境变量获取浮点数值，如果不存在则返回默认值
            
            Args:
                name: 环境变量名称
                default: 默认值
                
            Returns:
                环境变量对应的浮点数值
            """
            try:
                return float(os.getenv(name, str(default)))
            except Exception:
                return default
        
        # 向前看的出库负载（未来n个任务），内部参数
        self._future_n = _env_int("PROPOSED_FUTURE_N", aisle_cnt)  # 未来考虑的任务数量
        self._future_weight = _env_float("PROPOSED_FUTURE_WEIGHT", float(aisle_cnt))  # 未来负载权重
        self._future_tail_weight = _env_float("PROPOSED_FUTURE_TAIL_WEIGHT", 0 * float(aisle_cnt))  # 未来尾部权重
        self._feature_weight = _env_float("PROPOSED_FEATURE_WEIGHT", float(aisle_cnt))  # 特征权重
        self._pending_weight = _env_float("PROPOSED_PENDING_WEIGHT", 2 * float(aisle_cnt))  # 待处理任务权重
        self._recent_weight = _env_float("PROPOSED_RECENT_WEIGHT", 2 * float(aisle_cnt))  # 最近选择权重
        
        # 容量感知控制，避免小巷道过度填充
        self._capacity_ratio_weight = _env_float("PROPOSED_CAPACITY_RATIO_WEIGHT", 30.0)  # 容量比例权重
        
        # 创建调试日志文件
        self._debug_file = open('logs/proposed_allocation_debug.log', 'w', encoding='utf-8')
        
        # 最近选择窗口大小
        self._recent_select_window = _env_int("PROPOSED_RECENT_SELECT_WINDOW", aisle_cnt)
        # 最近选择的巷道记录（使用双端队列，具有固定最大长度）
        self._recent_selected_aisles = deque(maxlen=self._recent_select_window)

    def _compute_usable_capacity_by_aisle(self, inventory_positions: List[Any]) -> Dict[int, int]:
        """
        计算每个巷道的可用容量（排除禁用位置）。
        这是容量比率的分母，用于规范化异构巷道尺寸。
        
        Args:
            inventory_positions: 库存位置列表
            
        Returns:
            包含每个巷道可用容量的字典
        """
        capacity = {aisle: 0 for aisle in self.aisles}
        for pos in inventory_positions:
            if getattr(pos, "disabled", False):
                continue  # 跳过禁用的位置
            capacity[pos.aisle] += 1  # 累加巷道容量
        return capacity

    def _extract_feature_filters(self, task_info: Any, feature_keys: List[str]) -> List[dict]:
        """
        从任务信息中提取特征过滤器
        
        Args:
            task_info: 任务信息
            feature_keys: 特征键列表
            
        Returns:
            特征过滤器列表
        """
        filters = []
        for s in getattr(task_info, "skus", []) or []:
            if not isinstance(s, dict):
                continue
            feats = s.get("features")
            if not feats and feature_keys:
                # 如果没有特征但有特征键，则从SKU中提取
                feats = {k: s.get(k) for k in feature_keys if k in s}
            if feats:
                filters.append(feats)
        return filters

    def _compute_feature_aisle_loads(self, task_info: Any) -> Dict[int, float]:
        """
        计算每个巷道的特征负载
        
        Args:
            task_info: 任务信息
            
        Returns:
            包含每个巷道特征负载的字典
        """
        loads = {aisle: 0.0 for aisle in self.aisles}
        inventory = getattr(self.warehouse_core, "inventory_manager", None)
        if not inventory:
            return loads
        
        # 获取任务关联的产线
        production_line = getattr(task_info, "production_line", None) or 1
        # 获取匹配特征键
        feature_keys = self.warehouse_core._get_outbound_match_features(production_line)
        # 提取特征过滤器
        filters = self._extract_feature_filters(task_info, feature_keys)
        if not feature_keys or not filters:
            return loads
        
        target = filters[0]  # 目标特征
        # 遍历所有库存位置
        for pos in inventory.inventory_positions:
            if pos.is_double_layer:  # 如果是双层位置
                # 检查上层是否有匹配特征的货物
                if pos.upper_quantity > 0 and inventory._features_match(pos.upper_features, target, feature_keys):
                    loads[pos.aisle] += pos.upper_quantity
                # 检查下层是否有匹配特征的货物
                if pos.lower_quantity > 0 and inventory._features_match(pos.lower_features, target, feature_keys):
                    loads[pos.aisle] += pos.lower_quantity
            else:  # 单层位置
                # 检查是否有匹配特征的货物
                if pos.quantity > 0 and inventory._features_match(pos.features, target, feature_keys):
                    loads[pos.aisle] += pos.quantity
        return loads

    def _compute_future_outbound_loads(self) -> Dict[int, float]:
        """
        计算未来的出库负载
        
        Returns:
            包含每个巷道未来出库负载的字典
        """
        loads = {aisle: 0.0 for aisle in self.aisles}
        plan = getattr(self.warehouse_core, "production_plan", {}) or {}  # 获取生产计划
        current_idx = getattr(self.warehouse_core, "production_line_current_group", {}) or {}  # 获取当前进度
        inventory = getattr(self.warehouse_core, "inventory_manager", None)
        if not inventory:
            return loads

        # 遍历所有产线
        for pl in sorted(plan.keys()):
            groups = plan.get(pl, [])  # 获取该产线的任务组
            start = current_idx.get(pl, 0)  # 获取当前任务索引
            pl_future_tasks = []
            # 收集未来任务
            for group in groups[start:]:
                for task_skus in group:
                    pl_future_tasks.append(task_skus)

            # 计算每个任务的负载
            for idx, task_skus in enumerate(pl_future_tasks):
                if not task_skus:
                    continue
                sku_entry = task_skus[0]
                if isinstance(sku_entry, dict):
                    sku = sku_entry.get("skuId") or sku_entry.get("rfid") or sku_entry.get("RFID")
                else:
                    sku = sku_entry
                if not sku:
                    continue
                # 获取SKU所在位置
                positions = inventory.get_sku_positions(sku, only_available=True)
                # 获取涉及的巷道
                aisles = sorted({p.aisle for p in positions})
                if not aisles:
                    continue
                # 计算基础权重
                base = 1.0 if idx < self._future_n else self._future_tail_weight
                weight = base / len(aisles)  # 均匀分配权重
                for aisle in aisles:
                    loads[aisle] += weight  # 累加负载

        return loads

    def allocate(self, task_info: Any, inventory_positions: List[Any]) -> Optional[int]:
        """
        提出的巷道分配算法：
        1) 统计每个巷道的空闲位置数
        2) 减去待处理的入库任务数（1个待处理任务占用1个空闲位置）
        3) 应用未来出库负载惩罚
        4) 选择得分最高的巷道（平局时轮询选择）
        
        Args:
            task_info: 任务信息
            inventory_positions: 库存位置列表
            
        Returns:
            分配的巷道ID，如果没有可用巷道则返回None
        """
        print(
            "[DEBUG][ProposedAisleAllocator] task_info type=",
            type(task_info),
            "production_line=",
            getattr(task_info, "production_line", None),
        )
        
        # 1) 统计每个巷道的空闲位置数
        empty_by_aisle = {aisle: 0 for aisle in self.aisles}
        for pos in inventory_positions:
            if pos.is_empty():
                empty_by_aisle[pos.aisle] += 1

        # 2) 减去待处理的入库任务数
        pending_by_aisle = {}
        pending_map = getattr(self.warehouse_core, "pending_inbound_by_aisle", {})
        for aisle in self.aisles:
            pending_by_aisle[aisle] = len(pending_map.get(aisle, []))

        # 计算有效空闲位置数（扣除待处理任务）
        effective_empty = {
            aisle: max(0, empty_by_aisle[aisle] - pending_by_aisle[aisle])
            for aisle in self.aisles
        }
        
        # 计算可用容量
        usable_capacity = self._compute_usable_capacity_by_aisle(inventory_positions)
        # 计算有效容量比例
        effective_ratio = {
            aisle: (
                float(effective_empty[aisle]) / float(max(1, usable_capacity.get(aisle, 0)))
            )
            for aisle in self.aisles
        }

        # 有可用槽位的巷道
        available_aisles = [
            aisle
            for aisle in self.aisles
            if effective_empty[aisle] > 0
            and (not hasattr(self.warehouse_core, "_is_aisle_enabled") or self.warehouse_core._is_aisle_enabled(aisle))
        ]
        
        # 应用禁止规则过滤
        if hasattr(self.warehouse_core, "_get_valid_inbound_aisles"):
            production_line = getattr(task_info, "production_line", None)
            valid_aisles = set(self.warehouse_core._get_valid_inbound_aisles(task_info, production_line))
            available_aisles = [a for a in available_aisles if a in valid_aisles]
        
        if not available_aisles:
            return None
        # Filter out busy aisles (running tasks).
        # NOTE: Temporarily disabled by request; keep all available aisles in scoring.
        # busy_aisles = set()
        # running_tasks = getattr(self.warehouse_core, "running_tasks", {})
        # for task in running_tasks.values():
        #     aisle = getattr(task, "assigned_aisle", None)
        #     if aisle is not None:
        #         busy_aisles.add(aisle)
        #
        # candidate_aisles = [a for a in available_aisles if a not in busy_aisles]
        # If all aisles are busy, fall back to original available list.
        # if candidate_aisles:
        #     available_aisles = candidate_aisles
        # 决策计数器递增
        self._decision_count += 1

        # 计算未来出库负载
        future_loads = self._compute_future_outbound_loads()
        production_line = getattr(task_info, "production_line", None) or 1
        match_mode = self.warehouse_core._get_outbound_match_mode(production_line)
        if match_mode == "features":
            print("Using feature-based outbound matching")
        
        # 计算特征负载
        feature_loads = (
            self._compute_feature_aisle_loads(task_info)
            if match_mode == "features"
            else {aisle: 0.0 for aisle in self.aisles}
        )
        
        # 计算最近选择计数
        recent_counts_for_score = {a: 0 for a in available_aisles}
        for a in self._recent_selected_aisles:
            if a in recent_counts_for_score:
                recent_counts_for_score[a] += 1
        
        # 计算每个可用巷道的得分
        scores = {
            aisle: (
                self._capacity_ratio_weight * effective_ratio.get(aisle, 0.0)  # 容量比例权重
                - self._future_weight * future_loads.get(aisle, 0.0)  # 未来负载惩罚
                - self._feature_weight * feature_loads.get(aisle, 0.0)  # 特征负载惩罚
                - self._pending_weight * pending_by_aisle.get(aisle, 0)  # 待处理任务惩罚
                - self._recent_weight * recent_counts_for_score.get(aisle, 0)  # 最近选择惩罚
            )
            for aisle in available_aisles
        }

        # 调试日志（仅前500次决策）
        # if self._decision_count <= 500:
        #     self._debug_file.write(
        #         f"[Decision #{self._decision_count}] empty={empty_by_aisle}, "
        #         f"pending={pending_by_aisle}, effective={effective_empty}, "
        #         f"capacity={usable_capacity}, effective_ratio={effective_ratio}, "
        #         f"future_loads={future_loads}, feature_loads={feature_loads}, recent_counts={recent_counts_for_score}, scores={scores}, "
        #         f"weights={{'capacity_ratio': {self._capacity_ratio_weight}, 'future': {self._future_weight}, 'feature': {self._feature_weight}, 'pending': {self._pending_weight}, 'recent': {self._recent_weight}}}, "
        #         f"available={available_aisles}\n"
        #     )
        #     self._debug_file.flush()

        # 4) 选择得分最高的巷道
        max_score = max(scores[a] for a in available_aisles)
        candidate_aisles = [a for a in available_aisles if scores[a] == max_score]
        if not hasattr(self, '_rr_index'):
            self._rr_index = 0

        # 优先选择在最近选择窗口中出现最少的巷道
        recent_counts = {a: 0 for a in candidate_aisles}
        for a in self._recent_selected_aisles:
            if a in recent_counts:
                recent_counts[a] += 1
        min_recent = min(recent_counts.values()) if recent_counts else 0
        least_recent_aisles = [a for a in candidate_aisles if recent_counts[a] == min_recent]

        # 如果仍然平局，使用轮询方式确定性打破平局
        least_recent_aisles.sort()
        selected_index = self._rr_index % len(least_recent_aisles)
        selected_aisle = least_recent_aisles[selected_index]
        self._rr_index += 1
        self._recent_selected_aisles.append(selected_aisle)

        # if self._decision_count <= 500:
        #     self._debug_file.write(
        #         f"  -> choose aisle {selected_aisle} "
        #         f"(reason: max score={max_score:.3f}, candidates={candidate_aisles}, "
        #         f"recent_counts={recent_counts}, least_recent={least_recent_aisles})\n"
        #     )
        #     self._debug_file.flush()

        return selected_aisle



# ==========================================
# 2. 货位分配器 (ProposedPositionAllocator)
# ==========================================
class ProposedPositionAllocator:
    """
    具有明确入库/出库码头意识的位置分配器。

    评分目标（越低越好）：
    - 入库行程：入库码头 -> 目标位置
    - 出库行程：目标位置 -> 出库码头

    这使用任务级别的 `in_line` / `out_line`（如果有），因此决策
    与每个任务的实际输入/输出端口对齐。
    """

    def __init__(self, warehouse_core):
        """
        初始化货位分配器
        
        Args:
            warehouse_core: 仓库核心组件
        """
        self.warehouse_core = warehouse_core
        # 加载时间估算配置
        self._time_cfg = load_time_estimator_config("config/time_estimator.json")
        try:
            # 入库权重（默认1.0）
            self._w_in = float(os.getenv("PROPOSED_W_IN", "1.0"))
        except Exception:
            self._w_in = 1.0
        try:
            # 出库权重（默认4.0）
            self._w_out = float(os.getenv("PROPOSED_W_OUT", "4.0"))
        except Exception:
            self._w_out = 4.0

    @staticmethod
    def _travel_time_2d(
        time_estimator,
        delta_col: int,
        delta_level: int,
        physics_cfg: Dict[str, Any],
    ) -> float:
        """
        计算2D移动时间
        
        Args:
            time_estimator: 时间估算器
            delta_col: 列差值
            delta_level: 层差值
            physics_cfg: 物理配置
            
        Returns:
            移动时间
        """
        if not time_estimator:
            # 回退到曼哈顿距离代理
            return float(delta_col + delta_level)
        try:
            return float(
                time_estimator._physics_time_2d(
                    delta_col,
                    delta_level,
                    col_scale=float(physics_cfg.get("col_scale", 15.0)),  # 列缩放
                    layer_scale=float(physics_cfg.get("layer_scale", 0.5)),  # 层缩放
                    v_col_max=float(physics_cfg.get("v_col_max", 1.5)),  # 最大列速度
                    v_layer_max=float(physics_cfg.get("v_layer_max", 0.625)),  # 最大层速度
                    a_col=float(physics_cfg.get("a_col", 0.15)),  # 列加速度
                    a_layer=float(physics_cfg.get("a_layer", 0.075)),  # 层加速度
                )
            )
        except Exception:
            return float(delta_col + delta_level)

    def allocate(
        self,
        inventory_positions: List[InventoryPosition],
        task_info: TaskData,
        current_position: Optional[InventoryPosition] = None,
    ) -> List[InventoryPosition]:
        """
        分配最佳的货位
        
        Args:
            inventory_positions: 库存位置列表
            task_info: 任务数据
            current_position: 当前位置（可选）
            
        Returns:
            推荐的货位列表（通常只有一个）
        """
        # 获取分配的巷道
        aisle = getattr(task_info, "assigned_aisle", None)
        if aisle is None:
            return []

        # 获取指定巷道的空闲位置
        aisle_positions = [
            p for p in inventory_positions if p.aisle == aisle and p.is_empty()
        ]
        if not aisle_positions:
            return []

        # 获取时间估算器
        time_estimator = getattr(self.warehouse_core, "time_estimator", None)
        # 获取产线和入/出库线路信息
        production_line = getattr(task_info, "production_line", None) or 1
        in_line = getattr(task_info, "in_line", None) or 1
        out_line = getattr(task_info, "out_line", None) or production_line

        if time_estimator:
            # 解析入/出库码头位置
            dock_in_col, dock_in_level = time_estimator.resolve_inbound_dock(in_line, default_layer=1, aisle=aisle)
            dock_out_col, dock_out_level = time_estimator.resolve_outbound_dock(out_line, default_layer=1, aisle=aisle)
        else:
            # 如果估算器不可用，则使用保守回退
            dock_in_col = int(self._time_cfg.get("dock_in_col", 1))
            dock_out_col = int(self._time_cfg.get("dock_out_col", 1))
            dock_in_level, dock_out_level = 1, 1

        # 获取物理配置
        physics_cfg = self._time_cfg.get("physics", {}) or {}

        def _key(p: InventoryPosition):
            # 入库行程时间
            in_leg = self._travel_time_2d(
                time_estimator,
                abs(p.column - dock_in_col),
                abs(p.level - dock_in_level),
                physics_cfg,
            )
            # 出库行程时间
            out_leg = self._travel_time_2d(
                time_estimator,
                abs(p.column - dock_out_col),
                abs(p.level - dock_out_level),
                physics_cfg,
            )
            # 估算总时间
            est_time = self._w_in * in_leg + self._w_out * out_leg
            # 平局时的偏好：更大的列，更低的层，更低的行
            return (est_time, -p.column, p.level, p.row)

        # 按照计算的键值排序
        aisle_positions.sort(key=_key)
        # 返回最优的一个位置
        return [aisle_positions[0]]
