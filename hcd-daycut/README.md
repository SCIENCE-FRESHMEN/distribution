# 仓库仿真说明

本项目为事件驱动的仓库仿真框架，包含入库/出库调度、产线计划驱动、移库逻辑、拥堵/磁力吊资源控制、调度器对比实验与结果可视化等功能。

## 目录结构

- `run.py`：单次仿真主入口（事件驱动）
- `run_daily.py`：按天批量仿真
- `simulation/`：仿真核心逻辑（事件处理、库存、任务生成、评分）
- `allocation/`：入库巷道/货位策略
- `schedule/`：调度器（heuristic/optimization）
- `config/warehouse.json`：默认仓库/仿真参数配置
- `visualize_results.py`：单次仿真可视化
- `visualize_daily_results.py`：按天批量结果可视化
- `logs/`：运行日志输出目录

## 依赖环境

- Python 3.8+
- 常用依赖：`numpy`、`matplotlib`、`pytz`、`pandas`、`joblib`、`scikit-learn`

## 快速开始

1) 单次运行（读取默认配置）

```bash
python run.py
```

2) 指定日期配置运行

```bash
python run.py --date-str 20251012 --inbound-config simulation/data/daily/inbound_task_config_20251012.json --plan-config simulation/data/daily/production_plan_config_20251012.json
```

3) 按天批量运行

```bash
python run_daily.py
```

## 核心参数（config/warehouse.json）

以下参数可在 `config/warehouse.json` 配置作为默认值，CLI 参数会覆盖它们：

### 仓库结构参数

- `num_aisles`：巷道数量（默认 5）
- `num_production_lines`：产线数量（默认 3）
- `num_rows`：仓位行数（默认 2）
- `num_columns`：仓位列数（默认 3）
- `num_levels`：仓位层数（默认 18）
- `total_positions`：仓位总数（默认 540）
- `max_beams`：最大可存放纵梁数量（默认 980）
- `use_double_layer`：是否启用双层货位（默认 true）
- `aisle_production_line_mapping`：巷道-产线映射（默认所有巷道可服务所有产线）
- `disabled_positions`：不可用位置列表（入库/移库会自动过滤）

### 不可用位置
- `disabled_positions` 支持以下写法：
  - 位置字符串：`"aisle-row-column-level"`，例如 `1-1-01-01`
  - 位置对象：`{"aisle": 1, "row": 1, "column": 1, "level": 1}`

### 仿真参数

- `initial_inventory_count`：初始化入库组数（默认 200）
- `transport_delay_s`：入库任务从派发到到达入库口时间（秒，默认 30.0）
- `relocation_delay_s`：移库导致无法进行其他任务的时间（秒，默认 80.0）
- `outbound_congestion_time`：出库口拥堵等待时间（秒，默认 10.0）
- `use_magnetic_crane`：是否启用磁力吊（默认 false）
- `blockage_time`：拥堵时间（秒）
- `magnetic_crane_time`：磁力吊作业时间（秒）
- `lr_balance_weight`：左右均衡权重（0-1，默认 0.0）

## CLI 常用参数（run.py）

配置文件未覆盖时，可用 CLI 覆盖：

- `--inbound-rate-lambda`：入库到达泊松间隔参数（默认 1/100.0）
- `--initial-inventory-count`：初始化入库组数
- `--outbound-congestion-time`：出库口拥堵时间
- `--lr-balance-weight`：左右均衡权重
- `--scheduler-type`：`heuristic` 或 `optimization`
- `--inbound-allocation-strategy`：入库巷道策略（`baseline`或 `proposed`）
- `--inbound-position-strategy`：入库货位策略（`baseline`或 `proposed`）
- 评分权重（仅 `optimization`）：
  `--makespan-weight`：最大完工时间权重（默认 0.3；越大越强调整体完工时间）
  `--balance-weight`：库存均衡度变差惩罚权重（默认 0.001；均衡度下降惩罚更大）
  `--production-line-avg-time-weight`：产线平均完成时间权重（默认 0.5；越大越强调产线平均完工时间）
  `--production-line-balance-weight`：产线完成进度均衡权重（默认 0.3；越大越强调产线完成节奏一致）
  `--aisle-dispersion-weight`：巷道负载分散度权重（默认 0.3；越大越强调巷道负载分布均匀）
  `--inbound-wait-weight`：入库任务等待时间惩罚权重（默认 0.01；越大越强调减少入库等待）

示例：

```bash
python run.py --scheduler-type optimization --makespan-weight 0.4 --balance-weight 0.002
```

## 输出与可视化
图表默认输出到 `visualization/compare` 与 `visualization/daily`，可用 `--out-dir` 指定。

运行日志默认在 `logs/` 下生成。可视化工具：

- 单次仿真：`python visualize_results.py`（匹配 `run_compare.py`）
- 按天批量：`python visualize_daily_results.py`（匹配 `run_daily.py`）

当前可视化输出包含：

- 任务完成数量统计
- 移库次数统计
- 巷道利用率与结束时间
- 配对率曲线(仅visualize_results.py)
- 每日“开始配对率”柱状图（单次/按天）
