# 仓库调度系统 API 规范

## 概述

本文档描述当前仓库调度系统对外提供的主要 API 行为。当前接口统一以 JSON 作为请求与响应格式。

- 基础 URL：`http://{host}:{port}/api/v1`
- 字符编码：`UTF-8`
- 响应基类：`{status, message, data}`

---

## 0. 部署与启动说明

### 0.1. 环境要求

- Python 3.8 及以上
- 建议在独立虚拟环境中部署
- 主要依赖见项目根目录 `requirements.txt`

安装依赖示例：

```bash
pip install -r requirements.txt
```

### 0.2. 启动方式

项目默认启动入口为 `run_api.py`。

默认启动：

```bash
python run_api.py
```

开发模式（热重载）：

```bash
python run_api.py --reload
```

指定地址与端口：

```bash
python run_api.py --host 0.0.0.0 --port 8000
```

### 0.3. 默认访问地址

- 服务地址：`http://{host}:{port}`
- Swagger 文档：`http://{host}:{port}/docs`
- ReDoc 文档：`http://{host}:{port}/redoc`
- 根路径状态检查：`GET /`

默认情况下：

- `host = 0.0.0.0`
- `port = 8000`

### 0.4. 启动时初始化行为

服务启动时会自动执行以下初始化：

- 初始化仓库调度服务
- 重置任务状态管理器
- 读取 `config/warehouse.json`、`config/sku_config.json` 等启动配置

API 的库存快照、待执行任务、运行中任务和生产计划保存在当前服务进程内；服务重启后，上游应通过 `POST /schedule/mixed` 重新同步当前库存、巷道状态、生产计划及待调度任务。

对应入口位于 `api/main.py` 的应用生命周期初始化逻辑。

### 0.5. 配置与调试说明

- 仓库布局、巷道、货位等基础配置默认读取项目内配置。
- 本文档中的接口说明基于当前内存态服务运行逻辑，重启服务后会重新初始化。
- 若需要本地测试重置接口，可在启动前设置环境变量 `WMS_ENABLE_DEBUG_RESET=1`，此时会开放 `POST /api/v1/debug/reset` 调试接口。

### 0.6. API 日志、滚动与容量上限

`run_api.py` 启动后，Uvicorn 的访问日志和错误日志同时输出到控制台和运行根目录下的 `logs/api.log`。源码运行时运行根目录为项目根目录；目录包运行时为 `hcd-api.exe` 所在目录。

日志配置位于 `config/warehouse.json` 的 `api_logging`：

```jsonc
"api_logging": {
  "enabled": true,
  "directory": "logs",
  "file_name": "api.log",
  "max_file_size_mb": 10,
  "max_total_size_mb": 100,
  "backup_count": 20
}
```

- `max_file_size_mb`：`api.log` 达到该大小后滚动，归档文件命名为 `api.log.1`、`api.log.2` 等。
- `backup_count`：归档数量上限。
- `max_total_size_mb`：当前日志与全部归档的累计容量上限；超过上限时，系统先删除最旧归档，不删除正在写入的 `api.log`。
- 修改 `api_logging` 后必须重启 API 服务才生效。
- 该文件日志记录 Uvicorn 的请求访问和运行错误。业务代码中直接使用 `print()` 输出的诊断信息仍只输出到启动控制台；如需保存该部分，可由部署脚本额外重定向标准输出。

### 0.7 请求与响应字段索引

接口字段已统一收录在本说明书中，可按下列章节直接查阅：

| 接口或数据对象 | 请求字段位置 | 响应字段位置 |
| --- | --- | --- |
| 混合调度 `POST /schedule/mixed` | 1.2 请求结构、1.3 关键字段说明 | 1.5 响应结构 |
| 任务反馈 `POST /task/feedback` | 2.2 请求结构、2.3 字段说明 | 2.5 响应示例与状态规则 |
| 入库巷道分配 `POST /inbound/allocate` | 3.1 请求参数 | 3.1 响应参数 |
| 生产计划 | 4.1 请求参数、`planIndex` 说明 | 生产计划作为 mixed 调度结果约束，不单独返回计划对象 |
| BOM 与 SKU 配置 | 5.3 `POST /bom/update` | 5.3 `GET /bom/config` |
| 待确认任务 `GET /task/unconfirmed` | 无请求体 | 2.6 响应结构与字段说明 |

---

## 1. 混合调度接口

### 1.1 请求调度 (POST /schedule/mixed)

- 方法：`POST /schedule/mixed`
- 用途：同步当前库存、巷道状态、生产计划和任务列表，并返回本轮各巷道的推荐任务、当前可执行任务列表以及未提交的出库任务。

