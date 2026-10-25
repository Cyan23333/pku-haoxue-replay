# 给 AI Agent 的安装与配置说明

> 目标：在一台**新的 Windows 电脑**上装好这套“北大好学回放下载器”，验证能下载一节回放，并把使用方法交给用户。  
> 本包不含任何 token、密码、日志或状态文件。安装过程不需要、也不允许向用户索要北大门户密码。

## 0. 安全边界（必须遵守）

- 不询问、不接收、不记录用户的账号密码；登录必须由用户自己在模拟器里完成。
- 不打印 token / cookie；检查登录态只看长度或让脚本自检。
- 不修改 App 数据库，不删除 App 内的下载记录。
- 不把课程视频、token 或日志上传到任何第三方。
- 注册 Windows 计划任务前必须先征得用户同意。
- 只下载用户本人有权访问的课程内容。

## 1. 开始前，一次性问用户三件事

1. 视频输出目录用哪个？（默认 `%USERPROFILE%\Videos\haoxue`）
2. 是否要安装定时任务（每天自动同步 + 每 10 分钟转换 App 内下载）？
3. MuMu 模拟器和“北大好学”App 是否已经装好并登录？

不要反复追问；用户没特别要求时用默认值继续。

## 2. 环境检查

在包根目录（含 `install.ps1` 的目录）打开 PowerShell：

```powershell
$PSVersionTable.PSVersion
python --version
```

要求：

- Windows 10/11
- Python 3.10+（最低 3.9），且 `python` / `pip` 在 PATH
- MuMu 12 已安装（其他能 `adb root` 的安卓模拟器也可）

找 adb：

```powershell
Get-ChildItem "$env:ProgramFiles\Netease\MuMu\nx_device\*\shell\adb.exe",
              "D:\Program Files\Netease\MuMu\nx_device\*\shell\adb.exe" `
              -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName
```

启动模拟器后确认在线（把 `<adb>` 换成上面的路径）：

```powershell
& "<adb>" devices
```

期望看到 `emulator-5554    device`（或其他 serial + device）。没有设备就请用户启动 MuMu。

## 3. 安装依赖并部署 skill

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 `
  -TargetDir "D:\Videos\haoxue"
```

安装脚本会自动安装依赖。若想手动先装依赖，执行
`python -m pip install -r .\requirements.txt`，再给安装脚本加 `-SkipDeps`。

可选参数：

- `-AdbPath "<adb 完整路径>"`：自动探测失败时指定
- `-Serial emulator-5554`：多开/非默认 serial
- `-PipIndexUrl https://pypi.tuna.tsinghua.edu.cn/simple`：国内网络装依赖慢时
- `-SkipDeps`：已装好依赖
- `-CreateTasks`：注册计划任务（先问用户）
- `-Verify`：安装后立即跑一次登录自检

安装脚本会：

1. 检查 Python 版本
2. 安装 `requests` / `cryptography` / `imageio-ffmpeg`
3. 找 adb，写 `<skill>\haoxue_config.json`
4. 把整个 skill 复制到 `%USERPROFILE%\.qoder-cn\skills\pku-haoxue-replay`
5. 创建输出目录
6. 可选注册计划任务

安装后：

```powershell
$skill = Join-Path $env:USERPROFILE ".qoder-cn\skills\pku-haoxue-replay"
Get-Content "$skill\haoxue_config.json"
```

## 4. 准备账号（用户手动）

1. 用户在 MuMu 里打开“北大好学”，用自己的账号登录（扫码、短信或密码由用户自己输入）。
2. 确认模拟器有 root：`& "<adb>" -s emulator-5554 root`，然后 `& "<adb>" -s emulator-5554 wait-for-device`。
3. 验证 token 存在，**不要打印 token 本身**：

```powershell
& "<adb>" -s emulator-5554 shell "sqlite3 /data/data/com.pku.studious/databases/DBFactory.db 'SELECT length(_token), length(_cookieToken) FROM UserInfo LIMIT 1;'"
```

期望输出两个大于 0 的数字。为空说明还没登录。

如果设备上没有 `sqlite3`，回退方案：用 root 把数据库 pull 到临时目录，用 Python 的 `sqlite3` 读长度，读完后删除临时文件。

## 5. 登录自检

```powershell
python "$skill\scripts\pku_replay_cache.py" check
```

- 退出码 `0`：token 可用，继续。
- 退出码 `2`：登录失效或没有 token。请用户在模拟器里重新登录，然后重跑；**不要尝试绕过**。输出目录会生成 `NEED_LOGIN.txt`。
- 退出码 `1`：网络、adb 或 API 问题，按第 9 节排查。

## 6. 功能验证

### 6.1 列表

