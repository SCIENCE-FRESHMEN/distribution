# 车身立体库 Apifox 测试清单

---

## 1. POST `/api/v1/schedule/mixed`

### 1.1 空 mixed 基础验证

请求体：

```json
{
  "currentTime": "2026-05-24 10:00:00",
  "inventory": [],
  "aisleStatus": [],
  "tasks": []
}
```

预期：

- `status = SUCCESS`
- `data.scheduleId` 存在
- `data.aisleAssignments` 存在
- 默认返回 4 条巷道分配记录为 null

实际响应（当前环境复测，2026-06-08）：

```json
{
    "status": "SUCCESS",
    "message": "调度成功。",
    "data": {
        "scheduleId": "SCH-EDE55471",
        "timestamp": "2026-06-08T02:25:36Z",
        "unconfirmedTaskIds": [],
        "aisleAssignments": [
            {
                "aisleId": "1",
                "assignedTask": null,
                "matchedTasks": []
            },
            {
                "aisleId": "2",
                "assignedTask": null,
                "matchedTasks": []
            },
            {
                "aisleId": "3",
                "assignedTask": null,
                "matchedTasks": []
            },
            {
                "aisleId": "4",
                "assignedTask": null,
                "matchedTasks": []
            }
        ],
        "executableTasksByAisle": {
            "1": [],
            "2": [],
            "3": [],
            "4": []
        },
        "checks": {
            "aisle_forbidden": {
                "rule": "driven by config/warehouse.json aisle_forbidden",
                "checked_count": 0,
                "passed": true,
                "violations": []
            }
        }
    }
}
```

### 1.2 单车身入库

请求体：

```json
{
  "currentTime": "2026-05-24 10:01:00",
  "inventory": [],
  "aisleStatus": [],
  "tasks": [
    {
      "taskId": "IN-001",
      "taskType": "INBOUND",
      "targetAisle": "1",
      "inLine": "L4C1",
      "outLine": "L1C17",
      "skus": [
        {
          "skuId": "1RAT000012",
          "quantity": 1,
          "features": {
            "color": "W1",
            "skid_type": "0",
            "skid_state": "1"
          }
        }
      ]
    }
  ]
}
```

预期：

- `status = SUCCESS`
- `assignedTask.taskId = IN-001`
- `positions` 非空

实际响应（当前环境复测，2026-06-08）：

```json
{
    "status": "SUCCESS",
    "message": "调度成功。",
    "data": {
        "scheduleId": "SCH-24188A5A",
        "timestamp": "2026-06-08T02:25:36Z",
        "unconfirmedTaskIds": [],
        "aisleAssignments": [
            {
                "aisleId": "1",
                "assignedTask": {
                    "taskId": "IN-001",
                    "taskType": "INBOUND",
                    "planId": null,
                    "planIndex": null,
                    "inLine": "L4C1",
                    "outLine": "L1C17",
                    "positions": [
                        {
                            "row": 2,
                            "column": 17,
                            "level": 1,
                            "shelf": null,
                            "skuId": "1RAT000012",
                            "quantity": 1
                        }
                    ]
                },
                "matchedTasks": [
                    {
                        "taskId": "IN-001",
                        "taskType": "INBOUND",
                        "planId": null,
                        "planIndex": null,
                        "aisleId": "1",
                        "inLine": "L4C1",
                        "outLine": "L1C17",
                        "positions": [
                            {
                                "aisleId": "1",
                                "row": 2,
                                "column": 17,
                                "level": 1
                            }
                        ]
                    }
                ]
            },
            {
                "aisleId": "2",
                "assignedTask": null,
                "matchedTasks": []
            },
            {
                "aisleId": "3",
                "assignedTask": null,
                "matchedTasks": []
            },
            {
                "aisleId": "4",
                "assignedTask": null,
                "matchedTasks": []
            }
        ],
        "executableTasksByAisle": {
            "1": [
                {
                    "taskId": "IN-001",
                    "taskType": "INBOUND",
                    "planId": null,
                    "planIndex": null,
                    "aisleId": "1",
                    "inLine": "L4C1",
                    "outLine": "L1C17",
                    "positions": [
                        {
                            "aisleId": "1",
                            "row": 2,
                            "column": 17,
                            "level": 1
                        }
                    ]
                }
            ],
            "2": [],
            "3": [],
            "4": []
        },
        "checks": {
            "aisle_forbidden": {
                "rule": "driven by config/warehouse.json aisle_forbidden",
                "checked_count": 1,
                "passed": true,
                "violations": []
            }
        }
    }
}
```

### 1.3 同一请求内重复 taskId

请求体：

```json
{
  "currentTime": "2026-05-24 10:02:00",
  "inventory": [],
  "aisleStatus": [],
  "tasks": [
    {
      "taskId": "DUP-001",
      "taskType": "INBOUND",
      "targetAisle": "1",
      "inLine": "L4C1",
      "outLine": "L1C17",
      "skus": [
        {
          "skuId": "1RAT000012",
          "quantity": 1,
          "features": {
            "color": "W1",
            "skid_type": "0",
            "skid_state": "1"
          }
        }
      ]
    },
    {
      "taskId": "DUP-001",
      "taskType": "INBOUND",
      "targetAisle": "2",
      "inLine": "L4C1",
      "outLine": "L2C17",
      "skus": [
        {
          "skuId": "1RAT000013",
          "quantity": 1,
          "features": {
            "color": "W1",
            "skid_type": "0",
            "skid_state": "1"
          }
        }
      ]
    }
  ]
}
```

预期：

- HTTP `400`
- `status = FAILED`
- `message` 提示存在重复 `taskId`
- `data.duplicateTaskIds` 返回重复值

实际响应（当前环境复测，2026-06-08）：

```json
{
    "status": "FAILED",
    "message": "存在重复的taskId，无法执行调度。",
    "data": {
        "duplicateTaskIds": [
            "DUP-001"
        ]
    }
}
```

### 1.4 与系统内已有 taskId 冲突

前置：

- 先执行 `1.2`
- 不回 `EXECUTING`

请求体：

```json
{
  "currentTime": "2026-05-24 10:03:00",
  "inventory": [],
  "aisleStatus": [],
  "tasks": [
    {
      "taskId": "IN-001",
      "taskType": "INBOUND",
      "targetAisle": "2",
      "inLine": "L4C1",
      "outLine": "L2C17",
      "skus": [
        {
          "skuId": "1RAT000013",
          "quantity": 1,
          "features": {
            "color": "W1",
            "skid_type": "0",
            "skid_state": "1"
          }
        }
      ]
    }
  ]
}
```

预期：

- HTTP `400`
- `status = FAILED`
- `message` 提示存在已下发或执行中的 `taskId`

实际响应（当前环境复测，2026-06-08）：

```json
{
    "status": "FAILED",
    "message": "存在已下发或执行中的taskId，无法重复提交。",
    "data": {
        "duplicateTaskIds": [
            "IN-001"
        ]
    }
}
```

### 1.5 未确认任务冻结，不切换 assignedTask

前置：

- 先执行 `1.2`
- 不回 `EXECUTING`

请求体：

```json
{
  "currentTime": "2026-05-24 10:01:00",
  "inventory": [],
  "aisleStatus": [],
  "tasks": [
    {
      "taskId": "IN-002",
      "taskType": "INBOUND",
      "targetAisle": "1",
      "inLine": "L4C1",
      "outLine": "L1C17",
      "skus": [
        {
          "skuId": "1RAT000012",
          "quantity": 1,
          "features": {
            "color": "W1",
            "skid_type": "0",
            "skid_state": "1"
          }
        }
      ]
    }
  ]
}
```

预期：

- HTTP `200`
- `status = SUCCESS`
- `data.executableTasksByAisle` 中包含 `IN-001`和`IN-002`


实际响应（当前环境复测，2026-06-08）：

```json
{
    "status": "SUCCESS",
    "message": "调度成功。",
    "data": {
        "scheduleId": "SCH-58F905F5",
        "timestamp": "2026-06-08T02:16:28Z",
        "unconfirmedTaskIds": [
            "IN-001"
        ],
        "aisleAssignments": [
            {
                "aisleId": "1",
                "assignedTask": {
                    "taskId": "IN-001",
                    "taskType": "INBOUND",
                    "planId": null,
                    "planIndex": null,
                    "inLine": "L4C1",
                    "outLine": "L1C17",
                    "positions": [
                        {
                            "row": 2,
                            "column": 17,
                            "level": 1,
                            "shelf": null,
                            "skuId": "1RAT000012",
                            "quantity": 1
                        }
                    ]
                },
                "matchedTasks": [
                    {
                        "taskId": "IN-001",
                        "taskType": "INBOUND",
                        "planId": null,
                        "planIndex": null,
                        "aisleId": "1",
                        "inLine": "L4C1",
                        "outLine": "L1C17",
                        "positions": [
                            {
                                "aisleId": "1",
                                "row": 2,
                                "column": 17,
                                "level": 1
                            }
                        ]
                    },
                    {
                        "taskId": "IN-002",
                        "taskType": "INBOUND",
                        "planId": null,
                        "planIndex": null,
                        "aisleId": "1",
                        "inLine": "L4C1",
                        "outLine": "L1C17",
                        "positions": [
                            {
                                "aisleId": "1",
                                "row": 1,
                                "column": 16,
                                "level": 1
                            }
                        ]
                    }
                ]
            },
            {
                "aisleId": "2",
                "assignedTask": null,
                "matchedTasks": []
            },
            {
                "aisleId": "3",
                "assignedTask": null,
                "matchedTasks": []
            },
            {
                "aisleId": "4",
                "assignedTask": null,
                "matchedTasks": []
            }
        ],
        "executableTasksByAisle": {
            "1": [
                {
                    "taskId": "IN-001",
                    "taskType": "INBOUND",
                    "planId": null,
                    "planIndex": null,
                    "aisleId": "1",
                    "inLine": "L4C1",
                    "outLine": "L1C17",
                    "positions": [
                        {
                            "aisleId": "1",
                            "row": 2,
                            "column": 17,
                            "level": 1
                        }
                    ]
                },
                {
                    "taskId": "IN-002",
                    "taskType": "INBOUND",
                    "planId": null,
                    "planIndex": null,
                    "aisleId": "1",
                    "inLine": "L4C1",
                    "outLine": "L1C17",
                    "positions": [
                        {
                            "aisleId": "1",
                            "row": 1,
                            "column": 16,
                            "level": 1
                        }
                    ]
                }
            ],
            "2": [],
            "3": [],
            "4": []
        },
        "checks": {
            "aisle_forbidden": {
                "rule": "driven by config/warehouse.json aisle_forbidden",
                "checked_count": 1,
                "passed": true,
                "violations": []
            }
        }
    }
}
```

并且在 IN-001 执行完成后，执行一次空的 mixed 请求：
请求体：

```json
{
  "currentTime": "2026-05-24 10:04:00",
  "inventory": [],
  "aisleStatus": [],
  "tasks": []
}
```

预期：
在巷道1调度出 IN-002 任务

实际响应（当前环境复测，2026-06-08）：

