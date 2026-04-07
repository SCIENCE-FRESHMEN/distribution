"""
API调用示例代码
演示如何调用仓库调度系统的各个API接口
"""

import requests
import json
from datetime import datetime
from typing import Dict, List, Optional

# API基础配置
BASE_URL = "http://localhost:8000/api/v1"
HEADERS = {"Content-Type": "application/json"}


def format_timestamp() -> str:
    """生成ISO 8601格式的时间戳"""
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


# ============================================================
# 1. 生产计划接口示例
# ============================================================

def example_set_production_plan():
    """示例：设置生产计划"""
    url = f"{BASE_URL}/plan/production"
    
    payload = {
        "operationType": "ADD",
        "planDate": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "plans": [
            {
                "planId": "PLAN-001",
                "lineId": "LINE-1",
                "requiredSkus": [
                    {"skuId": "SKU-A1", "quantity": 10},
                    {"skuId": "SKU-A2", "quantity": 10}
                ]
            },
            {
                "planId": "PLAN-002",
                "lineId": "LINE-2",
                "requiredSkus": [
                    {"skuId": "SKU-B1", "quantity": 8},
                    {"skuId": "SKU-B2", "quantity": 8}
                ]
            },
            {
                "planId": "PLAN-003",
                "lineId": "LINE-3",
                "requiredSkus": [
                    {"skuId": "SKU-C1", "quantity": 12},
                    {"skuId": "SKU-C2", "quantity": 12}
                ]
            }
        ]
    }
    
    print("=== 设置生产计划 ===")
    print(f"请求URL: {url}")
    print(f"请求体: {json.dumps(payload, indent=2, ensure_ascii=False)}")
    
    response = requests.post(url, json=payload, headers=HEADERS)
    print(f"响应状态码: {response.status_code}")
    print(f"响应内容: {json.dumps(response.json(), indent=2, ensure_ascii=False)}")
    return response.json()


# ============================================================
# 2. 入库分配接口示例
# ============================================================

def example_inbound_allocation():
    """示例：入库巷道分配"""
    url = f"{BASE_URL}/inbound/allocate"
    
    payload = {
        "tasks": [
            {
                "taskId": "INBOUND-001",
                "skus": [
                    {"skuId": "SKU-A1", "quantity": 1},
                    {"skuId": "SKU-A2", "quantity": 1}
                ]
            },
            {
                "taskId": "INBOUND-002",
                "skus": [
                    {"skuId": "SKU-B1", "quantity": 1}
                ]
            }
        ]
    }
    
    print("\n=== 入库巷道分配 ===")
    print(f"请求URL: {url}")
    print(f"请求体: {json.dumps(payload, indent=2, ensure_ascii=False)}")
    
    response = requests.post(url, json=payload, headers=HEADERS)
    print(f"响应状态码: {response.status_code}")
    print(f"响应内容: {json.dumps(response.json(), indent=2, ensure_ascii=False)}")
    return response.json()


# ============================================================
# 3. 混合调度接口示例
# ============================================================

