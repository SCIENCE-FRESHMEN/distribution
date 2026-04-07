#!/usr/bin/env python3
"""
仓库管理系统 API 自动化测试脚本

按照业务流程顺序依次执行7个测试场景：
0. 设置生产计划
1. 初始化库存
2. 入库分配巷道
3. 混合调度 - 分配出入库任务
4. 任务执行反馈 - EXECUTING
5. 任务完成反馈 - COMPLETED
6. 库存同步 - 增量更新

运行方式：
    python test_api_flow.py [--base-url http://localhost:8000]
"""

import requests
import json
import time
import argparse
from typing import Dict, Any, Optional
from pathlib import Path

import sys; sys.stdout = open("log.md", "a", encoding="utf-8")


# 默认配置
DEFAULT_BASE_URL = "http://localhost:8000"
API_PREFIX = "/api/v1"

CONFIG_PATH = Path(__file__).resolve().parent / "config" / "warehouse.json"


def load_match_fields() -> list:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return list(cfg.get("match_fields", []) or [])
    except Exception:
        return []


def default_attr_value(field: str) -> Any:
    if field == "version":
        return "00"
    return f"{field}-默认"


def apply_sku_attrs(obj: Any, match_fields: list) -> None:
    if not match_fields:
        return
    if isinstance(obj, list):
        for item in obj:
            apply_sku_attrs(item, match_fields)
        return
    if not isinstance(obj, dict):
        return
    if "skuId" in obj:
        sku_id = obj.get("skuId")
        quantity = obj.get("quantity", 1)
        if sku_id and quantity > 0:
            for field in match_fields:
                if field not in obj:
                    obj[field] = default_attr_value(field)
    for value in obj.values():
        apply_sku_attrs(value, match_fields)

