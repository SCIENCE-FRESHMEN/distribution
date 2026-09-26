# Apifox 接口测试清单

本文档用于在 Apifox 中建立回归测试用例，覆盖当前 API 的基础能力、最近新增规则、异常兜底和计划/库存同步行为。

建议顺序：

1. 重启 `run_api.py`
2. 先执行 mixed 基础用例
3. 再执行 feedback 用例
4. 最后执行 production plan / currentGroups / inventory 特殊用例

---

## 1. POST `/api/v1/schedule/mixed`

### 1.1 空 mixed 基础

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
- `message = 调度成功`
- `data.scheduleId` 存在
- `data.aisleAssignments` 存在

真实返回摘要：

- `mixedResponse: HTTP 200 | SUCCESS | 调度成功 ; unconfirmedAfterUpdate: HTTP 200 | SUCCESS | 获取可执行任务成功`

当前环境实际返回：

```json
{
  "httpStatus": 200,
  "body": {
    "status": "SUCCESS",
    "message": "调度成功",
    "data": {
      "scheduleId": "SCH-FC6FACFA",
      "timestamp": "2026-06-05T06:15:37Z",
      "aisleAssignments": [
        {
          "aisleId": "1",
          "assignedTask": null,
          "matchedTasks": null
        },
        {
          "aisleId": "2",
          "assignedTask": null,
          "matchedTasks": null
        },
        {
          "aisleId": "3",
          "assignedTask": null,
          "matchedTasks": null
        },
        {
          "aisleId": "4",
          "assignedTask": null,
          "matchedTasks": null
        },
        {
          "aisleId": "5",
          "assignedTask": null,
          "matchedTasks": null
        }
      ],
      "executableTasksByAisle": {
        "1": [],
        "2": [],
        "3": [],
        "4": [],
        "5": []
      }
    }
  }
}
```

---

### 1.2 单梁入库

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

预期：

- `status = SUCCESS`
- `data.aisleAssignments[*].assignedTask.taskId = IN-001`
- `data.aisleAssignments[*].assignedTask.positions` 非空

真实返回摘要：

- `HTTP 200 | SUCCESS | 调度成功`

当前环境实际返回：

```json
{
  "httpStatus": 200,
  "body": {
    "status": "SUCCESS",
    "message": "调度成功",
    "data": {
      "scheduleId": "SCH-A2D8311E",
      "timestamp": "2026-06-05T06:15:37Z",
      "aisleAssignments": [
        {
          "aisleId": "1",
          "assignedTask": {
            "taskId": "IN-001",
            "taskType": "INBOUND",
            "planId": null,
            "planIndex": null,
            "positions": [
              {
                "row": 2,
                "column": 3,
                "level": 9,
                "shelf": "UPPER",
                "skuId": "2801038-TG152",
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
              "taskId": "IN-001",
              "taskType": "INBOUND",
              "planId": null,
              "planIndex": null,
              "positions": [
                {
                  "row": 2,
                  "column": 3,
                  "level": 9,
                  "shelf": "UPPER",
                  "skuId": "2801038-TG152",
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
        {
          "aisleId": "2",
          "assignedTask": null,
          "matchedTasks": null
        },
        {
          "aisleId": "3",
          "assignedTask": null,
          "matchedTasks": null
        },
        {
          "aisleId": "4",
          "assignedTask": null,
          "matchedTasks": null
        },
        {
          "aisleId": "5",
          "assignedTask": null,
          "matchedTasks": null
        }
      ],
      "executableTasksByAisle": {
        "1": [
          {
            "taskId": "IN-001",
            "taskType": "INBOUND",
            "planId": null,
            "planIndex": null,
            "positions": [
              {
                "row": 2,
                "column": 3,
                "level": 9,
                "shelf": "UPPER",
                "skuId": "2801038-TG152",
                "quantity": 1,
                "version": "00",
                "productionAttribute": "D",
                "militaryCivilianMark": "M",
                "salesArea": "N"
              }
            ]
          }
        ],
        "2": [],
        "3": [],
        "4": [],
        "5": []
      }
    }
  }
}
```

---

### 1.3 双梁入库

前置：

- 沿用 `1.2 单梁入库成功` 的同一服务状态

请求体：

