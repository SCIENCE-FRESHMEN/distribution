"""提供面向车身立体库的改进巷道与货位分配策略。"""

import json
import math
import os
from typing import List, Dict, Optional, Any, Set
from collections import deque
from copy import deepcopy
from simulation.task_data import TaskData
from simulation.position import InventoryPosition
from estimate.time_estimator import load_time_estimator_config


# ==========================================================================
# 主函数：入库巷道分配（ProposedAisleAllocator）
# ==========================================================================
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
        # 巷道列表。
        self.aisles = warehouse_core.aisles
        # 巷道数量，至少为 1。
        aisle_cnt = max(1, len(self.aisles))

        # 改进策略初值统一来自 warehouse.json；异常配置才回退到当前 4 巷道标定值。
        raw_strategy_cfg = getattr(warehouse_core, "config", {}).get("proposed_strategy", {})
        strategy_cfg = raw_strategy_cfg if isinstance(raw_strategy_cfg, dict) else {}

        def _config_float(key: str, default: float) -> float:
            """读取改进策略浮点参数，并允许调参脚本用环境变量临时覆盖。

            Args:
                key: ``warehouse.json`` 中的 proposed_strategy 参数名。
                default: 配置缺失或格式错误时使用的默认值。

            Returns:
                float: 当前试验进程使用的参数值。
            """
            try:
                raw_value = os.getenv(f"PROPOSED_{key.upper()}", strategy_cfg.get(key, default))
                return float(raw_value)
            except (TypeError, ValueError):
                return default

        # 每条产线的主未来窗口按 ``ceil(2 * 产线数 / 巷道数)`` 计算。
        # 该口径限制所有产线合计的基础观察量约为每条巷道 2 个任务，避免产线数
        # 增加时每条线都固定观察一个完整巷道周期，导致 future_load 被重复累加过大。
        # 环境变量仅供网格搜索等试验显式覆盖，正常 API/仿真启动始终使用此公式。
        raw_future_n = os.getenv("PROPOSED_FUTURE_N")
        self._future_n = (
            max(1, int(float(raw_future_n)))
            if raw_future_n is not None
            else max(1, math.ceil(2 * max(1, warehouse_core.num_production_lines) / aisle_cnt))
        )
        # 最近选择窗口随巷道数变化，用于记录最近一个巷道轮转周期内的分配历史。
        raw_recent_window = os.getenv("PROPOSED_RECENT_SELECT_WINDOW")
        self._recent_select_window = (
            max(1, int(float(raw_recent_window)))
            if raw_recent_window is not None
            else max(1, round(_config_float("recent_window_per_aisle", 1.0) * aisle_cnt))
        )
        # 最近选择的巷道记录（使用双端队列，具有固定最大长度）
        self._recent_selected_aisles = deque(maxlen=self._recent_select_window)
        # 未来出库负载惩罚系数。
        self._future_weight = _config_float("future_weight", 4.0)
        # 未来窗口外任务的单项基础权重。
        self._future_tail_weight = _config_float("future_tail_weight", 0.0)
        # 同匹配特征库存集中惩罚系数。
        self._feature_weight = _config_float("feature_weight", 4.0)
        # pending、待确认和运行中入库预占惩罚系数。
        self._pending_weight = _config_float("pending_weight", 8.0)
        # 秒级工作负载权重；为零时恢复原有按任务数扣分，便于同工况对照。
        self._workload_weight = max(0.0, _config_float("workload_time_weight", 0.0))
        # 最近巷道选择次数惩罚系数。
        self._recent_weight = _config_float("recent_weight", 8.0)

        # 剩余容量比例权重，避免小巷道过度填充
        self._capacity_ratio_weight = _config_float("capacity_ratio_weight", 30.0)

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
        # empty_by_aisle: 巷道号 -> 当前物理空货位数，尚未扣除待执行入库占位。
        empty_by_aisle = {aisle: 0 for aisle in self.aisles}
        for pos in inventory_positions:
            # pos 是待统计的物理货位；仅空货位才能支持新的入库分配。
            if pos.is_empty():
                empty_by_aisle[pos.aisle] += 1

        # pending_by_aisle: 巷道号 -> pending、待确认执行和运行中入库任务的预占数量。
        pending_by_aisle = {}
        # 三个映射分别保存尚未调度、已下发待确认和正在执行的任务状态。
        pending_map = getattr(self.warehouse_core, "pending_inbound_by_aisle", {})
        pending_execution_map = getattr(self.warehouse_core, "pending_execution_tasks", {}) or {}
        running_tasks = getattr(self.warehouse_core, "running_tasks", {}) or {}
        for aisle in self.aisles:
            # aisle 是当前计算有效容量的巷道号；pending_cnt 是该巷道全部入库预占的合计。
            pending_cnt = len(pending_map.get(aisle, []))
            # 计入等待中的入库任务
            pending_cnt += sum(
                1
                for task in pending_execution_map.values()
                if getattr(task, "task_type", "") == "INBOUND" and getattr(task, "assigned_aisle", None) == aisle
            )
            # 计入正在运行的入库任务
            pending_cnt += sum(
                1
                for task in running_tasks.values()
                if getattr(task, "task_type", "") == "INBOUND" and getattr(task, "assigned_aisle", None) == aisle
            )
            pending_by_aisle[aisle] = pending_cnt

        # 计算有效空闲位置数（扣除待处理任务）
        # effective_empty: 扣除所有入库预占后，本拍仍可承诺给新任务的空货位数量。
        effective_empty = {
            aisle: max(0, empty_by_aisle[aisle] - pending_by_aisle[aisle])
            for aisle in self.aisles
        }

        # 计算可用容量
        # usable_capacity: 排除禁用位后的巷道总容量，用于修正不同巷道的尺寸差异。
        usable_capacity = self._compute_usable_capacity_by_aisle(inventory_positions)
        # 计算有效容量比例
        # effective_ratio: 有效空位占可用总容量的比例，是容量均衡得分的正向项。
        effective_ratio = {
            aisle: (
                float(effective_empty[aisle]) / float(max(1, usable_capacity.get(aisle, 0)))
            )
            for aisle in self.aisles
        }

        # 有可用槽位的巷道
        # available_aisles 是通过容量和启用状态初筛的候选巷道。
        available_aisles = [
            aisle
            for aisle in self.aisles
            if effective_empty[aisle] > 0
            and (not hasattr(self.warehouse_core, "_is_aisle_enabled") or self.warehouse_core._is_aisle_enabled(aisle))
        ]

        # 应用禁止规则过滤
        if hasattr(self.warehouse_core, "_get_valid_inbound_aisles"):
            # production_line 是当前入库任务关联的产线；valid_aisles 是禁配规则后的合法巷道集合。
            production_line = getattr(task_info, "production_line", None)
            valid_aisles = set(self.warehouse_core._get_valid_inbound_aisles(task_info, production_line))
            available_aisles = [a for a in available_aisles if a in valid_aisles]

        if not available_aisles:
            return None

        # 计算未来出库负载
        # future_loads: 巷道号 -> 未来计划出库需求的压力，用于避免把未来要出库的物料继续集中。
        future_loads = self._compute_future_outbound_loads()
        # production_line / match_mode 决定是否需要按出库匹配特征额外计算库存集中度。
        production_line = getattr(task_info, "production_line", None) or 1
        match_mode = self.warehouse_core._get_outbound_match_mode(production_line)
        if match_mode == "features":
            print("Using feature-based outbound matching")

        # 计算特征负载
        # feature_loads: 巷道号 -> 与当前任务同匹配特征的在库数量，特征模式下作为集中惩罚。
        feature_loads = (
            self._compute_feature_aisle_loads(task_info)
            if match_mode == "features"
            else {aisle: 0.0 for aisle in self.aisles}
        )

        # 计算最近选择计数
        # recent_counts_for_score: 候选巷道 -> 最近窗口中被选中的次数。
        recent_counts_for_score = {a: 0 for a in available_aisles}
        for a in self._recent_selected_aisles:
            if a in recent_counts_for_score:
                recent_counts_for_score[a] += 1

        # 计算每个可用巷道的得分。
        # 各项观察范围：容量项读取全仓当前货位快照及全部 pending/待确认/running 入库预占；
        # 未来项只对每条产线从 current_group 起展开的前 self._future_n 个任务使用基础权重；
        # 特征项读取全仓当前在库货位；pending 项统计全部尚未完成的入库预占；
        # 最近项仅统计双端队列中最近 self._recent_select_window 次巷道选择。
        # score_details 记录每次决策的全部原始值、权重贡献和最终分数。通过 stdout
        # 输出后会被 run_compare 写进各策略独立日志，后续可从这些结构化记录统计 P95。
        score_details: Dict[int, Dict[str, float | int]] = {}
        scores: Dict[int, float] = {}
        workload = self._estimate_workload_seconds(task_info, inventory_positions, available_aisles) if self._workload_weight else {}
        min_workload = min(workload.values(), default=0.0)
        for aisle in available_aisles:
            # pending 入库已从 effective_empty 中扣除，空位比例基础分会自然反映
            # 小巷道单个货位被占用的更大影响，因此其余扣分保持原始业务量，避免重复放大。
            capacity_score = self._capacity_ratio_weight * effective_ratio.get(aisle, 0.0)
            future_penalty = self._future_weight * future_loads.get(aisle, 0.0)
            feature_penalty = self._feature_weight * feature_loads.get(aisle, 0.0)
            # 工作时长启用后替代数量惩罚，避免对同一排队压力叠加两次惩罚。
            pending_penalty = self._pending_weight * pending_by_aisle.get(aisle, 0) if not self._workload_weight else 0.0
            workload_penalty = self._workload_weight * max(0.0, workload.get(aisle, 0.0) - min_workload)
            recent_penalty = self._recent_weight * recent_counts_for_score.get(aisle, 0)
            total_score = capacity_score - future_penalty - feature_penalty - pending_penalty - recent_penalty - workload_penalty
            scores[aisle] = total_score
            score_details[aisle] = {
                "empty_positions": empty_by_aisle.get(aisle, 0),
                "pending_inbound": pending_by_aisle.get(aisle, 0),
                "effective_empty_positions": effective_empty.get(aisle, 0),
                "usable_capacity": usable_capacity.get(aisle, 0),
                "effective_capacity_ratio": effective_ratio.get(aisle, 0.0),
                "future_outbound_load": future_loads.get(aisle, 0.0),
                "feature_load": feature_loads.get(aisle, 0.0),
                "recent_selected_count": recent_counts_for_score.get(aisle, 0),
                "capacity_score": capacity_score,
                "future_penalty": future_penalty,
                "feature_penalty": feature_penalty,
                "pending_penalty": pending_penalty,
                "projected_workload_s": workload.get(aisle, 0.0),
                "workload_penalty": workload_penalty,
                "recent_penalty": recent_penalty,
                "total_score": total_score,
            }

        # 4) 选择得分最高的巷道
        # max_score 是本轮最佳综合分；candidate_aisles 是所有并列最优巷道。
        max_score = max(scores[a] for a in available_aisles)
        candidate_aisles = [a for a in available_aisles if scores[a] == max_score]
        if not hasattr(self, '_rr_index'):
            self._rr_index = 0

        # 优先选择在最近选择窗口中出现最少的巷道
        # recent_counts 仅在并列最优巷道间比较最近使用频率。
        recent_counts = {a: 0 for a in candidate_aisles}
        for a in self._recent_selected_aisles:
            if a in recent_counts:
                recent_counts[a] += 1
        # min_recent 是并列候选中的最小近期使用次数；least_recent_aisles 是第二轮筛选结果。
        min_recent = min(recent_counts.values()) if recent_counts else 0
        least_recent_aisles = [a for a in candidate_aisles if recent_counts[a] == min_recent]

        # 如果仍然平局，使用轮询方式确定性打破平局
        least_recent_aisles.sort()
        # selected_index 是轮询指针映射到并列候选后的下标；selected_aisle 是最终分配结果。
        selected_index = self._rr_index % len(least_recent_aisles)
        selected_aisle = least_recent_aisles[selected_index]
        self._rr_index += 1
        self._recent_selected_aisles.append(selected_aisle)
        print(
            "[PROPOSED_AISLE_SCORE] "
            + json.dumps(
                {
                    "taskId": getattr(task_info, "task_id", None),
                    "productionLine": production_line,
                    "matchMode": match_mode,
                    "futureWindow": self._future_n,
                    "recentWindow": self._recent_select_window,
                    "selectedAisle": selected_aisle,
                    "candidates": score_details,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )

        return selected_aisle

    def _estimate_workload_seconds(self, task_info, positions, aisles) -> Dict[int, float]:
        """估算各候选巷道处理当前已知任务及新增入库所需的工作秒数。

        Args:
            task_info: 当前入库任务，仅使用已知 in_line，不读取未来出库口。
            positions: 当前真实货位，空位平均坐标作为尚未冻结目标的距离代理。
            aisles: 已通过容量和业务规则筛选的候选巷道。

        Returns:
            Dict[int, float]: 巷道到预计工作秒数；不修改任务、库存或事件状态。
        """
        core = self.warehouse_core
        estimator = core.time_estimator
        now = float(core.current_time)
        running = list((getattr(core, "running_tasks", {}) or {}).values())
        accepted = list((getattr(core, "pending_execution_tasks", {}) or {}).values())
        result = {}
        for aisle in aisles:
            # 未冻结任务尚无真实目标货位，以该巷道全部物理空位的平均坐标估算典型行程。
            empty = [position for position in positions if position.aisle == aisle and position.is_empty()]
            col = sum(position.column for position in empty) / len(empty)
            level = sum(position.level for position in empty) / len(empty)
            physics = estimator.get_physics_params(aisle)

            def estimate_inbound(task):
                """按已知入库口、当前设备位置和目标位置估算入库移动及取放时间。

                Args:
                task: 待估入库任务；目标未冻结时使用巷道空位平均坐标。
                Returns:
                    float: 秒级近似作业时长。
                """
                dock_col, dock_level = estimator.resolve_inbound_dock(getattr(task, "in_line", None) or 1, default_layer=1, aisle=aisle)
                targets = getattr(task, "positions", None) or []
                target_col, target_level = (targets[0].column, targets[0].level) if targets else (col, level)
                current = getattr(core, "current_position_by_aisle", {}).get(aisle)
                approach = ProposedPositionAllocator._travel_time_2d(estimator, abs(current.column - dock_col), abs(current.level - dock_level), physics) if current else 0.0
                travel = ProposedPositionAllocator._travel_time_2d(estimator, abs(target_col - dock_col), abs(target_level - dock_level), physics)
                return approach + travel + estimator.pickup_time_default + estimator.drop_time_default

            seconds = 0.0
            seen = set()
            for task in running:
                if getattr(task, "assigned_aisle", None) != aisle:
                    continue
                seen.add(getattr(task, "task_id", None) or id(task))
                record = getattr(task, "task_record", None) or {}
                # 已派发任务使用预计完成时刻减当前时刻，不能重复累计完整 duration。
                seconds += max(0.0, float(record.get("delivery_time") or now) - now)
            queue = accepted + list(getattr(core, "pending_inbound_by_aisle", {}).get(aisle, []))
            for task in queue:
                key = getattr(task, "task_id", None) or id(task)
                if key in seen or getattr(task, "assigned_aisle", None) != aisle:
                    continue
                seen.add(key)
                record = getattr(task, "task_record", None) or {}
                if getattr(task, "task_type", "") == "INBOUND":
                    seconds += estimate_inbound(task)
                else:
                    seconds += max(0.0, float(record.get("duration") or 0.0))
            result[aisle] = seconds + estimate_inbound(task_info)
        return result
    # ==========================================================================
    # 辅助函数：巷道容量、特征需求和未来负载计算
    # ==========================================================================
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
                # 跳过禁用的位置。
                continue
            # 累加巷道容量。
            capacity[pos.aisle] += 1
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

        # 目标特征。
        target = filters[0]
        # 遍历所有库存位置
        for pos in inventory.inventory_positions:
            # 双层位置分别检查上下层。
            if pos.is_double_layer:
                # 检查上层是否有匹配特征的货物
                if pos.upper_quantity > 0 and inventory._features_match(pos.upper_features, target, feature_keys):
                    loads[pos.aisle] += pos.upper_quantity
                # 检查下层是否有匹配特征的货物
                if pos.lower_quantity > 0 and inventory._features_match(pos.lower_features, target, feature_keys):
                    loads[pos.aisle] += pos.lower_quantity
            else:
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
        # 获取生产计划。
        plan = getattr(self.warehouse_core, "production_plan", {}) or {}
        # 获取当前生产组进度。
        current_idx = getattr(self.warehouse_core, "production_line_current_group", {}) or {}
        inventory = getattr(self.warehouse_core, "inventory_manager", None)
        if not inventory:
            return loads

        # 遍历所有产线
        for pl in sorted(plan.keys()):
            # 获取该产线的任务组。
            groups = plan.get(pl, [])
            # 获取当前任务组索引。
            start = current_idx.get(pl, 0)
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
                # 将该未来任务的基础负载均匀分配给存在库存的巷道。
                weight = base / len(aisles)
                for aisle in aisles:
                    # 累加该巷道的未来负载。
                    loads[aisle] += weight

        return loads




# ==========================================================================
# 主函数：入库货位分配（ProposedPositionAllocator）
# ==========================================================================
class ProposedPositionAllocator:
    """
    具有明确入库/出库口意识的位置分配器。

    评分目标（越低越好）：
    - 入库行程：入库口 -> 目标位置
    - 出库行程：目标位置 -> 出库口

    这使用任务级别的 `in_line` / `out_line`（如果有），因此决策
    与每个任务的实际输入/输出端口对齐。
    """

    def __init__(self, warehouse_core):
        """初始化基于综合入/出库行程的车身库货位分配器。

        Args:
            warehouse_core: 提供 TimeEstimator、待执行任务和巷道配置的 WarehouseCore。

        Returns:
            None: 仅保存依赖引用和货位评分配置。
        """
        self.warehouse_core = warehouse_core
        # 加载时间估算配置
        self._time_cfg = load_time_estimator_config("config/time_estimator.json")
        # 货位行程权重与巷道策略初值同样从 warehouse.json 读取。
        raw_position_cfg = getattr(warehouse_core, "config", {}).get("proposed_position_strategy", {})
        position_cfg = raw_position_cfg if isinstance(raw_position_cfg, dict) else {}
        try:
            self._w_in = float(os.getenv("PROPOSED_W_IN", position_cfg.get("inbound_travel_weight", 1.0)))
        except (TypeError, ValueError):
            self._w_in = 1.0
        try:
            self._w_out = float(os.getenv("PROPOSED_W_OUT", position_cfg.get("outbound_travel_weight", 4.0)))
        except (TypeError, ValueError):
            self._w_out = 4.0

    def allocate(
        self,
        inventory_positions: List[InventoryPosition],
        task_info: TaskData,
        current_position: Optional[InventoryPosition] = None,
    ) -> List[InventoryPosition]:
        """在任务已确定巷道内返回综合入/出库行程最短的货位。

        Args:
            inventory_positions: 全仓真实货位，只读取 assigned_aisle 对应的空位。
            task_info: 已完成巷道选择的入库任务；其 in_line 决定入库口坐标。
            current_position: 预留给动态路径模型的当前设备位置。

        Returns:
            List[InventoryPosition]: 单层车身库返回一个位置；目标巷道无空位时返回空列表。
        """
        # 获取分配的巷道
        # aisle 是任务已确定的目标巷道；货位分配不负责重新选择巷道。
        aisle = getattr(task_info, "assigned_aisle", None)
        if aisle is None:
            return []

        # 获取指定巷道的空闲位置
        # aisle_positions 是目标巷道内所有真实空货位，后续按综合行程时间排序。
        aisle_positions = [
            p for p in inventory_positions if p.aisle == aisle and p.is_empty()
        ]
        if not aisle_positions:
            return []

        # 获取时间估算器
        # time_estimator 是物理行程时间模型；缺失时使用配置中的保守距离代理。
        time_estimator = getattr(self.warehouse_core, "time_estimator", None)
        # API 的入库请求未知后续出库口，仅以当前可获取的入库线路和虚拟出库口择位。
        in_line = getattr(task_info, "in_line", None) or 1
        if time_estimator:
            # 虚拟出库口由配置的列偏好模式确定，避免使用当前入库任务不可获得的未来数据。
            dock_in_col, dock_in_level = time_estimator.resolve_inbound_dock(in_line, default_layer=1, aisle=aisle)
            if hasattr(self.warehouse_core, "_resolve_inbound_virtual_outbound_dock"):
                dock_out_col, dock_out_level = self.warehouse_core._resolve_inbound_virtual_outbound_dock(aisle)
            else:
                dock_out_col, dock_out_level = (dock_in_col, dock_in_level)
        else:
            # 如果估算器不可用，则使用保守回退
            dock_in_col = int(self._time_cfg.get("dock_in_col", 1))
            dock_in_level = 1
            if hasattr(self.warehouse_core, "_resolve_inbound_virtual_outbound_dock"):
                dock_out_col, dock_out_level = self.warehouse_core._resolve_inbound_virtual_outbound_dock(aisle)
            else:
                dock_out_col, dock_out_level = int(self._time_cfg.get("dock_out_col", 1)), 1

        # 获取物理配置
        # physics_cfg 保存二维移动时间模型的速度、加速度等参数。
        # 与正式任务估时保持一致：优先读取目标巷道覆盖参数，未配置时回退全局 physics。
        physics_cfg = (
            time_estimator.get_physics_params(aisle)
            if time_estimator is not None and hasattr(time_estimator, "get_physics_params")
            else self._time_cfg.get("physics", {}) or {}
        )
        # position_score_details 保存每个真实候选货位的成本拆分；只在本公开分配入口
        # 输出，内部模拟占位仍走 _allocate_on_positions，避免重复记录同一任务的试算过程。
        position_score_details: Dict[tuple[int, int, int, int], Dict[str, Any]] = {}

        def _key(p: InventoryPosition):
            """计算真实候选货位的综合成本与确定性并列规则。

            Args:
                p: 当前巷道中的一个空货位。

            Returns:
                tuple[float, int, int, int]: 先按加权时间排序，再按列、层、排列稳定排序。
            """
            # p 是待评分货位；in_leg / out_leg 分别是从入库和到虚拟出库口的时间。
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
            # est_time 是按入库、出库权重加总后的货位综合成本。
            est_time = self._w_in * in_leg + self._w_out * out_leg
            # 路径时间相同才按配置的最小列/中间列/最大列模式消除并列，随后保持层、排稳定。
            column_preference_key = self.warehouse_core.get_position_column_preference_key(p.aisle, p.column)
            sort_key = (
                est_time,
                *column_preference_key,
                p.level,
                p.row,
            )
            # 保留原始行程和加权贡献，便于从日志统计各子项的分布与 P95。
            position_score_details[self._position_key(p)] = {
                "position": p.get_position_id(),
                "inbound_leg_s": in_leg,
                "outbound_leg_s": out_leg,
                "inbound_weighted_cost": self._w_in * in_leg,
                "outbound_weighted_cost": self._w_out * out_leg,
                "travel_cost": est_time,
                "column_preference_key": list(column_preference_key),
                "level": int(p.level),
                "row": int(p.row),
            }
            return sort_key

        # 按照计算的键值排序
        aisle_positions.sort(key=_key)
        # 排序后第一个元素是最终推荐；前 3 个候选足以解释本次择优，又不会为每个任务
        # 输出整条巷道的数百个位置评分。
        selected_position = aisle_positions[0]
        top_candidates = [
            position_score_details[self._position_key(position)]
            for position in aisle_positions[:3]
        ]
        print(
            "[PROPOSED_POSITION_SCORE] "
            + json.dumps(
                {
                    "taskId": getattr(task_info, "task_id", None),
                    "assignedAisle": int(aisle),
                    "candidateCount": len(aisle_positions),
                    "inboundDock": {"column": int(dock_in_col), "level": int(dock_in_level)},
                    "outboundDock": {"column": int(dock_out_col), "level": int(dock_out_level)},
                    "outboundDockSource": "virtual",
                    "weights": {"inbound": self._w_in, "outbound": self._w_out},
                    "selected": position_score_details[self._position_key(selected_position)],
                    "topCandidates": top_candidates,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        # 返回最优的一个位置。
        return [selected_position]
    # ==========================================================================
    # 辅助函数：货位筛选、模拟占位和行程评分
    # ==========================================================================
    def _position_key(self, position: InventoryPosition) -> tuple[int, int, int, int]:
        """生成货位坐标主键，用于将冻结位置映射到模拟副本。

        Args:
            self: 当前对象实例。
            position: 货位对象或货位描述。

        Returns:
            tuple[int, int, int, int]: ``(aisle, row, column, level)`` 坐标元组。
        """
        return (int(position.aisle), int(position.row), int(position.column), int(position.level))

    def _task_sku_count(self, task_info: TaskData) -> int:
        """获取任务有效 SKU 数量，兼容 TaskData 和最小任务桩对象。

        Args:
            self: 当前对象实例。
            task_info: 待处理的任务或任务描述对象。

        Returns:
            int: 至少为 1 的 SKU 数，用于决定单位置或多位置返回结构。
        """
        try:
            return max(1, len(task_info.get_sku_ids()))
        except Exception:
            return max(1, len(getattr(task_info, "skus", []) or []))

    def _mark_position_occupied(self, position: InventoryPosition) -> None:
        """在模拟货位上写入占位标记，不修改真实库存或 SKU 索引。

        Args:
            self: 当前对象实例。
            position: 货位对象或货位描述。

        Returns:
            None: 单层位置写入 SKU/数量；（双层位置优先填充尚未占用的上层再下层，纵梁库移植）
        """
        if not position.is_double_layer:
            position.sku = position.sku or "__SIM_PENDING__"
            position.quantity = max(1, int(getattr(position, "quantity", 0) or 0))
            return
        if int(getattr(position, "upper_quantity", 0) or 0) == 0:
            position.upper_sku = position.upper_sku or "__SIM_PENDING__"
            position.upper_quantity = 1
            return
        if int(getattr(position, "lower_quantity", 0) or 0) == 0:
            position.lower_sku = position.lower_sku or "__SIM_PENDING__"
            position.lower_quantity = 1

    def _allocate_on_positions(
        self,
        inventory_positions: List[InventoryPosition],
        task_info: TaskData,
        current_position: Optional[InventoryPosition] = None,
    ) -> List[InventoryPosition]:
        """在模拟货位副本中为一个任务选取不冲突的最低综合成本位置。

        Args:
            inventory_positions: 单一巷道或全仓的模拟货位副本；本方法不读取真实索引。
            task_info: 至少包含 assigned_aisle、skus、in_line 的入库任务。
            current_position: 预留的设备当前位置；当前成本由库口到货位的静态路径计算。

        Returns:
            List[InventoryPosition]: 单 SKU 返回一个位置；双层双 SKU 返回同一位置两次；
            无可用货位时返回空列表。
        """
        aisle = getattr(task_info, "assigned_aisle", None)
        if aisle is None:
            return []

        aisle_positions = [p for p in inventory_positions if p.aisle == aisle and p.is_empty()]
        if not aisle_positions:
            return []

        time_estimator = getattr(self.warehouse_core, "time_estimator", None)
        in_line = getattr(task_info, "in_line", None) or 1
        if time_estimator:
            # 内部模拟占位也只使用当前任务可见的虚拟出库口，避免评分泄漏未来出库信息。
            dock_in_col, dock_in_level = time_estimator.resolve_inbound_dock(in_line, default_layer=1, aisle=aisle)
            if hasattr(self.warehouse_core, "_resolve_inbound_virtual_outbound_dock"):
                dock_out_col, dock_out_level = self.warehouse_core._resolve_inbound_virtual_outbound_dock(aisle)
            else:
                dock_out_col, dock_out_level = (dock_in_col, dock_in_level)
        else:
            dock_in_col = int(self._time_cfg.get("dock_in_col", 1))
            dock_in_level = 1
            if hasattr(self.warehouse_core, "_resolve_inbound_virtual_outbound_dock"):
                dock_out_col, dock_out_level = self.warehouse_core._resolve_inbound_virtual_outbound_dock(aisle)
            else:
                dock_out_col, dock_out_level = int(self._time_cfg.get("dock_out_col", 1)), 1

        # 内部模拟占位必须与公开分配入口使用相同的巷道物理参数，否则连续任务的
        # 货位试算会和最终真实分配出现不一致。
        physics_cfg = (
            time_estimator.get_physics_params(aisle)
            if time_estimator is not None and hasattr(time_estimator, "get_physics_params")
            else self._time_cfg.get("physics", {}) or {}
        )

        def _key(p: InventoryPosition):
            """计算候选货位的字典序评分。

            Args:
                p: 目标巷道内的空货位。

            Returns:
                tuple[float, ...]: 加权入/出库时间优先，其后按配置列号模式、层、排稳定排序。
            """
            in_leg = self._travel_time_2d(
                time_estimator,
                abs(p.column - dock_in_col),
                abs(p.level - dock_in_level),
                physics_cfg,
            )
            out_leg = self._travel_time_2d(
                time_estimator,
                abs(p.column - dock_out_col),
                abs(p.level - dock_out_level),
                physics_cfg,
            )
            est_time = self._w_in * in_leg + self._w_out * out_leg
            return (
                est_time,
                *self.warehouse_core.get_position_column_preference_key(p.aisle, p.column),
                p.level,
                p.row,
            )

        aisle_positions.sort(key=_key)
        sku_count = self._task_sku_count(task_info)
        if sku_count <= 1:
            return [aisle_positions[0]]
        for pos in aisle_positions:
            if getattr(pos, "is_double_layer", False):
                return [pos, pos]
        if len(aisle_positions) >= sku_count:
            return aisle_positions[:sku_count]
        return []

    def _build_simulated_positions(
        self,
        inventory_positions: List[InventoryPosition],
        aisle: int,
        current_task: TaskData,
    ) -> List[InventoryPosition]:
        """构造已叠加前序入库占位的巷道货位副本。

        Args:
            inventory_positions: 当前真实货位集合，仅用于深拷贝。
            aisle: 要模拟的目标巷道。
            current_task: 正在评分的任务；pending 队列到该任务本身时停止占位。

        Returns:
            List[InventoryPosition]: 仅包含目标巷道的可写模拟副本。
        """
        # simulated 是当前巷道的深拷贝库存，用于模拟运行和排队入库的占位，避免修改真实库存。
        simulated = deepcopy(inventory_positions)
        # pos_map: 坐标键 -> 模拟货位对象，供固定 positions 快速定位。
        pos_map = {self._position_key(p): p for p in simulated}

        def occupy_task(task: TaskData) -> None:
            """将已运行、已推荐或排在当前任务之前的入库任务映射为模拟占位。

            Args:
                task: 待叠加的入库任务；有冻结 positions 时直接映射，否则先在副本内分配。

            Returns:
                None: 仅修改 ``simulated`` 和 ``pos_map`` 指向的深拷贝货位。
            """
            if getattr(task, "task_type", "") != "INBOUND":
                return
            if int(getattr(task, "assigned_aisle", 0) or 0) != int(aisle):
                return
            positions = list(getattr(task, "positions", None) or [])
            if positions:
                for pos in positions:
                    sim_pos = pos_map.get(self._position_key(pos))
                    if sim_pos is not None:
                        self._mark_position_occupied(sim_pos)
                return
            if getattr(task, "task_id", None) == getattr(current_task, "task_id", None):
                return
            allocated = self._allocate_on_positions(simulated, task, current_position=None)
            for pos in allocated:
                sim_pos = pos_map.get(self._position_key(pos))
                if sim_pos is not None:
                    self._mark_position_occupied(sim_pos)

        # 三类任务按执行先后占用模拟货位；pending_inbound 按队列顺序仅占用当前任务之前的任务。
        running_tasks = getattr(self.warehouse_core, "running_tasks", {}) or {}
        pending_execution = getattr(self.warehouse_core, "pending_execution_tasks", {}) or {}
        pending_inbound = getattr(self.warehouse_core, "pending_inbound_by_aisle", {}).get(aisle, []) or []

        for task in running_tasks.values():
            occupy_task(task)
        for task in pending_execution.values():
            occupy_task(task)
        for task in pending_inbound:
            if getattr(task, "task_id", None) == getattr(current_task, "task_id", None):
                break
            occupy_task(task)

        return simulated

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
                    # 列方向相邻货位距离。
                    col_scale=float(physics_cfg.get("col_scale", 6.0)),
                    # 层方向相邻货位距离。
                    layer_scale=float(physics_cfg.get("layer_scale", 2.6)),
                    # 列方向最大速度。
                    v_col_max=float(physics_cfg.get("v_col_max", 2.0)),
                    # 层方向最大速度。
                    v_layer_max=float(physics_cfg.get("v_layer_max", 0.5)),
                    # 列方向加速度。
                    a_col=float(physics_cfg.get("a_col", 0.4)),
                    # 层方向加速度。
                    a_layer=float(physics_cfg.get("a_layer", 0.1)),
                )
            )
        except Exception:
            return float(delta_col + delta_level)
