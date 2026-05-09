# Excel Feedback Fix Map (库管系统算法升级接口测试记录_20260419.xlsx)

> 2026-04-23 update: the standalone plan router has been removed from the
> public contract. Plan regressions below are now verified by
> `schedule/mixed` inline `productionPlan/currentGroups` and direct
> `service.core.production_plan` assertions in tests.

目标：让 2026-04-19 Excel 记录里“未关闭”的问题点有可审计的修复证据链（问题 -> 代码改动 -> 回归测试）。

## 计划上传接口

### 序号 4: `POST /api/v1/schedule/mixed` inline `productionPlan` 首次添加计划被覆盖
修复点：
1. 合并逻辑：`schedule/mixed` inline `productionPlan.operationType=ADD` 会把新 plan 与已有 plan merge，而不是覆盖。
2. API 合同：独立 `api.routes.plan` 已从回归入口移除；测试通过 `mixed_schedule` inline plan 和 `service.core.production_plan` 断言计划状态。

回归测试：
1. `test_excel_open_issues_unittest.py::TestExcelOpenIssues::test_production_plan_add_should_not_overwrite_existing_lines`
2. `test_api_10_rounds_unittest.py::Api10RoundsTests::test_round04_plan_add_then_add_preserves_existing_lines`

## 出库执行接口

### 序号 1: `/api/v1/schedule/mixed` 同一巷道 2 个出库任务只返回 1 个匹配
修复点：
1. 新增 `matchedTasks`：`api/models.py:AisleAssignmentResponse` 增加字段 `matchedTasks` 用于“匹配预览/排队展示”。
2. 调度返回同时给出“可执行任务 assignedTask”与“匹配预览 matchedTasks”：`api/routes/schedule.py:mixed_schedule`。
3. 调度侧计算“匹配预览”并返回：`WarehouseService.execute_schedule_with_preview()`。

回归测试：
1. `test_excel_open_issues_unittest.py::TestExcelOpenIssues::test_outbound_two_tasks_same_aisle_should_be_returned_as_matches`

### 序号 2: `/api/v1/schedule/mixed` 巷道 EXECUTING 时新出库任务无法匹配/不分配
修复点：
1. 当巷道存在 running_task 时：仅回 `assignedTask=running_task`，新任务只出现在 `matchedTasks`，不直接下发（避免同巷道并发执行的业务风险）：`api/services/warehouse_service.py:execute_schedule_with_preview`。
2. 同时保证“匹配预览”仍能显示：`api/routes/schedule.py:mixed_schedule`。

回归测试：
1. `test_excel_open_issues_unittest.py::TestExcelOpenIssues::test_outbound_match_should_be_returned_even_if_aisle_has_executing_task`
2. `test_api_10_rounds_unittest.py::Api10RoundsTests::test_round09_same_aisle_executing_exposes_match_preview_without_dispatched_conflict`

## 端到端立体库闭环模拟（自起服务）

覆盖点：
1. 10 轮 INBOUND（下发 -> 409 阻塞 -> EXECUTING -> COMPLETED）
2. OUTBOUND 分组门禁（GP2 必须等待 GP1 COMPLETED）
3. 库存闭环校验：每次 INBOUND COMPLETED 后查 `/api/v1/status` 验证落位；每次 OUTBOUND COMPLETED 后验证扣减

测试文件：
1. `test_api_workflow_simulation_http.py`

## 额外风险回归（饥饿/卡死）

### 多巷道同库存：应选择可下发巷道（避免绑定到被阻塞巷道导致长期无法出库）
测试：
1. `test_api_outbound_multi_aisle_prefer_dispatchable_http.py`
