"""定义车身库入库、出库任务的统一数据对象及其运行状态字段。"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, TypeAlias
from simulation.position import InventoryPosition

# TASK TYPE 常量
TASK_TYPE_INBOUND_UNASSIGNED = 'INBOUND_UNASSIGNED' # 未分配巷道的入库任务
TASK_TYPE_INBOUND = 'INBOUND' # 入库任务
TASK_TYPE_OUTBOUND = 'OUTBOUND' # 出库任务

# 不同设备/拥堵配置会生成不同字段组合，因此记录保持可扩展字典，而非强制所有键必填。
AisleScheduleRecord: TypeAlias = Dict[str, Any]


# ==========================================================================
# 数据定义：任务状态、SKU 请求和执行记录
# ==========================================================================
@dataclass
class TaskData:
    """贯穿 API、调度器和事件状态机的一条入库或出库任务。

    ``skus`` 中每项至少包含 ``skuId`` 和 ``quantity``，车身库可额外携带
    ``features``、``inLine``、``outLine`` 等属性。``positions`` 在入库/出库位置
    确定后冻结，``task_record`` 保存本次任务的时间估算与执行阶段标记。
    """
    # 出库任务通常编码为 OUTBOUND_PL{产线}_GP{组号}_{SKU...}；API 也允许外部 taskId。
    task_id: str
    task_type: str  # TASK_TYPE_INBOUND_UNASSIGNED / TASK_TYPE_INBOUND / TASK_TYPE_OUTBOUND
    task_name: str = ""  # 任务名称

    skus: List[Dict[str, Any]] = field(default_factory=list)
    # : [{'skuId': 'A1', 'quantity': 1}, {'skuId': 'A2', 'quantity': 1}]

    # 出库产线与出库口；未传时为 None，后续由计划或货位元数据补齐。
    production_line: Optional[int] = None
    out_line: Optional[int] = None
    # 入库线路；默认 1 是兼容旧仿真数据，API 可通过 inLine 覆盖。
    in_line: Optional[int] = 1

    # 已确定的执行巷道；位置冻结后须与 positions[0].aisle 保持一致。
    assigned_aisle: Optional[int] = None
    # 调度器写入的分配时刻和入库进入 pending 队列的时刻，单位均为仿真秒。
    assigned_time: float = 0.0
    pending_enter_time: Optional[float] = None
    # 生产计划归属：group_idx 是内部 0-based 组号，plan_id 保留外部计划批次标识。
    group_idx: Optional[int] = None
    plan_id: Optional[str] = None
    # 兼容旧格式的单 SKU 出库请求；优先级低于 skus。
    required_sku: Optional[Dict[str, Any]] = None

    # 算法选择并冻结的位置列表；顺序与 skus 一一对应，双层位置允许重复出现。
    positions: List[InventoryPosition] = field(default_factory=list)

    # 非 FIFO 出库时，当前已选巷道内满足任务要求的直接出库货位数。优化器以此
    # 计算选择弹性奖励；FIFO 任务不使用该字段，保持默认值即可。
    choice_count: int = 0

    # 时间估算、拥堵时刻和执行库存扣减标识组成的可扩展字典。
    task_record: Dict[str, Any] = field(default_factory=dict)

    # ==========================================================================
    # 辅助函数：SKU 数量与单双任务判断
    # ==========================================================================
    def get_sku_ids(self) -> List[str]:
        """按任务 SKU 顺序返回有效的 SKU 标识。

        Returns:
            List[str]: ``skus`` 中的 skuId/sku；未提供时兼容返回 required_sku 的 SKU。
        """
        if self.skus:
            return [
                str(sku_id)
                for entry in self.skus
                if isinstance(entry, dict)
                for sku_id in [entry.get("skuId") or entry.get("sku")]
                if sku_id
            ]
        if self.required_sku:
            sku_id = self.required_sku.get("skuId") or self.required_sku.get("sku")
            return [str(sku_id)] if sku_id else []
        return []

    def get_sku_quantities(self) -> Dict[str, int]:
        """汇总任务中每个 SKU 的需求数量。

        Returns:
            Dict[str, int]: SKU 到数量的映射；优先读取 skus，兼容 required_sku。
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
            sku_id = self.required_sku.get('skuId', self.required_sku.get('sku', ''))
            quantity = self.required_sku.get('quantity', 1)
            result[sku_id] = quantity
        return result

    def is_single_beam(self) -> bool:
        """判断任务是否只包含一个有效 SKU。

        Returns:
            bool: 有且仅有一个 SKU 时为 True。
        """
        return len(self.get_sku_ids()) == 1

    def is_double_beam(self) -> bool:
        """判断任务是否包含两个有效 SKU。

        Returns:
            bool: 有且仅有两个 SKU 时为 True。
        """
        return len(self.get_sku_ids()) == 2
