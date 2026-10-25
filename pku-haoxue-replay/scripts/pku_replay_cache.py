#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
北大好学 · 回放自动缓存（电脑直连版）

不从模拟器里“下载”，而是复用 App 已登录的 token，直接调用课程 API 取回放地址，
在电脑上下载 HLS、AES-128 解密、ffmpeg 封装为 mp4，输出到 haoxue_config.json 里的 target_dir。

常用命令：
    python pku_replay_cache.py check
    python pku_replay_cache.py list
    python pku_replay_cache.py list --course 心理学导论
    python pku_replay_cache.py download --course 心理学导论 --date 2026-03-31
    python pku_replay_cache.py download --course 心理学导论 --date 2026-03-31 --section 第10-11节
    python pku_replay_cache.py download --targets targets.json
    python pku_replay_cache.py sync --dry-run
    python pku_replay_cache.py sync

targets.json 示例：
    [
      {"course": "心理学导论", "date": "2026-03-31", "section": "第10-11节"},
      {"course": "概率论", "date": "2026-10-15"}
    ]

每日增量（推荐）：
    sync 按“日期”入口扫描，首次以当天为 baseline，此后每次回看最近 3 天；
    自动排除 普通物理 / 马克思主义基本原理 / 无极球 / 经济学原理讨论课；
    以 sub_id 去重，文件缺失会补下，回放未生成会在后续运行中重试。
    状态保存在 .replay_sync_state.json。

token 失效时：
    - 控制台与 replay_cache.log 明确报错；
    - 尝试 Windows 通知（--no-notify 可关闭）；
    - 在输出目录写 NEED_LOGIN.txt；
    - 退出码 2。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ---------------------------------------------------------------- config ---

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


def _config_int(key: str, default: int) -> int:
    try:
        return int(CONFIG.get(key) or default)
    except (TypeError, ValueError):
        return default


def _config_excludes() -> tuple[tuple[str, ...], ...]:
    raw = CONFIG.get("excludes")
    if not isinstance(raw, list):
        return ()
    rules: list[tuple[str, ...]] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            rules.append((item.strip(),))
        elif isinstance(item, (list, tuple)):
            parts = tuple(str(x).strip() for x in item if str(x).strip())
            if parts:
                rules.append(parts)
    return tuple(rules)


ADB_CANDIDATES: list[str] = []
if CONFIG.get("adb_path"):
    ADB_CANDIDATES.append(str(CONFIG["adb_path"]))
ADB_CANDIDATES += [
    r"D:\Program Files\Netease\MuMu\nx_device\15.0\shell\adb.exe",
    r"D:\Program Files\Netease\MuMu\nx_main\adb.exe",
    "adb",
]
SERIAL = os.environ.get("ADB_SERIAL") or _config_text("adb_serial", "emulator-5554")
PACKAGE = "com.pku.studious"
REMOTE_DB = f"/data/data/{PACKAGE}/databases/DBFactory.db"

BASE_URL = "https://resource.pku.edu.cn/app/"
COURSE_HOST = "https://yjapise.pku.edu.cn/courseapi/"
SIGN_SALT = "8225ca5fa4d56d6fde540db6024fba39"
COMMON_PARAMS = {
    "tenantid": "d438cafb4c34b3b49b3a523bbef50203",
    "tenantId": "d438cafb4c34b3b49b3a523bbef50203",
    "api_version": "3.12.0",
    "app_id": "50",
    "client": "android",
}
UA = "okhttp/4.9.0"
CST = timezone(timedelta(hours=8))

TOKEN_CACHE = SCRIPT_DIR / ".pku_tokens.json"
SYNC_STATE = SCRIPT_DIR / ".replay_sync_state.json"
LOG_FILE = SCRIPT_DIR / "replay_cache.log"
TARGET_DIR = Path(
    os.environ.get("HAOXUE_TARGET_DIR")
    or _config_text("target_dir", str(Path.home() / "Videos" / "haoxue"))
)
NEED_LOGIN_FLAG = TARGET_DIR / "NEED_LOGIN.txt"

DEFAULT_WORKERS = _config_int("workers", 8)
DEFAULT_EXCLUDES = _config_excludes()
SYNC_OVERLAP_DAYS = _config_int("overlap_days", 3)
WIN_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# --------------------------------------------------------------- logging ---

_log_lock = threading.Lock()


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    with _log_lock:
        print(line, flush=True)
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


# --------------------------------------------------------------- errors ---


class AuthExpired(Exception):
    pass


class PlaybackNotReady(Exception):
    pass


class ApiError(Exception):
    def __init__(self, err, message):
        super().__init__(f"API 错误 {err}: {message}")
        self.err = err
        self.message = message


# ----------------------------------------------------------------- adb ---


def find_adb() -> str:
    for cand in ADB_CANDIDATES:
        if os.path.isabs(cand):
            if os.path.exists(cand):
                return cand
        elif shutil.which(cand):
            return cand
    raise FileNotFoundError("未找到 adb，请确认 MuMu 模拟器路径或设置 ADB 环境变量")


def adb(*args: str, timeout: int = 30) -> str:
    exe = find_adb()
    proc = subprocess.run(
        [exe, "-s", SERIAL, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=WIN_FLAGS,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "adb 执行失败").strip())
    return proc.stdout


