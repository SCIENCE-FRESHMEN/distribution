#!/usr/bin/env python3
"""
API flow test script for current warehouse service.

Flow:
0. Init inventory and sync production plan inline via /schedule/mixed
1. Query synced production plan
2. Inbound aisle recommendation
3. Mixed scheduling
4. Feedback EXECUTING for assigned tasks
5. Feedback COMPLETED for assigned tasks
6. Incremental inventory sync and edge-case checks

Usage:
    python test_api_flow.py --base-url http://localhost:8000
"""

import argparse
import copy
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import requests


DEFAULT_BASE_URL = "http://localhost:8000"
API_PREFIX = "/api/v1"
CONFIG_PATH = Path(__file__).resolve().parent / "config" / "warehouse.json"
LOG_DIR = Path(__file__).resolve().parent / "logs"
DEFAULT_TEST_FEATURES: Dict[str, Any] = {
    "color": "W1",
    "skid_type": "0",
    "skid_state": "1",
}
BASE_PRODUCTION_PLAN: Dict[str, Any] = {
    "operationType": "ADD",
    "planDate": "2026-01-21 09:00:00",
    "plans": [
        {
            "planId": "PLAN-LINE1-20260121",
            "lineId": "LINE-1",
            "planIndex": [
                {"requiredSkus": [[{"skuId": "1RAT000001", "quantity": 1}]]},
                {"requiredSkus": [[{"skuId": "1RAT000002", "quantity": 1}]]},
                {"requiredSkus": [[{"skuId": "1RAT000003", "quantity": 1}]]},
                {"requiredSkus": [[{"skuId": "1RAT000004", "quantity": 1}]]},
                {"requiredSkus": [[{"skuId": "1RAT000005", "quantity": 1}]]},
                {"requiredSkus": [[{"skuId": "1RAT000006", "quantity": 1}]]},
            ],
        },
        {
            "planId": "PLAN-LINE2-20260121",
            "lineId": "LINE-2",
            "planIndex": [
                {"requiredSkus": [[{"skuId": "1RAK000005", "quantity": 1}]]},
                {"requiredSkus": [[{"skuId": "1RAK000006", "quantity": 1}]]},
            ],
        },
    ],
}
BASE_CURRENT_GROUPS: Dict[str, int] = {
    "LINE-1": 1,
    "LINE-2": 1,
}
APPEND_PRODUCTION_PLAN_1: Dict[str, Any] = {
    "operationType": "ADD",
    "planDate": "2026-01-21 12:00:00",
    "plans": [
        {
            "planId": "PLAN-LINE1-APPEND-1",
            "lineId": "LINE-1",
            "planIndex": [
                {"requiredSkus": [[{"skuId": "1RAT000011", "quantity": 1}]]},
                {"requiredSkus": [[{"skuId": "1RAT000012", "quantity": 1}]]},
            ],
        },
        {
            "planId": "PLAN-LINE2-APPEND-1",
            "lineId": "LINE-2",
            "planIndex": [
                {"requiredSkus": [[{"skuId": "1RAK000011", "quantity": 1}]]},
            ],
        },
    ],
}
APPEND_PRODUCTION_PLAN_2: Dict[str, Any] = {
    "operationType": "ADD",
    "planDate": "2026-01-21 12:10:00",
    "plans": [
        {
            "planId": "PLAN-LINE1-APPEND-2",
            "lineId": "LINE-1",
            "planIndex": [
                {"requiredSkus": [[{"skuId": "1RAT000013", "quantity": 1}]]},
            ],
        },
    ],
}


class Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data: str) -> None:
        for s in self.streams:
            s.write(data)
            s.flush()

    def flush(self) -> None:
        for s in self.streams:
            s.flush()


def load_config() -> Dict[str, Any]:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def load_match_fields(cfg: Dict[str, Any]) -> List[str]:
    return list(cfg.get("match_fields", []) or [])


def load_outbound_feature_fields_by_line(cfg: Dict[str, Any]) -> Dict[int, List[str]]:
    raw = cfg.get("outbound_match_features", {}) or {}
    if isinstance(raw, dict):
        result: Dict[int, List[str]] = {}
        for k, v in raw.items():
            try:
                line = int(k)
            except Exception:
                continue
            fields = [str(x) for x in (v or []) if str(x).lower() != "rfid"]
            result[line] = fields
        return result
    if isinstance(raw, list):
        fields = [str(x) for x in raw if str(x).lower() != "rfid"]
        return {1: fields, 2: fields, 3: fields}
    return {}


def default_attr_value(field: str) -> Any:
    if field == "version":
        return "00"
    if field == "color":
        return "red"
    return f"{field}-default"


def apply_sku_attrs_to_payload(obj: Any, required_fields: List[str]) -> None:
    if not required_fields:
        return

    if isinstance(obj, list):
        for item in obj:
            apply_sku_attrs_to_payload(item, required_fields)
        return

    if not isinstance(obj, dict):
        return

    if "skuId" in obj:
        sku_id = obj.get("skuId")
        qty = obj.get("quantity", 1)
        if sku_id and qty > 0:
            features = obj.get("features")
            if not isinstance(features, dict):
                features = {}
            for field in required_fields:
                if field not in obj:
                    obj[field] = default_attr_value(field)
                if field not in features:
                    features[field] = obj[field]
            obj["features"] = features

    for value in obj.values():
        apply_sku_attrs_to_payload(value, required_fields)


def remove_shelf_fields(obj: Any) -> None:
    if isinstance(obj, list):
        for item in obj:
            remove_shelf_fields(item)
        return
    if not isinstance(obj, dict):
        return
    obj.pop("shelf", None)
    for value in obj.values():
        remove_shelf_fields(value)


def apply_plan_line_feature_fields(plan_payload: Dict[str, Any], feature_fields_by_line: Dict[int, List[str]]) -> None:
    plans = plan_payload.get("plans", [])
    for plan in plans:
        line_raw = str(plan.get("lineId", ""))
        line_id = int(line_raw.replace("LINE-", "")) if "LINE-" in line_raw else int(line_raw or 0)
        required = feature_fields_by_line.get(line_id, [])
        if not required:
            continue

        for group in plan.get("planIndex", []):
            for task_skus in group.get("requiredSkus", []):
                for sku in task_skus:
                    sku_id = sku.get("skuId")
                    qty = sku.get("quantity", 1)
                    if not sku_id or qty <= 0:
                        continue
                    features = sku.get("features")
                    if not isinstance(features, dict):
                        features = {}
                    for field in required:
                        if field not in sku:
                            sku[field] = default_attr_value(field)
                        if field not in features:
                            features[field] = sku[field]
                    sku["features"] = features