# 测试场景数据
SCENARIOS = [
    {
        "name": "场景0: 设置生产计划",
        "method": "POST",
        "endpoint": "/plan/production",
        "data": {
            "operationType": "ADD",
            "planDate": "2026-01-21 09:00:00",
            "plans": [
                {
                    "planId": "PLAN-LINE1-20260121",
                    "lineId": "LINE-1",
                    "planIndex": [
                        {
                            "requiredSkus": [
                                [
                                    {"skuId": "2801021-H19H0", "quantity": 1},
                                    {"skuId": "2801037-H19H0", "quantity": 1}
                                ],
                                [
                                    {"skuId": "2801021-KFDD0", "quantity": 1},
                                    {"skuId": "2801037-KFDD0", "quantity": 1}
                                ]
                            ]
                        },
                        {
                            "requiredSkus": [
                                [
                                    {"skuId": "2801021-TR200", "quantity": 1},
                                    {"skuId": "2801037-TR200", "quantity": 1}
                                ]
                            ]
                        },
                        {
                            "requiredSkus": [
                                [
                                    {"skuId": "2801021-KS3K0", "quantity": 1},
                                    {"skuId": "2801037-KS3K0", "quantity": 1}
                                ],
                                [
                                    {"skuId": "2801021-TG210", "quantity": 1},
                                    {"skuId": "2801037-TG210", "quantity": 1}
                                ],
                                [
                                    {"skuId": "2801021-KRLM0", "quantity": 1},
                                    {"skuId": "2801037-KRLM0", "quantity": 1}
                                ]
                            ]
                        }
                    ]
                },
                {
                    "planId": "PLAN-LINE2-20260121",
                    "lineId": "LINE-2",
                    "planIndex": [
                        {
                            "requiredSkus": [
                                [
                                    {"skuId": "2801022-H19H0", "quantity": 1},
                                    {"skuId": "2801038-H19H0", "quantity": 1}
                                ]
                            ]
                        },
                        {
                            "requiredSkus": [
                                [
                                    {"skuId": "2801022-TR320", "quantity": 1},
                                    {"skuId": "2801038-TR320", "quantity": 1}
                                ],
                                [
                                    {"skuId": "2801022-KFDE0", "quantity": 1},
                                    {"skuId": "2801038-KFDE0", "quantity": 1}
                                ]
                            ]
                        }
                    ]
                }
            ]
        },
        "wait_after": 1
    },
    {
        "name": "场景0.1: 获取生产计划",
        "method": "GET",
        "endpoint": "/plan/production",
        "wait_after": 1
    },
    {
        "name": "场景1: 初始化库存（全量重置）",
        "method": "POST",
        "endpoint": "/schedule/mixed",
        "data": {
            "currentTime": "2026-01-21 10:00:00",
            "inventory": [
                {"aisleId": "1", "row": 1, "column": 1, "level": 1, "shelf": "UPPER", "positions": [{"skuId": "2801021-H19H0", "quantity": 1}]},
                {"aisleId": "1", "row": 1, "column": 1, "level": 1, "shelf": "LOWER", "positions": [{"skuId": "2801037-H19H0", "quantity": 1}]},
                {"aisleId": "1", "row": 1, "column": 2, "level": 2, "shelf": "UPPER", "positions": [{"skuId": "2801021-KFDD0", "quantity": 1}]},
                {"aisleId": "1", "row": 1, "column": 2, "level": 2, "shelf": "LOWER", "positions": [{"skuId": "2801037-KFDD0", "quantity": 1}]},
                {"aisleId": "1", "row": 1, "column": 3, "level": 3, "shelf": "UPPER", "positions": [{"skuId": "2801021-TR200", "quantity": 1}]},
                {"aisleId": "1", "row": 1, "column": 3, "level": 3, "shelf": "LOWER", "positions": [{"skuId": "2801037-TR200", "quantity": 1}]},
                {"aisleId": "1", "row": 2, "column": 1, "level": 5, "shelf": "UPPER", "positions": [{"skuId": "2801021-KS3K0", "quantity": 1}]},
                {"aisleId": "1", "row": 2, "column": 1, "level": 5, "shelf": "LOWER", "positions": [{"skuId": "2801037-KS3K0", "quantity": 1}]},
                {"aisleId": "1", "row": 2, "column": 2, "level": 6, "shelf": "UPPER", "positions": [{"skuId": "2801021-TG210", "quantity": 1}]},
                {"aisleId": "1", "row": 2, "column": 2, "level": 6, "shelf": "LOWER", "positions": [{"skuId": "2801037-TG210", "quantity": 1}]},
                {"aisleId": "2", "row": 3, "column": 1, "level": 4, "shelf": "UPPER", "positions": [{"skuId": "2801022-H19H0", "quantity": 1}]},
                {"aisleId": "2", "row": 3, "column": 1, "level": 4, "shelf": "LOWER", "positions": [{"skuId": "2801038-H19H0", "quantity": 1}]},
                {"aisleId": "2", "row": 3, "column": 2, "level": 7, "shelf": "UPPER", "positions": [{"skuId": "2801022-TR320", "quantity": 1}]},
                {"aisleId": "2", "row": 3, "column": 2, "level": 7, "shelf": "LOWER", "positions": [{"skuId": "2801038-TR320", "quantity": 1}]},
                {"aisleId": "2", "row": 3, "column": 3, "level": 9, "shelf": "UPPER", "positions": [{"skuId": "2801022-KFDE0", "quantity": 1}]},
                {"aisleId": "2", "row": 3, "column": 3, "level": 9, "shelf": "LOWER", "positions": [{"skuId": "2801038-KFDE0", "quantity": 1}]},
                {"aisleId": "2", "row": 4, "column": 3, "level": 12, "shelf": "UPPER", "positions": [{"skuId": "2801021-KRLM0", "quantity": 1}]},
                {"aisleId": "2", "row": 4, "column": 3, "level": 12, "shelf": "LOWER", "positions": [{"skuId": "2801037-KRLM0", "quantity": 1}]}
            ],
            "aisleStatus": [
                {
                    "aisleId": "1",
                    "isAvailable": True,
                    "bank": "LEFT",
                    "exitCongestion": [
                        {"lineId": "LINE-1", "isCongested": False},
                        {"lineId": "LINE-2", "isCongested": False},
                        {"lineId": "LINE-3", "isCongested": False}
                    ]
                },
                {
                    "aisleId": "2",
                    "isAvailable": True,
                    "bank": "RIGHT",
                    "exitCongestion": [
                        {"lineId": "LINE-1", "isCongested": False},
                        {"lineId": "LINE-2", "isCongested": False},
                        {"lineId": "LINE-3", "isCongested": False}
                    ]
                }
            ],
            "tasks": []
        },
        "wait_after": 1
    },
    {
        "name": "场景2: 入库分配巷道（推荐）",
        "method": "POST",
        "endpoint": "/inbound/allocate",
        "data": {
            "tasks": [
                {
                    "taskId": "INBOUND_A_2801021-KR8H4_2801037-KR8H4",
                    "skus": [
                        {"skuId": "2801021-KR8H4", "quantity": 1},
                        {"skuId": "2801037-KR8H4", "quantity": 1}
                    ]
                }
            ]
        },
        "wait_after": 1
    },
    {
        "name": "场景3: 混合调度 - 分配出入库任务",
        "method": "POST",
        "endpoint": "/schedule/mixed",
        "data": {
            "currentTime": "2026-01-21 10:15:00",
            "inventory": [],
            "aisleStatus": [
                {
                    "aisleId": "1",
                    "isAvailable": True,
                    "bank": "LEFT",
                    "exitCongestion": [
                        {"lineId": "LINE-1", "isCongested": False},
                        {"lineId": "LINE-2", "isCongested": False},
                        {"lineId": "LINE-3", "isCongested": False}
                    ]
                },
                {
                    "aisleId": "2",
                    "isAvailable": True,
                    "bank": "RIGHT",
                    "exitCongestion": [
                        {"lineId": "LINE-1", "isCongested": False},
                        {"lineId": "LINE-2", "isCongested": False},
                        {"lineId": "LINE-3", "isCongested": False}
                    ]
                }
            ],
            "tasks": [
                {
                    "taskId": "OUTBOUND_PL1_GP1_2801021-H19H0_2801037-H19H0",
                    "taskType": "OUTBOUND",
                    "planId": "PLAN-LINE1-20260121",
                    "planIndex": 1,
                    "skus": [
                        {"skuId": "2801021-H19H0", "quantity": 1},
                        {"skuId": "2801037-H19H0", "quantity": 1}
                    ]
                },
                {
                    "taskId": "OUTBOUND_PL1_GP1_2801021-KFDD0_2801037-KFDD0",
                    "taskType": "OUTBOUND",
                    "planId": "PLAN-LINE1-20260121",
                    "planIndex": 1,
                    "skus": [
                        {"skuId": "2801021-KFDD0", "quantity": 1},
                        {"skuId": "2801037-KFDD0", "quantity": 1}
                    ]
                },
                {
                    "taskId": "OUTBOUND_PL2_GP1_2801022-H19H0_2801038-H19H0",
                    "taskType": "OUTBOUND",
                    "planId": "PLAN-LINE2-20260121",
                    "planIndex": 1,
                    "skus": [
                        {"skuId": "2801022-H19H0", "quantity": 1},
                        {"skuId": "2801038-H19H0", "quantity": 1}
                    ]
                },
                {
                    "taskId": "INBOUND_2801021-KR8H4_2801037-KR8H4",
                    "taskType": "INBOUND",
                    "skus": [
                        {"skuId": "2801021-KR8H4", "quantity": 1},
                        {"skuId": "2801037-KR8H4", "quantity": 1}
                    ],
                    "targetAisle": "1"
                }
            ]
        },
        "wait_after": 2,
        "save_response_key": "assigned_tasks"  # 保存分配结果，用于后续反馈
    },
    {
        "name": "场景3.1: 未确认任务再次调度（409）",
        "method": "POST",
        "endpoint": "/schedule/mixed",
        "data": {
            "currentTime": "2026-01-21 10:16:00",
            "inventory": [],
            "aisleStatus": [
                {
                    "aisleId": "1",
                    "isAvailable": True,
                    "bank": "LEFT",
                    "exitCongestion": [
                        {"lineId": "LINE-1", "isCongested": False},
                        {"lineId": "LINE-2", "isCongested": False},
                        {"lineId": "LINE-3", "isCongested": False}
                    ]
                },
                {
                    "aisleId": "2",
                    "isAvailable": True,
                    "bank": "RIGHT",
                    "exitCongestion": [
                        {"lineId": "LINE-1", "isCongested": False},
                        {"lineId": "LINE-2", "isCongested": False},
                        {"lineId": "LINE-3", "isCongested": False}
                    ]
                }
            ],
            "tasks": []
        },
        "expected_status": 409,
        "wait_after": 1
    },
    {
        "name": "场景3.2: 查看未确认任务",
        "method": "GET",
        "endpoint": "/task/unconfirmed",
        "wait_after": 1
    },
    {
        "name": "场景4: 任务执行反馈 - EXECUTING",
        "method": "MULTI_POST",  # 特殊标记：需要发送多个请求
        "endpoint": "/task/feedback",
        "data_template": {
            "taskType": "OUTBOUND",
            "status": "EXECUTING",
            "startTime": "2026-01-21T10:16:00Z"
        },
        "use_assigned_tasks": True,  # 使用场景3返回的任务ID
        "wait_after": 2
    },
    {
        "name": "场景5: 任务完成反馈 - COMPLETED",
        "method": "MULTI_POST",
        "endpoint": "/task/feedback",
        "data_template": {
            "taskType": "OUTBOUND",
            "status": "COMPLETED",
            "startTime": "2026-01-21T10:16:00Z"
        },
        "use_assigned_tasks": True,
        "wait_after": 2
    },
    {
        "name": "场景6: 库存同步 - 增量更新",
        "method": "POST",
        "endpoint": "/schedule/mixed",
        "data": {
            "currentTime": "2026-01-21 10:20:00",
            "inventory": [
                {"aisleId": "1", "row": 1, "column": 1, "level": 1, "shelf": "UPPER", "positions": [{"skuId": "", "quantity": 0}]},
                {"aisleId": "1", "row": 1, "column": 1, "level": 1, "shelf": "LOWER", "positions": [{"skuId": "", "quantity": 0}]},
                {"aisleId": "2", "row": 3, "column": 1, "level": 4, "shelf": "UPPER", "positions": [{"skuId": "", "quantity": 0}]},
                {"aisleId": "2", "row": 3, "column": 1, "level": 4, "shelf": "LOWER", "positions": [{"skuId": "", "quantity": 0}]}
            ],
            "aisleStatus": [
                {
                    "aisleId": "1",
                    "isAvailable": True,
                    "bank": "LEFT",
                    "exitCongestion": [
                        {"lineId": "LINE-1", "isCongested": False},
                        {"lineId": "LINE-2", "isCongested": False},
                        {"lineId": "LINE-3", "isCongested": False}
                    ]
                },
                {
                    "aisleId": "2",
                    "isAvailable": True,
                    "bank": "RIGHT",
                    "exitCongestion": [
                        {"lineId": "LINE-1", "isCongested": False},
                        {"lineId": "LINE-2", "isCongested": False},
                        {"lineId": "LINE-3", "isCongested": False}
                    ]
                }
            ],
            "tasks": []
        },
        "wait_after": 1
    },
    {
        "name": "场景6.1: 获取系统状态",
        "method": "GET",
        "endpoint": "/status",
        "wait_after": 1
    }
]