def read_db_tokens() -> tuple[str, str] | None:
    """读取 App 本地 UserInfo 表里的 _token / _cookieToken。"""
    query = "SELECT _token,_cookieToken FROM UserInfo ORDER BY id LIMIT 1;"
    out = adb("shell", f"sqlite3 -separator '|' {REMOTE_DB} \"{query}\"").strip()
    if not out:
        return None
    first = out.splitlines()[0]
    if "|" not in first:
        return None
    token, cookie = first.split("|", 1)
    token, cookie = token.strip(), cookie.strip()
    if not token or not cookie:
        return None
    return token, cookie


# ---------------------------------------------------------------- tokens ---


@dataclass
class Tokens:
    token: str
    cookie: str
    source: str = "db"


def load_cached_tokens() -> Tokens | None:
    try:
        data = json.loads(TOKEN_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    token = str(data.get("_token") or "").strip()
    cookie = str(data.get("_cookieToken") or "").strip()
    if not token or not cookie:
        return None
    return Tokens(token, cookie, "cache")


def save_cached_tokens(tokens: Tokens) -> None:
    payload = {
        "_token": tokens.token,
        "_cookieToken": tokens.cookie,
        "updated_at": datetime.now(CST).isoformat(timespec="seconds"),
    }
    TOKEN_CACHE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if sys.platform == "win32":
        user = os.environ.get("USERNAME", "")
        if user:
            subprocess.run(
                ["icacls", str(TOKEN_CACHE), "/inheritance:r", "/grant:r", f"{user}:F"],
                capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )


def token_candidates() -> list[Tokens]:
    out: list[Tokens] = []
    try:
        pair = read_db_tokens()
        if pair:
            out.append(Tokens(pair[0], pair[1], "db"))
    except Exception as exc:
        log(f"[!] 读取模拟器数据库失败：{exc}")
    cached = load_cached_tokens()
    if cached and all(t.token != cached.token for t in out):
        out.append(cached)
    return out


def md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def signed_params(extra: dict | None = None) -> dict[str, str]:
    params = {k: str(v) for k, v in COMMON_PARAMS.items()}
    for key, value in (extra or {}).items():
        if value is None or str(value) == "":
            continue  # App 默认丢弃空值参数
        params[key] = str(value)
    concat = "".join(key + params[key] for key in sorted(params))
    params["sign"] = md5_hex(concat + SIGN_SALT)
    return params


_tls = threading.local()


def http_session() -> requests.Session:
    sess = getattr(_tls, "session", None)
    if sess is None:
        sess = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=16, pool_maxsize=32)
        sess.mount("https://", adapter)
        sess.mount("http://", adapter)
        _tls.session = sess
    return sess


def http_bytes(
    url: str,
    headers: dict | None = None,
    *,
    retries: int = 3,
    timeout: int = 60,
    auth_sensitive: bool = False,
) -> bytes:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            resp = http_session().get(url, headers=headers, timeout=timeout)
            if resp.status_code in (401, 403) and auth_sensitive:
                raise AuthExpired(f"资源鉴权失败（HTTP {resp.status_code}）")
            resp.raise_for_status()
            return resp.content
        except AuthExpired:
            raise
        except Exception as exc:
            last = exc
            if attempt + 1 < retries:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"下载失败：{url}（{last}）")


def api_request(
    path: str,
    params: dict | None = None,
    *,
    tokens: Tokens,
    host: str = COURSE_HOST,
    retries: int = 2,
) -> dict:
    url = host + path.lstrip("/")
    headers = {"token": tokens.token, "User-Agent": UA}
    last: Exception | None = None
    for attempt in range(retries):
        try:
            resp = http_session().get(
                url, params=signed_params(params), headers=headers, timeout=30
            )
            resp.raise_for_status()
            payload = resp.json()
            break
        except Exception as exc:
            last = exc
            if attempt + 1 < retries:
                time.sleep(1.5 * (attempt + 1))
    else:
        raise RuntimeError(f"课程 API 请求失败：{url}（{last}）")

    if isinstance(payload, dict) and payload.get("success") is False:
        err = payload.get("err")
        message = payload.get("errMsg") or payload.get("message") or ""
        if str(err) == "401" or "token" in str(message).lower():
            raise AuthExpired(f"登录 token 已失效：{message}")
        raise ApiError(err, message)
    return payload


def api_data(payload: dict) -> dict:
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    if data == [] or data is None:
        return {}
    raise ApiError("data", f"响应 data 结构异常：{type(data).__name__}")


def refresh_tokens(tokens: Tokens) -> Tokens:
    """用 _token 调 auth/re-token 换取新 token。"""
    params = signed_params({"r": "auth/re-token", "token": tokens.token})
    headers = {"token": tokens.token, "User-Agent": UA}
    resp = http_session().get(BASE_URL + "index.php", params=params, headers=headers, timeout=30)
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise AuthExpired(f"token 续期失败：{payload}")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    new_token = str(data.get("_token") or "").strip()
    new_cookie = str(data.get("_cookieToken") or "").strip()
    if not new_token or not new_cookie:
        raise AuthExpired(f"token 续期返回结构异常：{payload}")
    renewed = Tokens(new_token, new_cookie, "refreshed")
    save_cached_tokens(renewed)
    log("[+] token 已自动续期")
    return renewed


def validate_tokens(tokens: Tokens) -> None:
    """用最小请求验证 token 是否可用；失败抛 AuthExpired 或其他异常。"""
    api_request("v3/course/get-use-member", {"page": 1}, tokens=tokens)


