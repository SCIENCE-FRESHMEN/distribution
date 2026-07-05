# 仓库调度系统 API 规范

## 概述

本文档描述当前仓库调度系统对外提供的主要 API 行为。当前接口统一以 JSON 作为请求与响应格式。

- 基础 URL：`http://{host}:{port}/api/v1`
- 字符编码：`UTF-8`
- 响应基类：`{status, message, data}`

---

## 1. 混合调度接口

### 1.1 请求调度 (POST /schedule/mixed)

- 方法：`POST /schedule/mixed`
- 用途：同步计划、库存、巷道状态，并对当前可执行的入库/出库任务统一调度。

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

#### `tasks`

- 入库任务支持：
  - `taskId`
  - `taskType=INBOUND`
  - `targetAisle`
  - `inLine`
  - `skus`
- 出库任务支持：
  - `taskId`
  - `taskType=OUTBOUND`
  - `planId`
  - `planIndex`
  - `skus`

#### `inLine`

- 用于同一巷道下对入库 pending 按入库线分桶。
- 同一巷道、不同 `inLine` 的队头任务都可以参与候选。
- 同一巷道一次仍然只会真正下发一个任务。
- 未传时默认按 `1` 处理。

#### `inventory`

- 当 `inventory = []` 时：保持当前库存状态不变。
- 当 `inventory` 非空时：按本次传入内容全量重建库存快照。
- 非空全量重建只更新库存，不会清空已有任务状态。
- 未传到的货位会被视为本次快照中不存在，并从库存中清除。

#### `productionPlan`

- 推荐通过 `productionPlan` 对象传入计划。
- 兼容在 mixed 根节点直接传 `plans / operationType / planDate / resetAssigned` 的旧写法。

#### 计划补充说明

- 若外部同时传入该批新增计划对应的出库任务，且任务里的 `planIndex` 仍按新增片段内的局部组号填写，系统会按对应 `lineId` 自动续接到追加后的实际组号。
- `resetAssigned = true`：清理未下发 `pending`、已下发待确认 `pending_execution`、`running` 以及 `TaskStateManager` 中的未确认任务。
- `resetAssigned = false` / 未传：只清理尚未真正下发的 `pending`，保留 `pending_execution`、`running` 和未确认任务状态。

#### `currentGroups`

- 推荐使用 public 1-based 组号。
- `{"LINE-1": 1}` 表示当前从第 1 组开始。
- 主要用来指定当前从第几组开始，可以用来跳过某些组。
- 也支持列表形式：

```json
{
  "currentGroups": [
    {
      "lineId": "LINE-1",
      "currentGroup": 1
    },
  ]
}
```

- 兼容旧字段：`productionLineCurrentGroup`
  - 该字段直接使用 core 0-based 索引。
- 若 `currentGroups` 与 `productionLineCurrentGroup` 同时传入，以 `currentGroups` 为准。
- 当 `currentGroups = 0` 时，会自动按第 1 组处理，并在成功响应中返回补正提示。
- 当 `currentGroups` 超出当前计划组上限时，会返回 `400`，且本次计划更新和任务更新都不会生效。

### 1.4 计划更新语义

#### `operationType = ADD`

- 按 `lineId` 将新传入的组追加到现有计划末尾。
- 不会修改已有组的内部任务结构。
- 会保留当前组指针和已完成进度。
- 若外部同时传入该批新增计划对应的出库任务，且 `planIndex` 仍按新增片段内的局部组号填写，系统会按各自 `lineId` 自动续接到现有计划末尾。

#### `operationType = UPDATE`

- 按替换语义更新对应计划内容。
- 当前组指针会按当前系统逻辑重置。
- 支持可选 `resetAssigned`：
  - `true`：清理 `pending`、`pending_execution`、`running` 以及未确认任务状态。
  - `false` / 未传：仅清理尚未下发的 `pending` 队列，保留已下发待确认和 executing/running 任务。
- 若需要从指定组继续调度，建议在同一请求中同步传入 `currentGroups`。

### 1.5 响应结构

```json
{
  "status": "SUCCESS|FAILED",       // 操作状态
  "message": "string",              // 描述信息
  "data": {
    "scheduleId": "string",         // 调度ID
    "timestamp": "2024-01-01T00:00:00Z", // ISO 8601 时间戳
    "aisleAssignments": [
      {
        "aisleId": "string",        // 巷道ID
        "assignedTask": {           // 分配的任务，无分配为null
          "taskId": "string",       // 任务ID
          "taskType": "INBOUND|OUTBOUND", // 任务类型
          "planId": "string",       // 出库任务的计划ID
          "planIndex": 1,           // 计划下第几组
          "positions": [            // 出库任务的货位坐标
        {
          "row": 1,
          "column": 1,
          "level": 1,
          "shelf": "UPPER|LOWER",
          "skuId": "string",
          "quantity": 1,
          "version": "00",
          "productionAttribute": "D",
          "militaryCivilianMark": "M",
          "salesArea": "N"
        }
      ]
    },
    "matchedTasks": null  // 当前该巷道可 EXECUTING 的任务列表（已按可执行性过滤）
      }
    ],
    "normalizationNotices": [ // 规范化提示，一般不会触发
      "产线 1 的 currentGroups=0 已自动按第 1 组处理"
    ]
  }
}
```

