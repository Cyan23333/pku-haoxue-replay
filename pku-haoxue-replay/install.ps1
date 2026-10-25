<#
北大好学回放下载器 · 安装脚本
用法示例：
  powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
  powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -TargetDir "D:\Videos\haoxue"
  powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -AdbPath "D:\...\adb.exe" -Serial emulator-5554
  powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -CreateTasks
#>
param(
    [string]$TargetDir = "",
    [string]$AdbPath = "",
    [string]$Serial = "",
    [int]$Workers = 8,
    [string]$InstallRoot = (Join-Path $env:USERPROFILE ".qoder-cn\skills"),
    [string]$PipIndexUrl = "",
    [switch]$SkipDeps,
    [switch]$CreateTasks,
    [switch]$Verify
)

$ErrorActionPreference = "Stop"

try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch { }

Write-Host "== 北大好学回放下载器 · 安装 ==" -ForegroundColor Cyan

# ---------------------------------------------------------------- Python ---
$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
    throw "未找到 python。请安装 Python 3.10+ 并勾选 Add Python to PATH，然后重开终端。"
}
$python = $pythonCmd.Source
$version = & $python -c "import sys; print('{0}.{1}'.format(*sys.version_info[:2]))"
if ([version]$version -lt [version]"3.9") {
    throw "需要 Python 3.9+，当前为 $version。"
}
Write-Host "[+] Python $version：$python"

$pythonwCmd = Get-Command pythonw -ErrorAction SilentlyContinue
$pythonw = if ($pythonwCmd) { $pythonwCmd.Source } else { $python }

# ------------------------------------------------------------ dependencies ---
if (-not $SkipDeps) {
    Write-Host "[*] 安装依赖（requests / cryptography / imageio-ffmpeg）…"
    $pipArgs = @("-m", "pip", "install", "-r", (Join-Path $PSScriptRoot "requirements.txt"))
    if ($PipIndexUrl) { $pipArgs += @("-i", $PipIndexUrl) }
    & $python @pipArgs
    if ($LASTEXITCODE -ne 0) {
        throw "依赖安装失败。可重试并指定国内镜像：-PipIndexUrl https://pypi.tuna.tsinghua.edu.cn/simple"
    }
}

# --------------------------------------------------------------- locate adb ---
function Find-Adb {
    param([string]$Preferred)
    if ($Preferred) {
        if (Test-Path $Preferred) { return (Resolve-Path $Preferred).Path }
        $cmd = Get-Command $Preferred -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
        throw "指定的 adb 不存在：$Preferred"
    }
    $cmd = Get-Command adb -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }

    $roots = @(
        (Join-Path $env:ProgramFiles "Netease\MuMu"),
        (Join-Path ${env:ProgramFiles(x86)} "Netease\MuMu"),
        "D:\Program Files\Netease\MuMu",
        "C:\Program Files\Netease\MuMu"
    ) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -Unique

    foreach ($root in $roots) {
        $hits = @()
        $hits += Get-ChildItem -Path (Join-Path $root "nx_device\*\shell\adb.exe") -ErrorAction SilentlyContinue
        $hits += Get-ChildItem -Path (Join-Path $root "nx_main\adb.exe") -ErrorAction SilentlyContinue
        $hit = $hits | Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    return $null
}

