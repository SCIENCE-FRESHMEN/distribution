import json
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
PAIR_3 = "2801021-TG152"
PAIR_4 = "2801037-TG152"


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    _host, port = sock.getsockname()
    sock.close()
    return int(port)


class AsrsWorkflowHttpSimulationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = _free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"

        # Start a real uvicorn server (this is closer to "normal warehouse operation" than direct function calls).
        # Keep logs quiet to avoid pipe buffering issues.
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

        # Wait until server is ready.
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

    def test_asrs_shift_end_to_end_10_cycles_and_group_gating(self):
        """
        End-to-end AS/RS simulation over HTTP:
        - Seed aisle status + deterministic inventory via schedule/mixed (tasks=[]).
        - Run 10 inbound cycles: dispatch -> verify payload -> 409 until EXECUTING -> COMPLETE.
        - Then validate outbound group gating: GP2 blocked until GP1 COMPLETED.
        """
        aisle_status_all = [
            {"aisleId": str(i), "isAvailable": True, "bank": "LEFT" if i % 2 else "RIGHT", "exitCongestion": []}
            for i in range(1, 6)
        ]

        # Initial sync: just aisle status, no inventory mutation yet.
        self.post(
            "/api/v1/schedule/mixed",
            {"currentTime": "2026-09-10 18:18:51", "inventory": [], "aisleStatus": aisle_status_all, "tasks": []},
            expected_status=200,
        )

        # ---- 10 inbound cycles ----
        for cycle in range(1, 11):
            # Avoid aisle 1 so we can reserve it for deterministic outbound gating later.
            aisle = str(((cycle - 1) % 4) + 2)  # 2..5
            task_id = f"INBOUND_CYCLE_HTTP_{cycle:02d}"

            scheduled = self.post(
                "/api/v1/schedule/mixed",
                {
                    "currentTime": "2026-09-10 18:18:51",
                    "inventory": [],
                    "aisleStatus": aisle_status_all,
                    "tasks": [
                        {
                            "taskId": task_id,
                            "taskType": "INBOUND",
                            "targetAisle": aisle,
                            "skus": [sku(PAIR_1), sku(PAIR_2)],
                        }
                    ],
                },
                expected_status=200,
            )
            self.assertEqual(scheduled["status"], "SUCCESS")

            aisle_assignments = scheduled["data"]["aisleAssignments"]
            aisle_item = next(item for item in aisle_assignments if item["aisleId"] == aisle)
            self.assertIsNotNone(aisle_item["assignedTask"])
            assigned = aisle_item["assignedTask"]
            self.assertEqual(assigned["taskId"], task_id)
            self.assertEqual(assigned["taskType"], "INBOUND")

            # Payload correctness: two positions, same column/level, skuIds preserved.
            self.assertIsInstance(assigned["positions"], list)
            self.assertEqual(len(assigned["positions"]), 2)
            self.assertEqual(assigned["positions"][0]["column"], assigned["positions"][1]["column"])
            self.assertEqual(assigned["positions"][0]["level"], assigned["positions"][1]["level"])
            self.assertEqual(
                {assigned["positions"][0]["skuId"], assigned["positions"][1]["skuId"]},
                {PAIR_1, PAIR_2},
            )

            # Unconfirmed gate: before EXECUTING, any new schedule should 409 and list the task.
            blocked = requests.post(
                f"{self.base}/api/v1/schedule/mixed",
                json={
                    "currentTime": "2026-09-10 18:18:51",
                    "inventory": [],
                    "aisleStatus": aisle_status_all,
                    "tasks": [
                        {
                            "taskId": f"INBOUND_SHOULD_BLOCK_HTTP_{cycle:02d}",
                            "taskType": "INBOUND",
                            "targetAisle": aisle,
                            "skus": [sku(PAIR_1), sku(PAIR_2)],
                        }
                    ],
                },
                timeout=5.0,
            )
            self.assertEqual(blocked.status_code, 409, blocked.text)
            blocked_body = blocked.json()
            self.assertEqual(blocked_body["status"], "FAILED")
            self.assertIn(task_id, blocked_body["data"]["unconfirmed_tasks"])

            self.post(
                "/api/v1/task/feedback",
                {
                    "taskId": task_id,
                    "taskType": "INBOUND",
                    "status": "EXECUTING",
                    "startTime": "2026-09-10 18:19:00",
                    "failureReason": "",
                },
                expected_status=200,
            )
            self.post(
                "/api/v1/task/feedback",
                {
                    "taskId": task_id,
                    "taskType": "INBOUND",
                    "status": "COMPLETED",
                    "startTime": "2026-09-10 18:19:00",
                    "failureReason": "",
                },
                expected_status=200,
            )

            # inventory closed-loop assertion after COMPLETED:
            status = self.get("/api/v1/status")
            inv = status["data"]["inventory"]
            # Inbound returns 2 positions pointing to the same physical slot; inventory API returns
            # one record per shelf (UPPER/LOWER). Validate set equality at that (row,col,level).
            slot = (assigned["positions"][0]["row"], assigned["positions"][0]["column"], assigned["positions"][0]["level"])
            expected_skus = {p["skuId"] for p in assigned["positions"]}
            got = []
            for p in inv:
                if p.get("aisleId") != aisle:
                    continue
                if (p.get("row"), p.get("column"), p.get("level")) != slot:
                    continue
                if not p.get("positions"):
                    continue
                got.append(p["positions"][0])
            got_skus = {x.get("skuId") for x in got if x.get("quantity", 0) > 0}
            self.assertEqual(got_skus, expected_skus)

        unconfirmed = self.get("/api/v1/task/unconfirmed")
        self.assertEqual(unconfirmed["status"], "SUCCESS")
        self.assertEqual(unconfirmed["data"]["count"], 0)
        self.assertTrue(unconfirmed["data"]["can_accept_new_task"])

        # ---- outbound group gating ----
        # Reseed deterministic inventory for gating (in case inbound filled positions elsewhere).
        self.post(
            "/api/v1/schedule/mixed",
            {
                "currentTime": "2026-09-10 18:55:00",
                "inventory": [
                    {"aisleId": "1", "row": 1, "column": 2, "level": 2, "shelf": "UPPER", "positions": [sku(PAIR_1)]},
                    {"aisleId": "1", "row": 1, "column": 2, "level": 2, "shelf": "LOWER", "positions": [sku(PAIR_2)]},
                    {"aisleId": "1", "row": 2, "column": 3, "level": 3, "shelf": "UPPER", "positions": [sku(PAIR_3)]},
                    {"aisleId": "1", "row": 2, "column": 3, "level": 3, "shelf": "LOWER", "positions": [sku(PAIR_4)]},
                ],
                "aisleStatus": [{"aisleId": "1", "isAvailable": True, "bank": "LEFT", "exitCongestion": []}],
                "tasks": [],
            },
            expected_status=200,
        )

        inline_plan = {
            "operationType": "UPDATE",
            "planDate": "2026-01-21 09:00:00",
            "plans": [
                {
                    "planId": "PLAN-GROUPS-HTTP",
                    "lineId": "LINE-1",
                    "planIndex": [
                        {"requiredSkus": [[sku(PAIR_1), sku(PAIR_2)]]},
                        {"requiredSkus": [[sku(PAIR_3), sku(PAIR_4)]]},
                    ],
                }
            ],
        }

        gp1_id = f"OUTBOUND_PL1_GP1_{PAIR_1}_{PAIR_2}"
        gp2_id = f"OUTBOUND_PL1_GP2_{PAIR_3}_{PAIR_4}"

        scheduled = self.post(
            "/api/v1/schedule/mixed",
            {
                "currentTime": "2026-09-10 19:00:00",
                "productionPlan": inline_plan,
                "productionLineCurrentGroup": {"LINE-1": 0},
                "inventory": [],
                "aisleStatus": [{"aisleId": "1", "isAvailable": True, "bank": "LEFT", "exitCongestion": []}],
                "tasks": [
                    {
                        "taskId": gp2_id,
                        "taskType": "OUTBOUND",
                        "planId": "PLAN-LINE1-20260122",
                        "planIndex": 2,
                        "skus": [sku(PAIR_3), sku(PAIR_4)],
                    },
                    {
                        "taskId": gp1_id,
                        "taskType": "OUTBOUND",
                        "planId": "PLAN-LINE1-20260122",
                        "planIndex": 1,
                        "skus": [sku(PAIR_1), sku(PAIR_2)],
                    },
                ],
            },
            expected_status=200,
        )

        aisle_1 = next(item for item in scheduled["data"]["aisleAssignments"] if item["aisleId"] == "1")
        self.assertIsNotNone(aisle_1["assignedTask"])
        self.assertEqual(aisle_1["assignedTask"]["taskId"], gp1_id)
        # With match preview enabled, GP2 should be visible as a match but not dispatched yet.
        raw_matched = aisle_1.get("matchedTasks")
        if not raw_matched:
            raise AssertionError(f"matchedTasks missing/empty: {json.dumps(aisle_1, ensure_ascii=False)}")
        matched = [t["taskId"] for t in raw_matched]
        self.assertIn(gp1_id, matched)
        self.assertIn(gp2_id, matched)

        self.post(
            "/api/v1/task/feedback",
            {
                "taskId": gp1_id,
                "taskType": "OUTBOUND",
                "status": "EXECUTING",
                "startTime": "2026-09-10 19:00:10",
                "failureReason": "",
            },
            expected_status=200,
        )
        self.post(
            "/api/v1/task/feedback",
            {
                "taskId": gp1_id,
                "taskType": "OUTBOUND",
                "status": "COMPLETED",
                "startTime": "2026-09-10 19:00:10",
                "failureReason": "",
            },
            expected_status=200,
        )

        # inventory after gp1 completed: pair1/pair2 should be gone; pair3/pair4 should remain
        status = self.get("/api/v1/status")
        inv_summary = {(p["positions"][0]["skuId"], p["positions"][0]["quantity"]) for p in status["data"]["inventory"] if (p.get("aisleId") == "1" and p.get("positions") and p["positions"][0].get("skuId"))}
        self.assertTrue(all(qty == 0 or sku not in {PAIR_1, PAIR_2} for sku, qty in inv_summary))
        self.assertTrue(any(sku == PAIR_3 and qty >= 1 for sku, qty in inv_summary))
        self.assertTrue(any(sku == PAIR_4 and qty >= 1 for sku, qty in inv_summary))

        # After GP1 completes, GP2 may still be temporarily blocked by outbound cooldown.
        # Poll until it becomes dispatchable, but still require the match to show up immediately.
        deadline = time.time() + 20.0
        last = None
        while time.time() < deadline:
            last = self.post(
                "/api/v1/schedule/mixed",
                {
                    "currentTime": "2026-09-10 19:05:00",
                    "productionPlan": inline_plan,
                    "productionLineCurrentGroup": {"LINE-1": 1},
                    "inventory": [],
                    "aisleStatus": [{"aisleId": "1", "isAvailable": True, "bank": "LEFT", "exitCongestion": []}],
                    "tasks": [
                        {
                            "taskId": gp2_id,
                            "taskType": "OUTBOUND",
                            "planId": "PLAN-LINE1-20260122",
                            "planIndex": 2,
                            "skus": [sku(PAIR_3), sku(PAIR_4)],
                        }
                    ],
                },
                expected_status=200,
            )
            aisle_1b = next(item for item in last["data"]["aisleAssignments"] if item["aisleId"] == "1")
            matched = [t["taskId"] for t in (aisle_1b.get("matchedTasks") or [])]
            self.assertIn(gp2_id, matched)
            if aisle_1b["assignedTask"] is not None and aisle_1b["assignedTask"]["taskId"] == gp2_id:
                break
            time.sleep(0.25)
        else:
            raise AssertionError(f"GP2 was never dispatched within deadline. last={json.dumps(last, ensure_ascii=False)}")

        # complete GP2 and assert inventory closed-loop
        self.post(
            "/api/v1/task/feedback",
            {"taskId": gp2_id, "taskType": "OUTBOUND", "status": "EXECUTING", "startTime": "2026-09-10 19:06:00", "failureReason": ""},
            expected_status=200,
        )
        self.post(
            "/api/v1/task/feedback",
            {"taskId": gp2_id, "taskType": "OUTBOUND", "status": "COMPLETED", "startTime": "2026-09-10 19:06:00", "failureReason": ""},
            expected_status=200,
        )
        status = self.get("/api/v1/status")
        inv_summary = {(p["positions"][0]["skuId"], p["positions"][0]["quantity"]) for p in status["data"]["inventory"] if (p.get("aisleId") == "1" and p.get("positions") and p["positions"][0].get("skuId"))}
        self.assertTrue(all(qty == 0 or sku not in {PAIR_3, PAIR_4} for sku, qty in inv_summary))


if __name__ == "__main__":
    unittest.main()
