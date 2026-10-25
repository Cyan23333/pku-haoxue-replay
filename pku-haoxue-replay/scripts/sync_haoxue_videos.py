#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
PKU Haoxue (北大好学) Video Sync & Auto-Exporter
Author: Antigravity Pairing Assistant
Description:
    Monitors MuMu Player 12 for completed downloads from '北大好学' (com.pku.studious),
    decrypts AES-128 HLS streams using local keys, and exports them as standard .mp4
    videos into the configured target directory.
"""

import os
import sys
import time
import json
import re
import urllib.parse
import subprocess
import shutil
import tempfile
from pathlib import Path
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# --- Windows No-Window Flags & StartupInfo ---
WIN_FLAGS = 0
WIN_STARTUPINFO = None
if sys.platform == "win32":
    WIN_FLAGS = subprocess.CREATE_NO_WINDOW
    WIN_STARTUPINFO = subprocess.STARTUPINFO()
    WIN_STARTUPINFO.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    WIN_STARTUPINFO.wShowWindow = subprocess.SW_HIDE

# --- Configuration ---
SCRIPT_DIR = Path(__file__).resolve().parent


def _load_config() -> dict:
    """读取 haoxue_config.json；查找顺序：环境变量 → scripts/ → skill 根目录。"""
    candidates = []
    env_path = os.environ.get("HAOXUE_CONFIG")
    if env_path:
        if not Path(env_path).is_file():
            raise RuntimeError(f"HAOXUE_CONFIG 指向的配置文件不存在：{env_path}")
        candidates.append(Path(env_path))
    candidates.append(SCRIPT_DIR / "haoxue_config.json")
    candidates.append(SCRIPT_DIR.parent / "haoxue_config.json")
    for path in candidates:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"配置文件无法解析：{path}（{exc}）") from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"配置文件顶层必须是 JSON 对象：{path}")
        return data
    return {}


CONFIG = _load_config()


def _config_text(key: str, default: str) -> str:
    value = CONFIG.get(key)
    return str(value).strip() if value is not None and str(value).strip() else default


TARGET_DIR = str(
    os.environ.get("HAOXUE_TARGET_DIR")
    or _config_text("target_dir", str(Path.home() / "Videos" / "haoxue"))
)
HISTORY_FILE = os.path.join(TARGET_DIR, ".sync_history.json")
LOG_FILE = os.path.join(TARGET_DIR, "sync.log")
PACKAGE_NAME = "com.pku.studious"
REMOTE_DOWNLOAD_DIR = f"/sdcard/Android/data/{PACKAGE_NAME}/files/Video/Download"
REMOTE_DB_PATH = f"/data/data/{PACKAGE_NAME}/databases/AndroidAria.db"

ADB_SERIAL = os.environ.get("ANDROID_SERIAL") or _config_text("adb_serial", "emulator-5554")
os.environ.setdefault("ANDROID_SERIAL", ADB_SERIAL)

FAILURE_BACKOFF_BASE = 10 * 60
FAILURE_BACKOFF_MAX = 24 * 60 * 60

# --- Output Redirection & Encoding Safety ---
os.makedirs(TARGET_DIR, exist_ok=True)

class SafeLogger:
    def __init__(self, log_path):
        self.terminal = sys.stdout
        try:
            self.log_file = open(log_path, "a", encoding="utf-8", buffering=1)
        except Exception:
            self.log_file = None

    def write(self, message):
        if self.terminal:
            try:
                self.terminal.write(message)
                self.terminal.flush()
            except Exception:
                pass
        if self.log_file:
            try:
                self.log_file.write(message)
                self.log_file.flush()
            except Exception:
                pass

    def flush(self):
        if self.terminal:
            try:
                self.terminal.flush()
            except Exception:
                pass
        if self.log_file:
            try:
                self.log_file.flush()
            except Exception:
                pass

sys.stdout = SafeLogger(LOG_FILE)
sys.stderr = sys.stdout

def log_print(msg):
    ts = time.strftime("[%Y-%m-%d %H:%M:%S]")
    print(f"{ts} {msg}")


# MuMu ADB Candidates
MUMU_ADB_CANDIDATES = []
if _config_text("adb_path", ""):
    MUMU_ADB_CANDIDATES.append(_config_text("adb_path", ""))
MUMU_ADB_CANDIDATES += [
    r"D:\Program Files\Netease\MuMu\nx_device\15.0\shell\adb.exe",
    r"D:\Program Files\Netease\MuMu\nx_main\adb.exe",
    "adb"
]

def find_adb():
    for candidate in MUMU_ADB_CANDIDATES:
        if os.path.isabs(candidate) and os.path.exists(candidate):
            return candidate
        elif not os.path.isabs(candidate) and shutil.which(candidate):
            return candidate
    raise FileNotFoundError("未找到可用的 adb 工具，请检查 MuMu 模拟器安装路径。")

def find_ffmpeg():
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if os.path.exists(exe):
            return exe
    except Exception:
        pass
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    raise FileNotFoundError("未找到 ffmpeg 可执行文件。")

ADB_EXE = find_adb()
FFMPEG_EXE = find_ffmpeg()

def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/*?:"<>|]', '_', name)
    return re.sub(r'\s+', ' ', cleaned).strip()

def run_cmd(args, check=True, capture_output=True, text=True):
    return subprocess.run(
        args,
        check=check,
        capture_output=capture_output,
        text=text,
        encoding="utf-8",
        errors="ignore",
        creationflags=WIN_FLAGS,
        startupinfo=WIN_STARTUPINFO
    )

def is_mumu_online() -> bool:
    try:
        res = run_cmd([ADB_EXE, "devices"])
        for line in res.stdout.splitlines()[1:]:
            parts = line.strip().split()
            if len(parts) >= 2 and parts[1] == "device":
                return True
        run_cmd([ADB_EXE, "connect", "127.0.0.1:16384"], check=False)
        res = run_cmd([ADB_EXE, "devices"])
        for line in res.stdout.splitlines()[1:]:
            parts = line.strip().split()
            if len(parts) >= 2 and parts[1] == "device":
                return True
    except Exception:
        pass
    return False

def ensure_adb_root():
    try:
        run_cmd([ADB_EXE, "root"], check=False)
    except Exception:
        pass

def load_history() -> dict:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_history(history: dict):
    os.makedirs(TARGET_DIR, exist_ok=True)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

def should_process(history: dict, file_name: str) -> bool:
    record = history.get(file_name)
    if not record:
        return True
    output_path = record.get("output_path")
    if (
        output_path
        and os.path.exists(output_path)
        and os.path.getsize(output_path) > 10 * 1024 * 1024
    ):
        return False
    if record.get("status") == "failed":
        return time.time() >= float(record.get("retry_after") or 0)
    return True

def record_failure(history: dict, file_name: str, output_path: str, error: str):
    previous = history.get(file_name) or {}
    attempts = int(previous.get("attempts") or 0) + 1
    delay = min(FAILURE_BACKOFF_BASE * (2 ** (attempts - 1)), FAILURE_BACKOFF_MAX)
    history[file_name] = {
        "status": "failed",
        "output_path": output_path,
        "attempts": attempts,
        "last_error": str(error),
        "last_attempt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "retry_after": time.time() + delay,
    }
    save_history(history)
    log_print(f"[!] 已记录失败（第 {attempts} 次），{delay / 60:.0f} 分钟后允许重试。")

def cleanup_remote_source(file_name: str) -> bool:
    """删除 App 内对应的 m3u8 播放列表和切片目录（保留体积很小的密钥文件）。"""
    if not re.fullmatch(r"[A-Za-z0-9._-]+\.m3u8", file_name):
        log_print(f"[!] 文件名异常，跳过清理: {file_name}")
        return False

    remote_m3u8 = f"{REMOTE_DOWNLOAD_DIR}/{file_name}"
    remote_ts_dir = f"{REMOTE_DOWNLOAD_DIR}/.{file_name}_0"
    try:
        sizes = run_cmd(
            [ADB_EXE, "shell", "du", "-sk", remote_m3u8, remote_ts_dir],
            check=False,
        )
        total_kb = 0
        for line in (sizes.stdout or "").splitlines():
            parts = line.split()
            if parts and parts[0].isdigit():
                total_kb += int(parts[0])

        res = run_cmd(
            [ADB_EXE, "shell", "rm", "-rf", remote_m3u8, remote_ts_dir],
            check=False,
        )
        if res.returncode != 0:
            log_print(f"[!] 删除源文件失败: {(res.stderr or res.stdout).strip()[:200]}")
            return False
        if total_kb:
            log_print(f"[+] 已删除 App 下载文件，释放约 {total_kb / 1024:.0f} MB: {file_name}")
        else:
            log_print(f"[+] 已清理 App 下载文件: {file_name}")
        return True
    except Exception as e:
        log_print(f"[!] 清理源文件出错: {e}")
        return False

def get_completed_tasks() -> list:
    """Query AndroidAria.db for 100% completed video downloads."""
    sql = "SELECT fileName, downloadPath, url, str, isComplete, percent FROM DownloadEntity;"
    cmd = [ADB_EXE, "shell", f"sqlite3 {REMOTE_DB_PATH} \"{sql}\""]
    try:
        res = run_cmd(cmd)
    except Exception as e:
        log_print(f"[-] 查询数据库出错: {e}")
        return []

    tasks = []
    for line in res.stdout.strip().splitlines():
        if not line or "|" not in line:
            continue
        parts = line.split("|")
        if len(parts) < 6:
            continue
        file_name, download_path, url, str_json, is_complete, percent = parts[:6]
        
        # Check if 100% finished
        if str(percent).strip() != "100" and str(is_complete).strip().lower() != "true":
            continue

        try:
            meta = json.loads(urllib.parse.unquote(str_json))
        except Exception:
            meta = {}

        subject_title = meta.get("subjectTitle") or "未知课程"
        section_title = meta.get("title") or "课堂实录"
        
        tasks.append({
            "file_name": file_name.strip(),
            "download_path": urllib.parse.unquote(download_path.strip()),
            "url": urllib.parse.unquote(url.strip()),
            "subject_title": subject_title.strip(),
            "section_title": section_title.strip(),
            "meta": meta
        })
    return tasks

def process_video_task(task: dict, history: dict) -> bool:
    file_name = task["file_name"]
    subject = sanitize_filename(task["subject_title"])
    title = sanitize_filename(task["section_title"])
    output_name = f"{subject} - {title}.mp4"
    output_path = os.path.join(TARGET_DIR, output_name)

    def fail(message: str) -> bool:
        log_print(f"[-] {message}")
        record_failure(history, file_name, output_path, message)
        return False

    log_print(f"========================================================")
    log_print(f"[+] 发现已下载课程: 《{task['subject_title']}》 - {task['section_title']}")
    log_print(f"[*] 目标输出: {output_path}")

    # Check if target already exists and is non-trivial
    if os.path.exists(output_path) and os.path.getsize(output_path) > 10 * 1024 * 1024:
        log_print(f"[OK] 文件已存在且完整 ({os.path.getsize(output_path) / 1024 / 1024:.2f} MB)，跳过处理。")
        history[file_name] = {
            "output_path": output_path,
            "processed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "file_size": os.path.getsize(output_path)
        }
        save_history(history)
        cleanup_remote_source(file_name)
        return True

    temp_dir = tempfile.mkdtemp(prefix="haoxue_sync_")
    try:
        # 1. 抓取 .m3u8 索引内容
        remote_m3u8 = f"{REMOTE_DOWNLOAD_DIR}/{file_name}"
        m3u8_proc = subprocess.run(
            [ADB_EXE, "exec-out", "cat", remote_m3u8],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            creationflags=WIN_FLAGS,
            startupinfo=WIN_STARTUPINFO
        )
        m3u8_content = m3u8_proc.stdout
        if "#EXTM3U" not in m3u8_content:
            return fail("无法读取 m3u8 索引内容")

        # 2. 解析 Key 文件路径与 TS 切片清单
        key_filename = None
        for line in m3u8_content.splitlines():
            line = line.strip()
            if line.startswith("#EXT-X-KEY:") and "URI=" in line:
                m = re.search(r'URI="([^"]+)"', line)
                if m:
                    key_filename = os.path.basename(m.group(1))
                    break

        if not key_filename:
            log_print("[-] 未在 m3u8 中找到 AES 密钥 URI，尝试非加密。")
            key_data = None
        else:
            remote_key = f"{REMOTE_DOWNLOAD_DIR}/{key_filename}"
            key_proc = subprocess.run(
                [ADB_EXE, "exec-out", "cat", remote_key],
                capture_output=True,
                creationflags=WIN_FLAGS,
                startupinfo=WIN_STARTUPINFO
            )
            key_data = key_proc.stdout
            if len(key_data) != 16:
                return fail(f"密钥长度异常 ({len(key_data)} bytes)，期望 16 bytes")
            log_print(f"[*] 成功获取 AES-128 解密密钥: {key_data.hex()}")

        # 提取切片文件名列表 (按照 m3u8 中的严格顺序)
        ts_order = []
        for line in m3u8_content.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and line.endswith(".ts"):
                ts_order.append(os.path.basename(line))

        log_print(f"[*] 播放列表包含 {len(ts_order)} 个视频切片。")
        if not ts_order:
            return fail("未发现 TS 切片清单")

        # 3. 从模拟器批量抽取切片文件夹
        remote_ts_dir = f"{REMOTE_DOWNLOAD_DIR}/.{file_name}_0"
        log_print(f"[*] 正在高速提取切片数据流 ({remote_ts_dir})...")

        t0 = time.time()
        p_tar_in = subprocess.Popen(
            [ADB_EXE, "exec-out", "tar", "-C", remote_ts_dir, "-cf", "-", "."],
            stdout=subprocess.PIPE,
            creationflags=WIN_FLAGS,
            startupinfo=WIN_STARTUPINFO
        )
        p_tar_out = subprocess.Popen(
            ["tar", "-xf", "-", "-C", temp_dir],
            stdin=p_tar_in.stdout,
            creationflags=WIN_FLAGS,
            startupinfo=WIN_STARTUPINFO
        )
        p_tar_in.stdout.close()
        p_tar_out.communicate()
        t_tar = time.time() - t0
        log_print(f"[+] 切片提取完毕，耗时 {t_tar:.2f}s。")

        # 4. 解密并拼接为单个 TS 流
        merged_ts_path = os.path.join(temp_dir, "merged.ts")
        log_print(f"[*] 正在执行 AES-128-CBC 解密与分片拼接...")
        t0 = time.time()
        
        bad_segments = 0
        with open(merged_ts_path, "wb") as out_ts:
            for seq_num, ts_name in enumerate(ts_order):
                seg_path = os.path.join(temp_dir, ts_name)
                if not os.path.exists(seg_path):
                    seg_path = os.path.join(temp_dir, os.path.basename(ts_name))
                if not os.path.exists(seg_path):
                    bad_segments += 1
                    log_print(f"[!] 缺少切片 {ts_name}，跳过。")
                    continue

                try:
                    with open(seg_path, "rb") as seg_f:
                        enc_data = seg_f.read()

                    if key_data:
                        usable = len(enc_data) - len(enc_data) % 16
                        if usable != len(enc_data):
                            bad_segments += 1
                            log_print(
                                f"[!] 切片 {ts_name} 长度 {len(enc_data)} 不是 16 的倍数，"
                                f"丢弃末尾 {len(enc_data) - usable} 字节后继续。"
                            )
                        if usable == 0:
                            log_print(f"[!] 切片 {ts_name} 没有完整加密块，跳过。")
                            continue
                        iv = seq_num.to_bytes(16, byteorder="big")
                        cipher = Cipher(algorithms.AES(key_data), modes.CBC(iv), backend=default_backend())
                        decryptor = cipher.decryptor()
                        dec_data = decryptor.update(enc_data[:usable]) + decryptor.finalize()
                        out_ts.write(dec_data)
                    else:
                        out_ts.write(enc_data)
                except Exception as seg_exc:
                    bad_segments += 1
                    log_print(f"[!] 切片 {ts_name} 处理失败，跳过：{seg_exc}")
                    continue

        if bad_segments:
            log_print(f"[!] 共有 {bad_segments} 个切片异常，其余内容已保留。")

        t_dec = time.time() - t0
        ts_size_mb = os.path.getsize(merged_ts_path) / 1024 / 1024
        log_print(f"[+] 解密拼接完成，完整视频流大小: {ts_size_mb:.2f} MB (耗时 {t_dec:.2f}s)")

        # 5. FFmpeg 无损封装为标准 MP4
        log_print(f"[*] 正在调用 FFmpeg 无损封装为 MP4...")
        t0 = time.time()
        ffmpeg_cmd = [
            FFMPEG_EXE,
            "-y",
            "-i", merged_ts_path,
            "-c", "copy",
            "-movflags", "+faststart",
            output_path
        ]
        res = subprocess.run(
            ffmpeg_cmd,
            capture_output=True,
            text=True,
            errors="ignore",
            creationflags=WIN_FLAGS,
            startupinfo=WIN_STARTUPINFO
        )
        if res.returncode != 0:
            log_print(f"[-] FFmpeg 转码报错，回退保留为 TS 格式。")
            shutil.copy2(merged_ts_path, output_path.replace(".mp4", ".ts"))
        else:
            t_ff = time.time() - t0
            log_print(f"[OK] 封装完成！耗时 {t_ff:.2f}s，已输出到: {output_path}")

        # 6. 更新同步记录
        history[file_name] = {
            "output_path": output_path,
            "processed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "file_size": os.path.getsize(output_path) if os.path.exists(output_path) else 0
        }
        save_history(history)
        cleanup_remote_source(file_name)
        return True

    except Exception as e:
        log_print(f"[-] 处理课程《{task['subject_title']}》发生异常: {e}")
        import traceback
        traceback.print_exc()
        record_failure(history, file_name, output_path, f"{type(e).__name__}: {e}")
        return False
    finally:
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass

def sync_once():
    """扫描并同步一次"""
    log_print(f"[*] 正在检查 MuMu 12 模拟器在线状态...")
    if not is_mumu_online():
        log_print("[-] MuMu 模拟器未启动或离线。")
        return 0

    ensure_adb_root()
    history = load_history()
    tasks = get_completed_tasks()
    log_print(f"[*] 数据库中共有 {len(tasks)} 条已 100% 下载完毕的课程记录。")

    processed_count = 0
    for task in tasks:
        file_name = task["file_name"]
        if not should_process(history, file_name):
            continue

        ok = process_video_task(task, history)
        if ok:
            processed_count += 1

    log_print(f"[*] 本次同步完成，新增处理 {processed_count} 门课程。")
    return processed_count

def clean_converted_sources():
    """清理已转换完成课程的 App 下载源文件。"""
    log_print("[*] 清理已转换课程的 App 下载源文件...")
    if not is_mumu_online():
        log_print("[-] MuMu 模拟器未启动或离线。")
        return 0

    ensure_adb_root()
    history = load_history()
    cleaned = 0
    for file_name, record in history.items():
        if record.get("status") == "failed":
            continue
        output_path = record.get("output_path")
        if (
            not output_path
            or not os.path.exists(output_path)
            or os.path.getsize(output_path) <= 10 * 1024 * 1024
        ):
            log_print(f"[!] 本地文件缺失或过小，保留 App 源文件: {file_name}")
            continue
        if cleanup_remote_source(file_name):
            cleaned += 1

    log_print(f"[*] 清理完成，共处理 {cleaned} 个源文件。")
    return cleaned

def watch_loop(poll_interval=20):
    """常驻后台轮询监控"""
    log_print(f"""