### 1.2 请求结构

```json
{
  "currentTime": "2026-05-24 10:00:00",
  "productionPlan": { // 非必填
    "operationType": "ADD", // ADD/UPDATE
    "planDate": "2026-05-24 09:00:00",
    "plans": [
      {
        "planId": "PLAN-LINE1-20260524",
        "lineId": "LINE-1",
        "planIndex": [
          {
            "requiredSkus": [
              [
                {
                  "skuId": "2801021-TG152",
                  "quantity": 1
                },
                {
                  "skuId": "2801037-TG152",
                  "quantity": 1
                }
              ]
            ]
          }
        ]
      }
    ]
  },
  "currentGroups": {
    "LINE-1": 1
  },
  "inventory": [
    {
      "aisleId": "1",
      "row": 2,
      "column": 3,
      "level": 9,
      "shelf": "LOWER",
      "positions": [
        {
          "skuId": "2801021-TG152",
          "quantity": 1,
          "version": "00",
          "productionAttribute": "D",
          "militaryCivilianMark": "M",
          "salesArea": "N"
        }
      ]
    }
  ],
  "aisleStatus": [
    {
      "aisleId": "1",
      "isAvailable": true,
      "unavailableReason": null,
      "exitCongestion": [
        {
          "lineId": "LINE-1",
          "isCongested": false
        }
      ],
      "bank": "LEFT"
    }
  ],
  "tasks": [
    {
      "taskId": "IN-001",
      "taskType": "INBOUND",
      "targetAisle": "1",
      "inLine": 1,
      "skus": [
        {
          "skuId": "2801038-TG152",
          "quantity": 1,
          "beamSide": "RIGHT",
          "version": "00",
          "productionAttribute": "D",
          "militaryCivilianMark": "M",
          "salesArea": "N"
        }
      ]
    }
  ]
}
```

### 1.3 关键字段说明

**请求字段说明**：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `currentTime` | String | 否 | 当前业务时间，建议使用 `YYYY-MM-DD HH:mm:ss`。 |
| `productionPlan` | Object | 否 | 本次内联生产计划，结构见第 4.1 节。 |
| `currentGroups` | Object / Array | 否 | 对外采用 1 基组号的当前生产组设置。 |
| `inventory` | Array | 是 | 库存全量快照；空数组表示保持当前库存不变。 |
| `aisleStatus` | Array | 是 | 巷道可用状态、出口拥堵和库区信息。 |
| `tasks` | Array | 是 | 本轮提交的入库或出库任务。 |

**`inventory[i]` 字段说明**：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `aisleId` | String / Integer | 是 | 巷道编号。 |
| `row` | Integer | 是 | 货位排号，纵梁库为 `1` 或 `2`。 |
| `column` | Integer | 是 | 货位列号。 |
| `level` | Integer | 是 | 货位层号。 |
| `shelf` | String | 是 | 双层货位层位：`UPPER` 或 `LOWER`。 |
| `positions` | Array | 否 | 当前层位库存；省略或空数组表示空位。 |
| `positions[].skuId` | String | 否 | 在库 SKU 编号。 |
| `positions[].quantity` | Integer | 否 | 在库数量，默认按 `1` 处理。 |
| `positions[].inboundTime` | Number / String | 否 | SKU 实际入库时间；FIFO 启用时用于同条件库存排序。 |
| `positions[].version` 等 | String | 否 | `match_fields` 配置要求的 SKU 属性，例如版本、生产属性、军民标识、销售区域。 |

**`aisleStatus[i]` 字段说明**：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `aisleId` | String / Integer | 是 | 巷道编号。 |
| `isAvailable` | Boolean | 是 | 巷道能否承接新任务。 |
| `unavailableReason` | String / null | 否 | 不可用原因，用于诊断记录。 |
| `exitCongestion` | Array | 否 | 各出库产线的持续拥堵状态。 |
| `exitCongestion[].lineId` | String / Integer | 是 | 产线编号。 |
| `exitCongestion[].isCongested` | Boolean | 是 | 该产线出口是否拥堵。 |
| `bank` | String | 否 | 所属库区，例如 `LEFT`、`RIGHT`，用于库区均衡评价。 |

