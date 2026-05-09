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


class InventoryMoveAndClearHttpTests(unittest.TestCase):
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

    def _find_shelf(self, inv: list[dict], aisle: str, row: int, col: int, level: int, shelf: str) -> dict:
        for item in inv:
            if item.get("aisleId") != aisle:
                continue
            if (item.get("row"), item.get("column"), item.get("level")) != (row, col, level):
                continue
            if str(item.get("shelf") or "").upper() != shelf.upper():
                continue
            return item
        raise AssertionError(f"inventory item not found for {aisle}-{row}-{col}-{level}-{shelf}")

    def test_incremental_inventory_update_can_clear_and_move(self):
        """
        Realistic move simulation over HTTP:
        - Seed a slot with (UPPER,LOWER) pair SKUs.
        - Then "move" it by sending incremental inventory updates that clear the old slot
          and set the new slot.

        This requires sync_inventory incremental mode to support clearing, not only adding.
        """
        aisle_status_all = [
            {"aisleId": str(i), "isAvailable": True, "bank": "LEFT" if i % 2 else "RIGHT", "exitCongestion": []}
            for i in range(1, 6)
        ]

        # Slot A (aisle 2, external row=3 maps to internal row=1)
        a_aisle = "2"
        a_row, a_col, a_level = 3, 2, 2

        # Slot B (aisle 2, external row=3 maps to internal row=1)
        b_aisle = "2"
        b_row, b_col, b_level = 3, 3, 2

        # Seed inventory at Slot A.
        self.post(
            "/api/v1/schedule/mixed",
            {
                "currentTime": "2026-04-23 09:00:00",
                "inventory": [
                    {"aisleId": a_aisle, "row": a_row, "column": a_col, "level": a_level, "shelf": "UPPER", "positions": [sku(PAIR_1)]},
                    {"aisleId": a_aisle, "row": a_row, "column": a_col, "level": a_level, "shelf": "LOWER", "positions": [sku(PAIR_2)]},
                ],
                "aisleStatus": aisle_status_all,
                "tasks": [],
            },
            expected_status=200,
        )

        status = self.get("/api/v1/status")
        inv = status["data"]["inventory"]
        a_upper = self._find_shelf(inv, a_aisle, a_row, a_col, a_level, "UPPER")
        a_lower = self._find_shelf(inv, a_aisle, a_row, a_col, a_level, "LOWER")
        self.assertEqual(a_upper["positions"][0]["skuId"], PAIR_1)
        self.assertEqual(a_upper["positions"][0]["quantity"], 1)
        self.assertEqual(a_lower["positions"][0]["skuId"], PAIR_2)
        self.assertEqual(a_lower["positions"][0]["quantity"], 1)

        # Move: clear Slot A shelves, set Slot B shelves (incremental update, <15 entries).
        self.post(
            "/api/v1/schedule/mixed",
            {
                "currentTime": "2026-04-23 09:05:00",
                "inventory": [
                    {"aisleId": a_aisle, "row": a_row, "column": a_col, "level": a_level, "shelf": "UPPER", "positions": []},
                    {"aisleId": a_aisle, "row": a_row, "column": a_col, "level": a_level, "shelf": "LOWER", "positions": []},
                    {"aisleId": b_aisle, "row": b_row, "column": b_col, "level": b_level, "shelf": "UPPER", "positions": [sku(PAIR_1)]},
                    {"aisleId": b_aisle, "row": b_row, "column": b_col, "level": b_level, "shelf": "LOWER", "positions": [sku(PAIR_2)]},
                ],
                "aisleStatus": aisle_status_all,
                "tasks": [],
            },
            expected_status=200,
        )

        status2 = self.get("/api/v1/status")
        inv2 = status2["data"]["inventory"]

        # Old slot must be empty after clear.
        a_upper2 = self._find_shelf(inv2, a_aisle, a_row, a_col, a_level, "UPPER")
        a_lower2 = self._find_shelf(inv2, a_aisle, a_row, a_col, a_level, "LOWER")
        self.assertIn(a_upper2["positions"][0]["quantity"], (0, None))
        self.assertIn(a_lower2["positions"][0]["quantity"], (0, None))
        self.assertIn(a_upper2["positions"][0]["skuId"], ("", None))
        self.assertIn(a_lower2["positions"][0]["skuId"], ("", None))

        # New slot must have the pair.
        b_upper = self._find_shelf(inv2, b_aisle, b_row, b_col, b_level, "UPPER")
        b_lower = self._find_shelf(inv2, b_aisle, b_row, b_col, b_level, "LOWER")
        self.assertEqual(b_upper["positions"][0]["skuId"], PAIR_1)
        self.assertEqual(b_upper["positions"][0]["quantity"], 1)
        self.assertEqual(b_lower["positions"][0]["skuId"], PAIR_2)
        self.assertEqual(b_lower["positions"][0]["quantity"], 1)


if __name__ == "__main__":
    unittest.main()
