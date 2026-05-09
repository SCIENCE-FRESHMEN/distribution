import asyncio
import unittest
import warnings

from api.models import (
    BomConfigData,
    BomUpdateRequest,
    BeamSide,
    InboundAllocateRequest,
    InboundTaskRequest,
    MixedScheduleRequest,
    SkuInfo,
)
from api.routes.bom import update_bom_config
from api.routes.inbound import allocate_inbound
from api.routes.schedule import mixed_schedule
from api.services.warehouse_service import WarehouseService
from api.state import TaskStateManager


class TestNoDeprecationWarnings(unittest.TestCase):
    def test_no_datetime_utcnow_deprecation_warnings(self):
        """
        Regression guard: Python 3.13 deprecates datetime.utcnow().
        We treat DeprecationWarning as a test failure for API entrypoints.
        """
        warehouse_service = WarehouseService()

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", DeprecationWarning)

            # 1) schedule/mixed: unconfirmed task branch -> timestamp
            task_manager = TaskStateManager()
            task_manager.add_pending_task("OUTBOUND_X1", "OUTBOUND", aisle_id="2")
            req = MixedScheduleRequest(tasks=[], aisleStatus=[], inventory=[])
            resp = asyncio.run(mixed_schedule(req, warehouse_service, task_manager))
            # ensure response materializes
            _ = getattr(resp, "status_code", None)

            # 2) inbound/allocate: invalid SKU branch -> timestamp
            inbound_req = InboundAllocateRequest(
                tasks=[
                    InboundTaskRequest(
                        taskId="ALLOCATE_INVALID_SKU_1",
                        skus=[
                            SkuInfo(
                                skuId="NotInBOM_Sku",
                                quantity=1,
                                beamSide=BeamSide.LEFT,
                                version="00",
                                productionAttribute="D",
                                militaryCivilianMark="M",
                                salesArea="N",
                            )
                        ],
                    )
                ]
            )
            inbound_resp = asyncio.run(allocate_inbound(inbound_req, warehouse_service, task_manager))
            _ = getattr(inbound_resp, "body", None)

            # 3) bom/update: always writes timestamp
            class _StubWarehouseService:
                def update_sku_config(self, config_data):  # pragma: no cover
                    return True

            bom_req = BomUpdateRequest(
                config=BomConfigData(
                    sku_types=[],
                    sku_pairs={},
                    sku_solo={},
                    sku_to_production_line={},
                )
            )
            bom_resp = asyncio.run(update_bom_config(bom_req, _StubWarehouseService()))
            _ = getattr(bom_resp, "data", None)

        dep = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        if dep:
            msgs = [str(w.message) for w in dep]
            self.fail("DeprecationWarning detected:\n" + "\n".join(msgs))


if __name__ == "__main__":
    unittest.main()