def example_mixed_scheduling():
    """示例：混合调度（入库+出库）"""
    url = f"{BASE_URL}/schedule/mixed"
    
    payload = {
        "tasks": [
            # 入库任务
            {
                "taskId": "INBOUND-003",
                "taskType": "INBOUND",
                "targetAisle": "1",
                "skus": [
                    {"skuId": "SKU-A1", "quantity": 1},
                    {"skuId": "SKU-A2", "quantity": 1}
                ],
                "inboundUrgent": False
            },
            # 出库任务
            {
                "taskId": "OUTBOUND-001",
                "taskType": "OUTBOUND",
                "planId": "PLAN-001",
                "planIndex": 1,
                "skus": [
                    {"skuId": "SKU-A1", "quantity": 1},
                    {"skuId": "SKU-A2", "quantity": 1}
                ]
            },
            {
                "taskId": "OUTBOUND-002",
                "taskType": "OUTBOUND",
                "planId": "PLAN-002",
                "planIndex": 1,
                "skus": [
                    {"skuId": "SKU-B1", "quantity": 1},
                    {"skuId": "SKU-B2", "quantity": 1}
                ]
            }
        ],
        "aisleStatus": [
            {
                "aisleId": "1",
                "isAvailable": True,
                "unavailableReason": None,
                "exitCongestion": [
                    {"lineId": "LINE-1", "isCongested": False},
                    {"lineId": "LINE-2", "isCongested": False},
                    {"lineId": "LINE-3", "isCongested": False}
                ],
                "bank": "LEFT"
            },
            {
                "aisleId": "2",
                "isAvailable": True,
                "unavailableReason": None,
                "exitCongestion": [
                    {"lineId": "LINE-1", "isCongested": False},
                    {"lineId": "LINE-2", "isCongested": False},
                    {"lineId": "LINE-3", "isCongested": False}
                ],
                "bank": "LEFT"
            },
            {
                "aisleId": "3",
                "isAvailable": False,
                "unavailableReason": "MAINTENANCE",
                "exitCongestion": [
                    {"lineId": "LINE-1", "isCongested": False},
                    {"lineId": "LINE-2", "isCongested": False},
                    {"lineId": "LINE-3", "isCongested": False}
                ],
                "bank": "RIGHT"
            },
            {
                "aisleId": "4",
                "isAvailable": True,
                "unavailableReason": None,
                "exitCongestion": [
                    {"lineId": "LINE-1", "isCongested": True},  # 产线1拥堵
                    {"lineId": "LINE-2", "isCongested": False},
                    {"lineId": "LINE-3", "isCongested": False}
                ],
                "bank": "RIGHT"
            },
            {
                "aisleId": "5",
                "isAvailable": True,
                "unavailableReason": None,
                "exitCongestion": [
                    {"lineId": "LINE-1", "isCongested": False},
                    {"lineId": "LINE-2", "isCongested": False},
                    {"lineId": "LINE-3", "isCongested": False}
                ],
                "bank": "RIGHT"
            }
        ],
        "inventory": [
            # 示例库存数据
            {
                "aisleId": "1",
                "row": 1,
                "column": 1,
                "level": 1,
                "shelf": "UPPER",
                "positions": [{"skuId": "SKU-A1", "quantity": 1}]
            },
            {
                "aisleId": "1",
                "row": 1,
                "column": 1,
                "level": 1,
                "shelf": "LOWER",
                "positions": [{"skuId": "SKU-A2", "quantity": 1}]
            },
            {
                "aisleId": "2",
                "row": 1,
                "column": 2,
                "level": 2,
                "shelf": "UPPER",
                "positions": [{"skuId": "SKU-B1", "quantity": 1}]
            },
            {
                "aisleId": "2",
                "row": 1,
                "column": 2,
                "level": 2,
                "shelf": "LOWER",
                "positions": [{"skuId": "SKU-B2", "quantity": 1}]
            }
        ]
    }
    
    print("\n=== 混合调度 ===")
    print(f"请求URL: {url}")
    print(f"请求体: {json.dumps(payload, indent=2, ensure_ascii=False)}")
    
    response = requests.post(url, json=payload, headers=HEADERS)
    print(f"响应状态码: {response.status_code}")
    print(f"响应内容: {json.dumps(response.json(), indent=2, ensure_ascii=False)}")
    return response.json()


# ============================================================
# 4. 任务执行反馈接口示例
# ============================================================

def example_task_feedback_executing(task_id: str, task_type: str = "OUTBOUND"):
    """示例：报告任务开始执行"""
    url = f"{BASE_URL}/task/feedback"
    
    payload = {
        "taskId": task_id,
        "taskType": task_type,
        "status": "EXECUTING",
        "startTime": format_timestamp(),
        "failureReason": None
    }
    
    print(f"\n=== 任务反馈 - 执行中 ({task_id}) ===")
    print(f"请求URL: {url}")
    print(f"请求体: {json.dumps(payload, indent=2, ensure_ascii=False)}")
    
    response = requests.post(url, json=payload, headers=HEADERS)
    print(f"响应状态码: {response.status_code}")
    print(f"响应内容: {json.dumps(response.json(), indent=2, ensure_ascii=False)}")
    return response.json()


def example_task_feedback_completed(task_id: str, task_type: str = "OUTBOUND"):
    """示例：报告任务完成"""
    url = f"{BASE_URL}/task/feedback"
    
    payload = {
        "taskId": task_id,
        "taskType": task_type,
        "status": "COMPLETED",
        "startTime": format_timestamp(),
        "failureReason": None
    }
    
    print(f"\n=== 任务反馈 - 已完成 ({task_id}) ===")
    print(f"请求URL: {url}")
    print(f"请求体: {json.dumps(payload, indent=2, ensure_ascii=False)}")
    
    response = requests.post(url, json=payload, headers=HEADERS)
    print(f"响应状态码: {response.status_code}")
    print(f"响应内容: {json.dumps(response.json(), indent=2, ensure_ascii=False)}")
    return response.json()


def example_task_feedback_failed(task_id: str, task_type: str = "OUTBOUND", reason: str = "设备故障"):
    """示例：报告任务失败"""
    url = f"{BASE_URL}/task/feedback"
    
    payload = {
        "taskId": task_id,
        "taskType": task_type,
        "status": "FAILED",
        "startTime": format_timestamp(),
        "failureReason": reason
    }
    
    print(f"\n=== 任务反馈 - 失败 ({task_id}) ===")
    print(f"请求URL: {url}")
    print(f"请求体: {json.dumps(payload, indent=2, ensure_ascii=False)}")
    
    response = requests.post(url, json=payload, headers=HEADERS)
    print(f"响应状态码: {response.status_code}")
    print(f"响应内容: {json.dumps(response.json(), indent=2, ensure_ascii=False)}")
    return response.json()


# ============================================================
# 5. 完整流程示例
# ============================================================