**`tasks[i]` 字段说明**：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `taskId` | String | 是 | 任务唯一标识；不得与本轮或系统内活动任务重复。 |
| `taskType` | String | 是 | `INBOUND` 或 `OUTBOUND`。 |
| `targetAisle` | String / Integer | 入库建议传入 | 入库偏好巷道；不可行时系统可选择其他合法巷道。 |
| `inLine` | Integer | 否 | 入库线编号；未传按 `1` 处理。 |
| `productionLine` | String / Integer | 出库可选 | 出库任务的显式产线；未传时优先通过 `planId` 识别。 |
| `planId` | String | 出库建议传入 | 出库任务所属生产计划。 |
| `planIndex` | Integer | 出库建议传入 | 所属计划组号，采用 1 基口径。 |
| `skus` | Array | 是 | SKU 列表；纵梁双梁任务通常含两个 SKU。 |
| `skus[].skuId` | String | 是 | SKU 编号，必须存在于当前 BOM。 |
| `skus[].quantity` | Integer | 是 | SKU 数量。 |
| `skus[].beamSide` | String | 单梁建议传入 | 单梁存放侧别，如 `LEFT` 或 `RIGHT`。 |
| `skus[]` 的属性字段 | String | 按配置 | 必须与 `match_fields` 对应，例如 `version`、`productionAttribute`。 |

#### `tasks`

- 入库任务字段：`taskId`、`taskType=INBOUND`、`targetAisle`、`inLine`、`skus`
- 出库任务字段：`taskId`、`taskType=OUTBOUND`、`planId`、`planIndex`、`skus`
- 同一请求内 `taskId` 不允许重复。
- 若提交的 `taskId` 已存在于系统的待执行、执行中或待确认任务中，请求会返回 `400`。
- 入库任务会额外校验 SKU 是否维护在 BOM 中；存在未维护 SKU 时，请求会返回 `400`。

#### `inLine`

- 用于区分同一巷道内不同入库线的入库队列。
- 同一巷道、不同 `inLine` 的队首任务都可以进入候选。
- 同一巷道同一时刻仍只会真正推荐一个任务。
- 未传时按 `1` 处理。

#### `inventory`

- `inventory = []`：保持当前系统库存不变。
- `inventory` 非空：按本次请求内容全量重建库存快照。
- 非空重建只更新库存，不会自动清空任务状态。

#### `aisleStatus`

- 用于更新各巷道是否可承接、是否阻塞、出口是否拥堵等状态。
- mixed 返回的任务推荐会基于本次同步后的巷道状态重新计算。

#### `productionPlan`

- 推荐通过 `productionPlan` 对象传入计划。
- 兼容旧写法：在 mixed 根节点直接传 `plans / operationType / planDate / resetAssigned`。

#### `currentGroups`

- 使用对外 1-based 组号。
- `{"LINE-1": 1}` 表示当前从第 1 组开始。
- 也支持列表形式：

```json
{
  "currentGroups": [
    {
      "lineId": "LINE-1",
      "currentGroup": 1
    }
  ]
}
```

- 兼容旧字段 `productionLineCurrentGroup`。
- 若 `currentGroups` 与 `productionLineCurrentGroup` 同时传入，以 `currentGroups` 为准。
- `currentGroups=0` 会自动按第 1 组处理，并在响应中返回补正提示。
- 若 `currentGroups` 超出该产线计划组上限，返回 `400`，本次更新不生效。

### 1.4 计划更新语义

#### `operationType = ADD`

- 按 `lineId` 将新传入组追加到该产线现有计划末尾。
- 不改已有组内容，保留当前组推进状态。
- 若同一次请求同时传入新增计划对应的出库任务，任务里的 `planIndex` 会按对应 `lineId` 自动续接到追加后的实际组号。

#### `operationType = UPDATE`

- 以替换语义更新对应计划内容。
- 支持可选 `resetAssigned`：
  - `true`：清理 `pending`、`pending_execution`、`running` 以及未确认任务状态。
  - `false` / 未传：仅清理尚未真正下发的 `pending`，保留已下发待确认和执行中任务。

### 1.5 响应结构