```json
{
    "status": "SUCCESS",
    "message": "调度成功。",
    "data": {
        "scheduleId": "SCH-693D0A76",
        "timestamp": "2026-06-08T02:16:29Z",
        "unconfirmedTaskIds": [],
        "aisleAssignments": [
            {
                "aisleId": "1",
                "assignedTask": {
                    "taskId": "IN-002",
                    "taskType": "INBOUND",
                    "planId": null,
                    "planIndex": null,
                    "inLine": "L4C1",
                    "outLine": "L1C17",
                    "positions": [
                        {
                            "row": 1,
                            "column": 16,
                            "level": 1,
                            "shelf": null,
                            "skuId": "1RAT000012",
                            "quantity": 1
                        }
                    ]
                },
                "matchedTasks": [
                    {
                        "taskId": "IN-002",
                        "taskType": "INBOUND",
                        "planId": null,
                        "planIndex": null,
                        "aisleId": "1",
                        "inLine": "L4C1",
                        "outLine": "L1C17",
                        "positions": [
                            {
                                "aisleId": "1",
                                "row": 1,
                                "column": 16,
                                "level": 1
                            }
                        ]
                    }
                ]
            },
            {
                "aisleId": "2",
                "assignedTask": null,
                "matchedTasks": []
            },
            {
                "aisleId": "3",
                "assignedTask": null,
                "matchedTasks": []
            },
            {
                "aisleId": "4",
                "assignedTask": null,
                "matchedTasks": []
            }
        ],
        "executableTasksByAisle": {
            "1": [
                {
                    "taskId": "IN-002",
                    "taskType": "INBOUND",
                    "planId": null,
                    "planIndex": null,
                    "aisleId": "1",
                    "inLine": "L4C1",
                    "outLine": "L1C17",
                    "positions": [
                        {
                            "aisleId": "1",
                            "row": 1,
                            "column": 16,
                            "level": 1
                        }
                    ]
                }
            ],
            "2": [],
            "3": [],
            "4": []
        },
        "checks": {
            "aisle_forbidden": {
                "rule": "driven by config/warehouse.json aisle_forbidden",
                "checked_count": 1,
                "passed": true,
                "violations": []
            }
        }
    }
}
```

### 1.6 inventory 非空时按快照重建

请求体：

```json
{
  "currentTime": "2026-05-24 10:05:00",
  "inventory": [
    {
      "aisleId": "1",
      "row": 1,
      "column": 10,
      "level": 1,
      "positions": [
        {
          "skuId": "1RAT000001",
          "quantity": 1,
          "features": {
            "color": "W1",
            "skid_type": "0",
            "skid_state": "1"
          }
        }
      ]
    },
    {
      "aisleId": "2",
      "row": 3,
      "column": 12,
      "level": 1,
      "positions": [
        {
          "skuId": "1RAT000001",
          "quantity": 1,
          "features": {
            "color": "W1",
            "skid_type": "0",
            "skid_state": "1"
          }
        }
      ]
    }
  ],
  "aisleStatus": [],
  "tasks": []
}
```

预期：

- `status = SUCCESS`
- 后续调度以该快照库存为准，前面两个入库任务的库存被清除
- 调用 `/api/v1/status` 接口查看快照库存

实际响应（当前环境复测，2026-06-08）：

```json
{
    "status": "SUCCESS",
    "message": "调度成功。",
    "data": {
        "scheduleId": "SCH-FC443E38",
        "timestamp": "2026-06-08T02:17:59Z",
        "unconfirmedTaskIds": [],
        "aisleAssignments": [
            { "aisleId": "1", "assignedTask": null, "matchedTasks": [] },
            { "aisleId": "2", "assignedTask": null, "matchedTasks": [] },
            { "aisleId": "3", "assignedTask": null, "matchedTasks": [] },
            { "aisleId": "4", "assignedTask": null, "matchedTasks": [] }
        ],
        "executableTasksByAisle": {
            "1": [],
            "2": [],
            "3": [],
            "4": []
        },
        "checks": {
            "aisle_forbidden": {
                "rule": "driven by config/warehouse.json aisle_forbidden",
                "checked_count": 0,
                "passed": true,
                "violations": []
            }
        }
    }
}
```

当前环境复测说明（2026-06-08）：

- `GET /api/v1/status` 中的 `inventory_summary` 已按本次快照正确重建。
- 当前真实返回里还包含 `service_status`、`current_time`、`completed_tasks_count`、`aisle_status` 以及完整 `inventory` 数组。

`GET /api/v1/status` 关键片段：

```json
{
    "status": "SUCCESS",
    "message": "ok",
    "data": {
        "service_status": "running",
        "current_time": 0.05892038345336914,
        "running_tasks_count": 0,
        "completed_tasks_count": 0,
        "aisle_status": {
            "1": {
                "is_busy": false,
                "blockage": {
                    "1": { "blocked": false, "unblock_time": 0.0 },
                    "2": { "blocked": false, "unblock_time": 0.0 },
                    "3": { "blocked": false, "unblock_time": 0.0 },
                    "4": { "blocked": false, "unblock_time": 0.0 }
                },
                "current_position": null
            }
        },
        "inventory_summary": {
            "1": {
                "1RAT000001": 1
            },
            "2": {
                "1RAT000001": 1
            },
            "3": {},
            "4": {}
        }
    }
}
```

说明：

- 这里保留的是当前真实返回的关键字段；完整返回还会继续展开所有巷道的 `aisle_status`，并附带完整 `inventory` 货位数组。

补充校验 A：相同车身占据 `1/2` 巷道后，再调用 `POST /api/v1/inbound/allocate`，验证新的同款车身不会继续压到 `1/2` 巷道。

请求体：

```json
{
  "tasks": [
    {
      "taskId": "ALLOC-001",
      "inLine": "L4C1",
      "outLine": "L1C17",
      "skus": [
        {
          "skuId": "1RAT000001",
          "quantity": 1,
          "features": {
            "color": "W1",
            "skid_type": "0",
            "skid_state": "1"
          }
        }
      ]
    }
  ]
}
```

预期：

- `status = SUCCESS`
- 若策略避开已有同款车身巷道，应优先推荐到 `3/4` 巷道

实际响应（当前环境复测，2026-06-08）：

```json
{
    "status": "SUCCESS",
    "message": "分配成功",
    "data": {
        "allocationId": "ALLOC-696EA6E7",
        "assignments": [
            {
                "taskId": "ALLOC-001",
                "recommendedAisle": "3"
            }
        ],
        "checks": {
            "aisle_forbidden": {
                "rule": "driven by config/warehouse.json aisle_forbidden",
                "checked_count": 1,
                "passed": true,
                "violations": []
            }
        }
    }
}
```

补充校验 B：在库存快照写入成功后，直接构造两个对应出库任务，验证能否立刻被调度出来。

请求体：

```json
{
  "currentTime": "2026-05-24 10:05:30",
  "inventory": [],
  "aisleStatus": [],
  "tasks": [
    {
      "taskId": "OUT-CHECK-001",
      "taskType": "OUTBOUND",
      "productionLine": 1,
      "outLine": "L1C17",
      "skus": [
        {
          "skuId": "1RAT000001",
          "quantity": 1,
          "features": {
            "color": "W1",
            "skid_type": "0",
            "skid_state": "1"
          }
        }
      ]
    },
    {
      "taskId": "OUT-CHECK-002",
      "taskType": "OUTBOUND",
      "productionLine": 2,
      "outLine": "L1C17",
      "skus": [
        {
          "skuId": "1RAT000001",
          "quantity": 1,
          "features": {
            "color": "W1",
            "skid_type": "0",
            "skid_state": "1"
          }
        }
      ]
    }
  ]
}
```

预期：

- `status = SUCCESS`
- 1/2 巷道的 `aisleAssignments` 中每个存在一个出库任务
- 出库位置应落在刚才写入的库存中

实际响应（当前环境复测，2026-06-08）：

```json
{
    "status": "SUCCESS",
    "message": "调度成功。",
    "data": {
        "scheduleId": "SCH-61862930",
        "timestamp": "2026-06-08T02:17:59Z",
        "unconfirmedTaskIds": [],
        "aisleAssignments": [
            {
                "aisleId": "1",
                "assignedTask": {
                    "taskId": "OUT-CHECK-001",
                    "taskType": "OUTBOUND",
                    "planId": null,
                    "planIndex": null,
                    "inLine": "L1C1",
                    "outLine": "L1C17",
                    "positions": [
                        {
                            "row": 1,
                            "column": 10,
                            "level": 1,
                            "shelf": null,
                            "skuId": "1RAT000001",
                            "quantity": 1
                        }
                    ]
                }
            },
            {
                "aisleId": "2",
                "assignedTask": {
                    "taskId": "OUT-CHECK-002",
                    "taskType": "OUTBOUND",
                    "planId": null,
                    "planIndex": null,
                    "inLine": "L1C1",
                    "outLine": "L1C17",
                    "positions": [
                        {
                            "row": 3,
                            "column": 12,
                            "level": 1,
                            "shelf": null,
                            "skuId": "1RAT000001",
                            "quantity": 1
                        }
                    ]
                }
            },
            {
                "aisleId": "3",
                "assignedTask": null
            },
            {
                "aisleId": "4",
                "assignedTask": null
            }
        ],
        "checks": {
            "aisle_forbidden": {
                "rule": "driven by config/warehouse.json aisle_forbidden",
                "checked_count": 0,
                "passed": true,
                "violations": []
            }
        }
    }
}
```

### 1.7 inventory 为空时保持原库存不变

前置：

- 先执行 `1.6`

请求体：

```json
{
  "currentTime": "2026-05-24 10:06:00",
  "inventory": [],
  "aisleStatus": [],
  "tasks": []
}
```

预期：

- `status = SUCCESS`
- 未传库存快照时，继续沿用上一轮库存，查询库存

实际响应（当前环境复测，2026-06-08）：

```json
{
    "status": "SUCCESS",
    "message": "调度成功。",
    "data": {
        "scheduleId": "SCH-306BEAD2",
        "timestamp": "2026-06-08T02:17:59Z",
        "unconfirmedTaskIds": [
            "OUT-CHECK-001",
            "OUT-CHECK-002"
        ],
        "aisleAssignments": [
            {
                "aisleId": "1",
                "assignedTask": {
                    "taskId": "OUT-CHECK-001",
                    "taskType": "OUTBOUND",
                    "planId": null,
                    "planIndex": null,
                    "inLine": "L1C1",
                    "outLine": "L1C17",
                    "positions": [
                        {
                            "row": 1,
                            "column": 10,
                            "level": 1,
                            "shelf": null,
                            "skuId": "1RAT000001",
                            "quantity": 1
                        }
                    ]
                }
            },
            {
                "aisleId": "2",
                "assignedTask": {
                    "taskId": "OUT-CHECK-002",
                    "taskType": "OUTBOUND",
                    "planId": null,
                    "planIndex": null,
                    "inLine": "L1C1",
                    "outLine": "L1C17",
                    "positions": [
                        {
                            "row": 3,
                            "column": 12,
                            "level": 1,
                            "shelf": null,
                            "skuId": "1RAT000001",
                            "quantity": 1
                        }
                    ]
                }
            },
            {
                "aisleId": "3",
                "assignedTask": null
            },
            {
                "aisleId": "4",
                "assignedTask": null
            }
        ]
    }
}
```

当前环境复测说明（2026-06-08）：

- 当前环境下该结论依然成立，未传 inventory 时会沿用上一轮快照库存。
- 当前真实返回结构与 `1.6` 一致，仍会带上 `service_status`、`current_time`、`completed_tasks_count`、`aisle_status` 以及完整 `inventory` 数组。

`GET /api/v1/status` 关键片段：

```json
{
    "status": "SUCCESS",
    "message": "ok",
    "data": {
        "service_status": "running",
        "current_time": 0.05892038345336914,
        "running_tasks_count": 0,
        "completed_tasks_count": 0,
        "inventory_summary": {
            "1": {
                "1RAT000001": 1
            },
            "2": {
                "1RAT000001": 1
            },
            "3": {},
            "4": {}
        }
    }
}
```

### 1.8 mixed上传 productionPlan

请求体：

```json
{
  "currentTime": "2026-05-24 10:07:00",
  "inventory": [],
  "aisleStatus": [],
  "productionPlan": {
    "operationType": "UPDATE",
    "planDate": "2026-05-24 10:07:00",
    "plans": [
      {
        "planId": "PLAN-LINE1",
        "lineId": "1",
        "planIndex": [
          {
            "requiredSkus": [
              [
                {
                  "skuId": "1RAT000001",
                  "quantity": 1,
                  "features": {
                    "color": "W1",
                    "skid_type": "0",
                    "skid_state": "1"
                  }
                }
              ]
            ]
          }
        ]
      },
      {
        "planId": "PLAN-LINE2",
        "lineId": "2",
        "planIndex": [
          {
            "requiredSkus": [
              [
                {
                  "skuId": "1RAK000005",
                  "quantity": 1,
                  "features": {
                    "color": "W1",
                    "skid_type": "0",
                    "skid_state": "1"
                  }
                }
              ]
            ]
          }
        ]
      }
    ]
  },
  "tasks": []
}
```

