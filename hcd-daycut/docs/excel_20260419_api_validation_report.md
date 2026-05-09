# 库管系统算法升级接口测试记录_20260419 验收报告

> 2026-04-23 update: entries that mention `GET/POST /api/v1/plan/production`
> are historical Excel source paths. Current regression coverage uses
> `POST /api/v1/schedule/mixed` with inline `productionPlan` and
> `currentGroups`; standalone plan APIs are expected to return 404 and are
> absent from OpenAPI.

来源文件：`库管系统算法升级接口测试记录_20260419.xlsx`

## Excel 基线

用 `tools/extract_excel_cases.py` 重新抽取 Excel，审计口径如下：

| 指标 | 数量 |
|---|---:|
| 总记录数 | 94 |
| Sheet 数 | 5 |
| `OK/NG=❌` 总数 | 9 |
| 未关闭 `❌` | 3 |
| 已关闭但本轮仍纳入回归的 `❌` | 6 |

Sheet 分布：

| Sheet | 记录数 |
|---|---:|
| 分配巷道接口 | 17 |
| 入库执行接口 | 19 |
| 计划上传接口 | 19 |
| 出库执行接口 | 20 |
| BOM同步接口 | 19 |

## Excel NG 修复映射

| Excel Sheet | 序号 | 接口 | 状态 | 问题 | 修改点 | 自动化验证 |
|---|---:|---|---|---|---|---|
| 分配巷道接口 | 2 | `/api/v1/inbound/allocate` | 已修复 | 单梁缺少左右侧信息，无法约束高位侧向 | 单梁必须显式传 `beamSide`；缺失时返回参数错误，传入后按左右侧约束分配 | `test_v1_inbound_allocate_single_beam_requires_explicit_beamside` |
| 分配巷道接口 | 3 | `/api/v1/inbound/allocate` | 已修复 | 未维护在 BOM 的 SKU 仍能分配成功 | 入库分配增加 BOM 校验，返回 `400 FAILED`，包含 `invalidSkus` / `taskIds` | `test_v1_inbound_allocate_unknown_bom_rejected` |
| 分配巷道接口 | 4 | `/api/v1/inbound/allocate` | 已修复 | 库内已有主梁时，副梁未优先配对到 4 号巷道 | 双梁配对候选同时搜索 row1/row2，允许找到已有主梁所在巷道的配对空间 | `test_v1_inbound_allocate_prefers_aisle_with_pair_space` |
| 入库执行接口 | 5 | `/api/v1/schedule/mixed` | 已修复 | `EXECUTING` 后再次调度把已确认任务回退到待确认队列 | 已执行任务保持 running 状态；新任务只进入新的分配流程，不回写旧任务为待确认 | `test_v1_schedule_mixed_executing_task_does_not_block_new_requests` |
| 入库执行接口 | 6 | `/api/v1/schedule/mixed` | 已修复 | 不存在 BOM 的 SKU 能分配成功 | `mixed schedule` 对 `INBOUND` 任务执行 BOM 校验并返回结构化错误 | `test_v1_schedule_mixed_unknown_bom_inbound_rejected` |
| 入库执行接口 | 7 | `/api/v1/schedule/mixed` | 已修复 | 单梁缺少左右侧信息导致高位分配错误 | 单梁必须显式传 `beamSide`；缺失时返回参数错误，传入后策略层接收到明确侧向 | `test_v1_schedule_mixed_single_beam_requires_explicit_beamside` |
| 计划上传接口 | 4 | `POST /api/v1/schedule/mixed` inline `productionPlan` | 已修复 | 第二次 `ADD` 覆盖第一次计划 | 计划随 `schedule/mixed` 请求携带；inline `ADD` 合并已有计划，inline `UPDATE` 替换计划；独立 plan API 已删除 | `test_production_plan_add_should_not_overwrite_existing_lines` |
| 出库执行接口 | 1 | `/api/v1/schedule/mixed` | 已修复 | 同一巷道 2 个出库任务只返回 1 个匹配 | 响应增加 `matchedTasks`，展示当前请求中同巷道可匹配任务预览 | `test_outbound_two_tasks_same_aisle_should_be_returned_as_matches` |
| 出库执行接口 | 2 | `/api/v1/schedule/mixed` | 已修复 | 巷道已有 `EXECUTING` 任务时，新出库任务不返回匹配 | `assignedTask` 保持当前执行任务，新匹配任务进入 `matchedTasks`，避免同巷道并发下发 | `test_outbound_match_should_be_returned_even_if_aisle_has_executing_task` |

