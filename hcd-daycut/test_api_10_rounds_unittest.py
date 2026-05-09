import asyncio
import copy
import json
import unittest
import uuid

from api.models import (
    BomUpdateRequest,
    InboundAllocateRequest,
    MixedScheduleRequest,
    TaskFeedbackRequest,
)
from api.routes.bom import update_bom_config
from api.routes.feedback import task_feedback
from api.routes.inbound import allocate_inbound
from api.routes.schedule import mixed_schedule
from api.services.warehouse_service import get_warehouse_service, reset_warehouse_service
from api.state import get_task_state_manager, reset_task_state_manager


SKU_ATTRS = {
    "version": "00",
    "productionAttribute": "D",
    "militaryCivilianMark": "M",
    "salesArea": "N",
}


def sku(sku_id: str, quantity: int = 1, **extra) -> dict:
    return {"skuId": sku_id, "quantity": quantity, **SKU_ATTRS, **extra}


class Api10RoundsTests(unittest.TestCase):
    def setUp(self):
        reset_warehouse_service()
        reset_task_state_manager()
        self.service = get_warehouse_service()
        self.manager = get_task_state_manager()

    def tearDown(self):
        reset_warehouse_service()
        reset_task_state_manager()

    def run_async(self, coro):
        return asyncio.run(coro)

    def decode(self, response):
        if hasattr(response, "body"):
            return json.loads(response.body.decode("utf-8"))
        return response.model_dump()

    def apply_inline_plan(self, plan_payload: dict):
        return self.run_async(
            mixed_schedule(
                MixedScheduleRequest(
                    **{
                        "currentTime": "2026-01-21 09:00:00",
                        "productionPlan": plan_payload,
                        "productionLineCurrentGroup": {"LINE-1": 0, "LINE-2": 0, "LINE-3": 0},
                        "inventory": [],
                        "aisleStatus": [],
                        "tasks": [],
                    }
                ),
                warehouse_service=self.service,
                task_manager=self.manager,
            )
        )

    # Round 01
    def test_round01_allocate_inbound_success_payload(self):
        resp = self.run_async(
            allocate_inbound(
                InboundAllocateRequest(
                    tasks=[
                        {
                            "taskId": "R01_ALLOC",
                            "skus": [
                                sku("2801022-TG152", beamSide="LEFT"),
                                sku("2801038-TG152", beamSide="RIGHT"),
                            ],
                        }
                    ]
                ),
                warehouse_service=self.service,
                task_manager=self.manager,
            )
        )
        body = self.decode(resp)
        self.assertEqual(body["status"], "SUCCESS")
        self.assertTrue(body["data"]["allocationId"].startswith("ALLOC-"))
        self.assertEqual(len(body["data"]["assignments"]), 1)
        self.assertEqual(body["data"]["assignments"][0]["taskId"], "R01_ALLOC")
        self.assertIsInstance(body["data"]["assignments"][0]["recommendedAisle"], str)

    # Round 02
    def test_round02_allocate_inbound_unknown_bom_rejected_payload(self):
        resp = self.run_async(
            allocate_inbound(
                InboundAllocateRequest(
                    tasks=[
                        {
                            "taskId": "R02_BAD_BOM",
                            "skus": [sku("NOT_IN_BOM_R02", beamSide="LEFT")],
                        }
                    ]
                ),
                warehouse_service=self.service,
                task_manager=self.manager,
            )
        )
        self.assertEqual(resp.status_code, 400)
        body = self.decode(resp)
        self.assertEqual(body["status"], "FAILED")
        self.assertEqual(body["data"]["invalidSkus"], ["NOT_IN_BOM_R02"])
        self.assertEqual(body["data"]["taskIds"], ["R02_BAD_BOM"])
        self.assertIn("timestamp", body["data"])

    # Round 03
    def test_round03_mixed_schedule_rejects_inbound_unknown_bom_payload(self):
        req = MixedScheduleRequest(
            **{
                "currentTime": "2026-09-10 18:18:51",
                "inventory": [],
                "aisleStatus": [],
                "tasks": [
                    {
                        "taskId": "R03_BAD_INBOUND",
                        "taskType": "INBOUND",
                        "targetAisle": "1",
                        "skus": [sku("NOT_IN_BOM_R03", beamSide="LEFT")],
                    }
                ],
            }
        )
        resp = self.run_async(mixed_schedule(req, warehouse_service=self.service, task_manager=self.manager))
        self.assertEqual(resp.status_code, 400)
        body = self.decode(resp)
        self.assertEqual(body["status"], "FAILED")
        self.assertEqual(body["data"]["invalidSkus"], ["NOT_IN_BOM_R03"])
        self.assertEqual(body["data"]["taskIds"], ["R03_BAD_INBOUND"])
        self.assertIn("timestamp", body["data"])

    # Round 04
    def test_round04_plan_add_then_add_preserves_existing_lines(self):
        first = self.decode(self.apply_inline_plan({
            "operationType": "ADD",
            "planDate": "2026-01-21 09:00:00",
            "plans": [
                {
                    "planId": "PLAN-R04-L1",
                    "lineId": "LINE-1",
                    "planIndex": [{"requiredSkus": [[sku("2801022-TG152"), sku("2801038-TG152")]]}],
                }
            ],
        }))
        self.assertEqual(first["status"], "SUCCESS")
        second = self.decode(self.apply_inline_plan({
            "operationType": "ADD",
            "planDate": "2026-01-21 09:00:00",
            "plans": [
                {
                    "planId": "PLAN-R04-L2",
                    "lineId": "LINE-2",
                    "planIndex": [{"requiredSkus": [[sku("2801021-TG152"), sku("2801037-TG152")]]}],
                }
            ],
        }))
        self.assertEqual(second["status"], "SUCCESS")
        self.assertIn(1, self.service.core.production_plan)
        self.assertIn(2, self.service.core.production_plan)

    # Round 05
    def test_round05_plan_update_replaces_existing_lines(self):
        first = self.decode(self.apply_inline_plan({
            "operationType": "ADD",
            "planDate": "2026-01-21 09:00:00",
            "plans": [
                {
                    "planId": "PLAN-R05-L1",
                    "lineId": "LINE-1",
                    "planIndex": [{"requiredSkus": [[sku("2801022-TG152"), sku("2801038-TG152")]]}],
                }
            ],
        }))
        self.assertEqual(first["status"], "SUCCESS")
        second = self.decode(self.apply_inline_plan({
            "operationType": "UPDATE",
            "planDate": "2026-01-21 09:00:00",
            "plans": [
                {
                    "planId": "PLAN-R05-L2",
                    "lineId": "LINE-2",
                    "planIndex": [{"requiredSkus": [[sku("2801021-TG152"), sku("2801037-TG152")]]}],
                }
            ],
        }))
        self.assertEqual(second["status"], "SUCCESS")
        self.assertEqual(self.service.core.production_plan, {2: [[["2801021-TG152", "2801037-TG152"]]]})

    # Round 06
    def test_round06_feedback_unknown_task_returns_failed_payload(self):
        resp = self.run_async(
            task_feedback(
                TaskFeedbackRequest(
                    taskId="R06_UNKNOWN_TASK",
                    taskType="OUTBOUND",
                    status="EXECUTING",
                    startTime="2026-05-31 23:23:44",
                    failureReason="",
                ),
                warehouse_service=self.service,
                task_manager=self.manager,
            )
        )
        body = self.decode(resp)
        self.assertEqual(body["status"], "FAILED")
        self.assertIn("R06_UNKNOWN_TASK", body["message"])

    # Round 07
    def test_round07_feedback_failed_requires_reason_model_validation(self):
        with self.assertRaises(ValueError) as ctx:
            TaskFeedbackRequest(
                taskId="R07_FAIL",
                taskType="OUTBOUND",
                status="FAILED",
                startTime="2026-05-31 23:23:44",
                failureReason="",
            )
        self.assertIn("failureReason", str(ctx.exception))

    # Round 08
    def test_round08_outbound_runtime_unknown_sku_allowed_but_inbound_still_rejected(self):
        self.service._clear_all_inventory()
        self.service.sync_inventory(
            [
                {
                    "aisleId": "1",
                    "row": 1,
                    "column": 1,
                    "level": 1,
                    "shelf": "UPPER",
                    "positions": [sku("R08_RUNTIME_UNKNOWN")],
                }
            ]
        )

        out_req = MixedScheduleRequest(
            **{
                "currentTime": "2026-09-10 18:18:51",
                "inventory": [],
                "aisleStatus": [],
                "tasks": [
                    {
                        "taskId": "R08_OUT",
                        "taskType": "OUTBOUND",
                        "planId": "PLAN-LINE1-20260122",
                        "planIndex": 1,
                        "skus": [sku("R08_RUNTIME_UNKNOWN")],
                    }
                ],
            }
        )
        out_resp = self.run_async(mixed_schedule(out_req, warehouse_service=self.service, task_manager=self.manager))
        out_body = self.decode(out_resp)
        self.assertEqual(out_body["status"], "SUCCESS")
        assigned = [
            item["assignedTask"]
            for item in out_body["data"]["aisleAssignments"]
            if item["assignedTask"] is not None
        ]
        self.assertTrue(assigned)
        self.assertEqual(assigned[0]["positions"][0]["skuId"], "R08_RUNTIME_UNKNOWN")

        in_resp = self.run_async(
            allocate_inbound(
                InboundAllocateRequest(
                    tasks=[{"taskId": "R08_IN", "skus": [sku("R08_RUNTIME_UNKNOWN", beamSide="LEFT")]}]
                ),
                warehouse_service=self.service,
                task_manager=self.manager,
            )
        )
        self.assertEqual(in_resp.status_code, 400)
        in_body = self.decode(in_resp)
        self.assertEqual(in_body["data"]["invalidSkus"], ["R08_RUNTIME_UNKNOWN"])

    # Round 09
    def test_round09_same_aisle_executing_exposes_match_preview_without_dispatched_conflict(self):
        self.service._clear_all_inventory()
        self.service.sync_inventory(
            [
                {"aisleId": "1", "row": 1, "column": 2, "level": 2, "shelf": "UPPER", "positions": [sku("2801022-TG152")]},
                {"aisleId": "1", "row": 1, "column": 2, "level": 2, "shelf": "LOWER", "positions": [sku("2801038-TG152")]},
                {"aisleId": "1", "row": 2, "column": 3, "level": 3, "shelf": "UPPER", "positions": [sku("2801021-TG152")]},
                {"aisleId": "1", "row": 2, "column": 3, "level": 3, "shelf": "LOWER", "positions": [sku("2801037-TG152")]},
            ]
        )

        first_req = MixedScheduleRequest(
            **{
                "currentTime": "2026-09-10 18:18:51",
                "inventory": [],
                "aisleStatus": [],
                "tasks": [
                    {
                        "taskId": "R09_OUT_1",
                        "taskType": "OUTBOUND",
                        "planId": "PLAN-LINE1-20260122",
                        "planIndex": 1,
                        "skus": [sku("2801022-TG152"), sku("2801038-TG152")],
                    }
                ],
            }
        )
        first_resp = self.run_async(mixed_schedule(first_req, warehouse_service=self.service, task_manager=self.manager))
        self.assertEqual(self.decode(first_resp)["status"], "SUCCESS")

        self.run_async(
            task_feedback(
                TaskFeedbackRequest(
                    taskId="R09_OUT_1",
                    taskType="OUTBOUND",
                    status="EXECUTING",
                    startTime="2026-05-31 23:23:44",
                    failureReason="",
                ),
                warehouse_service=self.service,
                task_manager=self.manager,
            )
        )

        second_req = MixedScheduleRequest(
            **{
                "currentTime": "2026-09-10 18:18:51",
                "inventory": [],
                "aisleStatus": [],
                "tasks": [
                    {
                        "taskId": "R09_OUT_2",
                        "taskType": "OUTBOUND",
                        "planId": "PLAN-LINE1-20260122",
                        "planIndex": 1,
                        "skus": [sku("2801021-TG152"), sku("2801037-TG152")],
                    }
                ],
            }
        )
        second_resp = self.run_async(mixed_schedule(second_req, warehouse_service=self.service, task_manager=self.manager))
        body = self.decode(second_resp)
        self.assertEqual(body["status"], "SUCCESS")
        aisle_1 = next(item for item in body["data"]["aisleAssignments"] if item["aisleId"] == "1")
        # The aisle is executing R09_OUT_1; we should not dispatch a conflicting new task immediately.
        self.assertIsNotNone(aisle_1["assignedTask"])
        self.assertEqual(aisle_1["assignedTask"]["taskId"], "R09_OUT_1")
        # But we should still preview that R09_OUT_2 matches the same aisle (queue/planning view).
        matched = [t["taskId"] for t in (aisle_1.get("matchedTasks") or [])]
        self.assertIn("R09_OUT_2", matched)

    # Round 10
    def test_round10_bom_update_effective_immediately(self):
        sku_id = f"R10_NEW_SKU__{uuid.uuid4().hex}"

        bad_resp = self.run_async(
            allocate_inbound(
                InboundAllocateRequest(tasks=[{"taskId": "R10_PRE", "skus": [sku(sku_id, beamSide="LEFT")]}]),
                warehouse_service=self.service,
                task_manager=self.manager,
            )
        )
        if not hasattr(bad_resp, "status_code"):
            self.fail(f"expected 400 JSONResponse; got {self.decode(bad_resp)}")
        self.assertEqual(bad_resp.status_code, 400)

        current = self.service.core.config_data
        updated = copy.deepcopy(current)
        updated.setdefault("sku_types", [])
        if sku_id not in updated["sku_types"]:
            updated["sku_types"].append(sku_id)
        updated.setdefault("sku_to_production_line", {})
        updated["sku_to_production_line"][sku_id] = ["1"]
        updated.setdefault("sku_pairs", {})
        updated.setdefault("sku_solo", {})

        ok = self.run_async(update_bom_config(BomUpdateRequest(config=updated), warehouse_service=self.service))
        ok_body = self.decode(ok)
        self.assertEqual(ok_body["status"], "SUCCESS")

        good_resp = self.run_async(
            allocate_inbound(
                InboundAllocateRequest(tasks=[{"taskId": "R10_POST", "skus": [sku(sku_id, beamSide="LEFT")]}]),
                warehouse_service=self.service,
                task_manager=self.manager,
            )
        )
        good_body = self.decode(good_resp)
        self.assertEqual(good_body["status"], "SUCCESS")


if __name__ == "__main__":
    unittest.main()