def apply_default_features_to_payload(obj: Any, defaults: Dict[str, Any]) -> None:
    if isinstance(obj, list):
        for item in obj:
            apply_default_features_to_payload(item, defaults)
        return
    if not isinstance(obj, dict):
        return
    if "skuId" in obj:
        sku_id = obj.get("skuId")
        qty = obj.get("quantity", 1)
        qty_ok = False
        try:
            qty_ok = bool(qty) and int(qty) > 0
        except Exception:
            qty_ok = bool(qty)
        if sku_id and qty_ok:
            features = obj.get("features")
            if not isinstance(features, dict):
                features = {}
            for k, v in defaults.items():
                features.setdefault(k, v)
            obj["features"] = features
    for value in obj.values():
        apply_default_features_to_payload(value, defaults)


def extract_plan_group_sku(plan_payload: Dict[str, Any], line_id: str, group_index: int) -> str:
    payload = plan_payload.get("data", plan_payload) if isinstance(plan_payload, dict) else {}
    production_plan = payload.get("production_plan", {}) if isinstance(payload, dict) else {}

    line_groups = None
    if isinstance(production_plan, dict):
        line_groups = production_plan.get(line_id)
        if line_groups is None:
            try:
                line_groups = production_plan.get(int(line_id))
            except Exception:
                line_groups = None
    if not isinstance(line_groups, list) or group_index >= len(line_groups):
        return ""

    group = line_groups[group_index]
    if not isinstance(group, list) or not group:
        return ""
    first_task = group[0]
    if not isinstance(first_task, list) or not first_task:
        return ""
    first_sku = first_task[0]
    if not isinstance(first_sku, dict):
        return ""
    return str(first_sku.get("skuId") or "")