预期：

- `status = SUCCESS`
- mixed 内可同步计划
- 后续 `GET /api/v1/plan/production` 可查到

实际响应（当前环境复测，2026-06-08）：

```json
{
    "status": "SUCCESS",
    "message": "调度成功。",
    "data": {
        "scheduleId": "SCH-A233B002",
        "timestamp": "2026-06-08T02:23:03Z",
        "unconfirmedTaskIds": [],
        "aisleAssignments": [
            { "aisleId": "1", "assignedTask": null, "matchedTasks": [] },
            { "aisleId": "2", "assignedTask": null, "matchedTasks": [] },
            { "aisleId": "3", "assignedTask": null, "matchedTasks": [] },
            { "aisleId": "4", "assignedTask": null, "matchedTasks": [] }
        ],
        "executableTasksByAisle": {
            "1": [],
            "2": [],
            "3": [],
            "4": []
        },
        "checks": {
            "aisle_forbidden": {
                "rule": "driven by config/warehouse.json aisle_forbidden",
                "checked_count": 0,
                "passed": true,
                "violations": []
            }
        }
    }
}
```

补充查询 `GET /api/v1/plan/production`：

```json
{
    "status": "SUCCESS",
    "message": "ok",
    "data": {
        "production_plan": {
            "1": [
                [
                    [
                        {
                            "skuId": "1RAT000001"
                        }
                    ]
                ]
            ],
            "2": [
                [
                    [
                        {
                            "skuId": "1RAK000005"
                        }
                    ]
                ]
            ],
            "3": [],
            "4": []
        }
    }
}
```

### 1.9 空滑橇出库

请求体：

```json
{
  "currentTime": "2026-05-24 10:08:00",
  "inventory": [
    {
      "aisleId": "1",
      "row": 1,
      "column": 8,
      "level": 1,
      "positions": [
        {
          "skuId": "EMPTY-SKID-001",
          "quantity": 1,
          "features": {
            "skid_state": "0"
          }
        }
      ]
    },
    {
      "aisleId": "2",
      "row": 3,
      "column": 6,
      "level": 1,
      "positions": [
        {
          "skuId": "EMPTY-SKID-002",
          "quantity": 1,
          "features": {
            "skid_state": "0"
          }
        }
      ]
    }
  ],
  "aisleStatus": [],
  "tasks": [
    {
      "taskId": "OUT-EMPTY-001",
      "taskType": "OUTBOUND",
      "productionLine": 1,
      "outLine": "L1C17",
      "skus": [
        {
          "skuId": "",
          "quantity": 1,
          "features": {
            "skid_state": "0"
          }
        }
      ]
    }
  ]
}
```

预期：

- `status = SUCCESS`
- 能返回实际被选中的空滑橇任务
- 若有多个候选，应优先选择当前可执行且负载较低的巷道

实际响应（当前环境复测，2026-06-08）：

```json
{
    "status": "SUCCESS",
    "message": "调度成功。",
    "data": {
        "scheduleId": "SCH-398A3431",
        "timestamp": "2026-06-08T02:23:03Z",
        "unconfirmedTaskIds": [],
        "aisleAssignments": [
            {
                "aisleId": "1",
                "assignedTask": {
                    "taskId": "OUT-EMPTY-001",
                    "taskType": "OUTBOUND",
                    "planId": null,
                    "planIndex": null,
                    "inLine": "L1C1",
                    "outLine": "L1C17",
                    "positions": [
                        {
                            "row": 1,
                            "column": 8,
                            "level": 1,
                            "shelf": null,
                            "skuId": "EMPTY-SKID-001",
                            "quantity": 1
                        }
                    ]
                },
                "matchedTasks": [
                    {
                        "taskId": "OUT-EMPTY-001",
                        "taskType": "OUTBOUND",
                        "planId": null,
                        "planIndex": null,
                        "aisleId": "1",
                        "inLine": 1,
                        "outLine": "L1C17",
                        "positions": [
                            {
                                "aisleId": "1",
                                "row": 1,
                                "column": 8,
                                "level": 1
                            }
                        ]
                    }
                ]
            },
            {
                "aisleId": "2",
                "assignedTask": null,
                "matchedTasks": []
            },
            {
                "aisleId": "3",
                "assignedTask": null,
                "matchedTasks": []
            },
            {
                "aisleId": "4",
                "assignedTask": null,
                "matchedTasks": []
            }
        ],
        "executableTasksByAisle": {
            "1": [
                {
                    "taskId": "OUT-EMPTY-001",
                    "taskType": "OUTBOUND",
                    "planId": null,
                    "planIndex": null,
                    "aisleId": "1",
                    "inLine": 1,
                    "outLine": "L1C17",
                    "positions": [
                        {
                            "aisleId": "1",
                            "row": 1,
                            "column": 8,
                            "level": 1
                        }
                    ]
                }
            ],
            "2": [],
            "3": [],
            "4": []
        },
        "checks": {
            "aisle_forbidden": {
                "rule": "driven by config/warehouse.json aisle_forbidden",
                "checked_count": 0,
                "passed": true,
                "violations": []
            }
        }
    }
}
```

### 1.10 长滑橇 / 禁配滑橇

请求体：

```json
{
  "currentTime": "2026-05-24 10:09:00",
  "inventory": [],
  "aisleStatus": [],
  "tasks": [
    {
      "taskId": "IN-LONG-001",
      "taskType": "INBOUND",
      "targetAisle": "2",
      "inLine": "L4C1",
      "outLine": "L1C17",
      "skus": [
        {
          "skuId": "1RAT000099",
          "quantity": 1,
          "features": {
            "color": "W1",
            "skid_type": "1",
            "skid_state": "1"
          }
        }
      ]
    }
  ]
}
```

预期：
命中aisle_forbidden，返回错误
改为 4 巷道，入库成功

实际响应（当前环境复测，2026-06-08）：

```json
{
    "status": "FAILED",
    "message": "请求参数校验失败。",
    "data": {
        "stage": "execute_schedule",
        "detail": "任务 IN-LONG-001 指定的 targetAisle=2 不允许当前货物入库。",
        "timestamp": "2026-06-08T02:23:03Z"
    }
}
```

```json
{
    "status": "SUCCESS",
    "message": "调度成功。",
    "data": {
        "scheduleId": "SCH-52967C6E",
        "timestamp": "2026-06-08T02:23:03Z",
        "unconfirmedTaskIds": [],
        "aisleAssignments": [
            {
                "aisleId": "1",
                "assignedTask": null,
                "matchedTasks": []
            },
            {
                "aisleId": "2",
                "assignedTask": null,
                "matchedTasks": []
            },
            {
                "aisleId": "3",
                "assignedTask": null,
                "matchedTasks": []
            },
            {
                "aisleId": "4",
                "assignedTask": {
                    "taskId": "IN-LONG-001",
                    "taskType": "INBOUND",
                    "planId": null,
                    "planIndex": null,
                    "inLine": "L4C1",
                    "outLine": "L1C17",
                    "positions": [
                        {
                            "row": 8,
                            "column": 11,
                            "level": 1,
                            "shelf": null,
                            "skuId": "1RAT000099",
                            "quantity": 1
                        }
                    ]
                },
                "matchedTasks": [
                    {
                        "taskId": "IN-LONG-001",
                        "taskType": "INBOUND",
                        "planId": null,
                        "planIndex": null,
                        "aisleId": "4",
                        "inLine": "L4C1",
                        "outLine": "L1C17",
                        "positions": [
                            {
                                "aisleId": "4",
                                "row": 8,
                                "column": 11,
                                "level": 1
                            }
                        ]
                    }
                ]
            }
        ],
        "executableTasksByAisle": {
            "1": [],
            "2": [],
            "3": [],
            "4": [
                {
                    "taskId": "IN-LONG-001",
                    "taskType": "INBOUND",
                    "planId": null,
                    "planIndex": null,
                    "aisleId": "4",
                    "inLine": "L4C1",
                    "outLine": "L1C17",
                    "positions": [
                        {
                            "aisleId": "4",
                            "row": 8,
                            "column": 11,
                            "level": 1
                        }
                    ]
                }
            ]
        },
        "checks": {
            "aisle_forbidden": {
                "rule": "driven by config/warehouse.json aisle_forbidden",
                "checked_count": 1,
                "passed": true,
                "violations": []
            }
        }
    }
}
```

说明：

- 若触发规则禁用，本次请求整体回滚，包括：
  - inventory 重写不生效
  - mixed 内联上传的 `productionPlan` 不生效
  - mixed 内联传入的 `currentGroups` 不生效
  - 本次调度结果不进入 `pending / unconfirmed / assignedTask`

### 1.11 mixed 出库库存不足时返回未提交任务

前置：先写入两条库存，分别对应 `SKU-A` 和 `SKU-B`。

请求体：

```json
{
  "currentTime": "2026-06-13 10:00:00",
  "inventory": [
    {
      "aisleId": "1",
      "row": 1,
      "column": 10,
      "level": 1,
      "positions": [
        {
          "skuId": "SKU-A",
          "quantity": 1,
          "features": { "color": "W1", "skid_type": "0", "skid_state": "1" }
        }
      ]
    },
    {
      "aisleId": "2",
      "row": 3,
      "column": 10,
      "level": 1,
      "positions": [
        {
          "skuId": "SKU-B",
          "quantity": 1,
          "features": { "color": "W1", "skid_type": "0", "skid_state": "1" }
        }
      ]
    }
  ],
  "aisleStatus": [],
  "tasks": []
}
```

再调用 mixed，下发三条出库任务：

- `OUT-MISS-001`：产线 1，无对应库存
- `OUT-BLOCKED-002`：产线 1，虽然自身有对应库存，但位于缺库存任务之后
- `OUT-OK-003`：产线 2，有对应库存，应正常调度

请求体：

```json
{
  "currentTime": "2026-06-13 10:02:00",
  "inventory": [],
  "aisleStatus": [],
  "tasks": [
    {
      "taskId": "OUT-MISS-001",
      "taskType": "OUTBOUND",
      "productionLine": 1,
      "outLine": "L1C17",
      "skus": [
        {
          "skuId": "SKU-MISS",
          "quantity": 1,
          "features": { "color": "W1", "skid_type": "0", "skid_state": "1" }
        }
      ]
    },
    {
      "taskId": "OUT-BLOCKED-002",
      "taskType": "OUTBOUND",
      "productionLine": 1,
      "outLine": "L1C17",
      "skus": [
        {
          "skuId": "SKU-A",
          "quantity": 1,
          "features": { "color": "W1", "skid_type": "0", "skid_state": "1" }
        }
      ]
    },
    {
      "taskId": "OUT-OK-003",
      "taskType": "OUTBOUND",
      "productionLine": 2,
      "outLine": "L1C17",
      "skus": [
        {
          "skuId": "SKU-B",
          "quantity": 1,
          "features": { "color": "W1", "skid_type": "0", "skid_state": "1" }
        }
      ]
    }
  ]
}
```

预期：

- `status = SUCCESS`
- `OUT-MISS-001` 不进入已提交任务集合
- `OUT-BLOCKED-002` 作为同产线后续任务，被一起拦截且不进入已提交任务集合
- `OUT-OK-003` 正常进入调度结果
- 返回 `unsubmittedOutboundTasks`

实际响应（当前环境复测，2026-06-13）：

