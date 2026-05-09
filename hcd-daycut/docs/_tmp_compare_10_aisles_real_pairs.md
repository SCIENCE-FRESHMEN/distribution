# 10巷道不同策略仿真对比报告

## 实验摘要

- 目标：比较 10 个巷道下 4 组策略组合的仿真效果。
- 数据窗口：按切日逻辑运行 5 天，`cutoff_hour=4`。
- 随机种子：`42`。
- 仓库配置：`config\warehouse_10_aisles.json`。
- 日志目录：`logs\compare_10_aisles_real_pairs`。
- 图表目录：`visualization\compare_10_aisles_real_pairs`。

## 仿真配置

```bash
C:\Users\Jerry\anaconda3\envs\scip_env\python.exe run.py --warehouse-config C:\Users\Jerry\Desktop\各种项目\十堰wms\正式开始\code\shiyan\config\warehouse_10_aisles.json --random-seed 42 --real-time-days 5 --cutoff-hour 4 --inbound-allocation-strategy <strategy> --inbound-position-strategy <strategy> --scheduler-type <strategy>
```

- 仓库结构：10 巷道、3 条产线、2 行、3 列、18 层、双层货位。
- 10 巷道配置沿用默认 5 巷道参数，并将原禁用位模式对称复制到 6-10 巷道。
- 数据源：`simulation/data/inbound_task_config.json`、`simulation/data/production_plan_config.json`。

## 结果汇总


| 策略                       | 完成任务总数 | 移库总数 | 平均最后出库完成时间(s) | 平均开始货位配对率 | 平均开始梁配对率(含solo) | 最终货位配对率 | 最终梁配对率(含solo) |
| ------------------------ | ------ | ---- | ------------- | --------- | --------------- | ------- | ------------- |
| Baseline                 | 2353   | 208  | 19440.7         | 79.36%    | 79.00%          | 80.92%  | 81.64%        |
| Baseline + Scheduling    | 2353   | 215  | 20105.5       | 80.18%    | 79.61%          | 79.19%  | 80.35%        |
| Allocation+              | 2353   | 186  | 19061.6         | 83.24%    | 82.01%          | 80.92%  | 81.64%        |
| Allocation+ + Scheduling | 2353   | 190  | 18385.6        | 83.27%    | 82.05%          | 83.53%  | 83.59%        |


## 结论

- 完工时间最优：`Allocation+ + Scheduling`。
- 移库成本最优：`Allocation+`。
- 最终配对率最优：`Allocation+ + Scheduling`。
- 如果目标是吞吐与配对率兼顾，优先看同时优化入库与调度的组合；如果目标是减少移库，则优先看移库总数更低的组合。

## 说明与局限

- 本次对比固定随机种子，结果可复现，但仍建议后续增加多随机种子重复实验。
- 当前比较基于仓库默认 JSON 数据，结论代表该数据窗口下的相对效果，不直接等于线上长期平均表现。
- 图表由 `visualize_results.py` 生成，可进一步查看每日趋势与巷道利用分布。