```json
{
  "status": "SUCCESS|FAILED",
  "message": "string",
  "data": {
    "scheduleId": "string",
    "timestamp": "2026-05-24T02:00:00Z",
    "aisleAssignments": [
      {
        "aisleId": "1",
        "assignedTask": {
          "taskId": "OUT-001",
          "taskType": "OUTBOUND",
          "planId": "PLAN-LINE1-20260524",
          "planIndex": 1,
          "positions": [
            {
              "row": 1,
              "column": 3,
              "level": 9,
              "shelf": "UPPER",
              "skuId": "2801021-TG152",
              "quantity": 1,
              "version": "00",
              "productionAttribute": "D",
              "militaryCivilianMark": "M",
              "salesArea": "N"
            }
          ]
        },
        "matchedTasks": [
          {
            "taskId": "OUT-001",
            "taskType": "OUTBOUND",
            "planId": "PLAN-LINE1-20260524",
            "planIndex": 1,
            "positions": [
              {
                "row": 1,
                "column": 3,
                "level": 9,
                "shelf": "UPPER",
                "skuId": "2801021-TG152",
                "quantity": 1,
                "version": "00",
                "productionAttribute": "D",
                "militaryCivilianMark": "M",
                "salesArea": "N"
              }
            ]
          }
        ]
      }
    ],
    "executableTasksByAisle": {
      "1": [
        {
          "taskId": "OUT-001",
          "taskType": "OUTBOUND",
          "planId": "PLAN-LINE1-20260524",
          "planIndex": 1,
          "positions": [
            {
              "row": 1,
              "column": 3,
              "level": 9,
              "shelf": "UPPER",
              "skuId": "2801021-TG152",
              "quantity": 1,
              "version": "00",
              "productionAttribute": "D",
              "militaryCivilianMark": "M",
              "salesArea": "N"
            }
          ]
        }
      ]
    },
    "unsubmittedOutboundTasks": [
      {
        "taskId": "OUT-002",
        "taskType": "OUTBOUND",
        "planId": "PLAN-LINE1-20260524",
        "planIndex": 2,
        "productionLine": "LINE-1",
        "skus": [
          {
            "skuId": "2801038-TG152",
            "quantity": 1
          }
        ],
        "reason": "已提交出库任务占用导致当前优先级资源不足",
        "blockedByTaskId": "OUT-001",
        "conflictTaskIds": [
          "OUT-001"
        ]
      }
    ],
    "normalizationNotices": [
      "产线 LINE-1 的 currentGroups=0 已自动按第 1 组处理"
    ]
  }
}
```

**响应字段说明**：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `status` | String | 业务状态，`SUCCESS` 或 `FAILED`。 |
| `message` | String | 本次请求的处理结果说明。 |
| `data.scheduleId` | String | 本轮调度结果唯一标识。 |
| `data.timestamp` | String | 调度结果生成时间。 |
| `data.aisleAssignments` | Array | 按巷道组织的本轮推荐结果。 |
| `aisleAssignments[].aisleId` | String | 巷道编号。 |
| `aisleAssignments[].assignedTask` | Object / null | 该巷道本轮实际推荐或已冻结的任务。 |
| `assignedTask.positions` | Array | 已冻结的推荐执行货位；入库为目标位置，出库为来源位置。 |
| `aisleAssignments[].matchedTasks` | Array | 在该巷道下可行的候选任务，不等于最终下发任务。 |
| `data.executableTasksByAisle` | Object | 以巷道编号为键的可执行任务视图。 |
| `data.unsubmittedOutboundTasks` | Array | 因库存、当前组、资源或预占冲突未进入本轮的出库任务。 |
| `data.normalizationNotices` | Array | 对输入组号、计划等字段的兼容或补正提示。 |

### 1.6 当前调度行为说明

#### `assignedTask`

- 表示该巷道本轮推荐优先执行的任务。
- 若该巷道当前已有 `running` 任务，返回结果会优先显示该 `running` 任务。
- `assignedTask` 不是后续 `EXECUTING` 的唯一入口约束。

#### 任务进入系统后的状态

- 入库任务一旦被 mixed 接收，就会立即分配并固化位置，然后进入对应巷道、对应 `inLine` 的待执行队列。
- 出库任务只有在当前组约束和资源预占检查都成立后，才会带着固化后的 `positions` 进入系统。
- 没有进入系统的出库任务只会出现在 `unsubmittedOutboundTasks` 中，不会出现在 `matchedTasks`、`executableTasksByAisle` 或后续 `unconfirmed` 结果中。

#### `matchedTasks`

- 表示该巷道当前仍然可执行的候选任务列表。
- 这里已经按当前组约束、巷道占用、逻辑可执行性和资源预占做过过滤。
- 不是“只要库存能匹配上就返回”，而是“当前仍然能执行的任务才返回”。

#### `executableTasksByAisle`

- 表示每个巷道当前可执行任务列表。
- 与 `matchedTasks` 使用同一套可执行性结果。
- 该字段用于直接查看 mixed 之后各巷道当前还能执行哪些任务。

#### `unsubmittedOutboundTasks`

- 表示本轮没有进入系统的出库任务。
- 常见原因包括：
  - `库存不足`
  - `同组出库任务库存不足`
  - `已提交出库任务占用导致当前优先级资源不足`
- 可额外返回 `blockedByTaskId`、`conflictTaskIds` 用于说明被哪条已提交任务阻塞。

#### 出库任务组过滤

