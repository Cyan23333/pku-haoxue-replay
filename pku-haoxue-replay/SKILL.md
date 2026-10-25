---
name: pku-haoxue-replay
description: 从北大好学 App（com.pku.studious）批量下载和增量缓存课程回放：复用 MuMu 模拟器中已登录账号的 token，直连课程 API 获取 m3u8，AES-128 解密后封装 MP4。用户提到好学、回放下载、课程录像、批量缓存某门课某天的视频、补下历史回放、检查登录态、处理 App 内下载的 m3u8 时使用。
---

# 北大好学回放下载

## Overview

不操作 App UI，也不接触账号密码：从 MuMu 模拟器的 App 数据库读取已登录 token，直接调用课程 API 拿到回放地址，在电脑上下载 HLS 切片、AES-128 解密、ffmpeg 封装为 MP4。脚本随本 skill 分发，路径和输出目录由 `haoxue_config.json` 配置。

## 何时使用

- 用户指定课程名 / 日期 / 节次，要求批量下载回放
- 补下历史回放，或每日自动缓存新增回放
- 检查登录是否失效、列出课程与节次、探测回放是否已生成
- 把 App 内已下载的 m3u8 源文件转成 MP4，或清理转换后的源文件

## 核心原则

1. 一律通过 `scripts/pku_replay_cache.py` 操作；只有脚本缺功能时才改它，不要另写一套。
2. token 属于敏感凭据：只在本机使用，不打印、不写入对话、不外传。
3. 不代用户登录，不索要密码。登录失效时让用户在模拟器里打开 App 登录一次，然后重跑。
4. 批量下载前先 `--dry-run` 确认匹配结果，尤其是同名课程和日期范围。
5. 同名课程不猜：用 `list --date` 或 `--course-id` 区分。
6. 不改 App 数据库、不删 App 下载记录；清理只删文件。

## 文件布局

```text
pku-haoxue-replay/
├── SKILL.md
├── AGENT_SETUP.md          # 首次安装/配置（给 agent 看）
├── 操作指南.md              # 日常使用（给人看）
├── install.ps1             # 检测环境、写配置、安装 skill、可选注册任务
├── requirements.txt
├── config.example.json
├── haoxue_config.json      # 安装后生成（不入包）
└── scripts/
    ├── pku_replay_cache.py
    └── sync_haoxue_videos.py
```

安装位置通常是 `%USERPROFILE%\.qoder-cn\skills\pku-haoxue-replay`。下文命令用 `<SKILL>` 代指该目录。

运行期文件都在 `scripts/` 下：`.pku_tokens.json`（token 缓存，Windows ACL 限制）、`.replay_sync_state.json`（按 sub_id 去重）、`replay_cache.log`。

## 配置

`<SKILL>\haoxue_config.json`：

| 字段 | 说明 | 默认 |
|------|------|------|
| `adb_path` | MuMu 的 adb.exe 完整路径 | 自动探测 |
| `adb_serial` | 模拟器 serial | `emulator-5554` |
| `target_dir` | MP4 输出目录 | `%USERPROFILE%\Videos\haoxue` |
| `workers` | HLS 切片并发 | `8` |
| `overlap_days` | 增量同步回看天数 | `3` |
| `excludes` | `sync` 排除规则，字符串或字符串数组 | `[]` |

`excludes` 示例：`["普通物理", ["经济学原理", "讨论课"]]`。数组表示“同时包含所有词”才排除；`["经济学原理", "讨论课"]` 只排除讨论课，不排除主课。

查找顺序：环境变量 `HAOXUE_CONFIG` → `scripts\haoxue_config.json` → skill 根目录。`HAOXUE_TARGET_DIR` 可临时覆盖输出目录。

## 常用命令

```bash
cd "<SKILL>\scripts"

# 登录自检（失效时退出码 2，并在输出目录写 NEED_LOGIN.txt）
python pku_replay_cache.py check

# 列出课程；某门课的回放
python pku_replay_cache.py list
python pku_replay_cache.py list --course 心理学导论

# 按日期列出当天可回放课程（课程不在“我的课程”列表里时用这个）
python pku_replay_cache.py list --date 2026-10-20

# 精确下载
python pku_replay_cache.py download --course 概率论 --date 2026-10-20
python pku_replay_cache.py download --course 概率论 --date 2026-10-20 --section 第3-4节

# 批量清单
python pku_replay_cache.py download --targets targets.json

# 每日增量 / 历史补下
python pku_replay_cache.py sync --dry-run
python pku_replay_cache.py sync --since 2026-09-01
```