===================================================================
   北大好学 (MuMu 12) 课程视频自动同步监控服务已启动
   监控目录: 模拟器内 {REMOTE_DOWNLOAD_DIR}
   输出目录: {TARGET_DIR}
   轮询间隔: {poll_interval} 秒
===================================================================
    """)
    last_online_state = None
    while True:
        try:
            online = is_mumu_online()
            if online != last_online_state:
                last_online_state = online
                if online:
                    log_print("[+] 检测到 MuMu 12 模拟器已连接！")
                    # 仅在刚连接上线时执行一次 adb root
                    ensure_adb_root()
                else:
                    log_print("[-] MuMu 12 模拟器离线，持续后台静默守候...")

            if online:
                history = load_history()
                tasks = get_completed_tasks()
                for task in tasks:
                    file_name = task["file_name"]
                    if not should_process(history, file_name):
                        continue
                    process_video_task(task, history)
        except KeyboardInterrupt:
            log_print("[*] 监控服务已退出。")
            break
        except Exception as e:
            log_print(f"[-] 轮询周期出现异常: {e}")

        time.sleep(poll_interval)

if __name__ == "__main__":
    os.makedirs(TARGET_DIR, exist_ok=True)
    if "--clean-remote" in sys.argv:
        clean_converted_sources()
    elif "--once" in sys.argv:
        sync_once()
    else:
        poll_sec = 20
        for arg in sys.argv[1:]:
            if arg.isdigit():
                poll_sec = int(arg)
        watch_loop(poll_sec)
