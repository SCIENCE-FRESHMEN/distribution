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


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    _host, port = sock.getsockname()
    sock.close()
    return int(port)


class OutboundPrefersDispatchableAisleHttpTests(unittest.TestCase):
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
        while time.time() < deadline:
            try:
                r = requests.get(f"{cls.base}/", timeout=0.5)
                if r.status_code == 200:
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

    def test_outbound_should_choose_other_aisle_when_first_match_is_blocked(self):
        """
        Regression for starvation risk:
        If matching inventory exists in aisle 1 (blocked) and aisle 2 (available),
        the scheduler should dispatch from aisle 2 instead of binding the task to aisle 1 and returning null.
        """
        PAIR_A1 = "2801022-TG152"
        PAIR_A2 = "2801038-TG152"
        PAIR_B1 = "2801021-TG152"
        PAIR_B2 = "2801037-TG152"

        DUMMY_1 = "DUMMY-SKU-1"
        DUMMY_2 = "DUMMY-SKU-2"

        aisle_status = [
            {"aisleId": "1", "isAvailable": True, "bank": "LEFT", "exitCongestion": []},
            {"aisleId": "2", "isAvailable": True, "bank": "RIGHT", "exitCongestion": []},
        ]

        # seed dummy in aisle1 and complete an outbound to create temporary blockage on aisle1
        self.post(
            "/api/v1/schedule/mixed",
            {
                "currentTime": "2026-09-10 18:00:00",
                "inventory": [
                    {"aisleId": "1", "row": 1, "column": 2, "level": 2, "shelf": "UPPER", "positions": [sku(DUMMY_1)]},
                    {"aisleId": "1", "row": 1, "column": 2, "level": 2, "shelf": "LOWER", "positions": [sku(DUMMY_2)]},
                ],
                "aisleStatus": aisle_status,
                "tasks": [],
            },
        )

        dummy_task = "OUTBOUND_BLOCK_A1"
        sched_dummy = self.post(
            "/api/v1/schedule/mixed",
            {
                "currentTime": "2026-09-10 18:00:05",
                "inventory": [],
                "aisleStatus": aisle_status,
                "tasks": [
                    {
                        "taskId": dummy_task,
                        "taskType": "OUTBOUND",
                        "planId": "PLAN-LINE1-20260122",
                        "planIndex": 1,
                        "skus": [sku(DUMMY_1), sku(DUMMY_2)],
                    }
                ],
            },
        )
        a1 = next(x for x in sched_dummy["data"]["aisleAssignments"] if x["aisleId"] == "1")
        self.assertIsNotNone(a1["assignedTask"])
        self.assertEqual(a1["assignedTask"]["taskId"], dummy_task)

        self.post(
            "/api/v1/task/feedback",
            {"taskId": dummy_task, "taskType": "OUTBOUND", "status": "EXECUTING", "startTime": "2026-09-10 18:00:06", "failureReason": ""},
        )
        self.post(
            "/api/v1/task/feedback",
            {"taskId": dummy_task, "taskType": "OUTBOUND", "status": "COMPLETED", "startTime": "2026-09-10 18:00:06", "failureReason": ""},
        )

        # Now seed the same desired pair into BOTH aisle1 and aisle2.
        self.post(
            "/api/v1/schedule/mixed",
            {
                "currentTime": "2026-09-10 18:00:10",
                "inventory": [
                    # Make aisle 1 the "first match" by using the earliest physical coordinates.
                    {"aisleId": "1", "row": 1, "column": 2, "level": 2, "shelf": "UPPER", "positions": [sku(PAIR_A1)]},
                    {"aisleId": "1", "row": 1, "column": 2, "level": 2, "shelf": "LOWER", "positions": [sku(PAIR_A2)]},
                    # Aisle 2 also has matching inventory but at later coordinates.
                    {"aisleId": "2", "row": 2, "column": 2, "level": 3, "shelf": "UPPER", "positions": [sku(PAIR_A1)]},
                    {"aisleId": "2", "row": 2, "column": 2, "level": 3, "shelf": "LOWER", "positions": [sku(PAIR_A2)]},
                ],
                "aisleStatus": aisle_status,
                "tasks": [],
            },
        )

        outbound = "OUTBOUND_PREFER_A2"
        body = self.post(
            "/api/v1/schedule/mixed",
            {
                "currentTime": "2026-09-10 18:00:11",
                "inventory": [],
                "aisleStatus": aisle_status,
                "tasks": [
                    {
                        "taskId": outbound,
                        "taskType": "OUTBOUND",
                        "planId": "PLAN-LINE1-20260122",
                        "planIndex": 1,
                        "skus": [sku(PAIR_A1), sku(PAIR_A2)],
                    }
                ],
            },
        )

        # Expect dispatch to aisle 2 while aisle 1 is blocked.
        assigned_2 = next(x for x in body["data"]["aisleAssignments"] if x["aisleId"] == "2")["assignedTask"]
        self.assertIsNotNone(assigned_2, body)
        self.assertEqual(assigned_2["taskId"], outbound, body)

        # Also require positions to come from aisle 2's physical slot (not from the blocked aisle 1 match).
        # We seeded aisle 2 at (aisle=2,row=2,col=2,level=3) => external_row = 2*(2-1)+2 = 4.
        self.assertIsNotNone(assigned_2.get("positions"), body)
        rows = {p["row"] for p in assigned_2["positions"]}
        cols = {p["column"] for p in assigned_2["positions"]}
        lvls = {p["level"] for p in assigned_2["positions"]}
        self.assertEqual(rows, {4}, body)
        self.assertEqual(cols, {2}, body)
        self.assertEqual(lvls, {3}, body)


if __name__ == "__main__":
    unittest.main()
