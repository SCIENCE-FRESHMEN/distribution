# Schedule Mixed Inline Plan Upgrade Test Report

Date: 2026-04-23

## Scope

- Removed the standalone public production-plan API.
- Moved dynamic production-plan input into `POST /api/v1/schedule/mixed`.
- Added `currentGroups` as the recommended public 1-based progress field.
- Kept `productionLineCurrentGroup` as a compatibility 0-based field.
- Verified realistic continuous ASRS API usage with 100 beams.

## Code Changes

| Module | Change |
|---|---|
| `api/main.py`, `api/routes/__init__.py`, `api/routes/plan.py` | Removed the public plan router; `/api/v1/plan/production`, `/api/v2/plan/production`, and `/api/v3/plan/production` are not exposed. |
| `api/models.py` | Added inline `productionPlan`, `currentGroups`, and compatibility `productionLineCurrentGroup` fields to `MixedScheduleRequest`. |
| `api/routes/schedule.py` | Applies inline plan/progress before aisle, inventory, and task scheduling; malformed plan progress returns a structured `400 FAILED`. |
| `api/services/warehouse_service.py` | Converts inline plan payloads into core production-plan data, maps `planId` to line, normalizes public `currentGroups`, and preserves old 0-based progress compatibility. |
| `simulation/warehouse_core.py` | Uses request `planIndex`/group metadata for exact current-group dispatch constraints. |
| `API_Documentation.md`, `docs/api_specification.md`, `docs/api_examples.py`, `docs/deployment.md`, `scripts/test_api.py`, `test_api_flow.py` | Updated public examples and endpoint lists to use `schedule/mixed` inline plan instead of standalone plan API. |

## Test Evidence

| Command | Result |
|---|---|
| `conda run --no-capture-output -n scip_env python -m unittest -q test_inline_plan_mixed_schedule_api.InlinePlanMixedScheduleApiTests.test_public_current_groups_list_missing_group_is_rejected_with_400` | RED first: malformed list-form `currentGroups` raised `TypeError`; after fix PASS: `Ran 1 test in 0.038s OK`. |
| `conda run --no-capture-output -n scip_env python -m unittest -q test_api_10_rounds_unittest.py test_excel_open_issues_unittest.py test_inline_plan_mixed_schedule_api.py` | PASS: `Ran 21 tests in 5.385s OK`. |
| `conda run --no-capture-output -n scip_env python -m unittest -q test_excel_closed_issues_v1_http.py` | PASS: `Ran 8 tests in 5.875s OK`. |
| `conda run --no-capture-output -n scip_env python -m unittest -q test_api_inventory_move_and_clear_http.py test_api_failed_feedback_unblocks_http.py test_api_outbound_multi_aisle_prefer_dispatchable_http.py test_no_deprecation_warnings_unittest.py test_mixed_schedule_api.py` | PASS: `Ran 19 tests in 16.864s OK`. |
| `conda run --no-capture-output -n scip_env python -m unittest -q test_api_workflow_simulation_http.py test_continuous_asrs_100_beams.py` | PASS: `Ran 2 tests in 33.213s OK`. |
| `conda run --no-capture-output -n scip_env python -m unittest discover -s . -p "test_*.py" -q` | PASS: `Ran 52 tests in 55.705s OK`. |

## Continuous ASRS Simulation

The continuous simulation used real HTTP API calls against a temporary uvicorn server:

- Processed beams: 100
- Inbound beams accepted: 50
- Outbound beams dispatched: 50
- API calls: 152
- Elapsed seconds: 9.395
- Final status: running

Detailed generated report: `docs/continuous_asrs_100_beams_report.md`.

## Corner Cases Covered

- Removed plan endpoints return 404 and are absent from OpenAPI.
- `currentGroups` public group 1 dispatches group 1 and blocks group 2.
- `currentGroups` public group 2 dispatches group 2.
- Malformed list-form `currentGroups` returns `400 FAILED` instead of leaking a server error.
- Compatibility `productionLineCurrentGroup` still accepts core 0-based progress.
- Dynamic inline `productionPlan` can be replaced on each `schedule/mixed` request.
- Existing Excel open and closed issue regressions still pass under the v1-only API contract.