`targets.json`：

```json
[
  {"course": "概率论", "date": "2026-10-20", "section": "第3-4节"},
  {"course": "抽象代数", "date": "2026-10-19"}
]
```

退出码：`0` 成功；`1` 其它失败；`2` 登录失效（已发通知）。

## Agent 执行流程（用户要“下载某视频”时）

1. 解析需求：课程全名、日期、节次（可省略）、是否排除某些课。
2. 跑 `check`。退出码 2：告诉用户在模拟器里登录，**停止**，不要尝试绕过。
3. 用 `list --date YYYY-MM-DD` 或 `list --course 名称` 确认课程存在、节次写法。
4. 批量任务先加 `--dry-run`，把将下载的条目报给用户确认。
5. 正式执行。汇总：新增、已存在、待生成、失败，以及文件路径和大小。
6. 回放尚未生成（pending）不是失败：过几小时或次日重跑即可，`sync` 的重叠窗口会自动重试。

## 脚本命令一览

| 命令 | 作用 |
|------|------|
| `check` | 验证 token；失效时通知并写 `NEED_LOGIN.txt` |
| `list [--course] [--date] [--search]` | 课程 / 某天回放列表 |
| `download --course ... [--date] [--section] [--course-id]` | 下载指定回放 |
| `download --targets file.json` | 批量清单 |
| `download/sync --dry-run` | 只显示匹配结果和媒体地址 |
| `--force` | 覆盖已存在文件 |
| `--workers N` | 切片并发，默认 8 |
| `sync [--since] [--until] [--overlap N] [--exclude 关键词]` | 按日期增量缓存 |
| `probe --course ... [--date]` | 抓原始 JSON 到 `scripts/probe/`，排障用 |

`sync` 首次运行以当天为 baseline；之后只回看最近 `overlap_days` 天，并跳过状态文件里已完成的 `sub_id`；文件被删会自动补下。

## 实现原理（需要改脚本时读）

### 1. 登录 token

```bash
adb -s emulator-5554 shell sqlite3 \
  /data/data/com.pku.studious/databases/DBFactory.db \
  "SELECT _token,_cookieToken FROM UserInfo ORDER BY id LIMIT 1;"
```

- 需要 adb root（脚本会先 `adb root`）。
- 有本地缓存 `.pku_tokens.json` 时也会作为候选，模拟器离线时可兜底。
- 续期：`GET https://resource.pku.edu.cn/app/index.php`，参数 `r=auth/re-token&token=<_token>`，同样带签名；返回新的 `_token` / `_cookieToken` 并写回缓存。
- 所有课程 API 的请求头带 `token: <_token>`，媒体请求带 `Cookie: _token=<_cookieToken>`。

### 2. 请求签名

- Host：`https://yjapise.pku.edu.cn/courseapi/`
- 公共参数：`tenantid` 与 `tenantId` 均为 `d438cafb4c34b3b49b3a523bbef50203`，`api_version=3.12.0`，`app_id=50`，`client=android`
- 签名：所有参数（含公共参数，丢掉空值）按 key 排序，拼接 `key+value`，末尾追加盐 `8225ca5fa4d56d6fde540db6024fba39`，取 MD5 作为 `sign`
- UA：`okhttp/4.9.0`

### 3. 关键接口

| 用途 | 路径 | 关键参数 / 返回 |
|------|------|----------------|
| 我的课程 | `v3/course/get-use-member` | `page`、`search`；`data.lists[].course_id/course_name` |
| 按日期查回放 | `v3/course/get-use-member-sub` | `course_time=YYYY-MM-DD`、`page`；`lists[].sub_id/course_id/course_name/sub_title/sub_status` |
| 课程回放列表 | `v3/course/get-course-detail` | `course_id`；`data.sub_list[]` |
| 回放详情 | `v3/course/get-course-sub-detail` | `course_id`、`sub_id`；`sub_content.save_playback.contents` 或 `videoArr[].path` |

- `sub_status=2` 表示回放还没生成；可播放的实测为 `6`。
- 详情里没有播放地址 → `PlaybackNotReady`，按“待生成”处理，不要记为失败。
- 部分课程不在“我的课程”列表里，但能通过按日期接口查到；`list --course` 找不到时改用 `list --date`。

### 4. 媒体下载与解密

