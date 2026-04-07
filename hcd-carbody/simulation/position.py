"""

"""

from dataclasses import dataclass
from typing import Optional, Any


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
    features: Optional[dict] = None  # feature attributes for sku
    in_line: Optional[Any] = None
    out_line: Optional[Any] = None
    
    # 只在双层立体库下有用
    upper_sku: Optional[str] = None    # SKU
    upper_quantity: int = 0            # 
    lower_sku: Optional[str] = None    # SKU  
    lower_quantity: int = 0            # 
    upper_features: Optional[dict] = None
    lower_features: Optional[dict] = None
    upper_in_line: Optional[Any] = None
    upper_out_line: Optional[Any] = None
    lower_in_line: Optional[Any] = None
    lower_out_line: Optional[Any] = None
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

