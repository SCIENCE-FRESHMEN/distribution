import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import unittest

import requests

from api.models import MixedScheduleRequest
from api.routes.schedule import mixed_schedule
from api.services.warehouse_service import (
    get_warehouse_service,
    reset_warehouse_service,
)
from api.state import get_task_state_manager, reset_task_state_manager


SKU_ATTRS = {
    "version": "00",
    "productionAttribute": "D",
    "militaryCivilianMark": "M",
    "salesArea": "N",
}

PAIR_1 = "2801022-TG152"
PAIR_2 = "2801038-TG152"
PAIR_3 = "2801021-TG152"
PAIR_4 = "2801037-TG152"


def sku(sku_id: str, quantity: int = 1) -> dict:
    return {"skuId": sku_id, "quantity": quantity, **SKU_ATTRS}


def aisle_status() -> list[dict]:
    return [
        {
            "aisleId": str(i),
            "isAvailable": True,
            "bank": "LEFT" if i % 2 else "RIGHT",
            "exitCongestion": [{"lineId": "LINE-1", "isCongested": False}],
        }
        for i in range(1, 6)
    ]


def two_group_plan() -> dict:
    return {
        "operationType": "UPDATE",
        "planDate": "2026-04-23 08:00:00",
        "plans": [
            {
                "planId": "PLAN-LINE1-INLINE",
                "lineId": "LINE-1",
                "planIndex": [
                    {"requiredSkus": [[sku(PAIR_1), sku(PAIR_2)]]},
                    {"requiredSkus": [[sku(PAIR_3), sku(PAIR_4)]]},
                ],
            }
        ],
    }


def inventory_for_two_groups() -> list[dict]:
    return [
        {"aisleId": "1", "row": 1, "column": 1, "level": 1, "shelf": "UPPER", "positions": [sku(PAIR_1)]},
        {"aisleId": "1", "row": 1, "column": 1, "level": 1, "shelf": "LOWER", "positions": [sku(PAIR_2)]},
        {"aisleId": "1", "row": 1, "column": 2, "level": 1, "shelf": "UPPER", "positions": [sku(PAIR_3)]},
        {"aisleId": "1", "row": 1, "column": 2, "level": 1, "shelf": "LOWER", "positions": [sku(PAIR_4)]},
    ]


def mixed_payload(*, current_group: int, tasks: list[dict], inventory: list[dict] | None = None) -> dict:
    return {
        "currentTime": "2026-04-23 08:30:00",
        "productionPlan": two_group_plan(),
        "productionLineCurrentGroup": {"LINE-1": current_group - 1},
        "inventory": inventory if inventory is not None else [],
        "aisleStatus": aisle_status(),
        "tasks": tasks,
    }


def mixed_payload_public_current_groups(
    *, current_group: int, tasks: list[dict], inventory: list[dict] | None = None
) -> dict:
    return {
        "currentTime": "2026-04-23 08:30:00",
        "productionPlan": two_group_plan(),
        "currentGroups": {"LINE-1": current_group},
        "inventory": inventory if inventory is not None else [],
        "aisleStatus": aisle_status(),
        "tasks": tasks,
    }


def assigned_task_ids(body: dict) -> list[str]:
    ids = []
    for item in body["data"]["aisleAssignments"]:
        task = item.get("assignedTask")
        if task:
            ids.append(task["taskId"])
    return ids


