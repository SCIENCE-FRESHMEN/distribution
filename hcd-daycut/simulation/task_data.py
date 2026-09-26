"""调度任务及其巷道执行记录的数据对象。

``TaskData`` 是配置构建器、分配器、调度器、事件队列和 API 服务之间共用的
内部任务格式。它保存任务输入、选定巷道、冻结货位及预计执行记录；实际库存
由 ``InventoryManager`` 单独维护。
"""

from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any, TypedDict, cast
from simulation.position import InventoryPosition

# TASK TYPE 常量
# 未分配巷道的入库任务
TASK_TYPE_INBOUND_UNASSIGNED = 'INBOUND_UNASSIGNED'
# 入库任务
TASK_TYPE_INBOUND = 'INBOUND'
# 出库任务
TASK_TYPE_OUTBOUND = 'OUTBOUND'


class AisleScheduleRecord(TypedDict, total=False):
    """巷道调度明细记录的数据样式。

    start_time: 开始时间
    duration: 持续时间
    delivery_time: 货物到达出库口/入库完成的时间
    un_congested_time: 出库拥堵解除时间（若适用）
    crane_start_time: 磁力吊开始时间（若适用）
    crane_finish_time: 磁力吊及拥堵完全结束时间（若适用）
    """
    start_time: float
    duration: float
    delivery_time: float
    un_congested_time: float
    crane_start_time: float
    crane_finish_time: float


@dataclass
class TaskData:
    """一条入库或出库任务的运行态载体。

    ``skus`` 保留 SKU 与属性的输入顺序，``positions`` 必须与该顺序对应。
    ``assigned_aisle`` 和 ``positions`` 可在任务承诺后冻结；``task_record``
    保存时间估计结果，供事件完成、指标统计和日志复用。
    """
    """SKUSKU"""
    # 若为出库，task_id = f"{TASK_TYPE_OUTBOUND}_PL{production_line}_GP{current_group_idx+1}_{task_skus[0]}_{task_skus[1]}"
    task_id: str
    # TASK_TYPE_INBOUND_UNASSIGNED / TASK_TYPE_INBOUND / TASK_TYPE_OUTBOUND
    task_type: str
    # 任务名称
    task_name: str = ""

    skus: List[Dict[str, Any]] = field(default_factory=list)
    # 示例：[{'skuId': 'A1', 'quantity': 1}, {'skuId': 'A2', 'quantity': 1}]。

    # outbound专用
    # 产线编号 (1-3)
    production_line: int = 0
    # inbound 专用：入库线路（默认为1，如果有实际线路信息可覆盖）
    in_line: int = 1

    # 巷道编号
    assigned_aisle: Optional[int] = None
    # 任务开始时间
    assigned_time: int = 0

    # position, 算法决定位置
    positions: List[InventoryPosition] = field(default_factory=list)

    # task_record, 预期时间安排, AisleScheduleRecord
    task_record: AisleScheduleRecord = field(
        default_factory=lambda: cast(AisleScheduleRecord, {})
    )

    # 生产计划与 API 调度阶段补充的元数据。原先通过动态赋值添加，导致静态检查
    # 无法确认这些字段存在；显式声明后事件、服务层和调度器共享同一任务契约。
    plan_id: Optional[str] = None
    plan_index_public: Optional[int] = None
    group_idx: Optional[int] = None
    uses_virtual_group: bool = False
    _api_positions: List[Dict[str, Any]] = field(default_factory=list)
    required_sku: Optional[Dict[str, Any]] = None
    choice_count: int = 0

    def get_sku_ids(self) -> List[str]:
        """按任务 SKU 条目顺序提取 SKU 标识。

        Returns:
            任务 ``skus`` 中每个条目的 ``skuId``；兼容旧字段名 ``sku``。
            不会按 ``quantity`` 展开重复 SKU。
        """
        # 优先使用 skus 字段（入库和出库都可以使用）
        if self.skus:
            return [sku.get('skuId', sku.get('sku', '')) for sku in self.skus if 'skuId' != None]
        return []

    def get_sku_quantities(self) -> Dict[str, int]:
        """汇总任务中每种 SKU 的申报数量。

        Returns:
            键为 SKU 标识、值为任务条目中的 ``quantity``。出库旧格式未提供
            ``skus`` 时，退回读取 ``required_sku``。
        """
        result = {}
        # 优先使用 skus 字段
        if self.skus:
            for sku in self.skus:
                sku_id = sku.get('skuId', sku.get('sku', ''))
                quantity = sku.get('quantity', 1)
                result[sku_id] = quantity
        # 兼容旧的 required_sku 字段
        elif self.task_type.upper() == 'OUTBOUND' and self.required_sku:
            sku_id = self.required_sku.get('skuId', '')
            quantity = self.required_sku.get('quantity', 1)
            result[sku_id] = quantity
        return result

    def is_single_beam(self) -> bool:
        """判断任务是否只包含一个 SKU 条目。

        单梁任务只需占用一个层位；该判断按 ``skus`` 的条目数而非 ``quantity``
        计算，因为调度和货位分配以一条 SKU 条目对应一根待处理梁。

        Returns:
            ``True`` 表示任务恰有一个 SKU 条目，否则为 ``False``。
        """
        return len(self.get_sku_ids()) == 1

    def is_double_beam(self) -> bool:
        """判断任务是否包含两个待共同处理的 SKU 条目。

        返回 ``True`` 后，调用方仍需依据 BOM 配对关系和 ``match_fields`` 属性
        判断两根梁能否同位直接入库或直接出库；两个条目本身不等于已配对。

        Returns:
            ``True`` 表示任务恰有两个 SKU 条目，否则为 ``False``。
        """
        return len(self.get_sku_ids()) == 2
