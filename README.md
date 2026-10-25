# 北大好学回放下载器

Windows 上批量下载“北大好学”课程回放的脚本与 AI skill。复用 MuMu 模拟器中已登录账号的 token，直接调用课程 API 获取 m3u8，AES-128 解密后封装为 MP4。

## 特性

- 按课程名、日期、节次下载回放
- 按日期增量缓存每天新增的回放
- 把 App 内已下载的 m3u8 自动转成 MP4，并可清理源文件
- 自带 `pku-haoxue-replay` skill 和给 AI agent 的安装说明

## 快速开始

环境：Windows 10/11、[MuMu 模拟器](https://mumu.163.com/) 12、Python 3.10+，并在 MuMu 中安装、登录“北大好学”。

```powershell
# 1. 进入仓库里的 pku-haoxue-replay 目录
cd pku-haoxue-replay

# 2. 安装（自动装依赖、探测 adb、写配置、部署 skill）
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -TargetDir "D:\Videos\haoxue"

# 3. 登录自检
python "$env:USERPROFILE\.qoder-cn\skills\pku-haoxue-replay\scripts\pku_replay_cache.py" check
```

之后：

```powershell
cd "$env:USERPROFILE\.qoder-cn\skills\pku-haoxue-replay\scripts"

python pku_replay_cache.py list --date 2026-10-20
python pku_replay_cache.py download --course 概率论 --date 2026-10-20
python pku_replay_cache.py sync --since 2026-09-01
```

- 日常使用：[操作指南.md](pku-haoxue-replay/操作指南.md)
- 让 AI 帮你装：[AGENT_SETUP.md](pku-haoxue-replay/AGENT_SETUP.md)
- Skill 说明：[SKILL.md](pku-haoxue-replay/SKILL.md)

## 说明

- 需要模拟器具备 adb root；仅支持用户本人账号有权访问的课程内容。
- 仓库不包含任何账号、token、课程视频或运行日志。
- 登录必须由用户在模拟器内手动完成，本项目不处理密码。
- 请勿分享、上传或再分发下载的课程视频；使用前请确认符合学校与课程平台的使用条款。
