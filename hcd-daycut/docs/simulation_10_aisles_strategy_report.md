# 10巷道不同策略仿真对比报告

## 实验摘要

- 目标：比较 10 个巷道下 4 组策略组合的仿真效果。
- 数据窗口：按切日逻辑运行 5 天，`cutoff_hour=4`。
- 随机种子：`42`。
- 仓库配置：`config/warehouse_10_aisles.json`。
- 日志目录：`logs/compare_10_aisles`。
- 图表目录：`visualization/compare_10_aisles`。

## 仿真配置

```bash
conda run --no-capture-output -n scip_env python scripts/compare_10_aisles.py --warehouse-config config/warehouse_10_aisles.json --real-time-days 5 --cutoff-hour 4 --random-seed 42
```

- 仓库结构：10 巷道、3 条产线、2 行、3 列、18 层、双层货位。
- 10 巷道配置沿用默认 5 巷道参数，并将原禁用位模式对称复制到 6-10 巷道。
- 数据源：`simulation/data/inbound_task_config.json`、`simulation/data/production_plan_config.json`。

## 结果汇总


| 策略                       | 完成任务总数 | 移库总数 | 平均最后出库完成时间(s) | 平均巷道利用率 | 平均开始货位配对率 | 平均开始梁配对率(含solo) | 最终货位配对率 | 最终梁配对率(含solo) |
| ------------------------ | ------ | ---- | ------------- | ------- | --------- | --------------- | ------- | ------------- |
| Baseline                 | 3215   | 1680 | 39317.5       | 16.8%   | 0.00%     | 0.00%           | 0.00%   | 0.00%         |
| Baseline + Scheduling    | 3352   | 1898 | 44009.4       | 15.6%   | 0.00%     | 0.00%           | 0.00%   | 0.00%         |
| Allocation+              | 3258   | 1798 | 37709.2       | 16.4%   | 0.00%     | 0.00%           | 0.00%   | 0.00%         |
| Allocation+ + Scheduling | 3250   | 1811 | 39159.7       | 15.7%   | 0.00%     | 0.00%           | 0.00%   | 0.00%         |


## 结论

- 完工时间最优：`Allocation+`，5 天平均最后出库完成时间约 `37709.2s`。
- 完成任务数最多：`Baseline + Scheduling`，共完成 `3352` 个任务。
- 移库成本最优：`Baseline`，总移库 `1680` 次。
- 巷道负载最均匀：`Allocation+ + Scheduling`，巷道总耗时标准差均值最低，约 `908.3s`。
- 如果目标是缩短完工时间，优先考虑 `Allocation+`。
- 如果目标是最大化完成任务数，可以考虑 `Baseline + Scheduling`，但其移库成本最高。
- 如果目标是降低移库成本并保持较高利用率，`Baseline` 更稳妥。

## 说明与局限

- 本次对比固定随机种子，结果可复现，但仍建议后续增加多随机种子重复实验。
- 当前环境缺少 `matplotlib`，因此本次未生成图表文件；日志和 Markdown 报告已完整产出。
- 当前比较基于仓库默认 JSON 数据，结论代表该数据窗口下的相对效果，不直接等于线上长期平均表现。

