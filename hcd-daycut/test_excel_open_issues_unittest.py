import asyncio
import unittest

from api.models import MixedScheduleRequest, TaskFeedbackRequest
from api.routes.feedback import task_feedback
from api.routes.schedule import mixed_schedule
from api.services.warehouse_service import get_warehouse_service, reset_warehouse_service
from api.state import get_task_state_manager, reset_task_state_manager


def sku(sku_id: str, qty: int = 1) -> dict:
    # Keep attributes aligned with API schema but don't overfit.
    return {
        "skuId": sku_id,
        "quantity": qty,
        "version": "00",
        "productionAttribute": "D",
        "militaryCivilianMark": "M",
        "salesArea": "N",
    }


def inv_double(aisle_id: str, row: int, col: int, level: int, upper: str, lower: str) -> list[dict]:
    return [
        {
            "aisleId": aisle_id,
            "row": row,
            "column": col,
            "level": level,
            "shelf": "UPPER",
            "positions": [sku(upper)],
        },
        {
            "aisleId": aisle_id,
            "row": row,
            "column": col,
            "level": level,
            "shelf": "LOWER",
            "positions": [sku(lower)],
        },
    ]


def _decode(resp):
    if hasattr(resp, "model_dump"):
        return resp.model_dump()
    return resp


