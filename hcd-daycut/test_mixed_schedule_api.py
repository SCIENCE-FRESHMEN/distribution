import asyncio
import io
import json
import types
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from api.models import MixedScheduleRequest, TaskFeedbackRequest
from api.models import InboundAllocateRequest
from api.routes.inbound import allocate_inbound
from api.routes.feedback import task_feedback
from api.routes.schedule import mixed_schedule
from api.services.warehouse_service import (
    get_warehouse_service,
    reset_warehouse_service,
)
from api.state import get_task_state_manager, reset_task_state_manager
from allocation.proposed_strategy import ProposedPositionAllocator

SOLO_SKU = "2801022-TG150"
PAIR_SKU_1 = "2801022-TG152"
PAIR_SKU_2 = "2801038-TG152"


SKU_ATTRS = {
    "version": "00",
    "生产属性": "默认",
}

# Normalized attrs required by config/warehouse.json `match_fields`.
SKU_ATTRS = {
    "version": "00",
    "productionAttribute": "D",
    "militaryCivilianMark": "M",
    "salesArea": "N",
}


def sku_entry(sku_id: str, quantity: int = 1, **extra):
    return {
        "skuId": sku_id,
        "quantity": quantity,
        **SKU_ATTRS,
        **extra,
    }


def mixed_request(task_id: str, aisle: str, skus: list[dict]):
    return {
        "currentTime": "2026-09-10 18:18:51",
        "inventory": [],
        "aisleStatus": [],
        "tasks": [
            {
                "taskId": task_id,
                "taskType": "INBOUND",
                "targetAisle": aisle,
                "skus": skus,
            }
        ],
    }