- `past`：当前组之前的出库任务直接忽略，不进入候选、不进入提交队列。
- `current`：当前组任务在资源和逻辑条件成立时进入当前可执行集合。
- `future`：未来组任务不会直接进入当前可执行集合，只会在资源仍成立时保留为后续待执行。

#### 出库资源预占

- 已进入系统的出库任务会先参与资源预占。
- 预占范围包括：
  - `running`
  - 系统内已有的 `pending` 出库任务
- 新传入的出库任务只能在扣除这些已提交任务后的剩余资源上再判断能否成立。

### 1.7 入库位置固化与预占规则

- 入库任务进入系统时即分配并固化 `positions`。
- 后续再次调用 mixed，不会覆盖已固化的入库位置。
- 分配新入库任务前，会先预占同巷道内已固化位置，避免重复占位。
- 仿真和调度优先读取已固化位置，仅对还没有 `positions` 的任务继续分配。

## 2. 任务执行状态反馈接口

### 2.1 提交任务反馈

- 方法：`POST /task/feedback`
- 用途：上报任务执行状态，并在需要时用实际执行巷道/位置覆盖 mixed 分配结果。

### 2.2 请求结构

```json
{
  "taskId": "string", //必填，任务ID
  "taskType": "INBOUND|OUTBOUND", //必填，任务类型
  "status": "EXECUTING|COMPLETED|FAILED", // 必填，任务状态
  "startTime": "2024-01-01T00:00:00Z", // 必填，开始时间
  "aisleId": "2", // 可选，实际执行巷道ID
  "positions": [  // 可选，实际执行位置
    {
      "row": 4,
      "column": 2,
      "level": 9,
      "shelf": "LOWER",
      "skuId": "2801038-TG152",
      "quantity": 1,
      "features": "00",
    }
  ],
  "failureReason": "string|null"    // 失败原因，成功时为null
}
```

### 2.3 字段说明

- `taskId`：任务 ID
- `taskType`：支持 `INBOUND`、`INBOUND_AISLE`、`OUTBOUND`
- `status`：支持 `EXECUTING`、`COMPLETED`、`FAILED`
- `startTime`：ISO 8601 时间
- `aisleId`：
  - `EXECUTING` 时可选
  - 不传则默认沿用 mixed 分配的巷道
  - 传入则表示使用实际执行巷道覆盖 mixed 结果
- `positions`：
  - `EXECUTING` 时可选
  - 不传则默认沿用 mixed 分配的货位
  - 传入则表示使用实际执行货位覆盖 mixed 结果

### 2.4 EXECUTING 规则

#### 默认执行

- 不传 `aisleId` 和 `positions` 时，沿用 mixed 分配结果。

#### 改巷道执行

- 若传了新的 `aisleId` 且该巷道当前有 executing/running 任务，则反馈失败，任务保持待处理状态。
- 若传了新的 `aisleId` 但未传 `positions`，系统会在新巷道上基于当前真实库存自动重新分配位置。

#### 改位置执行

- 若传了 `positions`，则以反馈中的实际位置为准覆盖 mixed 结果。

#### 重复 EXECUTING

- 若任务已在执行中，再次提交 `EXECUTING` 按幂等处理。
- 接口返回 `SUCCESS`，并给出“重复 EXECUTING 已忽略”的提示信息。

#### 成功响应示例

```json
{
  "status": "SUCCESS",
  "message": "反馈处理成功",
  "data": {
    "taskId": "IN-001",
    "aisleId": "2",
    "positions": [
      {
        "row": 4,
        "column": 2,
        "level": 9,
        "shelf": "LOWER",
        "skuId": "2801038-TG152",
        "quantity": 1
      }
    ]
  }
}
```

#### 失败响应示例

```json
{
  "status": "FAILED",
  "message": "反馈处理失败: IN-001",
  "data": {
    "taskId": "IN-001",
    "reason": "巷道 4 当前有任务正在执行"
  }
}
```

---

### 2.5 任务调整接口（POST /task/adjust）

- 方法：`POST /task/adjust`
- 用途：在 `EXECUTING` 之前调整入库任务的巷道 / SKU / 位置。

请求示例：

```json
{
  "taskId": "INBOUND_A3_L1_001",
  "targetAisle": "2",
  "skus": [
    {
      "skuId": "2801022-TG152",
      "quantity": 1,
      "version": "00",
      "productionAttribute": "D",
      "militaryCivilianMark": "M",
      "salesArea": "N",
      "beamSide": "LEFT"
    }
  ],
  "positions": [
    {
      "row": 7,
      "column": 3,
      "level": 9,
      "shelf": "UPPER",
      "skuId": "2801022-TG152",
      "quantity": 1,
      "version": "00",
      "productionAttribute": "D",
      "militaryCivilianMark": "M",
      "salesArea": "N"
    }
  ]
}
```

