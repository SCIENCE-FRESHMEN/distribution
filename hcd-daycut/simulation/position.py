"""

"""

from dataclasses import dataclass, field
from typing import Optional, Dict, Any, Iterable


@dataclass
class InventoryPosition:
    """"""
    aisle: int      #  (1-5)
    row: int        #  (1-2)
    column: int     #  (1-4)  
    level: int      #  (1-18)
    is_double_layer: bool = False      # True: 双层立体库 False: 单层立体库

    # 只在单层立体库下有用
    sku: str = ""           # SKU
    quantity: int = 0       # 0
    sku_attrs: Dict[str, Any] = field(default_factory=dict)
    
    # 只在双层立体库下有用
    upper_sku: Optional[str] = None    # SKU
    upper_quantity: int = 0            # 
    lower_sku: Optional[str] = None    # SKU  
    lower_quantity: int = 0            # 
    upper_attrs: Dict[str, Any] = field(default_factory=dict)
    lower_attrs: Dict[str, Any] = field(default_factory=dict)
    reserved: bool = False             # reserved by relocation
    disabled: bool = False             # unavailable for inbound/relocation
    
    def get_position_id(self) -> str:
        """"""
        return f"{self.aisle:01d}-{self.row:01d}-{self.column:02d}-{self.level:02d}"
    
    def is_empty(self) -> bool:
        """"""
        if self.reserved or self.disabled:
            return False
        if self.is_double_layer:
            return self.upper_quantity == 0 and self.lower_quantity == 0
        else:
            return self.quantity == 0
    
    def has_space(self) -> bool:
        """"""
        if self.reserved or self.disabled:
            return False
        if not self.is_double_layer:
            return self.is_empty()
        return self.upper_quantity == 0 or self.lower_quantity == 0
    
    def can_place_sku(self, shelf: Optional[str] = None) -> bool:
        """SKU
        
        Args:
            sku: SKU
            shelf: 'upper'  'lower'None
            
        Returns:
            
        
        shelf UPPER/LOWER level1-18
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
    
    def get_available_skus(self) -> list:
        """返回已经有的SKU"""
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
        """Match extra attributes only when the task provides a non-None value."""
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
        """判断货位是否包含指定SKU且附加属性匹配。"""
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
        """"""
        if self.is_double_layer:
            return self.upper_quantity + self.lower_quantity
        else:
            return self.quantity

    def get_status_info(self) -> str:
        """获取货位当前状态信息"""
        if self.is_double_layer:
            upper = f"upper=({self.upper_sku}:{self.upper_quantity})" if self.upper_sku else "upper=empty"
            lower = f"lower=({self.lower_sku}:{self.lower_quantity})" if self.lower_sku else "lower=empty"
            return f"{upper}, {lower}"
        else:
            return f"({self.sku}:{self.quantity})" if self.sku else "empty"

