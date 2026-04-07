# 仓库调度系统 API 规范文档

## 概述

本文档定义了仓库调度系统的 RESTful API 接口规范，用于与外部系统（WMS/WCS）进行集成。

**基础URL**: `http://{host}:{port}/api/v1`

**数据格式**: JSON

**字符编码**: UTF-8

---

## 1. 混合调度接口

### 1.1 请求调度 (POST /schedule/mixed)

为当前可执行的入库和出库任务进行统一调度，返回巷道分配结果。

#### 请求参数

```json
{
  "tasks": [
    {
      "taskId": "string",           // 必填，任务唯一标识符
      "taskType": "INBOUND|OUTBOUND", // 必填，任务类型
      "targetAisle": "string",      // 入库必填，目标巷道ID
      "planId": "string",           // 出库必填，计划ID
      "planIndex": 1,               // 出库必填，计划下第几组（从1开始）
      "skus": [                     // 必填，货物列表（最多2个SKU）
        {
          "skuId": "string",        // 必填，货物ID
          "quantity": 1             // 必填，货物数量
        }
      ],
      "inboundUrgent": false        // 入库必填，是否紧急入库
    }
  ],
  "aisleStatus": [
    {
      "aisleId": "string",          // 必填，巷道ID
      "isAvailable": true,          // 必填，是否可用
      "unavailableReason": "string", // 不可用时填写原因
      "exitCongestion": [           // 必填，各产线拥堵状态
        {
          "lineId": "string",       // 必填，产线ID
          "isCongested": false      // 必填，是否拥堵
        }
      ],
      "bank": "LEFT|RIGHT"          // 必填，所属库区
    }
  ],
  "inventory": [
    {
      "aisleId": "string",          // 必填，巷道ID
      "row": 1,                     // 必填，行数
      "column": 1,                  // 必填，列数
      "level": 1,                   // 必填，层数
      "shelf": "UPPER|LOWER|null",  // 必填，货架位置
      "positions": [
        {
          "skuId": "string|null",   // 货物ID，空位为null
          "quantity": 1             // 货物数量
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
              "quantity": 1
            }
          ]
        }
      }
    ]
  }
}
```

---

## 2. 任务执行反馈接口

### 2.1 提交任务反馈 (POST /task/feedback)

外部系统报告任务执行状态。

#### 请求参数

```json
{
  "taskId": "string",               // 必填，任务ID
  "taskType": "INBOUND|INBOUND_AISLE|OUTBOUND", // 必填，任务类型
  "status": "EXECUTING|COMPLETED|FAILED", // 必填，任务状态
  "startTime": "2024-01-01T00:00:00Z", // 必填，开始时间
  "failureReason": "string|null"    // 失败原因，成功时为null
}
```

#### 响应参数

```json
{
  "status": "SUCCESS|FAILED",       // 操作状态
  "message": "string",              // 描述信息
  "data": null                      // 业务数据（此接口为null）
}
```

#### 任务状态说明

| 状态 | 说明 |
|------|------|
| EXECUTING | 任务正在执行中 |
| COMPLETED | 任务已完成 |
| FAILED | 任务执行失败 |

---

## 3. 入库分配接口

### 3.1 请求入库巷道分配 (POST /inbound/allocate)

为入库任务分配推荐的目标巷道。

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

## 4. 生产计划接口

### 4.1 设置/更新生产计划 (POST /plan/production)

添加或更新当日生产计划。

#### 请求参数

```json
{
  "operationType": "ADD|UPDATE",    // 必填，操作类型
  "planDate": "2024-01-01 00:00:00", // 必填，计划日期
  "plans": [
    {
      "planId": "string",           // 必填，计划ID
      "lineId": "string",           // 必填，产线ID
      "requiredSkus": [             // 必填，需求货物列表
        {
          "skuId": "string",        // 必填，货物ID
          "quantity": 1             // 必填，需求数量
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
  "data": null                      // 业务数据（此接口为null）
}
```

---

## 5. 错误码说明

| 错误码 | 说明 | 响应格式 |
|--------|------|---------|
| 200 | 请求成功 | `{status: "SUCCESS", message: "...", data: {...}}` |
| 400 | 请求参数错误 | `{status: "FAILED", message: "...", data: null}` |
| 404 | 资源不存在 | `{status: "FAILED", message: "...", data: null}` |
| 409 | 资源冲突（如存在未确认任务） | `{status: "FAILED", message: "...", data: {...}}` |
| 422 | 请求参数验证失败 | `{status: "FAILED", message: "请求参数验证失败", data: {errors: [...]}}` |
| 500 | 服务器内部错误 | `{status: "FAILED", message: "...", data: null}` |

> **所有 HTTP 状态码的响应均遵循统一的 `{status, message, data}` 三字段格式。**

---

## 6. 数据流程

```
外部系统                    API服务                    WarehouseCore
    |                          |                           |
    |-- POST /plan/production->|                           |
    |                          |-- set_production_plan() ->|
    |                          |<- 确认 -------------------|
    |<-- SUCCESS --------------|                           |
    |                          |                           |
    |-- POST /inbound/allocate>|                           |
    |                          |-- allocate_inbound_aisle()|
    |                          |<- 推荐巷道 ---------------|
    |<-- 分配结果 -------------|                           |
    |                          |                           |
    |-- POST /schedule/mixed ->|                           |
    |   (含aisleStatus+inventory)                          |
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

### 6.1 混合调度状态同步详情

每次调用 `POST /schedule/mixed` 时，系统会根据请求中的 `aisleStatus` 和 `inventory` 字段**完全同步**以下 WarehouseCore 内部状态：

#### aisleStatus 同步更新的参数：

| 字段 | 更新的Core参数 | 说明 |
|------|---------------|------|
| `isAvailable` | `blockage_status` | 不可用时阻塞该巷道所有产线 |
| `unavailableReason` | 内部缓存 | 用于调度决策参考 |
| `exitCongestion[].isCongested` | `blockage_status[(aisle, line)]` | 更新各产线拥堵状态 |
| `bank` | 内部缓存 | 用于左右库均衡计算 |

#### inventory 同步更新的参数：

| 字段 | 更新的Core参数 | 说明 |
|------|---------------|------|
| 所有货位数据 | `inventory_positions` | **完全重置**所有货位状态 |
| SKU数量统计 | `current_inventory[aisle][sku]` | 重新计算各巷道SKU数量 |
| SKU位置映射 | `sku_position_index[sku]` | 重建SKU到货位的索引 |

**重要说明**：每次混合调度请求会**完全重置**库存状态，而不是增量更新。这确保了内部状态与外部系统保持一致。

---

## 7. 注意事项

1. **任务反馈机制**: 每次发出调度指令后，必须等待外部系统返回 `EXECUTING` 状态的反馈，才能认为指令已成功传输。

2. **时间同步**: 所有时间戳使用 ISO 8601 格式，建议使用 UTC 时间。

3. **幂等性**: 任务反馈接口支持幂等调用，重复提交相同的反馈不会产生副作用。

4. **并发控制**: 同一巷道同一时间只能执行一个任务，调度时会自动排除占用中的巷道。