规则：

- 改巷道：任务移出原巷道 pending，插入新巷道可执行队首（`taskId`/`inLine` 保持）。
- 改 SKU：按目标巷道当前已固化占位重新分配/校验位置。
- 改 `positions`：会校验与可执行/执行中任务占位冲突，冲突直接失败。
- `positions` 坐标合法性：`row` 必须属于目标 `aisleId` 的外部行号范围。示例：2 巷道仅允许外部 `row=3/4`，传 `row=5` 会直接失败（`positions 无效`），不会自动映射。
- `positions` 冲突判定范围：除 `running` 与当前可执行任务外，还会校验目标巷道内全部 `pending` 入库任务的已分配位置（包括非队首）。
- 冲突错误返回口径：`reason` 中的位置采用外部展示格式 `aisle-externalRow-column-level`，不再返回内部 `position_id`。

### 2.6 查询当前可执行/待执行任务 (GET /task/unconfirmed)

- 方法：`GET /task/unconfirmed`
- 用途：返回各巷道当前的可执行任务、待执行任务和执行中任务。

#### 响应结构

```json
{
  "status": "SUCCESS",
  "message": "获取可执行任务成功",
  "data": {
    "tasksByAisle": {
      "1": {
        "can_executing": [
          {
            "taskId": "OUT11",
            "taskType": "OUTBOUND",
            "aisleId": "1",
            "positions": [
              {
                "row": 1,
                "column": 3,
                "level": 9,
                "shelf": "UPPER",
                "skuId": "2801022-TG152",
                "quantity": 1
              },
              {
                "row": 1,
                "column": 3,
                "level": 9,
                "shelf": "LOWER",
                "skuId": "2801038-TG152",
                "quantity": 1
              }
            ]
          }
        ],
        "pending": [
          {
            "taskId": "OUT21",
            "taskType": "OUTBOUND",
            "aisleId": "1",
            "positions": [
              {
                "row": 2,
                "column": 3,
                "level": 8,
                "shelf": "UPPER",
                "skuId": "2801021-TG152",
                "quantity": 1
              },
              {
                "row": 2,
                "column": 3,
                "level": 8,
                "shelf": "LOWER",
                "skuId": "2801037-TG152",
                "quantity": 1
              }
            ]
          }
        ],
        "running": []
      }
    },
    "canAcceptExecuting": true
  }
}
```

#### 字段说明

- `can_executing`：当前逻辑上可启动的任务。
- `pending`：已经进入系统、但当前还不能启动的任务。
- `running`：当前执行中的任务。
- `canAcceptExecuting`：当前是否至少存在一条 `can_executing` 任务；若所有巷道都没有可启动任务，则返回 `false`。

#### 分桶规则

- 入库任务：只有该巷道对应 `inLine` 的队首任务才会进入 `can_executing`，其余进入 `pending`。
- 出库任务：
  - `past` 组直接忽略，不返回。
  - `current` 组且逻辑条件成立的任务进入 `can_executing`。
  - `future` 组或当前尚不能启动的任务进入 `pending`。
- `running` 单独返回，不与 `can_executing`、`pending` 混放。
- 返回结果会覆盖全部巷道；没有任务的巷道也会返回空数组结构。

## 3. 入库巷道分配接口

### 3.1 请求入库巷道分配

- 方法：`POST /inbound/allocate`
- 用途：为入库任务推荐目标巷道，分配不改变任务状态，不产生入库任务。实际入库任务需要调用混合调度接口创建与分配。

#### 请求参数

```json
{
  "tasks": [
    {
      "taskId": "string",           // 必填，任务ID
      "skus": [                     // 必填，货物列表
        {
          "skuId": "string",        // 必填，货物ID
          "quantity": 1             // 必填，货物数量
          "feature": "string"       // 可选，货物特征
        }
      ]
    }
  ]
}
```

#### 响应参数

```json
{
  "status": "SUCCESS|FAILED",       // 操作状态
  "message": "string",              // 描述信息
  "data": {
    "allocationId": "string",       // 分配操作ID
    "assignments": [
      {
        "taskId": "string",         // 任务ID
        "recommendedAisle": "string" // 推荐巷道ID
      }
    ]
  }
}
```

---

## 4. 生产计划输入

### 4.1 内联生产计划 (POST /schedule/mixed)

生产计划随 `POST /api/v1/schedule/mixed` 内联传入，调度服务以本次请求中的 `productionPlan` 和当前组进度作为最新计划约束。