class TestExcelOpenIssues(unittest.TestCase):
    def setUp(self):
        reset_warehouse_service()
        reset_task_state_manager()
        self.service = get_warehouse_service()
        self.tm = get_task_state_manager()

    def tearDown(self):
        reset_warehouse_service()
        reset_task_state_manager()

    def run_async(self, coro):
        return asyncio.run(coro)

    def apply_inline_plan(self, plan_payload: dict):
        return self.run_async(
            mixed_schedule(
                MixedScheduleRequest(
                    currentTime="2026-04-19 09:00:00",
                    productionPlan=plan_payload,
                    productionLineCurrentGroup={"LINE-1": 0, "LINE-2": 0, "LINE-3": 0},
                    inventory=[],
                    aisleStatus=[],
                    tasks=[],
                ),
                warehouse_service=self.service,
                task_manager=self.tm,
            )
        )

    def test_outbound_two_tasks_same_aisle_should_be_returned_as_matches(self):
        # Excel sheet: 出库执行接口 #1
        inventory = []
        inventory += inv_double("2", 3, 3, 9, "2801021-TG152", "2801037-TG152")
        # warehouse.json num_columns=3, so keep column <= 3
        inventory += inv_double("2", 3, 2, 9, "2801038-TG152", "2801022-TG152")

        req = MixedScheduleRequest(
            currentTime="2026-09-10 18:18:51",
            inventory=inventory,
            aisleStatus=[],
            tasks=[
                {
                    "taskId": "OUTBOUND_111111",
                    "taskType": "OUTBOUND",
                    "planId": "PLAN-LINE1-20260122",
                    "planIndex": 1,
                    "skus": [sku("2801021-TG152"), sku("2801037-TG152")],
                },
                {
                    "taskId": "OUTBOUND_222222",
                    "taskType": "OUTBOUND",
                    "planId": "PLAN-LINE1-20260122",
                    "planIndex": 1,
                    "skus": [sku("2801038-TG152"), sku("2801022-TG152")],
                },
            ],
        )
        body = _decode(self.run_async(mixed_schedule(req, warehouse_service=self.service, task_manager=self.tm)))
        self.assertEqual(body["status"], "SUCCESS")

        aisle2 = next(a for a in body["data"]["aisleAssignments"] if a["aisleId"] == "2")
        # The API still selects one "assignedTask" to execute now...
        self.assertIsNotNone(aisle2["assignedTask"])
        self.assertEqual(aisle2["assignedTask"]["taskId"], "OUTBOUND_111111")
        # ...but should also report match results for both tasks in the same aisle.
        matched = [t["taskId"] for t in (aisle2.get("matchedTasks") or [])]
        self.assertEqual(set(matched), {"OUTBOUND_111111", "OUTBOUND_222222"})

    def test_outbound_match_should_be_returned_even_if_aisle_has_executing_task(self):
        # Excel sheet: 出库执行接口 #2
        inventory = []
        inventory += inv_double("2", 3, 3, 9, "2801021-TG152", "2801037-TG152")
        inventory += inv_double("2", 3, 2, 9, "2801038-TG152", "2801022-TG152")

        # 1) schedule first task
        first_req = MixedScheduleRequest(
            currentTime="2026-09-10 18:18:51",
            inventory=inventory,
            aisleStatus=[],
            tasks=[
                {
                    "taskId": "OUTBOUND_111111",
                    "taskType": "OUTBOUND",
                    "planId": "PLAN-LINE1-20260122",
                    "planIndex": 1,
                    "skus": [sku("2801021-TG152"), sku("2801037-TG152")],
                }
            ],
        )
        first_body = _decode(self.run_async(mixed_schedule(first_req, warehouse_service=self.service, task_manager=self.tm)))
        aisle2_first = next(a for a in first_body["data"]["aisleAssignments"] if a["aisleId"] == "2")
        self.assertEqual(aisle2_first["assignedTask"]["taskId"], "OUTBOUND_111111")

        # 2) feedback EXECUTING
        fb = TaskFeedbackRequest(
            taskId="OUTBOUND_111111",
            taskType="OUTBOUND",
            status="EXECUTING",
            startTime="2026-09-10 18:19:00",
            failureReason="",
        )
        fb_body = _decode(self.run_async(task_feedback(fb, warehouse_service=self.service, task_manager=self.tm)))
        self.assertEqual(fb_body["status"], "SUCCESS")

        # 3) schedule next task while the aisle is executing
        second_req = MixedScheduleRequest(
            currentTime="2026-09-10 18:20:00",
            inventory=inventory,
            aisleStatus=[],
            tasks=[
                {
                    "taskId": "OUTBOUND_222222",
                    "taskType": "OUTBOUND",
                    "planId": "PLAN-LINE1-20260122",
                    "planIndex": 1,
                    "skus": [sku("2801038-TG152"), sku("2801022-TG152")],
                }
            ],
        )
        second_body = _decode(self.run_async(mixed_schedule(second_req, warehouse_service=self.service, task_manager=self.tm)))
        aisle2_second = next(a for a in second_body["data"]["aisleAssignments"] if a["aisleId"] == "2")
        # Still executing the first task
        self.assertEqual(aisle2_second["assignedTask"]["taskId"], "OUTBOUND_111111")
        # But should expose the matching result for the new task.
        matched = [t["taskId"] for t in (aisle2_second.get("matchedTasks") or [])]
        self.assertIn("OUTBOUND_222222", matched)

    def test_production_plan_add_should_not_overwrite_existing_lines(self):
        # Excel sheet: 计划上传接口 #4 (GET shows first ADD got overwritten by later ADD)
        def plan_payload(operation: str, line_id: str) -> dict:
            return {
                "operationType": operation,
                "planDate": "2026-04-19 09:00:00",
                "plans": [
                    {
                        "planId": f"PLAN-{line_id}",
                        "lineId": line_id,
                        "planIndex": [
                            {
                                "requiredSkus": [
                                    [sku("2801022-TG152"), sku("2801038-TG152")],
                                    [sku("2801021-TG152"), sku("2801037-TG152")],
                                ],
                            }
                        ],
                    }
                ],
            }

        r1 = _decode(self.apply_inline_plan(plan_payload("ADD", "LINE-1")))
        self.assertEqual(r1["status"], "SUCCESS")

        r2 = _decode(self.apply_inline_plan(plan_payload("ADD", "LINE-2")))
        self.assertEqual(r2["status"], "SUCCESS")

        plan = self.service.core.production_plan
        self.assertIn(1, plan)
        self.assertIn(2, plan)


if __name__ == "__main__":
    unittest.main()
