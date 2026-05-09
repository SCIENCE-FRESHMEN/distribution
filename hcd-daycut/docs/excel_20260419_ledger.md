# Excel Ledger

来源: `库管系统算法升级接口测试记录_20260419.xlsx`

说明: 下表保留 Excel 原始接口路径；当前对外 API 只注册 `/api/v1`，原 `/api/v2`、`/api/v3` 问题已迁移到 `/api/v1` 自动化回归测试覆盖。2026-04-23 起，原 `GET/POST /api/v1/plan/production` 也是历史路径；当前计划回归通过 `POST /api/v1/schedule/mixed` inline `productionPlan/currentGroups` 覆盖，单独 plan API 应返回 404。

| ID | Sheet | 序号 | 接口 | OK/NG | 关闭状态 | 问题点 |
|---|---|---:|---|---|---|---|
| 分配巷道接口#1 | 分配巷道接口 | 1 | /api/v1/inbound/allocate | √ |  |  |
| 分配巷道接口#2 | 分配巷道接口 | 2 | /api/v2/inbound/allocate | ❌ | 已关闭 | 单根梁分两种情况：<br>1、单根梁在左侧，分配时无法使用堆垛机右侧货位的高位<br>2、单根梁在右侧，分配时不能使用堆垛机左侧货位的高位<br><br>当前sku用列表接收无法表达单根梁的位置关系，入库分配货位接口同样存在这个问题 |
| 分配巷道接口#3 | 分配巷道接口 | 3 | /api/v3/inbound/allocate | ❌ | 已关闭 | 未维护在BOM中的sku依然能分配成功 |
| 分配巷道接口#4 | 分配巷道接口 | 4 | /api/v1/inbound/allocate | ❌ | 已关闭 | 库内已有主梁的情况下，副梁未进行配对分配到4号巷道<br>确认是配对逻辑问题还是测试方式有误 |
| 入库执行接口#1 | 入库执行接口 | 1 | /api/v1/schedule/mixed | √ |  |  |
| 入库执行接口#2 | 入库执行接口 | 2 | /api/v2/schedule/mixed | √ | 第一组测试任务未反馈任务状态，报错属于正常 |  |
| 入库执行接口#3 | 入库执行接口 | 3 | /api/v1/task/feedback | √ | 反馈第一组分配任务，开始执行EXECUTING |  |
| 入库执行接口#4 | 入库执行接口 | 4 | /api/v2/schedule/mixed | √ | 第二次结果返回成功 |  |
| 入库执行接口#5 | 入库执行接口 | 5 | /api/v2/schedule/mixed | ❌ | 已关闭 |  |
| 入库执行接口#6 | 入库执行接口 | 6 | /api/v2/schedule/mixed | ❌ | 已关闭 |  |
| 入库执行接口#7 | 入库执行接口 | 7 | /api/v2/schedule/mixed | ❌ | 已关闭 |  |
| 入库执行接口#8 | 入库执行接口 | 8 | /api/v2/schedule/mixed | √ | 分配到5-3-9的高位 |  |
| 入库执行接口#9 | 入库执行接口 | 9 | /api/v2/schedule/mixed | √ | 分配到5-3-9的低位进行配对，符合预期 |  |
| 入库执行接口#10 | 入库执行接口 | 10 | /api/v2/schedule/mixed | √ | 分别分配到3-3-9和4-3-9的高位 |  |
| 入库执行接口#11 | 入库执行接口 | 11 | /api/v2/schedule/mixed | √ | 分别分配到3-3-8和4-3-8的高位 |  |
| 计划上传接口#1 | 计划上传接口 | 1 | POST /api/v1/schedule/mixed inline productionPlan | √ | 独立 plan API 已删除；当前计划随 mixed 请求携带 |  |
| 计划上传接口#2 | 计划上传接口 | 2 | POST /api/v1/schedule/mixed inline productionPlan | √ | GET plan API 已删除；通过 inline request + core state 验证 |  |
| 计划上传接口#3 | 计划上传接口 | 3 | POST /api/v1/schedule/mixed inline productionPlan | √ | 独立 plan API 已删除；当前计划随 mixed 请求携带 |  |
| 计划上传接口#4 | 计划上传接口 | 4 | POST /api/v1/schedule/mixed inline productionPlan | ❌ | 首次添加的计划被覆盖；当前用 inline ADD merge 回归 |  |
| 出库执行接口#1 | 出库执行接口 | 1 | /api/v1/schedule/mixed | ❌ | 提交了一个计划的2组任务，调度只返回匹配了一个任务 |  |
| 出库执行接口#2 | 出库执行接口 | 2 | /api/v1/schedule/mixed | ❌ | 当前出库任务未分配 |  |
| 出库执行接口#3 | 出库执行接口 | 3 | /api/v1/schedule/mixed | √ |  |  |
| 出库执行接口#4 | 出库执行接口 | 4 |  |  |  |  |