#### 请求参数

```json
{
  "currentTime": "2026-05-24 10:00:00",
  "productionPlan": {
    "operationType": "ADD | UPDATE",
    "planDate": "2026-05-24 09:00:00",
    "plans": [
      {
        "planId": "string",
        "lineId": "LINE-1",
        "planIndex": [
          {
            "requiredSkus": [
              [
                {
                  "skuId": "2801021-TG152",
                  "quantity": 1
                },
                {
                  "skuId": "2801037-TG152",
                  "quantity": 1
                }
              ]
            ]
          }
        ]
      }
    ]
  },
  "currentGroups": {
    "LINE-1": 1
  },
  "inventory": [],
  "aisleStatus": [],
  "tasks": []
}
```

#### `planIndex` 结构说明

当前 API 结构为：

- `plans[lineId]`
- `planIndex[group]`
- `requiredSkus[task]`
- `sku`

也就是：

- 一个 `plan` 对应一条产线
- 一个 `planIndex` 元素对应一个组
- 一个组里的 `requiredSkus` 对应该组下的多个任务

#### `ADD` 语义

- 按组追加
- 以 `planIndex` 为单位追加到对应 `lineId` 的现有计划末尾
- 会保持当前组指针不变

#### `UPDATE` 语义

- 按替换语义更新对应计划
- 当前组指针会按当前系统逻辑重置
- 如需从指定组继续，请在同一请求中显式传 `currentGroups`

#### `currentGroups`

- 使用 public 1-based 组号
- `0` 会自动按第 1 组处理，并在成功响应的 `data.normalizationNotices` 中给出提示
- 超出当前计划组上限时，返回 `400`
- 越界时，本次计划更新和任务更新都不会生效

---

## 5. 基础状态、任务查询与 BOM 配置

### 5.1 服务状态

- `GET /`：返回服务名称、版本和 OpenAPI 文档入口。
- `GET /api/v1/status`：返回当前进程中的运行任务数量、已完成任务数量、巷道状态、库存汇总和库存明细，适合联调时核验同步结果。

### 5.2 待处理任务查询

`GET /api/v1/task/pending` 返回任务状态管理器与仓库核心队列的合并视图：

- `PENDING_EXECUTION`：已进入入库或出库待执行队列。
- `RUNNING`：已收到 `EXECUTING` 反馈，正在执行。
- `source`：标识任务来自 API 待确认状态、入库队列、出库队列或 `running_tasks`。

`GET /api/v1/task/unconfirmed` 按巷道返回 `can_executing`、`pending` 和 `running` 三类任务；外部系统应仅对 `can_executing` 中的任务提交 `EXECUTING` 反馈。

### 5.3 BOM 与 SKU 配置

#### `POST /bom/update`

更新完整 SKU 配置，请求体为：

```json
{
  "config": {
    "sku_types": ["2801021-H19H0", "2801037-H19H0"],
    "sku_pairs": {
      "2801021-H19H0": "2801037-H19H0",
      "2801037-H19H0": "2801021-H19H0"
    },
    "sku_solo": {},
    "sku_to_production_line": {
      "2801021-H19H0": ["1", "2", "3"],
      "2801037-H19H0": ["1", "2", "3"]
    }
  }
}
```

更新成功后立即作用于后续入库 SKU 校验、双梁配对和产线服务范围判断；不回溯修改现有库存和已提交任务。

#### `GET /bom/config`

返回当前进程正在使用的 `sku_types`、`sku_pairs`、`sku_solo` 与 `sku_to_production_line`，用于核对 `/bom/update` 的实际生效内容。

---

## 6. 错误码说明

| HTTP 状态码 | 含义                                                               | 典型响应                                                                   |
| ----------- | ------------------------------------------------------------------ | -------------------------------------------------------------------------- |
| 200         | 请求成功                                                           | `{status: "SUCCESS", message: "...", data: {...}}`                       |
| 400         | 请求参数错误                                                       | `{status: "FAILED", message: "...", data: null}`                         |
| 404         | 资源不存在                                                         | `{status: "FAILED", message: "...", data: null}`                         |
| 409         | 资源冲突（保留给资源层冲突场景；mixed 未确认任务不再使用该状态码） | `{status: "FAILED", message: "...", data: {...}}`                        |
| 422         | 请求参数验证失败                                                   | `{status: "FAILED", message: "请求参数验证失败", data: {errors: [...]}}` |
| 500         | 服务器内部错误                                                     | `{status: "FAILED", message: "...", data: null}`                         |

> **所有 HTTP 状态码的响应均遵循统一的 `{status, message, data}` 三字段格式。**