class MixedScheduleApiTest(unittest.TestCase):
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

    def decode_response(self, response):
        if hasattr(response, "body"):
            return json.loads(response.body.decode("utf-8"))
        return response.model_dump()

    def test_mixed_schedule_keeps_executing_tasks_out_of_unconfirmed_list(self):
        first = self.run_async(
            mixed_schedule(
                MixedScheduleRequest(**mixed_request(
                "INBOUND_1111111",
                "2",
                [sku_entry(PAIR_SKU_1), sku_entry(PAIR_SKU_2)],
                )),
                warehouse_service=self.warehouse_service,
                task_manager=self.task_manager,
            )
        )
        self.assertEqual(self.decode_response(first)["status"], "SUCCESS")

        blocked = self.run_async(
            mixed_schedule(
                MixedScheduleRequest(**mixed_request(
                "INBOUND_2222222",
                "3",
                [sku_entry(PAIR_SKU_1), sku_entry(PAIR_SKU_2)],
                )),
                warehouse_service=self.warehouse_service,
                task_manager=self.task_manager,
            )
        )
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(self.decode_response(blocked)["data"]["unconfirmed_tasks"], ["INBOUND_1111111"])

        feedback = self.run_async(
            task_feedback(
                TaskFeedbackRequest(**{
                "taskId": "INBOUND_1111111",
                "taskType": "INBOUND",
                "status": "EXECUTING",
                "startTime": "2026-05-31 23:23:44",
                "failureReason": "",
                }),
                warehouse_service=self.warehouse_service,
                task_manager=self.task_manager,
            )
        )
        self.assertEqual(self.decode_response(feedback)["status"], "SUCCESS")

        second = self.run_async(
            mixed_schedule(
                MixedScheduleRequest(**mixed_request(
                "INBOUND_3333333",
                "4",
                [sku_entry(PAIR_SKU_1), sku_entry(PAIR_SKU_2)],
                )),
                warehouse_service=self.warehouse_service,
                task_manager=self.task_manager,
            )
        )
        aisle_assignments = self.decode_response(second)["data"]["aisleAssignments"]
        aisle_2 = next(item for item in aisle_assignments if item["aisleId"] == "2")
        aisle_4 = next(item for item in aisle_assignments if item["aisleId"] == "4")
        self.assertEqual(aisle_2["assignedTask"]["taskId"], "INBOUND_1111111")
        self.assertEqual(aisle_4["assignedTask"]["taskId"], "INBOUND_3333333")

        blocked_again = self.run_async(
            mixed_schedule(
                MixedScheduleRequest(**mixed_request(
                "INBOUND_4444444",
                "1",
                [sku_entry(PAIR_SKU_1), sku_entry(PAIR_SKU_2)],
                )),
                warehouse_service=self.warehouse_service,
                task_manager=self.task_manager,
            )
        )
        self.assertEqual(blocked_again.status_code, 409)
        self.assertEqual(self.decode_response(blocked_again)["data"]["unconfirmed_tasks"], ["INBOUND_3333333"])

    def test_mixed_schedule_rejects_unknown_bom_skus(self):
        response = self.run_async(
            mixed_schedule(
                MixedScheduleRequest(**mixed_request(
                "INBOUND_BAD_BOM",
                "3",
                [sku_entry("NOT_IN_BOM_1"), sku_entry("NOT_IN_BOM_2")],
                )),
                warehouse_service=self.warehouse_service,
                task_manager=self.task_manager,
            )
        )

        body = self.decode_response(response)
        self.assertEqual(body["status"], "FAILED")
        self.assertEqual(body["data"]["invalidSkus"], ["NOT_IN_BOM_1", "NOT_IN_BOM_2"])
        self.assertEqual(body["data"]["taskIds"], ["INBOUND_BAD_BOM"])

    def test_mixed_schedule_allows_outbound_unknown_sku_from_runtime_inventory(self):
        request = {
            "currentTime": "2026-09-10 18:18:51",
            "inventory": [
                {
                    "aisleId": "1",
                    "row": 1,
                    "column": 2,
                    "level": 2,
                    "shelf": "UPPER",
                    "positions": [sku_entry("RUNTIME_ONLY_SKU")],
                }
            ],
            "aisleStatus": [],
            "tasks": [
                {
                    "taskId": "OUTBOUND_RUNTIME_ONLY",
                    "taskType": "OUTBOUND",
                    "planId": "LINE-1",
                    "planIndex": 1,
                    "skus": [sku_entry("RUNTIME_ONLY_SKU")],
                }
            ],
        }
        response = self.run_async(
            mixed_schedule(
                MixedScheduleRequest(**request),
                warehouse_service=self.warehouse_service,
                task_manager=self.task_manager,
            )
        )

        body = self.decode_response(response)
        self.assertEqual(body["status"], "SUCCESS")

    def test_inventory_sync_does_not_whitelist_unknown_sku_for_later_inbound(self):
        outbound_request = {
            "currentTime": "2026-09-10 18:18:51",
            "inventory": [
                {
                    "aisleId": "1",
                    "row": 1,
                    "column": 2,
                    "level": 2,
                    "shelf": "UPPER",
                    "positions": [sku_entry("RUNTIME_ONLY_SKU")],
                }
            ],
            "aisleStatus": [],
            "tasks": [
                {
                    "taskId": "OUTBOUND_RUNTIME_ONLY",
                    "taskType": "OUTBOUND",
                    "planId": "LINE-1",
                    "planIndex": 1,
                    "skus": [sku_entry("RUNTIME_ONLY_SKU")],
                }
            ],
        }
        first = self.run_async(
            mixed_schedule(
                MixedScheduleRequest(**outbound_request),
                warehouse_service=self.warehouse_service,
                task_manager=self.task_manager,
            )
        )
        self.assertEqual(self.decode_response(first)["status"], "SUCCESS")

        feedback = self.run_async(
            task_feedback(
                TaskFeedbackRequest(
                    taskId="OUTBOUND_RUNTIME_ONLY",
                    taskType="OUTBOUND",
                    status="EXECUTING",
                    startTime="2026-05-31 23:23:44",
                    failureReason="",
                ),
                warehouse_service=self.warehouse_service,
                task_manager=self.task_manager,
            )
        )
        self.assertEqual(self.decode_response(feedback)["status"], "SUCCESS")

        second = self.run_async(
            mixed_schedule(
                MixedScheduleRequest(
                    **mixed_request(
                        "INBOUND_RUNTIME_ONLY",
                        "1",
                        [sku_entry("RUNTIME_ONLY_SKU", beamSide="LEFT")],
                    )
                ),
                warehouse_service=self.warehouse_service,
                task_manager=self.task_manager,
            )
        )
        body = self.decode_response(second)
        self.assertEqual(body["status"], "FAILED")
        self.assertEqual(body["data"]["invalidSkus"], ["RUNTIME_ONLY_SKU"])

    def test_single_beam_left_side_controls_allocated_row(self):
        response = self.run_async(
            mixed_schedule(
                MixedScheduleRequest(**mixed_request(
                "INBOUND_SINGLE_LEFT",
                "1",
                [sku_entry(SOLO_SKU, beamSide="LEFT")],
                )),
                warehouse_service=self.warehouse_service,
                task_manager=self.task_manager,
            )
        )

        assignment = next(
            item["assignedTask"]
            for item in self.decode_response(response)["data"]["aisleAssignments"]
            if item["aisleId"] == "1"
        )
        self.assertEqual(assignment["taskId"], "INBOUND_SINGLE_LEFT")
        self.assertEqual(assignment["positions"][0]["row"], 1)

    def test_single_beam_right_side_controls_allocated_row(self):
        response = self.run_async(
            mixed_schedule(
                MixedScheduleRequest(**mixed_request(
                "INBOUND_SINGLE_RIGHT",
                "1",
                [sku_entry(SOLO_SKU, beamSide="RIGHT")],
                )),
                warehouse_service=self.warehouse_service,
                task_manager=self.task_manager,
            )
        )

        assignment = next(
            item["assignedTask"]
            for item in self.decode_response(response)["data"]["aisleAssignments"]
            if item["aisleId"] == "1"
        )
        self.assertEqual(assignment["taskId"], "INBOUND_SINGLE_RIGHT")
        self.assertEqual(assignment["positions"][0]["row"], 2)

    def test_single_beam_without_beam_side_is_rejected(self):
        with self.assertRaises(ValidationError):
            MixedScheduleRequest(
                **mixed_request(
                    "INBOUND_SINGLE_DEFAULT",
                    "1",
                    [sku_entry(SOLO_SKU)],
                )
            )

    def test_double_beam_position_assignment_is_unchanged(self):
        response = self.run_async(
            mixed_schedule(
                MixedScheduleRequest(**mixed_request(
                    "INBOUND_DOUBLE_PAIR",
                    "2",
                    [sku_entry(PAIR_SKU_1), sku_entry(PAIR_SKU_2)],
                )),
                warehouse_service=self.warehouse_service,
                task_manager=self.task_manager,
            )
        )

        body = self.decode_response(response)
        self.assertEqual(body["status"], "SUCCESS")
        assignment = next(
            item["assignedTask"]
            for item in body["data"]["aisleAssignments"]
            if item["aisleId"] == "2"
        )
        self.assertEqual(assignment["taskId"], "INBOUND_DOUBLE_PAIR")
        self.assertEqual(len(assignment["positions"]), 2)
        self.assertEqual(assignment["positions"][0]["column"], assignment["positions"][1]["column"])
        self.assertEqual(assignment["positions"][0]["level"], assignment["positions"][1]["level"])

    def test_inbound_allocate_single_beam_without_beam_side_is_rejected(self):
        with self.assertRaises(ValidationError):
            InboundAllocateRequest(
                tasks=[
                    {
                        "taskId": "ALLOCATE_SINGLE_LEGACY",
                        "skus": [sku_entry(SOLO_SKU)],
                    }
                ]
            )

    def test_strategy_layer_accepts_legacy_side_b_for_single_beam(self):
        allocator = ProposedPositionAllocator(self.warehouse_service.core)
        task = types.SimpleNamespace(
            task_id="LEGACY_SIDE_B",
            assigned_aisle=1,
            skus=[{"skuId": SOLO_SKU, "quantity": 1, "side": "B", **SKU_ATTRS}],
        )
        positions = allocator.allocate(
            self.warehouse_service.core.inventory_manager.inventory_positions,
            task,
        )
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].row, 2)

    def test_strategy_layer_accepts_legacy_second_slot_single_beam(self):
        allocator = ProposedPositionAllocator(self.warehouse_service.core)
        task = types.SimpleNamespace(
            task_id="LEGACY_SLOT_B",
            assigned_aisle=1,
            skus=[
                {"skuId": None, "quantity": 1, "side": "A"},
                {"skuId": SOLO_SKU, "quantity": 1, "side": "B", **SKU_ATTRS},
            ],
        )
        positions = allocator.allocate(
            self.warehouse_service.core.inventory_manager.inventory_positions,
            task,
        )
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].row, 2)

    def test_inbound_allocate_prefers_pairable_aisle(self):
        self.warehouse_service._clear_all_inventory()
        self.warehouse_service.sync_inventory([
            {
                "aisleId": "3",
                "row": 5,
                "column": 2,
                "level": 2,
                "shelf": "UPPER",
                "positions": [sku_entry(PAIR_SKU_2)],
            }
        ])

        response = self.run_async(
            allocate_inbound(
                InboundAllocateRequest(
                    tasks=[
                        {
                            "taskId": "ALLOC_PAIR_PREF",
                            "skus": [sku_entry(PAIR_SKU_1, beamSide="LEFT")],
                        }
                    ]
                ),
                warehouse_service=self.warehouse_service,
                task_manager=self.task_manager,
            )
        )

        body = self.decode_response(response)
        self.assertEqual(body["status"], "SUCCESS")
        self.assertEqual(body["data"]["assignments"][0]["recommendedAisle"], "3")

    def test_proposed_aisle_allocator_accepts_api_single_beam_shape(self):
        allocator = self.warehouse_service.core.inbound_aisle_allocator
        task = types.SimpleNamespace(
            task_id="API_SINGLE_LEFT",
            skus=[{"skuId": SOLO_SKU, "quantity": 1, "beamSide": "LEFT", **SKU_ATTRS}],
        )
        aisle = allocator.allocate(task, self.warehouse_service.core.inventory_manager.inventory_positions)
        self.assertIsInstance(aisle, int)

    def test_inbound_allocate_rejects_unknown_bom_skus(self):
        response = self.run_async(
            allocate_inbound(
                InboundAllocateRequest(
                    tasks=[
                        {
                            "taskId": "ALLOCATE_BAD_BOM",
                            "skus": [sku_entry("NOT_IN_BOM_1"), sku_entry("NOT_IN_BOM_2")],
                        }
                    ]
                ),
                warehouse_service=self.warehouse_service,
                task_manager=self.task_manager,
            )
        )

        self.assertEqual(response.status_code, 400)
        body = self.decode_response(response)
        self.assertEqual(body["status"], "FAILED")
        self.assertEqual(body["data"]["invalidSkus"], ["NOT_IN_BOM_1", "NOT_IN_BOM_2"])

    def test_visualize_results_main_exits_cleanly_without_matplotlib(self):
        import visualize_results

        with patch.object(visualize_results, "plt", None):
            with patch("sys.argv", ["visualize_results.py"]):
                with patch("sys.stdout", new_callable=io.StringIO) as fake_stdout:
                    visualize_results.main()
        self.assertIn("matplotlib", fake_stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