def example_full_workflow():
    """
    示例：完整的工作流程
    
    流程说明：
    1. 设置生产计划
    2. 请求入库巷道分配
    3. 发起混合调度
    4. 接收调度结果后，报告任务开始执行（EXECUTING）
    5. 任务执行完成后，报告任务完成（COMPLETED）
    """
    print("=" * 60)
    print("完整工作流程示例")
    print("=" * 60)
    
    # 步骤1：设置生产计划
    print("\n>>> 步骤1：设置生产计划")
    plan_result = example_set_production_plan()
    
    # 步骤2：请求入库巷道分配
    print("\n>>> 步骤2：请求入库巷道分配")
    allocation_result = example_inbound_allocation()
    
    # 步骤3：发起混合调度
    print("\n>>> 步骤3：发起混合调度")
    schedule_result = example_mixed_scheduling()
    
    # 步骤4：对每个分配的任务报告 EXECUTING 状态
    print("\n>>> 步骤4：报告任务执行中")
    schedule_data = schedule_result.get("data") or {}
    if schedule_data.get("aisleAssignments"):
        for assignment in schedule_data["aisleAssignments"]:
            task = assignment.get("assignedTask")
            if task:
                example_task_feedback_executing(
                    task_id=task["taskId"],
                    task_type=task["taskType"]
                )
    
    # 步骤5：任务完成后报告 COMPLETED 状态
    print("\n>>> 步骤5：报告任务完成")
    if schedule_data.get("aisleAssignments"):
        for assignment in schedule_data["aisleAssignments"]:
            task = assignment.get("assignedTask")
            if task:
                example_task_feedback_completed(
                    task_id=task["taskId"],
                    task_type=task["taskType"]
                )
    
    print("\n" + "=" * 60)
    print("工作流程完成")
    print("=" * 60)


# ============================================================
# 6. 同步调用封装类
# ============================================================

class WarehouseAPIClient:
    """仓库API客户端封装类"""
    
    def __init__(self, base_url: str = "http://localhost:8000/api/v1"):
        self.base_url = base_url
        self.headers = {"Content-Type": "application/json"}
        self.pending_tasks: Dict[str, dict] = {}  # 待确认的任务
    
    def set_production_plan(self, plans: List[dict], operation_type: str = "ADD") -> dict:
        """设置生产计划"""
        url = f"{self.base_url}/plan/production"
        payload = {
            "operationType": operation_type,
            "planDate": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "plans": plans
        }
        response = requests.post(url, json=payload, headers=self.headers)
        return response.json()
    
    def allocate_inbound(self, tasks: List[dict]) -> dict:
        """请求入库巷道分配"""
        url = f"{self.base_url}/inbound/allocate"
        payload = {"tasks": tasks}
        response = requests.post(url, json=payload, headers=self.headers)
        return response.json()
    
    def request_schedule(self, tasks: List[dict], aisle_status: List[dict], 
                        inventory: List[dict]) -> dict:
        """请求混合调度"""
        url = f"{self.base_url}/schedule/mixed"
        payload = {
            "tasks": tasks,
            "aisleStatus": aisle_status,
            "inventory": inventory
        }
        response = requests.post(url, json=payload, headers=self.headers)
        result = response.json()
        
        result_data = result.get("data") or {}
        if result_data.get("aisleAssignments"):
            for assignment in result_data["aisleAssignments"]:
                task = assignment.get("assignedTask")
                if task:
                    self.pending_tasks[task["taskId"]] = {
                        "task": task,
                        "status": "PENDING",
                        "assigned_at": datetime.utcnow().isoformat()
                    }
        
        return result
    
    def report_task_status(self, task_id: str, task_type: str, status: str,
                          failure_reason: Optional[str] = None) -> dict:
        """报告任务状态"""
        url = f"{self.base_url}/task/feedback"
        payload = {
            "taskId": task_id,
            "taskType": task_type,
            "status": status,
            "startTime": format_timestamp(),
            "failureReason": failure_reason
        }
        response = requests.post(url, json=payload, headers=self.headers)
        result = response.json()
        
        # 更新本地任务状态
        if task_id in self.pending_tasks:
            if status == "EXECUTING":
                self.pending_tasks[task_id]["status"] = "CONFIRMED"
            elif status in ["COMPLETED", "FAILED"]:
                del self.pending_tasks[task_id]
        
        return result
    
    def get_pending_tasks(self) -> Dict[str, dict]:
        """获取待确认的任务列表"""
        return self.pending_tasks.copy()
    
    def has_unconfirmed_tasks(self) -> bool:
        """检查是否有未确认的任务"""
        return any(t["status"] == "PENDING" for t in self.pending_tasks.values())


# ============================================================
# 主函数
# ============================================================

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="仓库调度API示例")
    parser.add_argument("--example", choices=["plan", "inbound", "schedule", "feedback", "full"],
                       default="full", help="运行哪个示例")
    parser.add_argument("--url", default="http://localhost:8000/api/v1", 
                       help="API基础URL")
    
    args = parser.parse_args()
    BASE_URL = args.url
    
    if args.example == "plan":
        example_set_production_plan()
    elif args.example == "inbound":
        example_inbound_allocation()
    elif args.example == "schedule":
        example_mixed_scheduling()
    elif args.example == "feedback":
        example_task_feedback_executing("TEST-001", "OUTBOUND")
        example_task_feedback_completed("TEST-001", "OUTBOUND")
    else:
        example_full_workflow()