def acquire_tokens(*, allow_refresh: bool = True) -> Tokens:
    candidates = token_candidates()
    if not candidates:
        raise AuthExpired("本地没有任何登录 token（UserInfo 表为空）")

    auth_error: Exception | None = None
    network_error: Exception | None = None
    for cand in candidates:
        try:
            validate_tokens(cand)
            cached = load_cached_tokens()
            if (
                cached is None
                or cached.token != cand.token
                or cached.cookie != cand.cookie
            ):
                save_cached_tokens(cand)
            log(f"[+] 登录状态有效（来源：{cand.source}）")
            return cand
        except AuthExpired as exc:
            auth_error = exc
            log(f"[!] token 无效（来源：{cand.source}）：{exc}")
        except Exception as exc:
            network_error = exc

    if network_error is not None and auth_error is None:
        raise network_error

    if allow_refresh:
        for cand in candidates:
            try:
                renewed = refresh_tokens(cand)
                validate_tokens(renewed)
                return renewed
            except AuthExpired as exc:
                log(f"[!] 用 {cand.source} token 续期失败：{exc}")
            except Exception as exc:
                network_error = exc
        if network_error is not None:
            raise network_error

    raise AuthExpired(str(auth_error or "登录已失效"))


# ----------------------------------------------------------- notification ---


def notify_login_required(reason: str) -> None:
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    try:
        NEED_LOGIN_FLAG.write_text(
            f"{stamp}\n登录已失效：{reason}\n请打开模拟器里的北大好学登录一次，然后重新运行。\n",
            encoding="utf-8",
        )
    except OSError:
        pass
    log("[!] 已写入 NEED_LOGIN.txt")

    if os.environ.get("PKU_NO_NOTIFY") == "1":
        return
    title = "北大好学需要重新登录"
    message = "缓存脚本发现登录已失效。请打开 MuMu 模拟器里的好学 App 登录一次，然后重新运行缓存。"
    ps = f"""
$ErrorActionPreference = 'Stop'
$xml = @"
<toast><visual><binding template="ToastGeneric"><text>{title}</text><text>{message}</text></binding></visual></toast>
"@
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$doc = New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml($xml)
$toast = [Windows.UI.Notifications.ToastNotification]::new($doc)
$app = '{{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}}\\WindowsPowerShell\\v1.0\\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show($toast)
"""
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True,
            timeout=20,
            creationflags=WIN_FLAGS,
        )
        log("[!] 已发送 Windows 通知")
    except Exception as exc:
        log(f"[!] Windows 通知发送失败：{exc}")


def clear_login_flag() -> None:
    try:
        NEED_LOGIN_FLAG.unlink(missing_ok=True)
    except OSError:
        pass


# ------------------------------------------------------------------ HLS ---


@dataclass
class KeyInfo:
    method: str
    uri: str | None
    iv: bytes | None


@dataclass
class Segment:
    url: str
    seq: int
    key: KeyInfo | None


