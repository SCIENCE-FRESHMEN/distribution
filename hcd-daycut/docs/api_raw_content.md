# API接口字段说明文档

**源文件**: 2_API接口字段说明文档1023.xlsx
**Sheet数量**: 8

---

## Sheet: 混合调度-请求参数

| 字段路径 | 字段名称 | 数据类型 | 是否必填 | 说明 |
| --- | --- | --- | --- | --- |
| tasks | 任务列表 | Array[Object] | 是 | 所有目前潜在可执行的入库货位分配/出库任务列表 |
| tasks[].taskId | 任务ID | String | 是 | 任务的唯一标识符 |
| tasks[].taskType | 任务类型 | String (Enum) | 是 | 任务类型：INBOUND（入库货位分配）/ OUTBOUND（出库） |
| tasks[].targetAisle | 目标巷道 | String | 入库必填 | 入库任务的目标巷道ID |
| tasks[].planId | 计划ID | String | 出库必填 | 出库任务所属的计划ID |
| tasks[].planIndex | 计划下的第几组 | Integer | 出库必填 | 出库任务所属的计划ID下的第几个（从1开始数） |
| tasks[].skus | 货物列表 | Array[Object] | 出入库均必填 | 待入库的货物列表（最多2个SKU） |
| tasks[].skus[].skuId | 货物ID | String | 是 | 货物的唯一标识符（若为入库任务，则第一个放位置在左侧的梁，第二个放位置在右侧的梁） |
| tasks[].skus[].quantity | 货物数量 | Integer | 是 | 货物数量 |
| tasks[].inboundUrgent | 入库是否紧急 | Boolean | 入库必填 | 该入库任务是否因为入口堵塞需要紧急安排入库 |
| aisleStatus | 巷道状态列表 | Array[Object] | 是 | 各巷道的当前状态信息 |
| aisleStatus[].aisleId | 巷道ID | String | 是 | 巷道的唯一标识符 |
| aisleStatus[].isAvailable | 是否可用 | Boolean | 是 | 巷道是否可用于分配新任务 |
| aisleStatus[].unavailableReason | 不可用原因 | String | 可用时为空 | 巷道不可用时的原因，如：MAINTENANCE（维护）、ERROR（故障）、Occupied（被其它任务占用）等 |
| aisleStatus[].exitCongestion | 出口拥堵状态 | Array[Object] | 是 | 巷道对应各产线的拥堵状态列表 |
| aisleStatus[].exitCongestion[].lineId | 产线ID | String | 是 | 产线的唯一标识符 |
| aisleStatus[].exitCongestion[].isCongested | 是否拥堵 | Boolean | 是 | 该产线是否处于拥堵状态 |
| aisleStatus[].bank | 所属库区 | String | 是 | 巷道所属的库区，如：LEFT（左库）、RIGHT（右库），用于计算分散性 |
| inventory | 库存信息 | Array[Object] | 是 | 仓库所有有货货位的详细状态信息 |
| inventory[].aisleId | 巷道ID | String | 是 | 巷道的唯一标识符 |
| inventory[].row | 行数 | Integer | 是 | 货位所在行数 |
| inventory[].column | 列数 | Integer | 是 | 货位所在列数 |
| inventory[].level | 层数 | Integer | 是 | 货位所在层数 |
| inventory[].shelf | 货架位置 | String / null | 是 | 货位所在位置：UPPER（上层）或 LOWER（下层） |
| inventory[].positions[].skuId | 货物ID | String / null | 是 | 如果被占用，存储的货物ID；如果为空则为 null |
| inventory[].positions[].quantity | 货物数量 | Integer | 是 | 该货位存储的货物数量 |

---

## Sheet: 混合调度-响应参数

| 字段路径 | 字段名称 | 数据类型 | 是否必填 | 说明 | 是否系统执行必要信息 |
| --- | --- | --- | --- | --- | --- |
| scheduleId | 调度ID | String | 是 | 本次调度结果的唯一ID，便于追踪和日志记录 | 是 |
| status | 调度状态 | String (Enum) | 是 | 调度结果状态：SUCCESS（成功）、PARTIAL_SUCCESS（部分成功）、FAILED（失败） | 是 |
| timestamp | 时间戳 | String (ISO 8601) | 是 | 调度完成的时间戳 | 是 |
| aisleAssignments | 巷道分配结果 | Array[Object] | 是 | 按巷道维度组织的任务分配结果列表（每个巷道最多分配一个任务） | 是 |
| aisleAssignments[].aisleId | 巷道ID | String | 是 | 巷道的唯一标识符 | 是 |
| aisleAssignments[].assignedTask | 分配的任务 | Object / null | 是 | 分配给该巷道的任务详情，如果未分配则为 null | 是 |
| aisleAssignments[].assignedTask.taskId | 任务ID | String | 是 | 任务的唯一标识符 | 是 |
| aisleAssignments[].assignedTask.taskType | 任务类型 | String (Enum) | 是 | 任务类型：INBOUND（入库货位分配）/ OUTBOUND（出库） | 是 |
| aisleAssignments[].assignedTask.planId | 计划ID | String | 出库任务有 | 出库任务所属的计划ID | 是 |
| tasks[].planIndex | 计划下的第几组 | Integer | 出库必填 | 出库任务所属的计划ID下的第几个（从1开始数） |  |
| aisleAssignments[].assignedTask.positions[] | 源货位 | Object | 出库任务有 | 出库任务使用的具体货位坐标 | 是 |
| aisleAssignments[].assignedTask.positions[].row | 行数 | Integer | 是 | 货位所在行数 | 是 |
| aisleAssignments[].assignedTask.positions[].column | 列数 | Integer | 是 | 货位所在列数 | 是 |
| aisleAssignments[].assignedTask.positions[].level | 层数 | Integer | 是 | 货位所在层数 | 是 |
| aisleAssignments[].assignedTask.positions[].shelf | 货架位置 | String (Enum) | 是 | 货位位置：UPPER（上层）或 LOWER（下层） | 是 |
| aisleAssignments[].assignedTask.positions[].skuId | 货物ID | String | 是 | 货物的唯一标识符 | 是 |
| aisleAssignments[].assignedTask.positions[].quantity | 货物数量 | Integer | 是 | 货物数量 | 是 |