```json
{
  "status": "SUCCESS",
  "message": "调度成功。",
  "data": {
    "scheduleId": "SCH-E43E8DA8",
    "timestamp": "2026-06-13T02:44:52Z",
    "unconfirmedTaskIds": [],
    "aisleAssignments": [
      {
        "aisleId": "1",
        "assignedTask": null,
        "matchedTasks": []
      },
      {
        "aisleId": "2",
        "assignedTask": {
          "taskId": "OUT-OK-003",
          "taskType": "OUTBOUND",
          "planId": null,
          "planIndex": null,
          "inLine": "L1C1",
          "outLine": "L1C17",
          "positions": [
            {
              "row": 3,
              "column": 10,
              "level": 1,
              "shelf": null,
              "skuId": "SKU-B",
              "quantity": 1
            }
          ]
        },
        "matchedTasks": [
          {
            "taskId": "OUT-OK-003",
            "taskType": "OUTBOUND",
            "planId": null,
            "planIndex": null,
            "aisleId": "2",
            "inLine": 1,
            "outLine": "L1C17",
            "positions": [
              {
                "aisleId": "2",
                "row": 3,
                "column": 10,
                "level": 1
              }
            ]
          }
        ]
      },
      {
        "aisleId": "3",
        "assignedTask": null,
        "matchedTasks": []
      },
      {
        "aisleId": "4",
        "assignedTask": null,
        "matchedTasks": []
      }
    ],
    "unsubmittedOutboundTasks": [
      {
        "taskId": "OUT-MISS-001",
        "taskType": "OUTBOUND",
        "planId": null,
        "planIndex": null,
        "aisleId": null,
        "inLine": 1,
        "outLine": "L1C17",
        "positions": null,
        "productionLine": 1,
        "reason": "任务 OUT-MISS-001 当前无对应库存，未提交；该产线后续出库任务一并拦截。"
      },
      {
        "taskId": "OUT-BLOCKED-002",
        "taskType": "OUTBOUND",
        "planId": null,
        "planIndex": null,
        "aisleId": null,
        "inLine": 1,
        "outLine": "L1C17",
        "positions": null,
        "productionLine": 1,
        "reason": "产线 1 的任务 OUT-MISS-001 当前无对应库存，本任务未提交。",
        "blockedByTaskId": "OUT-MISS-001"
      }
    ],
    "executableTasksByAisle": {
      "1": [],
      "2": [
        {
          "taskId": "OUT-OK-003",
          "taskType": "OUTBOUND",
          "planId": null,
          "planIndex": null,
          "aisleId": "2",
          "inLine": 1,
          "outLine": "L1C17",
          "positions": [
            {
              "aisleId": "2",
              "row": 3,
              "column": 10,
              "level": 1
            }
          ]
        }
      ],
      "3": [],
      "4": []
    },
    "checks": {
      "aisle_forbidden": {
        "rule": "driven by config/warehouse.json aisle_forbidden",
        "checked_count": 0,
        "passed": true,
        "violations": []
      }
    }
  }
}
```

---

## 2. GET `/api/v1/task/unconfirmed`

### 2.1 查看未确认任务

请求体：

- 无

预期：

- `status = SUCCESS`
- 返回当前未确认任务信息

说明：

- 文档原预期中的 `data.tasks / count / can_accept_new_task` 已不是当前实现返回结构。
- 当前实现返回的是按巷道分组的 `tasksByAisle`，以及布尔值 `canAcceptExecuting`。
- 当前实现的 `/api/v1/task/unconfirmed` 已不再返回 `unconfirmedTaskIds`。

实际响应（当前环境复测，2026-06-08）：

```json
{
    "status": "SUCCESS",
    "message": "ok",
    "data": {
        "tasksByAisle": {
            "1": {
                "can_executing": [
                    {
                        "taskId": "ADJ-001",
                        "taskType": "INBOUND",
                        "planId": null,
                        "planIndex": null,
                        "aisleId": "1",
                        "inLine": "L4C1",
                        "outLine": "L1C17",
                        "positions": [
                            {
                                "aisleId": "1",
                                "row": 2,
                                "column": 17,
                                "level": 1
                            }
                        ]
                    },
                    {
                        "taskId": "ADJ-002",
                        "taskType": "INBOUND",
                        "planId": null,
                        "planIndex": null,
                        "aisleId": "1",
                        "inLine": "L4C1",
                        "outLine": "L1C17",
                        "positions": [
                            {
                                "aisleId": "1",
                                "row": 1,
                                "column": 16,
                                "level": 1
                            }
                        ]
                    }
                ],
                "pending": [],
                "running": []
            },
            "2": {
                "can_executing": [],
                "pending": [],
                "running": []
            },
            "3": {
                "can_executing": [],
                "pending": [],
                "running": []
            },
            "4": {
                "can_executing": [],
                "pending": [],
                "running": []
            }
        },
        "canAcceptExecuting": true
    }
}
```

### 2.2 查看待处理任务（包含真实出库 pending / running）

前置步骤 A：先写入一条可出库库存。

请求体：

```json
{
  "currentTime": "2026-06-13 11:00:00",
  "inventory": [
    {
      "aisleId": "1",
      "row": 1,
      "column": 10,
      "level": 1,
      "positions": [
        {
          "skuId": "SKU-A",
          "quantity": 1,
          "features": { "color": "W1", "skid_type": "0", "skid_state": "1" }
        }
      ]
    }
  ],
  "aisleStatus": [],
  "tasks": []
}
```

前置步骤 B：下发一条出库任务。

请求体：

```json
{
  "currentTime": "2026-06-13 11:01:00",
  "inventory": [],
  "aisleStatus": [],
  "tasks": [
    {
      "taskId": "OUT-PENDING-001",
      "taskType": "OUTBOUND",
      "productionLine": 1,
      "outLine": "L1C17",
      "skus": [
        {
          "skuId": "SKU-A",
          "quantity": 1,
          "features": { "color": "W1", "skid_type": "0", "skid_state": "1" }
        }
      ]
    }
  ]
}
```

步骤 C：先查询 `GET /api/v1/task/pending`。

预期：

- `status = SUCCESS`
- 能看到出库任务 `OUT-PENDING-001`
- 任务来源至少应能反映它来自真实待处理集合，而不是仅来自 `TaskStateManager`

实际响应（当前环境复测，2026-06-13）：

```json
{
  "status": "SUCCESS",
  "message": "ok",
  "data": {
    "count": 1,
    "tasks": [
      {
        "task_id": "OUT-PENDING-001",
        "task_type": "OUTBOUND",
        "aisle_id": "1",
        "status": "PENDING",
        "created_at": "2026-06-13T02:54:49.865307",
        "confirmed_at": null,
        "is_timeout": false,
        "source": "task_manager"
      }
    ]
  }
}
```

步骤 D：对该出库任务发 `EXECUTING` 后，再查询 `GET /api/v1/task/pending`。

请求体：

```json
{
  "taskId": "OUT-PENDING-001",
  "taskType": "OUTBOUND",
  "status": "EXECUTING",
  "startTime": "2026-06-13T11:12:00Z"
}
```

预期：

- `status = SUCCESS`
- 再次查询时，任务状态应体现为真实执行中
- `source` 应优先反映真实运行态

实际响应（当前环境复测，2026-06-13）：

```json
{
  "status": "SUCCESS",
  "message": "ok",
  "data": {
    "count": 1,
    "tasks": [
      {
        "task_id": "OUT-PENDING-001",
        "task_type": "OUTBOUND",
        "aisle_id": "1",
        "status": "EXECUTING",
        "created_at": null,
        "confirmed_at": null,
        "is_timeout": false,
        "source": "running_tasks"
      }
    ]
  }
}
```

---

## 3. POST `/api/v1/task/feedback`

### 3.1 EXECUTING 成功

前置：

- 先执行以下mixed请求：

请求体：

```json
{
  "currentTime": "2026-05-24 10:01:00",
  "inventory": [],
  "aisleStatus": [],
  "tasks": [
    {
      "taskId": "IN-001",
      "taskType": "INBOUND",
      "targetAisle": "1",
      "inLine": "L4C1",
      "outLine": "L1C17",
      "skus": [
        {
          "skuId": "1RAT000012",
          "quantity": 1,
          "features": {
            "color": "W1",
            "skid_type": "0",
            "skid_state": "1"
          }
        }
      ]
    },
    {
      "taskId": "IN-002",
      "taskType": "INBOUND",
      "targetAisle": "2",
      "inLine": "L4C1",
      "outLine": "L1C17",
      "skus": [
        {
          "skuId": "1RAT000012",
          "quantity": 1,
          "features": {
            "color": "W1",
            "skid_type": "0",
            "skid_state": "1"
          }
        }
      ]
    },
    {
      "taskId": "IN-003",
      "taskType": "INBOUND",
      "targetAisle": "3",
      "inLine": "L4C1",
      "outLine": "L1C17",
      "skus": [
        {
          "skuId": "1RAT000012",
          "quantity": 1,
          "features": {
            "color": "W1",
            "skid_type": "0",
            "skid_state": "1"
          }
        }
      ]
    }
  ]
}
```

再执行EXECUTING:

```json
{
  "taskId": "IN-001",
  "taskType": "INBOUND",
  "status": "EXECUTING",
  "startTime": "2026-05-24T10:11:00Z"
}
```

预期：

- `status = SUCCESS`
- 返回实际生效的 `aisleId`
- 返回实际生效的 `positions`

实际响应（当前环境复测，2026-06-08）：

```json
{
    "status": "SUCCESS",
    "message": "任务开始执行。",
    "data": {
        "taskId": "IN-001",
        "status": "EXECUTING",
        "aisleId": "1",
        "positions": [
            {
                "aisleId": "1",
                "row": 2,
                "column": 17,
                "level": 1
            }
        ],
        "reason": null
    }
}
```

### 3.2 COMPLETED 成功

请求体：

```json
{
  "taskId": "IN-001",
  "taskType": "INBOUND",
  "status": "COMPLETED",
  "startTime": "2026-05-24T10:12:00Z"
}
```

预期：

- `status = SUCCESS`
- `/api/v1/status` 中库存增加

实际响应（当前环境复测，2026-06-08）：

```json
{
    "status": "SUCCESS",
    "message": "任务执行完成。",
    "data": {
        "taskId": "IN-001",
        "status": "COMPLETED",
        "aisleId": "1",
        "positions": [
            {
                "aisleId": "1",
                "row": 2,
                "column": 17,
                "level": 1
            }
        ],
        "reason": null
    }
}
```

### 3.3 FAILED 成功

请求体：

```json
{
  "taskId": "IN-002",
  "taskType": "INBOUND",
  "status": "FAILED",
  "startTime": "2026-05-24T10:13:00Z",
  "failureReason": "manual stop"
}
```

预期：

- `status = SUCCESS`
- 返回 `reason`

实际响应（当前环境复测，2026-06-08）：

```json
{
    "status": "SUCCESS",
    "message": "任务失败已记录。",
    "data": {
        "taskId": "IN-002",
        "status": "FAILED",
        "aisleId": "2",
        "positions": [
            {
                "aisleId": "2",
                "row": 4,
                "column": 17,
                "level": 1
            }
        ],
        "reason": "manual stop"
    }
}
```

### 3.4 重复 EXECUTING 幂等

连续两次请求：

```json
{
  "taskId": "IN-003",
  "taskType": "INBOUND",
  "status": "EXECUTING",
  "startTime": "2026-05-24T10:14:00Z",
}
```

预期：

- 再次回 `EXECUTING` 仍成功
- `message` 明确提示任务已处于执行中

实际响应（当前环境复测，2026-06-08）：

```json
{
    "status": "SUCCESS",
    "message": "任务已处于执行中。",
    "data": {
        "taskId": "IN-003",
        "status": "EXECUTING",
        "aisleId": "3",
        "positions": [
            {
                "aisleId": "3",
                "row": 6,
                "column": 17,
                "level": 1
            }
        ],
        "reason": null
    }
}
```

