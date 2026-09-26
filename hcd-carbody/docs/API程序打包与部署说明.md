# 车身立体库 API 程序打包与部署说明

## 1. 交付形式

车身立体库 API 以 Windows 独立目录包交付，而不是单一 `exe`。最终交付目录为 `release/carbody-api/`，启动文件为 `carbody-api.exe`。目录包可在未安装 Python 的服务器上运行，并保留部署后可编辑的外置 `config/` 目录；不应只复制 `carbody-api.exe`，还必须保留 `_internal/`、`config/` 及同级其他文件。

构建机需安装 Python 3.10 或更高版本，执行：

```powershell
cd D:\hcdcarbody
.\scripts\build_api_package.ps1
```

构建脚本默认使用 `D:\anaconda\python.exe`，会创建或复用 `build\venv-api` 构建环境，安装 `requirements.txt` 和 `scripts/打包依赖.txt`，最后在 `release/carbody-api/` 生成部署目录。若构建机 Python 路径不同，可传入：

```powershell
.\scripts\build_api_package.ps1 -Python C:\Python311\python.exe -OutputDir D:\release
```

构建脚本会将当前 `config/` 复制到部署包的 `release/carbody-api/config/`，并复制当前版本的 `docs/API接口说明.md` 到部署根目录。

### 1.1 打包前配置检查

打包前应检查：

```text
D:\hcdcarbody\config\warehouse.json
D:\hcdcarbody\config\time_estimator.json
```

- `warehouse.json`：巷道尺寸与服务产线、禁用货位、特征匹配、FIFO、调度与分配权重、设备时间及 API 日志容量。
- `time_estimator.json`：入/出库口映射、取放货时间、堆垛机水平与垂直运动参数，以及异构巷道的物理参数覆盖。

应确认巷道 1 至 3 的 17 列、巷道 4 的 11 列等实际尺寸与禁用货位、出入库口坐标一致；配置中不应包含测试时的外部库存、任务或生产计划，这些运行时状态由 API 调用方在服务启动后同步。

### 1.2 修改配置后的构建命令

在完成上述配置修改并保存后，从项目根目录执行：

```powershell
cd D:\hcdcarbody
.\scripts\build_api_package.ps1
```

若 PowerShell 因执行策略拒绝运行本地脚本，仅对当前窗口执行：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\scripts\build_api_package.ps1
```

## 2. 部署与启动

部署服务器建议使用 Windows Server 2019 或更高版本、64 位系统、至少 4GB 内存和 2GB 可用磁盘。服务默认监听 `8000` 端口；由其他主机访问时需在 Windows 防火墙放行对应 TCP 端口。

将整个 `carbody-api` 目录复制到服务器后，在部署目录中启动：

```powershell
cd D:\release\carbody-api
.\carbody-api.exe --host 0.0.0.0 --port 8000 --workers 1
```

启动成功后可访问：

- Swagger：`http://服务器地址:8000/docs`
- ReDoc：`http://服务器地址:8000/redoc`
- 状态接口：`http://服务器地址:8000/api/v1/status`

生产环境应使用 `--workers 1`。库存、任务、生产计划、巷道状态、预占和执行状态均保存在单一进程内存，多工作进程之间不共享这些状态，可能对同一业务任务产生不一致决策。停止服务时在启动窗口按 `Ctrl+C`；不建议在生产环境使用 `--reload`，该参数仅用于开发时源码热重载。

### 2.1 部署后修改配置

部署后的 `carbody-api.exe` 优先读取与其同级的外置 `config/` 目录，不读取 `_internal/` 中的内置副本作为首选配置。因此部署包生成后，如需修改仓库参数、巷道尺寸、禁用货位、匹配规则、FIFO、出入库口、时间参数、调度权重或日志容量，只需修改：

```text
D:\release\carbody-api\config\warehouse.json
D:\release\carbody-api\config\time_estimator.json
```

然后停止并重新启动 `carbody-api.exe` 即可生效，**不需要重新打包**。只有修改 Python 源码、依赖版本或需要替换程序内置资源时，才需要回到构建机重新执行打包脚本。

库存、生产计划、巷道状态和任务不是配置文件内容。它们应由 WMS/WCS 通过 `POST /api/v1/schedule/mixed` 和任务反馈接口同步；服务重启后需要重新同步。

## 3. 配置与日志生效规则

服务启动时读取下列外置配置。手工修改后均需重启 API，修改才会进入当前进程内的 `WarehouseCore` 和估时器。

| 文件 | 用途 | 手工修改后 | API 是否可修改 |
| --- | --- | --- | --- |
| `config/warehouse.json` | 巷道几何、产线映射、禁用/禁入规则、特征匹配、FIFO、分配与调度参数、设备时间、日志参数 | 必须重启 API | 库存、任务、计划与巷道状态可通过 API 同步；其余配置不通过 API 修改 |
| `config/time_estimator.json` | 入/出库口位置、取放货时间、堆垛机运动参数、巷道差异参数 | 必须重启 API | 不可以 |

### 3.1 API 日志

API 的 Uvicorn 请求访问和错误日志默认同时输出到控制台和部署目录的 `logs/api.log`。日志参数位于 `config/warehouse.json` 的 `api_logging`：

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

- `api.log` 达到 `max_file_size_mb` 后滚动为 `api.log.1`、`api.log.2` 等归档。
- `backup_count` 限制归档数量；`max_total_size_mb` 限制当前日志和归档的累计容量。达到总容量上限时，系统自动删除最旧归档，不删除正在写入的 `api.log`。
- 修改 `api_logging` 后只需重启 API，不需要重新打包。
- 业务代码直接使用 `print()` 的诊断信息仍在启动控制台；PowerShell 的 `Start-Process -RedirectStandardOutput/-RedirectStandardError` 可额外保存标准输出，但该重定向文件不受 `api_logging` 容量限制，应由运维侧自行轮转或清理。

## 4. 发布前检查

1. 执行构建脚本后，确认 `release/carbody-api/carbody-api.exe`、`_internal/`、`config/` 和 `API接口说明.md` 均存在。
2. 检查部署包外置 `config/warehouse.json` 的巷道尺寸、禁用货位、特征匹配、FIFO 和 `api_logging`。
3. 运行 `carbody-api.exe --host 127.0.0.1 --port 8000 --workers 1`，访问 `/docs` 与 `/api/v1/status`，确认服务可启动。
4. 用一份完整库存快照和一条测试任务调用 `POST /api/v1/schedule/mixed`，确认返回的货位和巷道坐标在相应巷道尺寸范围内。
5. 调用 `POST /api/v1/task/feedback` 回传 `EXECUTING`、`COMPLETED`，再用 `GET /api/v1/task/unconfirmed` 核验状态切换。
6. 访问部署目录 `logs/api.log`，确认请求访问日志落盘且总容量配置正确。

## 5. 卸载与清理

当前交付为绿色目录包，不安装 Python、不注册 Windows 服务、不写入注册表。卸载前如需保留运行记录或当前参数，应先备份部署目录的 `config/` 和 `logs/`。

以命令行直接启动时，先按 `Ctrl+C` 停止 `carbody-api.exe`，确认进程已退出后，再删除部署目录：

```powershell
Remove-Item -LiteralPath D:\release\carbody-api -Recurse -Force
```

若运维侧额外注册了 Windows 服务，应先停止并删除该服务，再删除部署目录。PyInstaller 仅在构建机使用，目标服务器不需要卸载 Python 或 PyInstaller。
