import os
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path

import requests



SKU_ATTRS = {
    "version": "00",
    "productionAttribute": "D",
    "militaryCivilianMark": "M",
    "salesArea": "N",
}

PAIR_1 = "2801022-TG152"
PAIR_2 = "2801038-TG152"


def sku(sku_id: str, quantity: int = 1, **extra) -> dict:
    return {"skuId": sku_id, "quantity": quantity, **SKU_ATTRS, **extra}


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


def inline_plan(group_count: int, current_group: int) -> dict:
    return {
        "operationType": "UPDATE",
        "planDate": "2026-04-23 09:00:00",
        "plans": [
            {
                "planId": "PLAN-LINE1-100-BEAMS",
                "lineId": "LINE-1",
                "planIndex": [
                    {"requiredSkus": [[sku(PAIR_1), sku(PAIR_2)]]}
                    for _ in range(group_count)
                ],
            }
        ],
    }


def mixed_payload(*, plan_groups: int, current_group: int, tasks: list[dict], inventory: list[dict] | None = None) -> dict:
    return {
        "currentTime": "2026-04-23 09:00:00",
        "productionPlan": inline_plan(plan_groups, current_group),
        "productionLineCurrentGroup": {"LINE-1": current_group - 1},
        "inventory": inventory if inventory is not None else [],
        "aisleStatus": aisle_status(),
        "tasks": tasks,
    }


def assigned_task(body: dict, task_id: str) -> dict:
    for item in body["data"]["aisleAssignments"]:
        task = item.get("assignedTask")
        if task and task.get("taskId") == task_id:
            return task
    raise AssertionError(f"assigned task not found: {task_id}")


class ContinuousAsrs100BeamsTests(unittest.TestCase):
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
        self.api_calls = 0
        response = requests.post(f"{self.base}/api/v1/debug/reset", timeout=5.0)
        self.assertEqual(response.status_code, 200, response.text)

    def post(self, path: str, payload: dict, expected_status: int = 200) -> dict:
        self.api_calls += 1
        response = requests.post(f"{self.base}{path}", json=payload, timeout=5.0)
        self.assertEqual(response.status_code, expected_status, response.text)
        return response.json()

    def get(self, path: str, expected_status: int = 200) -> dict:
        self.api_calls += 1
        response = requests.get(f"{self.base}{path}", timeout=5.0)
        self.assertEqual(response.status_code, expected_status, response.text)
        return response.json()

    def feedback(self, task_id: str, task_type: str, status: str) -> None:
        body = self.post(
            "/api/v1/task/feedback",
            {
                "taskId": task_id,
                "taskType": task_type,
                "status": status,
                "startTime": "2026-04-23 09:01:00",
                "failureReason": "",
            },
        )
        self.assertEqual(body["status"], "SUCCESS")

    def test_continuous_asrs_processes_100_beams_and_writes_markdown_report(self):
        inbound_pairs = 25
        outbound_pairs = 25
        plan_groups = outbound_pairs
        started = time.perf_counter()
        inbound_assigned = 0
        outbound_assigned = 0

        self.post("/api/v1/schedule/mixed", mixed_payload(plan_groups=plan_groups, current_group=1, tasks=[]))

        for index in range(inbound_pairs):
            task_id = f"INBOUND_100_BEAMS_{index + 1:02d}"
            target_aisle = str((index % 5) + 1)
            body = self.post(
                "/api/v1/schedule/mixed",
                mixed_payload(
                    plan_groups=plan_groups,
                    current_group=1,
                    tasks=[
                        {
                            "taskId": task_id,
                            "taskType": "INBOUND",
                            "targetAisle": target_aisle,
                            "skus": [sku(PAIR_1), sku(PAIR_2)],
                        }
                    ],
                ),
            )
            assigned = assigned_task(body, task_id)
            self.assertEqual(assigned["taskType"], "INBOUND")
            self.assertEqual(len(assigned["positions"]), 2)
            inbound_assigned += len(assigned["positions"])
            self.feedback(task_id, "INBOUND", "EXECUTING")
            self.feedback(task_id, "INBOUND", "COMPLETED")

        for index in range(outbound_pairs):
            group_number = index + 1
            task_id = f"OUTBOUND_PL1_GP{group_number}_{PAIR_1}_{PAIR_2}"
            body = self.post(
                "/api/v1/schedule/mixed",
                mixed_payload(
                    plan_groups=plan_groups,
                    current_group=index + 1,
                    tasks=[
                        {
                            "taskId": task_id,
                            "taskType": "OUTBOUND",
                            "planId": "PLAN-LINE1-100-BEAMS",
                            "planIndex": group_number,
                            "skus": [sku(PAIR_1), sku(PAIR_2)],
                        }
                    ],
                ),
            )
            assigned = assigned_task(body, task_id)
            self.assertEqual(assigned["taskType"], "OUTBOUND")
            self.assertGreaterEqual(len(assigned["positions"]), 2)
            outbound_assigned += 2
            self.feedback(task_id, "OUTBOUND", "EXECUTING")
            self.feedback(task_id, "OUTBOUND", "COMPLETED")

        status = self.get("/api/v1/status")
        self.assertEqual(status["status"], "SUCCESS")
        processed_beams = inbound_assigned + outbound_assigned
        elapsed = time.perf_counter() - started
        self.assertGreaterEqual(processed_beams, 100)

        report = (
            "# Continuous ASRS 100 Beam Simulation\n\n"
            f"- Result: PASS\n"
            f"- Processed beams: {processed_beams}\n"
            f"- Inbound beams accepted: {inbound_assigned}\n"
            f"- Outbound beams dispatched: {outbound_assigned}\n"
            f"- API calls: {self.api_calls}\n"
            f"- Elapsed seconds: {elapsed:.3f}\n"
            f"- Final status: {status['data']['system_status']}\n"
        )
        Path("docs/continuous_asrs_100_beams_report.md").write_text(report, encoding="utf-8")


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    _host, port = sock.getsockname()
    sock.close()
    return int(port)


if __name__ == "__main__":
    unittest.main()