class InlinePlanMixedScheduleApiTests(unittest.TestCase):
    def setUp(self):
        reset_warehouse_service()
        reset_task_state_manager()
        self.warehouse_service = get_warehouse_service()
        self.task_manager = get_task_state_manager()

    def tearDown(self):
        reset_warehouse_service()
        reset_task_state_manager()

    def run_async(self, coro):
        return asyncio.run(coro)

    def decode_response(self, response) -> dict:
        if hasattr(response, "body"):
            return json.loads(response.body.decode("utf-8"))
        return response.model_dump()

    def post_mixed(self, payload: dict, expected_status: int = 200) -> dict:
        response = self.run_async(
            mixed_schedule(
                MixedScheduleRequest(**payload),
                warehouse_service=self.warehouse_service,
                task_manager=self.task_manager,
            )
        )
        status_code = getattr(response, "status_code", 200)
        self.assertEqual(status_code, expected_status, self.decode_response(response))
        return self.decode_response(response)

    def test_mixed_schedule_updates_core_plan_from_inline_production_plan(self):
        body = self.post_mixed(mixed_payload(current_group=2, tasks=[]))

        self.assertEqual(body["status"], "SUCCESS")
        core = self.warehouse_service.core
        self.assertEqual(core.production_plan[1], [[[PAIR_1, PAIR_2]], [[PAIR_3, PAIR_4]]])
        self.assertEqual(core.production_line_current_group[1], 1)

    def test_future_group_is_not_dispatched_when_inline_current_group_has_not_advanced(self):
        gp1_id = f"OUTBOUND_PL1_GP1_{PAIR_1}_{PAIR_2}"
        gp2_id = f"OUTBOUND_PL1_GP2_{PAIR_3}_{PAIR_4}"
        body = self.post_mixed(
            mixed_payload(
                current_group=1,
                inventory=inventory_for_two_groups(),
                tasks=[
                    {"taskId": gp2_id, "taskType": "OUTBOUND", "planId": "PLAN-LINE1-INLINE", "planIndex": 2, "skus": [sku(PAIR_3), sku(PAIR_4)]},
                    {"taskId": gp1_id, "taskType": "OUTBOUND", "planId": "PLAN-LINE1-INLINE", "planIndex": 1, "skus": [sku(PAIR_1), sku(PAIR_2)]},
                ],
            )
        )

        ids = assigned_task_ids(body)
        self.assertIn(gp1_id, ids)
        self.assertNotIn(gp2_id, ids)

    def test_public_current_groups_one_dispatches_group_one_and_blocks_group_two(self):
        gp1_id = f"OUTBOUND_PL1_GP1_{PAIR_1}_{PAIR_2}"
        gp2_id = f"OUTBOUND_PL1_GP2_{PAIR_3}_{PAIR_4}"
        body = self.post_mixed(
            mixed_payload_public_current_groups(
                current_group=1,
                inventory=inventory_for_two_groups(),
                tasks=[
                    {"taskId": gp2_id, "taskType": "OUTBOUND", "planId": "PLAN-LINE1-INLINE", "planIndex": 2, "skus": [sku(PAIR_3), sku(PAIR_4)]},
                    {"taskId": gp1_id, "taskType": "OUTBOUND", "planId": "PLAN-LINE1-INLINE", "planIndex": 1, "skus": [sku(PAIR_1), sku(PAIR_2)]},
                ],
            )
        )

        ids = assigned_task_ids(body)
        self.assertIn(gp1_id, ids)
        self.assertNotIn(gp2_id, ids)

    def test_public_current_groups_two_dispatches_group_two(self):
        gp2_id = f"OUTBOUND_PL1_GP2_{PAIR_3}_{PAIR_4}"
        body = self.post_mixed(
            mixed_payload_public_current_groups(
                current_group=2,
                inventory=inventory_for_two_groups(),
                tasks=[
                    {"taskId": gp2_id, "taskType": "OUTBOUND", "planId": "PLAN-LINE1-INLINE", "planIndex": 2, "skus": [sku(PAIR_3), sku(PAIR_4)]},
                ],
            )
        )

        self.assertIn(gp2_id, assigned_task_ids(body))

    def test_public_current_groups_list_missing_group_is_rejected_with_400(self):
        payload = mixed_payload_public_current_groups(
            current_group=1,
            inventory=inventory_for_two_groups(),
            tasks=[],
        )
        payload["currentGroups"] = [{"lineId": "LINE-1"}]

        body = self.post_mixed(payload, expected_status=400)

        self.assertEqual(body["status"], "FAILED")
        self.assertIn("currentGroups", body["message"])

    def test_future_group_is_dispatched_when_inline_current_group_advances(self):
        gp2_id = f"OUTBOUND_PL1_GP2_{PAIR_3}_{PAIR_4}"
        body = self.post_mixed(
            mixed_payload(
                current_group=2,
                inventory=inventory_for_two_groups(),
                tasks=[
                    {"taskId": gp2_id, "taskType": "OUTBOUND", "planId": "PLAN-LINE1-INLINE", "planIndex": 2, "skus": [sku(PAIR_3), sku(PAIR_4)]},
                ],
            )
        )

        self.assertIn(gp2_id, assigned_task_ids(body))


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    _host, port = sock.getsockname()
    sock.close()
    return int(port)


class RemovedPlanApiHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = _free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        env = os.environ.copy()
        env["WMS_ENABLE_DEBUG_RESET"] = "1"
        cls.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "api.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(cls.port),
                "--log-level",
                "warning",
            ],
            cwd=".",
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        deadline = time.time() + 15.0
        last_err = None
        while time.time() < deadline:
            try:
                response = requests.get(f"{cls.base}/", timeout=0.5)
                if response.status_code == 200:
                    return
            except Exception as exc:
                last_err = exc
            time.sleep(0.2)
        raise RuntimeError(f"uvicorn did not start on port {cls.port}: {last_err!r}")

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "proc", None) is not None:
            cls.proc.terminate()
            try:
                cls.proc.wait(timeout=10)
            except Exception:
                cls.proc.kill()

    def setUp(self):
        response = requests.post(f"{self.base}/api/v1/debug/reset", timeout=5.0)
        self.assertEqual(response.status_code, 200, response.text)

    def test_plan_production_api_is_removed_for_all_public_versions(self):
        for prefix in ("/api/v1", "/api/v2", "/api/v3"):
            with self.subTest(prefix=prefix, method="GET"):
                response = requests.get(f"{self.base}{prefix}/plan/production", timeout=5.0)
                self.assertEqual(response.status_code, 404, response.text)
            with self.subTest(prefix=prefix, method="POST"):
                response = requests.post(f"{self.base}{prefix}/plan/production", json=two_group_plan(), timeout=5.0)
                self.assertEqual(response.status_code, 404, response.text)

    def test_openapi_excludes_removed_plan_api_and_v2_v3_prefixes(self):
        response = requests.get(f"{self.base}/openapi.json", timeout=5.0)
        self.assertEqual(response.status_code, 200, response.text)
        paths = response.json()["paths"]

        self.assertNotIn("/api/v1/plan/production", paths)
        self.assertFalse(any(path.startswith("/api/v2") for path in paths), paths)
        self.assertFalse(any(path.startswith("/api/v3") for path in paths), paths)


if __name__ == "__main__":
    unittest.main()
