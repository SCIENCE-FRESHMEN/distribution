"""
实现仓库仿真中的性能指标计算
比如：库存均衡度、配对成功率、平均出库任务完成时间等

"""

import math
from typing import Dict, List


class MetricsCalculator:
    """"""
    
    def __init__(self, aisles: List[int], sku_types: List[str], 
                 left_aisles: List[int] = None, right_aisles: List[int] = None,
                 lr_balance_weight: float = 00):
        """
        Args:
            aisles: 巷道列表
            sku_types: SKU类型列表
            left_aisles: 左侧巷道列表（可选）
            right_aisles: 右侧巷道列表（可选）
            lr_balance_weight: 左右均衡度权重（0-1），默认0.3
        """
        self.aisles = aisles
        self.sku_types = sku_types
        self.lr_balance_weight = lr_balance_weight
        
        # 左右巷道定义
        if left_aisles is not None and right_aisles is not None:
            self.left_aisles = left_aisles
            self.right_aisles = right_aisles
        else:
            # 默认：对半分
            mid_point = len(self.aisles) // 2
            self.left_aisles = self.aisles[:mid_point]
            self.right_aisles = self.aisles[mid_point:]
    
    def calculate_distribution_balance(self, current_inventory: Dict[int, Dict[str, int]]) -> float:
        """
        Args:
            current_inventory:  {aisle: {sku: quantity}}
            
        Returns:
             (0-1)
        """
        balance_scores = []
        left_right_balance_scores = []
        sku_qty = []
        
        for sku in self.sku_types:
            quantities = [current_inventory[aisle].get(sku, 0) for aisle in self.aisles]
            total_qty = sum(quantities)
            sku_qty.append(total_qty)
            
            if total_qty == 0:
                continue
            
            # 1. 
            mean_qty = total_qty / len(self.aisles)
            if mean_qty > 0:
                variance = sum((x - mean_qty) ** 2 for x in quantities) / len(quantities)
                cv = math.sqrt(variance) / mean_qty  # 
                balance_score = 1 / (1 + cv)  # 0-1
                balance_scores.append(balance_score)
            
            # 2. 
            left_qty = sum(current_inventory[aisle].get(sku, 0) for aisle in self.left_aisles)
            right_qty = sum(current_inventory[aisle].get(sku, 0) for aisle in self.right_aisles)
            
            if left_qty + right_qty > 0:
                expected_left = total_qty * len(self.left_aisles) / len(self.aisles)
                expected_right = total_qty * len(self.right_aisles) / len(self.aisles)
                
                if expected_left > 0 and expected_right > 0:
                    left_deviation = abs(left_qty - expected_left) / expected_left
                    right_deviation = abs(right_qty - expected_right) / expected_right
                    avg_deviation = (left_deviation + right_deviation) / 2
                    lr_balance_score = 1 / (1 + avg_deviation)
                    left_right_balance_scores.append(lr_balance_score)
        
        # 使用可配置的权重计算综合均衡度
        overall_balance = sum(balance_scores) / len(balance_scores) if balance_scores else 0
        lr_balance = sum(left_right_balance_scores) / len(left_right_balance_scores) if left_right_balance_scores else 0
        
        mean_sku_qty = sum(sku_qty) / len(sku_qty) if sku_qty else 0
        if mean_sku_qty > 0:
            sku_variance = sum((qty - mean_sku_qty) ** 2 for qty in sku_qty) / len(sku_qty)
            sku_cv = math.sqrt(sku_variance) / mean_sku_qty
            sku_balance = 1 / (1 + sku_cv)
        else:
            sku_balance = 0

        # overall_balance权重 = 1 - lr_balance_weight
        combined_balance = (1 - self.lr_balance_weight) * overall_balance + self.lr_balance_weight * lr_balance + sku_balance
        return combined_balance
    
    
    def print_detailed_balance(self, current_inventory: Dict[int, Dict[str, int]]):
        """
        
        Args:
            current_inventory: 
        """
        print(f"  : {self.left_aisles}, : {self.right_aisles}")
        
        balance_scores = []
        left_right_balance_scores = []
        
        for sku in self.sku_types:
            quantities = [current_inventory[aisle][sku] for aisle in self.aisles]
            total_qty = sum(quantities)
            
            if total_qty > 0:
                # 
                mean_qty = total_qty / len(self.aisles)
                variance = sum((x - mean_qty) ** 2 for x in quantities) / len(quantities)
                cv = math.sqrt(variance) / mean_qty
                balance_score = 1 / (1 + cv)
                balance_scores.append(balance_score)
                
                # 
                left_qty = sum(current_inventory[aisle][sku] for aisle in self.left_aisles)
                right_qty = sum(current_inventory[aisle][sku] for aisle in self.right_aisles)
                
                expected_left = total_qty * len(self.left_aisles) / len(self.aisles)
                expected_right = total_qty * len(self.right_aisles) / len(self.aisles)
                
                if expected_left > 0 and expected_right > 0:
                    left_deviation = abs(left_qty - expected_left) / expected_left
                    right_deviation = abs(right_qty - expected_right) / expected_right
                    avg_deviation = (left_deviation + right_deviation) / 2
                    lr_balance_score = 1 / (1 + avg_deviation)
                    left_right_balance_scores.append(lr_balance_score)
                    
                    print(f"    {sku}: {left_qty}|{right_qty} "
                          f"(: {expected_left:.1f}|{expected_right:.1f}) "
                          f":{lr_balance_score:.3f}")
        
        overall_balance = sum(balance_scores) / len(balance_scores) if balance_scores else 0
        lr_balance = sum(left_right_balance_scores) / len(left_right_balance_scores) if left_right_balance_scores else 0
        combined_balance = (1 - self.lr_balance_weight) * overall_balance + self.lr_balance_weight * lr_balance
        
        print(f"  整体均衡度: {overall_balance:.3f} (权重: {1-self.lr_balance_weight:.1f})")
        print(f"  左右均衡度: {lr_balance:.3f} (权重: {self.lr_balance_weight:.1f})")
        print(f"  综合均衡度: {combined_balance:.3f}")