class APITester:
    """API 测试器"""
    
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip('/')
        self.api_url = f"{self.base_url}{API_PREFIX}"
        self.session = requests.Session()
        self.saved_data: Dict[str, Any] = {}
        
    def execute_scenario(self, scenario: Dict[str, Any], scenario_index: int) -> bool:
        """
        执行单个测试场景
        
        Returns:
            True if success, False if failed
        """
        print(f"\n{'='*80}")
        print(f"执行: {scenario['name']}")
        print(f"{'='*80}")
        
        method = scenario.get("method", "POST")
        endpoint = scenario["endpoint"]
        url = f"{self.api_url}{endpoint}"
        
        # 处理特殊的多请求场景
        if method == "MULTI_POST":
            return self._execute_multi_post(scenario, url)
        
        # 普通单请求场景
        data = scenario.get("data", {})
        
        print(f"\n请求:")
        print(f"  Method: {method}")
        print(f"  URL: {url}")
        print(f"  Data: {json.dumps(data, indent=2, ensure_ascii=False)}")
        
        try:
            if method == "GET":
                response = self.session.get(url)
            elif method == "POST":
                response = self.session.post(url, json=data)
            else:
                print(f"  ❌ 不支持的方法: {method}")
                return False
            
            print(f"\n响应:")
            print(f"  Status Code: {response.status_code}")
            
            # 尝试解析 JSON 响应
            try:
                response_data = response.json()
                print(f"  Data: {json.dumps(response_data, indent=2, ensure_ascii=False)}")
                
                # 保存响应数据（如果需要）
                save_key = scenario.get("save_response_key")
                if save_key:
                    self.saved_data[save_key] = response_data
                    print(f"  ✓ 已保存响应数据到: {save_key}")
                
            except json.JSONDecodeError:
                print(f"  Data: {response.text}")
            
            # 检查状态码
            expected_status = scenario.get("expected_status")
            if expected_status is not None:
                if response.status_code != expected_status:
                    print(f"\n  ❌ 请求失败! 期望 {expected_status}，实际 {response.status_code}")
                    return False
            elif response.status_code >= 400:
                print(f"\n  ❌ 请求失败! Status Code: {response.status_code}")
                return False
            
            print(f"\n  ✅ 场景执行成功!")
            
            # 等待
            wait_time = scenario.get("wait_after", 0)
            if wait_time > 0:
                print(f"  ⏳ 等待 {wait_time} 秒...")
                time.sleep(wait_time)
            
            return True
            
        except requests.exceptions.RequestException as e:
            print(f"\n  ❌ 请求异常: {e}")
            return False
    
    def _execute_multi_post(self, scenario: Dict[str, Any], base_url: str) -> bool:
        """
        执行多个 POST 请求（用于任务反馈场景）
        """
        # 获取要反馈的任务列表
        if scenario.get("use_assigned_tasks"):
            assigned_tasks_response = self.saved_data.get("assigned_tasks", {})
            response_data = assigned_tasks_response.get("data", {}) or {}
            aisle_assignments = response_data.get("aisleAssignments", [])
            
            # 提取所有已分配的任务ID
            task_ids = []
            for assignment in aisle_assignments:
                assigned_task = assignment.get("assignedTask")
                if assigned_task and assigned_task.get("taskId"):
                    task_ids.append(assigned_task["taskId"])
            
            if not task_ids:
                print("  ⚠️ 没有找到已分配的任务，跳过此场景")
                return True
            
            print(f"  找到 {len(task_ids)} 个已分配的任务")
        else:
            print("  ❌ 未指定任务来源")
            return False
        
        # 为每个任务发送反馈
        data_template = scenario.get("data_template", {})
        success_count = 0
        
        for i, task_id in enumerate(task_ids, 1):
            print(f"\n  [{i}/{len(task_ids)}] 反馈任务: {task_id}")
            
            # 构建请求数据
            feedback_data = {
                "taskId": task_id,
                **data_template
            }
            
            print(f"    Data: {json.dumps(feedback_data, indent=6, ensure_ascii=False)}")
            
            try:
                response = self.session.post(base_url, json=feedback_data)
                print(f"    Status Code: {response.status_code}")
                
                try:
                    response_data = response.json()
                    print(f"    Response: {json.dumps(response_data, indent=6, ensure_ascii=False)}")
                except json.JSONDecodeError:
                    print(f"    Response: {response.text}")
                
                if response.status_code < 400:
                    print(f"    ✅ 反馈成功")
                    success_count += 1
                else:
                    print(f"    ❌ 反馈失败")
                
                # 每个反馈之间稍作等待
                if i < len(task_ids):
                    time.sleep(0.5)
                    
            except requests.exceptions.RequestException as e:
                print(f"    ❌ 请求异常: {e}")
        
        # 等待
        wait_time = scenario.get("wait_after", 0)
        if wait_time > 0:
            print(f"\n  ⏳ 等待 {wait_time} 秒...")
            time.sleep(wait_time)
        
        # 如果至少有一个成功，就认为场景成功
        if success_count > 0:
            print(f"\n  ✅ 场景执行完成! ({success_count}/{len(task_ids)} 成功)")
            return True
        else:
            print(f"\n  ❌ 场景执行失败! 所有反馈都失败了")
            return False
    
    def run_all_scenarios(self):
        """运行所有测试场景"""
        print("\n" + "="*80)
        print("开始执行 API 自动化测试")
        print("="*80)
        print(f"API Base URL: {self.api_url}")
        print(f"总共 {len(SCENARIOS)} 个测试场景")
        
        match_fields = load_match_fields()
        if match_fields:
            for scenario in SCENARIOS:
                if "data" in scenario:
                    apply_sku_attrs(scenario["data"], match_fields)
        
        success_count = 0
        failed_scenarios = []
        
        for i, scenario in enumerate(SCENARIOS):
            success = self.execute_scenario(scenario, i)
            if success:
                success_count += 1
            else:
                failed_scenarios.append(scenario["name"])
        
        # 打印总结
        print("\n" + "="*80)
        print("测试执行完成")
        print("="*80)
        print(f"总场景数: {len(SCENARIOS)}")
        print(f"成功: {success_count}")
        print(f"失败: {len(failed_scenarios)}")
        
        if failed_scenarios:
            print(f"\n失败的场景:")
            for name in failed_scenarios:
                print(f"  - {name}")
        else:
            print(f"\n🎉 所有测试场景执行成功!")
        
        return len(failed_scenarios) == 0


def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description="仓库管理系统 API 自动化测试脚本"
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"API 基础 URL (默认: {DEFAULT_BASE_URL})"
    )
    
    args = parser.parse_args()
    
    # 创建测试器并运行
    tester = APITester(args.base_url)
    success = tester.run_all_scenarios()
    
    # 返回退出码
    return 0 if success else 1


if __name__ == "__main__":
    exit(main())