SCENARIOS: List[Dict[str, Any]] = [
    {
        "name": "S0 init inventory full reset with inline production plan",
        "method": "POST",
        "endpoint": "/schedule/mixed",
        "data": {
            "currentTime": "2026-01-21 09:00:00",
            "productionPlan": BASE_PRODUCTION_PLAN,
            "currentGroups": BASE_CURRENT_GROUPS,
            "inventory": [
                {"aisleId": "1", "row": 1, "column": 2, "level": 1, "positions": [{"skuId": "1RAT000001", "quantity": 1}]},
                {"aisleId": "1", "row": 1, "column": 2, "level": 2, "positions": [{"skuId": "1RAT000002", "quantity": 1}]},
                {"aisleId": "1", "row": 1, "column": 3, "level": 3, "positions": [{"skuId": "1RAT000003", "quantity": 1}]},
                {"aisleId": "1", "row": 1, "column": 3, "level": 4, "positions": [{"skuId": "1RAT000004", "quantity": 1}]},
                {"aisleId": "2", "row": 3, "column": 2, "level": 1, "positions": [{"skuId": "1RAK000005", "quantity": 1}]},
                {"aisleId": "2", "row": 3, "column": 2, "level": 2, "positions": [{"skuId": "1RAK000006", "quantity": 1}]},
                {"aisleId": "2", "row": 3, "column": 3, "level": 3, "positions": [{"skuId": "1RAK000007", "quantity": 1}]},
                {"aisleId": "2", "row": 3, "column": 3, "level": 4, "positions": [{"skuId": "1RAK000008", "quantity": 1}]},
                {"aisleId": "1", "row": 2, "column": 2, "level": 1, "positions": [{"skuId": "1RAT000005", "quantity": 1}]},
                {"aisleId": "1", "row": 2, "column": 2, "level": 2, "positions": [{"skuId": "1RAT000006", "quantity": 1}]},
                {"aisleId": "1", "row": 2, "column": 3, "level": 3, "positions": [{"skuId": "1RAT000007", "quantity": 1}]},
                {"aisleId": "1", "row": 2, "column": 3, "level": 4, "positions": [{"skuId": "1RAT000008", "quantity": 1}]},
                {"aisleId": "2", "row": 4, "column": 2, "level": 1, "positions": [{"skuId": "1RAT000009", "quantity": 1}]},
                {"aisleId": "2", "row": 4, "column": 2, "level": 2, "positions": [{"skuId": "1RAT000010", "quantity": 1}]},
                {"aisleId": "2", "row": 4, "column": 3, "level": 3, "positions": [{"skuId": "1RAK000009", "quantity": 1}]},
                {"aisleId": "2", "row": 4, "column": 3, "level": 4, "positions": [{"skuId": "1RAK000010", "quantity": 1}]},
                # 每个巷道增加一个空滑橇
                {"aisleId": "1", "row": 1, "column": 4, "level": 1, "positions": [{"skuId": "0EMP000001", "quantity": 1, "features": {"skid_state": "0", "skid_type": "0", "color": "W1"}}]},
                {"aisleId": "2", "row": 3, "column": 4, "level": 1, "positions": [{"skuId": "0EMP000002", "quantity": 1, "features": {"skid_state": "0", "skid_type": "0", "color": "W1"}}]},
            ],
            "aisleStatus": [
                {
                    "aisleId": "1",
                    "isAvailable": True,
                    "bank": "LEFT",
                    "exitCongestion": [
                        {"lineId": "LINE-1", "isCongested": False},
                        {"lineId": "LINE-2", "isCongested": False},
                        {"lineId": "LINE-3", "isCongested": False},
                    ],
                },
                {
                    "aisleId": "2",
                    "isAvailable": True,
                    "bank": "RIGHT",
                    "exitCongestion": [
                        {"lineId": "LINE-1", "isCongested": False},
                        {"lineId": "LINE-2", "isCongested": False},
                        {"lineId": "LINE-3", "isCongested": False},
                    ],
                },
            ],
            "tasks": [],
        },
        "wait_after": 1,
    },
    {
        "name": "S0.1 get production plan",
        "method": "GET",
        "endpoint": "/plan/production",
        "wait_after": 1,
    },
    {
        "name": "S2 inbound aisle recommendation",
        "method": "POST",
        "endpoint": "/inbound/allocate",
        "data": {
            "tasks": [
                {
                    "taskId": "INBOUND_A_1RAT000012",
                    "inLine": "L4C1",
                    "outLine": "L1C17",
                    "skus": [
                        {"skuId": "1RAT000012", "quantity": 1},
                    ],
                },
                {
                    "taskId": "INBOUND_A_1RAT000013",
                    "inLine": "L1C17",
                    "outLine": "L2C1",
                    "skus": [
                        {"skuId": "1RAT000013", "quantity": 1},
                    ],
                }
            ]
        },
        "wait_after": 1,
    },
    {
        "name": "S3 mixed scheduling",
        "method": "POST",
        "endpoint": "/schedule/mixed",
        "data": {
            "currentTime": "2026-01-21 10:15:00",
            "productionPlan": BASE_PRODUCTION_PLAN,
            "currentGroups": BASE_CURRENT_GROUPS,
            "inventory": [],
            "aisleStatus": [
                {
                    "aisleId": "1",
                    "isAvailable": True,
                    "bank": "LEFT",
                    "exitCongestion": [
                        {"lineId": "LINE-1", "isCongested": False},
                        {"lineId": "LINE-2", "isCongested": False},
                        {"lineId": "LINE-3", "isCongested": False},
                    ],
                },
                {
                    "aisleId": "2",
                    "isAvailable": True,
                    "bank": "RIGHT",
                    "exitCongestion": [
                        {"lineId": "LINE-1", "isCongested": False},
                        {"lineId": "LINE-2", "isCongested": False},
                        {"lineId": "LINE-3", "isCongested": False},
                    ],
                },
            ],
            "tasks": [
                {
                    "taskId": "OUTBOUND_PL1_GP1_1RAT000001",
                    "taskType": "OUTBOUND",
                    "planId": "PLAN-LINE1-20260121",
                    "planIndex": 1,
                    "inLine": "L1C17",
                    "outLine": "L1C17",
                    "skus": [
                        {"skuId": "1RAT000001", "quantity": 1},
                    ],
                },
                {
                    "taskId": "OUTBOUND_PL1_GP1_1RAT000002",
                    "taskType": "OUTBOUND",
                    "planId": "PLAN-LINE1-20260121",
                    "planIndex": 1,
                    "inLine": "L1C17",
                    "outLine": "L1C1",
                    "skus": [
                        {"skuId": "1RAT000002", "quantity": 1},
                    ],
                },
                {
                    "taskId": "OUTBOUND_PL2_GP1_1RAK000005",
                    "taskType": "OUTBOUND",
                    "planId": "PLAN-LINE2-20260121",
                    "planIndex": 1,
                    "inLine": "L2C17",
                    "outLine": "L2C17",
                    "skus": [
                        {"skuId": "1RAK000005", "quantity": 1},
                    ],
                },
                {
                    "taskId": "OUTBOUND_PL2_GP1_1RAK000006",
                    "taskType": "OUTBOUND",
                    "planId": "PLAN-LINE2-20260121",
                    "planIndex": 1,
                    "inLine": "L2C17",
                    "outLine": "L2C1",
                    "skus": [
                        {"skuId": "1RAK000006", "quantity": 1},
                    ],
                },
                {
                    "taskId": "INBOUND_1RAT000012",
                    "taskType": "INBOUND",
                    "targetAisle": "1",
                    "inLine": "L4C1",
                    "outLine": "L1C17",
                    "skus": [
                        {"skuId": "1RAT000012", "quantity": 1},
                    ],
                },
                {
                    "taskId": "INBOUND_1RAT000013",
                    "taskType": "INBOUND",
                    "targetAisle": "2",
                    "inLine": "L1C17",
                    "outLine": "L2C1",
                    "skus": [
                        {"skuId": "1RAT000013", "quantity": 1},
                    ],
                },
            ],
        },
        "save_response_key": "assigned_tasks",
        "wait_after": 2,
    },
    {
        "name": "S3.1 schedule again before confirm should keep unconfirmed assignments frozen",
        "method": "POST",
        "endpoint": "/schedule/mixed",
        "data": {
            "inventory": [],
            "aisleStatus": [
                {
                    "aisleId": "1",
                    "isAvailable": True,
                    "bank": "LEFT",
                    "exitCongestion": [
                        {"lineId": "LINE-1", "isCongested": False},
                        {"lineId": "LINE-2", "isCongested": False},
                        {"lineId": "LINE-3", "isCongested": False},
                    ],
                },
                {
                    "aisleId": "2",
                    "isAvailable": True,
                    "bank": "RIGHT",
                    "exitCongestion": [
                        {"lineId": "LINE-1", "isCongested": False},
                        {"lineId": "LINE-2", "isCongested": False},
                        {"lineId": "LINE-3", "isCongested": False},
                    ],
                },
            ],
            "tasks": [],
        },
        "assert_assigned_task_count": 2,
        "wait_after": 1,
    },
    {"name": "S3.2 get unconfirmed", "method": "GET", "endpoint": "/task/unconfirmed", "wait_after": 1},
    {
        "name": "S4 feedback EXECUTING",
        "method": "MULTI_POST",
        "endpoint": "/task/feedback",
        "data_template": {"status": "EXECUTING", "startTime": "2026-01-21T10:16:00Z"},
        "use_assigned_tasks": True,
        "wait_after": 2,
    },
    {
        "name": "S5 feedback COMPLETED",
        "method": "MULTI_POST",
        "endpoint": "/task/feedback",
        "data_template": {"status": "COMPLETED", "startTime": "2026-01-21T10:16:00Z"},
        "use_assigned_tasks": True,
        "wait_after": 2,
    },
    {
        "name": "S6 incremental inventory sync",
        "method": "POST",
        "endpoint": "/schedule/mixed",
        "data": {
            "inventory": [
                {"aisleId": "1", "row": 1, "column": 1, "level": 1, "positions": [{"skuId": "", "quantity": 0}]},
                {"aisleId": "1", "row": 1, "column": 1, "level": 1, "positions": [{"skuId": "", "quantity": 0}]},
                {"aisleId": "2", "row": 3, "column": 1, "level": 4, "positions": [{"skuId": "", "quantity": 0}]},
                {"aisleId": "2", "row": 3, "column": 1, "level": 4, "positions": [{"skuId": "", "quantity": 0}]},
            ],
            "aisleStatus": [
                {
                    "aisleId": "1",
                    "isAvailable": True,
                    "bank": "LEFT",
                    "exitCongestion": [
                        {"lineId": "LINE-1", "isCongested": False},
                        {"lineId": "LINE-2", "isCongested": False},
                        {"lineId": "LINE-3", "isCongested": False},
                    ],
                },
                {
                    "aisleId": "2",
                    "isAvailable": True,
                    "bank": "RIGHT",
                    "exitCongestion": [
                        {"lineId": "LINE-1", "isCongested": False},
                        {"lineId": "LINE-2", "isCongested": False},
                        {"lineId": "LINE-3", "isCongested": False},
                    ],
                },
            ],
            "tasks": [],
        },
        "wait_after": 1,
    },
    {"name": "S6.1 get system status", "method": "GET", "endpoint": "/status", "wait_after": 1},
    {
        "name": "S6.15 duplicate task ids in one mixed request should be rejected",
        "method": "POST",
        "endpoint": "/schedule/mixed",
        "data": {
            "inventory": [],
            "aisleStatus": [
                {"aisleId": "1", "isAvailable": True, "bank": "LEFT", "exitCongestion": []},
                {"aisleId": "2", "isAvailable": True, "bank": "RIGHT", "exitCongestion": []},
            ],
            "tasks": [
                {"taskId": "DUPLICATE_TASK_001", "taskType": "INBOUND", "targetAisle": "1", "inLine": "L4C1", "outLine": "L1C17", "skus": [{"skuId": "1RAT000012", "quantity": 1}]},
                {"taskId": "DUPLICATE_TASK_001", "taskType": "INBOUND", "targetAisle": "2", "inLine": "L1C17", "outLine": "L2C1", "skus": [{"skuId": "1RAT000013", "quantity": 1}]},
            ],
        },
        "expected_status": 400,
        "wait_after": 1,
    },
    {
        "name": "S6.16 disabled inventory position should be rejected",
        "method": "POST",
        "endpoint": "/schedule/mixed",
        "data": {
            "inventory": [
                {"aisleId": "1", "row": 1, "column": 1, "level": 1, "positions": [{"skuId": "1RAT000001", "quantity": 1}]},
            ],
            "aisleStatus": [
                {"aisleId": "1", "isAvailable": True, "bank": "LEFT", "exitCongestion": []},
                {"aisleId": "2", "isAvailable": True, "bank": "RIGHT", "exitCongestion": []},
            ],
            "tasks": [],
        },
        "expected_status": 400,
        "wait_after": 1,
    },
    {
        "name": "S6.2 cleanup pending before empty-skid test",
        "method": "CLEAN_PENDING",
        "wait_after": 1,
    },
    {
        "name": "S6.3 all outbound docks disabled should block outbound dispatch",
        "method": "POST",
        "endpoint": "/schedule/mixed",
        "data": {
            "currentTime": "2026-01-21 10:15:00",
            "productionPlan": BASE_PRODUCTION_PLAN,
            "currentGroups": BASE_CURRENT_GROUPS,
            "inventory": [],
            "aisleStatus": [
                {
                    "aisleId": "1",
                    "isAvailable": True,
                    "bank": "LEFT",
                    "exitCongestion": [
                        {"lineId": "LINE-1", "isCongested": False},
                        {"lineId": "LINE-2", "isCongested": False},
                        {"lineId": "LINE-3", "isCongested": False},
                    ],
                    "dockAvailability": [
                        {"direction": "OUTBOUND", "lineRef": "L2C17", "isAvailable": False, "reason": "TEST_DISABLE"},
                        {"direction": "OUTBOUND", "lineRef": "L2C1", "isAvailable": False, "reason": "TEST_DISABLE"},
                    ],
                },
                {
                    "aisleId": "2",
                    "isAvailable": True,
                    "bank": "RIGHT",
                    "exitCongestion": [
                        {"lineId": "LINE-1", "isCongested": False},
                        {"lineId": "LINE-2", "isCongested": False},
                        {"lineId": "LINE-3", "isCongested": False},
                    ],
                    "dockAvailability": [
                        {"direction": "OUTBOUND", "lineRef": "L2C17", "isAvailable": False, "reason": "TEST_DISABLE"},
                        {"direction": "OUTBOUND", "lineRef": "L2C1", "isAvailable": False, "reason": "TEST_DISABLE"},
                    ],
                },
            ],
            "tasks": [
                {
                    "taskId": "OUTBOUND_DOCK_BLOCKED_001",
                    "taskType": "OUTBOUND",
                    "planId": "PLAN-LINE2-20260121",
                    "planIndex": 1,
                    "inLine": "L2C17",
                    "outLine": "L2C17",
                    "skus": [
                        {"skuId": "1RAK000005", "quantity": 1},
                    ],
                },
                {
                    "taskId": "OUTBOUND_DOCK_BLOCKED_002",
                    "taskType": "OUTBOUND",
                    "planId": "PLAN-LINE2-20260121",
                    "planIndex": 1,
                    "inLine": "L2C17",
                    "outLine": "L2C1",
                    "skus": [
                        {"skuId": "1RAK000006", "quantity": 1},
                    ],
                },
            ],
        },
        "assert_no_assigned_task_prefix": "OUTBOUND_DOCK_",
        "wait_after": 1,
    },
    {
        "name": "S6.35 cleanup pending before empty-skid scheduling",
        "method": "CLEAN_PENDING",
        "wait_after": 1,
    },
    {
        "name": "S7 empty-skid outbound request should be scheduled with high priority",
        "method": "POST",
        "endpoint": "/schedule/mixed",
        "data": {
            "inventory": [],
            "aisleStatus": [
                {
                    "aisleId": "1",
                    "isAvailable": True,
                    "bank": "LEFT",
                    "exitCongestion": [
                        {"lineId": "LINE-1", "isCongested": False},
                        {"lineId": "LINE-2", "isCongested": False},
                        {"lineId": "LINE-3", "isCongested": False},
                    ],
                },
                {
                    "aisleId": "2",
                    "isAvailable": True,
                    "bank": "RIGHT",
                    "exitCongestion": [
                        {"lineId": "LINE-1", "isCongested": False},
                        {"lineId": "LINE-2", "isCongested": False},
                        {"lineId": "LINE-3", "isCongested": False},
                    ],
                },
            ],
            "tasks": [
                {
                    "taskId": "OUTBOUND_EMPTY_SKID_REQ_001",
                    "taskType": "OUTBOUND",
                    "planId": "PLAN-LINE1-20260121",
                    "planIndex": 1,
                    "inLine": "L1C17",
                    "outLine": "L2C17",
                    "skus": [
                        # empty-skid request: only skid_state=0 is required for detection
                        {"skuId": "", "quantity": 1, "features": {"skid_state": "0", "skid_type": "0", "color": "W1"}},
                    ],
                },
                {
                    "taskId": "OUTBOUND_NORMAL_FOR_COMPARE_001",
                    "taskType": "OUTBOUND",
                    "planId": "PLAN-LINE1-20260121",
                    "planIndex": 1,
                    "inLine": "L1C17",
                    "outLine": "L1C17",
                    "skus": [
                        {"skuId": "1RAT000003", "quantity": 1},
                    ],
                },
            ],
        },
        "assert_assigned_task_id": "OUTBOUND_EMPTY_SKID_REQ_001",
        "wait_after": 1,
    },
    {
        "name": "S7.1 pending queue head should be empty-skid outbound",
        "method": "GET",
        "endpoint": "/task/pending",
        "assert_first_pending_task_id": "OUTBOUND_EMPTY_SKID_REQ_001",
        "wait_after": 1,
    },
    {
        "name": "S8 cleanup pending before partial-task scheduling test",
        "method": "CLEAN_PENDING",
        "wait_after": 1,
    },
    {
        "name": "S8.1 reset minimal inventory for partial-task scheduling",
        "method": "POST",
        "endpoint": "/schedule/mixed",
        "data": {
            "currentTime": "2026-01-21 11:00:00",
            "productionPlan": BASE_PRODUCTION_PLAN,
            "currentGroups": BASE_CURRENT_GROUPS,
            "inventory": [
                {"aisleId": "1", "row": 1, "column": 2, "level": 1, "positions": [{"skuId": "1RAT000001", "quantity": 1}]},
                {"aisleId": "1", "row": 1, "column": 2, "level": 2, "positions": [{"skuId": "1RAT000002", "quantity": 1}]},
                {"aisleId": "2", "row": 3, "column": 2, "level": 1, "positions": [{"skuId": "1RAK000005", "quantity": 1}]},
                {"aisleId": "2", "row": 3, "column": 2, "level": 2, "positions": [{"skuId": "1RAK000006", "quantity": 1}]},
            ],
            "aisleStatus": [
                {
                    "aisleId": "1",
                    "isAvailable": True,
                    "bank": "LEFT",
                    "exitCongestion": [
                        {"lineId": "LINE-1", "isCongested": False},
                        {"lineId": "LINE-2", "isCongested": False},
                        {"lineId": "LINE-3", "isCongested": False},
                    ],
                },
                {
                    "aisleId": "2",
                    "isAvailable": True,
                    "bank": "RIGHT",
                    "exitCongestion": [
                        {"lineId": "LINE-1", "isCongested": False},
                        {"lineId": "LINE-2", "isCongested": False},
                        {"lineId": "LINE-3", "isCongested": False},
                    ],
                },
            ],
            "tasks": [],
        },
        "assert_assigned_task_count": 0,
        "wait_after": 1,
    },
    {
        "name": "S8.2 schedule first partial batch",
        "method": "POST",
        "endpoint": "/schedule/mixed",
        "data": {
            "currentTime": "2026-01-21 11:01:00",
            "currentGroups": BASE_CURRENT_GROUPS,
            "inventory": [],
            "aisleStatus": [
                {
                    "aisleId": "1",
                    "isAvailable": True,
                    "bank": "LEFT",
                    "exitCongestion": [
                        {"lineId": "LINE-1", "isCongested": False},
                        {"lineId": "LINE-2", "isCongested": False},
                        {"lineId": "LINE-3", "isCongested": False},
                    ],
                },
                {
                    "aisleId": "2",
                    "isAvailable": True,
                    "bank": "RIGHT",
                    "exitCongestion": [
                        {"lineId": "LINE-1", "isCongested": False},
                        {"lineId": "LINE-2", "isCongested": False},
                        {"lineId": "LINE-3", "isCongested": False},
                    ],
                },
            ],
            "tasks": [
                {
                    "taskId": "PARTIAL_BATCH1_LINE1",
                    "taskType": "OUTBOUND",
                    "planId": "PLAN-LINE1-20260121",
                    "planIndex": 1,
                    "inLine": "L1C17",
                    "outLine": "L1C17",
                    "skus": [{"skuId": "1RAT000001", "quantity": 1}],
                },
                {
                    "taskId": "PARTIAL_BATCH1_LINE2",
                    "taskType": "OUTBOUND",
                    "planId": "PLAN-LINE2-20260121",
                    "planIndex": 1,
                    "inLine": "L2C17",
                    "outLine": "L2C17",
                    "skus": [{"skuId": "1RAK000005", "quantity": 1}],
                },
            ],
        },
        "save_response_key": "partial_batch_1",
        "assert_assigned_task_ids": ["PARTIAL_BATCH1_LINE1", "PARTIAL_BATCH1_LINE2"],
        "wait_after": 1,
    },
    {
        "name": "S8.3 schedule second partial batch before confirm should keep first batch frozen",
        "method": "POST",
        "endpoint": "/schedule/mixed",
        "data": {
            "currentTime": "2026-01-21 11:02:00",
            "currentGroups": BASE_CURRENT_GROUPS,
            "inventory": [],
            "aisleStatus": [
                {
                    "aisleId": "1",
                    "isAvailable": True,
                    "bank": "LEFT",
                    "exitCongestion": [
                        {"lineId": "LINE-1", "isCongested": False},
                        {"lineId": "LINE-2", "isCongested": False},
                        {"lineId": "LINE-3", "isCongested": False},
                    ],
                },
                {
                    "aisleId": "2",
                    "isAvailable": True,
                    "bank": "RIGHT",
                    "exitCongestion": [
                        {"lineId": "LINE-1", "isCongested": False},
                        {"lineId": "LINE-2", "isCongested": False},
                        {"lineId": "LINE-3", "isCongested": False},
                    ],
                },
            ],
            "tasks": [
                {
                    "taskId": "PARTIAL_BATCH2_LINE1",
                    "taskType": "OUTBOUND",
                    "planId": "PLAN-LINE1-20260121",
                    "planIndex": 2,
                    "inLine": "L1C17",
                    "outLine": "L1C1",
                    "skus": [{"skuId": "1RAT000002", "quantity": 1}],
                },
                {
                    "taskId": "PARTIAL_BATCH2_LINE2",
                    "taskType": "OUTBOUND",
                    "planId": "PLAN-LINE2-20260121",
                    "planIndex": 2,
                    "inLine": "L2C17",
                    "outLine": "L2C1",
                    "skus": [{"skuId": "1RAK000006", "quantity": 1}],
                },
            ],
        },
        "assert_assigned_task_ids": ["PARTIAL_BATCH1_LINE1", "PARTIAL_BATCH1_LINE2"],
        "wait_after": 1,
    },
    {
        "name": "S8.4 pending should contain first partial batch",
        "method": "GET",
        "endpoint": "/task/pending",
        "assert_pending_task_ids": ["PARTIAL_BATCH1_LINE1", "PARTIAL_BATCH1_LINE2"],
        "wait_after": 1,
    },
    {
        "name": "S8.5 confirm first partial batch",
        "method": "MULTI_POST",
        "endpoint": "/task/feedback",
        "data_template": {"status": "EXECUTING", "startTime": "2026-01-21T11:03:00Z"},
        "assigned_source_key": "partial_batch_1",
        "wait_after": 1,
    },
    {
        "name": "S8.6 schedule second partial batch after confirm should succeed but keep first batch dispatched",
        "method": "POST",
        "endpoint": "/schedule/mixed",
        "data": {
            "currentTime": "2026-01-21 11:04:00",
            "currentGroups": BASE_CURRENT_GROUPS,
            "inventory": [],
            "aisleStatus": [
                {
                    "aisleId": "1",
                    "isAvailable": True,
                    "bank": "LEFT",
                    "exitCongestion": [
                        {"lineId": "LINE-1", "isCongested": False},
                        {"lineId": "LINE-2", "isCongested": False},
                        {"lineId": "LINE-3", "isCongested": False},
                    ],
                },
                {
                    "aisleId": "2",
                    "isAvailable": True,
                    "bank": "RIGHT",
                    "exitCongestion": [
                        {"lineId": "LINE-1", "isCongested": False},
                        {"lineId": "LINE-2", "isCongested": False},
                        {"lineId": "LINE-3", "isCongested": False},
                    ],
                },
            ],
            "tasks": [
                {
                    "taskId": "PARTIAL_BATCH2_LINE1",
                    "taskType": "OUTBOUND",
                    "planId": "PLAN-LINE1-20260121",
                    "planIndex": 2,
                    "inLine": "L1C17",
                    "outLine": "L1C1",
                    "skus": [{"skuId": "1RAT000002", "quantity": 1}],
                },
                {
                    "taskId": "PARTIAL_BATCH2_LINE2",
                    "taskType": "OUTBOUND",
                    "planId": "PLAN-LINE2-20260121",
                    "planIndex": 2,
                    "inLine": "L2C17",
                    "outLine": "L2C1",
                    "skus": [{"skuId": "1RAK000006", "quantity": 1}],
                },
            ],
        },
        "assert_assigned_task_ids": ["PARTIAL_BATCH1_LINE1", "PARTIAL_BATCH1_LINE2"],
        "wait_after": 1,
    },
    {
        "name": "S8.7 pending should still contain first partial batch only",
        "method": "GET",
        "endpoint": "/task/pending",
        "assert_pending_task_ids": ["PARTIAL_BATCH1_LINE1", "PARTIAL_BATCH1_LINE2"],
        "assert_pending_task_count": 2,
        "wait_after": 1,
    },
    {
        "name": "S8.8 complete first partial batch",
        "method": "MULTI_POST",
        "endpoint": "/task/feedback",
        "data_template": {"status": "COMPLETED", "startTime": "2026-01-21T11:05:00Z"},
        "assigned_source_key": "partial_batch_1",
        "wait_after": 1,
    },
    {
        "name": "S8.9 empty task schedule should dispatch queued second batch",
        "method": "POST",
        "endpoint": "/schedule/mixed",
        "data": {
            "currentTime": "2026-01-21 11:07:00",
            "currentGroups": BASE_CURRENT_GROUPS,
            "inventory": [],
            "aisleStatus": [
                {
                    "aisleId": "1",
                    "isAvailable": True,
                    "bank": "LEFT",
                    "exitCongestion": [
                        {"lineId": "LINE-1", "isCongested": False},
                        {"lineId": "LINE-2", "isCongested": False},
                        {"lineId": "LINE-3", "isCongested": False},
                    ],
                },
                {
                    "aisleId": "2",
                    "isAvailable": True,
                    "bank": "RIGHT",
                    "exitCongestion": [
                        {"lineId": "LINE-1", "isCongested": False},
                        {"lineId": "LINE-2", "isCongested": False},
                        {"lineId": "LINE-3", "isCongested": False},
                    ],
                },
            ],
            "tasks": [],
        },
        "save_response_key": "partial_batch_2",
        "assert_assigned_task_ids": ["PARTIAL_BATCH2_LINE1", "PARTIAL_BATCH2_LINE2"],
        "wait_after": 1,
    },
    {
        "name": "S8.10 pending after empty task schedule should keep second batch only",
        "method": "GET",
        "endpoint": "/task/pending",
        "assert_pending_task_ids": ["PARTIAL_BATCH2_LINE1", "PARTIAL_BATCH2_LINE2"],
        "assert_pending_task_count": 2,
        "wait_after": 1,
    },
    {
        "name": "S8.11 confirm second partial batch",
        "method": "MULTI_POST",
        "endpoint": "/task/feedback",
        "data_template": {"status": "EXECUTING", "startTime": "2026-01-21T11:08:00Z"},
        "assigned_source_key": "partial_batch_2",
        "wait_after": 1,
    },
    {
        "name": "S8.12 complete second partial batch",
        "method": "MULTI_POST",
        "endpoint": "/task/feedback",
        "data_template": {"status": "COMPLETED", "startTime": "2026-01-21T11:09:00Z"},
        "assigned_source_key": "partial_batch_2",
        "wait_after": 1,
    },
    {
        "name": "S9 cleanup pending before appended-plan test",
        "method": "CLEAN_PENDING",
        "wait_after": 1,
    },
    {
        "name": "S9.1 reset base plan before append checks",
        "method": "POST",
        "endpoint": "/plan/production",
        "data": {
            "operationType": "UPDATE",
            "planDate": "2026-01-21 09:00:00",
            "plans": BASE_PRODUCTION_PLAN["plans"],
        },
        "wait_after": 1,
    },
    {
        "name": "S9.2 current plan should match base plan",
        "method": "GET",
        "endpoint": "/plan/production",
        "assert_plan_group_counts": {"1": 6, "2": 2},
        "assert_plan_boundary_skus": {
            "1": {"first": "1RAT000001", "last": "1RAT000006"},
            "2": {"first": "1RAK000005", "last": "1RAK000006"},
        },
        "wait_after": 1,
    },
    {
        "name": "S9.3 append extra production plan batch 1",
        "method": "POST",
        "endpoint": "/plan/production",
        "data": APPEND_PRODUCTION_PLAN_1,
        "wait_after": 1,
    },
    {
        "name": "S9.4 current plan should keep old groups and append batch 1",
        "method": "GET",
        "endpoint": "/plan/production",
        "assert_plan_group_counts": {"1": 8, "2": 3},
        "assert_plan_boundary_skus": {
            "1": {"first": "1RAT000001", "last": "1RAT000012"},
            "2": {"first": "1RAK000005", "last": "1RAK000011"},
        },
        "wait_after": 1,
    },
    {
        "name": "S9.5 append extra production plan batch 2",
        "method": "POST",
        "endpoint": "/plan/production",
        "data": APPEND_PRODUCTION_PLAN_2,
        "wait_after": 1,
    },
    {
        "name": "S9.6 current plan should keep previous groups and append batch 2",
        "method": "GET",
        "endpoint": "/plan/production",
        "assert_plan_group_counts": {"1": 9, "2": 3},
        "assert_plan_boundary_skus": {
            "1": {"first": "1RAT000001", "last": "1RAT000013"},
            "2": {"first": "1RAK000005", "last": "1RAK000011"},
        },
        "wait_after": 1,
    },
]