```json
{
  "currentTime": "2026-05-24 10:02:00",
  "inventory": [],
  "aisleStatus": [],
  "tasks": [
    {
      "taskId": "IN-002",
      "taskType": "INBOUND",
      "targetAisle": "1",
      "inLine": 1,
      "skus": [
        {
          "skuId": "2801022-TG152",
          "quantity": 1,
          "beamSide": "LEFT",
          "version": "00",
          "productionAttribute": "D",
          "militaryCivilianMark": "M",
          "salesArea": "N"
        },
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

预期：

由于此时仍指定到 `aisle 1`+`inLine 1`，分两种情况：

1. `IN-001` 已完成

- `status = SUCCESS`
- `assignedTask.taskId = IN-002`
- `positions` 返回双梁对应位置

2. `IN-001` 未完成

- `status = SUCCESS`
- `aisle 1` 仍优先返回当前推荐任务或执行中的任务
- `IN-002` 不会立即顶掉前序任务，而是进入 pending 或保持待调度

#### 双梁入库，前序未完成

真实返回摘要：

- `HTTP 200 | SUCCESS | 调度成功`

当前环境实际返回：

```json
{
  "httpStatus": 200,
  "body": {
    "status": "SUCCESS",
    "message": "调度成功",
    "data": {
      "scheduleId": "SCH-109C318A",
      "timestamp": "2026-06-05T06:15:38Z",
      "aisleAssignments": [
        {
          "aisleId": "1",
          "assignedTask": {
            "taskId": "IN-001",
            "taskType": "INBOUND",
            "planId": null,
            "planIndex": null,
            "positions": [
              {
                "row": 2,
                "column": 3,
                "level": 9,
                "shelf": "UPPER",
                "skuId": "2801038-TG152",
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
              "taskId": "IN-001",
              "taskType": "INBOUND",
              "planId": null,
              "planIndex": null,
              "positions": [
                {
                  "row": 2,
                  "column": 3,
                  "level": 9,
                  "shelf": "UPPER",
                  "skuId": "2801038-TG152",
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
        {
          "aisleId": "2",
          "assignedTask": null,
          "matchedTasks": null
        },
        {
          "aisleId": "3",
          "assignedTask": null,
          "matchedTasks": null
        },
        {
          "aisleId": "4",
          "assignedTask": null,
          "matchedTasks": null
        },
        {
          "aisleId": "5",
          "assignedTask": null,
          "matchedTasks": null
        }
      ],
      "executableTasksByAisle": {
        "1": [
          {
            "taskId": "IN-001",
            "taskType": "INBOUND",
            "planId": null,
            "planIndex": null,
            "positions": [
              {
                "row": 2,
                "column": 3,
                "level": 9,
                "shelf": "UPPER",
                "skuId": "2801038-TG152",
                "quantity": 1,
                "version": "00",
                "productionAttribute": "D",
                "militaryCivilianMark": "M",
                "salesArea": "N"
              }
            ]
          }
        ],
        "2": [],
        "3": [],
        "4": [],
        "5": []
      }
    }
  }
}
```

#### 双梁入库，前序已完成

真实返回摘要：

- `HTTP 200 | SUCCESS | 调度成功`

当前环境实际返回：

```json
{
  "httpStatus": 200,
  "body": {
    "status": "SUCCESS",
    "message": "调度成功",
    "data": {
      "scheduleId": "SCH-A6A22E7E",
      "timestamp": "2026-06-05T06:15:38Z",
      "aisleAssignments": [
        {
          "aisleId": "1",
          "assignedTask": {
            "taskId": "IN-002",
            "taskType": "INBOUND",
            "planId": null,
            "planIndex": null,
            "positions": [
              {
                "row": 1,
                "column": 3,
                "level": 9,
                "shelf": "UPPER",
                "skuId": "2801022-TG152",
                "quantity": 1,
                "version": "00",
                "productionAttribute": "D",
                "militaryCivilianMark": "M",
                "salesArea": "N"
              },
              {
                "row": 1,
                "column": 3,
                "level": 9,
                "shelf": "LOWER",
                "skuId": "2801038-TG152",
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
              "taskId": "IN-002",
              "taskType": "INBOUND",
              "planId": null,
              "planIndex": null,
              "positions": [
                {
                  "row": 1,
                  "column": 3,
                  "level": 9,
                  "shelf": "UPPER",
                  "skuId": "2801022-TG152",
                  "quantity": 1,
                  "version": "00",
                  "productionAttribute": "D",
                  "militaryCivilianMark": "M",
                  "salesArea": "N"
                },
                {
                  "row": 1,
                  "column": 3,
                  "level": 9,
                  "shelf": "LOWER",
                  "skuId": "2801038-TG152",
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
        {
          "aisleId": "2",
          "assignedTask": null,
          "matchedTasks": null
        },
        {
          "aisleId": "3",
          "assignedTask": null,
          "matchedTasks": null
        },
        {
          "aisleId": "4",
          "assignedTask": null,
          "matchedTasks": null
        },
        {
          "aisleId": "5",
          "assignedTask": null,
          "matchedTasks": null
        }
      ],
      "executableTasksByAisle": {
        "1": [
          {
            "taskId": "IN-002",
            "taskType": "INBOUND",
            "planId": null,
            "planIndex": null,
            "positions": [
              {
                "row": 1,
                "column": 3,
                "level": 9,
                "shelf": "UPPER",
                "skuId": "2801022-TG152",
                "quantity": 1,
                "version": "00",
                "productionAttribute": "D",
                "militaryCivilianMark": "M",
                "salesArea": "N"
              },
              {
                "row": 1,
                "column": 3,
                "level": 9,
                "shelf": "LOWER",
                "skuId": "2801038-TG152",
                "quantity": 1,
                "version": "00",
                "productionAttribute": "D",
                "militaryCivilianMark": "M",
                "salesArea": "N"
              }
            ]
          }
        ],
        "2": [],
        "3": [],
        "4": [],
        "5": []
      }
    }
  }
}
```

---

### 1.4 同一请求内重复 taskId

请求体：

```json
{
  "currentTime": "2026-05-24 10:03:00",
  "inventory": [],
  "aisleStatus": [],
  "tasks": [
    {
      "taskId": "DUP-001",
      "taskType": "INBOUND",
      "targetAisle": "1",
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
    },
    {
      "taskId": "DUP-001",
      "taskType": "INBOUND",
      "targetAisle": "2",
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

预期：

- HTTP `400`
- `status = FAILED`
- `message` 提示存在重复 `taskId`
- `data.duplicateTaskIds` 或等价字段包含 `DUP-001`

真实返回摘要：

- `HTTP 400 | FAILED | 存在重复的taskId，无法执行调度。`

当前环境实际返回：

```json
{
  "httpStatus": 400,
  "body": {
    "status": "FAILED",
    "message": "存在重复的taskId，无法执行调度。",
    "data": {
      "duplicateTaskIds": [
        "DUP-001"
      ],
      "timestamp": "2026-06-05T06:15:38Z"
    }
  }
}
```

---

### 1.5 与系统内已有 taskId 冲突

步骤：

1. 先执行 `1.2 单梁入库成功`
2. 不回 `EXECUTING`
3. 再发下面请求

请求体：

```json
{
  "currentTime": "2026-05-24 10:04:00",
  "inventory": [],
  "aisleStatus": [],
  "tasks": [
    {
      "taskId": "IN-001",
      "taskType": "INBOUND",
      "targetAisle": "1",
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

预期：

- HTTP `400`
- `status = FAILED`
- `message` 提示已在系统中排队或执行
- `data.conflictingTaskIds` 包含 `IN-001`

真实返回摘要：

- `HTTP 400 | FAILED | 存在已在系统中排队或执行的taskId，无法重复提交。`

当前环境实际返回：

```json
{
  "httpStatus": 400,
  "body": {
    "status": "FAILED",
    "message": "存在已在系统中排队或执行的taskId，无法重复提交。",
    "data": {
      "conflictingTaskIds": [
        "IN-001"
      ],
      "timestamp": "2026-06-05T06:15:38Z"
    }
  }
}
```

---

### 1.6 assignment 仅推荐，EXECUTING 按可执行池校验

步骤：

1. 先执行 `1.2 单梁入库成功`
2. 不回 `EXECUTING`
3. 再发下面请求（同巷道、不同 `inLine`）

请求体：

```json
{
  "currentTime": "2026-05-24 10:05:00",
  "inventory": [],
  "aisleStatus": [],
  "tasks": [
    {
      "taskId": "IN-003",
      "taskType": "INBOUND",
      "targetAisle": "1",
      "inLine": 2,
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

预期：

- `status = SUCCESS`
- `assignedTask` 可能是 `IN-001` 或 `IN-003`（assignment 仅推荐，不再冻结）

真实返回摘要：

- mixed: HTTP 200 | SUCCESS | 调度成功 ;
- executing: HTTP 200 | SUCCESS | 反馈处理成功

当前环境实际返回：

```json
{
  "mixed": {
    "httpStatus": 200,
    "body": {
      "status": "SUCCESS",
      "message": "调度成功",
      "data": {
        "scheduleId": "SCH-85EB2038",
        "timestamp": "2026-06-05T06:15:38Z",
        "aisleAssignments": [
          {
            "aisleId": "1",
            "assignedTask": {
              "taskId": "IN-003",
              "taskType": "INBOUND",
              "planId": null,
              "planIndex": null,
              "positions": [
                {
                  "row": 2,
                  "column": 3,
                  "level": 8,
                  "shelf": "UPPER",
                  "skuId": "2801038-TG152",
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
                "taskId": "IN-003",
                "taskType": "INBOUND",
                "planId": null,
                "planIndex": null,
                "positions": [
                  {
                    "row": 2,
                    "column": 3,
                    "level": 8,
                    "shelf": "UPPER",
                    "skuId": "2801038-TG152",
                    "quantity": 1,
                    "version": "00",
                    "productionAttribute": "D",
                    "militaryCivilianMark": "M",
                    "salesArea": "N"
                  }
                ]
              },
              {
                "taskId": "IN-001",
                "taskType": "INBOUND",
                "planId": null,
                "planIndex": null,
                "positions": [
                  {
                    "row": 2,
                    "column": 3,
                    "level": 9,
                    "shelf": "UPPER",
                    "skuId": "2801038-TG152",
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
          {
            "aisleId": "2",
            "assignedTask": null,
            "matchedTasks": null
          },
          {
            "aisleId": "3",
            "assignedTask": null,
            "matchedTasks": null
          },
          {
            "aisleId": "4",
            "assignedTask": null,
            "matchedTasks": null
          },
          {
            "aisleId": "5",
            "assignedTask": null,
            "matchedTasks": null
          }
        ],
        "executableTasksByAisle": {
          "1": [
            {
              "taskId": "IN-001",
              "taskType": "INBOUND",
              "planId": null,
              "planIndex": null,
              "positions": [
                {
                  "row": 2,
                  "column": 3,
                  "level": 9,
                  "shelf": "UPPER",
                  "skuId": "2801038-TG152",
                  "quantity": 1,
                  "version": "00",
                  "productionAttribute": "D",
                  "militaryCivilianMark": "M",
                  "salesArea": "N"
                }
              ]
            },
            {
              "taskId": "IN-003",
              "taskType": "INBOUND",
              "planId": null,
              "planIndex": null,
              "positions": [
                {
                  "row": 2,
                  "column": 3,
                  "level": 8,
                  "shelf": "UPPER",
                  "skuId": "2801038-TG152",
                  "quantity": 1,
                  "version": "00",
                  "productionAttribute": "D",
                  "militaryCivilianMark": "M",
                  "salesArea": "N"
                }
              ]
            }
          ],
          "2": [],
          "3": [],
          "4": [],
          "5": []
        }
      }
    }
  },
```

```json
  "executing": {
    "httpStatus": 200,
    "body": {
      "status": "SUCCESS",
      "message": "反馈处理成功",
      "data": {
        "taskId": "IN-003",
        "aisleId": "1",
        "positions": [
          {
            "row": 2,
            "column": 3,
            "level": 8,
            "shelf": "UPPER",
            "skuId": "2801038-TG152",
            "quantity": 1
          }
        ]
      }
    }
  }
}
```

---

### 1.7 inventory 非空时按全量快照重建

用途：

- 先建立一份“当前库存基线”
- 后续 `1.8` 会基于这份基线验证空 `inventory` 不会改库存

请求体：

测试之前重置一下API环境，主要是清掉所有任务

```json
{
  "currentTime": "2026-05-24 10:07:00",
  "inventory": [
    {
      "aisleId": "1",
      "row": 2,
      "column": 3,
      "level": 9,
      "shelf": "UPPER",
      "positions": [
        {
          "skuId": "2801038-TG152",
          "quantity": 1,
          "version": "00",
          "productionAttribute": "D",
          "militaryCivilianMark": "M",
          "salesArea": "N"
        }
      ]
    },
    {
      "aisleId": "2",
      "row": 4,
      "column": 2,
      "level": 9,
      "shelf": "UPPER",
      "positions": [
        {
          "skuId": "2801022-TG152",
          "quantity": 1,
          "version": "00",
          "productionAttribute": "D",
          "militaryCivilianMark": "M",
          "salesArea": "N"
        }
      ]
    }
  ],
  "aisleStatus": [],
  "tasks": [
    {
      "taskId": "IN-CHECK-001",
      "taskType": "INBOUND",
      "targetAisle": "1",
      "skus": [
        {
          "skuId": "2801022-TG152",
          "quantity": 1,
          "beamSide": "LEFT",
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

预期：

- `status = SUCCESS`
- 这次请求中未出现的旧货位库存会被移除
- 会基于inventory 传入的库存进行货位选择

真实返回摘要：

- `HTTP 200 | SUCCESS | 调度成功`

当前环境实际返回：

```json
{
  "httpStatus": 200,
  "body": {
    "status": "SUCCESS",
    "message": "调度成功",
    "data": {
      "scheduleId": "SCH-84DC2F73",
      "timestamp": "2026-06-05T06:15:38Z",
      "aisleAssignments": [
        {
          "aisleId": "1",
          "assignedTask": {
            "taskId": "IN-CHECK-001",
            "taskType": "INBOUND",
            "planId": null,
            "planIndex": null,
            "positions": [
              {
                "row": 2,
                "column": 3,
                "level": 9,
                "shelf": "LOWER",
                "skuId": "2801022-TG152",
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
              "taskId": "IN-CHECK-001",
              "taskType": "INBOUND",
              "planId": null,
              "planIndex": null,
              "positions": [
                {
                  "row": 2,
                  "column": 3,
                  "level": 9,
                  "shelf": "LOWER",
                  "skuId": "2801022-TG152",
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
        {
          "aisleId": "2",
          "assignedTask": null,
          "matchedTasks": null
        },
        {
          "aisleId": "3",
          "assignedTask": null,
          "matchedTasks": null
        },
        {
          "aisleId": "4",
          "assignedTask": null,
          "matchedTasks": null
        },
        {
          "aisleId": "5",
          "assignedTask": null,
          "matchedTasks": null
        }
      ],
      "executableTasksByAisle": {
        "1": [
          {
            "taskId": "IN-CHECK-001",
            "taskType": "INBOUND",
            "planId": null,
            "planIndex": null,
            "positions": [
              {
                "row": 2,
                "column": 3,
                "level": 9,
                "shelf": "LOWER",
                "skuId": "2801022-TG152",
                "quantity": 1,
                "version": "00",
                "productionAttribute": "D",
                "militaryCivilianMark": "M",
                "salesArea": "N"
              }
            ]
          }
        ],
        "2": [],
        "3": [],
        "4": [],
        "5": []
      }
    }
  }
}
```

---

### 1.8 inventory 为空时保持原库存不变

前置：

- 先执行 `1.7 inventory 非空时按全量快照重建`

请求体：

```json
{
  "currentTime": "2026-05-24 10:08:00",
  "inventory": [],
  "aisleStatus": [],
  "tasks": [
    {
      "taskId": "IN-CHECK-002",
      "taskType": "INBOUND",
      "targetAisle": "2",
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

预期：

- `status = SUCCESS`
- 库存保持 `1.7` 建立的状态不变,"IN-002" 分配到 2 巷道对应位置

真实返回摘要：

- `HTTP 200 | SUCCESS | 调度成功`

规范/兼容性标注：

- 未发现明显异常。

当前环境实际返回：

```json
{
  "httpStatus": 200,
  "body": {
    "status": "SUCCESS",
    "message": "调度成功",
    "data": {
      "scheduleId": "SCH-5273F618",
      "timestamp": "2026-06-05T06:15:38Z",
      "aisleAssignments": [
        {
          "aisleId": "1",
          "assignedTask": {
            "taskId": "IN-CHECK-001",
            "taskType": "INBOUND",
            "planId": null,
            "planIndex": null,
            "positions": [
              {
                "row": 2,
                "column": 3,
                "level": 9,
                "shelf": "LOWER",
                "skuId": "2801022-TG152",
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
              "taskId": "IN-CHECK-001",
              "taskType": "INBOUND",
              "planId": null,
              "planIndex": null,
              "positions": [
                {
                  "row": 2,
                  "column": 3,
                  "level": 9,
                  "shelf": "LOWER",
                  "skuId": "2801022-TG152",
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
        {
          "aisleId": "2",
          "assignedTask": {
            "taskId": "IN-CHECK-002",
            "taskType": "INBOUND",
            "planId": null,
            "planIndex": null,
            "positions": [
              {
                "row": 4,
                "column": 2,
                "level": 9,
                "shelf": "LOWER",
                "skuId": "2801038-TG152",
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
              "taskId": "IN-CHECK-002",
              "taskType": "INBOUND",
              "planId": null,
              "planIndex": null,
              "positions": [
                {
                  "row": 4,
                  "column": 2,
                  "level": 9,
                  "shelf": "LOWER",
                  "skuId": "2801038-TG152",
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
        {
          "aisleId": "3",
          "assignedTask": null,
          "matchedTasks": null
        },
        {
          "aisleId": "4",
          "assignedTask": null,
          "matchedTasks": null
        },
        {
          "aisleId": "5",
          "assignedTask": null,
          "matchedTasks": null
        }
      ],
      "executableTasksByAisle": {
        "1": [
          {
            "taskId": "IN-CHECK-001",
            "taskType": "INBOUND",
            "planId": null,
            "planIndex": null,
            "positions": [
              {
                "row": 2,
                "column": 3,
                "level": 9,
                "shelf": "LOWER",
                "skuId": "2801022-TG152",
                "quantity": 1,
                "version": "00",
                "productionAttribute": "D",
                "militaryCivilianMark": "M",
                "salesArea": "N"
              }
            ]
          }
        ],
        "2": [
          {
            "taskId": "IN-CHECK-002",
            "taskType": "INBOUND",
            "planId": null,
            "planIndex": null,
            "positions": [
              {
                "row": 4,
                "column": 2,
                "level": 9,
                "shelf": "LOWER",
                "skuId": "2801038-TG152",
                "quantity": 1,
                "version": "00",
                "productionAttribute": "D",
                "militaryCivilianMark": "M",
                "salesArea": "N"
              }
            ]
          }
        ],
        "3": [],
        "4": [],
        "5": []
      }
    }
  }
}
```

---

## 2. GET `/api/v1/task/unconfirmed`

真实返回摘要：

- `unconfirmed: HTTP 200 | SUCCESS | 获取可执行任务成功`

当前环境实际返回：

```json
  "unconfirmed": {
    "httpStatus": 200,
    "body": {
      "status": "SUCCESS",
      "message": "获取可执行任务成功",
      "data": {
        "tasksByAisle": {
          "1": {
            "can_executing": [
              {
                "taskId": "IN-L1-001",
                "taskType": "INBOUND",
                "aisleId": "1",
                "positions": [
                  {
                    "row": 2,
                    "column": 3,
                    "level": 9,
                    "shelf": "UPPER",
                    "skuId": "2801038-TG152",
                    "quantity": 1
                  }
                ]
              },
              {
                "taskId": "IN-L2-001",
                "taskType": "INBOUND",
                "aisleId": "1",
                "positions": [
                  {
                    "row": 2,
                    "column": 3,
                    "level": 8,
                    "shelf": "UPPER",
                    "skuId": "2801038-TG152",
                    "quantity": 1
                  }
                ]
              }
            ],
            "pending": [
              {
                "taskId": "IN-L1-002",
                "taskType": "INBOUND",
                "aisleId": "1",
                "positions": [
                  {
                    "row": 2,
                    "column": 3,
                    "level": 10,
                    "shelf": "UPPER",
                    "skuId": "2801038-TG152",
                    "quantity": 1
                  }
                ]
              }
            ],
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
          },
          "5": {
            "can_executing": [],
            "pending": [],
            "running": []
          }
        },
        "canAcceptExecuting": true
      }
    }
  }
}
```

---

## 3. POST `/api/v1/task/feedback`

建议执行方式：

- 本章各用例尽量独立执行
- 也就是说每执行一条 `3.x` 用例前，先重新执行一次下面这条“统一前置大调度”
- 如果不想重复执行，也至少要保证相关任务仍处于已下发、未完成状态

### 3.0 统一前置大调度

请求体：

```json
{
  "currentTime": "2026-05-24 10:08:30",
  "inventory": [],
  "aisleStatus": [],
  "tasks": [
    {
      "taskId": "IN-001",
      "taskType": "INBOUND",
      "targetAisle": "1",
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
    },
    {
      "taskId": "IN-004",
      "taskType": "INBOUND",
      "targetAisle": "2",
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
    },
    {
      "taskId": "IN-005",
      "taskType": "INBOUND",
      "targetAisle": "3",
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
    },
    {
      "taskId": "IN-006",
      "taskType": "INBOUND",
      "targetAisle": "4",
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
    },
    {
      "taskId": "IN-007",
      "taskType": "INBOUND",
      "targetAisle": "5",
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

预期：

- `status = SUCCESS`
- `data.aisleAssignments` 中能看到 `IN-001`、`IN-004`、`IN-005`、`IN-006`、`IN-007`
- 这些任务应进入“可在后续提交 EXECUTING 的候选队列”状态

真实返回摘要：

- `HTTP 200 | SUCCESS | 调度成功`

当前环境实际返回：

```json
{
  "httpStatus": 200,
  "body": {
    "status": "SUCCESS",
    "message": "调度成功",
    "data": {
      "scheduleId": "SCH-C855CA22",
      "timestamp": "2026-06-05T06:55:02Z",
      "aisleAssignments": [
        {
          "aisleId": "1",
          "assignedTask": {
            "taskId": "IN-001",
            "taskType": "INBOUND",
            "planId": null,
            "planIndex": null,
            "positions": [
              {
                "row": 2,
                "column": 3,
                "level": 9,
                "shelf": "UPPER",
                "skuId": "2801038-TG152",
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
              "taskId": "IN-001",
              "taskType": "INBOUND",
              "planId": null,
              "planIndex": null,
              "positions": [
                {
                  "row": 2,
                  "column": 3,
                  "level": 9,
                  "shelf": "UPPER",
                  "skuId": "2801038-TG152",
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
        {
          "aisleId": "2",
          "assignedTask": {
            "taskId": "IN-004",
            "taskType": "INBOUND",
            "planId": null,
            "planIndex": null,
            "positions": [
              {
                "row": 4,
                "column": 3,
                "level": 9,
                "shelf": "UPPER",
                "skuId": "2801038-TG152",
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
              "taskId": "IN-004",
              "taskType": "INBOUND",
              "planId": null,
              "planIndex": null,
              "positions": [
                {
                  "row": 4,
                  "column": 3,
                  "level": 9,
                  "shelf": "UPPER",
                  "skuId": "2801038-TG152",
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
        {
          "aisleId": "3",
          "assignedTask": {
            "taskId": "IN-005",
            "taskType": "INBOUND",
            "planId": null,
            "planIndex": null,
            "positions": [
              {
                "row": 6,
                "column": 3,
                "level": 9,
                "shelf": "UPPER",
                "skuId": "2801038-TG152",
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
              "taskId": "IN-005",
              "taskType": "INBOUND",
              "planId": null,
              "planIndex": null,
              "positions": [
                {
                  "row": 6,
                  "column": 3,
                  "level": 9,
                  "shelf": "UPPER",
                  "skuId": "2801038-TG152",
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
        {
          "aisleId": "4",
          "assignedTask": {
            "taskId": "IN-006",
            "taskType": "INBOUND",
            "planId": null,
            "planIndex": null,
            "positions": [
              {
                "row": 8,
                "column": 3,
                "level": 9,
                "shelf": "UPPER",
                "skuId": "2801038-TG152",
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
              "taskId": "IN-006",
              "taskType": "INBOUND",
              "planId": null,
              "planIndex": null,
              "positions": [
                {
                  "row": 8,
                  "column": 3,
                  "level": 9,
                  "shelf": "UPPER",
                  "skuId": "2801038-TG152",
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
        {
          "aisleId": "5",
          "assignedTask": {
            "taskId": "IN-007",
            "taskType": "INBOUND",
            "planId": null,
            "planIndex": null,
            "positions": [
              {
                "row": 10,
                "column": 3,
                "level": 9,
                "shelf": "UPPER",
                "skuId": "2801038-TG152",
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
              "taskId": "IN-007",
              "taskType": "INBOUND",
              "planId": null,
              "planIndex": null,
              "positions": [
                {
                  "row": 10,
                  "column": 3,
                  "level": 9,
                  "shelf": "UPPER",
                  "skuId": "2801038-TG152",
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
            "taskId": "IN-001",
            "taskType": "INBOUND",
            "planId": null,
            "planIndex": null,
            "positions": [
              {
                "row": 2,
                "column": 3,
                "level": 9,
                "shelf": "UPPER",
                "skuId": "2801038-TG152",
                "quantity": 1,
                "version": "00",
                "productionAttribute": "D",
                "militaryCivilianMark": "M",
                "salesArea": "N"
              }
            ]
          }
        ],
        "2": [
          {
            "taskId": "IN-004",
            "taskType": "INBOUND",
            "planId": null,
            "planIndex": null,
            "positions": [
              {
                "row": 4,
                "column": 3,
                "level": 9,
                "shelf": "UPPER",
                "skuId": "2801038-TG152",
                "quantity": 1,
                "version": "00",
                "productionAttribute": "D",
                "militaryCivilianMark": "M",
                "salesArea": "N"
              }
            ]
          }
        ],
        "3": [
          {
            "taskId": "IN-005",
            "taskType": "INBOUND",
            "planId": null,
            "planIndex": null,
            "positions": [
              {
                "row": 6,
                "column": 3,
                "level": 9,
                "shelf": "UPPER",
                "skuId": "2801038-TG152",
                "quantity": 1,
                "version": "00",
                "productionAttribute": "D",
                "militaryCivilianMark": "M",
                "salesArea": "N"
              }
            ]
          }
        ],
        "4": [
          {
            "taskId": "IN-006",
            "taskType": "INBOUND",
            "planId": null,
            "planIndex": null,
            "positions": [
              {
                "row": 8,
                "column": 3,
                "level": 9,
                "shelf": "UPPER",
                "skuId": "2801038-TG152",
                "quantity": 1,
                "version": "00",
                "productionAttribute": "D",
                "militaryCivilianMark": "M",
                "salesArea": "N"
              }
            ]
          }
        ],
        "5": [
          {
            "taskId": "IN-007",
            "taskType": "INBOUND",
            "planId": null,
            "planIndex": null,
            "positions": [
              {
                "row": 10,
                "column": 3,
                "level": 9,
                "shelf": "UPPER",
                "skuId": "2801038-TG152",
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
    }
  }
}
```

---

### 3.1 默认 EXECUTING，沿用 mixed 结果

请求体：

```json
{
  "taskId": "IN-001",
  "taskType": "INBOUND",
  "status": "EXECUTING",
  "startTime": "2026-05-24T10:09:00Z"
}
```

预期：

- `status = SUCCESS`
- `data.taskId = IN-001`
- `data.aisleId` 存在
- `data.positions` 返回 mixed 已分配的位置

真实返回摘要：

- `HTTP 200 | SUCCESS | 反馈处理成功`

当前环境实际返回：

```json
{
  "httpStatus": 200,
  "body": {
    "status": "SUCCESS",
    "message": "反馈处理成功",
    "data": {
      "taskId": "IN-001",
      "aisleId": "1",
      "positions": [
        {
          "row": 2,
          "column": 3,
          "level": 9,
          "shelf": "UPPER",
          "skuId": "2801038-TG152",
          "quantity": 1
        }
      ]
    }
  }
}
```

---

### 3.2 COMPLETED 成功

请求体：

```json
{
  "taskId": "IN-001",
  "taskType": "INBOUND",
  "status": "COMPLETED",
  "startTime": "2026-05-24T10:10:00Z"
}
```

预期：

- `status = SUCCESS`
- 任务完成

实际响应：

```json
{
    "status": "SUCCESS",
    "message": "反馈处理成功",
    "data": null
}
```

真实返回摘要：

- `HTTP 200 | SUCCESS | 反馈处理成功`

当前环境实际返回：

```json
{
  "httpStatus": 200,
  "body": {
    "status": "SUCCESS",
    "message": "反馈处理成功",
    "data": null
  }
}
```

---

### 3.3 入库位置固化 + 预占验证（同巷道连续入库）

接口：`POST /api/v1/schedule/mixed`

第一拍请求体：

```json
{
  "currentTime": "2026-05-30 10:00:00",
  "inventory": [],
  "aisleStatus": [],
  "tasks": [
    {
      "taskId": "IN-FIX-001",
      "taskType": "INBOUND",
      "targetAisle": "3",
      "inLine": 1,
      "skus": [
        {
          "skuId": "2801022-TG152",
          "quantity": 1,
          "beamSide": "LEFT",
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

第二拍请求体（追加第二个入库）：

```json
{
  "currentTime": "2026-05-30 10:01:00",
  "inventory": [],
  "aisleStatus": [],
  "tasks": [
    {
      "taskId": "IN-FIX-002",
      "taskType": "INBOUND",
      "targetAisle": "3",
      "inLine": 2,
      "skus": [
        {
          "skuId": "2801022-TG152",
          "quantity": 1,
          "beamSide": "LEFT",
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

预期：

- `IN-FIX-001` 的 `positions` 在第二拍后保持不变。
- `IN-FIX-002` 不能复用 `IN-FIX-001` 已固化货位。
- `executableTasksByAisle["3"]` 中任务位置与 mixed 返回一致。

真实返回摘要：

- `first: HTTP 200 | SUCCESS | 调度成功 ; second: HTTP 200 | SUCCESS | 调度成功`

当前环境实际返回：

```json
{
  "first": {
    "httpStatus": 200,
    "body": {
      "status": "SUCCESS",
      "message": "调度成功",
      "data": {
        "scheduleId": "SCH-32BFBD07",
        "timestamp": "2026-06-05T06:55:02Z",
        "aisleAssignments": [
          {
            "aisleId": "1",
            "assignedTask": null,
            "matchedTasks": null
          },
          {
            "aisleId": "2",
            "assignedTask": null,
            "matchedTasks": null
          },
          {
            "aisleId": "3",
            "assignedTask": {
              "taskId": "IN-FIX-001",
              "taskType": "INBOUND",
              "planId": null,
              "planIndex": null,
              "positions": [
                {
                  "row": 5,
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
            },
            "matchedTasks": [
              {
                "taskId": "IN-FIX-001",
                "taskType": "INBOUND",
                "planId": null,
                "planIndex": null,
                "positions": [
                  {
                    "row": 5,
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
            ]
          },
          {
            "aisleId": "4",
            "assignedTask": null,
            "matchedTasks": null
          },
          {
            "aisleId": "5",
            "assignedTask": null,
            "matchedTasks": null
          }
        ],
        "executableTasksByAisle": {
          "1": [],
          "2": [],
          "3": [
            {
              "taskId": "IN-FIX-001",
              "taskType": "INBOUND",
              "planId": null,
              "planIndex": null,
              "positions": [
                {
                  "row": 5,
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
          ],
          "4": [],
          "5": []
        }
      }
    }
  },
  "second": {
    "httpStatus": 200,
    "body": {
      "status": "SUCCESS",
      "message": "调度成功",
      "data": {
        "scheduleId": "SCH-96F4F4BC",
        "timestamp": "2026-06-05T06:55:02Z",
        "aisleAssignments": [
          {
            "aisleId": "1",
            "assignedTask": null,
            "matchedTasks": null
          },
          {
            "aisleId": "2",
            "assignedTask": null,
            "matchedTasks": null
          },
          {
            "aisleId": "3",
            "assignedTask": {
              "taskId": "IN-FIX-001",
              "taskType": "INBOUND",
              "planId": null,
              "planIndex": null,
              "positions": [
                {
                  "row": 5,
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
            },
            "matchedTasks": [
              {
                "taskId": "IN-FIX-001",
                "taskType": "INBOUND",
                "planId": null,
                "planIndex": null,
                "positions": [
                  {
                    "row": 5,
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
              },
              {
                "taskId": "IN-FIX-002",
                "taskType": "INBOUND",
                "planId": null,
                "planIndex": null,
                "positions": [
                  {
                    "row": 5,
                    "column": 3,
                    "level": 8,
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
            ]
          },
          {
            "aisleId": "4",
            "assignedTask": null,
            "matchedTasks": null
          },
          {
            "aisleId": "5",
            "assignedTask": null,
            "matchedTasks": null
          }
        ],
        "executableTasksByAisle": {
          "1": [],
          "2": [],
          "3": [
            {
              "taskId": "IN-FIX-001",
              "taskType": "INBOUND",
              "planId": null,
              "planIndex": null,
              "positions": [
                {
                  "row": 5,
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
            },
            {
              "taskId": "IN-FIX-002",
              "taskType": "INBOUND",
              "planId": null,
              "planIndex": null,
              "positions": [
                {
                  "row": 5,
                  "column": 3,
                  "level": 8,
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
          ],
          "4": [],
          "5": []
        }
      }
    }
  }
}
```

---

### 3.4 调整接口验证（切换巷道/SKU/位置）

接口：`POST /api/v1/task/adjust`

前置：先调用一次 `POST /api/v1/schedule/mixed`，将两条入库任务送入系统，但都不要先回 `EXECUTING`。

前置请求体：

```json
{
  "currentTime": "2026-06-14 10:00:00",
  "inventory": [],
  "aisleStatus": [],
  "tasks": [
    {
      "taskId": "IN-BLOCK-001",
      "taskType": "INBOUND",
      "targetAisle": "2",
      "inLine": 1,
      "skus": [
        {
          "skuId": "2801022-TG152",
          "quantity": 1,
          "beamSide": "LEFT",
          "version": "00",
          "productionAttribute": "D",
          "militaryCivilianMark": "M",
          "salesArea": "N"
        }
      ]
    },
    {
      "taskId": "IN-FIX-002",
      "taskType": "INBOUND",
      "targetAisle": "3",
      "inLine": 1,
      "skus": [
        {
          "skuId": "2801022-TG152",
          "quantity": 1,
          "beamSide": "LEFT",
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

前置实际返回：

```json
{
  "httpStatus": 200,
  "body": {
    "status": "SUCCESS",
    "message": "调度成功",
    "data": {
      "scheduleId": "SCH-CF86518B",
      "timestamp": "2026-06-18T09:54:46Z",
      "aisleAssignments": [
        {
          "aisleId": "1",
          "assignedTask": null,
          "matchedTasks": null
        },
        {
          "aisleId": "2",
          "assignedTask": {
            "taskId": "IN-BLOCK-001",
            "taskType": "INBOUND",
            "planId": null,
            "planIndex": null,
            "positions": [
              {
                "row": 3,
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
          },
          "matchedTasks": [
            {
              "taskId": "IN-BLOCK-001",
              "taskType": "INBOUND",
              "planId": null,
              "planIndex": null,
              "positions": [
                {
                  "row": 3,
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
          ]
        },
        {
          "aisleId": "3",
          "assignedTask": {
            "taskId": "IN-FIX-002",
            "taskType": "INBOUND",
            "planId": null,
            "planIndex": null,
            "positions": [
              {
                "row": 5,
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
          },
          "matchedTasks": [
            {
              "taskId": "IN-FIX-002",
              "taskType": "INBOUND",
              "planId": null,
              "planIndex": null,
              "positions": [
                {
                  "row": 5,
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
          ]
        },
        {
          "aisleId": "4",
          "assignedTask": null,
          "matchedTasks": null
        },
        {
          "aisleId": "5",
          "assignedTask": null,
          "matchedTasks": null
        }
      ]
    }
  }
}
```

请求体 1（切换到新巷道并改 SKU）：

```json
{
  "taskId": "IN-FIX-002",
  "taskType": "INBOUND",
  "aisleId": "2",
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
```

预期：

- 返回 `status = SUCCESS`。
- 返回的 `data.aisleId = "2"`。
- 返回 `data.positions` 非空，且位置不与当前可执行/执行中任务冲突。

实际返回：

```json
{
  "httpStatus": 200,
  "body": {
    "status": "SUCCESS",
    "message": "任务调整成功",
    "data": {
      "taskId": "IN-FIX-002",
      "aisleId": "2",
      "positions": [
        {
          "row": 4,
          "column": 3,
          "level": 9,
          "shelf": "UPPER",
          "skuId": "2801038-TG152",
          "quantity": 1
        }
      ]
    }
  }
}
```

冲突失败用例（显式指定冲突位置，真实可复现）：

从前置 mixed 响应中取 2 巷道任务（`IN-BLOCK-001`）的已分配位置，作为下面 adjust 的 `positions`。

请求体 2（显式指定冲突位置）：

```json
{
  "taskId": "IN-FIX-002",
  "taskType": "INBOUND",
  "aisleId": "2",
  "positions": [
    {
      "row": 1,
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

预期：

- 返回 `status = FAILED`。
- HTTP 状态码为 `409`。
- `data.reason` 包含占位冲突语义。
- 冲突位置按当前 adjust 接口口径返回为 `aisle-row-column-level`，其中 `row` 为巷道内行号，仅为 `1` 或 `2`，例如：`2-1-03-09`。

实际返回：

```json
{
  "httpStatus": 409,
  "body": {
    "status": "FAILED",
    "message": "任务调整失败: IN-FIX-002",
    "data": {
      "taskId": "IN-FIX-002",
      "reason": "位置冲突: 2-1-03-09"
    }
  }
}
```

非法 row 失败补充（位置合法性）：

请求体 3（目标巷道为 `2`，但 row 非法）：

```json
{
  "taskId": "IN-FIX-002",
  "taskType": "INBOUND",
  "aisleId": "2",
  "positions": [
    {
      "row": 5,
      "column": 3,
      "level": 9,
      "shelf": "UPPER",
      "skuId": "2801038-TG152",
      "quantity": 1,
      "version": "00",
      "productionAttribute": "D",
      "militaryCivilianMark": "M",
      "salesArea": "N"
    }
  ]
}
```

预期：

- 返回 `status = FAILED`。
- HTTP 状态码为 `400`。
- `data.reason` 包含 `positions 无效` 语义。
- 不会进入冲突判定，也不会自动映射到合法行。

实际返回：

```json
{
  "httpStatus": 400,
  "body": {
    "status": "FAILED",
    "message": "任务调整失败: IN-FIX-002",
    "data": {
      "taskId": "IN-FIX-002",
      "reason": "任务 IN-FIX-002 提供的 positions 无效"
    }
  }
}
```

调整后查询接口：`GET /api/v1/task/unconfirmed`

实际返回：

```json
{
  "httpStatus": 200,
  "body": {
    "status": "SUCCESS",
    "message": "获取可执行任务成功",
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
              "taskId": "IN-FIX-002",
              "taskType": "INBOUND",
              "aisleId": "2",
              "positions": [
                {
                  "row": 4,
                  "column": 3,
                  "level": 9,
                  "shelf": "UPPER",
                  "skuId": "2801038-TG152",
                  "quantity": 1
                }
              ]
            }
          ],
          "pending": [
            {
              "taskId": "IN-BLOCK-001",
              "taskType": "INBOUND",
              "aisleId": "2",
              "positions": [
                {
                  "row": 3,
                  "column": 3,
                  "level": 9,
                  "shelf": "UPPER",
                  "skuId": "2801022-TG152",
                  "quantity": 1
                }
              ]
            }
          ],
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
        },
        "5": {
          "can_executing": [],
          "pending": [],
          "running": []
        }
      },
      "canAcceptExecuting": true
    }
  }
}
```

---

## 4. POST `/api/v1/inbound/allocate`

这里需要说明的是，`POST /api/v1/inbound/allocate` 仅用于“推荐巷道”，不会真正创建调度结果，也不会把任务加入系统待执行队列。也就是说，这个接口主要用来验证当前库存、配对关系、pending / executing 模拟占位对推荐顺序的影响；如果要让任务真正进入系统并参与后续 `EXECUTING / COMPLETED` 反馈，仍然需要调用 `POST /api/v1/schedule/mixed` 做实际调度。

### 4.1 基础推荐巷道

请求体：

```json
{
  "tasks": [
    {
      "taskId": "ALLOC-001",
      "skus": [
        {
          "skuId": "2801038-TG152",
          "quantity": 1,
          "beamSide": "LEFT",
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

预期：

- `status = SUCCESS`
- `data.assignments[0].taskId = ALLOC-001`
- `data.assignments[0].recommendedAisle` 存在

真实返回摘要：

- `HTTP 200 | SUCCESS | 入库分配成功`

当前环境实际返回：

```json
{
  "httpStatus": 200,
  "body": {
    "status": "SUCCESS",
    "message": "入库分配成功",
    "data": {
      "allocationId": "ALLOC-61FDE83C",
      "assignments": [
        {
          "taskId": "ALLOC-001",
          "recommendedAisle": "1"
        }
      ]
    }
  }
}
```

---

### 4.2 连续配对梁的基线顺序

目的：

- 验证同一对配对梁已经分散放在 1/2/3 巷道，且数量分别为 1/2/3 个时，新的同类入库任务推荐顺序是否符合“连续配对优先”的策略。

前置：

- 先重启服务，确保无历史 pending / executing 干扰。
- 用 `POST /api/v1/schedule/mixed` 写入一份基线库存：
  - 1 巷道放 1 个可与新任务配对的 mate 梁
  - 2 巷道放 2 个同类 mate 梁
  - 3 巷道放 3 个同类 mate 梁

库存示意：

```json
{
  "currentTime": "2026-05-24 11:00:00",
  "inventory": [
    {
      "aisleId": "1",
      "row": 1,
      "column": 2,
      "level": 9,
      "shelf": "UPPER",
      "positions": [
        {
          "skuId": "2801038-TG152",
          "quantity": 1,
          "version": "00",
          "productionAttribute": "D",
          "militaryCivilianMark": "M",
          "salesArea": "N"
        }
      ]
    },
    {
      "aisleId": "2",
      "row": 3,
      "column": 2,
      "level": 9,
      "shelf": "UPPER",
      "positions": [
        {
          "skuId": "2801038-TG152",
          "quantity": 1,
          "version": "00",
          "productionAttribute": "D",
          "militaryCivilianMark": "M",
          "salesArea": "N"
        }
      ]
    },
    {
      "aisleId": "2",
      "row": 4,
      "column": 2,
      "level": 9,
      "shelf": "UPPER",
      "positions": [
        {
          "skuId": "2801038-TG152",
          "quantity": 1,
          "version": "00",
          "productionAttribute": "D",
          "militaryCivilianMark": "M",
          "salesArea": "N"
        }
      ]
    },
    {
      "aisleId": "3",
      "row": 5,
      "column": 2,
      "level": 9,
      "shelf": "UPPER",
      "positions": [
        {
          "skuId": "2801038-TG152",
          "quantity": 1,
          "version": "00",
          "productionAttribute": "D",
          "militaryCivilianMark": "M",
          "salesArea": "N"
        }
      ]
    },
    {
      "aisleId": "3",
      "row": 6,
      "column": 2,
      "level": 9,
      "shelf": "UPPER",
      "positions": [
        {
          "skuId": "2801038-TG152",
          "quantity": 1,
          "version": "00",
          "productionAttribute": "D",
          "militaryCivilianMark": "M",
          "salesArea": "N"
        }
      ]
    },
    {
      "aisleId": "3",
      "row": 6,
      "column": 1,
      "level": 9,
      "shelf": "UPPER",
      "positions": [
        {
          "skuId": "2801038-TG152",
          "quantity": 1,
          "version": "00",
          "productionAttribute": "D",
          "militaryCivilianMark": "M",
          "salesArea": "N"
        }
      ]
    }
  ],
  "aisleStatus": [],
  "tasks": []
}
```

然后调用 `POST /api/v1/inbound/allocate`：

```json
{
  "tasks": [
    {
      "taskId": "ALLOC-PAIR-001",
      "skus": [
        {
          "skuId": "2801022-TG152",
          "quantity": 1,
          "beamSide": "LEFT",
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

预期：

- `status = SUCCESS`
- `data.assignments[0].recommendedAisle` 存在
- 如果当前策略是“连续配对数量越多越优先”，则应优先推荐 `3`

真实返回摘要：

- `HTTP 200 | SUCCESS | 入库分配成功`

当前环境实际返回：

```json
{
  "httpStatus": 200,
  "body": {
    "status": "SUCCESS",
    "message": "入库分配成功",
    "data": {
      "allocationId": "ALLOC-44214A9B",
      "assignments": [
        {
          "taskId": "ALLOC-PAIR-001",
          "recommendedAisle": "3"
        }
      ]
    }
  }
}
```

---

### 4.3 pending 入库是否影响推荐顺序

目的：

- 验证未执行完成、仍停留在 pending 中的同类入库任务，是否会被纳入巷道推荐判断。

步骤：

1. 先执行 `4.2` 的基线库存写入。
2. 调一次 `POST /api/v1/schedule/mixed`，向 `3` 巷道送入该配对入库任务IN-PEND-001，但不要回 `EXECUTING`。

示意请求：

```json
{
  "currentTime": "2026-05-24 11:05:00",
  "inventory": [],
  "aisleStatus": [],
  "tasks": [
    {
      "taskId": "IN-PEND-001",
      "taskType": "INBOUND",
      "targetAisle": "3",
      "skus": [
        {
          "skuId": "2801022-TG152",
          "quantity": 1,
          "beamSide": "LEFT",
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

3. 不做 feedback，直接再次调用 `POST /api/v1/inbound/allocate`，仍然推荐同类新任务ALLOC-PAIR-002。

```json
{
  "tasks": [
    {
      "taskId": "ALLOC-PAIR-002",
      "skus": [
        {
          "skuId": "2801022-TG152",
          "quantity": 1,
          "beamSide": "LEFT",
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

预期：

- `status = SUCCESS`
- 推荐结果会受到这条 pending 入库的影响，所以推荐巷道会变为 `2` 巷道
- 也就是推荐顺序不只看真实库存，还会把 pending 模拟占位一并考虑进去

真实返回摘要：

- `HTTP 200 | SUCCESS | 入库分配成功`

当前环境实际返回：

```json
{
  "httpStatus": 200,
  "body": {
    "status": "SUCCESS",
    "message": "入库分配成功",
    "data": {
      "allocationId": "ALLOC-E742759E",
      "assignments": [
        {
          "taskId": "ALLOC-PAIR-002",
          "recommendedAisle": "2"
        }
      ]
    }
  }
}
```

---

### 4.4 executing 入库是否影响推荐顺序

目的：

- 验证已经进入 `EXECUTING`、但尚未 `COMPLETED` 的入库任务，是否会继续影响推荐巷道。

步骤：

1. 以执行 `4.3` 为基准。
2. 对IN-PEND-001这条任务回 `POST /api/v1/task/feedback` 的 `EXECUTING`，但先不要 `COMPLETED`。
3. 此时再次调用 `POST /api/v1/inbound/allocate` 推荐相同的配对新任务ALLOC-PAIR-002。

```json
{
  "tasks": [
    {
      "taskId": "ALLOC-PAIR-002",
      "skus": [
        {
          "skuId": "2801022-TG152",
          "quantity": 1,
          "beamSide": "LEFT",
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

预期：

- `status = SUCCESS`
- 推荐结果会继续受 executing 入库影响，所以仍然推荐巷道 `2`
- 也就是 executing 任务对应的模拟占位仍然参与巷道优先级判断

真实返回摘要：

- `HTTP 200 | SUCCESS | 入库分配成功`

当前环境实际返回：

```json
{
  "httpStatus": 200,
  "body": {
    "status": "SUCCESS",
    "message": "入库分配成功",
    "data": {
      "allocationId": "ALLOC-528E37B7",
      "assignments": [
        {
          "taskId": "ALLOC-PAIR-002",
          "recommendedAisle": "2"
        }
      ]
    }
  }
}
```

---

### 4.5 连续推荐下 pending / executing 的影响验证

目的：

- 验证在“1 巷道有 1 个 mate、2 巷道有 2 个 mate、3 巷道有 3 个 mate”的基线下，
- 当 3 巷道已执行完成一个配对（剩下2个配对）， 再给2/3 巷道已经额外挂同类 `2801022-TG152` 的 pending 或 executing 入库任务时，
- 推荐结果是否会优先避开这些巷道，并回到仍可继续接配对任务的 1 巷道。

前置：

- 以 `4.4` 为基准，IN-PEND-001 已在 `3` 巷道执行完成一个配对。
- 再通过 `POST /api/v1/schedule/mixed` 分别向 `2`、`3` 巷道送入一条 `2801022-TG152` 的入库任务。此时 1 巷道剩余 1 个 mate、2 巷道剩余 1 个 mate，1 个 pending、3 巷道有 1 个 mate， 1 个 pending， 1 个 已配对。

示意请求 1：给 2 巷道挂一条同类入库

```json
{
  "currentTime": "2026-05-24 11:10:00",
  "inventory": [],
  "aisleStatus": [],
  "tasks": [
    {
      "taskId": "IN-PEND-002",
      "taskType": "INBOUND",
      "targetAisle": "2",
      "skus": [
        {
          "skuId": "2801022-TG152",
          "quantity": 1,
          "beamSide": "LEFT",
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

示意请求 2：给 3 巷道挂一条同类入库

```json
{
  "currentTime": "2026-05-24 11:11:00",
  "inventory": [],
  "aisleStatus": [],
  "tasks": [
    {
      "taskId": "IN-PEND-003",
      "taskType": "INBOUND",
      "targetAisle": "3",
      "skus": [
        {
          "skuId": "2801022-TG152",
          "quantity": 1,
          "beamSide": "LEFT",
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

然后再次调用 `POST /api/v1/inbound/allocate`：

```json
{
  "tasks": [
    {
      "taskId": "ALLOC-PAIR-004",
      "skus": [
        {
          "skuId": "2801022-TG152",
          "quantity": 1,
          "beamSide": "LEFT",
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

预期：

- `status = SUCCESS`
- 当 2/3 巷道已经被同类 pending / executing 入库占位后，推荐结果应回到 `1` 巷道
- 这说明当前推荐逻辑会把 pending / executing 入库当作“即将占用”的配对位置一起纳入判断
- 如果此时 `recommendedAisle` 不是 `1`，需要继续检查：
  - 当前基线库存是否落在可用货位
  - 1 巷道的 mate 货位是否仍然可参与配对
  - 2/3 巷道的 pending / executing 是否已正确进入模拟占位

说明：

- 这一条用例主要验证“策略本身是否会正确避让 pending / executing 影响后的巷道”。
- 在关闭禁用货位影响后，如果基线库存写在可用货位上，当前策略已经能够正常把这类任务推荐回 `1` 巷道。

真实返回摘要：

- `HTTP 200 | SUCCESS | 入库分配成功`

当前环境实际返回：

```json
{
  "httpStatus": 200,
  "body": {
    "status": "SUCCESS",
    "message": "入库分配成功",
    "data": {
      "allocationId": "ALLOC-4B309E60",
      "assignments": [
        {
          "taskId": "ALLOC-PAIR-004",
          "recommendedAisle": "1"
        }
      ]
    }
  }
}
```

---

## 5. 生产计划与 currentGroups

### 5.1 currentGroups = 2，直接调第 2 组

请求体示意：

```json
{
  "currentTime": "2026-05-24 10:15:00",
  "productionPlan": {
    "operationType": "UPDATE",
    "planDate": "2026-05-24 09:00:00",
    "plans": [
      {
        "planId": "PLAN-LINE1-THREE-GROUPS",
        "lineId": "LINE-1",
        "planIndex": [
          {
            "requiredSkus": [
              [
                {
                  "skuId": "2801022-TG152",
                  "quantity": 1,
                  "version": "00",
                  "productionAttribute": "D",
                  "militaryCivilianMark": "M",
                  "salesArea": "N"
                },
                {
                  "skuId": "2801038-TG152",
                  "quantity": 1,
                  "version": "00",
                  "productionAttribute": "D",
                  "militaryCivilianMark": "M",
                  "salesArea": "N"
                }
              ]
            ]
          },
          {
            "requiredSkus": [
              [
                {
                  "skuId": "2801021-TG152",
                  "quantity": 1,
                  "version": "00",
                  "productionAttribute": "D",
                  "militaryCivilianMark": "M",
                  "salesArea": "N"
                },
                {
                  "skuId": "2801037-TG152",
                  "quantity": 1,
                  "version": "00",
                  "productionAttribute": "D",
                  "militaryCivilianMark": "M",
                  "salesArea": "N"
                }
              ]
            ]
          },
          {
            "requiredSkus": [
              [
                {
                  "skuId": "2801021-H19H0",
                  "quantity": 1,
                  "version": "00",
                  "productionAttribute": "D",
                  "militaryCivilianMark": "M",
                  "salesArea": "N"
                },
                {
                  "skuId": "2801037-H19H0",
                  "quantity": 1,
                  "version": "00",
                  "productionAttribute": "D",
                  "militaryCivilianMark": "M",
                  "salesArea": "N"
                }
              ]
            ]
          }
        ]
      }
    ]
  },
  "currentGroups": {
    "LINE-1": 2
  },
  "inventory": [
    {
      "aisleId": "1",
      "row": 1,
      "column": 1,
      "level": 2,
      "shelf": "UPPER",
      "positions": [
        {
          "skuId": "2801022-TG152",
          "quantity": 1,
          "version": "00",
          "productionAttribute": "D",
          "militaryCivilianMark": "M",
          "salesArea": "N"
        }
      ]
    },
    {
      "aisleId": "1",
      "row": 1,
      "column": 1,
      "level": 2,
      "shelf": "LOWER",
      "positions": [
        {
          "skuId": "2801038-TG152",
          "quantity": 1,
          "version": "00",
          "productionAttribute": "D",
          "militaryCivilianMark": "M",
          "salesArea": "N"
        }
      ]
    },
    {
      "aisleId": "1",
      "row": 1,
      "column": 2,
      "level": 2,
      "shelf": "UPPER",
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
    },
    {
      "aisleId": "1",
      "row": 1,
      "column": 2,
      "level": 2,
      "shelf": "LOWER",
      "positions": [
        {
          "skuId": "2801037-TG152",
          "quantity": 1,
          "version": "00",
          "productionAttribute": "D",
          "militaryCivilianMark": "M",
          "salesArea": "N"
        }
      ]
    },
    {
      "aisleId": "1",
      "row": 1,
      "column": 3,
      "level": 2,
      "shelf": "UPPER",
      "positions": [
        {
          "skuId": "2801021-H19H0",
          "quantity": 1,
          "version": "00",
          "productionAttribute": "D",
          "militaryCivilianMark": "M",
          "salesArea": "N"
        }
      ]
    },
    {
      "aisleId": "1",
      "row": 1,
      "column": 3,
      "level": 2,
      "shelf": "LOWER",
      "positions": [
        {
          "skuId": "2801037-H19H0",
          "quantity": 1,
          "version": "00",
          "productionAttribute": "D",
          "militaryCivilianMark": "M",
          "salesArea": "N"
        }
      ]
    }
  ],
  "aisleStatus": [],
  "tasks": [
    {
      "taskId": "OUTBOUND_PL1_GP1_2801022-TG152_2801038-TG152",
      "taskType": "OUTBOUND",
      "planId": "PLAN-LINE1-THREE-GROUPS",
      "planIndex": 1,
      "skus": [
        {
          "skuId": "2801022-TG152",
          "quantity": 1,
          "version": "00",
          "productionAttribute": "D",
          "militaryCivilianMark": "M",
          "salesArea": "N"
        },
        {
          "skuId": "2801038-TG152",
          "quantity": 1,
          "version": "00",
          "productionAttribute": "D",
          "militaryCivilianMark": "M",
          "salesArea": "N"
        }
      ]
    },
    {
      "taskId": "OUTBOUND_PL1_GP2_2801021-TG152_2801037-TG152",
      "taskType": "OUTBOUND",
      "planId": "PLAN-LINE1-THREE-GROUPS",
      "planIndex": 2,
      "skus": [
        {
          "skuId": "2801021-TG152",
          "quantity": 1,
          "version": "00",
          "productionAttribute": "D",
          "militaryCivilianMark": "M",
          "salesArea": "N"
        },
        {
          "skuId": "2801037-TG152",
          "quantity": 1,
          "version": "00",
          "productionAttribute": "D",
          "militaryCivilianMark": "M",
          "salesArea": "N"
        }
      ]
    },
    {
      "taskId": "OUTBOUND_PL1_GP3_2801021-H19H0_2801037-H19H0",
      "taskType": "OUTBOUND",
      "planId": "PLAN-LINE1-THREE-GROUPS",
      "planIndex": 3,
      "skus": [
        {
          "skuId": "2801021-H19H0",
          "quantity": 1,
          "version": "00",
          "productionAttribute": "D",
          "militaryCivilianMark": "M",
          "salesArea": "N"
        },
        {
          "skuId": "2801037-H19H0",
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
```

预期：

- 调出第 2 组任务 `OUTBOUND_PL1_GP2_2801021-TG152_2801037-TG152`
- 不调出第 1 组 `OUTBOUND_PL1_GP1_2801022-TG152_2801038-TG152`

真实返回摘要：

- `HTTP 200 | SUCCESS | 调度成功`

当前环境实际返回：

```json
{
  "httpStatus": 200,
  "body": {
    "status": "SUCCESS",
    "message": "调度成功",
    "data": {
      "scheduleId": "SCH-B49C9A80",
      "timestamp": "2026-06-05T06:55:03Z",
      "aisleAssignments": [
        {
          "aisleId": "1",
          "assignedTask": {
            "taskId": "OUTBOUND_PL1_GP2_2801021-TG152_2801037-TG152",
            "taskType": "OUTBOUND",
            "planId": "PLAN-LINE1-THREE-GROUPS",
            "planIndex": 2,
            "positions": [
              {
                "row": 1,
                "column": 2,
                "level": 2,
                "shelf": "UPPER",
                "skuId": "2801021-TG152",
                "quantity": 1,
                "version": "00",
                "productionAttribute": "D",
                "militaryCivilianMark": "M",
                "salesArea": "N"
              },
              {
                "row": 1,
                "column": 2,
                "level": 2,
                "shelf": "LOWER",
                "skuId": "2801037-TG152",
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
              "taskId": "OUTBOUND_PL1_GP2_2801021-TG152_2801037-TG152",
              "taskType": "OUTBOUND",
              "planId": "PLAN-LINE1-THREE-GROUPS",
              "planIndex": 2,
              "positions": [
                {
                  "row": 1,
                  "column": 2,
                  "level": 2,
                  "shelf": "UPPER",
                  "skuId": "2801021-TG152",
                  "quantity": 1,
                  "version": "00",
                  "productionAttribute": "D",
                  "militaryCivilianMark": "M",
                  "salesArea": "N"
                },
                {
                  "row": 1,
                  "column": 2,
                  "level": 2,
                  "shelf": "LOWER",
                  "skuId": "2801037-TG152",
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
        {
          "aisleId": "2",
          "assignedTask": null,
          "matchedTasks": null
        },
        {
          "aisleId": "3",
          "assignedTask": null,
          "matchedTasks": null
        },
        {
          "aisleId": "4",
          "assignedTask": null,
          "matchedTasks": null
        },
        {
          "aisleId": "5",
          "assignedTask": null,
          "matchedTasks": null
        }
      ],
      "executableTasksByAisle": {
        "1": [
          {
            "taskId": "OUTBOUND_PL1_GP2_2801021-TG152_2801037-TG152",
            "taskType": "OUTBOUND",
            "planId": "PLAN-LINE1-THREE-GROUPS",
            "planIndex": 2,
            "positions": [
              {
                "row": 1,
                "column": 2,
                "level": 2,
                "shelf": "UPPER",
                "skuId": "2801021-TG152",
                "quantity": 1,
                "version": "00",
                "productionAttribute": "D",
                "militaryCivilianMark": "M",
                "salesArea": "N"
              },
              {
                "row": 1,
                "column": 2,
                "level": 2,
                "shelf": "LOWER",
                "skuId": "2801037-TG152",
                "quantity": 1,
                "version": "00",
                "productionAttribute": "D",
                "militaryCivilianMark": "M",
                "salesArea": "N"
              }
            ]
          }
        ],
        "2": [],
        "3": [],
        "4": [],
        "5": []
      }
    }
  }
}
```

---

#### 5.1.1 补充：currentGroups = 2 时 past/current/future 分桶验证

目的：

- 验证 `currentGroups = 2` 时，第 1 组出库任务会被识别为 `past` 并直接忽略
- 验证第 2 组任务进入当前可执行集合
- 验证第 3 组任务不会进入 mixed 的 `matchedTasks`，但会保留在 `/api/v1/task/unconfirmed` 的 `pending`

请求体示意：

```json
{
  "currentTime": "2026-05-24 10:15:00",
  "productionPlan": {
    "operationType": "UPDATE",
    "planDate": "2026-05-24 09:00:00",
    "plans": [
      {
        "planId": "PLAN-L1-THREE",
        "lineId": "LINE-1",
        "planIndex": [
          {
            "requiredSkus": [
              [
                {
                  "skuId": "2801022-TG152",
                  "quantity": 1,
                  "version": "00",
                  "productionAttribute": "D",
                  "militaryCivilianMark": "M",
                  "salesArea": "N"
                },
                {
                  "skuId": "2801038-TG152",
                  "quantity": 1,
                  "version": "00",
                  "productionAttribute": "D",
                  "militaryCivilianMark": "M",
                  "salesArea": "N"
                }
              ]
            ]
          },
          {
            "requiredSkus": [
              [
                {
                  "skuId": "2801021-TG152",
                  "quantity": 1,
                  "version": "00",
                  "productionAttribute": "D",
                  "militaryCivilianMark": "M",
                  "salesArea": "N"
                },
                {
                  "skuId": "2801037-TG152",
                  "quantity": 1,
                  "version": "00",
                  "productionAttribute": "D",
                  "militaryCivilianMark": "M",
                  "salesArea": "N"
                }
              ]
            ]
          },
          {
            "requiredSkus": [
              [
                {
                  "skuId": "2801021-H19H0",
                  "quantity": 1,
                  "version": "00",
                  "productionAttribute": "D",
                  "militaryCivilianMark": "M",
                  "salesArea": "N"
                },
                {
                  "skuId": "2801037-H19H0",
                  "quantity": 1,
                  "version": "00",
                  "productionAttribute": "D",
                  "militaryCivilianMark": "M",
                  "salesArea": "N"
                }
              ]
            ]
          }
        ]
      }
    ]
  },
  "currentGroups": {
    "LINE-1": 2
  },
  "inventory": [
    {
      "aisleId": "1",
      "row": 1,
      "column": 1,
      "level": 2,
      "shelf": "UPPER",
      "positions": [
        {
          "skuId": "2801022-TG152",
          "quantity": 1,
          "version": "00",
          "productionAttribute": "D",
          "militaryCivilianMark": "M",
          "salesArea": "N"
        }
      ]
    },
    {
      "aisleId": "1",
      "row": 1,
      "column": 1,
      "level": 2,
      "shelf": "LOWER",
      "positions": [
        {
          "skuId": "2801038-TG152",
          "quantity": 1,
          "version": "00",
          "productionAttribute": "D",
          "militaryCivilianMark": "M",
          "salesArea": "N"
        }
      ]
    },
    {
      "aisleId": "1",
      "row": 1,
      "column": 2,
      "level": 2,
      "shelf": "UPPER",
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
    },
    {
      "aisleId": "1",
      "row": 1,
      "column": 2,
      "level": 2,
      "shelf": "LOWER",
      "positions": [
        {
          "skuId": "2801037-TG152",
          "quantity": 1,
          "version": "00",
          "productionAttribute": "D",
          "militaryCivilianMark": "M",
          "salesArea": "N"
        }
      ]
    },
    {
      "aisleId": "1",
      "row": 1,
      "column": 3,
      "level": 2,
      "shelf": "UPPER",
      "positions": [
        {
          "skuId": "2801021-H19H0",
          "quantity": 1,
          "version": "00",
          "productionAttribute": "D",
          "militaryCivilianMark": "M",
          "salesArea": "N"
        }
      ]
    },
    {
      "aisleId": "1",
      "row": 1,
      "column": 3,
      "level": 2,
      "shelf": "LOWER",
      "positions": [
        {
          "skuId": "2801037-H19H0",
          "quantity": 1,
          "version": "00",
          "productionAttribute": "D",
          "militaryCivilianMark": "M",
          "salesArea": "N"
        }
      ]
    }
  ],
  "aisleStatus": [],
  "tasks": [
    {
      "taskId": "OUT-G1",
      "taskType": "OUTBOUND",
      "planId": "PLAN-L1-THREE",
      "planIndex": 1,
      "skus": [
        {
          "skuId": "2801022-TG152",
          "quantity": 1,
          "version": "00",
          "productionAttribute": "D",
          "militaryCivilianMark": "M",
          "salesArea": "N"
        },
        {
          "skuId": "2801038-TG152",
          "quantity": 1,
          "version": "00",
          "productionAttribute": "D",
          "militaryCivilianMark": "M",
          "salesArea": "N"
        }
      ]
    },
    {
      "taskId": "OUT-G2",
      "taskType": "OUTBOUND",
      "planId": "PLAN-L1-THREE",
      "planIndex": 2,
      "skus": [
        {
          "skuId": "2801021-TG152",
          "quantity": 1,
          "version": "00",
          "productionAttribute": "D",
          "militaryCivilianMark": "M",
          "salesArea": "N"
        },
        {
          "skuId": "2801037-TG152",
          "quantity": 1,
          "version": "00",
          "productionAttribute": "D",
          "militaryCivilianMark": "M",
          "salesArea": "N"
        }
      ]
    },
    {
      "taskId": "OUT-G3",
      "taskType": "OUTBOUND",
      "planId": "PLAN-L1-THREE",
      "planIndex": 3,
      "skus": [
        {
          "skuId": "2801021-H19H0",
          "quantity": 1,
          "version": "00",
          "productionAttribute": "D",
          "militaryCivilianMark": "M",
          "salesArea": "N"
        },
        {
          "skuId": "2801037-H19H0",
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
```

预期：

- mixed 只下发第 2 组 `OUT-G2`
- 第 1 组 `OUT-G1` 作为 `past` 任务被直接忽略，不出现在 `matchedTasks`、`executableTasksByAisle` 和 `/task/unconfirmed`
- 第 3 组 `OUT-G3` 作为 `future` 任务不进入 mixed 的可执行结果，但会保留在 `/api/v1/task/unconfirmed` 的 `pending`

当前环境实际 mixed 返回：

```json
{
  "status": "SUCCESS",
  "message": "调度成功",
  "data": {
    "scheduleId": "SCH-50DAE4E2",
    "timestamp": "2026-06-14T04:07:20Z",
    "aisleAssignments": [
      {
        "aisleId": "1",
        "assignedTask": {
          "taskId": "OUT-G2",
          "taskType": "OUTBOUND",
          "planId": "PLAN-L1-THREE",
          "planIndex": 2,
          "positions": [
            {
              "row": 1,
              "column": 2,
              "level": 2,
              "shelf": "UPPER",
              "skuId": "2801021-TG152",
              "quantity": 1,
              "version": "00",
              "productionAttribute": "D",
              "militaryCivilianMark": "M",
              "salesArea": "N"
            },
            {
              "row": 1,
              "column": 2,
              "level": 2,
              "shelf": "LOWER",
              "skuId": "2801037-TG152",
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
            "taskId": "OUT-G2",
            "taskType": "OUTBOUND",
            "planId": "PLAN-L1-THREE",
            "planIndex": 2,
            "positions": [
              {
                "row": 1,
                "column": 2,
                "level": 2,
                "shelf": "UPPER",
                "skuId": "2801021-TG152",
                "quantity": 1,
                "version": "00",
                "productionAttribute": "D",
                "militaryCivilianMark": "M",
                "salesArea": "N"
              },
              {
                "row": 1,
                "column": 2,
                "level": 2,
                "shelf": "LOWER",
                "skuId": "2801037-TG152",
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
      {
        "aisleId": "2",
        "assignedTask": null,
        "matchedTasks": null
      },
      {
        "aisleId": "3",
        "assignedTask": null,
        "matchedTasks": null
      },
      {
        "aisleId": "4",
        "assignedTask": null,
        "matchedTasks": null
      },
      {
        "aisleId": "5",
        "assignedTask": null,
        "matchedTasks": null
      }
    ],
    "executableTasksByAisle": {
      "1": [
        {
          "taskId": "OUT-G2",
          "taskType": "OUTBOUND",
          "planId": "PLAN-L1-THREE",
          "planIndex": 2,
          "positions": [
            {
              "row": 1,
              "column": 2,
              "level": 2,
              "shelf": "UPPER",
              "skuId": "2801021-TG152",
              "quantity": 1,
              "version": "00",
              "productionAttribute": "D",
              "militaryCivilianMark": "M",
              "salesArea": "N"
            },
            {
              "row": 1,
              "column": 2,
              "level": 2,
              "shelf": "LOWER",
              "skuId": "2801037-TG152",
              "quantity": 1,
              "version": "00",
              "productionAttribute": "D",
              "militaryCivilianMark": "M",
              "salesArea": "N"
            }
          ]
        }
      ],
      "2": [],
      "3": [],
      "4": [],
      "5": []
    },
    "unsubmittedOutboundTasks": null
  }
}
```

随后查询 `/api/v1/task/unconfirmed`，当前环境实际返回：

```json
{
  "status": "SUCCESS",
  "message": "获取可执行任务成功",
  "data": {
    "tasksByAisle": {
      "1": {
        "can_executing": [
          {
            "taskId": "OUT-G2",
            "taskType": "OUTBOUND",
            "aisleId": "1",
            "positions": [
              {
                "row": 1,
                "column": 2,
                "level": 2,
                "shelf": "UPPER",
                "skuId": "2801021-TG152",
                "quantity": 1
              },
              {
                "row": 1,
                "column": 2,
                "level": 2,
                "shelf": "LOWER",
                "skuId": "2801037-TG152",
                "quantity": 1
              }
            ]
          }
        ],
        "pending": [
          {
            "taskId": "OUT-G3",
            "taskType": "OUTBOUND",
            "aisleId": "1",
            "positions": [
              {
                "row": 1,
                "column": 3,
                "level": 2,
                "shelf": "UPPER",
                "skuId": "2801021-H19H0",
                "quantity": 1
              },
              {
                "row": 1,
                "column": 3,
                "level": 2,
                "shelf": "LOWER",
                "skuId": "2801037-H19H0",
                "quantity": 1
              }
            ]
          }
        ],
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
      },
      "5": {
        "can_executing": [],
        "pending": [],
        "running": []
      }
    },
    "canAcceptExecuting": true
  }
}
```

2026-06-14 回归结论：

- 通过
- 第 1 组 `past` 任务不会再混入 mixed 的 `matchedTasks` 和 `executableTasksByAisle`
- 第 3 组 `future` 任务不会提前变成可执行任务，但仍会在 `/task/unconfirmed` 中以 `pending` 形式保留

---

### 5.2 currentGroups = 3，直接调第 3 组

在 `5.1` 基础上仅修改：

```json
"currentGroups": {
  "LINE-1": 3
}
```

预期：

- 调出第 3 组任务 `OUTBOUND_PL1_GP3_2801021-H19H0_2801037-H19H0`

说明：这里的 matchedTasks 现在表示“当前可 EXECUTING 的任务列表”（已过滤不可执行任务）；是否可执行仍受当前组约束和队列队首约束。
真实返回摘要：

- `HTTP 200 | SUCCESS | 调度成功`

当前环境实际返回：

```json
{
  "httpStatus": 200,
  "body": {
    "status": "SUCCESS",
    "message": "调度成功",
    "data": {
      "scheduleId": "SCH-19553E92",
      "timestamp": "2026-06-05T07:31:15Z",
      "aisleAssignments": [
        {
          "aisleId": "1",
          "assignedTask": {
            "taskId": "OUTBOUND_PL1_GP3_2801021-H19H0_2801037-H19H0",
            "taskType": "OUTBOUND",
            "planId": "PLAN-LINE1-THREE-GROUPS",
            "planIndex": 3,
            "positions": [
              {
                "row": 1,
                "column": 3,
                "level": 2,
                "shelf": "UPPER",
                "skuId": "2801021-H19H0",
                "quantity": 1,
                "version": "00",
                "productionAttribute": "D",
                "militaryCivilianMark": "M",
                "salesArea": "N"
              },
              {
                "row": 1,
                "column": 3,
                "level": 2,
                "shelf": "LOWER",
                "skuId": "2801037-H19H0",
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
              "taskId": "OUTBOUND_PL1_GP3_2801021-H19H0_2801037-H19H0",
              "taskType": "OUTBOUND",
              "planId": "PLAN-LINE1-THREE-GROUPS",
              "planIndex": 3,
              "positions": [
                {
                  "row": 1,
                  "column": 3,
                  "level": 2,
                  "shelf": "UPPER",
                  "skuId": "2801021-H19H0",
                  "quantity": 1,
                  "version": "00",
                  "productionAttribute": "D",
                  "militaryCivilianMark": "M",
                  "salesArea": "N"
                },
                {
                  "row": 1,
                  "column": 3,
                  "level": 2,
                  "shelf": "LOWER",
                  "skuId": "2801037-H19H0",
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
        {
          "aisleId": "2",
          "assignedTask": null,
          "matchedTasks": null
        },
        {
          "aisleId": "3",
          "assignedTask": null,
          "matchedTasks": null
        },
        {
          "aisleId": "4",
          "assignedTask": null,
          "matchedTasks": null
        },
        {
          "aisleId": "5",
          "assignedTask": null,
          "matchedTasks": null
        }
      ],
      "executableTasksByAisle": {
        "1": [
          {
            "taskId": "OUTBOUND_PL1_GP3_2801021-H19H0_2801037-H19H0",
            "taskType": "OUTBOUND",
            "planId": "PLAN-LINE1-THREE-GROUPS",
            "planIndex": 3,
            "positions": [
              {
                "row": 1,
                "column": 3,
                "level": 2,
                "shelf": "UPPER",
                "skuId": "2801021-H19H0",
                "quantity": 1,
                "version": "00",
                "productionAttribute": "D",
                "militaryCivilianMark": "M",
                "salesArea": "N"
              },
              {
                "row": 1,
                "column": 3,
                "level": 2,
                "shelf": "LOWER",
                "skuId": "2801037-H19H0",
                "quantity": 1,
                "version": "00",
                "productionAttribute": "D",
                "militaryCivilianMark": "M",
                "salesArea": "N"
              }
            ]
          }
        ],
        "2": [],
        "3": [],
        "4": [],
        "5": []
      }
    }
  }
}
```

### 5.3 currentGroups = 0 自动补正为 1

在 `5.1` 基础上仅修改：

```json
"currentGroups": {
  "LINE-1": 0
}
```

预期：

- 按第 1 组调度
- `data.normalizationNotices` 包含：
  - `产线 1 的 currentGroups=0 已自动按第 1 组处理`

真实返回摘要：

- `HTTP 200 | SUCCESS | 调度成功`

当前环境实际返回：

```json
{
  "httpStatus": 200,
  "body": {
    "status": "SUCCESS",
    "message": "调度成功",
    "data": {
      "scheduleId": "SCH-882B56B8",
      "timestamp": "2026-06-05T07:31:15Z",
      "aisleAssignments": [
        {
          "aisleId": "1",
          "assignedTask": {
            "taskId": "OUTBOUND_PL1_GP1_2801022-TG152_2801038-TG152",
            "taskType": "OUTBOUND",
            "planId": "PLAN-LINE1-THREE-GROUPS",
            "planIndex": 1,
            "positions": [
              {
                "row": 1,
                "column": 1,
                "level": 2,
                "shelf": "UPPER",
                "skuId": "2801022-TG152",
                "quantity": 1,
                "version": "00",
                "productionAttribute": "D",
                "militaryCivilianMark": "M",
                "salesArea": "N"
              },
              {
                "row": 1,
                "column": 1,
                "level": 2,
                "shelf": "LOWER",
                "skuId": "2801038-TG152",
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
              "taskId": "OUTBOUND_PL1_GP1_2801022-TG152_2801038-TG152",
              "taskType": "OUTBOUND",
              "planId": "PLAN-LINE1-THREE-GROUPS",
              "planIndex": 1,
              "positions": [
                {
                  "row": 1,
                  "column": 1,
                  "level": 2,
                  "shelf": "UPPER",
                  "skuId": "2801022-TG152",
                  "quantity": 1,
                  "version": "00",
                  "productionAttribute": "D",
                  "militaryCivilianMark": "M",
                  "salesArea": "N"
                },
                {
                  "row": 1,
                  "column": 1,
                  "level": 2,
                  "shelf": "LOWER",
                  "skuId": "2801038-TG152",
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
        {
          "aisleId": "2",
          "assignedTask": null,
          "matchedTasks": null
        },
        {
          "aisleId": "3",
          "assignedTask": null,
          "matchedTasks": null
        },
        {
          "aisleId": "4",
          "assignedTask": null,
          "matchedTasks": null
        },
        {
          "aisleId": "5",
          "assignedTask": null,
          "matchedTasks": null
        }
      ],
      "executableTasksByAisle": {
        "1": [
          {
            "taskId": "OUTBOUND_PL1_GP1_2801022-TG152_2801038-TG152",
            "taskType": "OUTBOUND",
            "planId": "PLAN-LINE1-THREE-GROUPS",
            "planIndex": 1,
            "positions": [
              {
                "row": 1,
                "column": 1,
                "level": 2,
                "shelf": "UPPER",
                "skuId": "2801022-TG152",
                "quantity": 1,
                "version": "00",
                "productionAttribute": "D",
                "militaryCivilianMark": "M",
                "salesArea": "N"
              },
              {
                "row": 1,
                "column": 1,
                "level": 2,
                "shelf": "LOWER",
                "skuId": "2801038-TG152",
                "quantity": 1,
                "version": "00",
                "productionAttribute": "D",
                "militaryCivilianMark": "M",
                "salesArea": "N"
              }
            ]
          }
        ],
        "2": [],
        "3": [],
        "4": [],
        "5": []
      },
      "normalizationNotices": [
        "产线 1 的 currentGroups=0 已自动按第 1 组处理"
      ]
    }
  }
}
```

---

### 5.4 currentGroups 越界报错

将第 1 组任务标记为完成，传：

```json
"currentGroups": {
  "LINE-1": 5
}
```

预期：

- HTTP `400`
- `status = FAILED`
- `message` 类似：
  - `产线 1 的 currentGroups=5 超出计划组上限，当前计划仅有 3 组`
- 本次请求如果同时包含ADD/UPDATE 计划和任务(会依据ADD/UPDATE之后的计划进行匹配)，则都不会生效。

实际响应：

```json
{
    "status": "FAILED",
    "message": "产线 1 的 currentGroups=5 超出计划组上限，当前计划仅有 3 组",
    "data": {
        "timestamp": "2026-05-24T12:36:21Z"
    }
}
```
---

#### 5.4.1 补充：mixed 出库库存不足时返回未提交任务

目的：

- 验证 mixed 在处理出库任务时，库存不足的任务不会进入已提交队列
- 验证同一产线同一组内只要有一条出库任务库存不足，则整组都不提交
- 验证该产线后续组也会被拦截，并额外返回未提交任务列表

请求体：

```json
{
  "currentTime": "2026-06-13 10:00:00",
  "productionPlan": {
    "operationType": "UPDATE",
    "planDate": "2026-06-13 10:00:00",
    "plans": [
      {
        "planId": "PLAN-LINE1-BLOCK",
        "lineId": "LINE-1",
        "planIndex": [
          {
            "requiredSkus": [
              [
                {
                  "skuId": "2801021-TR200",
                  "quantity": 1,
                  "version": "00",
                  "productionAttribute": "D",
                  "militaryCivilianMark": "M",
                  "salesArea": "N"
                },
                {
                  "skuId": "2801037-TR200",
                  "quantity": 1,
                  "version": "00",
                  "productionAttribute": "D",
                  "militaryCivilianMark": "M",
                  "salesArea": "N"
                }
              ],
              [
                {
                  "skuId": "2801022-TR200",
                  "quantity": 1,
                  "version": "00",
                  "productionAttribute": "D",
                  "militaryCivilianMark": "M",
                  "salesArea": "N"
                },
                {
                  "skuId": "2801038-TR200",
                  "quantity": 1,
                  "version": "00",
                  "productionAttribute": "D",
                  "militaryCivilianMark": "M",
                  "salesArea": "N"
                }
              ]
            ]
          },
          {
            "requiredSkus": [
              [
                {
                  "skuId": "2801022-TR9A0",
                  "quantity": 1,
                  "version": "00",
                  "productionAttribute": "D",
                  "militaryCivilianMark": "M",
                  "salesArea": "N"
                },
                {
                  "skuId": "2801038-TR9A0",
                  "quantity": 1,
                  "version": "00",
                  "productionAttribute": "D",
                  "militaryCivilianMark": "M",
                  "salesArea": "N"
                }
              ]
            ]
          }
        ]
      },
      {
        "planId": "PLAN-LINE2-OK",
        "lineId": "LINE-2",
        "planIndex": [
          {
            "requiredSkus": [
              [
                {
                  "skuId": "2801021-H19H0",
                  "quantity": 1,
                  "version": "00",
                  "productionAttribute": "D",
                  "militaryCivilianMark": "M",
                  "salesArea": "N"
                },
                {
                  "skuId": "2801037-H19H0",
                  "quantity": 1,
                  "version": "00",
                  "productionAttribute": "D",
                  "militaryCivilianMark": "M",
                  "salesArea": "N"
                }
              ]
            ]
          }
        ]
      }
    ]
  },
  "currentGroups": {
    "LINE-1": 1,
    "LINE-2": 1
  },
  "inventory": [
    {
      "aisleId": "1",
      "row": 1,
      "column": 1,
      "level": 2,
      "shelf": "UPPER",
      "positions": [
        {
          "skuId": "2801021-H19H0",
          "quantity": 1,
          "version": "00",
          "productionAttribute": "D",
          "militaryCivilianMark": "M",
          "salesArea": "N"
        }
      ]
    },
    {
      "aisleId": "1",
      "row": 1,
      "column": 1,
      "level": 2,
      "shelf": "LOWER",
      "positions": [
        {
          "skuId": "2801037-H19H0",
          "quantity": 1,
          "version": "00",
          "productionAttribute": "D",
          "militaryCivilianMark": "M",
          "salesArea": "N"
        }
      ]
    }
  ],
  "aisleStatus": [],
  "tasks": [
    {
      "taskId": "OUT-L1-G1-A",
      "taskType": "OUTBOUND",
      "planId": "PLAN-LINE1-BLOCK",
      "planIndex": 1,
      "skus": [
        {
          "skuId": "2801021-TR200",
          "quantity": 1,
          "version": "00",
          "productionAttribute": "D",
          "militaryCivilianMark": "M",
          "salesArea": "N"
        },
        {
          "skuId": "2801037-TR200",
          "quantity": 1,
          "version": "00",
          "productionAttribute": "D",
          "militaryCivilianMark": "M",
          "salesArea": "N"
        }
      ]
    },
    {
      "taskId": "OUT-L1-G1-B",
      "taskType": "OUTBOUND",
      "planId": "PLAN-LINE1-BLOCK",
      "planIndex": 1,
      "skus": [
        {
          "skuId": "2801022-TR200",
          "quantity": 1,
          "version": "00",
          "productionAttribute": "D",
          "militaryCivilianMark": "M",
          "salesArea": "N"
        },
        {
          "skuId": "2801038-TR200",
          "quantity": 1,
          "version": "00",
          "productionAttribute": "D",
          "militaryCivilianMark": "M",
          "salesArea": "N"
        }
      ]
    },
    {
      "taskId": "OUT-L1-G2",
      "taskType": "OUTBOUND",
      "planId": "PLAN-LINE1-BLOCK",
      "planIndex": 2,
      "skus": [
        {
          "skuId": "2801022-TR9A0",
          "quantity": 1,
          "version": "00",
          "productionAttribute": "D",
          "militaryCivilianMark": "M",
          "salesArea": "N"
        },
        {
          "skuId": "2801038-TR9A0",
          "quantity": 1,
          "version": "00",
          "productionAttribute": "D",
          "militaryCivilianMark": "M",
          "salesArea": "N"
        }
      ]
    },
    {
      "taskId": "OUT-L2-G1",
      "taskType": "OUTBOUND",
      "planId": "PLAN-LINE2-OK",
      "planIndex": 1,
      "skus": [
        {
          "skuId": "2801021-H19H0",
          "quantity": 1,
          "version": "00",
          "productionAttribute": "D",
          "militaryCivilianMark": "M",
          "salesArea": "N"
        },
        {
          "skuId": "2801037-H19H0",
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
```

预期：

- `LINE-1` 第 1 组中只要有一条任务库存不足，则该组两条任务都不提交
- `LINE-1` 第 2 组也会被拦截，不进入已提交队列
- `LINE-2` 有库存的任务正常进入调度
- mixed 响应新增 `unsubmittedOutboundTasks`

真实返回摘要：

- `HTTP 200 | SUCCESS | 调度成功`

当前环境实际返回：

```json
{
  "httpStatus": 200,
  "body": {
    "status": "SUCCESS",
    "message": "调度成功",
    "data": {
      "scheduleId": "SCH-33ECA14B",
      "timestamp": "2026-06-13T02:50:13Z",
      "aisleAssignments": [
        {
          "aisleId": "1",
          "assignedTask": {
            "taskId": "OUT-L2-G1",
            "taskType": "OUTBOUND",
            "planId": "PLAN-LINE2-OK",
            "planIndex": 1,
            "positions": [
              {
                "row": 1,
                "column": 1,
                "level": 2,
                "shelf": "UPPER",
                "skuId": "2801021-H19H0",
                "quantity": 1,
                "version": "00",
                "productionAttribute": "D",
                "militaryCivilianMark": "M",
                "salesArea": "N"
              },
              {
                "row": 1,
                "column": 1,
                "level": 2,
                "shelf": "LOWER",
                "skuId": "2801037-H19H0",
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
              "taskId": "OUT-L2-G1",
              "taskType": "OUTBOUND",
              "planId": "PLAN-LINE2-OK",
              "planIndex": 1,
              "positions": [
                {
                  "row": 1,
                  "column": 1,
                  "level": 2,
                  "shelf": "UPPER",
                  "skuId": "2801021-H19H0",
                  "quantity": 1,
                  "version": "00",
                  "productionAttribute": "D",
                  "militaryCivilianMark": "M",
                  "salesArea": "N"
                },
                {
                  "row": 1,
                  "column": 1,
                  "level": 2,
                  "shelf": "LOWER",
                  "skuId": "2801037-H19H0",
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
      "unsubmittedOutboundTasks": [
        {
          "taskId": "OUT-L1-G1-A",
          "taskType": "OUTBOUND",
          "planId": "PLAN-LINE1-BLOCK",
          "planIndex": 1,
          "productionLine": 1,
          "reason": "库存不足"
        },
        {
          "taskId": "OUT-L1-G1-B",
          "taskType": "OUTBOUND",
          "planId": "PLAN-LINE1-BLOCK",
          "planIndex": 1,
          "productionLine": 1,
          "reason": "同组出库任务库存不足",
          "blockedByTaskId": "OUT-L1-G1-A"
        },
        {
          "taskId": "OUT-L1-G2",
          "taskType": "OUTBOUND",
          "planId": "PLAN-LINE1-BLOCK",
          "planIndex": 2,
          "productionLine": 1,
          "reason": "前序出库任务库存不足",
          "blockedByTaskId": "OUT-L1-G1-A"
        }
      ]
    }
  }
}
```

2026-06-14 回归结论：

- 通过
- 当前 mixed 已支持返回 `unsubmittedOutboundTasks`
- 同产线同组内一条库存不足，会导致整组都不提交
- 同产线后续组也不会进入已提交队列
- 其他产线的正常出库任务不受影响

### 5.5 ADD 按组追加，保持当前组指针
补测说明：

- 上述旧响应保留用于说明原始拦截效果
- 2026-06-14 重新回归后，当前代码在 `unsubmittedOutboundTasks` 中已额外返回每条未提交出库任务的 `skus` 明细，便于外部直接定位缺料任务

2026-06-14 补测响应（当前代码实际返回）：

```json
{
  "status": "SUCCESS",
  "message": "调度成功",
  "data": {
    "scheduleId": "SCH-BB9C7623",
    "timestamp": "2026-06-14T04:21:06Z",
    "aisleAssignments": [
      {
        "aisleId": "1",
        "assignedTask": {
          "taskId": "OUT-L2-G1",
          "taskType": "OUTBOUND",
          "planId": "PLAN-LINE2-OK",
          "planIndex": 1,
          "positions": [
            {
              "row": 1,
              "column": 1,
              "level": 2,
              "shelf": "UPPER",
              "skuId": "2801021-H19H0",
              "quantity": 1,
              "version": "00",
              "productionAttribute": "D",
              "militaryCivilianMark": "M",
              "salesArea": "N"
            },
            {
              "row": 1,
              "column": 1,
              "level": 2,
              "shelf": "LOWER",
              "skuId": "2801037-H19H0",
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
            "taskId": "OUT-L2-G1",
            "taskType": "OUTBOUND",
            "planId": "PLAN-LINE2-OK",
            "planIndex": 1,
            "positions": [
              {
                "row": 1,
                "column": 1,
                "level": 2,
                "shelf": "UPPER",
                "skuId": "2801021-H19H0",
                "quantity": 1,
                "version": "00",
                "productionAttribute": "D",
                "militaryCivilianMark": "M",
                "salesArea": "N"
              },
              {
                "row": 1,
                "column": 1,
                "level": 2,
                "shelf": "LOWER",
                "skuId": "2801037-H19H0",
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
      {
        "aisleId": "2",
        "assignedTask": null,
        "matchedTasks": null
      },
      {
        "aisleId": "3",
        "assignedTask": null,
        "matchedTasks": null
      },
      {
        "aisleId": "4",
        "assignedTask": null,
        "matchedTasks": null
      },
      {
        "aisleId": "5",
        "assignedTask": null,
        "matchedTasks": null
      }
    ],
    "executableTasksByAisle": {
      "1": [
        {
          "taskId": "OUT-L2-G1",
          "taskType": "OUTBOUND",
          "planId": "PLAN-LINE2-OK",
          "planIndex": 1,
          "positions": [
            {
              "row": 1,
              "column": 1,
              "level": 2,
              "shelf": "UPPER",
              "skuId": "2801021-H19H0",
              "quantity": 1,
              "version": "00",
              "productionAttribute": "D",
              "militaryCivilianMark": "M",
              "salesArea": "N"
            },
            {
              "row": 1,
              "column": 1,
              "level": 2,
              "shelf": "LOWER",
              "skuId": "2801037-H19H0",
              "quantity": 1,
              "version": "00",
              "productionAttribute": "D",
              "militaryCivilianMark": "M",
              "salesArea": "N"
            }
          ]
        }
      ],
      "2": [],
      "3": [],
      "4": [],
      "5": []
    },
    "unsubmittedOutboundTasks": [
      {
        "taskId": "OUT-L1-G1-A",
        "taskType": "OUTBOUND",
        "planId": "PLAN-LINE1-BLOCK",
        "planIndex": 1,
        "productionLine": 1,
        "skus": [
          {
            "skuId": "2801021-TR200",
            "quantity": 1,
            "beamSide": null,
            "inLine": null,
            "version": "00",
            "productionAttribute": "D",
            "militaryCivilianMark": "M",
            "salesArea": "N"
          },
          {
            "skuId": "2801037-TR200",
            "quantity": 1,
            "beamSide": null,
            "inLine": null,
            "version": "00",
            "productionAttribute": "D",
            "militaryCivilianMark": "M",
            "salesArea": "N"
          }
        ],
        "reason": "库存不足"
      },
      {
        "taskId": "OUT-L1-G1-B",
        "taskType": "OUTBOUND",
        "planId": "PLAN-LINE1-BLOCK",
        "planIndex": 1,
        "productionLine": 1,
        "skus": [
          {
            "skuId": "2801022-TR200",
            "quantity": 1,
            "beamSide": null,
            "inLine": null,
            "version": "00",
            "productionAttribute": "D",
            "militaryCivilianMark": "M",
            "salesArea": "N"
          },
          {
            "skuId": "2801038-TR200",
            "quantity": 1,
            "beamSide": null,
            "inLine": null,
            "version": "00",
            "productionAttribute": "D",
            "militaryCivilianMark": "M",
            "salesArea": "N"
          }
        ],
        "reason": "同组出库任务库存不足",
        "blockedByTaskId": "OUT-L1-G1-A"
      },
      {
        "taskId": "OUT-L1-G2",
        "taskType": "OUTBOUND",
        "planId": "PLAN-LINE1-BLOCK",
        "planIndex": 2,
        "productionLine": 1,
        "skus": [
          {
            "skuId": "2801022-TR9A0",
            "quantity": 1,
            "beamSide": null,
            "inLine": null,
            "version": "00",
            "productionAttribute": "D",
            "militaryCivilianMark": "M",
            "salesArea": "N"
          },
          {
            "skuId": "2801038-TR9A0",
            "quantity": 1,
            "beamSide": null,
            "inLine": null,
            "version": "00",
            "productionAttribute": "D",
            "militaryCivilianMark": "M",
            "salesArea": "N"
          }
        ],
        "reason": "前序出库任务库存不足",
        "blockedByTaskId": "OUT-L1-G1-A"
      }
    ]
  }
}
```

---

### 5.5 ADD 按组追加，保持当前组指针

因为现在已经推进到第2组任务，所以ADD后应该调度出第2组任务。

请求体：

```json
{
  "currentTime": "2026-05-24 10:20:00",
  "productionPlan": {
    "operationType": "ADD",
    "planDate": "2026-05-24 10:20:00",
    "plans": [
      {
        "planId": "PLAN-LINE1-ADD",
        "lineId": "LINE-1",
        "planIndex": [
          {
            "requiredSkus": [
              [
                {
                  "skuId": "2801021-H19H0",
                  "quantity": 1,
                  "version": "00",
                  "productionAttribute": "D",
                  "militaryCivilianMark": "M",
                  "salesArea": "N"
                },
                {
                  "skuId": "2801037-H19H0",
                  "quantity": 1,
                  "version": "00",
                  "productionAttribute": "D",
                  "militaryCivilianMark": "M",
                  "salesArea": "N"
                }
              ]
            ]
          },
          {
            "requiredSkus": [
              [
                {
                  "skuId": "2801021-TR9A0",
                  "quantity": 1,
                  "version": "00",
                  "productionAttribute": "D",
                  "militaryCivilianMark": "M",
                  "salesArea": "N"
                },
                {
                  "skuId": "2801037-TR9A0",
                  "quantity": 1,
                  "version": "00",
                  "productionAttribute": "D",
                  "militaryCivilianMark": "M",
                  "salesArea": "N"
                }
              ]
            ]
          }
        ]
      }
    ]
  },
  "inventory": [],
  "aisleStatus": [],
  "tasks": []
}
```

预期：

- 组按 `planIndex` 追加到末尾
- 当前组指针保持不变

实际响应：

```json
{
  "status": "SUCCESS",
  "message": "调度成功",
  "data": {
    "scheduleId": "SCH-522EAE54",
    "timestamp": "2026-06-05T08:19:02Z",
    "aisleAssignments": [
      {
        "aisleId": "1",
        "assignedTask": {
          "taskId": "OUTBOUND_PL1_GP2_2801021-TG152_2801037-TG152",
          "taskType": "OUTBOUND",
          "planId": "PLAN-LINE1-THREE-GROUPS",
          "planIndex": 2,
          "positions": [
            {
              "row": 1,
              "column": 2,
              "level": 2,
              "shelf": "UPPER",
              "skuId": "2801021-TG152",
              "quantity": 1,
              "version": "00",
              "productionAttribute": "D",
              "militaryCivilianMark": "M",
              "salesArea": "N"
            },
            {
              "row": 1,
              "column": 2,
              "level": 2,
              "shelf": "LOWER",
              "skuId": "2801037-TG152",
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
            "taskId": "OUTBOUND_PL1_GP2_2801021-TG152_2801037-TG152",
            "taskType": "OUTBOUND",
            "planId": "PLAN-LINE1-THREE-GROUPS",
            "planIndex": 2,
            "positions": [
              {
                "row": 1,
                "column": 2,
                "level": 2,
                "shelf": "UPPER",
                "skuId": "2801021-TG152",
                "quantity": 1,
                "version": "00",
                "productionAttribute": "D",
                "militaryCivilianMark": "M",
                "salesArea": "N"
              },
              {
                "row": 1,
                "column": 2,
                "level": 2,
                "shelf": "LOWER",
                "skuId": "2801037-TG152",
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
      {
        "aisleId": "2",
        "assignedTask": null,
        "matchedTasks": null
      },
      {
        "aisleId": "3",
        "assignedTask": null,
        "matchedTasks": null
      },
      {
        "aisleId": "4",
        "assignedTask": null,
        "matchedTasks": null
      },
      {
        "aisleId": "5",
        "assignedTask": null,
        "matchedTasks": null
      }
    ],
    "executableTasksByAisle": {
      "1": [
        {
          "taskId": "OUTBOUND_PL1_GP2_2801021-TG152_2801037-TG152",
          "taskType": "OUTBOUND",
          "planId": "PLAN-LINE1-THREE-GROUPS",
          "planIndex": 2,
          "positions": [
            {
              "row": 1,
              "column": 2,
              "level": 2,
              "shelf": "UPPER",
              "skuId": "2801021-TG152",
              "quantity": 1,
              "version": "00",
              "productionAttribute": "D",
              "militaryCivilianMark": "M",
              "salesArea": "N"
            },
            {
              "row": 1,
              "column": 2,
              "level": 2,
              "shelf": "LOWER",
              "skuId": "2801037-TG152",
              "quantity": 1,
              "version": "00",
              "productionAttribute": "D",
              "militaryCivilianMark": "M",
              "salesArea": "N"
            }
          ]
        }
      ],
      "2": [],
      "3": [],
      "4": [],
      "5": []
    }
  }
}
```
---

### 5.6 UPDATE 替换计划并重置指针

请求体：

```json
{
  "currentTime": "2026-05-24 10:25:00",
  "productionPlan": {
    "operationType": "UPDATE",
    "planDate": "2026-05-24 10:25:00",
    "plans": [
      {
        "planId": "PLAN-LINE1-RESET",
        "lineId": "LINE-1",
        "planIndex": [
          {
            "requiredSkus": [
              [
                {
                  "skuId": "2801021-TR200",
                  "quantity": 1,
                  "version": "00",
                  "productionAttribute": "D",
                  "militaryCivilianMark": "M",
                  "salesArea": "N"
                },
                {
                  "skuId": "2801037-TR200",
                  "quantity": 1,
                  "version": "00",
                  "productionAttribute": "D",
                  "militaryCivilianMark": "M",
                  "salesArea": "N"
                }
              ]
            ]
          },
          {
            "requiredSkus": [
              [
                {
                  "skuId": "2801022-TR9A0",
                  "quantity": 1,
                  "version": "00",
                  "productionAttribute": "D",
                  "militaryCivilianMark": "M",
                  "salesArea": "N"
                },
                {
                  "skuId": "2801038-TR9A0",
                  "quantity": 1,
                  "version": "00",
                  "productionAttribute": "D",
                  "militaryCivilianMark": "M",
                  "salesArea": "N"
                }
              ]
            ]
          }
        ]
      }
    ]
  },
  "inventory": [
    {
      "aisleId": "1",
      "row": 1,
      "column": 1,
      "level": 2,
      "shelf": "UPPER",
      "positions": [
        {
          "skuId": "2801021-TR200",
          "quantity": 1,
          "version": "00",
          "productionAttribute": "D",
          "militaryCivilianMark": "M",
          "salesArea": "N"
        }
      ]
    },
    {
      "aisleId": "1",
      "row": 1,
      "column": 1,
      "level": 2,
      "shelf": "LOWER",
      "positions": [
        {
          "skuId": "2801037-TR200",
          "quantity": 1,
          "version": "00",
          "productionAttribute": "D",
          "militaryCivilianMark": "M",
          "salesArea": "N"
        }
      ]
    },
    {
      "aisleId": "1",
      "row": 1,
      "column": 2,
      "level": 2,
      "shelf": "UPPER",
      "positions": [
        {
          "skuId": "2801022-TR9A0",
          "quantity": 1,
          "version": "00",
          "productionAttribute": "D",
          "militaryCivilianMark": "M",
          "salesArea": "N"
        }
      ]
    },
    {
      "aisleId": "1",
      "row": 1,
      "column": 2,
      "level": 2,
      "shelf": "LOWER",
      "positions": [
        {
          "skuId": "2801038-TR9A0",
          "quantity": 1,
          "version": "00",
          "productionAttribute": "D",
          "militaryCivilianMark": "M",
          "salesArea": "N"
        }
      ]
    }
  ],
  "aisleStatus": [],
  "tasks": [
    {
      "taskId": "OUTBOUND_PL1_GP1_2801021-TR200_2801037-TR200",
      "taskType": "OUTBOUND",
      "planId": "PLAN-LINE1-RESET",
      "planIndex": 1,
      "skus": [
        {
          "skuId": "2801021-TR200",
          "quantity": 1,
          "version": "00",
          "productionAttribute": "D",
          "militaryCivilianMark": "M",
          "salesArea": "N"
        },
        {
          "skuId": "2801037-TR200",
          "quantity": 1,
          "version": "00",
          "productionAttribute": "D",
          "militaryCivilianMark": "M",
          "salesArea": "N"
        }
      ]
    },
    {
      "taskId": "OUTBOUND_PL1_GP2_2801022-TR9A0_2801038-TR9A0",
      "taskType": "OUTBOUND",
      "planId": "PLAN-LINE1-RESET",
      "planIndex": 2,
      "skus": [
        {
          "skuId": "2801022-TR9A0",
          "quantity": 1,
          "version": "00",
          "productionAttribute": "D",
          "militaryCivilianMark": "M",
          "salesArea": "N"
        },
        {
          "skuId": "2801038-TR9A0",
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
```

不传 `currentGroups`。

预期：

- 当前组按现有实现会重置
- 后续会从更新后的前部组重新调度
- 应优先调出第 1 组任务 `OUTBOUND_PL1_GP1_2801021-TR200_2801037-TR200`

实际响应：

```json
{
  "status": "SUCCESS",
  "message": "调度成功",
  "data": {
    "scheduleId": "SCH-1FFC9090",
    "timestamp": "2026-06-05T08:19:02Z",
    "aisleAssignments": [
      {
        "aisleId": "1",
        "assignedTask": {
          "taskId": "OUTBOUND_PL1_GP1_2801021-TR200_2801037-TR200",
          "taskType": "OUTBOUND",
          "planId": "PLAN-LINE1-RESET",
          "planIndex": 1,
          "positions": [
            {
              "row": 1,
              "column": 1,
              "level": 2,
              "shelf": "UPPER",
              "skuId": "2801021-TR200",
              "quantity": 1,
              "version": "00",
              "productionAttribute": "D",
              "militaryCivilianMark": "M",
              "salesArea": "N"
            },
            {
              "row": 1,
              "column": 1,
              "level": 2,
              "shelf": "LOWER",
              "skuId": "2801037-TR200",
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
            "taskId": "OUTBOUND_PL1_GP1_2801021-TR200_2801037-TR200",
            "taskType": "OUTBOUND",
            "planId": "PLAN-LINE1-RESET",
            "planIndex": 1,
            "positions": [
              {
                "row": 1,
                "column": 1,
                "level": 2,
                "shelf": "UPPER",
                "skuId": "2801021-TR200",
                "quantity": 1,
                "version": "00",
                "productionAttribute": "D",
                "militaryCivilianMark": "M",
                "salesArea": "N"
              },
              {
                "row": 1,
                "column": 1,
                "level": 2,
                "shelf": "LOWER",
                "skuId": "2801037-TR200",
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
      {
        "aisleId": "2",
        "assignedTask": null,
        "matchedTasks": null
      },
      {
        "aisleId": "3",
        "assignedTask": null,
        "matchedTasks": null
      },
      {
        "aisleId": "4",
        "assignedTask": null,
        "matchedTasks": null
      },
      {
        "aisleId": "5",
        "assignedTask": null,
        "matchedTasks": null
      }
    ],
    "executableTasksByAisle": {
      "1": [
        {
          "taskId": "OUTBOUND_PL1_GP1_2801021-TR200_2801037-TR200",
          "taskType": "OUTBOUND",
          "planId": "PLAN-LINE1-RESET",
          "planIndex": 1,
          "positions": [
            {
              "row": 1,
              "column": 1,
              "level": 2,
              "shelf": "UPPER",
              "skuId": "2801021-TR200",
              "quantity": 1,
              "version": "00",
              "productionAttribute": "D",
              "militaryCivilianMark": "M",
              "salesArea": "N"
            },
            {
              "row": 1,
              "column": 1,
              "level": 2,
              "shelf": "LOWER",
              "skuId": "2801037-TR200",
              "quantity": 1,
              "version": "00",
              "productionAttribute": "D",
              "militaryCivilianMark": "M",
              "salesArea": "N"
            }
          ]
        }
      ],
      "2": [],
      "3": [],
      "4": [],
      "5": []
    }
  }
}
```

#### 5.6.1 补充：UPDATE + `resetAssigned = false`

前置：

- 先通过一条 mixed 下发任务，但不要回 `EXECUTING`
- 再准备一条尚未下发的 pending 任务

请求体：

```json
{
  "currentTime": "2026-05-24 10:26:30",
  "productionPlan": {
    "operationType": "UPDATE",
    "resetAssigned": false,
    "planDate": "2026-05-24 10:26:30",
    "plans": [
      {
        "planId": "PLAN-LINE1-RESET",
        "lineId": "LINE-1",
        "planIndex": [
          {
            "requiredSkus": [
              [
                {
                  "skuId": "2801021-TR200",
                  "quantity": 1,
                  "version": "00",
                  "productionAttribute": "D",
                  "militaryCivilianMark": "M",
                  "salesArea": "N"
                },
                {
                  "skuId": "2801037-TR200",
                  "quantity": 1,
                  "version": "00",
                  "productionAttribute": "D",
                  "militaryCivilianMark": "M",
                  "salesArea": "N"
                }
              ]
            ]
          }
        ]
      }
    ]
  },
  "inventory": [],
  "aisleStatus": [],
  "tasks": []
}
```

预期：

- 未下发的 `pending` 会被清掉
- 已下发待确认执行的任务仍保留
- `running` 任务也保留

实际响应：

```json
{
  "mixedResponse": {
    "httpStatus": 200,
    "body": {
      "status": "SUCCESS",
      "message": "调度成功",
      "data": {
        "scheduleId": "SCH-2CB06991",
        "timestamp": "2026-06-05T08:19:02Z",
        "aisleAssignments": [
          {
            "aisleId": "1",
            "assignedTask": {
              "taskId": "RUN-01",
              "taskType": "OUTBOUND",
              "planId": "PLAN-OLD",
              "planIndex": 1,
              "positions": [
                {
                  "row": 1,
                  "column": 1,
                  "level": 2,
                  "shelf": "UPPER",
                  "skuId": "2801022-TG152",
                  "quantity": 1,
                  "version": "00",
                  "productionAttribute": "D",
                  "militaryCivilianMark": "M",
                  "salesArea": "N"
                },
                {
                  "row": 1,
                  "column": 1,
                  "level": 2,
                  "shelf": "LOWER",
                  "skuId": "2801038-TG152",
                  "quantity": 1,
                  "version": "00",
                  "productionAttribute": "D",
                  "militaryCivilianMark": "M",
                  "salesArea": "N"
                }
              ]
            },
            "matchedTasks": null
          },
          {
            "aisleId": "2",
            "assignedTask": null,
            "matchedTasks": null
          },
          {
            "aisleId": "3",
            "assignedTask": null,
            "matchedTasks": null
          },
          {
            "aisleId": "4",
            "assignedTask": null,
            "matchedTasks": null
          },
          {
            "aisleId": "5",
            "assignedTask": null,
            "matchedTasks": null
          }
        ],
        "executableTasksByAisle": {
          "1": [],
          "2": [],
          "3": [],
          "4": [],
          "5": []
        }
      }
    }
  },
  "unconfirmedAfterUpdate": {
    "httpStatus": 200,
    "body": {
      "status": "SUCCESS",
      "message": "获取可执行任务成功",
      "data": {
        "tasksByAisle": {
          "1": {
            "can_executing": [],
            "pending": [],
            "running": [
              {
                "taskId": "RUN-01",
                "taskType": "OUTBOUND",
                "aisleId": "1",
                "positions": [
                  {
                    "row": 1,
                    "column": 1,
                    "level": 2,
                    "shelf": "UPPER",
                    "skuId": "2801022-TG152",
                    "quantity": 1
                  },
                  {
                    "row": 1,
                    "column": 1,
                    "level": 2,
                    "shelf": "LOWER",
                    "skuId": "2801038-TG152",
                    "quantity": 1
                  }
                ]
              }
            ]
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
          },
          "5": {
            "can_executing": [],
            "pending": [],
            "running": []
          }
        },
        "canAcceptExecuting": false
      }
    }
  }
}
```

回归结论：

- 通过
- `resetAssigned = false` 时，未下发的 `pending` 会被清掉
- 已经 `running` 的任务会保留，需结合 `/api/v1/task/unconfirmed` 一起验证

#### 5.6.2 补充：UPDATE + `resetAssigned = true`

前置：

- 同 `5.6.1`

请求体：

```json
{
  "currentTime": "2026-05-24 10:27:00",
  "productionPlan": {
    "operationType": "UPDATE",
    "resetAssigned": true,
    "planDate": "2026-05-24 10:27:00",
    "plans": [
      {
        "planId": "PLAN-LINE1-RESET",
        "lineId": "LINE-1",
        "planIndex": [
          {
            "requiredSkus": [
              [
                {
                  "skuId": "2801021-TR200",
                  "quantity": 1,
                  "version": "00",
                  "productionAttribute": "D",
                  "militaryCivilianMark": "M",
                  "salesArea": "N"
                },
                {
                  "skuId": "2801037-TR200",
                  "quantity": 1,
                  "version": "00",
                  "productionAttribute": "D",
                  "militaryCivilianMark": "M",
                  "salesArea": "N"
                }
              ]
            ]
          }
        ]
      }
    ]
  },
  "inventory": [],
  "aisleStatus": [],
  "tasks": []
}
```

预期：

- `pending`
- `running`

都会被清掉。

注意：

- 当前 mixed 响应不再依赖 `unconfirmedTaskIds` 作为核心语义
- `resetAssigned = true` 的核心是清理可执行状态与队列状态

真实返回摘要：

- `HTTP 200 | SUCCESS | 调度成功`

当前环境实际返回：

```json
{
  "mixedResponse": {
    "httpStatus": 200,
    "body": {
      "status": "SUCCESS",
      "message": "调度成功",
      "data": {
        "scheduleId": "SCH-C71772C9",
        "timestamp": "2026-06-05T08:19:02Z",
        "aisleAssignments": [
          {
            "aisleId": "1",
            "assignedTask": null,
            "matchedTasks": null
          },
          {
            "aisleId": "2",
            "assignedTask": null,
            "matchedTasks": null
          },
          {
            "aisleId": "3",
            "assignedTask": null,
            "matchedTasks": null
          },
          {
            "aisleId": "4",
            "assignedTask": null,
            "matchedTasks": null
          },
          {
            "aisleId": "5",
            "assignedTask": null,
            "matchedTasks": null
          }
        ],
        "executableTasksByAisle": {
          "1": [],
          "2": [],
          "3": [],
          "4": [],
          "5": []
        }
      }
    }
  },
  "unconfirmedAfterUpdate": {
    "httpStatus": 200,
    "body": {
      "status": "SUCCESS",
      "message": "获取可执行任务成功",
      "data": {
        "tasksByAisle": {
          "1": {
            "can_executing": [],
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
          },
          "5": {
            "can_executing": [],
            "pending": [],
            "running": []
          }
        },
        "canAcceptExecuting": false
      }
    }
  }
}
```

回归结论：

- 通过
- `resetAssigned = true` 会清空 `pending / running / can_executing`
- 同样建议结合 `/api/v1/task/unconfirmed` 一起验证，而不只看 `mixed` 返回