### 3.5 `task/adjust` 修改巷道

前置步骤 A：先通过 mixed 下发两个入库任务（`ADJ-001` / `ADJ-002`）。

请求体：

```json
{
  "currentTime": "2026-05-24 10:15:00",
  "inventory": [],
  "aisleStatus": [],
  "tasks": [
    {
      "taskId": "ADJ-001",
      "taskType": "INBOUND",
      "targetAisle": "1",
      "inLine": "L4C1",
      "outLine": "L1C17",
      "skus": [
        {
          "skuId": "ADJSKU1",
          "quantity": 1,
          "features": { "color": "W1", "skid_type": "0", "skid_state": "1" }
        }
      ]
    },
    {
      "taskId": "ADJ-002",
      "taskType": "INBOUND",
      "targetAisle": "1",
      "inLine": "L4C1",
      "outLine": "L1C17",
      "skus": [
        {
          "skuId": "ADJSKU2",
          "quantity": 1,
          "features": { "color": "W1", "skid_type": "0", "skid_state": "1" }
        }
      ]
    }
  ]
}
```

步骤 B：调用 `POST /api/v1/task/adjust`，将 `ADJ-002` 从 1 巷道改到 2 巷道（预期成功并重算位置）。

请求体：

```json
{
  "taskId": "ADJ-002",
  "taskType": "INBOUND",
  "aisleId": "2"
}
```

预期：

- `status = SUCCESS`
- 返回 `data.aisleId = "2"`
- 返回 `data.positions`（新巷道下位置）

步骤 C：修改后再发 `EXECUTING` 只确认执行，不再改位。

请求体：

```json
{
  "taskId": "ADJ-002",
  "taskType": "INBOUND",
  "status": "EXECUTING",
  "startTime": "2026-05-24T10:16:00Z"
}
```

预期：

- `status = SUCCESS`
- 执行巷道/位置与 `adjust` 后结果一致
- 调整后再次查询 `/api/v1/task/unconfirmed`，任务应从原巷道迁移到新巷道

实际响应（当前环境复测，2026-06-08）：

步骤 A：

```json
{
    "status": "SUCCESS",
    "message": "调度成功。",
    "data": {
        "scheduleId": "SCH-E0ACD016",
        "timestamp": "2026-06-08T02:32:51Z",
        "unconfirmedTaskIds": [],
        "aisleAssignments": [
            {
                "aisleId": "1",
                "assignedTask": {
                    "taskId": "ADJ-001",
                    "taskType": "INBOUND",
                    "planId": null,
                    "planIndex": null,
                    "inLine": "L4C1",
                    "outLine": "L1C17",
                    "positions": [
                        {
                            "row": 2,
                            "column": 17,
                            "level": 1,
                            "shelf": null,
                            "skuId": "ADJSKU1",
                            "quantity": 1
                        }
                    ]
                },
                "matchedTasks": [
                    {
                        "taskId": "ADJ-001",
                        "taskType": "INBOUND",
                        "planId": null,
                        "planIndex": null,
                        "aisleId": "1",
                        "inLine": "L4C1",
                        "outLine": "L1C17",
                        "positions": [
                            {
                                "aisleId": "1",
                                "row": 2,
                                "column": 17,
                                "level": 1
                            }
                        ]
                    },
                    {
                        "taskId": "ADJ-002",
                        "taskType": "INBOUND",
                        "planId": null,
                        "planIndex": null,
                        "aisleId": "1",
                        "inLine": "L4C1",
                        "outLine": "L1C17",
                        "positions": [
                            {
                                "aisleId": "1",
                                "row": 1,
                                "column": 16,
                                "level": 1
                            }
                        ]
                    }
                ]
            },
            {
                "aisleId": "2",
                "assignedTask": null,
                "matchedTasks": []
            },
            {
                "aisleId": "3",
                "assignedTask": null,
                "matchedTasks": []
            },
            {
                "aisleId": "4",
                "assignedTask": null,
                "matchedTasks": []
            }
        ],
        "executableTasksByAisle": {
            "1": [
                {
                    "taskId": "ADJ-001",
                    "taskType": "INBOUND",
                    "planId": null,
                    "planIndex": null,
                    "aisleId": "1",
                    "inLine": "L4C1",
                    "outLine": "L1C17",
                    "positions": [
                        {
                            "aisleId": "1",
                            "row": 2,
                            "column": 17,
                            "level": 1
                        }
                    ]
                },
                {
                    "taskId": "ADJ-002",
                    "taskType": "INBOUND",
                    "planId": null,
                    "planIndex": null,
                    "aisleId": "1",
                    "inLine": "L4C1",
                    "outLine": "L1C17",
                    "positions": [
                        {
                            "aisleId": "1",
                            "row": 1,
                            "column": 16,
                            "level": 1
                        }
                    ]
                }
            ],
            "2": [],
            "3": [],
            "4": []
        }
    }
}
```

步骤 B：

```json
{
    "status": "SUCCESS",
    "message": "任务调整成功。",
    "data": {
        "taskId": "ADJ-002",
        "aisleId": "2",
        "positions": [
            {
                "aisleId": "2",
                "row": 4,
                "column": 17,
                "level": 1
            }
        ],
        "reason": null
    }
}
```

步骤 B 后立即查询 `/api/v1/task/unconfirmed`：

```json
{
    "status": "SUCCESS",
    "message": "ok",
    "data": {
        "tasksByAisle": {
            "1": {
                "can_executing": [
                    {
                        "taskId": "ADJ-001",
                        "taskType": "INBOUND",
                        "planId": null,
                        "planIndex": null,
                        "aisleId": "1",
                        "inLine": "L4C1",
                        "outLine": "L1C17",
                        "positions": [
                            {
                                "aisleId": "1",
                                "row": 2,
                                "column": 17,
                                "level": 1
                            }
                        ]
                    }
                ],
                "pending": [],
                "running": []
            },
            "2": {
                "can_executing": [
                    {
                        "taskId": "ADJ-002",
                        "taskType": "INBOUND",
                        "planId": null,
                        "planIndex": null,
                        "aisleId": "2",
                        "inLine": "L4C1",
                        "outLine": "L1C17",
                        "positions": [
                            {
                                "aisleId": "2",
                                "row": 4,
                                "column": 17,
                                "level": 1
                            }
                        ]
                    }
                ],
                "pending": [],
                "running": []
            },
            "3": {
                "can_executing": [],
                "pending": [],
                "running": []
            },
            "4": {
                "can_executing": [],
                "pending": [],
                "running": []
            }
        },
        "canAcceptExecuting": true
    }
}
```

步骤 C：

```json
{
    "status": "SUCCESS",
    "message": "任务开始执行。",
    "data": {
        "taskId": "ADJ-002",
        "status": "EXECUTING",
        "aisleId": "2",
        "positions": [
            {
                "aisleId": "2",
                "row": 4,
                "column": 17,
                "level": 1
            }
        ],
        "reason": null
    }
}
```

### 3.5.1 入库任务执行中，同巷道出库任务被拦截

前置步骤 A：先下发并启动一个 1 巷道入库任务。

步骤 A1，请求体：

```json
{
  "currentTime": "2026-06-04 14:10:00",
  "inventory": [],
  "aisleStatus": [],
  "tasks": [
    {
      "taskId": "IB2-IN-001",
      "taskType": "INBOUND",
      "targetAisle": "1",
      "inLine": "L4C1",
      "outLine": "L1C17",
      "skus": [
        {
          "skuId": "IB2SKU1",
          "quantity": 1,
          "features": { "color": "W1", "skid_type": "0", "skid_state": "1" }
        }
      ]
    }
  ]
}
```

步骤 A2，请求体：

```json
{
  "taskId": "IB2-IN-001",
  "taskType": "INBOUND",
  "status": "EXECUTING",
  "startTime": "2026-06-04T14:11:00Z"
}
```

步骤 B：在 1 巷道补一条出库任务，再查未确认任务。

请求体：

```json
{
  "currentTime": "2026-06-04 14:12:00",
  "inventory": [
    {
      "aisleId": "1",
      "row": 1,
      "column": 10,
      "level": 1,
      "positions": [
        {
          "skuId": "OBSKU1",
          "quantity": 1,
          "features": { "color": "W1", "skid_type": "0", "skid_state": "1" }
        }
      ]
    }
  ],
  "aisleStatus": [],
  "tasks": [
    {
      "taskId": "IB2-OUT-001",
      "taskType": "OUTBOUND",
      "productionLine": 1,
      "outLine": "L1C17",
      "skus": [
        {
          "skuId": "OBSKU1",
          "quantity": 1,
          "features": { "color": "W1", "skid_type": "0", "skid_state": "1" }
        }
      ]
    }
  ]
}
```

查询 `/api/v1/task/unconfirmed` 的实际响应：

```json
{
    "status": "SUCCESS",
    "message": "ok",
    "data": {
        "tasksByAisle": {
            "1": {
                "can_executing": [
                    {
                        "taskId": "IB2-OUT-001",
                        "taskType": "OUTBOUND",
                        "planId": null,
                        "planIndex": null,
                        "aisleId": "1",
                        "inLine": 1,
                        "outLine": "L1C17",
                        "positions": [
                            {
                                "aisleId": "1",
                                "row": 1,
                                "column": 10,
                                "level": 1
                            }
                        ]
                    }
                ],
                "pending": [],
                "running": [
                    {
                        "taskId": "IB2-IN-001",
                        "taskType": "INBOUND",
                        "planId": null,
                        "planIndex": null,
                        "aisleId": "1",
                        "inLine": "L4C1",
                        "outLine": "L1C17",
                        "positions": [
                            {
                                "aisleId": "1",
                                "row": 2,
                                "column": 17,
                                "level": 1
                            }
                        ]
                    }
                ]
            },
            "2": { "can_executing": [], "pending": [], "running": [] },
            "3": { "can_executing": [], "pending": [], "running": [] },
            "4": { "can_executing": [], "pending": [], "running": [] }
        },
        "canAcceptExecuting": true
    }
}
```

步骤 C：尝试启动同巷道出库任务。

请求体：

```json
{
  "taskId": "IB2-OUT-001",
  "taskType": "OUTBOUND",
  "status": "EXECUTING",
  "startTime": "2026-06-04T14:13:00Z"
}
```

实际响应（当前环境复测，2026-06-08）：

```json
{
    "status": "FAILED",
    "message": "任务反馈被拒绝。",
    "data": {
        "taskId": "IB2-OUT-001",
        "reason": "任务 IB2-IN-001 当前正在执行。"
    }
}
```

### 3.5.2 出库任务执行中，同巷道入库任务被拦截

前置步骤 A：先下发并启动一个 1 巷道出库任务。

步骤 A1，请求体：

```json
{
  "currentTime": "2026-06-04 14:20:00",
  "inventory": [
    {
      "aisleId": "1",
      "row": 1,
      "column": 10,
      "level": 1,
      "positions": [
        {
          "skuId": "OB2SKU1",
          "quantity": 1,
          "features": { "color": "W1", "skid_type": "0", "skid_state": "1" }
        }
      ]
    }
  ],
  "aisleStatus": [],
  "tasks": [
    {
      "taskId": "OB2-OUT-001",
      "taskType": "OUTBOUND",
      "productionLine": 1,
      "outLine": "L1C17",
      "skus": [
        {
          "skuId": "OB2SKU1",
          "quantity": 1,
          "features": { "color": "W1", "skid_type": "0", "skid_state": "1" }
        }
      ]
    }
  ]
}
```

步骤 A2，请求体：

```json
{
  "taskId": "OB2-OUT-001",
  "taskType": "OUTBOUND",
  "status": "EXECUTING",
  "startTime": "2026-06-04T14:21:00Z"
}
```

步骤 B：再下发一个 1 巷道入库任务，并查询 `/api/v1/task/unconfirmed`。

请求体：