class APITester:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.api_url = f"{self.base_url}{API_PREFIX}"
        self.session = requests.Session()
        self.saved_data: Dict[str, Any] = {}

    def cleanup_pending_tasks(self) -> None:
        pending_url = f"{self.api_url}/task/pending"
        feedback_url = f"{self.api_url}/task/feedback"
        print("\n" + "=" * 80)
        print("Preflight: cleanup pending tasks")
        print("=" * 80)

        try:
            resp = self.session.get(pending_url)
        except requests.exceptions.RequestException as e:
            print(f"Skip cleanup, cannot query pending tasks: {e}")
            return

        if resp.status_code >= 400:
            print(f"Skip cleanup, pending query failed: {resp.status_code}")
            return

        try:
            data = resp.json()
        except Exception:
            print("Skip cleanup, pending response is not JSON")
            return
        payload = data.get("data", data) if isinstance(data, dict) else {}
        tasks = payload.get("tasks", []) or []
        stale = [t for t in tasks if str(t.get("status", "")).upper() in {"PENDING", "CONFIRMED"}]
        if not stale:
            print("No stale pending tasks")
            return

        print(f"Found {len(stale)} stale tasks, mark as FAILED")
        now_utc = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        for t in stale:
            task_id = t.get("task_id")
            task_type = str(t.get("task_type", "OUTBOUND")).upper()
            payload = {
                "taskId": task_id,
                "taskType": "INBOUND" if "INBOUND" in task_type else "OUTBOUND",
                "status": "FAILED",
                "startTime": now_utc,
                "failureReason": "test preflight cleanup",
            }
            print(f"POST {feedback_url}: {json.dumps(payload, ensure_ascii=False)}")
            try:
                fb = self.session.post(feedback_url, json=payload)
                print(f"cleanup status={fb.status_code}")
            except requests.exceptions.RequestException as e:
                print(f"cleanup request error: {e}")

    def execute_scenario(self, scenario: Dict[str, Any]) -> bool:
        print("\n" + "=" * 80)
        print(f"Run: {scenario['name']}")
        print("=" * 80)

        method = scenario.get("method", "POST")

        if method == "CLEAN_PENDING":
            self.cleanup_pending_tasks()
            wait_after = int(scenario.get("wait_after", 0))
            if wait_after > 0:
                time.sleep(wait_after)
            return True

        url = f"{self.api_url}{scenario['endpoint']}"

        if method == "MULTI_POST":
            return self._execute_multi_post(scenario, url)

        data = scenario.get("data", {})
        print(f"Request: {method} {url}")
        if method != "GET":
            print(json.dumps(data, indent=2, ensure_ascii=False))

        try:
            if method == "GET":
                response = self.session.get(url)
            else:
                response = self.session.post(url, json=data)
        except requests.exceptions.RequestException as e:
            print(f"Request error: {e}")
            return False

        print(f"Status: {response.status_code}")
        expected = scenario.get("expected_status")
        if expected is not None:
            ok = response.status_code == expected
        else:
            ok = response.status_code < 400

        try:
            response_data = response.json()
            print(json.dumps(response_data, indent=2, ensure_ascii=False))
            save_key = scenario.get("save_response_key")
            if save_key:
                self.saved_data[save_key] = response_data.get("data", response_data)
            if ok:
                assigned_expect = scenario.get("assert_assigned_task_id")
                if assigned_expect:
                    payload = response_data.get("data", response_data) if isinstance(response_data, dict) else {}
                    aisle_assignments = payload.get("aisleAssignments", []) if isinstance(payload, dict) else []
                    assigned_ids = []
                    for row in aisle_assignments:
                        t = row.get("assignedTask") if isinstance(row, dict) else None
                        if isinstance(t, dict) and t.get("taskId"):
                            assigned_ids.append(str(t.get("taskId")))
                    if assigned_expect not in assigned_ids:
                        print(f"Assertion failed: expected assigned task '{assigned_expect}', got {assigned_ids}")
                        ok = False

                pending_first_expect = scenario.get("assert_first_pending_task_id")
                if pending_first_expect:
                    payload = response_data.get("data", response_data) if isinstance(response_data, dict) else {}
                    tasks = payload.get("tasks", []) if isinstance(payload, dict) else []
                    first_id = None
                    if tasks and isinstance(tasks[0], dict):
                        first_id = str(tasks[0].get("task_id") or "")
                    if first_id != pending_first_expect:
                        print(f"Assertion failed: expected first pending task '{pending_first_expect}', got '{first_id}'")
                        ok = False

                out_line_expect = scenario.get("assert_all_assigned_out_line")
                if out_line_expect:
                    task_prefix = str(scenario.get("assert_out_line_task_prefix", "") or "")
                    payload = response_data.get("data", response_data) if isinstance(response_data, dict) else {}
                    aisle_assignments = payload.get("aisleAssignments", []) if isinstance(payload, dict) else []
                    bad_rows = []
                    for row in aisle_assignments:
                        t = row.get("assignedTask") if isinstance(row, dict) else None
                        if not isinstance(t, dict):
                            continue
                        if str(t.get("taskType", "")).upper() != "OUTBOUND":
                            continue
                        task_id = str(t.get("taskId") or "")
                        if task_prefix and not task_id.startswith(task_prefix):
                            continue
                        got = str(t.get("outLine") or "")
                        if got != str(out_line_expect):
                            bad_rows.append({"aisleId": row.get("aisleId"), "taskId": task_id, "outLine": got})
                    if bad_rows:
                        print(f"Assertion failed: expected all assigned OUTBOUND outLine={out_line_expect}, got {bad_rows}")
                        ok = False

                no_assign_prefix = str(scenario.get("assert_no_assigned_task_prefix", "") or "")
                if no_assign_prefix:
                    payload = response_data.get("data", response_data) if isinstance(response_data, dict) else {}
                    aisle_assignments = payload.get("aisleAssignments", []) if isinstance(payload, dict) else []
                    hit_rows = []
                    for row in aisle_assignments:
                        t = row.get("assignedTask") if isinstance(row, dict) else None
                        if not isinstance(t, dict):
                            continue
                        task_id = str(t.get("taskId") or "")
                        if task_id.startswith(no_assign_prefix):
                            hit_rows.append({"aisleId": row.get("aisleId"), "taskId": task_id})
                    if hit_rows:
                        print(f"Assertion failed: expected no assigned task with prefix '{no_assign_prefix}', got {hit_rows}")
                        ok = False

                assigned_expect_ids = scenario.get("assert_assigned_task_ids")
                if assigned_expect_ids is not None:
                    payload = response_data.get("data", response_data) if isinstance(response_data, dict) else {}
                    aisle_assignments = payload.get("aisleAssignments", []) if isinstance(payload, dict) else []
                    assigned_ids = []
                    for row in aisle_assignments:
                        t = row.get("assignedTask") if isinstance(row, dict) else None
                        if isinstance(t, dict) and t.get("taskId"):
                            assigned_ids.append(str(t.get("taskId")))
                    expected_ids = [str(x) for x in assigned_expect_ids]
                    if sorted(assigned_ids) != sorted(expected_ids):
                        print(f"Assertion failed: expected assigned task ids {expected_ids}, got {assigned_ids}")
                        ok = False

                assigned_expect_count = scenario.get("assert_assigned_task_count")
                if assigned_expect_count is not None:
                    payload = response_data.get("data", response_data) if isinstance(response_data, dict) else {}
                    aisle_assignments = payload.get("aisleAssignments", []) if isinstance(payload, dict) else []
                    assigned_count = 0
                    for row in aisle_assignments:
                        t = row.get("assignedTask") if isinstance(row, dict) else None
                        if isinstance(t, dict) and t.get("taskId"):
                            assigned_count += 1
                    if int(assigned_count) != int(assigned_expect_count):
                        print(f"Assertion failed: expected assigned task count {assigned_expect_count}, got {assigned_count}")
                        ok = False

                pending_expect_ids = scenario.get("assert_pending_task_ids")
                if pending_expect_ids is not None:
                    payload = response_data.get("data", response_data) if isinstance(response_data, dict) else {}
                    tasks = payload.get("tasks", []) if isinstance(payload, dict) else []
                    pending_ids = []
                    for row in tasks:
                        if isinstance(row, dict) and row.get("task_id"):
                            pending_ids.append(str(row.get("task_id")))
                    expected_ids = [str(x) for x in pending_expect_ids]
                    if sorted(pending_ids) != sorted(expected_ids):
                        print(f"Assertion failed: expected pending task ids {expected_ids}, got {pending_ids}")
                        ok = False

                pending_expect_count = scenario.get("assert_pending_task_count")
                if pending_expect_count is not None:
                    payload = response_data.get("data", response_data) if isinstance(response_data, dict) else {}
                    tasks = payload.get("tasks", []) if isinstance(payload, dict) else []
                    pending_count = len(tasks)
                    if int(pending_count) != int(pending_expect_count):
                        print(f"Assertion failed: expected pending task count {pending_expect_count}, got {pending_count}")
                        ok = False

                plan_group_counts = scenario.get("assert_plan_group_counts")
                if plan_group_counts is not None:
                    payload = response_data.get("data", response_data) if isinstance(response_data, dict) else {}
                    production_plan = payload.get("production_plan", {}) if isinstance(payload, dict) else {}
                    actual_counts: Dict[str, int] = {}
                    for line_id, expected_count in plan_group_counts.items():
                        groups = None
                        if isinstance(production_plan, dict):
                            groups = production_plan.get(line_id)
                            if groups is None:
                                try:
                                    groups = production_plan.get(int(line_id))
                                except Exception:
                                    groups = None
                        actual_counts[str(line_id)] = len(groups) if isinstance(groups, list) else 0
                        if actual_counts[str(line_id)] != int(expected_count):
                            print(
                                f"Assertion failed: expected plan group count line {line_id} = {expected_count}, "
                                f"got {actual_counts[str(line_id)]}"
                            )
                            ok = False

                plan_boundary_skus = scenario.get("assert_plan_boundary_skus")
                if plan_boundary_skus is not None:
                    for line_id, expected in plan_boundary_skus.items():
                        first_expected = str(expected.get("first") or "")
                        last_expected = str(expected.get("last") or "")
                        first_actual = extract_plan_group_sku(response_data, str(line_id), 0)
                        last_index = max(0, int(scenario["assert_plan_group_counts"][line_id]) - 1)
                        last_actual = extract_plan_group_sku(response_data, str(line_id), last_index)
                        if first_actual != first_expected or last_actual != last_expected:
                            print(
                                f"Assertion failed: expected plan boundary skus line {line_id} "
                                f"first={first_expected}, last={last_expected}; got first={first_actual}, last={last_actual}"
                            )
                            ok = False
        except Exception:
            print(response.text)

        if not ok:
            print("Scenario failed")
            return False

        wait_after = int(scenario.get("wait_after", 0))
        if wait_after > 0:
            time.sleep(wait_after)
        return True

    def _execute_multi_post(self, scenario: Dict[str, Any], url: str) -> bool:
        assigned_key = str(scenario.get("assigned_source_key") or "assigned_tasks")
        assigned = self.saved_data.get(assigned_key, {})
        task_items = []
        for row in assigned.get("aisleAssignments", []):
            t = row.get("assignedTask")
            if t and t.get("taskId"):
                task_items.append({"taskId": t.get("taskId"), "taskType": t.get("taskType", "OUTBOUND")})

        if not task_items:
            print("No assigned tasks found; skip")
            return True

        template = scenario.get("data_template", {})
        success = 0
        for item in task_items:
            payload = {
                "taskId": item["taskId"],
                "taskType": item["taskType"],
                **template,
            }
            print(f"POST {url}: {json.dumps(payload, ensure_ascii=False)}")
            try:
                resp = self.session.post(url, json=payload)
            except requests.exceptions.RequestException as e:
                print(f"feedback error: {e}")
                continue

            print(f"feedback status={resp.status_code}")
            if resp.status_code < 400:
                success += 1
            time.sleep(0.3)

        wait_after = int(scenario.get("wait_after", 0))
        if wait_after > 0:
            time.sleep(wait_after)
        return success > 0

    def run_all(self, scenarios: List[Dict[str, Any]]) -> bool:
        total = len(scenarios)
        print(f"Base URL: {self.api_url}")
        print(f"Scenarios: {total}")
        self.cleanup_pending_tasks()

        ok_count = 0
        failed: List[str] = []
        for scenario in scenarios:
            if self.execute_scenario(scenario):
                ok_count += 1
            else:
                failed.append(scenario["name"])

        print("\n" + "=" * 80)
        print("Done")
        print(f"Success: {ok_count}/{total}")
        if failed:
            print("Failed:")
            for name in failed:
                print(f"  - {name}")
        return not failed