## 额外测试覆盖

本轮不仅验证“有没有返回”，还验证响应里的业务值：

| 测试文件 | 覆盖点 |
|---|---|
| `test_api_10_rounds_unittest.py` | 至少 10 轮关键业务回归：计划追加、未确认门禁、同巷道执行中匹配预览等 |
| `test_api_workflow_simulation_http.py` | 自起 HTTP 服务，模拟 10 轮入库闭环、409 门禁、`EXECUTING` / `COMPLETED` 反馈、出库分组门禁 |
| `test_api_inventory_move_and_clear_http.py` | 增量库存移动和空货位清理后，`/api/v1/status` 中旧位为空、新位有货 |
| `test_api_failed_feedback_unblocks_http.py` | `FAILED` 反馈释放未确认/执行状态，新调度可继续 |
| `test_api_outbound_multi_aisle_prefer_dispatchable_http.py` | 多巷道同库存时优先选择可下发巷道，避免绑定到被阻塞巷道 |
| `test_no_deprecation_warnings_unittest.py` | API 入口无 `DeprecationWarning` 回归 |
| `test_mixed_schedule_api.py` | 模型校验、单梁侧向显式校验、调度响应结构等单元级回归 |

关键断言不是只看 HTTP 状态：

- 出库 Excel #1：断言同一巷道 `matchedTasks` 同时包含两个任务 ID，且 `assignedTask` 是当前下发任务。
- 出库 Excel #2：断言 `EXECUTING` 中的旧任务仍是 `assignedTask`，新任务只出现在 `matchedTasks`，不会替换正在执行的任务。
- 单梁 Excel #2/#7：覆盖 `/api/v1/inbound/allocate` 和 `/api/v1/schedule/mixed`；缺失 `beamSide` 会被拒绝，显式 `LEFT/RIGHT` 会按侧向约束分配。
- BOM Excel #3/#6：断言返回 `FAILED`、`invalidSkus` 和对应 `taskIds`，不是只检查 400。
- 立体库 HTTP 流程：断言 409 门禁、反馈状态迁移、库存落位/扣减、分组顺序，而不是只检查成功响应。

## 对应修改

| 文件 | 修改摘要 |
|---|---|
| `api/models.py` | 单梁入库强制校验 `beamSide`；`AisleAssignmentResponse` 增加 `matchedTasks` |
| `api/routes/inbound.py` | 入库分配保持显式 `beamSide` 契约，并保持未知 BOM 拒绝 |
| `api/routes/schedule.py` | 混合调度响应返回 `matchedTasks`；时间戳改为 timezone-aware |
| `api/services/warehouse_service.py` | 调度转换层补齐单梁侧向；执行中任务不回退；库存增量清空后重建索引 |
| `allocation/proposed_strategy.py` | 双梁配对候选同时搜索 row1/row2，修复已有主梁时副梁配对巷道选择 |
| `api/main.py` | 只注册 `/api/v1` 业务路由；`/api/v2`、`/api/v3` 不再作为兼容别名暴露；新增本地测试专用 `/api/v1/debug/reset` |
| `api/state.py` / `api/routes/bom.py` | 时间戳改为 timezone-aware，消除弃用警告 |
| `simulation/__init__.py` / `run.py` | 解除启动路径的循环导入问题 |
| `API_Documentation.md` | 同步 `beamSide` 显式必填、`matchedTasks`、`/api/v1/debug/reset`，并注明当前仅支持 `/api/v1` |

