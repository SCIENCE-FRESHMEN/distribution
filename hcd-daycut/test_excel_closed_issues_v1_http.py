import socket
import subprocess
import sys
import time
import unittest
import os

import requests


SKU_ATTRS = {
    "version": "00",
    "productionAttribute": "D",
    "militaryCivilianMark": "M",
    "salesArea": "N",
}


def sku(sku_id: str, quantity: int = 1, **extra) -> dict:
    return {"skuId": sku_id, "quantity": quantity, **SKU_ATTRS, **extra}


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    _host, port = sock.getsockname()
    sock.close()
    return int(port)


class ExcelClosedIssuesV1HttpTests(unittest.TestCase):
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
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        deadline = time.time() + 15.0
        while time.time() < deadline:
            try:
                if requests.get(f"{cls.base}/", timeout=0.5).status_code == 200:
                    return
            except Exception:
                pass
            time.sleep(0.2)
        raise RuntimeError("uvicorn did not start")

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "proc", None) is not None:
            cls.proc.terminate()
            try:
                cls.proc.wait(timeout=10)
            except Exception:
                cls.proc.kill()

    def post(self, path: str, payload: dict, expected_status: int = 200) -> dict:
        r = requests.post(f"{self.base}{path}", json=payload, timeout=5.0)
        self.assertEqual(r.status_code, expected_status, r.text)
        return r.json()

    def setUp(self):
        # Reset core + task manager between tests (server is shared across tests in this class).
        requests.post(f"{self.base}/api/v1/debug/reset", json={}, timeout=5.0)

    def test_v1_inbound_allocate_unknown_bom_rejected(self):
        # Excel: 分配巷道接口 #3, now covered through /api/v1/inbound/allocate.
        body = self.post(
            "/api/v1/inbound/allocate",
            {"tasks": [{"taskId": "ALLOC_V1_UNKNOWN", "skus": [sku("NotInBOM_Sku", beamSide="LEFT")]}]},
            expected_status=400,
        )
        self.assertEqual(body["status"], "FAILED")
        self.assertIn("NotInBOM_Sku", body["data"]["invalidSkus"])

    def test_v1_schedule_mixed_unknown_bom_inbound_rejected(self):
        # Excel: 入库执行接口 #6, now covered through /api/v1/schedule/mixed.
        body = self.post(
            "/api/v1/schedule/mixed",
            {
                "currentTime": "2026-09-10 18:18:51",
                "inventory": [],
                "aisleStatus": [],
                "tasks": [
                    {
                        "taskId": "INBOUND_V1_UNKNOWN",
                        "taskType": "INBOUND",
                        "targetAisle": "3",
                        "skus": [sku("11111111111111112"), sku("22222222222222221")],
                    }
                ],
            },
            expected_status=400,
        )
        self.assertEqual(body["status"], "FAILED")
        self.assertIn("11111111111111112", body["data"]["invalidSkus"])

    def test_v1_schedule_mixed_single_beam_requires_explicit_beamside(self):
        # Excel: 入库执行接口 #7, now covered through /api/v1/schedule/mixed.
        rejected = self.post(
            "/api/v1/schedule/mixed",
            {
                "currentTime": "2026-09-10 18:18:51",
                "inventory": [],
                "aisleStatus": [],
                "tasks": [
                    {
                        "taskId": "INBOUND_V1_SINGLE_MISSING_SIDE",
                        "taskType": "INBOUND",
                        "targetAisle": "1",
                        "skus": [sku("2801022-TG152")],
                    }
                ],
            },
            expected_status=422,
        )
        self.assertEqual(rejected["status"], "FAILED")

        body = self.post(
            "/api/v1/schedule/mixed",
            {
                "currentTime": "2026-09-10 18:18:51",
                "inventory": [],
                "aisleStatus": [],
                "tasks": [
                    {
                        "taskId": "INBOUND_V1_SINGLE",
                        "taskType": "INBOUND",
                        "targetAisle": "1",
                        "skus": [sku("2801022-TG152", beamSide="LEFT")],
                    }
                ],
            },
            expected_status=200,
        )
        self.assertEqual(body["status"], "SUCCESS")
        aisle1 = next(x for x in body["data"]["aisleAssignments"] if x["aisleId"] == "1")
        assigned = aisle1["assignedTask"]
        self.assertEqual(assigned["taskId"], "INBOUND_V1_SINGLE")
        self.assertEqual({p["row"] for p in assigned["positions"]}, {1})

    def test_v1_schedule_mixed_executing_task_does_not_block_new_requests(self):
        # Excel: 入库执行接口 #5, now covered through /api/v1/schedule/mixed.
        # After task EXECUTING, it must not return to unconfirmed list and block scheduling (409).
        first = self.post(
            "/api/v1/schedule/mixed",
            {
                "currentTime": "2026-09-10 18:18:51",
                "inventory": [],
                "aisleStatus": [{"aisleId": "1", "isAvailable": True, "bank": "LEFT", "exitCongestion": []}],
                "tasks": [
                    {
                        "taskId": "INBOUND_V1_EXEC_1",
                        "taskType": "INBOUND",
                        "targetAisle": "1",
                        "skus": [sku("2801022-TG152"), sku("2801021-TG152")],
                    }
                ],
            },
        )
        aisle1 = next(x for x in first["data"]["aisleAssignments"] if x["aisleId"] == "1")
        self.assertEqual(aisle1["assignedTask"]["taskId"], "INBOUND_V1_EXEC_1")

        self.post(
            "/api/v1/task/feedback",
            {"taskId": "INBOUND_V1_EXEC_1", "taskType": "INBOUND", "status": "EXECUTING", "startTime": "2026-09-10 18:19:00", "failureReason": ""},
        )

        second = requests.post(
            f"{self.base}/api/v1/schedule/mixed",
            json={
                "currentTime": "2026-09-10 18:20:00",
                "inventory": [],
                "aisleStatus": [{"aisleId": "1", "isAvailable": True, "bank": "LEFT", "exitCongestion": []}],
                "tasks": [
                    {
                        "taskId": "INBOUND_V1_EXEC_2",
                        "taskType": "INBOUND",
                        "targetAisle": "1",
                        "skus": [sku("2801022-TG152"), sku("2801021-TG152")],
                    }
                ],
            },
            timeout=5.0,
        )
        self.assertNotEqual(second.status_code, 409, second.text)

    def test_v1_inbound_allocate_single_beam_requires_explicit_beamside(self):
        # Excel: 分配巷道接口 #2, now covered through /api/v1/inbound/allocate.
        rejected = self.post(
            "/api/v1/inbound/allocate",
            {"tasks": [{"taskId": "ALLOC_V1_SINGLE_MISSING_SIDE", "skus": [sku("2801022-TG150")]}]},
            expected_status=422,
        )
        self.assertEqual(rejected["status"], "FAILED")

        body = self.post(
            "/api/v1/inbound/allocate",
            {"tasks": [{"taskId": "ALLOC_V1_SINGLE", "skus": [sku("2801022-TG150", beamSide="LEFT")]}]},
            expected_status=200,
        )
        self.assertEqual(body["status"], "SUCCESS")
        self.assertEqual(body["data"]["assignments"][0]["taskId"], "ALLOC_V1_SINGLE")
        self.assertTrue(body["data"]["assignments"][0]["recommendedAisle"])

    def test_v2_v3_prefixes_are_not_exposed(self):
        for prefix in ("/api/v2", "/api/v3"):
            with self.subTest(prefix=prefix):
                response = requests.get(f"{self.base}{prefix}/inbound/allocate", timeout=5.0)
                self.assertEqual(response.status_code, 404, response.text)

                response = requests.post(f"{self.base}{prefix}/inbound/allocate", json={}, timeout=5.0)
                self.assertEqual(response.status_code, 404, response.text)

                response = requests.post(f"{self.base}{prefix}/schedule/mixed", json={}, timeout=5.0)
                self.assertEqual(response.status_code, 404, response.text)

    def test_openapi_documents_only_v1_api_paths(self):
        response = requests.get(f"{self.base}/openapi.json", timeout=5.0)
        self.assertEqual(response.status_code, 200, response.text)
        paths = response.json()["paths"]

        for prefix in ("/api/v2", "/api/v3"):
            self.assertFalse(any(path.startswith(prefix) for path in paths), paths)

        for path in (
            "/api/v1/schedule/mixed",
            "/api/v1/inbound/allocate",
            "/api/v1/task/feedback",
            "/api/v1/bom/update",
        ):
            self.assertIn(path, paths)
        self.assertNotIn("/api/v1/plan/production", paths)

    def test_v1_inbound_allocate_prefers_aisle_with_pair_space(self):
        # Excel: 分配巷道接口 #4 /api/v1/inbound/allocate
        # If aisle 4 already has mates with free space, allocator should recommend aisle 4 for pairing.
        self.post(
            "/api/v1/schedule/mixed",
            {
                "currentTime": "2026-04-23 12:00:00",
                "inventory": [
                    {"aisleId": "4", "row": 7, "column": 1, "level": 2, "shelf": "UPPER", "positions": [sku("2801022-TG152")]},
                    {"aisleId": "4", "row": 7, "column": 2, "level": 2, "shelf": "UPPER", "positions": [sku("2801021-TG152")]},
                ],
                "aisleStatus": [{"aisleId": "4", "isAvailable": True, "bank": "RIGHT", "exitCongestion": []}],
                "tasks": [],
            },
            expected_status=200,
        )

        allocated = self.post(
            "/api/v1/inbound/allocate",
            {"tasks": [{"taskId": "ALLOC_PAIR_TO_4", "skus": [sku("2801038-TG152"), sku("2801037-TG152")]}]},
            expected_status=200,
        )
        self.assertEqual(allocated["status"], "SUCCESS")
        self.assertEqual(allocated["data"]["assignments"][0]["recommendedAisle"], "4")


if __name__ == "__main__":
    unittest.main()