def build_scenarios() -> List[Dict[str, Any]]:
    cfg = load_config()
    match_fields = load_match_fields(cfg)
    outbound_feature_fields_by_line = load_outbound_feature_fields_by_line(cfg)

    scenarios = copy.deepcopy(SCENARIOS)
    for scenario in scenarios:
        if "data" in scenario:
            remove_shelf_fields(scenario["data"])
            apply_default_features_to_payload(scenario["data"], DEFAULT_TEST_FEATURES)
            apply_sku_attrs_to_payload(scenario["data"], match_fields)
            if scenario.get("endpoint") == "/plan/production":
                apply_plan_line_feature_fields(scenario["data"], outbound_feature_fields_by_line)
            if isinstance(scenario["data"].get("productionPlan"), dict):
                apply_plan_line_feature_fields(scenario["data"]["productionPlan"], outbound_feature_fields_by_line)
    return scenarios


def main() -> int:
    parser = argparse.ArgumentParser(description="Warehouse API flow test")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"default: {DEFAULT_BASE_URL}")
    parser.add_argument("--log-file", default="", help="log file path; default auto-generate under logs/")
    args = parser.parse_args()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = Path(args.log_file) if args.log_file else LOG_DIR / f"test_api_flow_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    log_fp = open(log_path, "w", encoding="utf-8")
    sys.stdout = Tee(sys.stdout, log_fp)
    sys.stderr = Tee(sys.stderr, log_fp)
    print(f"Log file: {log_path}")

    tester = APITester(args.base_url)
    scenarios = build_scenarios()
    success = tester.run_all(scenarios)
    log_fp.close()
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())