### 1.6 当前调度行为说明

#### assignment / EXECUTING

- `assignedTask` 仅表示本次调度推荐结果，不再作为后续 `EXECUTING` 的唯一入口约束。
- `POST /task/feedback` 的 `EXECUTING` 会按当前可执行池校验任务，而不是仅允许 assignment 中的任务。
- INBOUND：仅允许该巷道该 `inLine` 的队首任务启动。
- OUTBOUND：仅允许满足当前产线组约束（`can_start_outbound_task`）且仍在 pending 队列中的任务启动。

#### `assignedTask` 与 `matchedTasks`

- `assignedTask` 表示本次推荐给该巷道的首选任务。
- `matchedTasks` 表示该巷道当前可 `EXECUTING` 的任务列表（已过滤掉“有库存但当前不可执行”的任务）。
- 若该巷道存在本拍 `assignedTask` 且该任务可执行，则会排在 `matchedTasks` 第一位。

#### 重复 `taskId`

- 同一请求体中若存在重复 `taskId`，返回 `400`。
- 若请求中的 `taskId` 已存在于系统中的 pending、running 或待确认任务中，返回 `400`。

失败示例：

```json
{
  "status": "FAILED",
  "message": "存在已在系统中排队或执行的taskId，无法重复提交。",
  "data": {
    "conflictingTaskIds": [
      "111111"
    ],
    "timestamp": "2026-05-24T02:00:00Z"
  }
}
```

---

### 1.7 入库位置固化与预占规则

- 入库任务进入 `pending_inbound_by_aisle` 时即分配并固化 `positions`。
- mixed 后续轮次不再覆盖已固化 `positions`。
- 分配新入库任务位置前，会临时预占同巷道 pending/running 已固化位置，避免重复占位。
- `proposed` 仿真优先读取已固化位置，仅对无 `positions` 任务做模拟分配。

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

## 5. 错误码说明

| HTTP 状态码 | 含义 | 典型响应 |
|---|---|---|
| 200 | 请求成功 | `{status: "SUCCESS", message: "...", data: {...}}` |
| 400 | 请求参数错误 | `{status: "FAILED", message: "...", data: null}` |
| 404 | 资源不存在 | `{status: "FAILED", message: "...", data: null}` |
| 409 | 资源冲突（保留给资源层冲突场景；mixed 未确认任务不再使用该状态码） | `{status: "FAILED", message: "...", data: {...}}` |
| 422 | 请求参数验证失败 | `{status: "FAILED", message: "请求参数验证失败", data: {errors: [...]}}` |
| 500 | 服务器内部错误 | `{status: "FAILED", message: "...", data: null}` |

> **所有 HTTP 状态码的响应均遵循统一的 `{status, message, data}` 三字段格式。**

---

## 6. 数据流说明

### 6.1 `POST /schedule/mixed`

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

### 6.2 混合调度状态同步详情

每次调用 `POST /schedule/mixed` 时，系统会根据请求中的 `aisleStatus` 和 `inventory` 字段同步 WarehouseCore 内部状态。

#### `aisleStatus` 同步更新的参数

| 字段 | 更新的 Core 参数 | 说明 |
|---|---|---|
| `isAvailable` | `blockage_status` | 不可用时阻塞该巷道的可执行性 |
| `unavailableReason` | 内部缓存 | 用于调度决策参考 |
| `exitCongestion[].isCongested` | `blockage_status[(aisle, line)]` | 更新各产线拥堵状态 |
| `bank` | 内部缓存 | 用于左右库均衡计算 |
##### `inventory` 同步更新的参数

| 场景 | 更新的 Core 参数 | 说明 |
|---|---|---|
| `inventory = []` | 不更新库存 | 保持当前库存状态不变 |
| `inventory` 非空 | `inventory_positions` | 按本次传入内容全量重建库存快照 |
| `inventory` 非空 | `current_inventory[aisle][sku]` | 重新计算各巷道 SKU 数量 |
| `inventory` 非空 | `sku_position_index[sku]` | 重建 SKU 到货位的索引 |

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

### 6.3 `POST /task/feedback`

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

## 7. 注意事项

1. 同一巷道同一时刻只允许一个 executing/running 任务。
2. mixed 允许重复调用；`assignedTask` 仅作为当前拍推荐结果，不再冻结后续任务切换。
3. `inventory` 非空时按全量快照处理，建议上游传入准确完整的库存视图。
4. `ADD` 适合追加后续组；`UPDATE` 适合替换当前整份计划。
5. `currentGroups` 主要用于初始化、恢复和人工校正；若运行中持续外部覆盖，可能改变自动推进后的当前组。