`matchedTasks` 文档约定：

- 无匹配时返回 `null`。
- 包含本次请求里可匹配到该巷道的任务；如果某个任务同时被下发，也会出现在 `assignedTask`。
- 巷道已有历史 `EXECUTING` 任务时，历史任务保持为 `assignedTask`；本次新匹配任务只在 `matchedTasks` 中展示，不会替换当前执行任务。
- 只有 `assignedTask` 表示已下发并进入待确认；仅出现在 `matchedTasks` 的任务是预览，不代表已下发。

## 验证命令记录

```powershell
conda run --no-capture-output -n scip_env python tools\extract_excel_cases.py --all-ng
conda run --no-capture-output -n scip_env python -m unittest -q test_excel_open_issues_unittest.py test_excel_closed_issues_v1_http.py
conda run --no-capture-output -n scip_env python -m unittest -q test_api_10_rounds_unittest.py test_api_workflow_simulation_http.py
conda run --no-capture-output -n scip_env python -m unittest -q test_api_inventory_move_and_clear_http.py test_api_failed_feedback_unblocks_http.py test_api_outbound_multi_aisle_prefer_dispatchable_http.py test_no_deprecation_warnings_unittest.py
conda run --no-capture-output -n scip_env python -m unittest -q test_mixed_schedule_api.py
conda run --no-capture-output -n scip_env python -m unittest discover -s . -p "test_*.py" -q
```

## 验证结果

| 命令 | 结果 |
|---|---|
| `conda run --no-capture-output -n scip_env python tools\extract_excel_cases.py --all-ng` | PASS：抽取到总记录 94、未关闭 NG 3、全部 NG 9 |
| `conda run --no-capture-output -n scip_env python -m unittest -q test_api_10_rounds_unittest.py test_excel_open_issues_unittest.py test_inline_plan_mixed_schedule_api.py` | PASS：`Ran 21 tests in 5.385s OK` |
| `conda run --no-capture-output -n scip_env python -m unittest -q test_excel_closed_issues_v1_http.py` | PASS：`Ran 8 tests in 5.875s OK` |
| `conda run --no-capture-output -n scip_env python -m unittest -q test_api_inventory_move_and_clear_http.py test_api_failed_feedback_unblocks_http.py test_api_outbound_multi_aisle_prefer_dispatchable_http.py test_no_deprecation_warnings_unittest.py test_mixed_schedule_api.py` | PASS：`Ran 19 tests in 16.864s OK` |
| `conda run --no-capture-output -n scip_env python -m unittest -q test_api_workflow_simulation_http.py test_continuous_asrs_100_beams.py` | PASS：`Ran 2 tests in 33.213s OK`; 100 梁连续仿真通过 |
| `conda run --no-capture-output -n scip_env python -m unittest discover -s . -p "test_*.py" -q` | PASS：`Ran 52 tests in 55.705s OK` |

2026-04-23 inline-plan migration evidence:

| 命令 | 结果 |
|---|---|
| `conda run --no-capture-output -n scip_env python -m unittest -q test_inline_plan_mixed_schedule_api.InlinePlanMixedScheduleApiTests.test_public_current_groups_list_missing_group_is_rejected_with_400` | RED first: malformed `currentGroups` raised `TypeError`; after service fix PASS: `Ran 1 test in 0.038s OK` |
| `docs/continuous_asrs_100_beams_report.md` | PASS: processed 100 beams via 152 API calls in 9.395s; 50 inbound accepted and 50 outbound dispatched |

所有测试命令均在 5 分钟内完成，未触发中断。HTTP 测试结束后未发现残留的 `uvicorn` 进程。

## 剩余风险

- `/api/v1/debug/reset` 是自动化测试和本地调试接口；仅在 `WMS_ENABLE_DEBUG_RESET=1` 时注册，生产环境不要开启该变量。
- Excel 中空白占位行不作为缺陷处理；本轮承诺覆盖的是所有 `OK/NG=❌` 的问题行。
