"""改进入库巷道与货位分配策略。

``WarehouseCore`` 在接收到未分配入库任务时调用本模块；API 服务也通过同一策略为 pending 入库任务生成并冻结位置。
巷道分配器返回巷道号，货位分配器返回 ``InventoryPosition`` 列表。
"""

from collections.abc import Mapping
from typing import List, Dict, Optional, Any, Set, Tuple
from copy import deepcopy
import random
from collections import defaultdict
from estimate.time_estimator import TimeEstimator
from simulation.position import InventoryPosition


def _get_task_field(task_info: Any, field_name: str, default: Any = None) -> Any:
    """从 API 字典或仿真任务对象读取同名字段。

    Args:
        task_info: API 层传入的映射对象，或仿真层传入的 ``TaskData``/临时任务对象。
        field_name: 需要读取的字段名，例如 ``skus``、``production_line``。
        default: 字段不存在或任务为空时返回的默认值。

    Returns:
        字段实际值或 ``default``。先判断 ``Mapping``，避免静态分析把字典误认为
        带 ``skus`` 属性的对象。
    """
    if isinstance(task_info, Mapping):
        return task_info.get(field_name, default)
    return getattr(task_info, field_name, default)

# 巷道分配：优先选择能形成配对或便于后续同巷道移库的候选巷道；
# 在候选集合内，按目标 SKU 分布更少、剩余空位更多的方向做平衡。
# 对非天然配对的双梁任务，会结合未来计划中相关配对的紧急度决定优先侧。
# 货位分配：1. 已有双侧配对时，直接选择可用配对位。
#  2. 仅一侧能配对时，另一侧优先放到正对位；必要时选择代价更小的位置。
#  3. 都无法直接配对时，按偏好层、列距离和可用性选择当前位置。
# ==========================================
# 1. 巷道分配器 (ProposedAisleAllocator)
# ==========================================
class ProposedAisleAllocator:
    """根据配对机会、模拟库存和分布指标选择入库巷道。

    调用入口为 ``WarehouseCore.allocate_inbound_aisle`` 输入是任务 SKU/属性与真实货位列表，输出为一个可承接巷道号或
    ``None``。本类依赖 ``WarehouseCore`` 的 pending/running 入库状态、SKU配对表、巷道服务范围和生产计划。
    """
    def __init__(self, warehouse_core):
        """保存巷道评分所依赖的仓库只读状态。

        Args:
            warehouse_core (WarehouseCore): 仓库核心对象，提供库存索引、BOM 配对、
                巷道列表、产线服务范围和 pending/running 入库任务。

        Returns:
            None: 本构造函数只建立依赖引用，不创建或修改任务、库存和货位。
        """
        # 全局状态入口；后续模拟视图从该对象读取 pending/running 入库任务。
        self.warehouse_core = warehouse_core
        # 真实库存和货位索引；巷道评分仅读取 inventory_positions，不直接写入。
        self.inventory_manager = warehouse_core.inventory_manager
        # {skuA: skuB} 的 BOM 配对表，用于判断天然配对和查找待补配的另一根梁。
        self.sku_pairs = warehouse_core.sku_pairs
        # 仓库中存在的巷道号列表，例如 [1, 2, 3]；是未过滤前的候选全集。
        self.aisles = warehouse_core.aisles
        # 配对时必须相等的 SKU 属性字段，例如 version、productionAttribute。
        self.match_fields = getattr(warehouse_core, 'match_fields', []) or []
        # 所有参与 BOM 配对的 SKU 集合，供单梁/非天然双梁分支快速判断。
        self.paired_sku_set = set(self.sku_pairs.keys()) | set(self.sku_pairs.values())

    # ========================================================================
    # 主流程：入库巷道分配
    # ========================================================================
    def allocate(self, task_info: Any, inventory_positions: List[Any]) -> Optional[int]:
        """给一个入库任务选择巷道。

        ``task_info`` 可为 ``TaskData`` 或字典，``inventory_positions`` 是真实
        货位列表。方法先构造包含 pending/running 入库的模拟视图，再按单梁、
        天然配对双梁或非天然配对双梁分支形成候选，最终返回一个巷道号。

        局部变量：``task_id`` 只用于日志与模拟排除自身；``skus_data`` 是原始任务
        条目；``sku_ids`` 是用于配对查询的 SKU 顺序；``target_pls`` 是每根梁可服务
        的产线；``valid_aisles`` 是取交集后的候选巷道；``simulated_positions_by_aisle``
        是已叠加 running/pending 入库的只读副本；``result`` 是最终巷道或 ``None``。

        Args:
            task_info: 包含 SKU、生产线和可选任务标识的任务对象或字典。
            inventory_positions: 当前仓库的真实货位列表；仅用于生成候选和模拟副本。

        Returns:
            推荐巷道编号；产线约束、模拟占位或货位容量使全部候选失效时返回 ``None``。
        """
        # 任务 ID 仅用于日志与模拟时排除当前任务，不参与巷道评分。
        task_id = _get_task_field(task_info, "task_id") or _get_task_field(task_info, "id")
        # API 传字典，仿真传 TaskData；统一读取 SKU 和任务类型。
        skus_data = _get_task_field(task_info, "skus", []) or []
        task_type = str(_get_task_field(task_info, "task_type", "")).upper()
        # 入库任务没有生产计划产线归属；出库或其他显式产线约束任务才读取
        # production_line。入库的物料可达性由下方 eligible_production_lines 单独处理。
        production_line_value = None
        if "INBOUND" not in task_type:
            production_line_value = _get_task_field(task_info, "production_line")
        # 没有 SKU 时无法判断库存、配对或货位，直接返回无分配结果。
        if not skus_data:
            print(f"[WARN][allocator] ProposedAisleAllocator: empty skus task={task_id}")
            return None

        # 提取 SKU ID与属性用于配对判断。
        # 特征字段由 match_fields 配置，例如 version、颜色或生产属性；后续配对时会一并校验。
        sku_ids = [s.get('skuId') if isinstance(s, dict) else s for s in skus_data]
        sku_attrs = [self._extract_attrs(s) for s in skus_data]

        # 出库按所属生产计划产线筛选；入库的 SKU 可服务产线在下一段独立筛选，
        # 两者不能混用，否则会把尚未下发的生产计划误当作入库任务属性。
        valid_aisles = self.aisles
        if production_line_value is None:
            target_pls = [None] * len(sku_ids)
        elif isinstance(production_line_value, (list, tuple, set)):
            target_pls = list(production_line_value)
        else:
            target_pls = [production_line_value]

        # 单个产线编号适用于整项任务；双梁分别指定产线时，按 SKU 顺序补齐。
        if len(target_pls) < len(sku_ids):
            target_pls = target_pls + [None] * (len(sku_ids) - len(target_pls))

        for pl in target_pls:
            if pl is not None:
                # 同一任务必须在同一巷道执行，因此多条产线约束取共同可服务的巷道。
                pl_aisles = [a for a, pls in self.warehouse_core.aisle_production_line_mapping.items() if pl in pls]
                valid_aisles = list(set(valid_aisles) & set(pl_aisles))

        # 入库任务的每根梁都必须能从推荐巷道送往其至少一条可服务产线。
        # 输入格式为 [[SKU1 可服务产线...], [SKU2 可服务产线...]]，与 skus 顺序对齐。
        # 单个 SKU 的可服务产线可以有多个；同一双梁任务的两根梁允许对应不同产线，
        # 但最终巷道必须分别与两者的候选集合存在交集。
        eligible_lines_value = _get_task_field(task_info, "eligible_production_lines")
        if "INBOUND" in task_type and eligible_lines_value is not None:
            eligible_lines_by_sku = list(eligible_lines_value)
            if len(eligible_lines_by_sku) < len(sku_ids):
                eligible_lines_by_sku.extend([None] * (len(sku_ids) - len(eligible_lines_by_sku)))

            for eligible_lines in eligible_lines_by_sku[:len(sku_ids)]:
                if eligible_lines is None:
                    # 调用方未提供该 SKU 的映射时不在此处额外收紧；配置加载层会负责诊断。
                    continue
                allowed_lines = {int(line) for line in eligible_lines}
                serviceable_aisles = [
                    aisle for aisle, aisle_lines in self.warehouse_core.aisle_production_line_mapping.items()
                    if allowed_lines & {int(line) for line in aisle_lines}
                ]
                valid_aisles = list(set(valid_aisles) & set(serviceable_aisles))

        if not valid_aisles:
            # 两根梁的服务巷道没有交集时，不能绕过配置落入任意巷道。
            print(f"[WARN][allocator] incompatible production-line constraint task={task_id} lines={target_pls}")
            return None

        # 模拟视图已叠加 running/pending 入库任务的占位，后续评分只看剩余空间。
        simulated_positions_by_aisle = self._build_simulated_positions_by_aisle(
            inventory_positions,
            current_task=task_info,
        )
        # 已有任务无法在某巷道同时落位时，该巷道不能再被推荐给当前任务。
        valid_aisles = [a for a in valid_aisles if simulated_positions_by_aisle.get(a)]
        if not valid_aisles:
            print(f"[WARN][allocator] no feasible simulated aisle task={task_id}")
            return None

        # 1. 情况一：单梁入库
        if len(sku_ids) == 1:
            # 后续逻辑按 [左梁, 右梁] 判断；单梁保留空槽位以避免改变 beamSide 的排位含义。
            beam_side = None
            if skus_data and isinstance(skus_data[0], dict):
                beam_side = getattr(skus_data[0].get("beamSide"), "value", skus_data[0].get("beamSide"))
            if str(beam_side).upper() == "RIGHT":
                # RIGHT 单梁占第二个槽位，后续配对候选优先按 row 2 解释。
                sku_ids = [None, sku_ids[0]]
                sku_attrs = [{}, sku_attrs[0]]
            else:
                sku_ids = [sku_ids[0], None]
                sku_attrs = [sku_attrs[0], {}]

        # 2. 情况二：入库双梁本身已配对 (A, B 是配对)
        if len(sku_ids) == 2 and self.sku_pairs.get(sku_ids[0]) == sku_ids[1]:
            # 本次同时入库的两根梁已互为配对，不需要比较哪一侧更紧急，只做分布均衡。
            result = self._balance_by_sku_distribution(
                sku_ids,
                sku_attrs,
                valid_aisles,
                inventory_positions,
                simulated_positions_by_aisle=simulated_positions_by_aisle,
            )
            # result = self._balance_by_production_line(target_pls, valid_aisles, inventory_positions)
            if result is None:
                print(f"[WARN][allocator] ProposedAisleAllocator: no aisle allocated task={task_id} skus={sku_ids}")
            return result

        # 3. 情况三：非配对梁或包含单梁，进行紧急度对比
        result = self._allocate_with_urgency_comparison(
            sku_ids,
            sku_attrs,
            target_pls,
            valid_aisles,
            inventory_positions,
            simulated_positions_by_aisle=simulated_positions_by_aisle,
            task_id=task_id,
        )
        if result is None:
            print(f"[WARN][allocator] ProposedAisleAllocator: no aisle allocated task={task_id} skus={sku_ids}")
        return result

    # ========================================================================
    # 辅助函数：任务属性与模拟库存视图
    # ========================================================================
    def _extract_attrs(self, sku_entry: Any) -> Dict[str, Any]:
        """提取参与 SKU 配对的属性字段。

        Args:
            sku_entry (Any): 任务中的单个 SKU 字典。

        Returns:
            Dict[str, Any]: 仅包含 ``match_fields`` 的属性；未配置属性时为空字典。
        """
        if not isinstance(sku_entry, dict) or not self.match_fields:
            return {}
        return {field: sku_entry.get(field) for field in self.match_fields}

    def _get_simulated_positions_for_aisle(
        self,
        aisle: int,
        inventory_positions: List[Any],
        current_task: Any = None,
        current_position: Optional[InventoryPosition] = None,
    ) -> List[Any]:
        """取得指定巷道叠加前序入库后的模拟货位副本。

        Args:
            aisle: 需要建立模拟视图的巷道编号。
            inventory_positions: 当前真实货位列表；本方法不会修改其中任何对象。
            current_task: 正在预览的任务；传入时会在模拟中排除该任务自身。
            current_position: 当前设备位置或任务位置，用于下游按当前层位选择入库位置。

        Returns:
            目标巷道的深拷贝货位列表；模拟失败或巷道不可用时返回空列表。
        """
        position_allocator = getattr(self.warehouse_core, "inbound_position_allocator", None)
        if position_allocator is None or not hasattr(position_allocator, "simulate_pending_positions_for_aisle"):
            return [p for p in inventory_positions if p.aisle == aisle]
        try:
            return position_allocator.simulate_pending_positions_for_aisle(
                aisle=aisle,
                inventory_positions=inventory_positions,
                current_task=current_task,
                current_position=current_position,
            )
        except Exception as exc:
            print(f"[WARN][allocator] failed to simulate aisle={aisle}: {exc}")
            return []

    def _build_simulated_positions_by_aisle(
        self,
        inventory_positions: List[Any],
        current_task: Any = None,
        current_position: Optional[InventoryPosition] = None,
    ) -> Dict[int, List[Any]]:
        """为全部可服务巷道建立一致的模拟货位视图。

        Args:
            inventory_positions: 当前真实货位列表。
            current_task: 当前待分配任务，用于避免该任务在预览时重复占位。
            current_position: 当前设备位置或任务位置，用于传递层位偏好。

        Returns:
            以巷道编号为键的模拟货位副本；值为空列表表示该巷道无法承载前序入库。
        """
        return {
            aisle: self._get_simulated_positions_for_aisle(
                aisle,
                inventory_positions,
                current_task=current_task,
                current_position=current_position,
            )
            for aisle in self.aisles
        }

    def _get_distribution_metrics(
        self,
        sku_ids: List[Optional[str]],
        sku_attrs: List[Dict[str, Any]],
        candidate_aisles: List[int],
        inventory_positions: List[Any],
        simulated_positions_by_aisle: Optional[Dict[int, List[Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """统计各候选巷道的配对机会、库存聚集度和剩余容量。

        Args:
            sku_ids: 当前任务的 SKU 顺序，``None`` 表示单梁任务的空槽位。
            sku_attrs: 与 ``sku_ids`` 对齐的 ``match_fields`` 属性，用于判断可补齐的配对梁。
            candidate_aisles: 当前任务物理可达且满足产线约束的巷道集合。
            inventory_positions: 真实货位列表；仅在未传入模拟视图时用于构造副本。
            simulated_positions_by_aisle: 已叠加 running/pending 入库的货位副本。

        Returns:
            每个巷道一条统计字典，包含 ``direct_pair_count``、``sku_count``、
            ``same_suffix_sku_count`` 和 ``empties`` 等后续排序字段。
        """
        valid_target_skus = [s for s in sku_ids if s is not None]
        candidate_set = set(candidate_aisles)
        if simulated_positions_by_aisle is None:
            simulated_positions_by_aisle = self._build_simulated_positions_by_aisle(inventory_positions)

        target_suffixes = {
            s.split('-', 1)[1]
            for s in valid_target_skus
            if isinstance(s, str) and '-' in s
        }
        target_sku_set = set(valid_target_skus)

        per_aisle_sku_counts = {aisle: 0 for aisle in self.aisles}
        per_aisle_same_suffix_counts = {aisle: 0 for aisle in self.aisles}
        per_aisle_empty_counts = {aisle: 0 for aisle in self.aisles}
        per_aisle_direct_pair_counts = {aisle: 0 for aisle in self.aisles}
        per_aisle_anywhere_pair_counts = {aisle: 0 for aisle in self.aisles}

        def _position_sku_qty(pos: Any, sku_id: Optional[str]) -> int:
            """读取一个货位中指定 SKU 的实际数量，兼容单层与双层货位。

            Args:
                pos: 当前统计的 ``InventoryPosition`` 或等价货位对象。
                sku_id: 需要统计的 SKU；为空时不计数量。

            Returns:
                该 SKU 在上、下层或单层货位中的数量总和。
            """
            if not sku_id:
                return 0
            qty = 0
            if getattr(pos, "is_double_layer", False):
                if getattr(pos, "upper_sku", None) == sku_id:
                    qty += int(getattr(pos, "upper_quantity", 0) or 0)
                if getattr(pos, "lower_sku", None) == sku_id:
                    qty += int(getattr(pos, "lower_quantity", 0) or 0)
            else:
                if getattr(pos, "sku", None) == sku_id:
                    qty += int(getattr(pos, "quantity", 0) or 0)
            return qty

        for aisle, sim_positions in simulated_positions_by_aisle.items():
            for pos in sim_positions:
                if pos.is_empty():
                    # 空货位仅贡献可用容量，不参与已有 SKU、已有配对或后缀聚集统计。
                    per_aisle_empty_counts[aisle] += 1
                else:
                    for t_sku in valid_target_skus:
                        per_aisle_sku_counts[aisle] += _position_sku_qty(pos, t_sku)

                    for idx, t_sku in enumerate(sku_ids):
                        if not t_sku:
                            # 单梁归一化时的空槽位没有配对需求，跳过即可。
                            continue
                        pair_sku = self.sku_pairs.get(t_sku)
                        if not pair_sku or not pos.has_space():
                            # 没有 BOM 配对或该位置无法再写入时，即使库存中有 SKU 也不能形成新配对。
                            continue
                        attrs = sku_attrs[idx] if idx < len(sku_attrs) else {}
                        if pos.matches_sku(pair_sku, attrs, self.match_fields):
                            # 只有 SKU 和 match_fields 均匹配时才计为可利用的已有配对梁。
                            per_aisle_anywhere_pair_counts[aisle] += 1
                            is_single_beam_task = len(valid_target_skus) == 1
                            target_row = 1 if idx == 0 else 2
                            if is_single_beam_task or getattr(pos, "row", None) == target_row:
                                # 双梁需落在各自约定排才是“直接配对”；单梁不固定配对所在排。
                                per_aisle_direct_pair_counts[aisle] += 1

                    if target_suffixes:
                        if getattr(pos, "is_double_layer", False):
                            if (
                                pos.upper_sku
                                and pos.upper_sku not in target_sku_set
                                and isinstance(pos.upper_sku, str)
                                and '-' in pos.upper_sku
                                and pos.upper_sku.split('-', 1)[1] in target_suffixes
                                and pos.upper_quantity > 0
                            ):
                                per_aisle_same_suffix_counts[aisle] += pos.upper_quantity
                            if (
                                pos.lower_sku
                                and pos.lower_sku not in target_sku_set
                                and isinstance(pos.lower_sku, str)
                                and '-' in pos.lower_sku
                                and pos.lower_sku.split('-', 1)[1] in target_suffixes
                                and pos.lower_quantity > 0
                            ):
                                per_aisle_same_suffix_counts[aisle] += pos.lower_quantity
                        else:
                            if (
                                pos.sku
                                and pos.sku not in target_sku_set
                                and isinstance(pos.sku, str)
                                and '-' in pos.sku
                                and pos.sku.split('-', 1)[1] in target_suffixes
                                and pos.quantity > 0
                            ):
                                per_aisle_same_suffix_counts[aisle] += pos.quantity

        scored_aisles = []
        for aisle in self.aisles:
                scored_aisles.append({
                    'aisle': aisle,
                    'is_candidate': 0 if aisle in candidate_set else 1,
                    'direct_pair_count': per_aisle_direct_pair_counts[aisle],
                    'anywhere_pair_count': per_aisle_anywhere_pair_counts[aisle],
                    'sku_count': per_aisle_sku_counts[aisle],
                    'same_suffix_sku_count': per_aisle_same_suffix_counts[aisle],
                    'empties': per_aisle_empty_counts[aisle],
                })
        return scored_aisles


    def _get_pair_urgency_index(
        self,
        pl: Optional[int],
        sku: str,
        mate: Optional[str],
        sku_attrs: Optional[Dict[str, Any]] = None,
    ) -> int:
        """返回指定产线未来最早需要该配对的生产组距离。

        该值不以库存数量代替生产需求：从产线当前组开始扫描生产计划，任一任务
        使用 ``sku`` 或其配对 SKU 时返回相对组号。值越小，说明该配对越早影响
        产线推进；未绑定产线、无计划或未出现该 SKU 时返回大值作为中性优先级。

        Args:
            pl: 目标生产线编号；未指定时不施加生产计划紧急度。
            sku: 当前准备入库的 SKU。
            mate: ``sku`` 的 BOM 配对 SKU；不存在时只检查当前 SKU。
            sku_attrs: 当前 SKU 的 ``match_fields`` 属性；仅与属性兼容的计划需求计入紧急度。

        Returns:
            从当前组起的相对组号；``0`` 表示当前组即有需求，``9999`` 表示未找到兼容需求。
        """
        # 未指定产线或 SKU 时，返回大值作为最低优先级
        if pl is None or not sku:
            return 9999

        plan = (getattr(self.warehouse_core, 'production_plan', {}) or {}).get(pl)
        if not plan:
            return 9999
        current_group = (getattr(self.warehouse_core, 'production_line_current_group', {}) or {}).get(pl, 0)
        target_skus = {sku}
        if mate:
            target_skus.add(mate)
        plan_attrs = getattr(self.warehouse_core, 'production_plan_attrs', {}) or {}
        # 获取该计划中该 SKU 的配对 SKU
        for relative_index, group in enumerate(plan[current_group:]):
            absolute_group = current_group + relative_index
            # 检查该计划中该 SKU 的配对 SKU 是否已安排
            for task_index, task_skus in enumerate(group or []):
                for sku_index, planned_sku in enumerate(task_skus or []):
                    sku_id = planned_sku.get('skuId') if isinstance(planned_sku, dict) else planned_sku
                    if sku_id not in target_skus:
                        # 只关心本梁或其配对梁，其他 SKU 不影响该侧的配对紧急度。
                        continue
                    planned_attrs = {
                        field: (by_line.get(pl, [])[absolute_group][task_index][sku_index]
                                if absolute_group < len(by_line.get(pl, []))
                                and task_index < len(by_line.get(pl, [])[absolute_group])
                                and sku_index < len(by_line.get(pl, [])[absolute_group][task_index])
                                else None)
                        for field, by_line in plan_attrs.items()
                        if field in self.match_fields
                    }
                    # 未来计划带有属性时，只把真正可能与当前入库梁配对的需求
                    # 计入紧急度；未给出的字段沿用货位匹配的通配语义。
                    if all(
                        sku_attrs is None
                        or sku_attrs.get(field) is None
                        or planned_attrs.get(field) is None
                        or planned_attrs.get(field) == sku_attrs.get(field)
                        for field in self.match_fields
                    ):
                        return relative_index
        return 9999

    def _allocate_with_urgency_comparison(
        self,
        sku_ids,
        sku_attrs,
        target_pl,
        aisles,
        inventory_positions,
        simulated_positions_by_aisle: Optional[Dict[int, List[Any]]] = None,
        task_id: Optional[str] = None,
    ):
        """按单梁、天然配对双梁和非天然配对双梁分支生成候选巷道。

        该方法只负责巷道层面的组合选择；最终货位仍由
        ProposedPositionAllocator 在目标巷道内按层位、对称位置和空位
        规则完成，调用方会把返回位置转换为任务 positions。

        Args:
            sku_ids: 固定为两个槽位的 SKU 列表；单梁任务以 ``None`` 补齐另一槽位。
            sku_attrs: 与两个 SKU 对齐的匹配属性，用于过滤属性不兼容的已有梁和计划需求。
            target_pl: 两个 SKU 对应的目标产线编号，用于比较未来组配对紧急度。
            aisles: 已通过产线服务范围和容量校验的候选巷道。
            inventory_positions: 当前真实货位列表。
            simulated_positions_by_aisle: 已叠加前序入库占位的货位副本。
            task_id: 当前任务标识，仅供诊断和模拟排除自身使用，不参与评分。

        Returns:
            最终选中的巷道编号；所有候选均不可行时返回 ``None``。
        """
        # 第一个SKU匹配左排(Row1)，第二个SKU匹配右排(Row2)
        # --- 场景 A：单梁入库 ---
        # [SKU, None]
        is_solo_1 = sku_ids[1] is None
        # [None, SKU]
        is_solo_2 = sku_ids[0] is None
        # 1. 情况一：单梁入库
        if is_solo_1 or is_solo_2:
            # 单梁可以利用任一排的已有配对梁；因此先分别收集两排候选再合并。
            sku = sku_ids[0] if is_solo_1 else sku_ids[1]
            if sku is None:
                # 两个槽位均为空时不应进入分配；该保护同时帮助类型检查确认下游 SKU 非空。
                return None
            # 同时在左排和右排寻找配对
            cands_r1 = self._find_paired_beam_in_side(
                sku, sku_attrs[0] if is_solo_1 else sku_attrs[1], inventory_positions, 1,
                simulated_positions_by_aisle=simulated_positions_by_aisle,
            )
            cands_r2 = self._find_paired_beam_in_side(
                sku, sku_attrs[0] if is_solo_1 else sku_attrs[1], inventory_positions, 2,
                simulated_positions_by_aisle=simulated_positions_by_aisle,
            )

            # 合并所有能实现配对的巷道
            candidates = list(set(cands_r1) | set(cands_r2))
            candidates = [a for a in candidates if a in aisles]

            if candidates:
                # 已能直接形成配对时，优先在这些巷道中做库存分散
                # 只要能配对，就在这些巷道里选一个该产线分布最均匀的
                return self._balance_by_sku_distribution(
                    sku_ids, sku_attrs, candidates, inventory_positions, simulated_positions_by_aisle=simulated_positions_by_aisle
                )
                # return self._balance_by_production_line(target_pl, candidates, inventory_positions)
            else:
                # 所有巷道都没有属性匹配的配对梁，只能退化为常规库存/空位均衡。
                # 无法配对，全局均匀分布
                return self._balance_by_sku_distribution(
                    sku_ids, sku_attrs, aisles, inventory_positions, simulated_positions_by_aisle=simulated_positions_by_aisle
                )
                # return self._balance_by_production_line(target_pl, aisles, inventory_positions)
        # --- 场景 B：双梁入库 ---
        # 非天然配对双梁按历史排位约定处理：sku[0] 放入 row1，sku[1] 放入 row2。
        candidates1 = self._find_paired_beam_in_side(
            sku_ids[0], sku_attrs[0], inventory_positions, 1, simulated_positions_by_aisle=simulated_positions_by_aisle
        )
        candidates2 = self._find_paired_beam_in_side(
            sku_ids[1], sku_attrs[1], inventory_positions, 2, simulated_positions_by_aisle=simulated_positions_by_aisle
        )

        # 过滤物理不可达巷道
        candidates1 = [a for a in candidates1 if a in aisles]
        candidates2 = [a for a in candidates2 if a in aisles]

        # A. 同一巷道能同时解决两个配对
        both_match = list(set(candidates1) & set(candidates2))
        if both_match:
            # 两侧均可直接配对是最优情况：同巷道即可同时改善两根梁的后续可执行性。
            return self._balance_by_sku_distribution(
                sku_ids, sku_attrs, both_match, inventory_positions, simulated_positions_by_aisle=simulated_positions_by_aisle
            )
            # return self._balance_by_production_line(target_pl, both_match, inventory_positions)

        # B. 核心：如果两个SKU在不同巷道能配对，看谁的配对在未来计划中更紧急
        if candidates1 and candidates2:
            # 两侧分别有机会但不能落到同一巷道时，比较哪一侧更早影响对应产线生产组。
            mate1 = self.sku_pairs.get(sku_ids[0])
            mate2 = self.sku_pairs.get(sku_ids[1])
            urg_idx1 = self._get_pair_urgency_index(target_pl[0], sku_ids[0], mate1, sku_attrs[0])
            urg_idx2 = self._get_pair_urgency_index(target_pl[1], sku_ids[1], mate2, sku_attrs[1])
            # SKU1 配对更紧急。
            if urg_idx1 < urg_idx2:
                # 固定 SKU1 的直接配对机会，并尽可能让 SKU2 的配对梁也留在同一巷道。
                pref = self._prefer_same_aisle_with_mate(
                    candidates1, sku_ids[1], sku_attrs[1], inventory_positions, simulated_positions_by_aisle
                )
                if not pref:
                    pref = candidates1
                return self._balance_by_sku_distribution(
                    sku_ids, sku_attrs, pref, inventory_positions, simulated_positions_by_aisle
                )
            # SKU2 配对更紧急。
            elif urg_idx2 < urg_idx1:
                # 与上一分支对称，优先保留 SKU2 的直接配对机会。
                pref = self._prefer_same_aisle_with_mate(
                    candidates2, sku_ids[0], sku_attrs[0], inventory_positions, simulated_positions_by_aisle
                )
                if not pref:
                    pref = candidates2
                return self._balance_by_sku_distribution(
                    sku_ids, sku_attrs, pref, inventory_positions, simulated_positions_by_aisle
                )
            else:
                # 紧急度相同，不人为偏向任一产线，按同巷道后续移库机会和分布评分打破平局。
                # 优先：在能配对 SKU1 的巷道里，挑含 SKU2 配对梁的巷道；否则换另一侧；最后再均衡
                pref = self._prefer_same_aisle_with_mate(
                    candidates1, sku_ids[1], sku_attrs[1], inventory_positions, simulated_positions_by_aisle
                )
                if not pref:
                    pref = self._prefer_same_aisle_with_mate(
                        candidates2, sku_ids[0], sku_attrs[0], inventory_positions, simulated_positions_by_aisle
                    )
                if not pref:
                    pref = list(set(candidates1) | set(candidates2))
                return self._balance_by_sku_distribution(
                    sku_ids, sku_attrs, pref, inventory_positions, simulated_positions_by_aisle
                )

        # C. 只有单侧能配对
        if candidates1 or candidates2:
            # 仅一侧可配对时保留该机会；另一根梁再优先寻找其配对品同巷道的布局。
            options = list(set(candidates1) | set(candidates2))
            # 优先包含另一SKU配对品的巷道，便于同巷道移库
            other_sku = sku_ids[1] if candidates1 else sku_ids[0]
            pref = self._prefer_same_aisle_with_mate(
                options, other_sku, sku_attrs[1] if candidates1 else sku_attrs[0], inventory_positions,
                simulated_positions_by_aisle=simulated_positions_by_aisle
            )
            if not pref:
                pref = options
            return self._balance_by_sku_distribution(
                sku_ids, sku_attrs, pref, inventory_positions, simulated_positions_by_aisle=simulated_positions_by_aisle
            )

        # D. 无法配对，执行产线均匀分布
        # 尝试寻找含配对SKU的巷道以便同巷道移库；若仍无，则执行产线均匀分布
        mate_aisles_1 = self._find_aisles_with_mate_anywhere(
            sku_ids[0], sku_attrs[0], inventory_positions, simulated_positions_by_aisle=simulated_positions_by_aisle
        ) if sku_ids[0] else []
        mate_aisles_2 = self._find_aisles_with_mate_anywhere(
            sku_ids[1], sku_attrs[1], inventory_positions, simulated_positions_by_aisle=simulated_positions_by_aisle
        ) if sku_ids[1] else []
        options = list(set(mate_aisles_1) & set(mate_aisles_2))
        options = [a for a in options if a in aisles]
        if options:
            # 虽无直接配对，若两根梁的配对品已在同一巷道，后续移库仍可在巷道内完成。
            return self._balance_by_sku_distribution(
                sku_ids, sku_attrs, options, inventory_positions, simulated_positions_by_aisle=simulated_positions_by_aisle
            )
        return self._balance_by_sku_distribution(
            sku_ids, sku_attrs, aisles, inventory_positions, simulated_positions_by_aisle=simulated_positions_by_aisle
        )
    def _balance_by_sku_distribution(
        self,
        sku_ids: List[Optional[str]],
        sku_attrs: List[Dict[str, Any]],
        candidate_aisles: List[int],
        inventory_positions: List[Any],
        simulated_positions_by_aisle: Optional[Dict[int, List[Any]]] = None,
    ) -> Optional[int]:
        """基于可配对数量、SKU 分布和空货位数量进行巷道评分。

        Args:
            sku_ids: 当前任务的 SKU 槽位。
            sku_attrs: 当前任务各 SKU 的匹配属性。
            candidate_aisles: 需要参与评分的可行巷道。
            inventory_positions: 当前真实货位列表。
            simulated_positions_by_aisle: 前序入库占位后的货位视图。

        Returns:
            排序第一的巷道编号；没有可用巷道时返回 ``None``。
        """
        # 过滤掉没有可用位置的巷道
        candidate_aisles = [a for a in candidate_aisles if not simulated_positions_by_aisle or simulated_positions_by_aisle.get(a)]
        if not candidate_aisles:
            return None

        # 排序键首先保留可配对数量，再减少同 SKU 集中度，最后以巷道号保证结果稳定。
        scored_aisles = self._get_distribution_metrics(
            sku_ids,
            sku_attrs,
            candidate_aisles,
            inventory_positions,
            simulated_positions_by_aisle=simulated_positions_by_aisle,
        )
        scored_aisles = [item for item in scored_aisles if item['aisle'] in candidate_aisles]
        # 排序依据：可配对数量、SKU数量、同后缀SKU数量、空货位数量
        scored_aisles.sort(
            key=lambda x: (
                -x['direct_pair_count'],
                x['sku_count'],
                x['same_suffix_sku_count'],
                -x['empties'],
            )
        )
        return scored_aisles[0]['aisle'] if scored_aisles else None

    def _find_paired_beam_in_side(
        self,
        sku,
        sku_attrs: Dict[str, Any],
        inventory_positions,
        target_row,
        simulated_positions_by_aisle: Optional[Dict[int, List[Any]]] = None,
    ):
        """查找指定排中可直接补齐配对梁的巷道。

        Args:
            sku: 准备入库的 SKU。
            sku_attrs: 该 SKU 的 ``match_fields`` 属性。
            inventory_positions: 当前真实货位列表。
            target_row: 已有配对梁必须所在的排号。
            simulated_positions_by_aisle: 可选的模拟货位视图。

        Returns:
            去重后的巷道编号列表；仅包含存在属性匹配配对梁且仍可写入的巷道。
        """
        candidate_aisles = []
        pair_sku = self.sku_pairs.get(sku)
        if not pair_sku: return []
        aisle_positions_source = simulated_positions_by_aisle or self._build_simulated_positions_by_aisle(inventory_positions)
        for aisle, positions in aisle_positions_source.items():
            for pos in positions:
                if pos.row == target_row and not pos.is_empty():
                    if pos.matches_sku(pair_sku, sku_attrs, self.match_fields) and pos.has_space():
                        candidate_aisles.append(aisle)
        return list(set(candidate_aisles))

    def _find_aisles_with_mate_anywhere(
        self,
        sku: str,
        sku_attrs: Dict[str, Any],
        inventory_positions,
        simulated_positions_by_aisle: Optional[Dict[int, List[Any]]] = None,
    ) -> List[int]:
        """返回包含配对SKU的巷道（不要求特定排/层，用于同巷道移库可行性估计）

        Args:
            sku: 准备入库的 SKU。
            sku_attrs: 该 SKU 的 ``match_fields`` 属性。
            inventory_positions: 当前真实货位列表。
            simulated_positions_by_aisle: 可选的模拟货位视图。

        Returns:
            包含属性兼容配对梁且仍有写入空间的巷道编号列表。
        """
        mate = self.sku_pairs.get(sku)
        if not mate:
            return []
        aisles = set()
        aisle_positions_source = simulated_positions_by_aisle or self._build_simulated_positions_by_aisle(inventory_positions)
        for aisle, positions in aisle_positions_source.items():
            for pos in positions:
                if (not pos.is_empty()) and pos.has_space():
                    if pos.matches_sku(mate, sku_attrs, self.match_fields):
                        aisles.add(aisle)
        return list(aisles)

    def _prefer_same_aisle_with_mate(
        self,
        candidate_aisles: List[int],
        other_sku: str,
        other_sku_attrs: Dict[str, Any],
        inventory_positions,
        simulated_positions_by_aisle: Optional[Dict[int, List[Any]]] = None,
    ) -> List[int]:
        """将包含另一SKU配对品的巷道优先排序，便于后续同巷道移库

        Args:
            candidate_aisles: 已具备一侧直接配对机会的巷道列表。
            other_sku: 另一根待入库梁的 SKU。
            other_sku_attrs: 另一根梁的 ``match_fields`` 属性。
            inventory_positions: 当前真实货位列表。
            simulated_positions_by_aisle: 可选的模拟货位视图。

        Returns:
            优先包含另一根梁配对品的巷道列表；不存在时原样返回 ``candidate_aisles``。
        """
        if not other_sku:
            return candidate_aisles
        mate_aisles = set(
            self._find_aisles_with_mate_anywhere(
                other_sku,
                other_sku_attrs,
                inventory_positions,
                simulated_positions_by_aisle=simulated_positions_by_aisle,
            )
        )
        preferred = [aisle for aisle in candidate_aisles if aisle in mate_aisles]
        return preferred if preferred else candidate_aisles


# ==========================================
# 2. 货位分配器 (ProposedPositionAllocator)
# ==========================================
class ProposedPositionAllocator:
    """在已选巷道内选择货位，并支持前序入库的非破坏性模拟。

    ``PositionAllocator`` 的输出是与任务 SKU 顺序对应的位置列表；同一双层货位可在列表中出现两次，随后由 ``WarehouseCore`` 的上下层规则决定 UPPER/LOWER。
    """
    def __init__(self, warehouse_core):
        """初始化货位分配所需的仓库状态引用。

        Args:
            warehouse_core: ``WarehouseCore`` 实例，提供库存管理器、SKU 配对、
                时间估算器和 匹配属性配置。

        Returns:
            ``None``；初始化后实例持有上述依赖对象的只读引用。
        """
        # 核心状态入口；用于读取 pending/running 入库任务和当前堆垛机结束位置。
        self.warehouse_core = warehouse_core
        # 真实货位和 SKU 索引；正常 allocate 只读取，模拟函数在深拷贝副本上写入。
        self.inventory_manager = warehouse_core.inventory_manager
        # BOM 配对关系，用于区分天然配对和需要分别落位的非天然双梁。
        self.sku_pairs = warehouse_core.sku_pairs
        # 同一时间估算器，货位排序用其偏好层/路径规则保持与调度一致。
        self.time_estimator = warehouse_core.time_estimator
        # 货位 matches_sku 时必须同时满足的属性字段。
        self.match_fields = getattr(warehouse_core, 'match_fields', [])
        # 每列可等价的连续升降层数缓存。每个分配器实例只按物理配置计算一次，
        # 后续所有候选只做加权曼哈顿距离，避免 API 对每个货位重复运行加减速模型。
        self._levels_per_column: Optional[float] = None

    # ========================================================================
    # 主流程：入库货位选择
    # ========================================================================
    def allocate(self, inventory_positions: List[Any], task_info: Any, current_position: Optional[InventoryPosition] = None) -> List[Any]:
        """在任务所选择的巷道内生成与 SKU 顺序对应的候选货位。

        单梁先查找已有配对梁；天然配对双梁优先同一空双层货位；非天然配对双梁分别在左右排查找直接配对、对称空位和最近空位。返回空列表代表该巷道无合法落位。

        局部变量：
        ``positions`` 是指定巷道的局部货位视图；
        ``sku_ids`` 是 SKU 列表；
        ``attrs1/attrs2`` 是按 ``match_fields`` 取出的匹配属性；
        ``beam_side_1/2``是单梁所在的排；
        ``default_pref_level`` 是路径层位偏好；
        ``result`` 为与输入SKU对应的候选货位列表。

        Args:
            inventory_positions: 待筛选的货位列表；只读取，不写入真实库存。
            task_info: 包含 ``assigned_aisle``、``skus`` 和可选 ``task_id`` 的任务对象或字典。
            current_position: 当前堆垛机所在货位；传入时其层号作为优选层位。

        Returns:
            与有效 SKU 输入顺序对应的 ``InventoryPosition`` 列表；无合法落位时返回空列表。
        """
        # 任务标识只用于告警日志；货位选择依据 SKU、属性和指定巷道。
        task_id = _get_task_field(task_info, "task_id") or _get_task_field(task_info, "id")
        # 已由巷道分配阶段决定的目标巷道；兼容内部字段和 API 字段名。
        aisle = (
            _get_task_field(task_info, "assigned_aisle")
            or _get_task_field(task_info, "aisle")
            or _get_task_field(task_info, "aisleId")
        )
        # 原始 SKU 条目保留属性，用于后续 match_fields 校验，而不仅按 skuId 分配。
        skus_data = _get_task_field(task_info, "skus", []) or []
        # 货位分配必须保持与任务 SKU 顺序一致，且必须提供已选择的巷道号
        if not skus_data:
            print(f"[WARN][allocator] ProposedPositionAllocator: empty skus task={task_id}")
            return []
        if aisle is None:
            print(f"[WARN][allocator] ProposedPositionAllocator: missing aisle task={task_id}")
            return []
        # 只保留目标巷道货位，避免下游辅助函数跨巷道取到不一致的位置。
        positions = [p for p in inventory_positions if p.aisle == aisle]
        # SKU 顺序决定返回 positions 的顺序，也决定 WarehouseCore 写入上下层时的映射。
        sku_ids = [s.get('skuId') if isinstance(s, dict) else s for s in skus_data]
        # 两根梁各自参与匹配的属性；缺失 SKU 时使用空字典以保持分支可处理。
        attrs1 = self._get_sku_attrs(skus_data, sku_ids[0]) if len(sku_ids) > 0 else {}
        attrs2 = self._get_sku_attrs(skus_data, sku_ids[1]) if len(sku_ids) > 1 else {}
        # 单梁的优先排位；beamSide/legacy side 用于避免左右梁被反向放置。
        beam_side_1 = self._resolve_single_beam_side(skus_data, 0)
        beam_side_2 = self._resolve_single_beam_side(skus_data, 1)
        # 选择当前堆垛机所在层作为偏好层位，若未传入当前货位则使用仓库中间层作为偏好层
        default_pref_level = self._get_default_preferred_level(current_position)
        # 是否为左单梁/右单梁
        is_solo_1 = sku_ids[1] is None if len(sku_ids) > 1 else True
        is_solo_2 = sku_ids[0] is None if len(sku_ids) > 1 else False
        # 三类入库场景共用返回格式，结果顺序必须与 skus_data 一致。
        if is_solo_1:
            # [SKU, None]：第一槽位为有效单梁。先在两排扫描已有属性匹配的配对梁；均不存在时依据 beamSide 选择对应排的空位
            home_row = 2 if str(beam_side_1).upper() == "RIGHT" else 1
            guest_row = 1 if home_row == 2 else 2
            result = self._allocate_single_beam_flexible(
                positions, sku_ids[0], default_pref_level, home_row=home_row, guest_row=guest_row,
                sku_attrs=attrs1
            )
        elif is_solo_2:
            # [None, SKU]：第二槽位为有效单梁，先在两排扫描已有属性匹配的配对梁；均不存在时依据 beamSide 选择对应排的空位
            home_row = 2 if str(beam_side_2).upper() == "RIGHT" else 1
            guest_row = 1 if home_row == 2 else 2
            result = self._allocate_single_beam_flexible(
                positions, sku_ids[1], default_pref_level, home_row=home_row, guest_row=guest_row,
                sku_attrs=attrs2
            )
        elif self.sku_pairs.get(sku_ids[0]) == sku_ids[1] and self._attrs_equal(attrs1, attrs2):
            # 天然配对且属性一致时复用同一空双层货位。返回两次同一对象是有意为之：
            # 后续 _resolve_inbound_layer 根据 row 和输入顺序分别写 upper/lower，
            # 不能把它简化成只返回一个位置，否则任务 SKU 到位置的映射会丢失。
            pos = self._find_best_empty_slot(positions, default_pref_level)
            result = [pos, pos] if pos else []
        else:
            # 非天然配对或属性不同的双梁不能共用一个双层位置，分别按左右排寻找落位；
            # _allocate_double_constrained 会排除已经分给另一根梁的位置，防止产生“同一层被两根梁占用”的结果。
            result = self._allocate_double_constrained(
                positions, sku_ids, default_pref_level, default_pref_level, attrs1, attrs2,
            )

        if not result:
            print(f"[WARN][allocator] ProposedPositionAllocator: no position allocated task={task_id} aisle={aisle} skus={sku_ids}")
        return result

    # ========================================================================
    # 阶段处理：待入库任务的模拟占位
    # ========================================================================
    def simulate_pending_positions_for_aisle(
        self,
        aisle: int,
        inventory_positions: List[Any],
        current_task: Any = None,
        current_position: Optional[InventoryPosition] = None,
    ) -> List[Any]:
        """构造某巷道的模拟货位快照，反映前序入库将造成的占位。

        先将 ``running_tasks`` 中已经启动的入库映射到副本，再按 pending 队列
        处理已冻结 positions 的任务；未冻结任务调用 ``allocate`` 在副本上落位。
        输入真实货位不会被修改；返回副本供巷道评分和后续任务位置选择使用。

        局部变量：``simulated_positions`` 是目标巷道深拷贝；
        ``simulated_map`` 用位置主键把运行/等待任务的冻结位置映射回副本；
        ``running_tasks``、``pending_tasks`` 按先后顺序叠加；
        ``allocated``是防止存在之前分配入库任务时因货位不足产生空货位，临时使未冻结 pending 任务在该副本上的临时落位结果。

        Args:
            aisle: 需要模拟的巷道编号。
            inventory_positions: 当前真实货位列表；会被深拷贝后再写入模拟入库结果。
            current_task: 当前预览任务；其自身会从 running/pending 占位中排除。
            current_position: 传给未冻结 pending 任务分配器的当前位置，用于保持层位偏好一致。

        Returns:
            已叠加 running 和 pending 入库占位的货位副本
        """
        simulated_positions = [deepcopy(p) for p in inventory_positions if p.aisle == aisle]
        # 巷道不存在货位时无法建立快照，返回空列表让上游排除该巷道。
        if not simulated_positions:
            return []

        simulated_map = {p.get_position_id(): p for p in simulated_positions}
        current_task_id = getattr(current_task, 'task_id', None) if current_task is not None else None
        running_tasks = getattr(self.warehouse_core, 'running_tasks', {}) or {}
        pending_by_aisle = getattr(self.warehouse_core, 'pending_inbound_by_aisle', {}) or {}
        pending_tasks = list(pending_by_aisle.get(aisle, []))

        # running 入库必然早于当前任务完成，先占用；固定 pending positions 是
        # 已承诺的落位，必须在未固定 pending 的推演之前生效。
        # 第一轮只重放 running 入库：它们已经开始执行，位置必须视为确定占位
        for running_task in running_tasks.values():
            if getattr(running_task, 'task_type', None) != 'INBOUND':
                # 出库 running 不会新增库存，不应污染入库货位的模拟占位。
                continue
            if getattr(running_task, 'assigned_aisle', None) != aisle:
                # 快照只描述一个巷道，其他巷道的入库任务与其容量无关。
                continue
            if current_task_id and getattr(running_task, 'task_id', None) == current_task_id:
                # 预览当前任务时排除自身，避免同一任务先占位后又被判定无位。
                continue
            running_positions = []
            for pos in getattr(running_task, 'positions', []) or []:
                sim_pos = simulated_map.get(pos.get_position_id())
                if sim_pos is not None:
                    running_positions.append(sim_pos)
            if running_positions:
                # running 任务已有冻结位置，直接映射并写入副本，不再触发新的货位选择。
                if not self._apply_simulated_inbound(running_positions, getattr(running_task, 'skus', []) or []):
                    return []

        # 第二轮重放同巷道 pending 入库。已冻结任务直接写副本；未冻结任务按队列
        # 顺序重新分配并立即写副本，使后续任务能够看到所有前序容量消耗。
        for pending_task in pending_tasks:
            if current_task_id and getattr(pending_task, 'task_id', None) == current_task_id:
                # 当前任务尚未在本次选择前占位，必须从 pending 队列中排除。
                continue
            fixed_positions = []
            for pos in getattr(pending_task, 'positions', []) or []:
                sim_pos = simulated_map.get(pos.get_position_id())
                if sim_pos is not None:
                    fixed_positions.append(sim_pos)
            if fixed_positions:
                # 固化位置来自 API 已承诺的落位，不能由后续模拟重新选择。
                if not self._apply_simulated_inbound(fixed_positions, getattr(pending_task, 'skus', []) or []):
                    return []
                continue
            sim_task = self._build_task_for_simulation(
                getattr(pending_task, 'skus', []) or [],
                aisle,
                getattr(pending_task, 'task_id', None),
            )
            # 未冻结任务才允许在当前模拟快照中重新计算位置；失败时不伪造占位。
            allocated = self.allocate(
                simulated_positions,
                sim_task,
                current_position=current_position,
            )
            if not allocated:
                # 未冻结 pending 在当前副本中无法落位时，不伪造库存写入；后续任务基于实际剩余容量判断。
                continue
            if not self._apply_simulated_inbound(allocated, sim_task.skus):
                return []

        return simulated_positions


    # ========================================================================
    # 辅助函数：属性、层位与候选货位选择
    # ========================================================================
    def _extract_attrs(self, sku_entry: Optional[Dict]) -> Dict:
        """从单个 SKU 条目中抽取配置要求参与匹配的属性。

        Args:
            sku_entry: 请求或任务中的 SKU 字典，例如包含 ``version``、
                ``productionAttribute`` 等字段。

        Returns:
            键为 ``match_fields`` 的属性字典；未配置匹配字段或输入不是字典时返回空字典。
        """
        if not self.match_fields or not isinstance(sku_entry, dict):
            return {}
        return {k: sku_entry.get(k) for k in self.match_fields}

    def _attrs_equal(self, attrs_a: Optional[Dict], attrs_b: Optional[Dict]) -> bool:
        """比较两根梁在配置要求的配对属性上是否完全一致。

        输入：两组 SKU 属性字典。
        输出：未配置匹配字段时为 ``True``；否则每个 ``match_fields`` 字段相同才返回 ``True``。
        作用：天然 SKU 对只有在属性也一致时才能进入同一双层货位。

        Args:
            attrs_a: 第一根梁从任务条目提取的匹配属性。
            attrs_b: 第二根梁从任务条目提取的匹配属性。

        Returns:
            未配置 ``match_fields`` 时为 ``True``；否则仅所有配置字段相等时为 ``True``。
        """
        if not self.match_fields:
            return True
        if attrs_a is None or attrs_b is None:
            return False
        for field in self.match_fields:
            if attrs_a.get(field) != attrs_b.get(field):
                return False
        return True

    def _get_sku_attrs(self, skus_data: List[Dict], sku_id: Optional[str]) -> Dict:
        """按 SKU 标识从任务条目中取得其参与匹配的属性。

        Args:
            skus_data: 任务的原始 ``skus`` 列表。
            sku_id: 需要查找属性的 SKU 标识。

        Returns:
            对应 SKU 的 ``match_fields`` 属性；任务未携带该 SKU 时返回空字典。
        """
        for entry in skus_data or []:
            if isinstance(entry, dict) and entry.get('skuId') == sku_id:
                return self._extract_attrs(entry)
        return {}

    def _resolve_single_beam_side(self, skus_data: List[Dict], sku_index: int) -> Optional[str]:
        """确定单梁应优先使用的左右排。

        输入：任务 SKU 列表和待判断条目的下标。
        输出：``LEFT``、``RIGHT`` 或 ``None``。
        优先读取 ``beamSide``，兼容旧字段 ``side=A/B``；只有一根有效梁时再按其所在
        索引提供默认左右侧，防止空占位条目改变单梁存放方向。

        Args:
            skus_data: 任务原始 SKU 条目，支持 ``beamSide`` 和兼容字段 ``side``。
            sku_index: 需要判断的 SKU 槽位下标。

        Returns:
            ``LEFT``、``RIGHT`` 或 ``None``；缺少明确方向且不能由单梁槽位推断时返回 ``None``。
        """
        if sku_index < 0 or sku_index >= len(skus_data):
            return None
        entry = skus_data[sku_index]
        if not isinstance(entry, dict):
            return None

        beam_side = getattr(entry.get("beamSide"), "value", entry.get("beamSide"))
        if beam_side:
            side_str = str(beam_side).upper()
            if side_str in {"LEFT", "RIGHT"}:
                return side_str

        legacy_side = getattr(entry.get("side"), "value", entry.get("side"))
        if legacy_side:
            legacy_str = str(legacy_side).upper()
            if legacy_str == "A":
                return "LEFT"
            if legacy_str == "B":
                return "RIGHT"

        if len(skus_data) == 2:
            first_sku = skus_data[0].get("skuId") if isinstance(skus_data[0], dict) else None
            second_sku = skus_data[1].get("skuId") if isinstance(skus_data[1], dict) else None
            if first_sku and not second_sku:
                return "LEFT"
            if second_sku and not first_sku:
                return "RIGHT"

        return None

    def _get_default_preferred_level(self, current_position: Optional[InventoryPosition] = None) -> int:
        """确定空位排序使用的优选层位。

        Args:
            current_position: 当前设备位置或任务位置；有层号时优先沿用该层。

        Returns:
            优选层号；无当前位置时取仓库中部层或默认第 8 层。
        """
        if current_position is not None and getattr(current_position, "level", None) is not None:
            return current_position.level
        num_levels = getattr(self.inventory_manager, "num_levels", 0) or 0
        if num_levels > 0:
            return max(int(num_levels / 2), 8)
        return 8

    def _get_preferred_column_from_candidates(self, candidates: List[InventoryPosition]) -> int:
        """从当前可用候选列中取得与配置列号模式一致的参考列。

        Args:
            candidates: 已完成空位、排号等约束过滤的候选货位。

        Returns:
            int: 模式 1 返回候选中的最小列，模式 2 返回最接近中间的候选列，模式 3
            返回候选中的最大列；候选为空时回退为第 1 列。
        """
        candidate_columns = {int(position.column) for position in candidates}
        if not candidate_columns:
            return 1
        reference_aisle = int(candidates[0].aisle)
        return min(
            candidate_columns,
            key=lambda column: self.warehouse_core.get_position_column_preference_key(
                reference_aisle,
                column,
            ),
        )

    def _get_levels_per_column(self) -> float:
        """计算并缓存“移动一列时间内可连续升降的层数”。

        Args:
            无显式参数；读取 ``time_estimator.physics_params`` 中的物理距离、速度和加速度。

        Returns:
            float: 一列对应的连续升降层数。该比例不以单层启停时间线性外推，而是
            比较完整的一列移动与连续升降多层的物理时间。
        """
        if self._levels_per_column is not None:
            return self._levels_per_column

        physics_params = getattr(self.time_estimator, "physics_params", {}) or {}
        try:
            column_step_time = float(
                self.time_estimator._physics_time_2d(1, 0, **physics_params)
            )
            single_level_time = float(
                self.time_estimator._physics_time_2d(0, 1, **physics_params)
            )
            if column_step_time <= 0 or single_level_time <= 0:
                raise ValueError("单步物理移动时间必须为正数")

            # 连续升降不应把单层启停时间重复乘以层数。对单调的层方向物理时间函数
            # 二分查找：在完成一次横移一列的时间内，最多能完成多少层连续升降。
            max_layer_delta = max(1, int(getattr(self.inventory_manager, "num_levels", 1) or 1) - 1)
            if single_level_time > column_step_time:
                # 极端情况下单层已经慢于一列，保留小于 1 的等价比例。
                levels_per_column = max(column_step_time / single_level_time, 0.01)
            else:
                low, high = 1, max_layer_delta
                while low < high:
                    mid = (low + high + 1) // 2
                    layer_time = float(
                        self.time_estimator._physics_time_2d(0, mid, **physics_params)
                    )
                    if layer_time <= column_step_time:
                        low = mid
                    else:
                        high = mid - 1
                levels_per_column = float(low)
        except (AttributeError, TypeError, ValueError):
            # 无估算器或配置异常时退回等权距离，确保货位选择仍然可用。
            levels_per_column = 1.0

        self._levels_per_column = levels_per_column
        return self._levels_per_column

    def _estimate_column_level_move_time(self, column_delta: int, level_delta: int) -> float:
        """按“一列可连续升降层数”计算列等价的加权曼哈顿距离。

        Args:
            column_delta: 起终点列号之差的绝对值。
            level_delta: 起终点层号之差的绝对值。

        Returns:
            float: ``列差 + 层差 / 每列等价层数``。等价层数由完整一列移动与连续多层
            升降的物理时间比较得到，避免把单层启停时间重复叠加；本方法不逐货位调用
            复杂模型。
        """
        safe_column_delta = abs(int(column_delta))
        safe_level_delta = abs(int(level_delta))
        levels_per_column = self._get_levels_per_column()
        return safe_column_delta + safe_level_delta / levels_per_column



    def _build_task_for_simulation(
        self,
        skus_data: List[Dict],
        aisle: int,
        task_id: Optional[str] = None,
    ) -> Any:
        """构造仅供模拟货位分配使用的轻量入库任务对象。

        Args:
            skus_data: 需要模拟写入的原始 SKU 条目。
            aisle: 该模拟任务固定使用的巷道。
            task_id: 原 pending 任务标识；缺失时生成仅用于模拟的标识。

        Returns:
            带有 ``task_id``、``task_type``、``skus`` 和 ``assigned_aisle`` 属性的临时对象。
        """
        return type(
            "SimTaskData",
            (),
            {
                "task_id": task_id or f"SIM_INBOUND_{aisle}",
                "task_type": "INBOUND",
                "skus": deepcopy(skus_data),
                "assigned_aisle": aisle,
            },
        )()

    def _resolve_inbound_layer(
        self,
        pos: InventoryPosition,
        allocated_positions: List[InventoryPosition],
        sku_count: int,
        non_null_idx: int,
    ) -> Optional[str]:
        """根据冻结位置和双层规则确定当前 SKU 的实际落位层。

        输入：目标货位、按 SKU 顺序分配的位置列表、SKU 数量和当前有效 SKU 下标。
        输出：``upper``、``lower`` 或无合法层位时的 ``None``。
        同一双层位置的双梁按排号固定上下层；若使用不同位置则优先填已有配对梁的空层。

        Args:
            pos: 当前待写入的双层货位。
            allocated_positions: 本任务已选货位，顺序与有效 SKU 顺序一致。
            sku_count: 本任务实际需要写入的 SKU 数量。
            non_null_idx: 当前 SKU 在有效 SKU 序列中的下标。

        Returns:
            合法写入层 ``upper`` 或 ``lower``；无可用层或层序冲突时返回 ``None``。
        """
        if pos.is_double_layer and sku_count > 1:
            # 双梁任务的层位要同时满足“同位上下层配对”或“各自位置可写”的约束。
            if len(allocated_positions) == 2:
                if allocated_positions[0] == allocated_positions[1]:
                    # 天然配对复用同一位置：排号决定第一、第二根梁的上下层方向。
                    if pos.row == 1:
                        return 'upper' if non_null_idx == 0 else 'lower'
                    if pos.row == 2:
                        return 'lower' if non_null_idx == 0 else 'upper'
                    return None
                if ((pos.upper_sku is not None and pos.upper_sku != '') and
                    (pos.lower_sku is None or pos.lower_sku == '')):
                    # 已有上层库存时只能填下层，避免破坏现有梁的层位。
                    return 'lower'
                if ((pos.upper_sku is None or pos.upper_sku == '') and
                    (pos.lower_sku is None or pos.lower_sku == '')):
                    # 两层均空且本梁使用独立位置时，以 upper 作为稳定的默认写入层。
                    return 'upper'
                return None
            return 'upper' if non_null_idx == 0 else 'lower'

        if pos.is_double_layer and sku_count == 1:
            # 单梁优先放 upper；upper 需要双层全空，若不满足才检查 lower 是否仍可合法写入。
            if pos.can_place_sku('upper'):
                return 'upper'
            if pos.can_place_sku('lower'):
                return 'lower'
            return None

        return None

    def _apply_simulated_inbound(
        self,
        allocated_positions: List[InventoryPosition],
        skus_data: List[Dict],
    ) -> bool:
        """将一次入库结果写入货位副本，供后续任务继续推演。

        ``allocated_positions`` 与 SKU 输入顺序对应；两根梁共用一个双层货位时，
        ``_resolve_inbound_layer`` 按排位规则确定上下层。本方法只改副本，返回
        ``False`` 表示前序占位已使该模拟序列不可行。

        Args:
            allocated_positions: 已为本次模拟选择的货位；可包含同一双层货位两次。
            skus_data: 对应任务的原始 SKU 条目，空槽位不会占用货位。

        Returns:
            所有有效 SKU 均成功写入副本时为 ``True``；任一层位冲突时为 ``False``。
        """
        sku_entries = [s for s in skus_data if isinstance(s, dict)]
        sku_ids = [s.get('skuId') for s in sku_entries if s.get('skuId') is not None]
        non_null_idx = 0

        for sku_entry in sku_entries:
            sku_id = sku_entry.get('skuId')
            if sku_id is None:
                # 允许 API 用空槽位表示单梁左右侧，空槽位不消耗 positions 下标。
                continue

            # 单位置双梁复用同一位置对象；多位置任务按 SKU 输入顺序一一取位。
            pos = allocated_positions[min(non_null_idx, len(allocated_positions) - 1)]
            layer = self._resolve_inbound_layer(pos, allocated_positions, len(sku_ids), non_null_idx)
            attrs = self._extract_attrs(sku_entry)

            if pos.is_double_layer:
                # 双层货位按 _resolve_inbound_layer 给出的层写入，并同步保存该层属性。
                if layer == 'upper':
                    if pos.upper_quantity > 0:
                        return False
                    pos.upper_sku = sku_id
                    pos.upper_quantity = 1
                    pos.upper_attrs = attrs
                elif layer == 'lower':
                    if pos.lower_quantity > 0:
                        return False
                    pos.lower_sku = sku_id
                    pos.lower_quantity = 1
                    pos.lower_attrs = attrs
                else:
                    # 位置已满、层序不合法或双梁布局不完整时，该模拟序列整体不可行。
                    return False
            else:
                # 单层库没有上下层选择，必须保持整个货位为空才能写入。
                if not pos.is_empty():
                    return False
                pos.sku = sku_id
                pos.quantity = 1
                pos.sku_attrs = attrs

            non_null_idx += 1

        return True


    def _allocate_single_beam_flexible(
        self,
        positions,
        sku,
        pref_level,
        home_row,
        guest_row,
        sku_attrs: Optional[Dict] = None,
    ):
        """按原有优先级为单梁选择配对位置或优先排空位。

        Args:
            positions: 已选巷道中的货位。
            sku: 待入库 SKU。
            pref_level: 优选层位。
            home_row: 单梁的优先排。
            guest_row: 保留的兼容参数，原逻辑不以其筛选空位。
            sku_attrs: 参与属性匹配的字段。

        Returns:
            List[InventoryPosition]: 仅含一个位置的列表；优先返回已有属性匹配配对梁
            所在货位，其次返回 ``home_row`` 的最佳空位；没有合法位置时为空列表。
        """
        mate = self.sku_pairs.get(sku)
        if mate:
            # 原逻辑在两排查找已有配对梁，不以单梁偏好排限制配对机会。
            # 两排均需检查：beamSide 约束的是无配对时的新开空位方向，不应阻止
            # 单梁补齐另一排既有配对梁，从而避免降低直接出库比例。
            for row in [1, 2]:
                matched = self._find_mate_in_row(positions, mate, row, sku_attrs=sku_attrs)
                if matched:
                    # 只要一侧已有属性匹配的配对梁，即优先补齐该位置而不新开空位。
                    return [matched]
        # 没有可补齐的配对梁时，才按单梁优先排和层位偏好选择全空货位。
        pos = self._find_best_empty_slot(positions, pref_level, row=home_row)
        return [pos] if pos else []

    def _allocate_double_constrained(
        self,
        positions, sku_ids, pref_level1, pref_level2,
        attrs1: Optional[Dict] = None, attrs2: Optional[Dict] = None,
    ):
        """按原有双梁约束逻辑返回已找到的有效位置。

        Args:
            positions: 已选巷道中的货位。
            sku_ids: 两根梁的 SKU 顺序。
            pref_level1: 第一根梁的优选层位。
            pref_level2: 第二根梁的优选层位。
            attrs1: 第一根梁的匹配属性。
            attrs2: 第二根梁的匹配属性。

        Returns:
            List[InventoryPosition]: 两根梁均成功落位时按输入 SKU 顺序返回两个位置；
            若原有策略未能为某一根梁找到位置，则返回已找到的非空位置供调用方判定
            该组合是否完整，不在此处虚构第二个货位。
        """
        res = [None, None]
        sku1, sku2 = sku_ids[0], sku_ids[1]
        mate1, mate2 = self.sku_pairs.get(sku1), self.sku_pairs.get(sku2)
        target_p1 = self._find_mate_in_row(positions, mate1, 1, sku_attrs=attrs1)
        target_p2 = self._find_mate_in_row(positions, mate2, 2, sku_attrs=attrs2)

        if target_p1 and target_p2:
            # 两根梁各自都有直接配对位置，分别落在对应排，不再为对称布局牺牲配对机会。
            return [target_p1, target_p2]

        if target_p1:
            # 先固定第一根梁的直接配对位置；第二根梁优先借助其配对品的对侧空位布局。
            res[0] = target_p1
            mate_target = self._find_mate_in_row(
                positions, mate2, 2, sku_attrs=attrs2, exclude_position=target_p1
            )
            opposite = (
                self._get_opposite_slot(positions, mate_target, 1, exclude_position=target_p1)
                if mate_target else None
            )
            res[1] = opposite if opposite else self._find_nearest_empty(
                positions, target_p1, 2, pref_level2, exclude_position=target_p1
            )
        elif target_p2:
            # 与上一分支对称：第二根梁有直接配对时，围绕该位置为第一根梁安排相邻位置。
            res[1] = target_p2
            mate_target = self._find_mate_in_row(
                positions, mate1, 1, sku_attrs=attrs1, exclude_position=target_p2
            )
            opposite = (
                self._get_opposite_slot(positions, mate_target, 2, exclude_position=target_p2)
                if mate_target else None
            )
            res[0] = opposite if opposite else self._find_nearest_empty(
                positions, target_p2, 1, pref_level1, exclude_position=target_p2
            )
        else:
            # 两侧均无直接配对，从第一排最佳空位起步，再为第二排寻找同列或最近空位。
            res[0] = self._find_best_empty_slot(positions, pref_level1, row=1)
            if res[0]:
                opposite = self._get_opposite_slot(positions, res[0], 2, exclude_position=res[0])
                res[1] = opposite if opposite else self._find_nearest_empty(
                    positions, res[0], 2, pref_level2, exclude_position=res[0]
                )
        return [pos for pos in res if pos]

    def _find_best_empty_slot(self, positions, pref_level, row=None):
        """从指定排的空位中选择列等价加权距离更小的位置。

        Args:
            positions: 已选巷道内的候选货位。
            pref_level: 任务当前位置对应的优选层号。
            row: 需要限定的排号；不传时两排均可参与。

        Returns:
            排序最优的空货位；没有符合排号和空位条件的货位时返回 ``None``。
        """
        empty_slots = [p for p in positions if p.is_empty()]
        if row is not None:
            empty_slots = [p for p in empty_slots if p.row == row]

        if not empty_slots: return None

        # 参考列由最小列/中间列/最大列模式确定；参考层来自当前堆垛机层或默认层。
        # 不能再把列号和层号拆成固定先后顺序，否则设备列/层速度变化时会选到较慢位置。
        reference_column = self._get_preferred_column_from_candidates(empty_slots)

        def _score(position: InventoryPosition) -> Tuple[float, float, int, int, int]:
            """计算空位相对于列号模式和优选层位的加权距离评分。

            Args:
                position: 已过滤为可用的候选空货位。

            Returns:
                tuple[float, ...]: 列等价加权距离优先；距离相同后依次按列号模式、层差、
                排号稳定排序。
            """
            weighted_distance = self._estimate_column_level_move_time(
                position.column - reference_column,
                position.level - pref_level,
            )
            column_preference = self.warehouse_core.get_position_column_preference_key(
                position.aisle,
                position.column,
            )
            return (
                weighted_distance,
                *column_preference,
                abs(position.level - pref_level),
                position.row,
            )

        empty_slots.sort(key=_score)
        return empty_slots[0]

    def _find_mate_in_row(self, positions, mate_sku, target_row, sku_attrs: Optional[Dict] = None,
                          exclude_position: Optional[InventoryPosition] = None):
        """在指定排按配置的列号择优规则查找属性匹配且仍有空间的配对梁。

        Args:
            positions: 已选巷道内的候选货位。
            mate_sku: 待入库梁对应的 BOM 配对 SKU。
            target_row: 配对梁应当所在的排号。
            sku_attrs: 待入库梁的 ``match_fields`` 属性；缺失字段按货位匹配规则作为通配。
            exclude_position: 已分配给另一根梁的位置，避免非天然双梁重复引用。

        Returns:
            列号偏好最高的首个可补齐配对梁货位；找不到时返回 ``None``。
        """
        if not mate_sku:
            # 非配对 SKU 不存在“补齐已有梁”的候选，调用方将退化到空位布局。
            return None
        # 已有配对梁也使用同一列号偏好，避免单梁与双梁任务采用相反的择优方向。
        row_pos = sorted(
            [p for p in positions if p.row == target_row],
            key=lambda p: self.warehouse_core.get_position_column_preference_key(p.aisle, p.column),
        )
        for p in row_pos:
            if exclude_position is not None and p.get_position_id() == exclude_position.get_position_id():
                # 非天然双梁不能同时选中同一个位置，排除已固定给另一根梁的位置。
                continue
            if not p.is_empty() and p.matches_sku(mate_sku, sku_attrs, self.match_fields) and p.has_space():
                # matches_sku 校验 SKU 与属性；has_space 同时校验上下层顺序、预留和禁用状态。
                return p
        return None

    def _get_opposite_slot(self, positions, target_pos, target_row,
                           exclude_position: Optional[InventoryPosition] = None):
        """取得同列同层、另一排的空货位。

        Args:
            positions: 已选巷道内的候选货位。
            target_pos: 已固定的一根梁或其配对梁的位置。
            target_row: 需要查找的对侧排号。
            exclude_position: 不可再次使用的位置，通常是另一根梁已选位置。

        Returns:
            同列同层的对侧空货位；不存在或已被排除时返回 ``None``。
        """
        for p in positions:
            if (
                p.row == target_row
                and p.column == target_pos.column
                and p.level == target_pos.level
                and p.is_empty()
            ):
                if exclude_position is not None and p.get_position_id() == exclude_position.get_position_id():
                    continue
                # 同列同层的对侧空位能减少横向移动，也便于后续形成对称存放布局。
                return p
        return None

    def _find_nearest_empty(self, positions, target_pos, target_row, pref_level=None,
                            exclude_position: Optional[InventoryPosition] = None):
        """在目标排寻找距离固定配对位置最近的空位。

        排序先比较固定配对位置到候选位置的列等价加权距离，再按配置列号方向和
        优选层位消除并列。列、层速度或加速度改变后，等价层数会同步改变排序结果。

        Args:
            positions: 已选巷道内的候选货位。
            target_pos: 已固定位置，作为距离排序的参考点。
            target_row: 第二根梁必须使用的排号。
            pref_level: 空位层号的偏好值；仅在加权距离和列号偏好相同时参与排序。
            exclude_position: 已被另一根梁占用、不可复用的位置。

        Returns:
            目标排中评分最低的空货位；目标排无空位时返回 ``None``。
        """
        empty_slots = [p for p in positions if p.row == target_row and p.is_empty()]
        if exclude_position is not None:
            empty_slots = [p for p in empty_slots if p.get_position_id() != exclude_position.get_position_id()]
        if not empty_slots:
            # 目标排没有可用位置时，调用方会把该双梁布局判为不可行或尝试其他候选巷道。
            return None
        # 优先同列同层，其次同列层差更小
        def _score(p):
            """计算候选空位相对于固定位置的字典序评分。

            Args:
                p: 待评分的空货位。

            Returns:
                ``(加权距离, 列号偏好, 层差)``；元组越小，说明二维行程更短且更符合
                配置方向。
            """
            weighted_distance = self._estimate_column_level_move_time(
                p.column - target_pos.column,
                p.level - target_pos.level,
            )
            level_diff = abs(p.level - pref_level) if pref_level is not None else 0
            return (
                # 加权距离已按列/层物理参数换算，不再固定“同列一定优先”。
                weighted_distance,
                # 加权距离相同时按 warehouse.json 指定的最小列/中间列/最大列模式择优。
                *self.warehouse_core.get_position_column_preference_key(p.aisle, p.column),
                # 仍相同时选择更接近任务优选层的候选。
                level_diff,
            )
        empty_slots.sort(key=_score)
        return empty_slots[0]
