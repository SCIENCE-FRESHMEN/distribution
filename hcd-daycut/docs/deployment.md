---



# 部署文档

本文档用于指导在目标环境中部署本项目。

## 1. 概述

- 系统用途：仓储仿真/调度，并支撑线上真实仓库运行（入库/出库/移库策略）
- 目标读者：研发/算法/运维/测试
- 部署目标：本地验证 + 线上环境部署运行

## 2. 环境要求

- 操作系统：Windows 10/11 或 Linux（推荐 Ubuntu 20.04+）
- CPU/内存：>= 4C/8G
- 磁盘：>= 10G 可用
- Python：3.10+
- 依赖库：见 `requirements.txt`
- 端口：离线仿真无需端口；若启用 API，默认 8000

## 3. 简要目录结构

- `run.py`：主仿真入口
- `run_compare.py`：策略对比入口
- `simulation/`：仿真核心逻辑
- `allocation/`：分配策略
- `config/`：配置
- `simulation/data/`：任务/计划数据
- `logs/`：运行日志

## 4. 安装与依赖

1) 获取代码
   通过代码包的形式发送
2) 创建并激活虚拟环境（运行离线仿真时需要，线上部署可忽略）

```bash
python -m venv .venv
.\.venv\Scripts\activate  # Windows
source .venv/bin/activate # Linux/Mac
```

3) 安装依赖

```bash
pip install -r requirements.txt
```

## 5. 配置说明

请根据实际路径与数据修改以下配置

- `config/warehouse.json`
  - 仓库规模、层数、列数、巷道等
- `simulation/data/inbound_task_config.json`
  - 入库任务数据（可由 `simulation\config_bulider\inbound_task_config_builder.py` 生成）
- `simulation/data/production_plan_config.json`
  - 生产计划数据（可由 `simulation\config_bulider\plan_config_builder.py` 生成）
- `simulation/data/sku_config.json`
  - SKU 配对/单梁配置（可由 `simulation\config_bulider\sku_config_builder.py` 生成）

### 可选属性匹配

- 额外属性字段示例：`version`、`生产属性`
- `match_fields` 用于指定需要参与匹配的字段（如有）
- 由于入库数据中没有额外字段，我们添加了默认值，所以遇到产线所需内容不匹配时会无法推进产线。所以想要运行离线策略时，请将 `match_fields` 置空

## 6. 离线策略运行步骤

1) 准备数据Excel/CSV -> JSON

```bash
python simulation/config_bulider/inbound_task_config_builder.py
python simulation/config_bulider/plan_config_builder.py
python simulation/config_bulider/sku_config_builder.py
```

2) 运行离线仿真

```bash
python run.py
```

3) 运行策略对比

```bash
python run_compare.py
```

## 7. API 服务化部署说明

### 基础信息

- **协议**: HTTP/HTTPS
- **基础 URL**: `http://{host}:{port}/api/v1`
- **默认端口**: 8000
- **数据格式**: JSON
- **字符编码**: UTF-8

### 请求头

```http
Content-Type: application/json
Accept: application/json
```

### 启动 API 服务

```bash
# 默认启动（监听所有网络接口）
python run_api.py
```

### 访问服务

**本机访问**:

- API 文档: `http://localhost:8000/docs`
- API 服务: `http://localhost:8000`

**局域网访问**（让其他人访问）:

1. **查看本机 IP 地址**:

   ```bash
   # macOS/Linux
   ifconfig | grep "inet "

   # Windows
   ipconfig
   ```
2. **其他人访问地址**（假设您的 IP 是 `192.168.1.100`）:

   - API 文档: `http://192.168.1.100:8000/docs`
   - API 服务: `http://192.168.1.100:8000`
3. **注意事项**:

   - 默认配置 `host=0.0.0.0` 已支持外部访问
   - 如无法连接，请检查防火墙设置并允许 8000 端口
   - 确保在同一局域网内

### 完整的 API 端点列表

| 方法 | 端点                     | 说明                 | 类型 |
| ---- | ------------------------ | -------------------- | ---- |
| POST | /api/v1/schedule/mixed   | 混合调度（核心接口，内联 productionPlan + currentGroups） | 调度 |
| POST | /api/v1/inbound/allocate | 入库巷道分配         | 入库 |
| POST | /api/v1/task/feedback    | 任务执行状态反馈     | 反馈 |
| GET  | /api/v1/task/unconfirmed | 查看未确认任务       | 调试 |
| GET  | /api/v1/status           | 获取系统状态         | 调试 |
| GET  | /                        | 服务信息             | 基础 |

## 8. 运行与验证

- 运行成功后检查 `logs/` 下最新日志
- 关键指标：
  - 产线完成组数
  - 出入库任务完成数量
  - 配对率统计
- 若无任务派发或库存不足，请查看 `[WARN]` 或 `[DEBUG]` 相关日志

## 9. 日志与监控

- 日志目录：`logs/`
- 常用关键字：
  - `[WARN]`：分配/调度异常或无可用资源
  - `[DEBUG]`：调度细节
  - `[relocation-audit]`：移库审计

## 10. 常见问题

1) **无任务派发/事件为空**
   - 检查 `production_plan_config.json` 是否为空
   - 检查 `inbound_task_config.json` 是否有数据
2) **库存充足但无法出库**
   - 检查 `match_fields` 与任务属性是否一致
   - 检查 SKU 配对配置是否正确
3) **单梁入库异常**
   - 检查入库任务中的 SKU 与属性是否对齐
