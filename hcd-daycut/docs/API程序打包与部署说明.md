# API 程序打包与部署说明

## 1. 交付形式

API 以 Windows 独立目录包交付，而不是单一 `exe`。最终交付目录为 `release/hcd-api/`，启动文件为 `hcd-api.exe`。目录包仍可在未安装 Python 的服务器上运行；选择目录包是因为 `config/warehouse.json`、`config/time_estimator.json` 与 `config/sku_config.json` 必须在部署后保留为可审查、可修改的外部文件。单文件 `exe` 会将这些文件打入临时目录，不适合配置维护。

构建机需安装 Python 3.10 或更高版本，并执行：

```powershell
cd D:\hcd
.\scripts\build_api_package.ps1
```

脚本使用 `D:\anaconda\python.exe`，自动安装 `scripts/打包依赖.txt` 中的 PyInstaller，并在 `release/hcd-api/` 生成部署目录。若构建机 Python 路径不同，可传入：

```powershell
.\scripts\build_api_package.ps1 -Python C:\Python311\python.exe -OutputDir D:\release
```

构建脚本会将当前 `config/` 复制到 `release/hcd-api/config/`，并将 `docs/API接口说明.md` 与本说明复制到 `release/hcd-api/` 根目录。构建后应将整个 `hcd-api` 目录复制到服务器，不能只复制 `hcd-api.exe`。

### 1.1 打包前修改配置

打包前在源代码目录直接修改下列文件，再执行构建脚本：

```text
D:\hcd\config\warehouse.json
D:\hcd\config\time_estimator.json
D:\hcd\config\sku_config.json
```

构建脚本会将当前 `config/` 目录复制到 `release/hcd-api/config/`。因此应先确认仓库几何、禁用货位、巷道-产线关系、时间参数和优化权重；`sku_config.json` 应保持空 BOM，或预置已确认的 BOM。

### 1.2 修改配置后的构建命令

在完成上述配置修改并保存后，从项目根目录执行：

```powershell
cd D:\hcd
.\scripts\build_api_package.ps1
```

若 PowerShell 因执行策略拒绝运行本地脚本，仅对当前窗口执行：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\scripts\build_api_package.ps1
```

## 2. 部署与启动

部署服务器建议使用 Windows Server 2019 或更高版本、64 位系统、至少 4 GB 内存和 2 GB 可用磁盘。服务监听端口默认为 `8000`；若由其他主机访问，应在 Windows 防火墙中放行该 TCP 端口。

首次部署前，检查部署目录内的 `config/`。启动命令示例：

```powershell
cd D:\release\hcd-api
.\hcd-api.exe --host 0.0.0.0 --port 8000 --workers 1
```

启动成功后，可访问 `http://服务器地址:8000/docs` 查看 OpenAPI 文档。部署包根目录的 `API接口说明.md` 说明各接口、请求字段和响应字段；在线 Swagger 用于按当前运行版本直接发起接口调试。生产环境应保持 `--workers 1`：当前 API 的库存、待提交任务、运行任务、生产计划和虚拟预占均保存于单一进程内存；多工作进程不会共享这些状态，可能造成同一任务被不同进程分别处理。

停止服务使用 `Ctrl+C`。建议通过 Windows 服务管理器、计划任务或受控的启动脚本守护进程，不建议使用 `--reload`；该参数仅用于开发环境的源码热重载。

部署包已经生成时，也可以直接修改服务器部署目录内的 `config/warehouse.json`、`config/time_estimator.json` 和 `config/sku_config.json`，然后重启 `hcd-api.exe`。不需要重新打包；但应保留修改记录，并避免只复制 `exe` 后遗漏对应 `config/` 文件。

### 2.1 部署包配置与日志维护

部署后的 `hcd-api.exe` 优先读取与其同级的外置 `config/` 目录，而不是 `_internal/` 中的内置副本。因此修改仓库参数、禁用货位、FIFO、时间参数、调度权重、BOM 或日志容量时，只需停止并重新启动 API；只有修改 Python 源码、依赖版本或需要替换程序内置资源时才需要重新打包。

API 的 Uvicorn 请求访问和错误日志默认写入部署目录的 `logs/api.log`，并同时保留控制台输出。日志配置位于外置 `config/warehouse.json` 的 `api_logging`：

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
- `backup_count` 限制归档数量；`max_total_size_mb` 限制当前日志和归档的总容量，超过上限时自动删除最旧归档。
- 修改 `api_logging` 同样只需重启 API，不需要重新打包。
- 业务代码通过 `print()` 输出的诊断信息仍在启动控制台；如果运维需要保存该部分，可额外用 PowerShell 标准输出重定向保存，但该重定向文件不受 `api_logging` 容量限制。