def parse_attrs(raw: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for part in re.finditer(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', raw):
        value = part.group(2).strip('"')
        attrs[part.group(1)] = value
    return attrs


def resolve_url(base: str, ref: str) -> str:
    from urllib.parse import urljoin

    return urljoin(base, ref)


def pick_media_playlist(text: str, base: str, headers: dict) -> tuple[str, str]:
    """处理 master playlist，选码率最高的一路。"""
    if "#EXT-X-STREAM-INF" not in text:
        return text, base
    best_url = None
    best_bw = -1
    lines = [ln.strip() for ln in text.splitlines()]
    for idx, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        attrs = parse_attrs(line.split(":", 1)[1])
        try:
            bw = int(attrs.get("BANDWIDTH", "0"))
        except ValueError:
            bw = 0
        for nxt in lines[idx + 1 :]:
            if nxt and not nxt.startswith("#"):
                if bw >= best_bw:
                    best_bw = bw
                    best_url = resolve_url(base, nxt)
                break
    if not best_url:
        raise RuntimeError("master playlist 中没有找到可用码率")
    new_base = best_url
    log(f"[*] master playlist → {best_url}")
    body = http_bytes(best_url, headers, auth_sensitive=True).decode("utf-8", "replace")
    return body, new_base


def parse_media_playlist(text: str, base: str) -> tuple[list[Segment], list[str]]:
    segments: list[Segment] = []
    init_urls: list[str] = []
    key: KeyInfo | None = None
    seq = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            seq = int(line.split(":", 1)[1].strip())
        elif line.startswith("#EXT-X-KEY:"):
            attrs = parse_attrs(line.split(":", 1)[1])
            method = attrs.get("METHOD", "NONE").upper()
            uri = resolve_url(base, attrs["URI"]) if attrs.get("URI") else None
            iv_hex = attrs.get("IV", "").lower().removeprefix("0x")
            iv = bytes.fromhex(iv_hex) if iv_hex else None
            key = None if method == "NONE" else KeyInfo(method, uri, iv)
        elif line.startswith("#EXT-X-MAP:"):
            attrs = parse_attrs(line.split(":", 1)[1])
            if attrs.get("URI"):
                init_urls.append(resolve_url(base, attrs["URI"]))
        elif not line.startswith("#"):
            segments.append(Segment(resolve_url(base, line), seq, key))
            seq += 1
    return segments, init_urls


def decrypt_segment(data: bytes, key: bytes, iv: bytes) -> bytes:
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return decryptor.update(data) + decryptor.finalize()


def download_hls(
    playlist_url: str,
    cookie: str,
    workdir: Path,
    *,
    workers: int = DEFAULT_WORKERS,
) -> Path:
    headers = {"Cookie": f"_token={cookie}", "User-Agent": UA}
    text = http_bytes(playlist_url, headers, auth_sensitive=True).decode("utf-8", "replace")
    text, playlist_url = pick_media_playlist(text, playlist_url, headers)
    segments, init_urls = parse_media_playlist(text, playlist_url)
    if not segments:
        raise RuntimeError("播放列表里没有视频切片")
    log(f"[*] 播放列表包含 {len(segments)} 个切片")

    key_cache: dict[str, bytes] = {}
    for seg in segments:
        if seg.key and seg.key.method == "AES-128":
            assert seg.key.uri
            if seg.key.uri not in key_cache:
                key_data = http_bytes(seg.key.uri, headers, auth_sensitive=True)
                if len(key_data) != 16:
                    raise RuntimeError(f"AES 密钥长度异常：{len(key_data)} 字节")
                key_cache[seg.key.uri] = key_data
    if key_cache:
        log(f"[*] 已获取 {len(key_cache)} 个 AES-128 密钥")

    def fetch(seg: Segment) -> bytes:
        data = http_bytes(seg.url, headers, auth_sensitive=True)
        if seg.key and seg.key.method == "AES-128":
            key = key_cache[str(seg.key.uri)]
            iv = seg.key.iv or seg.seq.to_bytes(16, "big")
            if len(data) % 16:
                raise RuntimeError(
                    f"切片 {seg.seq} 密文长度 {len(data)} 不是 16 的倍数（数据不完整）"
                )
            data = decrypt_segment(data, key, iv)
        return data

    merged = workdir / "merged.ts"
    total = len(segments)
    done = 0
    batch_size = max(workers * 4, 16)
    with open(merged, "wb") as out:
        for init_url in init_urls:
            out.write(http_bytes(init_url, headers, auth_sensitive=True))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for start in range(0, total, batch_size):
                batch = segments[start : start + batch_size]
                futures = [pool.submit(fetch, seg) for seg in batch]
                for fut in futures:
                    out.write(fut.result())
                done += len(batch)
                pct = done * 100 // total
                log(f"    - 切片 {done}/{total}（{pct}%）")
    return merged


# --------------------------------------------------------------- ffmpeg ---


def find_ffmpeg() -> str:
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if os.path.exists(exe):
            return exe
    except Exception:
        pass
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    raise FileNotFoundError("未找到 ffmpeg，请安装 ffmpeg 或 imageio-ffmpeg")


def remux_to_mp4(source: Path, target: Path) -> bool:
    cmd = [
        find_ffmpeg(),
        "-y",
        "-i",
        str(source),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(target),
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=WIN_FLAGS,
    )
    if proc.returncode != 0:
        log(f"[!] ffmpeg 封装失败：{(proc.stderr or '').strip()[:500]}")
        return False
    return True


# -------------------------------------------------------------- naming ---


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/*?:"<>|]', "_", name)
    return re.sub(r"\s+", " ", cleaned).strip()


def session_date(sub: dict) -> str:
    for key in ("class_begin", "start_at", "class_begin_time"):
        raw = str(sub.get(key) or "").strip()
        if raw.isdigit():
            return datetime.fromtimestamp(int(raw), CST).strftime("%Y-%m-%d")
        m = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
        if m:
            return m.group(1)
    return ""


def session_id(sub: dict) -> str:
    return str(sub.get("id") or sub.get("sub_id") or "").strip()


def session_section(sub: dict) -> str:
    return str(sub.get("sub_title") or sub.get("sub_name") or "").strip()


def normalize_name(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def normalize_section(text: str) -> str:
    return re.sub(r"[\s第节]", "", text or "")


# -------------------------------------------------------------- API ops ---


def fetch_courses(tokens: Tokens, search: str = "") -> list[dict]:
    courses: list[dict] = []
    page = 1
    while page <= 100:
        payload = api_request(
            "v3/course/get-use-member",
            {"page": page, "search": search},
            tokens=tokens,
        )
        data = api_data(payload)
        courses.extend(x for x in (data.get("lists") or []) if isinstance(x, dict))
        total = int(data.get("count") or 0)
        page_size = int(data.get("pageSize") or 10)
        if page * page_size >= total or not data.get("lists"):
            break
        page += 1
    return courses


def fetch_sessions_by_date(tokens: Tokens, date: str) -> list[dict]:
    sessions: list[dict] = []
    page = 1
    while page <= 100:
        payload = api_request(
            "v3/course/get-use-member-sub",
            {"course_time": date, "page": page},
            tokens=tokens,
        )
        data = api_data(payload)
        sessions.extend(x for x in (data.get("lists") or []) if isinstance(x, dict))
        total = int(data.get("count") or 0)
        page_size = int(data.get("pageSize") or 10)
        if page * page_size >= total or not data.get("lists"):
            break
        page += 1
    return sessions


def pick_course(courses: Iterable[dict], name: str) -> dict:
    name = name.strip()
    exact = [c for c in courses if str(c.get("course_name", "")).strip() == name]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise RuntimeError(
            "同名课程有多条：" + "，".join(f"{c.get('course_name')}({c.get('course_id')})" for c in exact)
        )
    fuzzy = [c for c in courses if name and name in str(c.get("course_name", ""))]
    if len(fuzzy) == 1:
        return fuzzy[0]
    if not fuzzy:
        raise RuntimeError(f"没有找到课程“{name}”，可用 list 命令查看")
    raise RuntimeError(
        "课程名不唯一，请写全名：" + "，".join(str(c.get("course_name")) for c in fuzzy[:10])
    )


def fetch_sessions(tokens: Tokens, course_id: int) -> list[dict]:
    payload = api_request(
        "v3/course/get-course-detail", {"course_id": course_id}, tokens=tokens
    )
    data = api_data(payload)
    return [x for x in (data.get("sub_list") or []) if isinstance(x, dict) and x.get("id")]


def select_sessions(sessions: list[dict], date: str, section: str) -> list[dict]:
    chosen = []
    for sub in sessions:
        if date and session_date(sub) != date:
            continue
        if section and normalize_section(section) not in normalize_section(session_section(sub)):
            continue
        chosen.append(sub)
    return chosen


def fetch_video_info(tokens: Tokens, course_id: int, sub_id: str) -> dict:
    payload = api_request(
        "v3/course/get-course-sub-detail",
        {"course_id": course_id, "sub_id": sub_id},
        tokens=tokens,
    )
    return api_data(payload)


def pick_media_url(info: dict) -> tuple[str, str]:
    """返回 (url, 类型)；类型为 hls 或 direct。"""
    sub_content = info.get("sub_content") or {}
    videos = info.get("videoArr") or []
    videos = [v for v in videos if isinstance(v, dict) and v.get("path")]
    if videos:
        selected = next((v for v in videos if v.get("select")), None)
        if selected is None:
            selected = max(videos, key=lambda v: int(v.get("file_size") or 0))
        url = str(selected["path"])
    else:
        save_playback = sub_content.get("save_playback") or {}
        url = str(save_playback.get("contents") or "")
        if not url:
            url = str(sub_content.get("download_url") or "")
    if not url:
        raise PlaybackNotReady("接口未返回播放地址，回放可能尚未生成")
    kind = "hls" if ".m3u8" in url.lower() else "direct"
    return url, kind


def download_direct(url: str, cookie: str, target: Path) -> bool:
    headers = {"Cookie": f"_token={cookie}", "User-Agent": UA}
    tmp = target.with_suffix(target.suffix + ".part")
    data = http_bytes(url, headers, retries=3, timeout=120, auth_sensitive=True)
    tmp.write_bytes(data)
    tmp.replace(target)
    return True


# ------------------------------------------------------------- commands ---


def cmd_check(args: argparse.Namespace) -> int:
    try:
        tokens = acquire_tokens()
        validate_tokens(tokens)
        clear_login_flag()
        log("[+] 自检通过：token 可用")
        return 0
    except AuthExpired as exc:
        log(f"[X] {exc}")
        notify_login_required(str(exc))
        return 2
    except Exception as exc:
        log(f"[X] 自检失败（非登录问题）：{exc}")
        return 1


def cmd_list(args: argparse.Namespace) -> int:
    try:
        tokens = acquire_tokens()
        if args.date:
            sessions = fetch_sessions_by_date(tokens, args.date)
            rows = [
                s
                for s in sessions
                if not args.course
                or normalize_name(args.course)
                in normalize_name(str(s.get("course_name") or ""))
            ]
            for sub in rows:
                print(
                    f"{sub.get('course_id')}\t{sub.get('course_name')}\t"
                    f"{session_section(sub)}\tsub_id={session_id(sub)}"
                )
            if not rows:
                log(f"{args.date} 没有匹配的课程/回放")
            return 0

        courses = fetch_courses(tokens, args.search or "")
        if args.course:
            wanted = normalize_name(args.course)
            courses = [
                c
                for c in courses
                if normalize_name(str(c.get("course_name") or "")) == wanted
            ]
            if not courses:
                log(
                    f"“{args.course}”不在按课程列表中；"
                    f"请用 list --date YYYY-MM-DD 或 download --course ... --date ... 查询"
                )
                return 1
        if not courses:
            log("没有查询到课程")
            return 0
        for course in courses:
            cid = course.get("course_id")
            name = course.get("course_name") or ""
            teacher = course.get("course_teacher") or ""
            term = course.get("term") or course.get("course_term") or ""
            print(f"{cid}\t{name}\t{teacher}\t{term}")
            if args.course:
                for sub in fetch_sessions(tokens, int(cid)):
                    print(
                        f"    {session_id(sub)}\t{session_date(sub)}\t"
                        f"{session_section(sub)}\t状态={sub.get('playback_status')}"
                    )
        return 0
    except AuthExpired as exc:
        log(f"[X] {exc}")
        notify_login_required(str(exc))
        return 2
    except Exception as exc:
        log(f"[X] 查询失败：{exc}")
        return 1


def load_targets(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise RuntimeError("targets 文件必须是 JSON 数组")
    out = []
    for idx, item in enumerate(data):
        if not isinstance(item, dict) or not item.get("course"):
            raise RuntimeError(f"targets 第 {idx + 1} 项缺少 course 字段")
        out.append(item)
    return out


def resolve_sessions(
    tokens: Tokens,
    target: dict,
    *,
    courses: list[dict],
    course_cache: dict[int, list[dict]],
    date_cache: dict[str, list[dict]],
) -> tuple[str, int, list[dict]]:
    course_name = str(target["course"]).strip()
    date = str(target.get("date") or "").strip()
    section = str(target.get("section") or "").strip()
    course_id_filter = int(target["course_id"]) if target.get("course_id") else 0

    if date:
        if date not in date_cache:
            date_cache[date] = fetch_sessions_by_date(tokens, date)
        day_sessions = date_cache[date]
        wanted = normalize_name(course_name)
        matched = [
            s
            for s in day_sessions
            if normalize_name(str(s.get("course_name") or "")) == wanted
        ]
        if not matched:
            matched = [
                s
                for s in day_sessions
                if wanted and wanted in normalize_name(str(s.get("course_name") or ""))
            ]
        if not matched:
            names = sorted({str(s.get("course_name") or "") for s in day_sessions})
            raise RuntimeError(
                f"{date} 没有课程“{course_name}”。当天的课程有：\n  " + "\n  ".join(names)
            )
        groups: dict[str, list[dict]] = {}
        for item in matched:
            groups.setdefault(str(item.get("course_id")), []).append(item)
        if course_id_filter:
            groups = {k: v for k, v in groups.items() if int(k) == course_id_filter}
            if not groups:
                raise RuntimeError(
                    f"{date} 的“{course_name}”没有 course_id={course_id_filter}"
                )
        if len(groups) > 1:
            detail = "；".join(
                f"course_id={cid}（{len(items)} 节）" for cid, items in groups.items()
            )
            raise RuntimeError(
                f"{date} 有多个同名课程“{course_name}”：{detail}。请加 --course-id 指定"
            )
        course_id_str, sessions = next(iter(groups.items()))
        course_id = int(course_id_str)
        selected = [
            s
            for s in sessions
            if not section
            or normalize_section(section) in normalize_section(session_section(s))
        ]
        if not selected:
            options = [session_section(s) for s in sessions]
            raise RuntimeError(
                f"{date}《{course_name}》没有节次“{section}”。当天可选：{options}"
            )
    else:
        if course_id_filter:
            course_id = course_id_filter
        else:
            course = pick_course(courses, course_name)
            course_id = int(course["course_id"])
        if course_id not in course_cache:
            course_cache[course_id] = fetch_sessions(tokens, course_id)
        sessions = course_cache[course_id]
        selected = select_sessions(sessions, "", section)
        if not selected:
            recent = [
                f"{session_date(s)} {session_section(s)}".strip() for s in sessions[-20:]
            ]
            raise RuntimeError(
                f"《{course_name}》没有匹配的回放（section={section or '全部'}）。"
                f"最近可选：\n  " + "\n  ".join(recent)
            )

    return course_name, course_id, selected


def download_session(
    tokens: Tokens,
    course_id: int,
    sub: dict,
    *,
    course_name: str = "",
    force: bool,
    dry_run: bool,
    workers: int,
) -> tuple[str, Path | None]:
    """下载单个回放，返回 (状态, 文件路径)。状态为 downloaded/exists/dry-run。"""
    sub_id = session_id(sub)
    if not sub_id:
        raise RuntimeError(f"记录缺少 sub_id：{sub}")
    info = fetch_video_info(tokens, course_id, sub_id)
    course_label = str(info.get("course_name") or course_name)
    title = str(info.get("title") or "").strip()
    if not title:
        title = session_section(sub)
    if not title:
        title = f"{session_date(sub)}{session_section(sub)}"
    target_path = TARGET_DIR / sanitize_filename(f"{course_label} - {title}.mp4")
    if target_path.exists() and target_path.stat().st_size > 0 and not force:
        log(f"[=] 已存在，跳过：{target_path.name}")
        return "exists", target_path
    url, kind = pick_media_url(info)
    log(f"[*] {course_label} - {title}")
    log(f"    日期={session_date(sub)}  sub_id={sub_id}  类型={kind}")
    if dry_run:
        log(f"    URL={url}")
        return "dry-run", target_path

    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix=".haoxue_", dir=TARGET_DIR))
    try:
        if kind == "hls":
            merged = download_hls(url, tokens.cookie, workdir, workers=workers)
            suffix = ".ts"
            source = merged
        else:
            suffix = Path(url.split("?", 1)[0]).suffix or ".mp4"
            source = workdir / f"direct{suffix}"
            log("[*] 直链下载中…")
            download_direct(url, tokens.cookie, source)

        if suffix.lower() in (".mp4", ".m4v"):
            shutil.move(str(source), str(target_path))
        else:
            log("[*] ffmpeg 封装为 mp4…")
            if not remux_to_mp4(source, target_path):
                fallback = target_path.with_suffix(".ts")
                shutil.move(str(source), str(fallback))
                log(f"[!] 已保留 TS：{fallback.name}")
                return "downloaded", fallback
        log(f"[+] 完成：{target_path.name}")
        return "downloaded", target_path
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def process_target(
    tokens: Tokens,
    target: dict,
    *,
    courses: list[dict],
    course_cache: dict[int, list[dict]],
    date_cache: dict[str, list[dict]],
    force: bool,
    dry_run: bool,
    workers: int,
) -> int:
    course_name, course_id, selected = resolve_sessions(
        tokens,
        target,
        courses=courses,
        course_cache=course_cache,
        date_cache=date_cache,
    )

    log(f"[+] 《{course_name}》匹配到 {len(selected)} 个回放")
    count = 0
    for sub in selected:
        status, _ = download_session(
            tokens,
            course_id,
            sub,
            course_name=course_name,
            force=force,
            dry_run=dry_run,
            workers=workers,
        )
        if status == "downloaded":
            count += 1
    return count


def cmd_download(args: argparse.Namespace) -> int:
    try:
        tokens = acquire_tokens()
        targets = load_targets(Path(args.targets)) if args.targets else []
        if args.course:
            item = {"course": args.course, "date": args.date, "section": args.section}
            if args.course_id:
                item["course_id"] = args.course_id
            targets.append(item)
        if not targets:
            log("[X] 需要 --course 或 --targets")
            return 1

        clear_login_flag()
        need_courses = any(
            not str(t.get("date") or "").strip() and not t.get("course_id")
            for t in targets
        )
        courses = fetch_courses(tokens) if need_courses else []
        course_cache: dict[int, list[dict]] = {}
        date_cache: dict[str, list[dict]] = {}
        total = 0
        for target in targets:
            total += process_target(
                tokens,
                target,
                courses=courses,
                course_cache=course_cache,
                date_cache=date_cache,
                force=args.force,
                dry_run=args.dry_run,
                workers=args.workers,
            )
        log(f"[+] 本次完成 {total} 个回放")
        return 0
    except AuthExpired as exc:
        log(f"[X] {exc}")
        notify_login_required(str(exc))
        return 2
    except Exception as exc:
        log(f"[X] 缓存失败：{exc}")
        return 1


# ------------------------------------------------------------- daily sync ---


def parse_date_arg(text: str, label: str) -> date:
    try:
        return datetime.strptime(str(text).strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise RuntimeError(f"{label} 应为 YYYY-MM-DD：{text}") from exc


def load_sync_state() -> dict:
    if not SYNC_STATE.exists():
        return {}
    try:
        data = json.loads(SYNC_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log(f"[!] 同步状态读取失败，按首次运行处理：{exc}")
        return {}
    if not isinstance(data, dict):
        return {}
    if not isinstance(data.get("processed"), dict):
        data["processed"] = {}
    return data


def save_sync_state(state: dict) -> None:
    tmp = SYNC_STATE.with_name(SYNC_STATE.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(SYNC_STATE)


def is_excluded_course(course_name: str, rules: Iterable[tuple[str, ...]]) -> bool:
    norm = normalize_name(course_name)
    return any(
        all(normalize_name(term) in norm for term in rule if normalize_name(term))
        for rule in rules
        if rule
    )


def cmd_sync(args: argparse.Namespace) -> int:
    try:
        tokens = acquire_tokens()
        clear_login_flag()

        today = datetime.now(CST).date()
        since = parse_date_arg(args.since, "--since") if args.since else None
        until = parse_date_arg(args.until, "--until") if args.until else today

        state = load_sync_state()
        baseline_text = str(state.get("baseline_date") or "")
        first_run = not baseline_text
        if first_run:
            baseline = since or today
        else:
            baseline = parse_date_arg(baseline_text, "状态文件 baseline_date")

        if since:
            start = since
            baseline = min(baseline, since)
        elif first_run:
            start = min(until, today)
        else:
            last_text = str(state.get("last_check_date") or "")
            last = (
                parse_date_arg(last_text, "状态文件 last_check_date")
                if last_text
                else baseline
            )
            start = max(baseline, last - timedelta(days=max(args.overlap, 0)))

        if start > until:
            log(f"[=] 无需扫描：{start} 晚于 {until}")
            return 0

        rules = list(DEFAULT_EXCLUDES) + [
            (term,) for term in (args.exclude or []) if normalize_name(term)
        ]
        rules = list(dict.fromkeys(rules))
        processed = state.setdefault("processed", {})
        state["baseline_date"] = baseline.isoformat()
        last_check = (
            parse_date_arg(str(state["last_check_date"]), "状态文件 last_check_date")
            if state.get("last_check_date")
            else None
        )

        log(f"[*] 每日同步：扫描 {start} → {until}（baseline={baseline}）")
        log("[*] 排除课程：" + ("、".join(" + ".join(rule) for rule in rules) if rules else "（无）"))

        counts = {
            "downloaded": 0,
            "exists": 0,
            "processed": 0,
            "excluded": 0,
            "pending": 0,
            "failed": 0,
        }
        failures: list[str] = []

        try:
            day = start
            while day <= until:
                date_str = day.strftime("%Y-%m-%d")
                log(f"[*] 扫描 {date_str} …")
                try:
                    sessions = fetch_sessions_by_date(tokens, date_str)
                except AuthExpired:
                    raise
                except Exception as exc:
                    counts["failed"] += 1
                    failures.append(f"{date_str} 列表获取失败：{exc}")
                    log(f"[X] {date_str} 获取回放列表失败：{exc}")
                    day += timedelta(days=1)
                    continue

                day_found = 0
                for sub in sessions:
                    course_name = str(sub.get("course_name") or "").strip()
                    sub_id = session_id(sub)
                    if is_excluded_course(course_name, rules):
                        counts["excluded"] += 1
                        continue
                    try:
                        course_id = int(sub.get("course_id") or 0)
                    except (TypeError, ValueError):
                        course_id = 0
                    if not course_id or not sub_id:
                        log(f"[!] 跳过异常记录：{sub}")
                        continue

                    record = processed.get(sub_id)
                    if record and not args.force:
                        saved = Path(str(record.get("file") or ""))
                        if record.get("file") and saved.exists() and saved.stat().st_size > 0:
                            counts["processed"] += 1
                            continue
                        log(
                            f"[!] 记录存在但文件缺失，重新缓存："
                            f"{course_name} {session_section(sub)}"
                        )

                    try:
                        status, path = download_session(
                            tokens,
                            course_id,
                            sub,
                            course_name=course_name,
                            force=args.force,
                            dry_run=args.dry_run,
                            workers=args.workers,
                        )
                    except AuthExpired:
                        raise
                    except PlaybackNotReady as exc:
                        counts["pending"] += 1
                        day_found += 1
                        log(f"[=] {course_name} {session_section(sub)}：{exc}")
                        continue
                    except Exception as exc:
                        counts["failed"] += 1
                        failures.append(f"{date_str} {course_name} {session_section(sub)}：{exc}")
                        log(f"[X] {course_name} {session_section(sub)} 缓存失败：{exc}")
                        continue

                    day_found += 1
                    if status == "exists":
                        counts["exists"] += 1
                    elif status == "downloaded":
                        counts["downloaded"] += 1

                    if status in ("exists", "downloaded") and path and not args.dry_run:
                        processed[sub_id] = {
                            "course": course_name,
                            "course_id": course_id,
                            "date": date_str,
                            "title": session_section(sub),
                            "file": str(path),
                            "updated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
                        }
                        save_sync_state(state)

                if day_found == 0:
                    log(f"[=] {date_str} 没有需要缓存的回放")

                if not args.dry_run:
                    if last_check is None or day > last_check:
                        last_check = day
                    state["last_check_date"] = last_check.isoformat()
                    save_sync_state(state)
                day += timedelta(days=1)
        finally:
            if not args.dry_run:
                state["baseline_date"] = baseline.isoformat()
                save_sync_state(state)

        log(
            "[+] 同步完成：新增 {downloaded}，已存在 {exists}，此前已处理 {processed}，"
            "排除 {excluded}，待生成 {pending}，失败 {failed}".format(**counts)
        )
        if failures:
            log("[!] 失败明细：\n  " + "\n  ".join(failures))
            return 1
        return 0
    except AuthExpired as exc:
        log(f"[X] {exc}")
        notify_login_required(str(exc))
        return 2
    except Exception as exc:
        log(f"[X] 同步失败：{exc}")
        return 1


def cmd_probe(args: argparse.Namespace) -> int:
    try:
        tokens = acquire_tokens()
        target = {
            "course": args.course,
            "date": args.date or "",
            "section": args.section or "",
        }
        courses = [] if args.date else fetch_courses(tokens, args.course)
        name, course_id, selected = resolve_sessions(
            tokens,
            target,
            courses=courses,
            course_cache={},
            date_cache={},
        )
        out_dir = SCRIPT_DIR / "probe"
        out_dir.mkdir(exist_ok=True)
        (out_dir / f"course_{course_id}_sessions.json").write_text(
            json.dumps(selected, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        for sub in selected[:5]:
            sub_id = session_id(sub)
            info = fetch_video_info(tokens, course_id, sub_id)
            path = out_dir / f"sub_{course_id}_{sub_id}.json"
            path.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
            print(path)
        log(f"[+] {name} 探测结果已写入 {out_dir}")
        return 0
    except AuthExpired as exc:
        log(f"[X] {exc}")
        notify_login_required(str(exc))
        return 2
    except Exception as exc:
        log(f"[X] 探测失败：{exc}")
        return 1


# ------------------------------------------------------------------ CLI ---


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="北大好学回放自动缓存（电脑直连）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="检查 token 是否有效，失效时通知")
    p_check.set_defaults(func=cmd_check)

    p_list = sub.add_parser("list", help="列出课程/回放")
    p_list.add_argument("--search", default="", help="课程名关键词")
    p_list.add_argument("--course", default="", help="指定课程时列出其回放")
    p_list.add_argument("--date", default="", help="按日期列出可回放课程")
    p_list.set_defaults(func=cmd_list)

    p_dl = sub.add_parser("download", help="下载并缓存回放")
    p_dl.add_argument("--course", default="", help="课程名（精确或唯一模糊匹配）")
    p_dl.add_argument("--course-id", type=int, default=0, help="课程 ID（同名课程时用于区分）")
    p_dl.add_argument("--date", default="", help="日期 YYYY-MM-DD")
    p_dl.add_argument("--section", default="", help="节次，如 第10-11节")
    p_dl.add_argument("--targets", default="", help="JSON 清单文件")
    p_dl.add_argument("--force", action="store_true", help="已存在也重新下载")
    p_dl.add_argument("--dry-run", action="store_true", help="只显示匹配结果和地址")
    p_dl.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="切片并发数")
    p_dl.set_defaults(func=cmd_download)

    p_sync = sub.add_parser("sync", help="按日期增量缓存每天新增的回放（自动排除指定课程）")
    p_sync.add_argument("--since", default="", help="起始日期 YYYY-MM-DD（默认首次为今天；之后自动增量）")
    p_sync.add_argument("--until", default="", help="结束日期 YYYY-MM-DD（默认今天）")
    p_sync.add_argument(
        "--overlap", type=int, default=SYNC_OVERLAP_DAYS, help="每次回看最近几天，默认 3"
    )
    p_sync.add_argument("--exclude", action="append", default=[], help="额外排除的课程关键词，可重复")
    p_sync.add_argument("--force", action="store_true", help="已存在也重新下载")
    p_sync.add_argument("--dry-run", action="store_true", help="只列出将下载的回放，不写状态")
    p_sync.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="切片并发数")
    p_sync.set_defaults(func=cmd_sync)

    p_probe = sub.add_parser("probe", help="抓取接口原始 JSON（调试用）")
    p_probe.add_argument("--course", required=True)
    p_probe.add_argument("--date", default="")
    p_probe.add_argument("--section", default="")
    p_probe.set_defaults(func=cmd_probe)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