```json
{
  "currentTime": "2026-06-04 14:22:00",
  "inventory": [],
  "aisleStatus": [],
  "tasks": [
    {
      "taskId": "OB2-IN-001",
      "taskType": "INBOUND",
      "targetAisle": "1",
      "inLine": "L4C1",
      "outLine": "L1C17",
      "skus": [
        {
          "skuId": "OB2INSKU1",
          "quantity": 1,
          "features": { "color": "W1", "skid_type": "0", "skid_state": "1" }
        }
      ]
    }
  ]
}
```

查询 `/api/v1/task/unconfirmed` 的实际响应：

```json
{
    "status": "SUCCESS",
    "message": "ok",
    "data": {
        "tasksByAisle": {
            "1": {
                "can_executing": [
                    {
                        "taskId": "OB2-IN-001",
                        "taskType": "INBOUND",
                        "planId": null,
                        "planIndex": null,
                        "aisleId": "1",
                        "inLine": "L4C1",
                        "outLine": "L1C17",
                        "positions": [
                            {
                                "aisleId": "1",
                                "row": 2,
                                "column": 17,
                                "level": 1
                            }
                        ]
                    }
                ],
                "pending": [],
                "running": [
                    {
                        "taskId": "OB2-OUT-001",
                        "taskType": "OUTBOUND",
                        "planId": null,
                        "planIndex": null,
                        "aisleId": "1",
                        "inLine": 1,
                        "outLine": "L1C17",
                        "positions": [
                            {
                                "aisleId": "1",
                                "row": 1,
                                "column": 10,
                                "level": 1
                            }
                        ]
                    }
                ]
            },
            "2": { "can_executing": [], "pending": [], "running": [] },
            "3": { "can_executing": [], "pending": [], "running": [] },
            "4": { "can_executing": [], "pending": [], "running": [] }
        },
        "canAcceptExecuting": true
    }
}
```

步骤 C：尝试启动同巷道入库任务。

请求体：

```json
{
  "taskId": "OB2-IN-001",
  "taskType": "INBOUND",
  "status": "EXECUTING",
  "startTime": "2026-06-04T14:23:00Z"
}
```

实际响应（当前环境复测，2026-06-08）：

```json
{
    "status": "FAILED",
    "message": "任务反馈被拒绝。",
    "data": {
        "taskId": "OB2-IN-001",
        "reason": "任务 OB2-OUT-001 当前正在执行。"
    }
}
```

### 3.6 `task/adjust` 冲突与非法 row 校验

#### 3.6.1 与目标巷道 pending 任务占位冲突（插队漏检回归）

构造思路：

- 先让 2 巷道已有一个 pending 入库任务占用 `row=3,column=11,level=1`
- 再用 `adjust` 把另一个任务改到同一位置

请求体：

```json
{
  "taskId": "IN-005",
  "taskType": "INBOUND",
  "aisleId": "2",
  "positions": [
    { "aisleId": "2", "row": 3, "column": 11, "level": 1 }
  ]
}
```

预期：

- HTTP `400`
- `status = FAILED`
- `data.reason` 明确为位置冲突，并指向目标巷道待执行入库任务
- 位置格式是外部口径：`aisle-externalRow-column-level`（例如 `2-3-11-1`）

实际响应（当前环境复测，2026-06-08）：

```json
{
    "status": "SUCCESS",
    "message": "任务调整成功。",
    "data": {
        "taskId": "IN-005",
        "aisleId": "2",
        "positions": [
            {
                "aisleId": "2",
                "row": 3,
                "column": 11,
                "level": 1
            }
        ],
        "reason": null
    }
}
```

标注：

- 这一条不符合逻辑。按构造意图，这里本应命中“目标巷道已有 pending 入库任务占位冲突”，但当前接口却直接返回了调整成功。
- 这说明当前 `adjust` 对目标巷道 pending 入库占位的冲突校验仍然不完整，至少没有拦住这组显式位置。

#### 3.6.2 非法 row 校验

请求体（2巷道传非法 row=5）：

```json
{
  "taskId": "IN-005",
  "taskType": "INBOUND",
  "aisleId": "2",
  "positions": [
    { "aisleId": "2", "row": 5, "column": 12, "level": 1 }
  ]
}
```

预期：

- HTTP `400`
- `status = FAILED`
- `data.reason` 明确提示 row 非法（2巷道只允许 `1/2` 或 `3/4`）
- 不允许把非法 row 自动折叠/映射成合法行

实际响应（当前环境复测，2026-06-08）：

```json
{
    "status": "FAILED",
    "message": "任务调整参数校验失败。",
    "data": {
        "taskId": "IN-005",
        "reason": "2巷道的row=5不合法，只允许传1/2或3/4。"
    }
}
```

---

## 4. POST `/api/v1/inbound/allocate`

### 4.1 基础推荐巷道

请求体：

```json
{
  "tasks": [
    {
      "taskId": "ALLOC-BASE-001",
      "inLine": "L4C1",
      "outLine": "L1C17",
      "skus": [
        {
          "skuId": "1RAT000012",
          "quantity": 1,
          "features": {
            "color": "W1",
            "skid_type": "0",
            "skid_state": "1"
          }
        }
      ]
    }
  ]
}
```

预期：

- `status = SUCCESS`
- 返回 `recommendedAisle`

实际响应（当前环境复测，2026-06-08）：

```json
{
    "status": "SUCCESS",
    "message": "分配成功",
    "data": {
        "allocationId": "ALLOC-BFEEC127",
        "assignments": [
            {
                "taskId": "ALLOC-BASE-001",
                "recommendedAisle": "1"
            }
        ],
        "checks": {
            "aisle_forbidden": {
                "rule": "driven by config/warehouse.json aisle_forbidden",
                "checked_count": 1,
                "passed": true,
                "violations": []
            }
        }
    }
}
```

### 4.2 多任务推荐顺序

请求体：

```json
{
  "tasks": [
    {
      "taskId": "ALLOC-MULTI-001",
      "inLine": "L4C1",
      "outLine": "L1C17",
      "skus": [
        {
          "skuId": "1RAT000012",
          "quantity": 1,
          "features": {
            "color": "W1",
            "skid_type": "0",
            "skid_state": "1"
          }
        }
      ]
    },
    {
      "taskId": "ALLOC-MULTI-002",
      "inLine": "L4C1",
      "outLine": "L2C17",
      "skus": [
        {
          "skuId": "1RAT000013",
          "quantity": 1,
          "features": {
            "color": "W1",
            "skid_type": "0",
            "skid_state": "1"
          }
        }
      ]
    }
  ]
}
```

预期：

- 多条任务都能返回推荐结果

实际响应（当前环境复测，2026-06-08）：

```json
{
    "status": "SUCCESS",
    "message": "分配成功",
    "data": {
        "allocationId": "ALLOC-90DA345A",
        "assignments": [
            {
                "taskId": "ALLOC-MULTI-001",
                "recommendedAisle": "3"
            },
            {
                "taskId": "ALLOC-MULTI-002",
                "recommendedAisle": "2"
            }
        ],
        "checks": {
            "aisle_forbidden": {
                "rule": "driven by config/warehouse.json aisle_forbidden",
                "checked_count": 2,
                "passed": true,
                "violations": []
            }
        }
    }
}
```

### 4.3 长滑橇分配

请求体：

```json
{
  "tasks": [
    {
      "taskId": "ALLOC-LONG-001",
      "inLine": "L4C1",
      "outLine": "L1C17",
      "skus": [
        {
          "skuId": "1RAT000099",
          "quantity": 1,
          "features": {
            "color": "W1",
            "skid_type": "1",
            "skid_state": "1"
          }
        }
      ]
    }
  ]
}
```

预期：

- `status = SUCCESS`
- 返回 `recommendedAisle`
- `recommendedAisle` 必须在该长滑橇允许的巷道集合内
- 不应推荐到命中禁配规则的巷道

实际响应（当前环境复测，2026-06-08）：

```json
{
    "status": "SUCCESS",
    "message": "分配成功",
    "data": {
        "allocationId": "ALLOC-24DB6E7E",
        "assignments": [
            {
                "taskId": "ALLOC-LONG-001",
                "recommendedAisle": "4"
            }
        ],
        "checks": {
            "aisle_forbidden": {
                "rule": "driven by config/warehouse.json aisle_forbidden",
                "checked_count": 1,
                "passed": true,
                "violations": []
            }
        }
    }
}
```

---

## 5. 生产计划与 currentGroups

### 5.1 currentGroups = 2，直接调第 2 组

请求体：

```json
{
  "currentTime": "2026-05-24 11:00:00",
  "inventory": [
    {
      "aisleId": "1",
      "row": 1,
      "column": 10,
      "level": 1,
      "positions": [
        { "skuId": "1RAT000001", "quantity": 1, "features": { "color": "W1", "skid_type": "0", "skid_state": "1" } }
      ]
    },
    {
      "aisleId": "2",
      "row": 3,
      "column": 11,
      "level": 1,
      "positions": [
        { "skuId": "1RAT000012", "quantity": 1, "features": { "color": "W1", "skid_type": "0", "skid_state": "1" } }
      ]
    },
    {
      "aisleId": "3",
      "row": 5,
      "column": 12,
      "level": 1,
      "positions": [
        { "skuId": "1RAT000013", "quantity": 1, "features": { "color": "W1", "skid_type": "0", "skid_state": "1" } }
      ]
    }
  ],
  "aisleStatus": [],
  "productionPlan": {
    "operationType": "UPDATE",
    "planDate": "2026-05-24 11:00:00",
    "plans": [
      {
        "planId": "PLAN-LINE1",
        "lineId": "1",
        "planIndex": [
          {
            "requiredSkus": [[{ "skuId": "1RAT000001", "quantity": 1, "features": { "color": "W1", "skid_type": "0", "skid_state": "1" } }]]
          },
          {
            "requiredSkus": [[{ "skuId": "1RAT000012", "quantity": 1, "features": { "color": "W1", "skid_type": "0", "skid_state": "1" } }]]
          },
          {
            "requiredSkus": [[{ "skuId": "1RAT000013", "quantity": 1, "features": { "color": "W1", "skid_type": "0", "skid_state": "1" } }]]
          }
        ]
      }
    ]
  },
  "currentGroups": [
    { "lineId": "1", "currentGroup": 2 }
  ],
  "tasks": [
    {
      "taskId": "OUT-PL1-G1-001",
      "taskType": "OUTBOUND",
      "planId": "PLAN-LINE1",
      "planIndex": 1,
      "productionLine": 1,
      "outLine": "L1C17",
      "skus": [
        { "skuId": "1RAT000001", "quantity": 1, "features": { "color": "W1", "skid_type": "0", "skid_state": "1" } }
      ]
    },
    {
      "taskId": "OUT-PL1-G2-001",
      "taskType": "OUTBOUND",
      "planId": "PLAN-LINE1",
      "planIndex": 2,
      "productionLine": 1,
      "outLine": "L1C17",
      "skus": [
        { "skuId": "1RAT000012", "quantity": 1, "features": { "color": "W1", "skid_type": "0", "skid_state": "1" } }
      ]
    },
    {
      "taskId": "OUT-PL1-G3-001",
      "taskType": "OUTBOUND",
      "planId": "PLAN-LINE1",
      "planIndex": 3,
      "productionLine": 1,
      "outLine": "L1C17",
      "skus": [
        { "skuId": "1RAT000013", "quantity": 1, "features": { "color": "W1", "skid_type": "0", "skid_state": "1" } }
      ]
    }
  ]
}
```

预期：

- `status = SUCCESS`
- 第 2 组任务可被调度
- 第 1 组任务不再出现在 `matchedTasks`
- 第 3 组任务不进入 `matchedTasks`，而是在查询接口中进入 `pending`

实际响应（当前环境复测，2026-06-14）：

