# 库管系统算法升级接口测试记录_20260419.xlsx 用例映射

> 2026-04-23 update: plan-upload rows keep the original Excel wording for
> traceability, but the active API contract is `POST /api/v1/schedule/mixed`
> with inline `productionPlan` and `currentGroups`. `GET/POST
> /api/v1/plan/production` are deletion regressions and should return 404.

本文件用于把 Excel 里的接口测试记录（OK/NG）落到可回归的自动化测试上，做到：
`Excel 用例 -> pytest/unittest 测试名 -> 关键断言点 -> 回归门禁`。

当前对外 API 只保留 `/api/v1`。Excel 原始记录里的 `/api/v2`、`/api/v3` 问题已迁移为 `/api/v1` 回归测试覆盖。

## NG 用例（包含“已关闭”记录，按用户要求全部修复）

| Excel Sheet | 序号 | 接口 | 关闭状态 | 问题点摘要 | 对应自动化测试 |
|---|---:|---|---|---|
| 出库执行接口 | 1 | `/api/v1/schedule/mixed` |  | 同一巷道 2 个出库任务，只返回匹配 1 个任务 | `test_excel_open_issues_unittest.py::TestExcelOpenIssues::test_outbound_two_tasks_same_aisle_should_be_returned_as_matches` |
| 出库执行接口 | 2 | `/api/v1/schedule/mixed` |  | 巷道任务 EXECUTING 时，再调度同巷道任务匹配失败/不返回 | `test_excel_open_issues_unittest.py::TestExcelOpenIssues::test_outbound_match_should_be_returned_even_if_aisle_has_executing_task` |
| 计划上传接口 | 4 | `POST /api/v1/schedule/mixed` inline `productionPlan` |  | 第二次 `ADD` 导致首次添加的计划被覆盖；当前通过 inline plan 回归，独立 plan API 删除 | `test_excel_open_issues_unittest.py::TestExcelOpenIssues::test_production_plan_add_should_not_overwrite_existing_lines` |
| 分配巷道接口 | 2 | `/api/v1/inbound/allocate` | 已关闭 | 单梁 beamSide 缺失导致无法区分左右高位使用 | `test_excel_closed_issues_v1_http.py::ExcelClosedIssuesV1HttpTests::test_v1_inbound_allocate_single_beam_requires_explicit_beamside` |
| 分配巷道接口 | 3 | `/api/v1/inbound/allocate` | 已关闭 | 未维护在 BOM 的 sku 仍能分配成功 | `test_excel_closed_issues_v1_http.py::ExcelClosedIssuesV1HttpTests::test_v1_inbound_allocate_unknown_bom_rejected` |
| 分配巷道接口 | 4 | `/api/v1/inbound/allocate` | 已关闭 | 已有主梁库存时，副梁未配对分配到 4 号巷道 | `test_excel_closed_issues_v1_http.py::ExcelClosedIssuesV1HttpTests::test_v1_inbound_allocate_prefers_aisle_with_pair_space` |
| 入库执行接口 | 5 | `/api/v1/schedule/mixed` | 已关闭 | EXECUTING 反馈后不应回退到未确认并阻塞调度 | `test_excel_closed_issues_v1_http.py::ExcelClosedIssuesV1HttpTests::test_v1_schedule_mixed_executing_task_does_not_block_new_requests` |
| 入库执行接口 | 6 | `/api/v1/schedule/mixed` | 已关闭 | 不存在 BOM 的 sku 能分配成功 | `test_excel_closed_issues_v1_http.py::ExcelClosedIssuesV1HttpTests::test_v1_schedule_mixed_unknown_bom_inbound_rejected` |
| 入库执行接口 | 7 | `/api/v1/schedule/mixed` | 已关闭 | 单梁 beamSide 缺失导致左右高位分配错误 | `test_excel_closed_issues_v1_http.py::ExcelClosedIssuesV1HttpTests::test_v1_schedule_mixed_single_beam_requires_explicit_beamside` |

## 运行命令（scip_env）

```powershell
conda run --no-capture-output -n scip_env python -m unittest -q test_excel_open_issues_unittest.py test_excel_closed_issues_v1_http.py
```