# ------------------------------------------------------------- skill target ---
$SkillDest = Join-Path $InstallRoot "pku-haoxue-replay"
$sourcePath = (Resolve-Path $PSScriptRoot).Path.TrimEnd("\")
$destPath = [System.IO.Path]::GetFullPath($SkillDest).TrimEnd("\")

if ($sourcePath -ne $destPath) {
    Write-Host "[*] 安装 skill 到：$destPath"
    New-Item -ItemType Directory -Force -Path $destPath | Out-Null
    Get-ChildItem -Path $sourcePath -Force | ForEach-Object {
        Copy-Item -Path $_.FullName -Destination $destPath -Recurse -Force
    }
} else {
    Write-Host "[=] 已在安装目录内运行，跳过复制"
}

$configPath = Join-Path $destPath "haoxue_config.json"
$existing = $null
if (Test-Path $configPath) {
    try { $existing = Get-Content -Path $configPath -Raw -Encoding UTF8 | ConvertFrom-Json } catch { }
}

if (-not $TargetDir) {
    if ($existing -and $existing.target_dir) {
        $TargetDir = [string]$existing.target_dir
    } else {
        $TargetDir = Join-Path $env:USERPROFILE "Videos\haoxue"
    }
}
if (-not $Serial) {
    if ($existing -and $existing.adb_serial) { $Serial = [string]$existing.adb_serial } else { $Serial = "emulator-5554" }
}
if (-not $PSBoundParameters.ContainsKey("Workers") -and $existing -and $existing.workers) {
    $Workers = [int]$existing.workers
}
if (-not $AdbPath) {
    if ($existing -and $existing.adb_path) { $AdbPath = [string]$existing.adb_path }
}
$AdbPath = Find-Adb -Preferred $AdbPath
if (-not $AdbPath) {
    throw "未找到 adb。请安装 MuMu 12，或用 -AdbPath 指定 adb.exe，或把 adb 加入 PATH。"
}
Write-Host "[+] adb：$AdbPath"

New-Item -ItemType Directory -Force -Path $TargetDir | Out-Null
Write-Host "[+] 输出目录：$TargetDir"

$excludes = @()
if ($existing -and $existing.excludes) { $excludes = @($existing.excludes) }

$config = [ordered]@{
    adb_path     = $AdbPath
    adb_serial   = $Serial
    target_dir   = $TargetDir
    workers      = $Workers
    overlap_days = if ($existing -and $existing.overlap_days) { [int]$existing.overlap_days } else { 3 }
    excludes     = $excludes
}
$json = $config | ConvertTo-Json -Depth 6
[System.IO.File]::WriteAllText($configPath, $json, (New-Object System.Text.UTF8Encoding($false)))
Write-Host "[+] 配置写入：$configPath"

# -------------------------------------------------------------- tasks ---
if ($CreateTasks) {
    Write-Host "[*] 注册 Windows 计划任务（当前用户，登录时运行）…"
    $scriptsDir = Join-Path $destPath "scripts"
    $syncScript = Join-Path $scriptsDir "pku_replay_cache.py"
    $convertScript = Join-Path $scriptsDir "sync_haoxue_videos.py"
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 6)

    $syncAction = New-ScheduledTaskAction -Execute $pythonw -Argument ('"{0}" sync' -f $syncScript) -WorkingDirectory $scriptsDir
    $syncTriggers = @("00:00", "06:00", "12:00", "18:00") | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ }
    Register-ScheduledTask -TaskName "PKU Replay Sync" -Action $syncAction -Trigger $syncTriggers `
        -Settings $settings -Principal $principal -Force | Out-Null

    $convertAction = New-ScheduledTaskAction -Execute $pythonw -Argument ('"{0}" --once' -f $convertScript) -WorkingDirectory $scriptsDir
    $daily = New-ScheduledTaskTrigger -Daily -At "00:00"
    $repeat = New-ScheduledTaskTrigger -Once -At "00:00" `
        -RepetitionInterval (New-TimeSpan -Minutes 10) -RepetitionDuration (New-TimeSpan -Days 1)
    $daily.Repetition = $repeat.Repetition
    Register-ScheduledTask -TaskName "PKU Video Convert" -Action $convertAction -Trigger $daily `
        -Settings $settings -Principal $principal -Force | Out-Null

    Write-Host "[+] 已注册：PKU Replay Sync（每天 4 次）、PKU Video Convert（每 10 分钟）"
} else {
    Write-Host "[=] 未注册计划任务（需要时加 -CreateTasks）"
}

# --------------------------------------------------------------- verify ---
if ($Verify) {
    Write-Host "[*] 运行登录自检…"
    & $python (Join-Path $destPath "scripts\pku_replay_cache.py") check
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "自检未通过（退出码 $LASTEXITCODE）。请确认 MuMu 已启动、好学已登录。"
    }
}

Write-Host ""
Write-Host "安装完成。" -ForegroundColor Green
Write-Host "  skill 目录：$destPath"
Write-Host "  配置文件 ：$configPath"
Write-Host "  输出目录 ：$TargetDir"
Write-Host ""
Write-Host "下一步："
Write-Host "  1. 启动 MuMu，打开好学并登录（只需一次）"
Write-Host "  2. python `"$destPath\scripts\pku_replay_cache.py`" check"
Write-Host "  3. 详见 AGENT_SETUP.md 或 操作指南.md"