```json
{
    "status": "SUCCESS",
    "message": "调度成功。",
    "data": {
        "scheduleId": "SCH-8BB0A715",
        "timestamp": "2026-06-14T03:35:54Z",
        "unconfirmedTaskIds": [],
        "aisleAssignments": [
            {
                "aisleId": "1",
                "assignedTask": null,
                "matchedTasks": []
            },
            {
                "aisleId": "2",
                "assignedTask": {
                    "taskId": "OUT-PL1-G2-001",
                    "taskType": "OUTBOUND",
                    "planId": "PLAN-LINE1",
                    "planIndex": 2,
                    "inLine": "L1C1",
                    "outLine": "L1C17",
                    "positions": [
                        {
                            "row": 3,
                            "column": 11,
                            "level": 1,
                            "shelf": null,
                            "skuId": "1RAT000012",
                            "quantity": 1
                        }
                    ]
                },
                "matchedTasks": [
                    {
                        "taskId": "OUT-PL1-G2-001",
                        "taskType": "OUTBOUND",
                        "planId": "PLAN-LINE1",
                        "planIndex": 2,
                        "aisleId": "2",
                        "inLine": 1,
                        "outLine": "L1C17",
                        "positions": [
                            {
                                "aisleId": "2",
                                "row": 3,
                                "column": 11,
                                "level": 1
                            }
                        ]
                    }
                ]
            },
            {
                "aisleId": "3",
                "assignedTask": null,
                "matchedTasks": []
            },
            {
                "aisleId": "4",
                "assignedTask": null,
                "matchedTasks": []
            }
        ],
        "executableTasksByAisle": {
            "1": [],
            "2": [
                {
                    "taskId": "OUT-PL1-G2-001",
                    "taskType": "OUTBOUND",
                    "planId": "PLAN-LINE1",
                    "planIndex": 2,
                    "aisleId": "2",
                    "inLine": 1,
                    "outLine": "L1C17",
                    "positions": [
                        {
                            "aisleId": "2",
                            "row": 3,
                            "column": 11,
                            "level": 1
                        }
                    ]
                }
            ],
            "3": [],
            "4": []
        },
        "checks": {
            "aisle_forbidden": {
                "rule": "driven by config/warehouse.json aisle_forbidden",
                "checked_count": 0,
                "passed": true,
                "violations": []
            }
        }
    }
}
```

补充查询：立即调用 `GET /api/v1/task/unconfirmed`

实际响应（当前环境复测，2026-06-14）：

```json
{
    "status": "SUCCESS",
    "message": "ok",
    "data": {
        "tasksByAisle": {
            "1": {
                "can_executing": [],
                "pending": [],
                "running": []
            },
            "2": {
                "can_executing": [
                    {
                        "taskId": "OUT-PL1-G2-001",
                        "taskType": "OUTBOUND",
                        "planId": "PLAN-LINE1",
                        "planIndex": 2,
                        "aisleId": "2",
                        "inLine": 1,
                        "outLine": "L1C17",
                        "positions": [
                            {
                                "aisleId": "2",
                                "row": 3,
                                "column": 11,
                                "level": 1
                            }
                        ]
                    }
                ],
                "pending": [],
                "running": []
            },
            "3": {
                "can_executing": [],
                "pending": [
                    {
                        "taskId": "OUT-PL1-G3-001",
                        "taskType": "OUTBOUND",
                        "planId": "PLAN-LINE1",
                        "planIndex": 3,
                        "aisleId": "3",
                        "inLine": 1,
                        "outLine": "L1C17",
                        "positions": [
                            {
                                "aisleId": "3",
                                "row": 5,
                                "column": 12,
                                "level": 1
                            }
                        ]
                    }
                ],
                "running": []
            },
            "4": {
                "can_executing": [],
                "pending": [],
                "running": []
            }
        },
        "canAcceptExecuting": true
    }
}
```

### 5.2 currentGroups = 0，自动补正为第 1 组

请求体：

```json
{
  "currentTime": "2026-05-24 11:04:00",
  "inventory": [
    {
      "aisleId": "1",
      "row": 1,
      "column": 10,
      "level": 1,
      "positions": [
        { "skuId": "1RAT000001", "quantity": 1, "features": { "color": "W1", "skid_type": "0", "skid_state": "1" } }
      ]
    },
    {
      "aisleId": "2",
      "row": 3,
      "column": 11,
      "level": 1,
      "positions": [
        { "skuId": "1RAT000012", "quantity": 1, "features": { "color": "W1", "skid_type": "0", "skid_state": "1" } }
      ]
    }
  ],
  "aisleStatus": [],
  "productionPlan": {
    "operationType": "UPDATE",
    "planDate": "2026-05-24 11:04:00",
    "plans": [
      {
        "planId": "PLAN-LINE1",
        "lineId": "1",
        "planIndex": [
          {
            "requiredSkus": [[{ "skuId": "1RAT000001", "quantity": 1, "features": { "color": "W1", "skid_type": "0", "skid_state": "1" } }]]
          },
          {
            "requiredSkus": [[{ "skuId": "1RAT000012", "quantity": 1, "features": { "color": "W1", "skid_type": "0", "skid_state": "1" } }]]
          }
        ]
      }
    ]
  },
  "currentGroups": [
    { "lineId": "1", "currentGroup": 0 }
  ],
  "tasks": [
    {
      "taskId": "OUT-PL1-G1-001",
      "taskType": "OUTBOUND",
      "planId": "PLAN-LINE1",
      "planIndex": 1,
      "productionLine": 1,
      "outLine": "L1C17",
      "skus": [
        { "skuId": "1RAT000001", "quantity": 1, "features": { "color": "W1", "skid_type": "0", "skid_state": "1" } }
      ]
    },
    {
      "taskId": "OUT-PL1-G2-001",
      "taskType": "OUTBOUND",
      "planId": "PLAN-LINE1",
      "planIndex": 2,
      "productionLine": 1,
      "outLine": "L1C17",
      "skus": [
        { "skuId": "1RAT000012", "quantity": 1, "features": { "color": "W1", "skid_type": "0", "skid_state": "1" } }
      ]
    }
  ]
}
```

预期：

- `status = SUCCESS`
- `currentGroup=0` 被按第 1 组处理

实际响应（当前环境复测，2026-06-08）：

```json
{
    "status": "SUCCESS",
    "message": "调度成功。",
    "data": {
        "scheduleId": "SCH-F9061405",
        "timestamp": "2026-06-08T02:18:00Z",
        "unconfirmedTaskIds": [],
        "aisleAssignments": [
            {
                "aisleId": "1",
                "assignedTask": {
                    "taskId": "OUT-PL1-G1-001",
                    "taskType": "OUTBOUND",
                    "planId": "PLAN-LINE1",
                    "planIndex": 1,
                    "inLine": "L1C1",
                    "outLine": "L1C17",
                    "positions": [
                        {
                            "row": 1,
                            "column": 10,
                            "level": 1,
                            "shelf": null,
                            "skuId": "1RAT000001",
                            "quantity": 1
                        }
                    ]
                }
            },
            {
                "aisleId": "2",
                "assignedTask": null
            },
            {
                "aisleId": "3",
                "assignedTask": null
            },
            {
                "aisleId": "4",
                "assignedTask": null
            }
        ],
        "checks": {
            "aisle_forbidden": {
                "rule": "driven by config/warehouse.json aisle_forbidden",
                "checked_count": 0,
                "passed": true,
                "violations": []
            }
        }
    }
}
```

### 5.3 第1组未完成时强传 currentGroups = 2

前置：

- 先让第 1 组任务下发但不回 `COMPLETED`

请求体：

```json
{
  "currentTime": "2026-05-24 11:06:00",
  "inventory": [],
  "aisleStatus": [],
  "currentGroups": [
    { "lineId": "1", "currentGroup": 2 }
  ],
  "tasks": []
}
```

预期：

- HTTP `400`
- `status = FAILED`
- 本次 mixed 不生效（如果有计划、组指针等均会回滚）

实际响应（当前环境复测，2026-06-08）：

```json
{
    "status": "FAILED",
    "message": "请求参数校验失败。",
    "data": {
        "stage": "sync_production_context",
        "detail": "产线1仍有未完成的第1组任务，不能直接切到第2组。",
        "timestamp": "2026-06-08T02:18:00Z"
    }
}
```

### 5.4 currentGroups 越界报错

请求体：

```json
{
  "currentTime": "2026-05-24 11:06:00",
  "inventory": [],
  "aisleStatus": [],
  "currentGroups": {
    "LINE-1": 5
    },
  "tasks": []
}
```

注意这里使用了不同的 `currentGroups` 写法，两种写法均可识别：

```json
"currentGroups": [
  { "lineId": "1", "currentGroup": 5 }
]
```

```json
"currentGroups": {
  "LINE-1": 5
}
```

预期：

- HTTP `400`
- `status = FAILED`
- 返回 currentGroups 越界提示

实际响应（当前环境复测，2026-06-08）：

```json
{
    "status": "FAILED",
    "message": "请求参数校验失败。",
    "data": {
        "stage": "sync_production_context",
        "detail": "产线 1 设置的组 5 超过当前上限 2",
        "timestamp": "2026-06-08T02:18:00Z"
    }
}
```

### 5.5 ADD 按组追加，保持当前组指针

重启API后执行5.1，然后继续进行以下请求：

请求体：

```json
{
  "currentTime": "2026-05-24 11:10:00",
  "inventory": [
    {
      "aisleId": "1",
      "row": 1,
      "column": 10,
      "level": 1,
      "positions": [
        { "skuId": "1RAT000001", "quantity": 1, "features": { "color": "W1", "skid_type": "0", "skid_state": "1" } }
      ]
    },
    {
      "aisleId": "2",
      "row": 3,
      "column": 11,
      "level": 1,
      "positions": [
        { "skuId": "1RAT000012", "quantity": 1, "features": { "color": "W1", "skid_type": "0", "skid_state": "1" } }
      ]
    },
    {
      "aisleId": "3",
      "row": 5,
      "column": 12,
      "level": 1,
      "positions": [
        { "skuId": "1RAT000013", "quantity": 1, "features": { "color": "W1", "skid_type": "0", "skid_state": "1" } }
      ]
    }
  ],
  "aisleStatus": [],
  "productionPlan": {
    "operationType": "ADD",
    "planDate": "2026-05-24 11:10:00",
    "plans": [
      {
        "planId": "PLAN-LINE1-ADD",
        "lineId": "1",
        "planIndex": [
          {
            "requiredSkus": [[{ "skuId": "1RAT000013", "quantity": 1, "features": { "color": "W1", "skid_type": "0", "skid_state": "1" } }]]
          }
        ]
      }
    ]
  },
  "tasks": [
    {
      "taskId": "OUT-PL1-ADD-001",
      "taskType": "OUTBOUND",
      "planId": "PLAN-LINE1-ADD",
      "planIndex": 1,
      "productionLine": 1,
      "outLine": "L1C17",
      "skus": [
        { "skuId": "1RAT000013", "quantity": 1, "features": { "color": "W1", "skid_type": "0", "skid_state": "1" } }
      ]
    }
  ]
}
```

预期：

- `status = SUCCESS`
- 计划按组追加到末尾
- 当前组指针保持不变

实际响应（当前环境复测，2026-06-08）：

```json
{
    "status": "SUCCESS",
    "message": "调度成功。",
    "data": {
        "scheduleId": "SCH-509254C9",
        "timestamp": "2026-06-08T02:18:00Z",
        "unconfirmedTaskIds": [
            "OUT-PL1-G2-001"
        ],
        "aisleAssignments": [
            {
                "aisleId": "1",
                "assignedTask": null
            },
            {
                "aisleId": "2",
                "assignedTask": {
                    "taskId": "OUT-PL1-G2-001",
                    "taskType": "OUTBOUND",
                    "planId": "PLAN-LINE1",
                    "planIndex": 2,
                    "inLine": "L1C1",
                    "outLine": "L1C17",
                    "positions": [
                        {
                            "row": 3,
                            "column": 11,
                            "level": 1,
                            "shelf": null,
                            "skuId": "1RAT000012",
                            "quantity": 1
                        }
                    ]
                }
            },
            {
                "aisleId": "3",
                "assignedTask": null
            },
            {
                "aisleId": "4",
                "assignedTask": null
            }
        ],
        "checks": {
            "aisle_forbidden": {
                "rule": "driven by config/warehouse.json aisle_forbidden",
                "checked_count": 0,
                "passed": true,
                "violations": []
            }
        }
    }
}
```