- 地址可能是 m3u8（HLS）或直链，按 URL 里是否含 `.m3u8` 判断。
- master playlist 选 `BANDWIDTH` 最高的一路；`#EXT-X-MAP` 初始化段要拼在最前。
- `#EXT-X-KEY:METHOD=AES-128`：key 单独下载（必须 16 字节）；IV 优先用 `IV` 属性，否则用切片序号（`EXT-X-MEDIA-SEQUENCE` 起算）的大端 16 字节。
- CBC 解密后按顺序拼接为 `merged.ts`，再用 ffmpeg `-c copy -movflags +faststart` 封装 MP4。封装失败时保留 `.ts`。
- 直链直接下载，`.mp4/.m4v` 直接移动，其它格式走 ffmpeg。

### 5. 命名与去重

- `课程名 - 标题.mp4`，标题取详情里的 `title`（如 `2026-10-20第3-4节`），非法字符替换为 `_`。
- 目标文件存在且非空就跳过（`--force` 覆盖）。
- `sync` 以 `sub_id` 记录在 `.replay_sync_state.json`；记录在但文件没了会重下。

## App 内下载的源文件转 MP4

适合用户在 App 里手动下载后自动转换；转换成功后默认删除 App 内源文件。

```bash
cd "<SKILL>\scripts"
python sync_haoxue_videos.py --once          # 扫描 AndroidAria.db，转换完成的下载
python sync_haoxue_videos.py --clean-remote  # 清理已转换课程的 m3u8 + 切片目录
python sync_haoxue_videos.py                 # 常驻监听（一般不用，建议用计划任务）
```

- 源目录：`/sdcard/Android/data/com.pku.studious/files/Video/Download`
- 每个下载 = `<hash>.m3u8` + 隐藏切片目录 `.<hash>.m3u8_0`；key 文件很小，保留不删
- 转换进度/去重在 `<target_dir>\.sync_history.json`；失败按 10 分钟起指数退避，最长 24 小时
- 切片密文长度不是 16 的倍数时，丢弃末尾不足一块的字节继续，避免整个视频卡死
- **不删** `AndroidAria.db` 里的下载记录；App 里仍会显示“已下载”，但离线播放会失败，需要用户在 App 内自行删除

## 定时任务

`install.ps1 -CreateTasks` 会注册：

| 任务 | 命令 | 频率 |
|------|------|------|
| `PKU Replay Sync` | `pythonw.exe pku_replay_cache.py sync` | 每天 00:00 / 06:00 / 12:00 / 18:00 |
| `PKU Video Convert` | `pythonw.exe sync_haoxue_videos.py --once` | 每 10 分钟 |

两者都设置为错过补跑、不并发、仅当前用户登录时运行。查看状态：

```powershell
Get-ScheduledTask -TaskName 'PKU Replay Sync','PKU Video Convert' | Get-ScheduledTaskInfo
```

## 排障

| 现象 | 处理 |
|------|------|
| `check` 退出码 2 / `NEED_LOGIN.txt` | 用户在模拟器里登录好学，再重跑；不要索要密码 |
| 没有 token / UserInfo 为空 | 模拟器没登录，或 adb 无 root；先 `adb root` |
| 回放地址为空 / `sub_status=2` | 回放未生成，几小时后重试，不要算失败 |
| 切片 401/403 | token 或 cookie 失效，先 `check` |
| “没有找到课程” | 换 `list --date`；课程可能不在“我的课程”列表 |
| 同名课程多条 | 加 `--course-id`，先用 `list --date` 看 ID |
| 模拟器离线 | 启动 MuMu；脚本会尝试 `adb connect 127.0.0.1:16384` |
| ffmpeg 缺失 | `pip install imageio-ffmpeg`，或把 ffmpeg 放进 PATH |
| App 下载转换失败反复重试 | 看 `sync.log`；确认切片目录完整，失败会自动退避 |
| 磁盘空间不足 | 清理 App 内源文件（`--clean-remote`）或旧 MP4；转换临时目录在 `%TEMP%` |

## 安全边界

- 仅用于用户本人已登录账号有权访问的课程内容。
- token / cookie 只存在本机缓存文件，权限收紧到当前用户；不写入日志、不发送到任何第三方。
- 不修改 App 数据库；不删除下载记录；删除源文件只做用户明确同意的范围。
- 自动化只做下载、转换与本地清理，不做分享、上传或再分发。
