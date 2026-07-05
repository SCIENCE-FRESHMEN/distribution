"""
Simple tester for POST /api/v1/task/adjust.

Usage examples:
1) Minimal (only switch aisle):
   python scripts/test_adjust_api.py --task-id IN-001 --aisle-id 2

2) Update SKU:
   python scripts/test_adjust_api.py --task-id IN-001 --sku-id 2801022-TG152 --beam-side LEFT

3) Full payload from file:
   python scripts/test_adjust_api.py --payload-file payload_adjust.json

4) Built-in full-flow demos:
   python scripts/test_adjust_api.py --demo full_flow_move_aisle
   python scripts/test_adjust_api.py --demo full_flow_change_sku
   python scripts/test_adjust_api.py --demo full_flow_conflict_position
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import requests


def build_default_sku(
    sku_id: str,
    quantity: int = 1,
    beam_side: Optional[str] = None,
) -> Dict[str, Any]:
    sku: Dict[str, Any] = {
        "skuId": sku_id,
        "quantity": quantity,
        "version": "00",
        "productionAttribute": "D",
        "militaryCivilianMark": "M",
        "salesArea": "N",
    }
    if beam_side:
        sku["beamSide"] = beam_side
    return sku


def build_default_position(
    row: int,
    column: int,
    level: int,
    shelf: str,
    sku_id: str,
    quantity: int = 1,
) -> Dict[str, Any]:
    return {
        "row": row,
        "column": column,
        "level": level,
        "shelf": shelf,
        "skuId": sku_id,
        "quantity": quantity,
        "version": "00",
        "productionAttribute": "D",
        "militaryCivilianMark": "M",
        "salesArea": "N",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Test /api/v1/task/adjust endpoint")
    parser.add_argument("--base-url", default="http://localhost:8000", help="API host root")
    parser.add_argument("--timeout", type=float, default=15.0, help="Request timeout (seconds)")

    parser.add_argument("--payload-file", type=str, help="Path to full JSON payload file")
    parser.add_argument(
        "--demo",
        type=str,
        choices=[
            "full_flow_move_aisle",
            "full_flow_change_sku",
            "full_flow_conflict_position",
        ],
        help="Use built-in demo payload",
    )

    parser.add_argument("--task-id", type=str, help="Task id to adjust")
    parser.add_argument("--task-type", type=str, default="INBOUND", help="Task type, default INBOUND")
    parser.add_argument("--aisle-id", type=str, help="Target aisle id")

    parser.add_argument("--sku-id", type=str, help="SKU id for one-item skus update")
    parser.add_argument("--quantity", type=int, default=1, help="SKU quantity")
    parser.add_argument("--beam-side", type=str, choices=["LEFT", "RIGHT"], help="Beam side")

    parser.add_argument("--row", type=int, help="Position row")
    parser.add_argument("--column", type=int, help="Position column")
    parser.add_argument("--level", type=int, help="Position level")
    parser.add_argument("--shelf", type=str, choices=["UPPER", "LOWER"], help="Position shelf")

    return parser.parse_args()


def load_payload_from_file(payload_file: str) -> Dict[str, Any]:
    path = Path(payload_file)
    if not path.exists():
        raise FileNotFoundError(f"Payload file not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def build_demo_payload(demo: str) -> Dict[str, Any]:
    if demo == "full_flow_move_aisle":
        return {
            "taskId": "IN-FIX-002",
            "taskType": "INBOUND",
            "aisleId": "2",
        }
    if demo == "full_flow_change_sku":
        return {
            "taskId": "IN-FIX-002",
            "taskType": "INBOUND",
            "aisleId": "2",
            "skus": [build_default_sku("2801038-TG152", quantity=1, beam_side="RIGHT")],
        }
    if demo == "full_flow_conflict_position":
        return {
            "taskId": "IN-FIX-002",
            "taskType": "INBOUND",
            "aisleId": "2",
            "positions": [
                build_default_position(
                    row=5,
                    column=3,
                    level=9,
                    shelf="UPPER",
                    sku_id="2801038-TG152",
                    quantity=1,
                )
            ],
        }
    raise ValueError(f"Unknown demo: {demo}")


def build_full_flow_mixed_payload(demo: str) -> Dict[str, Any]:
    if demo == "full_flow_conflict_position":
        return {
            "currentTime": "2026-06-01 10:00:00",
            "inventory": [],
            "aisleStatus": [],
            "tasks": [
                {
                    "taskId": "IN-BLOCK-001",
                    "taskType": "INBOUND",
                    "targetAisle": "2",
                    "inLine": 1,
                    "skus": [
                        build_default_sku(
                            sku_id="2801022-TG152",
                            quantity=1,
                            beam_side="LEFT",
                        )
                    ],
                },
                {
                    "taskId": "IN-FIX-002",
                    "taskType": "INBOUND",
                    "targetAisle": "3",
                    "inLine": 1,
                    "skus": [
                        build_default_sku(
                            sku_id="2801022-TG152",
                            quantity=1,
                            beam_side="LEFT",
                        )
                    ],
                },
            ],
        }

    return {
        "currentTime": "2026-06-01 10:00:00",
        "inventory": [],
        "aisleStatus": [],
        "tasks": [
            {
                "taskId": "IN-FIX-002",
                "taskType": "INBOUND",
                "targetAisle": "3",
                "inLine": 1,
                "skus": [
                    build_default_sku(
                        sku_id="2801022-TG152",
                        quantity=1,
                        beam_side="LEFT",
                    )
                ],
            }
        ],
    }


def post_json(url: str, payload: Dict[str, Any], timeout: float) -> requests.Response:
    return requests.post(
        url,
        headers={"Content-Type": "application/json"},
        json=payload,
        timeout=timeout,
    )


def _get_conflict_position_from_mixed_response(mixed_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    executable = (mixed_data or {}).get("data", {}).get("executableTasksByAisle", {}) or {}
    aisle2_tasks = executable.get("2") or []
    if not aisle2_tasks:
        return None
    positions = aisle2_tasks[0].get("positions") or []
    if not positions:
        return None
    first = positions[0]
    return {
        "row": first.get("row"),
        "column": first.get("column"),
        "level": first.get("level"),
        "shelf": first.get("shelf"),
        "skuId": first.get("skuId") or "2801022-TG152",
        "quantity": first.get("quantity") or 1,
        "version": "00",
        "productionAttribute": "D",
        "militaryCivilianMark": "M",
        "salesArea": "N",
    }


def build_payload_from_args(args: argparse.Namespace) -> Dict[str, Any]:
    if not args.task_id:
        raise ValueError("task-id is required when payload-file is not provided")

    payload: Dict[str, Any] = {
        "taskId": args.task_id,
        "taskType": args.task_type,
    }

    if args.aisle_id is not None:
        payload["aisleId"] = str(args.aisle_id)

    if args.sku_id:
        payload["skus"] = [
            build_default_sku(
                sku_id=args.sku_id,
                quantity=args.quantity,
                beam_side=args.beam_side,
            )
        ]

    has_position_fields = all(
        v is not None for v in [args.row, args.column, args.level, args.shelf, args.sku_id]
    )
    if has_position_fields:
        payload["positions"] = [
            build_default_position(
                row=args.row,
                column=args.column,
                level=args.level,
                shelf=args.shelf,
                sku_id=args.sku_id,
                quantity=args.quantity,
            )
        ]

    return payload


def main() -> int:
    args = parse_args()
    try:
        if args.demo:
            payload = build_demo_payload(args.demo)
        elif args.payload_file:
            payload = load_payload_from_file(args.payload_file)
        else:
            payload = build_payload_from_args(args)
    except Exception as exc:
        print(f"[ERROR] Build payload failed: {exc}")
        return 2

    adjust_url = f"{args.base_url.rstrip('/')}/api/v1/task/adjust"
    mixed_url = f"{args.base_url.rstrip('/')}/api/v1/schedule/mixed"

    if args.demo in {
        "full_flow_move_aisle",
        "full_flow_change_sku",
        "full_flow_conflict_position",
    }:
        mixed_payload = build_full_flow_mixed_payload(args.demo)
        print("=== REQUEST (STEP 1: MIXED) ===")
        print(f"POST {mixed_url}")
        print(json.dumps(mixed_payload, ensure_ascii=False, indent=2))
        try:
            mixed_resp = post_json(mixed_url, mixed_payload, args.timeout)
        except Exception as exc:
            print(f"[ERROR] Mixed request failed: {exc}")
            return 3

        print("\n=== RESPONSE (STEP 1: MIXED) ===")
        print(f"HTTP {mixed_resp.status_code}")
        try:
            print(json.dumps(mixed_resp.json(), ensure_ascii=False, indent=2))
        except Exception:
            print(mixed_resp.text)

        if args.demo == "full_flow_conflict_position":
            try:
                mixed_json = mixed_resp.json()
            except Exception:
                mixed_json = {}
            conflict_pos = _get_conflict_position_from_mixed_response(mixed_json)
            if conflict_pos is None:
                print("[ERROR] Could not extract occupied position from mixed response for conflict demo.")
                return 4
            payload["positions"] = [conflict_pos]

        print("\n=== REQUEST (STEP 2: ADJUST) ===")
        print(f"POST {adjust_url}")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        try:
            resp = post_json(adjust_url, payload, args.timeout)
        except Exception as exc:
            print(f"[ERROR] Adjust request failed: {exc}")
            return 3

        print("\n=== RESPONSE (STEP 2: ADJUST) ===")
        print(f"HTTP {resp.status_code}")
        try:
            print(json.dumps(resp.json(), ensure_ascii=False, indent=2))
        except Exception:
            print(resp.text)
        return 0 if resp.ok else 1

    print("=== REQUEST ===")
    print(f"POST {adjust_url}")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    try:
        resp = post_json(adjust_url, payload, args.timeout)
    except Exception as exc:
        print(f"[ERROR] Request failed: {exc}")
        return 3

    print("\n=== RESPONSE ===")
    print(f"HTTP {resp.status_code}")
    try:
        print(json.dumps(resp.json(), ensure_ascii=False, indent=2))
    except Exception:
        print(resp.text)

    return 0 if resp.ok else 1


if __name__ == "__main__":
    sys.exit(main())
