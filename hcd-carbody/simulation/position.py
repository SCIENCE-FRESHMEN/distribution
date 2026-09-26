"""定义车身库货位坐标、库存载荷、线路元数据及可用性判断。"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


# ==========================================================================
# 数据定义：物理货位、库存载荷与可用性判断
# ==========================================================================
@dataclass
class InventoryPosition:
    """一个物理货位及其单层或双层库存状态。

    车身库默认使用单层字段 ``sku``、``quantity``、``features``；保留上下层字段以
    兼容共用的纵梁仿真接口。``reserved`` 是移库过程临时锁，``disabled`` 是配置长期禁用，
    二者都会使位置不可作为新的入库或移库目标。
    """
    aisle: int      # 巷道号，从 1 开始。
    row: int        # 排号，区分巷道两侧或业务定义的行。
    column: int     # 列号，沿巷道纵深方向。
    level: int      # 层号，垂直方向。
    is_double_layer: bool = False      # False 为车身库单层货位；True 为兼容双层货位。

    # 只在单层立体库下有用
    sku: str = ""           # 单层库存 SKU；空字符串表示无库存。
    quantity: int = 0       # 单层库存数量；车身库通常为 0 或 1。
    # 与该 SKU 绑定的 color、rfid、skid_type 等属性；内部 FIFO 时间也以
    # ``_inbound_time`` 键随该 SKU 保存，不能放在物理货位对象上。
    features: Optional[Dict[str, Any]] = None
    in_line: Optional[Any] = None
    out_line: Optional[Any] = None

    # 只在双层立体库下有用
    upper_sku: Optional[str] = None    # 双层上层 SKU。
    upper_quantity: int = 0            # 双层上层数量。
    lower_sku: Optional[str] = None    # 双层下层 SKU。
    lower_quantity: int = 0            # 双层下层数量。
    upper_features: Optional[Dict[str, Any]] = None
    lower_features: Optional[Dict[str, Any]] = None
    upper_in_line: Optional[Any] = None
    upper_out_line: Optional[Any] = None
    lower_in_line: Optional[Any] = None
    lower_out_line: Optional[Any] = None
    reserved: bool = False             # 移库计划的短期位置锁，完成/失败后应释放。
    disabled: bool = False             # 配置定义的长期禁用位，整个运行期均不可使用。

    # ==========================================================================
    # 辅助函数：货位状态和坐标查询
    # ==========================================================================
    def get_position_id(self) -> str:
        """生成货位唯一标识。

        Returns:
            str: ``巷道-排-两位列-两位层``，例如 ``1-2-03-09``。
        """
        return f"{self.aisle:01d}-{self.row:01d}-{self.column:02d}-{self.level:02d}"

    def is_empty(self) -> bool:
        """判断货位是否完全空闲且可参与分配。

        Returns:
            bool: 禁用或预留位始终为 False；双层时要求上下层均无库存。
        """
        if self.reserved or self.disabled:
            return False
        if self.is_double_layer:
            return self.upper_quantity == 0 and self.lower_quantity == 0
        else:
            return self.quantity == 0

    def has_space(self) -> bool:
        """判断货位是否还可接收一件库存。

        Returns:
            bool: 单层要求为空；双层允许上层或下层至少一层为空。
        """
        if self.reserved or self.disabled:
            return False
        if not self.is_double_layer:
            return self.is_empty()
        return self.upper_quantity == 0 or self.lower_quantity == 0

    def can_place_sku(self, shelf: Optional[str] = None) -> bool:
        """判断指定层位是否可写入库存。

        Args:
            shelf: 双层货位的 ``upper`` 或 ``lower``；None 表示任一层均可。

        Returns:
            bool: 预留/禁用位为 False；单层退化为 ``is_empty`` 判断。
        """
        if self.reserved or self.disabled:
            return False
        if not self.is_double_layer:
            return self.is_empty()

        if shelf == 'upper':
            return self.upper_quantity == 0
        elif shelf == 'lower':
            return self.lower_quantity == 0
        else:
            #
            return self.upper_quantity == 0 or self.lower_quantity == 0

    def get_available_skus(self) -> List[str]:
        """返回该货位中数量大于零的 SKU。

        Returns:
            List[str]: 单层至多一个 SKU，双层按上层、下层顺序返回。
        """
        skus = []
        if self.is_double_layer:
            if self.upper_quantity > 0 and self.upper_sku:
                skus.append(self.upper_sku)
            if self.lower_quantity > 0 and self.lower_sku:
                skus.append(self.lower_sku)
        else:
            if self.quantity > 0 and self.sku:
                skus.append(self.sku)
        return skus

    def get_total_quantity(self) -> int:
        """汇总单层或双层库存数量。

        Returns:
            int: 单层 quantity，或双层 upper_quantity 与 lower_quantity 之和。
        """
        if self.is_double_layer:
            return self.upper_quantity + self.lower_quantity
        else:
            return self.quantity

    def get_status_info(self) -> str:
        """生成人类可读的库存状态摘要。

        Returns:
            str: 用于日志诊断的单层或上下层 SKU/数量描述。
        """
        if self.is_double_layer:
            upper = f"upper=({self.upper_sku}:{self.upper_quantity})" if self.upper_sku else "upper=empty"
            lower = f"lower=({self.lower_sku}:{self.lower_quantity})" if self.lower_sku else "lower=empty"
            return f"{upper}, {lower}"
        else:
            return f"({self.sku}:{self.quantity})" if self.sku else "empty"