## 3. 配置与生效规则

服务启动时读取以下文件。手工修改任一文件后，必须停止并重新启动 API，修改才会进入内存中的 `WarehouseCore`。

| 文件                           | 用途                                                                           | 手工修改后             | API 是否可修改                                |
| ------------------------------ | ------------------------------------------------------------------------------ | ---------------------- | --------------------------------------------- |
| `config/warehouse.json`      | 仓库几何、巷道-产线映射、禁用货位、设备时长、SKU 属性匹配字段、FIFO 与优化权重 | 必须重启 API           | 不可以                                        |
| `config/time_estimator.json` | 堆垛机路径、速度、加速度及取放货时间                                           | 必须重启 API           | 不可以                                        |
| `config/sku_config.json`     | API 专用 BOM/SKU 结构                                                          | 手工修改后必须重启 API | 可以，通过 `POST /api/v1/bom/update` 热更新 |

`config/sku_config.json` 首次交付时保持空结构：

```json
{
  "sku_types": [],
  "sku_pairs": {},
  "sku_solo": {},
  "sku_to_production_line": {}
}
```

API 收到 `POST /api/v1/bom/update` 的完整 BOM 后，会直接覆盖并创建 `config/sku_config.json`，同时刷新当前进程的 BOM、入库巷道分配器和货位分配器缓存，因此该接口更新 BOM 后不需要重启。请求必须同时提供 `sku_types`、`sku_pairs`、`sku_solo`、`sku_to_production_line` 四个字段。

`simulation/data/sku_config.json` 保留为命令行仿真的原始 BOM 文件。API 不读取、不覆盖该文件；因此 API BOM 变更不会改变既有仿真数据，反之亦然。

库存、任务、生产计划和巷道状态不是配置文件。它们通过 `POST /api/v1/schedule/mixed`、入库分配、任务反馈等接口在当前进程内更新；服务重启后需要由 WMS/WCS 重新同步。当前接口不能在线修改仓库几何、禁用货位、巷道-产线服务关系、路径物理参数、`match_fields`、FIFO 开关或优化权重。

## 4. 空 BOM 的启动和使用

空 BOM 是可启动状态。服务可以正常启动、访问健康检查和 BOM 查询接口；但任何携带 SKU 的入库任务或混合调度请求都会被校验拦截，返回 HTTP `400`，响应包括：

```json
{
  "status": "FAILED",
  "message": "存在未维护在BOM中的SKU，无法执行调度。",
  "data": {
    "invalidSkus": ["示例SKU"]
  }
}
```

这表示需要先调用 BOM 更新接口维护该 SKU，而不是服务异常。出库任务同样应在 BOM 已维护且库存已同步后再提交。

## 5. 发布前检查

1. 构建前确认 `config/sku_config.json` 为交付所需的空 BOM 或已确认 BOM，且不携带测试库存、任务或生产计划。
2. 运行 `hcd-api.exe --host 127.0.0.1 --port 8000`，访问 `/docs`，确认服务可启动。
3. 在空 BOM 交付场景下，提交一个未知 SKU 的入库或 mixed 请求，确认返回 `400` 和 `invalidSkus`。
4. 调用 `POST /api/v1/bom/update` 写入一组完整 BOM，再调用 `GET /api/v1/bom/config`，确认文件与内存配置一致。
5. 重启服务后再次调用 `GET /api/v1/bom/config`，确认 BOM 从 `config/sku_config.json` 正确恢复。

## 6. 卸载与清理

当前交付为绿色目录包，不安装 Python、不注册 Windows 服务、不写入注册表。卸载前如需保留 BOM、仓库配置或运行日志，应先备份部署目录下的 `config/` 和日志目录。

以命令行直接启动时，先在运行窗口按 `Ctrl+C` 停止 `hcd-api.exe`，确认进程已退出后，删除整个部署目录即可：

```powershell
Remove-Item -LiteralPath D:\release\hcd-api -Recurse -Force
```

如果部署时由运维人员额外注册为 Windows 服务，则应先停止并删除该服务，再删除部署目录。以下命令中的服务名仅为示例，应替换为实际注册名称：

```powershell
sc.exe stop HcdApiService
sc.exe delete HcdApiService
Remove-Item -LiteralPath D:\release\hcd-api -Recurse -Force
```

PyInstaller 仅安装在构建机，用于生成部署包；目标服务器不需要卸载 Python 或 PyInstaller。源码目录中的 `build/`、`release/` 可在确认不再需要构建产物后删除，不影响项目源代码。