```powershell
python "$skill\scripts\pku_replay_cache.py" list --date 2026-10-20
```

日期用今天，或最近一个工作日。若为空，换前一个工作日再试。

### 6.2 干跑

```powershell
python "$skill\scripts\pku_replay_cache.py" download `
  --course "<课名>" --date "<YYYY-MM-DD>" --dry-run
```

确认输出里有匹配的节次和媒体地址；同名课程用 `list --date` 看 `course_id`，再加 `--course-id`。

### 6.3 真实下载一节（让用户指定）

```powershell
python "$skill\scripts\pku_replay_cache.py" download `
  --course "<课名>" --date "<YYYY-MM-DD>" --section "<第X-X节>"
```

验证：

```powershell
Get-ChildItem "D:\Videos\haoxue" -Filter *.mp4 | Sort-Object LastWriteTime -Descending | Select-Object -First 3 Name,Length,LastWriteTime

$ff = python -c "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"
& $ff -hide_banner -i "<刚下载的 mp4>" 2>&1 | Select-String "Duration"
```

文件应有几百 MB 到 1 GB 以上，时长与课程节次吻合。若回放还没生成，接口会返回“待生成”，这不是故障，过几小时重试。

## 7. 可选：定时任务（先问用户）

用户同意后再执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -CreateTasks
Get-ScheduledTask -TaskName "PKU Replay Sync","PKU Video Convert" | Get-ScheduledTaskInfo
```

- `PKU Replay Sync`：每天 00:00 / 06:00 / 12:00 / 18:00 按日期增量缓存
- `PKU Video Convert`：每 10 分钟把 App 内已下载的 m3u8 转成 MP4，成功后清理源文件
- 两者只在当前用户登录 Windows 时运行，错过会补跑，不并发

`sync` 的排除课程在 `haoxue_config.json` 的 `excludes` 里配置；默认不排除任何课，需要时再让用户给清单。

## 8. 交付给用户

告诉用户：

- 输出目录：`haoxue_config.json` 里的 `target_dir`
- 日志：`<skill>\scripts\replay_cache.log`；App 下载转换日志在输出目录 `sync.log`
- 以前怎么用：`check` 自检、`list --date` 查课、`download --course ... --date ...` 下载
- 登录过期：模拟器里重新登录好学即可，脚本会写 `NEED_LOGIN.txt` 并通知
- 以后可以直接对 AI 说：“下载概率论 10 月 20 日的回放”

## 9. 排障

| 现象 | 处理 |
|------|------|
| `python` 不是内部命令 | 安装 Python 3.10+，勾选 Add to PATH，重开终端；或用 `py -3` |
| `pip install` 很慢/超时 | 加 `-PipIndexUrl https://pypi.tuna.tsinghua.edu.cn/simple` |
| 找不到 adb | 用 `-AdbPath` 指定 MuMu 的 `adb.exe`；或把 adb 加入 PATH |
| `adb devices` 为空 | 启动 MuMu；必要时 `adb connect 127.0.0.1:16384` |
| `check` 退出码 2 | 用户在模拟器里登录好学；确认 UserInfo 表非空 |
| 没有 root / 读不到数据库 | MuMu 设置里开启 root；执行 `adb root`，等设备重连 |
| 列表为空 | 日期可能没课，换工作日；或课程不在“我的课程”列表，用 `--date` |
| 找不到课程 | 先 `list --date` 看当天课程名；同名课程加 `--course-id` |
| 回放“待生成” | 正常，几小时后重试；不要反复强行下载 |
| 下载中 401/403 | token 失效，重跑 `check` 并重新登录 |
| 计划任务没跑 | 确认用户已登录 Windows；`Get-ScheduledTaskInfo` 看上次结果 |
| 杀毒软件拦截 | 将 `pythonw.exe` 与 skill 目录加入白名单（脚本只访问本地 adb 和课程 API） |

## 10. 验收清单

- [ ] `check` 退出码 0
- [ ] `list --date <最近工作日>` 能列出课程
- [ ] `download ... --dry-run` 能显示媒体地址
- [ ] 实际下载至少 1 节 MP4，文件大小和时长正常
- [ ] `haoxue_config.json` 的 `target_dir` 与用户确认的一致
- [ ] 包内没有 `.pku_tokens.json`、`.replay_sync_state.json`、`.sync_history.json`、日志或 probe JSON
- [ ] 如注册了计划任务，已向用户说明运行频率和登录前提

## 11. 卸载

```powershell
Unregister-ScheduledTask -TaskName "PKU Replay Sync","PKU Video Convert" -Confirm:$false -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force (Join-Path $env:USERPROFILE ".qoder-cn\skills\pku-haoxue-replay")
```

已下载的 MP4 不会被删除；如需清理，由用户自行处理输出目录。
