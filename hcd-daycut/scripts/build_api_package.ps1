<#
构建可部署的 Windows API 服务目录包。

目录包而非 --onefile：BOM 和仓库配置必须作为部署后可编辑的外部文件保留，
且 FastAPI、NumPy 等依赖在 onedir 模式下启动更稳定。
#>
param(
    [string]$Python = "D:\anaconda\python.exe",
    [string]$OutputDir = "release",
    [string]$BuildEnvDir = "build\venv-api"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

if (-not (Test-Path $Python)) {
    throw "未找到 Python: $Python"
}

# 使用独立构建环境而非完整 Conda 环境。PyInstaller 按实际导入链收集依赖，若直接
# 使用包含训练、文档和测试工具的 Conda，部分 hook 会把无关包一起分析或打入产物。
$BuildEnvPath = Join-Path $ProjectRoot $BuildEnvDir
$BuildPython = Join-Path $BuildEnvPath "Scripts\python.exe"
if (-not (Test-Path $BuildPython)) {
    & $Python -m venv $BuildEnvPath
    if ($LASTEXITCODE -ne 0) {
        throw "创建构建虚拟环境失败，退出码: $LASTEXITCODE"
    }
}

# 构建环境只安装 API 的运行依赖和 PyInstaller，不继承 Conda 的额外包。
& $BuildPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
    throw "升级构建环境 pip 失败，退出码: $LASTEXITCODE"
}
& $BuildPython -m pip install -r requirements.txt -r (Join-Path $PSScriptRoot "打包依赖.txt")
if ($LASTEXITCODE -ne 0) {
    throw "安装构建环境依赖失败，退出码: $LASTEXITCODE"
}

$Separator = [IO.Path]::PathSeparator
$DistPath = Join-Path $ProjectRoot $OutputDir
$ConfigSource = Join-Path $ProjectRoot "config"
$SimulationDataSource = Join-Path $ProjectRoot "simulation\data"

# Uvicorn 以字符串 ``api.main:app`` 导入应用，需显式声明该动态入口。
# 其余业务模块由 api.main 的静态导入链自动收集，避免扫描整个 Conda 环境。
# 当前 API 源码未导入深度学习训练或测试框架；Conda 环境中的可选依赖会被部分
# PyInstaller hook 间接发现，显式排除可避免无关框架显著放大分析时间和包体。
$WorkPath = Join-Path $ProjectRoot "build\pyinstaller"
$SpecPath = Join-Path $ProjectRoot "build\spec"
$BuildLog = Join-Path $ProjectRoot "build\pyinstaller-build.log"
$PyInstallerArgs = @(
    "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--console",
    "--name", "hcd-api", "--distpath", $DistPath, "--workpath", $WorkPath,
    "--specpath", $SpecPath, "--add-data", "${ConfigSource}${Separator}config",
    "--add-data", "${SimulationDataSource}${Separator}simulation\data",
    "--hidden-import", "api.main",
    "--exclude-module", "IPython",
    "--exclude-module", "pytest",
    "--exclude-module", "py",
    "--exclude-module", "tensorflow",
    "--exclude-module", "keras",
    "--exclude-module", "tensorboard",
    "--exclude-module", "torch",
    "--exclude-module", "torchvision",
    "--exclude-module", "torchaudio",
    "run_api.py"
)

# 在当前 PowerShell 进程中执行并同步等待；日志保留完整 PyInstaller 输出，便于定位
# 缺失动态导入或二进制依赖等打包问题。
& $BuildPython @PyInstallerArgs 2>&1 | Tee-Object -FilePath $BuildLog
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller 打包失败，退出码: $LASTEXITCODE。详见日志: $BuildLog"
}

# 保留一份 exe 同级的可编辑配置。运行时优先读取这里；_internal 中的副本只作为兜底。
$ReleaseConfigPath = Join-Path $DistPath "hcd-api\config"
Copy-Item -Path $ConfigSource -Destination $ReleaseConfigPath -Recurse -Force

# 将面向部署人员的接口与部署说明置于可执行文件同级，交付包无需依赖源码目录。
$ReleaseRoot = Join-Path $DistPath "hcd-api"
Copy-Item -Path (Join-Path $ProjectRoot "docs\API接口说明.md") -Destination $ReleaseRoot -Force
Copy-Item -Path (Join-Path $ProjectRoot "docs\API程序打包与部署说明.md") -Destination $ReleaseRoot -Force

Write-Host "构建完成: $(Join-Path $DistPath 'hcd-api\hcd-api.exe')"
Write-Host "可编辑配置目录: $ReleaseConfigPath"
Write-Host "接口文档: $(Join-Path $ReleaseRoot 'API接口说明.md')"
