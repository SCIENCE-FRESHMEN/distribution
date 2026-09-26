"""货位领域对象。

``InventoryPosition`` 是库存、入库分配、出库配对和 API 位置校验共用的
最小空间单元。它只描述一个坐标及其上下层库存，不负责修改全局索引；实际
写入和扣减必须经由 :mod:`simulation.inventory` 的 ``InventoryManager``，以
保证库存汇总和 SKU 索引同步。
"""

from dataclasses import dataclass, field
from typing import Optional, Dict, Any, Iterable


@dataclass
class InventoryPosition:
    """表示一个单层货位或一个由 ``UPPER``/``LOWER`` 组成的双层货位。

    调用方包括 ``InventoryManager``、入库巷道/货位分配器、``WarehouseCore``
    事件处理和 API 服务。``reserved`` 是移库规划期间的临时排他占用，
    ``disabled`` 是配置层面的永久不可用标记；两者都会令货位不能接受新的
    入库或移库，但不会删除已经记录的真实库存。
    """
    #  (1-5)
    aisle: int
    #  (1-2)
    row: int
    #  (1-4)
    column: int
    #  (1-18)
    level: int
    # True: 双层立体库 False: 单层立体库
    is_double_layer: bool = False

    # 只在单层立体库下有用
    # 单层货位 SKU；空位使用 None，便于 API 全量同步清空。
    sku: Optional[str] = None
    # 0
    quantity: int = 0
    sku_attrs: Dict[str, Any] = field(default_factory=dict)

    # 只在双层立体库下有用
    # SKU
    upper_sku: Optional[str] = None
    #
    upper_quantity: int = 0
    # SKU
    lower_sku: Optional[str] = None
    #
    lower_quantity: int = 0
    upper_attrs: Dict[str, Any] = field(default_factory=dict)
    lower_attrs: Dict[str, Any] = field(default_factory=dict)
    # reserved by relocation
    reserved: bool = False
    # unavailable for inbound/relocation
    disabled: bool = False

    def get_position_id(self) -> str:
        """返回跨库存索引、任务 positions 和 API 坐标校验共享的坐标主键。

        Returns:
            ``巷道-排-列-层`` 格式的坐标；列和层固定补齐为两位数。
        """
        return f"{self.aisle:01d}-{self.row:01d}-{self.column:02d}-{self.level:02d}"

    def is_empty(self) -> bool:
        """返回货位是否可视为完全空闲。

        该判断同时考虑移库预留和禁用状态，因此分配器不能将 ``reserved`` 或
        ``disabled`` 的位置作为空货位。调用本方法不会修改库存或预留状态。

        Returns:
            ``True`` 表示没有库存且未被预留或禁用。
        """
        if self.reserved or self.disabled:
            return False
        if self.is_double_layer:
            return self.upper_quantity == 0 and self.lower_quantity == 0
        else:
            return self.quantity == 0

    def has_space(self) -> bool:
        """返回是否仍可承接一根梁。

        双层库只在下层为空时返回 ``True``，从而禁止“下层已有货、再放上层”
        的写入顺序；这是上下层取放约束，而非库存数量统计规则。

        Returns:
            ``True`` 表示按层位写入顺序仍存在可用空间。
        """
        if self.reserved or self.disabled:
            return False
        if not self.is_double_layer:
            return self.is_empty()
        # 双层入库不能在下层已占用后再写入上层；仅下层为空时仍存在合法剩余容量。
        return self.lower_quantity == 0

    def can_place_sku(self, shelf: Optional[str] = None) -> bool:
        """校验指定层是否可写入，不修改本对象。

        Args:
            shelf: ``upper``、``lower`` 或 ``None``。单层库忽略该参数；双层库
                的 ``upper`` 必须上下均空，``lower`` 仅要求下层为空。

        Returns:
            ``True`` 表示该层满足上下层、预留和禁用约束，可交由库存管理器写入。
        """
        if self.reserved or self.disabled:
            return False
        if not self.is_double_layer:
            return self.is_empty()

        if shelf == 'upper':
            return self.upper_quantity == 0 and self.lower_quantity == 0
        elif shelf == 'lower':
            return self.lower_quantity == 0
        else:
            # 双层货位仅在下层仍可写入时才视为存在可用容量。
            return self.lower_quantity == 0

    def get_available_skus(self) -> list:
        """按上、下层顺序返回当前位置实际有库存的 SKU。

        Returns:
            双层库依次返回上层、下层 SKU；单层库最多返回一个 SKU。
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

    def _attrs_match(self, stored: Dict[str, Any], required: Optional[Dict[str, Any]], match_fields: Optional[Iterable[str]]) -> bool:
        """Match extra attributes only when the task provides a non-None value.

        """
        if not match_fields:
            return True
        if not required:
            return True
        for field in match_fields:
            req_val = required.get(field)
            if req_val is None:
                continue
            if stored.get(field) != req_val:
                return False
        return True

    def matches_sku(self, sku_id: str, attrs: Optional[Dict[str, Any]] = None,
                    match_fields: Optional[Iterable[str]] = None, shelf: Optional[str] = None) -> bool:
        """判断货位是否包含指定 SKU 且附加属性匹配。

        出库匹配和 API 虚拟预占均依赖该只读判断。``shelf`` 指定时只检查该层；
        未指定时任一层命中即可。属性仅比较 ``match_fields`` 中任务实际提供的
        非 ``None`` 字段，避免缺省字段误把同 SKU 库存排除。

        """
        if not sku_id:
            return False
        if self.is_double_layer:
            if shelf == 'upper':
                return (
                    self.upper_sku == sku_id
                    and self.upper_quantity > 0
                    and self._attrs_match(self.upper_attrs, attrs, match_fields)
                )
            if shelf == 'lower':
                return (
                    self.lower_sku == sku_id
                    and self.lower_quantity > 0
                    and self._attrs_match(self.lower_attrs, attrs, match_fields)
                )
            return (
                (self.upper_sku == sku_id and self.upper_quantity > 0 and self._attrs_match(self.upper_attrs, attrs, match_fields))
                or (self.lower_sku == sku_id and self.lower_quantity > 0 and self._attrs_match(self.lower_attrs, attrs, match_fields))
            )
        return self.sku == sku_id and self.quantity > 0 and self._attrs_match(self.sku_attrs, attrs, match_fields)

    def matches_pair(self, sku1: str, attrs1: Optional[Dict[str, Any]],
                     sku2: str, attrs2: Optional[Dict[str, Any]],
                     match_fields: Optional[Iterable[str]] = None) -> bool:
        """判断两根梁是否在本双层货位的上下层形成可直接出库的配对。

        同一业务配对允许上下层顺序交换，但每个 SKU 必须满足自己的
        属性匹配字段；单层位置、任一层为空或属性不一致都会返回 False。

        """
        if not self.is_double_layer:
            return False
        return (
            self.matches_sku(sku1, attrs1, match_fields, shelf='upper') and
            self.matches_sku(sku2, attrs2, match_fields, shelf='lower')
        ) or (
            self.matches_sku(sku2, attrs2, match_fields, shelf='upper') and
            self.matches_sku(sku1, attrs1, match_fields, shelf='lower')
        )

    def get_total_quantity(self) -> int:
        """返回该位置上下层或单层的总库存数量。

        Returns:
            双层库的上下层数量之和，或单层库的 ``quantity``。
        """
        if self.is_double_layer:
            return self.upper_quantity + self.lower_quantity
        else:
            return self.quantity

    def get_status_info(self) -> str:
        """生成用于日志和排查的货位库存摘要，不包含预留或禁用标记。

        Returns:
            单层 ``SKU:数量`` 或双层 ``upper/lower`` 状态文本。
        """
        if self.is_double_layer:
            upper = f"upper=({self.upper_sku}:{self.upper_quantity})" if self.upper_sku else "upper=empty"
            lower = f"lower=({self.lower_sku}:{self.lower_quantity})" if self.lower_sku else "lower=empty"
            return f"{upper}, {lower}"
        else:
            return f"({self.sku}:{self.quantity})" if self.sku else "empty"