---

## Sheet: 任务执行反馈-请求参数

| 字段路径 | 字段名称 | 数据类型 | 是否必填 | 说明 |
| --- | --- | --- | --- | --- |
| taskId | 任务ID | String | 是 | 任务的唯一标识符 |
| taskType | 任务类型 | String (Enum) | 是 | 任务类型：INBOUND（入库货位分配）/INBOUND_AISLE（入库巷道选择）/ OUTBOUND（出库） |
| status | 任务状态 | String (Enum) | 是 | 任务状态：EXECUTING（执行中）、COMPLETED（已完成）、FAILED（失败） |
| startTime | 开始时间 | String (ISO 8601) | 是 | 任务开始执行的时间戳，格式：ISO 8601 |
| failureReason | 失败原因 | String / null | 是 | 如果任务失败，说明失败原因；否则为 null |

---

## Sheet: 任务执行反馈-相应参数

| 字段路径 | 字段名称 | 数据类型 | 是否必填 | 说明 | 是否系统执行必要信息 |
| --- | --- | --- | --- | --- | --- |
| status | 反馈结果状态 | String (Enum) | 是 | 反馈结果状态接受：SUCCESS（成功）、FAILED（失败） | 是 |

---

## Sheet: 入库分配-请求参数

| 字段路径 | 字段名称 | 数据类型 | 是否必填 | 说明 |
| --- | --- | --- | --- | --- |
| tasks | 任务列表 | Array[Object] | 是 | 需要分配巷道的入库任务列表 |
| tasks[].taskId | 任务ID | String | 是 | 任务的唯一标识符 |
| tasks[].skus | 货物列表 | Array[Object] | 是 | 待入库的货物列表（最多支持2个SKU） |
| tasks[].skus[].skuId | 货物ID | String | 是 | 货物的唯一标识符 |
| tasks[].skus[].quantity | 货物数量 | Integer | 是 | 货物数量 |

---

## Sheet: 入库分配-响应参数

| 字段路径 | 字段名称 | 数据类型 | 是否必填 | 说明 | 是否系统执行必要信息 |
| --- | --- | --- | --- | --- | --- |
| allocationId | 分配ID | String | 是 | 本次分配操作的唯一ID，便于追踪 | 是 |
| assignments | 分配结果列表 | Array[Object] | 是 | 具体的巷道分配结果 | 是 |
| assignments[].taskId | 任务ID | String | 是 | 任务的唯一标识符 | 是 |
| assignments[].recommendedAisle | 推荐巷道 | String | 是 | 算法推荐的目标巷道ID | 是 |

---

## Sheet: 生产计划-请求参数

| 字段路径 | 字段名称 | 数据类型 | 是否必填 | 说明 |
| --- | --- | --- | --- | --- |
| operationType | 操作类型 | String (Enum) | 是 | 操作类型：ADD（新增计划）或 UPDATE（更新计划，即替换原有计划为该计划） |
| planDate | 当前时间 | String (YYYY-MM-DD HH:mm:ss) | 是 | 生产计划的日期，格式：YYYY-MM-DD HH:mm:ss |
| plans | 生产计划列表 | Array[Object] | 是 | 当日各产线的生产计划列表 |
| plans[].planId | 计划ID | String | 是 | 生产计划的唯一标识符 |
| plans[].lineId | 产线ID | String | 是 | 产线的唯一标识符 |
| plans[].requiredSkus | 需求货物列表 | Array[Object] | 是 | 该产线需要的货物清单 |
| plans[].requiredSkus[].skuId | 货物ID | String | 是 | 货物的唯一标识符 |
| plans[].requiredSkus[].quantity | 需求数量 | Integer | 是 | 该货物的需求数量 |

---

## Sheet: 生产计划-响应参数

| 字段路径 | 字段名称 | 数据类型 | 是否必填 | 说明 | 是否系统执行必要信息 |
| --- | --- | --- | --- | --- | --- |
| status | 反馈结果状态 | String (Enum) | 是 | 反馈结果状态接受：SUCCESS（成功）、FAILED（失败） | 是 |

---