补充查询 `GET /api/v1/plan/production`：

```json
{
    "status": "SUCCESS",
    "message": "ok",
    "data": {
        "production_plan": {
            "1": [
                [
                    [
                        {
                            "skuId": "1RAT000001"
                        }
                    ]
                ],
                [
                    [
                        {
                            "skuId": "1RAT000012"
                        }
                    ]
                ],
                [
                    [
                        {
                            "skuId": "1RAT000013"
                        }
                    ]
                ],
                [
                    [
                        {
                            "skuId": "1RAT000013"
                        }
                    ]
                ]
            ],
            "2": [],
            "3": [],
            "4": []
        }
    }
}
```

### 5.6 UPDATE 替换计划（resetAssigned=false，仅清 pending）

重启API后执行5.1，然后继续进行以下请求：

请求体：

```json
{
  "currentTime": "2026-05-24 11:12:00",
  "inventory": [
    {
      "aisleId": "1",
      "row": 1,
      "column": 10,
      "level": 1,
      "positions": [
        { "skuId": "1RAT000001", "quantity": 1, "features": { "color": "W1", "skid_type": "0", "skid_state": "1" } }
      ]
    },
    {
      "aisleId": "2",
      "row": 3,
      "column": 11,
      "level": 1,
      "positions": [
        { "skuId": "1RAT000012", "quantity": 1, "features": { "color": "W1", "skid_type": "0", "skid_state": "1" } }
      ]
    },
    {
      "aisleId": "3",
      "row": 5,
      "column": 12,
      "level": 1,
      "positions": [
        { "skuId": "1RAT000013", "quantity": 1, "features": { "color": "W1", "skid_type": "0", "skid_state": "1" } }
      ]
    },
    {
      "aisleId": "4",
      "row": 8,
      "column": 6,
      "level": 1,
      "positions": [
        { "skuId": "1RAT000099", "quantity": 1, "features": { "color": "W1", "skid_type": "1", "skid_state": "1" } }
      ]
    }
  ],
  "aisleStatus": [],
  "productionPlan": {
    "operationType": "UPDATE",
    "resetAssigned": false,
    "planDate": "2026-05-24 11:12:00",
    "plans": [
      {
        "planId": "PLAN-LINE1-REPLACE",
        "lineId": "1",
        "planIndex": [
          {
            "requiredSkus": [[{ "skuId": "1RAT000099", "quantity": 1, "features": { "color": "W1", "skid_type": "1", "skid_state": "1" } }]]
          }
        ]
      }
    ]
  },
  "tasks": [
    {
      "taskId": "OUT-PL1-REPLACE-001",
      "taskType": "OUTBOUND",
      "planId": "PLAN-LINE1-REPLACE",
      "planIndex": 1,
      "productionLine": 1,
      "outLine": "L1C17",
      "skus": [
        { "skuId": "1RAT000099", "quantity": 1, "features": { "color": "W1", "skid_type": "1", "skid_state": "1" } }
      ]
    }
  ]
}
```

预期：

- `status = SUCCESS`
- 原计划被替换
- 指针重置到新计划起点（未显式传新 `currentGroups` 时）
- 仅清理未下发 `pending`，但保留 `pending_execution/running`

实际响应（当前环境复测，2026-06-08）：

```json
{
    "status": "SUCCESS",
    "message": "调度成功。",
    "data": {
        "scheduleId": "SCH-4788DE66",
        "timestamp": "2026-06-08T02:18:00Z",
        "unconfirmedTaskIds": [
            "OUT-PL1-G2-001"
        ],
        "aisleAssignments": [
            {
                "aisleId": "1",
                "assignedTask": null
            },
            {
                "aisleId": "2",
                "assignedTask": {
                    "taskId": "OUT-PL1-G2-001",
                    "taskType": "OUTBOUND",
                    "planId": "PLAN-LINE1",
                    "planIndex": 2,
                    "inLine": "L1C1",
                    "outLine": "L1C17",
                    "positions": [
                        {
                            "row": 3,
                            "column": 11,
                            "level": 1,
                            "shelf": null,
                            "skuId": "1RAT000012",
                            "quantity": 1
                        }
                    ]
                }
            },
            {
                "aisleId": "3",
                "assignedTask": null
            },
            {
                "aisleId": "4",
                "assignedTask": {
                    "taskId": "OUT-PL1-REPLACE-001",
                    "taskType": "OUTBOUND",
                    "planId": "PLAN-LINE1-REPLACE",
                    "planIndex": 1,
                    "inLine": "L1C1",
                    "outLine": "L1C17",
                    "positions": [
                        {
                            "row": 8,
                            "column": 6,
                            "level": 1,
                            "shelf": null,
                            "skuId": "1RAT000099",
                            "quantity": 1
                        }
                    ]
                }
            }
        ],
        "checks": {
            "aisle_forbidden": {
                "rule": "driven by config/warehouse.json aisle_forbidden",
                "checked_count": 0,
                "passed": true,
                "violations": []
            }
        }
    }
}
```

补充查询 `GET /api/v1/plan/production`：

```json
{
    "status": "SUCCESS",
    "message": "ok",
    "data": {
        "production_plan": {
            "1": [
                [
                    [
                        {
                            "skuId": "1RAT000099"
                        }
                    ]
                ]
            ],
            "2": [],
            "3": [],
            "4": []
        }
    }
}
```

### 5.7 UPDATE 替换计划（resetAssigned=true，全清任务态）

请求体：

```json
{
  "currentTime": "2026-05-24 11:14:00",
  "inventory": [
    {
      "aisleId": "1",
      "row": 1,
      "column": 10,
      "level": 1,
      "positions": [
        { "skuId": "1RAT000001", "quantity": 1, "features": { "color": "W1", "skid_type": "0", "skid_state": "1" } }
      ]
    }
  ],
  "aisleStatus": [],
  "productionPlan": {
    "operationType": "UPDATE",
    "resetAssigned": true,
    "planDate": "2026-05-24 11:14:00",
    "plans": [
      {
        "planId": "PLAN-LINE1-RESET",
        "lineId": "1",
        "planIndex": [
          {
            "requiredSkus": [[{ "skuId": "1RAT000001", "quantity": 1, "features": { "color": "W1", "skid_type": "0", "skid_state": "1" } }]]
          }
        ]
      }
    ]
  },
  "tasks": [
    {
      "taskId": "OUT-PL1-RESET-001",
      "taskType": "OUTBOUND",
      "planId": "PLAN-LINE1-RESET",
      "planIndex": 1,
      "productionLine": 1,
      "outLine": "L1C17",
      "skus": [
        { "skuId": "1RAT000001", "quantity": 1, "features": { "color": "W1", "skid_type": "0", "skid_state": "1" } }
      ]
    }
  ]
}
```

预期：

- `status = SUCCESS`
- 原计划被替换
- `pending/pending_execution/running` 全部清空后再按本次请求调度

实际响应（当前环境复测，2026-06-08）：

```json
{
    "status": "SUCCESS",
    "message": "调度成功。",
    "data": {
        "scheduleId": "SCH-091B9B4F",
        "timestamp": "2026-06-08T02:18:00Z",
        "unconfirmedTaskIds": [],
        "aisleAssignments": [
            {
                "aisleId": "1",
                "assignedTask": {
                    "taskId": "OUT-PL1-RESET-001",
                    "taskType": "OUTBOUND",
                    "planId": "PLAN-LINE1-RESET",
                    "planIndex": 1,
                    "inLine": "L1C1",
                    "outLine": "L1C17",
                    "positions": [
                        {
                            "row": 1,
                            "column": 10,
                            "level": 1,
                            "shelf": null,
                            "skuId": "1RAT000001",
                            "quantity": 1
                        }
                    ]
                }
            },
            {
                "aisleId": "2",
                "assignedTask": null
            },
            {
                "aisleId": "3",
                "assignedTask": null
            },
            {
                "aisleId": "4",
                "assignedTask": null
            }
        ],
        "checks": {
            "aisle_forbidden": {
                "rule": "driven by config/warehouse.json aisle_forbidden",
                "checked_count": 0,
                "passed": true,
                "violations": []
            }
        }
    }
}
```

补充查询 `GET /api/v1/plan/production`：

```json
{
    "status": "SUCCESS",
    "message": "ok",
    "data": {
        "production_plan": {
            "1": [
                [
                    [
                        {
                            "skuId": "1RAT000001"
                        }
                    ]
                ]
            ],
            "2": [],
            "3": [],
            "4": []
        }
    }
}
```

---

## 6. GET `/api/v1/status`

### 6.1 查询系统状态

请求体：

- 无

预期：

- `status = SUCCESS`
- 返回 `service_status`
- 返回 `inventory_summary`
- 返回 `aisle_status`
- 返回完整 `inventory` 货位数组

实际响应（当前环境复测，2026-06-08）：

```json
{
    "status": "SUCCESS",
    "message": "ok",
    "data": {
        "service_status": "running",
        "current_time": 0.05892038345336914,
        "running_tasks_count": 0,
        "completed_tasks_count": 0,
        "aisle_status": {
            "1": {
                "is_busy": false,
                "blockage": {
                    "1": { "blocked": false, "unblock_time": 0.0 },
                    "2": { "blocked": false, "unblock_time": 0.0 },
                    "3": { "blocked": false, "unblock_time": 0.0 },
                    "4": { "blocked": false, "unblock_time": 0.0 }
                },
                "current_position": null
            },
            "2": {
                "is_busy": false,
                "blockage": {
                    "1": { "blocked": false, "unblock_time": 0.0 },
                    "2": { "blocked": false, "unblock_time": 0.0 },
                    "3": { "blocked": false, "unblock_time": 0.0 },
                    "4": { "blocked": false, "unblock_time": 0.0 }
                },
                "current_position": null
            },
            "3": {
                "is_busy": false,
                "blockage": {
                    "1": { "blocked": false, "unblock_time": 0.0 },
                    "2": { "blocked": false, "unblock_time": 0.0 },
                    "3": { "blocked": false, "unblock_time": 0.0 },
                    "4": { "blocked": false, "unblock_time": 0.0 }
                },
                "current_position": null
            },
            "4": {
                "is_busy": false,
                "blockage": {
                    "1": { "blocked": false, "unblock_time": 0.0 },
                    "2": { "blocked": false, "unblock_time": 0.0 },
                    "3": { "blocked": false, "unblock_time": 0.0 },
                    "4": { "blocked": false, "unblock_time": 0.0 }
                },
                "current_position": null
            }
        },
        "inventory_summary": {
            "1": {
                "1RAT000001": 1
            },
            "2": {},
            "3": {},
            "4": {}
        },
        "inventory": [
            {
                "aisleId": "1",
                "row": 1,
                "column": 1,
                "level": 1,
                "positions": [
                    {
                        "skuId": "",
                        "quantity": 0
                    }
                ]
            },
            {
                "aisleId": "1",
                "row": 1,
                "column": 1,
                "level": 2,
                "positions": [
                    {
                        "skuId": "",
                        "quantity": 0
                    }
                ]
            },
            {
                "aisleId": "1",
                "row": 1,
                "column": 10,
                "level": 1,
                "positions": [
                    {
                        "skuId": "1RAT000001",
                        "quantity": 1
                    }
                ]
            }
        ]
    }
}
```

说明：

- 当前真实返回中的 `inventory` 会继续展开所有货位；这里仅保留开头若干项和实际有货位项，避免整段测试文档被数千行空货位淹没。
