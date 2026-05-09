import socket
import subprocess
import sys
import time
import unittest

import requests


SKU_ATTRS = {
    "version": "00",
    "productionAttribute": "D",
    "militaryCivilianMark": "M",
    "salesArea": "N",
}


def sku(sku_id: str, quantity: int = 1, **extra) -> dict:
    return {"skuId": sku_id, "quantity": quantity, **SKU_ATTRS, **extra}


PAIR_1 = "2801022-TG152"
PAIR_2 = "2801038-TG152"


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    _host, port = sock.getsockname()
    sock.close()
    return int(port)


class FailedFeedbackUnblocksHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = _free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"

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
        )

        deadline = time.time() + 15.0
        last_err = None
        while time.time() < deadline:
            try:
                r = requests.get(f"{cls.base}/", timeout=0.5)
                if r.status_code == 200:
                    return
            except Exception as e:
                last_err = e
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

    def post(self, path: str, payload: dict, expected_status: int = 200) -> dict:
        r = requests.post(f"{self.base}{path}", json=payload, timeout=5.0)
        self.assertEqual(r.status_code, expected_status, r.text)
        return r.json()

    def get(self, path: str, expected_status: int = 200) -> dict:
        r = requests.get(f"{self.base}{path}", timeout=5.0)
        self.assertEqual(r.status_code, expected_status, r.text)
        return r.json()

    def test_failed_feedback_clears_unconfirmed_and_allows_new_schedule(self):
        aisle_status_all = [
            {"aisleId": str(i), "isAvailable": True, "bank": "LEFT" if i % 2 else "RIGHT", "exitCongestion": []}
            for i in range(1, 6)
        ]

        task_id = "INBOUND_FAIL_HTTP_01"
        scheduled = self.post(
            "/api/v1/schedule/mixed",
            {
                "currentTime": "2026-04-23 11:00:00",
                "inventory": [],
                "aisleStatus": aisle_status_all,
                "tasks": [
                    {"taskId": task_id, "taskType": "INBOUND", "targetAisle": "2", "skus": [sku(PAIR_1), sku(PAIR_2)]}
                ],
            },
            expected_status=200,
        )
        self.assertEqual(scheduled["status"], "SUCCESS")

        # Before feedback, unconfirmed gate should block.
        blocked = requests.post(
            f"{self.base}/api/v1/schedule/mixed",
            json={
                "currentTime": "2026-04-23 11:00:10",
                "inventory": [],
                "aisleStatus": aisle_status_all,
                "tasks": [
                    {"taskId": "INBOUND_SHOULD_BLOCK_HTTP_01", "taskType": "INBOUND", "targetAisle": "2", "skus": [sku(PAIR_1), sku(PAIR_2)]}
                ],
            },
            timeout=5.0,
        )
        self.assertEqual(blocked.status_code, 409, blocked.text)

        # FAILED feedback should clear pending/unconfirmed state.
        feedback = self.post(
            "/api/v1/task/feedback",
            {
                "taskId": task_id,
                "taskType": "INBOUND",
                "status": "FAILED",
                "startTime": "2026-04-23 11:00:20",
                "failureReason": "device_error",
            },
            expected_status=200,
        )
        self.assertEqual(feedback["status"], "SUCCESS")

        unconfirmed = self.get("/api/v1/task/unconfirmed")
        self.assertEqual(unconfirmed["data"]["count"], 0)
        self.assertTrue(unconfirmed["data"]["can_accept_new_task"])

        # Now scheduling should be accepted again.
        scheduled2 = self.post(
            "/api/v1/schedule/mixed",
            {
                "currentTime": "2026-04-23 11:01:00",
                "inventory": [],
                "aisleStatus": aisle_status_all,
                "tasks": [
                    {"taskId": "INBOUND_OK_AFTER_FAIL_HTTP_01", "taskType": "INBOUND", "targetAisle": "2", "skus": [sku(PAIR_1), sku(PAIR_2)]}
                ],
            },
            expected_status=200,
        )
        self.assertEqual(scheduled2["status"], "SUCCESS")


if __name__ == "__main__":
    unittest.main()