---

## 7. 数据流说明

### 7.1 `POST /schedule/mixed`

当前 mixed 调用顺序为：

1. 同步 inline `productionPlan`
2. 同步 `currentGroups` / `productionLineCurrentGroup`
3. 同步 `aisleStatus`
4. 同步 `inventory`
5. 转换 `tasks`
6. 执行统一调度
7. 返回 `aisleAssignments`

```
外部系统                    API服务                    WarehouseCore
    |                          |                           |
    |-- POST /schedule/mixed ->|                           |
    |   (含productionPlan+currentGroups)                    |
    |                          |-- set_production_plan() ->|
    |                          |<- 确认 -------------------|
    |<-- 调度响应 --------------|                           |
    |                          |                           |
    |-- POST /inbound/allocate>|                           |
    |                          |-- allocate_inbound_aisle()|
    |                          |<- 推荐巷道 ---------------|
    |<-- 分配结果 -------------|                           |
    |                          |                           |
    |-- POST /schedule/mixed ->|                           |
    |   (含productionPlan+currentGroups+aisleStatus+inventory)
    |                          |-- sync_aisle_status() --->|  ← 同步巷道状态
    |                          |-- sync_inventory() ------>|  ← 同步库存状态
    |                          |-- decide_for_idle_aisles()|
    |                          |<- 调度结果 ---------------|
    |<-- 调度分配 -------------|                           |
    |                          |                           |
    |-- POST /task/feedback -->|                           |
    |   (EXECUTING)            |-- apply_task_feedback() ->|
    |<-- SUCCESS --------------|                           |
    |                          |                           |
    |-- POST /task/feedback -->|                           |
    |   (COMPLETED)            |-- on_event() ------------>|
    |                          |<- 状态更新 ---------------|
    |<-- SUCCESS --------------|                           |
```

### 7.2 混合调度状态同步详情

每次调用 `POST /schedule/mixed` 时，系统会根据请求中的 `aisleStatus` 和 `inventory` 字段同步 WarehouseCore 内部状态。

#### `aisleStatus` 同步更新的参数

| 字段                             | 更新的 Core 参数                   | 说明                         |
| -------------------------------- | ---------------------------------- | ---------------------------- |
| `isAvailable`                  | `blockage_status`                | 不可用时阻塞该巷道的可执行性 |
| `unavailableReason`            | 内部缓存                           | 用于调度决策参考             |
| `exitCongestion[].isCongested` | `blockage_status[(aisle, line)]` | 更新各产线拥堵状态           |
| `bank`                         | 内部缓存                           | 用于左右库均衡计算           |

##### `inventory` 同步更新的参数

| 场景               | 更新的 Core 参数                  | 说明                           |
| ------------------ | --------------------------------- | ------------------------------ |
| `inventory = []` | 不更新库存                        | 保持当前库存状态不变           |
| `inventory` 非空 | `inventory_positions`           | 按本次传入内容全量重建库存快照 |
| `inventory` 非空 | `current_inventory[aisle][sku]` | 重新计算各巷道 SKU 数量        |
| `inventory` 非空 | `sku_position_index[sku]`       | 重建 SKU 到货位的索引          |

#### `inventory` 同步的重要说明

- 当前 `inventory` 不是增量补丁语义。
- 当 `inventory` 非空时，系统会将本次请求视为最新库存快照。
- 未传到的货位会从库存中移除。
- 该过程只重建库存，不会清空已有任务状态，例如：
  - `running_tasks`
  - `pending_inbound_by_aisle`
  - `pending_outbound_queue`
  - `completed_tasks`
  - `_pending_execution_tasks`

### 7.3 `POST /task/feedback`

当前 feedback 调用顺序为：

1. 根据 `taskId` 找到已下发或执行中的任务
2. 若为 `EXECUTING`：
   - 解析可选的 `aisleId` / `positions`
   - 必要时覆盖 mixed 分配结果
   - 将任务移入 running
3. 若为 `COMPLETED`：
   - 更新任务完成状态
   - 同步库存或推进后续逻辑

---

## 8. 注意事项

1. 同一巷道同一时刻只允许一个 executing/running 任务。
2. mixed 允许重复调用；`assignedTask` 仅作为当前拍推荐结果，不再冻结后续任务切换。
3. `inventory` 非空时按全量快照处理，建议上游传入准确完整的库存视图。
4. `ADD` 适合追加后续组；`UPDATE` 适合替换当前整份计划。
5. `currentGroups` 主要用于初始化、恢复和人工校正；若运行中持续外部覆盖，可能改变自动推进后的当前组。
