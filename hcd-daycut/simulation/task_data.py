"""

"""

from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any, TypedDict
from simulation.position import InventoryPosition

# TASK TYPE 常量
TASK_TYPE_INBOUND_UNASSIGNED = 'INBOUND_UNASSIGNED' # 未分配巷道的入库任务
TASK_TYPE_INBOUND = 'INBOUND' # 入库任务
TASK_TYPE_OUTBOUND = 'OUTBOUND' # 出库任务


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
    """SKUSKU"""
    # 若为出库，task_id = f"{TASK_TYPE_OUTBOUND}_PL{production_line}_GP{current_group_idx+1}_{task_skus[0]}_{task_skus[1]}"
    task_id: str  
    task_type: str  # TASK_TYPE_INBOUND_UNASSIGNED / TASK_TYPE_INBOUND / TASK_TYPE_OUTBOUND
    task_name: str = ""  # 任务名称
    
    skus: List[Dict[str, Any]] = field(default_factory=list)  
    # : [{'skuId': 'A1', 'quantity': 1}, {'skuId': 'A2', 'quantity': 1}]

    # outbound专用
    production_line: int = 0  # 产线编号 (1-3)
    # inbound 专用：入库线路（默认为1，如果有实际线路信息可覆盖）
    in_line: int = 1  # (1-3)
    
    assigned_aisle: Optional[int] = None  # 
    # inbound_urgent: bool = False  # 
    assigned_time: int = 0  # 

    # position, 算法决定位置
    positions: List[InventoryPosition] = field(default_factory=list)

    # task_record, 预期时间安排, AisleScheduleRecord
    task_record: AisleScheduleRecord = field(default_factory=dict)

    def get_sku_ids(self) -> List[str]:
        """获取任务的SKU ID列表"""
        # 优先使用 skus 字段（入库和出库都可以使用）
        if self.skus:
            return [sku.get('skuId', sku.get('sku', '')) for sku in self.skus if 'skuId' != None]
        return []
    
    def get_sku_quantities(self) -> Dict[str, int]:
        """获取任务的SKU数量"""
        result = {}
        # 优先使用 skus 字段
        if self.skus:
            for sku in self.skus:
                sku_id = sku.get('skuId', sku.get('sku', ''))
                quantity = sku.get('quantity', 1)
                result[sku_id] = quantity
        # 兼容旧的 required_sku 字段
        elif self.task_type.upper() == 'OUTBOUND' and getattr(self, 'required_sku', None):
            sku_id = getattr(self, 'required_sku').get('skuId', '')
            quantity = self.required_sku.get('quantity', 1)
            result[sku_id] = quantity
        return result
    
    def is_single_beam(self) -> bool:
        """"""
        return len(self.get_sku_ids()) == 1
    
    def is_double_beam(self) -> bool:
        """"""
        return len(self.get_sku_ids()) == 2

