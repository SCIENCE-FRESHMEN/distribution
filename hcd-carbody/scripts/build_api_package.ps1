<#
构建车身立体库可部署的 Windows API 目录包。

采用 PyInstaller --onedir 而非 --onefile：配置文件必须在部署后继续编辑，
且 FastAPI、NumPy、Pandas 等依赖以目录包方式启动更稳定。
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

# 独立构建环境只安装 API 的实际依赖，避免完整 Conda 环境的训练、测试包被误打入产物。
$BuildEnvPath = Join-Path $ProjectRoot $BuildEnvDir
$BuildPython = Join-Path $BuildEnvPath "Scripts\python.exe"
if (-not (Test-Path $BuildPython)) {
    & $Python -m venv $BuildEnvPath
    if ($LASTEXITCODE -ne 0) {
        throw "创建构建虚拟环境失败，退出码: $LASTEXITCODE"
    }
}

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
$WorkPath = Join-Path $ProjectRoot "build\pyinstaller"
$SpecPath = Join-Path $ProjectRoot "build\spec"
$BuildLog = Join-Path $ProjectRoot "build\pyinstaller-build.log"

# Uvicorn 通过字符串 api.main:app 导入应用，需显式声明。配置和仿真数据作为内置兜底
# 一并打包；构建完成后再复制一份到 exe 同级 config/，运行时优先使用该外置副本。
$PyInstallerArgs = @(
    "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--console",
    "--name", "carbody-api", "--distpath", $DistPath, "--workpath", $WorkPath,
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

& $BuildPython @PyInstallerArgs 2>&1 | Tee-Object -FilePath $BuildLog
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller 打包失败，退出码: $LASTEXITCODE。详见日志: $BuildLog"
}

$ReleaseRoot = Join-Path $DistPath "carbody-api"
$ReleaseConfigPath = Join-Path $ReleaseRoot "config"
Copy-Item -Path $ConfigSource -Destination $ReleaseConfigPath -Recurse -Force

# 与可执行文件一同提供当前接口文档，便于部署方按实际版本联调。
Copy-Item -Path (Join-Path $ProjectRoot "docs\API接口说明.md") -Destination $ReleaseRoot -Force
# 同步部署说明，明确外置配置、日志与重启规则。
Copy-Item -Path (Join-Path $ProjectRoot "docs\API程序打包与部署说明.md") -Destination $ReleaseRoot -Force

Write-Host "构建完成: $(Join-Path $ReleaseRoot 'carbody-api.exe')"
Write-Host "可编辑配置目录: $ReleaseConfigPath"
Write-Host "接口文档: $(Join-Path $ReleaseRoot 'API接口说明.md')"
Write-Host "部署说明: $(Join-Path $ReleaseRoot 'API程序打包与部署说明.md')"
