# -*- coding: utf-8 -*-
"""
视频号直播 / 回放下载器  v5.0
============================================================================
· 直播：FLV 边拉边写，断流自动重连，录完转封装 MP4
· 回放：HTTP Range 下载（可续传）→ 官方 WASM 解密 → MP4 / MP3 / 封面
· 嗅探：mitmproxy 只解密 channels.weixin.qq.com，JSBridge 注入取 decode_key
· 队列：可排序（上移/下移/置顶/置底）、串行执行、断点续传、任务持久化

依赖：Python 标准库 + ffmpeg.exe（回放解密另需 node.exe）
============================================================================
"""
import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import bridge_server
import sniffer as sniff_mod
import ui as ui_mod
import wxdecrypt

VERSION = "5.2.0"
BUILD = "2026-10-29"

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
TASKS_PATH = os.path.join(APP_DIR, "tasks.json")
DEFAULT_FFMPEG = r"D:\WorkBuddy\工具\ffmpeg\package\ffmpeg.exe"
DEFAULT_OUTDIR = os.path.join(APP_DIR, "录制")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
CREATE_NO_WINDOW = 0x08000000
FLV_HEADER_LEN = 13          # 9 字节文件头 + 4 字节 PreviousTagSize
PORT = 8765
PROBE_INTERVAL = 20          # 蹲守模式探测间隔（秒）
SILENCE_SECONDS = 300        # 静音多久算"没人了"
SNIFF_MAX_SECONDS = 1800     # 嗅探最长挂 30 分钟，之后自动停并还原代理
STALL_SECONDS = 120          # 直播多久没有新数据算卡死
MAX_RECONNECT_FAIL = 4       # 直播连续几次拉不到数据算结束

# 终态：这些状态的任务不再占用队列
FINAL_STATES = ("已完成", "失败", "已停止", "已取消", "已中断", "地址失效")
RUNNING_STATES = ("录制中", "下载中", "收尾中")
PAUSED = "已暂停"          # 不是终态：随时可以「继续」捡起来


# ----------------------------------------------------------------- 通用
def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_config(data):
    """写配置：先合并再落盘，避免把一个字段写成空就把别的字段抹掉"""
    cur = load_config()
    cur.update({k: v for k, v in (data or {}).items() if v is not None})
    tmp = CONFIG_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
    except Exception:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except Exception:
            pass


_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def open_stream(url, timeout=15):
    """优先直连（国内 CDN 直连最快，也不经过系统代理），直连失败再退回系统代理"""
    req = urllib.request.Request(
        url, headers={"User-Agent": UA,
                      "Referer": "https://channels.weixin.qq.com/"})
    try:
        return _DIRECT_OPENER.open(req, timeout=timeout), "直连"
    except Exception:
        return urllib.request.urlopen(req, timeout=timeout), "系统代理"


def find_ffmpeg():
    for p in (load_config().get("ffmpeg", ""), DEFAULT_FFMPEG):
        if p and os.path.isfile(p):
            return p
    return shutil.which("ffmpeg") or "ffmpeg"


def has_ffmpeg():
    p = find_ffmpeg()
    return os.path.isfile(p) if os.path.isabs(p) else bool(shutil.which(p))


def parse_expiry(url):
    m = re.search(r"[?&]txTime=([0-9a-fA-F]+)", url or "")
    if not m:
        return None
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(m.group(1), 16)))
    except Exception:
        return None


def safe_filename(s, limit=80):
    """把任意标题变成安全的文件名。

    视频号回放的标题里经常夹着 PPT 目录页的文字（"目   录 / 第一部分 …"），
    还会带一堆连续空格。这里一并收拾，否则文件名在资源管理器里长得离谱。
    """
    s = str(s or "")
    s = s.replace("\u3000", " ")                      # 全角空格
    s = re.sub(r"[\r\n\t]+", " ", s)
    s = re.sub(r"\s{2,}", " ", s)                     # 连续空白压成一个
    s = re.sub(r'[\\/:*?"<>|]', "_", s)
    s = re.sub(r"_{2,}", "_", s)
    s = s.strip(" ._-")
    if len(s) > limit:
        s = s[:limit].rstrip(" ._-")
    # Windows 保留名
    if s.upper().split(".")[0] in ("CON", "PRN", "AUX", "NUL") or \
            re.match(r"^(COM|LPT)[1-9]$", s.upper()):
        s = "_" + s
    return s


def build_basename(name):
    today = time.strftime("%Y-%m-%d")
    base = safe_filename(name)
    return f"{base}_{today}" if base else f"视频号直播_{today}"


def human_dur(sec):
    sec = int(max(0, sec))
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def fmt_size(b):
    b = float(b or 0)
    if b >= 1073741824:
        return f"{b / 1073741824:.2f} GB"
    if b >= 1048576:
        return f"{b / 1048576:.1f} MB"
    return f"{b / 1024:.0f} KB"


def ffmpeg_run(args, timeout=1800):
    try:
        r = subprocess.run([find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y"]
                           + args, creationflags=CREATE_NO_WINDOW,
                           capture_output=True, text=True, errors="ignore",
                           timeout=timeout)
        return r.returncode == 0, (r.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return False, "ffmpeg 超时"
    except Exception as e:
        return False, str(e)


def ffmpeg_remux(src, dst):
    return ffmpeg_run(["-i", src, "-c", "copy", "-movflags", "+faststart", dst])


def ffmpeg_to_mp3(src, dst):
    return ffmpeg_run(["-i", src, "-vn", "-c:a", "libmp3lame", "-q:a", "4", dst],
                      timeout=3600)


def ffmpeg_cover(src, dst, at=5):
    """抽一帧当封面（课程目录用），失败就退到第 0 秒再试一次"""
    for ss in (at, 0):
        ffmpeg_run(["-ss", str(ss), "-i", src, "-frames:v", "1", "-q:v", "3", dst],
                   timeout=120)
        if os.path.isfile(dst) and os.path.getsize(dst) > 1024:
            return True
    return False


def probe_alive(url, timeout=8):
    """探一下这个地址现在能不能拉流（开播监控用）。

    注意不能只看"有没有收到字节"：地址过期时服务端会返回一个 HTML 错误页，
    字节数也不少，会被误判成"已开播"。必须确认是 FLV 头，或者 mp4 的 ftyp。
    """
    try:
        resp, _ = open_stream(url, timeout=timeout)
        try:
            head = resp.read(64)
        finally:
            try:
                resp.close()
            except Exception:
                pass
        if head[:3] == b"FLV":
            return True, "FLV"
        if head[4:8] == b"ftyp":
            return True, "MP4"
        return False, "不是流（%r）" % head[:16]
    except Exception as e:
        return False, str(e)[:80]


def disk_free(path):
    try:
        return shutil.disk_usage(path).free
    except Exception:
        return -1


# ----------------------------------------------------------------- 录制线程
class Recorder(threading.Thread):
    """直播录制：一路 HTTP 长连接往下写 FLV，断了重连。

    FLV 天然抗中断：每段重新连上时服务端会先发一遍 13 字节的文件头
    （"FLV"+ver+flags+datasize+PreviousTagSize0），这一段必须丢掉，
    否则在文件中间插进去会让整个流的时间戳错乱、ffmpeg 找不到关键帧。
    """

    def __init__(self, url, flv_path, append=False):
        super().__init__(daemon=True)
        self.url = url
        self.flv_path = flv_path
        # append=True 表示这段文件里已经有内容（暂停后继续录），接着往后写 ——
        # 和断流重连是同一套逻辑：服务端每次都会重发 13 字节文件头，跳过即可
        self.append = bool(append)
        self.bytes = os.path.getsize(flv_path) if (append and os.path.isfile(flv_path)) else 0
        self.reconnects = 0
        self.empty_connects = 0
        self.state = "准备中"
        self.live_ended = False
        self.error = ""
        self.t0 = time.time()
        self.last_data = time.time()
        self.logs = __import__("queue").Queue()
        self._stop = threading.Event()

    def log(self, msg):
        self.logs.put(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def stop(self):
        self._stop.set()

    def run(self):
        first = not self.append
        try:
            f = open(self.flv_path, "ab" if self.append else "wb")
        except Exception as e:
            self.error = f"无法创建文件：{e}"
            self.state = "失败"
            return
        if self.append:
            self.log(f"接着已有文件往后录（{fmt_size(self.bytes)}）")

        try:
            while not self._stop.is_set():
                got = 0
                resp = None
                try:
                    self.state = "连接中"
                    resp, how = open_stream(self.url, timeout=15)
                    ctype = resp.headers.get("Content-Type", "未知")
                    self.log(f"连上了（{how}），返回类型 {ctype}")
                    self.state = "录制中"

                    head = b""
                    while len(head) < FLV_HEADER_LEN and not self._stop.is_set():
                        more = resp.read(FLV_HEADER_LEN - len(head))
                        if not more:
                            break
                        head += more

                    # 不是 FLV 头，基本都是错误页/空响应 —— 千万别把垃圾写进文件，
                    # 否则字节数看着"有增长"，永远判不出直播结束
                    if head[:3] != b"FLV":
                        self.log(f"不是 FLV 流（开头是 {head[:8]!r}）")
                        raise ValueError("not-flv")

                    if first:
                        f.write(head)
                        self.bytes += len(head)
                        got += len(head)
                        first = False
                    # 重连时这一段直接丢弃（服务端每次都重发文件头）

                    last_flush = time.time()
                    while not self._stop.is_set():
                        chunk = resp.read(65536)
                        if not chunk:
                            self.log("服务端关闭了连接")
                            break
                        f.write(chunk)
                        self.bytes += len(chunk)
                        got += len(chunk)
                        self.last_data = time.time()
                        self.empty_connects = 0
                        # 定期落盘，避免异常断电/强杀时把最后几分钟全丢了
                        if time.time() - last_flush > 5:
                            try:
                                f.flush()
                            except Exception:
                                pass
                            last_flush = time.time()

                except ValueError:
                    got = 0
                except Exception as e:
                    if self._stop.is_set():
                        break
                    self.error = str(e)
                    self.log(f"断开：{e}")
                    if "403" in str(e) or "404" in str(e):
                        self.live_ended = True
                        self.state = "地址失效"
                        break
                finally:
                    if resp is not None:
                        try:
                            resp.close()
                        except Exception:
                            pass

                if self._stop.is_set():
                    break

                # 连着几次都一个字节没拿到 → 直播源已经停了
                if got == 0:
                    self.empty_connects += 1
                else:
                    self.empty_connects = 0
                if self.empty_connects >= MAX_RECONNECT_FAIL:
                    self.live_ended = True
                    self.state = "直播结束"
                    self.log(f"连续 {MAX_RECONNECT_FAIL} 次拉不到数据，判定直播已结束")
                    break

                self.reconnects += 1
                self.state = "重连中"
                self.log(f"第 {self.reconnects} 次重连…")
                self._stop.wait(2)
        except Exception as e:
            self.error = str(e)[:200]
            self.state = "失败"
            self.log(f"录制线程异常：{e}")
        finally:
            try:
                f.flush()
            except Exception:
                pass
            try:
                f.close()
            except Exception:
                pass
            if not self.live_ended and self.state not in ("失败", "地址失效"):
                self.state = "已停止"


# ----------------------------------------------------------------- 回放下载线程
class Downloader(threading.Thread):
    """直播回放（点播 MP4）下载器。

    和直播录制不同，回放是有限长度的普通文件，所以用 HTTP Range 分块下载：
    可以显示百分比、断线能续、停止后重开还能接着下（.part 文件保留）。
    下载完的文件前 128KB 可能是加密的，交给 wxdecrypt 处理。
    """

    def __init__(self, url, out_path, decode_key="", enc_len=0):
        super().__init__(daemon=True)
        self.url = url
        self.path = out_path              # 最终 mp4 路径（加密态）
        self.part = out_path + ".part"    # 下载中的临时文件（保留以便续传）
        self.decode_key = str(decode_key or "")
        self.enc_len = int(enc_len or 0)
        self.bytes = 0
        self.total = 0
        self.state = "连接中"
        self.error = ""
        self.logs = __import__("queue").Queue()
        self.last_data = time.time()
        self.reconnects = 0
        self._stop = threading.Event()

    def log(self, msg):
        self.logs.put(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def stop(self):
        self._stop.set()

    def run(self):
        attempt = 0
        while not self._stop.is_set():
            attempt += 1
            try:
                self._fetch_once()
                if self._stop.is_set():
                    self.state = "已停止"
                    return
                if self.total and self.bytes < self.total:
                    raise IOError("连接提前结束（%s / %s）"
                                  % (fmt_size(self.bytes), fmt_size(self.total)))
                self.state = "下载完成"
                return
            except Exception as e:
                if self._stop.is_set():
                    self.state = "已停止"
                    return
                self.error = str(e)[:200]
                self.reconnects += 1
                self.log(f"下载中断：{e}，2 秒后重试（第 {attempt} 次）")
                self._stop.wait(2)
                if attempt >= 8:
                    self.state = "失败"
                    self.error = f"重试 {attempt} 次仍失败：{self.error}"
                    return

    def _fetch_once(self):
        start = os.path.getsize(self.part) if os.path.isfile(self.part) else 0
        self.bytes = start
        headers = {"User-Agent": UA, "Referer": "https://channels.weixin.qq.com/"}
        if start:
            headers["Range"] = "bytes=%d-" % start
        req = urllib.request.Request(self.url, headers=headers)
        # 优先直连（本机系统代理会拖慢大文件），直连不通再退回默认 opener
        try:
            resp = _DIRECT_OPENER.open(req, timeout=30)
        except Exception:
            resp = urllib.request.urlopen(req, timeout=30)
        try:
            code = resp.getcode()
            if code == 206:
                m = re.search(r"/(\d+)\s*$", resp.headers.get("Content-Range", "") or "")
                if m:
                    self.total = int(m.group(1))
            elif code == 200:
                if start:
                    self.log("服务器不支持断点续传，从头下载")
                start = 0                      # 服务器没给 206，只能从头来
                self.bytes = 0
                cl = resp.headers.get("Content-Length")
                if cl and cl.isdigit():
                    self.total = int(cl)
            else:
                raise IOError("HTTP %s" % code)

            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "text/html" in ctype:
                raise IOError("返回的是网页不是视频（地址可能已过期）")

            self.state = "下载中"
            self.log("开始接收数据，总大小 %s" % (
                fmt_size(self.total) if self.total else "未知"))

            mode = "ab" if start else "wb"
            with open(self.part, mode) as f:
                while not self._stop.is_set():
                    chunk = resp.read(262144)
                    if not chunk:
                        break
                    f.write(chunk)
                    self.bytes += len(chunk)
                    self.last_data = time.time()
        finally:
            try:
                resp.close()
            except Exception:
                pass
        if self._stop.is_set():
            self.state = "已停止"


# ----------------------------------------------------------------- 静音监控
def silence_monitor(url, silent_sec, on_silent, should_stop):
    """另拉一路只解音频，静音超过 silent_sec 秒就回调。

    silencedetect 的 d 设小一点（5 秒）让它及时报 silence_start / silence_end，
    真正的阈值由墙钟判断，这样静音还在持续时也能触发。
    """
    state = {"since": None}
    proc = None
    ffmpeg = find_ffmpeg()
    if not has_ffmpeg():
        return
    try:
        cmd = [ffmpeg, "-hide_banner", "-nostats", "-loglevel", "info",
               "-i", url, "-vn",
               "-af", "silencedetect=noise=-45dB:d=5",
               "-f", "null", "-"]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE,
                                creationflags=CREATE_NO_WINDOW)
    except Exception:
        return

    def reader():
        try:
            for raw in proc.stderr:
                s = raw.decode("utf-8", "ignore")
                if "silence_start" in s:
                    state["since"] = time.time()
                elif "silence_end" in s:
                    state["since"] = None
        except Exception:
            pass

    threading.Thread(target=reader, daemon=True).start()
    try:
        while not should_stop():
            time.sleep(2)
            if proc.poll() is not None:
                break
            if state["since"] and time.time() - state["since"] >= silent_sec:
                try:
                    on_silent()
                except Exception:
                    pass
                break
    finally:
        try:
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except Exception:
                    proc.kill()
        except Exception:
            pass


# ----------------------------------------------------------------- 状态
STATE = {
    "tasks": [],          # 全部任务
    "queue": [],          # 待录任务 id（顺序 = 队列顺序，可被排序接口调整）
    "active": None,       # 当前在录/收尾的任务 id
    "next_id": 1,
    "logs": [],
    "log_seq": 0,         # 日志单调序号（不能拿 len(logs) 当序号，裁剪后会错乱）
    "started_at": time.time(),
    "restart_count": 0,
}
LOCK = threading.RLock()
SNIFFER = sniff_mod.Sniffer()


def add_log(msg, stamped=False):
    line = msg if stamped else f"[{time.strftime('%H:%M:%S')}] {msg}"
    with LOCK:
        STATE["logs"].append((STATE["log_seq"], line))
        STATE["log_seq"] += 1
        if len(STATE["logs"]) > 1200:
            STATE["logs"] = STATE["logs"][-700:]


def drain_rec_logs():
    for t in list(STATE["tasks"]):
        r = t.get("rec")
        if not r:
            continue
        q = r.logs
        while not q.empty():
            try:
                add_log(q.get_nowait(), stamped=True)
            except Exception:
                break


def task_by_id(tid):
    try:
        tid = int(tid)
    except Exception:
        return None
    for t in list(STATE["tasks"]):
        if t.get("id") == tid:
            return t
    return None


KIND_CN = {"live": "直播", "replay": "回放"}


def public_task(t):
    """给前端的任务快照。只挑需要的字段，别把 rec 线程对象序列化出去。"""
    qpos = 0
    try:
        qpos = STATE["queue"].index(t["id"]) + 1
    except ValueError:
        qpos = 0
    total = t.get("total") or 0
    pct = 0.0
    if total > 0:
        pct = min(100.0, t["bytes"] * 100.0 / total)
    elif t.get("status") == "已完成":
        pct = 100.0
    # mp4 字段只在真正产出文件后才会有值，这里再落一次盘确认 ——
    # 文件被用户手工删掉时按钮要跟着变成不可点，而不是点了报错
    mp4 = t.get("mp4") or ""
    mp3 = t.get("mp3") or ""
    has_mp4 = bool(mp4 and os.path.isfile(mp4))
    has_mp3 = bool(mp3 and os.path.isfile(mp3))
    return {
        "id": t["id"], "name": t["name"], "status": t["status"],
        "elapsed": round(t.get("elapsed", 0), 1),
        "bytes": t.get("bytes", 0), "total": total, "pct": round(pct, 1),
        "reconnects": t.get("reconnects", 0),
        "mp4": mp4 if has_mp4 else "", "mp3": mp3 if has_mp3 else "",
        "has_mp4": has_mp4, "has_mp3": has_mp3,
        "err": t.get("err", ""), "note": t.get("note", ""),
        "created": t.get("created", ""), "limit_min": t.get("limit_min", 0),
        "silence_stop": bool(t.get("silence_stop")), "wait_live": bool(t.get("wait_live")),
        "audio": bool(t.get("audio")), "kind": t.get("kind", "live"),
        "qpos": qpos, "has_part": bool(t.get("has_part")),
        "dir": t.get("dir", ""), "paused": bool(t.get("paused")),
    }


# ----------------------------------------------------------------- 持久化
def _to_disk(t):
    d = {k: v for k, v in t.items() if k != "rec"}
    d["t0"] = 0.0            # 重启后墙钟基准失效，别带着旧值算时长
    return d


def save_tasks():
    with LOCK:
        data = {"version": VERSION,
                "next_id": STATE["next_id"],
                "queue": list(STATE["queue"]),
                "tasks": [_to_disk(t) for t in STATE["tasks"]]}
    tmp = TASKS_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, TASKS_PATH)
    except Exception:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except Exception:
            pass


def load_tasks():
    """启动时恢复上次的列表。

    进程重启后线程已经没了，所以原来"录制中/下载中/收尾中"的任务一律改成
    "已中断"——但文件留在盘上，用户可以点「继续」接着下（回放支持断点续传）。
    """
    if not os.path.isfile(TASKS_PATH):
        return
    try:
        with open(TASKS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return
    if not isinstance(data, dict):
        return
    tasks = data.get("tasks") or []
    max_id = 0
    restored = 0
    for d in tasks:
        if not isinstance(d, dict) or "id" not in d:
            continue
        t = dict(d)
        t.setdefault("kind", "live")
        t.setdefault("dir", DEFAULT_OUTDIR)
        t.setdefault("note", "")
        t["rec"] = None
        t["finishing"] = False
        t["probe_t"] = 0.0
        t["t0"] = 0.0
        t["elapsed"] = float(t.get("elapsed") or 0)
        # 旧版本会把"预期输出路径"直接写进 mp4 字段，重启后看着像已完成。
        # 一律以磁盘为准：文件不在就当没有。
        for k in ("mp4", "mp3"):
            if t.get(k) and not os.path.isfile(t[k]):
                t[k] = ""
        part = (t.get("flv") or "") + ".part"
        t["has_part"] = os.path.isfile(part)
        if t.get("status") in RUNNING_STATES:
            t["status"] = "已中断"
            t["note"] = "程序重启导致中断" + (
                "，断点已保留，可点「继续」" if t["has_part"] else "")
        # 排队中的保持排队；蹲守的必须还原成"等待开播"。
        # 这里以前一律写成"排队中"，蹲守任务重启后就退化成普通任务 ——
        # 调度线程拿着还没开播的地址直接开录，几秒后必然报"失败"，白丢一次蹲守。
        if t.get("status") == PAUSED:
            t["paused"] = True
        elif t.get("status") in ("排队中", "等待开播"):
            t["status"] = "等待开播" if t.get("wait_live") else "排队中"
        STATE["tasks"].append(t)
        max_id = max(max_id, int(t.get("id") or 0))
        restored += 1
    STATE["next_id"] = max(int(data.get("next_id") or 0), max_id + 1)
    # 队列只保留真正还在排队的，其余按 id 顺序补回去
    q = [i for i in (data.get("queue") or []) if task_by_id(i)]
    waiting = [t["id"] for t in STATE["tasks"] if t.get("status") == "排队中"]
    seen = set()
    STATE["queue"] = []
    for i in q + waiting:
        if i in seen:
            continue
        t = task_by_id(i)
        if t and t.get("status") == "排队中":
            STATE["queue"].append(i)
            seen.add(i)
    if restored:
        add_log(f"已恢复上次的 {restored} 条任务记录")


# ----------------------------------------------------------------- 任务生命周期
def new_task(name, url, outdir, limit_min=0, audio=False, silence_stop=False,
             wait_live=False, kind="live", decode_key="", dedup=True):
    """创建一个任务并排进队列。

    dedup=True 时，若已有同 kind + 同 url 或同名字的**活跃**任务，直接返回它，
    不重复建 —— 否则用户点两下「下载」，同一个片子会下两份、还共用同一个文件名。
    """
    url = (url or "").strip()
    if not url.lower().startswith("http"):
        raise ValueError("地址要以 http/https 开头")

    outdir = (outdir or "").strip() or DEFAULT_OUTDIR
    is_replay = (kind == "replay")
    if is_replay:
        # 回放是有限长度的文件，蹲守/静音/限时这些直播专属选项对它没有意义
        wait_live, silence_stop, limit_min = False, False, 0

    with LOCK:
        if dedup:
            for x in STATE["tasks"]:
                if x.get("kind") != kind or x.get("status") in ("已取消", "失败"):
                    continue
                if x.get("url") == url:
                    return x
        try:
            os.makedirs(outdir, exist_ok=True)
        except Exception as e:
            raise ValueError(f"保存目录不可用：{e}")

        base = build_basename(name)

        # 同名任务可能同时排队，此时文件还没落地，只看 os.path.exists 会撞车
        used = set()
        for x in STATE["tasks"]:
            for k in ("flv", "mp4"):
                if x.get(k):
                    used.add(os.path.normcase(os.path.abspath(x[k])))

        def taken(p):
            return os.path.normcase(os.path.abspath(p)) in used or os.path.exists(p)

        if is_replay:
            # 回放：先下到 .enc.mp4（可能是加密态），解密成功后再产出 .mp4
            # 判重只看最终文件和"已被其它任务占用"，不看 .part ——
            # .part 存在多半是上次下到一半，留着正好用来续传。
            work = os.path.join(outdir, f"{base}.enc.mp4")
            i = 2
            while taken(work):
                work = os.path.join(outdir, f"{base}({i}).enc.mp4")
                i += 1
        else:
            work = os.path.join(outdir, f"{base}.flv")
            i = 2
            while taken(work) or os.path.exists(work[:-4] + ".mp4"):
                work = os.path.join(outdir, f"{base}({i}).flv")
                i += 1

        t = {
            "id": STATE["next_id"], "name": safe_filename(name) or
            ("视频号回放" if is_replay else "视频号直播"),
            "url": url, "dir": outdir, "flv": work, "mp4": "", "mp3": "",
            "err": "", "note": "",
            "kind": "replay" if is_replay else "live",
            "decode_key": str(decode_key or ""),
            "status": "等待开播" if wait_live else "排队中",
            "elapsed": 0.0, "bytes": 0, "total": 0, "reconnects": 0,
            "created": time.strftime("%H:%M:%S"), "t0": 0.0,
            "limit_min": float(limit_min or 0), "audio": bool(audio),
            "silence_stop": bool(silence_stop), "wait_live": bool(wait_live),
            "rec": None, "probe_t": 0.0, "finishing": False,
            "has_part": os.path.isfile(work + ".part"),
        }
        STATE["next_id"] += 1
        STATE["tasks"].append(t)
        STATE["queue"].append(t["id"])
    add_log(f"加入{'回放' if is_replay else '直播'}任务 #{t['id']}　{t['name']}"
            + ("（蹲守，等开播）" if wait_live else ""))
    save_tasks()
    return t


def start_task(t):
    t["t0"] = time.time()
    t["err"] = ""
    t["note"] = ""
    t["paused"] = False
    free = disk_free(t["dir"])
    if 0 <= free < 2 * 1024 ** 3:
        add_log(f"⚠ #{t['id']} 磁盘只剩 {free / 1024 ** 3:.1f} GB，长录制可能装不下")

    # 回放：走下载器（可续传），不是直播那种无限录制
    if t["kind"] == "replay":
        d = Downloader(t["url"], t["flv"], t.get("decode_key", ""))
        t["rec"] = d
        t["status"] = "下载中"
        d.start()
        STATE["active"] = t["id"]
        add_log(f"⬇ 开始下载回放 #{t['id']}　{os.path.basename(t['flv'])}")
        return

    # 已有内容（暂停后继续）就接着往后录，不要覆盖掉暂停前那一段
    prev = os.path.getsize(t["flv"]) if os.path.isfile(t["flv"]) else 0
    r = Recorder(t["url"], t["flv"], append=prev > 10240)
    t["rec"] = r
    t["status"] = "录制中"
    r.start()
    STATE["active"] = t["id"]
    add_log(f"▶ 开始录制 #{t['id']}　{os.path.basename(t['flv'])}"
            + (f"（已有 {fmt_size(prev)}，接着录）" if prev > 10240 else ""))

    if t["silence_stop"]:
        def on_silent():
            add_log(f"⚠ #{t['id']} 静音超过 {SILENCE_SECONDS // 60} 分钟，自动停止")
            stop_task(t["id"], "静音自动停止")

        threading.Thread(
            target=silence_monitor,
            args=(t["url"], SILENCE_SECONDS, on_silent,
                  lambda: (t.get("rec") is not r) or (not r.is_alive())),
            daemon=True).start()


def stop_task(tid, reason=""):
    """停止 / 取消一个任务。

    以前只处理"正在跑"和"等待开播"两种情况 —— 「排队中」的任务点停止毫无反应，
    因为它既没有线程、状态又不是"等待开播"，调度线程一有空就把它捞起来开录。
    """
    t = task_by_id(tid)
    if not t:
        return {"ok": False, "msg": "任务不存在"}
    st = t.get("status")
    if st in FINAL_STATES:
        return {"ok": True, "msg": "任务已结束"}

    r = t.get("rec")
    if st == "收尾中":
        # 收尾里已经在 join 线程 / 转封装了，这时候把它标成"已取消"会让
        # 收尾线程结束时的判断全部落空，文件白处理一遍
        return {"ok": True, "msg": "正在收尾，请稍等"}
    if st in RUNNING_STATES and r and r.is_alive():
        r.stop()
        add_log(f"■ 停止 #{t['id']}" + (f"（{reason}）" if reason else ""))
        return {"ok": True}

    # 还没跑起来的：直接从队列摘掉
    with LOCK:
        try:
            STATE["queue"].remove(t["id"])
        except ValueError:
            pass
    t["status"] = "已取消"
    t["note"] = reason or "已取消"
    add_log(f"已取消 #{t['id']}　{t['name']}" + (f"（{reason}）" if reason else ""))
    save_tasks()
    return {"ok": True}


def move_task(tid, direction):
    """调整队列顺序。只动"还没开始"的任务，正在录的不许挪。"""
    t = task_by_id(tid)
    if not t:
        return {"ok": False, "msg": "任务不存在"}
    with LOCK:
        q = STATE["queue"]
        if tid not in q:
            return {"ok": False, "msg": "这条不在队列里（可能在录或已结束）"}
        i = q.index(tid)
        if direction == "up" and i > 0:
            q[i - 1], q[i] = q[i], q[i - 1]
        elif direction == "down" and i < len(q) - 1:
            q[i + 1], q[i] = q[i], q[i + 1]
        elif direction == "top":
            q.insert(0, q.pop(i))
        elif direction == "bottom":
            q.append(q.pop(i))
        else:
            return {"ok": True, "msg": "已经到头了"}
        pos = q.index(tid) + 1
    save_tasks()
    return {"ok": True, "pos": pos}


def retry_task(tid):
    """把已停止/失败/已中断的任务重新排回队列（回放会从 .part 续传）"""
    t = task_by_id(tid)
    if not t:
        return {"ok": False, "msg": "任务不存在"}
    if t.get("status") in RUNNING_STATES:
        return {"ok": False, "msg": "任务正在运行"}
    if t.get("status") == "已完成":
        return {"ok": False, "msg": "任务已经完成了"}
    part = (t.get("flv") or "") + ".part"
    t["has_part"] = os.path.isfile(part)
    t["finishing"] = False
    t["paused"] = False
    t["rec"] = None
    t["err"] = ""
    t["note"] = "重新排队" + (f"，从 {fmt_size(os.path.getsize(part))} 处续传"
                              if t["has_part"] else "")
    t["status"] = "等待开播" if (t.get("wait_live") and t["kind"] == "live") else "排队中"
    with LOCK:
        if t["id"] not in STATE["queue"]:
            STATE["queue"].append(t["id"])
    add_log(f"↻ #{t['id']} 重新排队　{t['name']}")
    save_tasks()
    return {"ok": True}


def pause_all():
    """全部暂停。

    正在录/下载的停下（文件保留），排队和蹲守的清出队列并标「已暂停」。
    跟「停止」的区别：状态不进终态，点「继续」能原样捡回来 ——
    回放从 .part 续传，直播接着同一个 FLV 往后写。
    """
    n = 0
    with LOCK:
        pending = [t for t in STATE["tasks"]
                   if t.get("status") in ("排队中", "等待开播")]
        STATE["queue"] = []
    for t in pending:
        t["paused"] = True
        t["status"] = PAUSED
        t["note"] = "已暂停"
        n += 1

    aid = STATE["active"]
    t = task_by_id(aid) if aid else None
    if t and t.get("status") in ("录制中", "下载中"):
        t["paused"] = True
        stop_task(aid, "全部暂停")
        n += 1

    save_tasks()
    if n:
        add_log(f"⏸ 已暂停 {n} 个任务")
    return {"ok": True, "n": n,
            "msg": f"已暂停 {n} 个任务" if n else "没有可暂停的任务"}


def resume_all():
    """恢复所有被暂停的任务，按原顺序重新排队"""
    n = 0
    with LOCK:
        for t in STATE["tasks"]:
            if t.get("status") != PAUSED:
                continue
            t["paused"] = False
            t["finishing"] = False
            t["rec"] = None
            t["err"] = ""
            t["note"] = "恢复排队"
            t["status"] = ("等待开播"
                           if (t.get("wait_live") and t.get("kind") == "live")
                           else "排队中")
            if t["id"] not in STATE["queue"]:
                STATE["queue"].append(t["id"])
            n += 1
    save_tasks()
    if n:
        add_log(f"▶ 恢复 {n} 个任务")
    return {"ok": True, "n": n,
            "msg": f"已恢复 {n} 个任务" if n else "没有被暂停的任务"}


def dedup_tasks(apply=True):
    """清理任务列表里的重复项。

    重复的典型来源：同一个回放被嗅探抓了好几遍（每次 svrnonce 不同）、
    或者用户连点了两下「下载」。判重键用「类型 + 名称 + 文件大小」，
    保留优先级：正在跑 > 已完成 > 队列中 > 其它，同优先级保留 id 小的。
    """
    groups = {}
    for t in list(STATE["tasks"]):
        key = (t.get("kind"), t.get("name"), int(t.get("total") or 0))
        groups.setdefault(key, []).append(t)

    rank = {"录制中": 0, "下载中": 0, "收尾中": 0, "已完成": 1, "排队中": 2,
            "等待开播": 3, "已停止": 4, "已中断": 4, "失败": 5, "已取消": 6}
    removed = []
    for key, lst in groups.items():
        if len(lst) < 2:
            continue
        lst.sort(key=lambda x: (rank.get(x.get("status"), 9), x.get("id", 0)))
        keep = lst[0]
        for x in lst[1:]:
            # 正在跑的绝不删，哪怕看着像重复
            if x.get("status") in RUNNING_STATES:
                continue
            removed.append((x.get("id"), x.get("name"), keep.get("id")))
    if not removed or not apply:
        return {"ok": True, "n": len(removed), "items": removed[:50]}

    drop_ids = {r[0] for r in removed}
    with LOCK:
        STATE["tasks"] = [t for t in STATE["tasks"] if t["id"] not in drop_ids]
        STATE["queue"] = [i for i in STATE["queue"] if i not in drop_ids]
    for tid, name, keep in removed:
        add_log(f"🧹 删除重复 #{tid}　{name}（保留 #{keep}）")
    save_tasks()
    return {"ok": True, "n": len(removed), "items": removed[:50]}


def _finish_replay(t):
    """回放下载收尾：把临时文件转正 → 必要时解密 → 抽音频/封面"""
    r = t.get("rec")
    state = getattr(r, "state", "") or ""
    if getattr(r, "total", 0) and getattr(r, "bytes", 0):
        t["total"] = r.total
        t["bytes"] = r.bytes
    enc = t["flv"]                    # xxx.enc.mp4
    part = enc + ".part"

    if state != "下载完成":
        if os.path.isfile(part):
            try:
                mb = os.path.getsize(part) / 1048576
            except Exception:
                mb = 0
            if t.get("paused"):
                t["status"] = "已暂停"
                t["note"] = f"已暂停，已下载 {mb:.1f} MB，点「继续」从断点续传"
            else:
                t["status"] = "已停止"
                t["note"] = f"未下载完，已保留断点 {mb:.1f} MB，点「继续」可续传"
                t["err"] = t["note"]
            t["has_part"] = True
            add_log(f"{'⏸' if t.get('paused') else '■'} #{t['id']} {t['note']}")
        else:
            t["status"] = "失败"
            t["err"] = (state or "下载失败") + (
                f"：{t['err']}" if t.get("err") else "")
            t["note"] = t["err"]
            add_log(f"✘ #{t['id']} 下载失败：{t['err']}")
        return

    try:
        if os.path.isfile(part):
            os.replace(part, enc)
    except Exception as e:
        t["status"], t["err"] = "失败", f"整理临时文件失败：{e}"
        return
    t["has_part"] = False

    if not os.path.isfile(enc) or os.path.getsize(enc) < 10240:
        t["status"], t["err"] = "失败", "下载到的文件为空或过小"
        t["note"] = t["err"]
        add_log(f"✘ #{t['id']} 下载到的文件无效")
        return

    out = enc[:-len(".enc.mp4")] + ".mp4" if enc.endswith(".enc.mp4") else enc[:-4] + ".mp4"
    size_mb = os.path.getsize(enc) / 1048576

    chk = wxdecrypt.check(enc)
    if not chk.get("ok"):
        t["status"], t["err"] = "失败", "无法判断文件是否加密：" + str(chk.get("reason", ""))
        t["note"] = t["err"]
        add_log(f"✘ #{t['id']} {t['err']}")
        return

    if not chk.get("encrypted"):
        try:
            os.replace(enc, out)
        except Exception as e:
            t["status"], t["err"] = "失败", f"重命名失败：{e}"
            return
        t["mp4"] = out
        add_log(f"✔ #{t['id']} 下载完成（未加密）{os.path.basename(out)}　{size_mb:.1f} MB")
    else:
        key = (t.get("decode_key") or "").strip()
        if not key:
            t["status"] = "失败"
            t["err"] = "文件是加密的，但没抓到 decode_key —— 请重新嗅探（回放页接口会带 key）"
            t["note"] = t["err"]
            add_log(f"✘ #{t['id']} {t['err']}")
            add_log(f"　 加密文件保留在 {os.path.basename(enc)}，拿到 key 后可手动解密")
            return
        add_log(f"　 解密中（decode_key={key}）…")
        res = wxdecrypt.decrypt(enc, out, key)
        if not res.get("ok"):
            t["status"] = "失败"
            t["err"] = res.get("reason", "解密失败")
            t["note"] = t["err"]
            add_log(f"✘ #{t['id']} {t['err']}")
            add_log(f"　 加密文件保留在 {os.path.basename(enc)}")
            return
        t["mp4"] = out
        try:
            os.remove(enc)
        except Exception:
            pass
        add_log(f"✔ #{t['id']} 解密完成 {os.path.basename(out)}　"
                f"{os.path.getsize(out) / 1048576:.1f} MB")

    if t["audio"]:
        mp3 = t["mp4"][:-4] + ".mp3"
        ok2, err2 = ffmpeg_to_mp3(t["mp4"], mp3)
        if ok2 and os.path.isfile(mp3):
            t["mp3"] = mp3
            add_log(f"✔ #{t['id']} 音频 {os.path.basename(mp3)}　"
                    f"{os.path.getsize(mp3) / 1048576:.1f} MB")
        else:
            add_log(f"✘ #{t['id']} MP3 导出失败：{err2[:150]}")
    cover = t["mp4"][:-4] + ".jpg"
    if ffmpeg_cover(t["mp4"], cover):
        add_log(f"　 封面 {os.path.basename(cover)}")
    t["status"] = "已完成"


def finish_task(t):
    """收尾。整个流程必须包一层 try —— 里面任何一步（ffmpeg 崩、文件被占用）
    抛异常，这个线程就消失了，而 STATE["active"] 被置空，任务却永远停在
    「收尾中」：既不算完成也不能重排，用户只能重启程序。"""
    add_log(f"正在收尾 #{t['id']} …")
    r = t.get("rec")
    if r:
        try:
            r.join(timeout=20)
        except Exception:
            pass
    drain_rec_logs()

    try:
        if t.get("kind") == "replay":
            return _finish_replay(t)
        _finish_live(t)
    except Exception as e:
        t["status"] = "失败"
        t["err"] = f"收尾异常：{e}"
        t["note"] = t["err"]
        add_log(f"✘ #{t['id']} 收尾异常：{e}")
        add_log("　 " + traceback.format_exc(limit=3).replace("\n", " | ")[:400])


def _finish_live(t):
    flv = t["flv"]
    r = t.get("rec")

    # 「全部暂停」停下来的任务不转封装：FLV 先留着，恢复后接着往后录，
    # 等真正录完再一次性转，避免中途反复生成/覆盖 mp4。
    if t.get("paused"):
        size = os.path.getsize(flv) if os.path.isfile(flv) else 0
        t["status"] = "已暂停"
        t["note"] = (f"已暂停，已录 {fmt_size(size)}，点「继续」接着录"
                     if size > 10240 else "已暂停（这段没录到数据）")
        add_log(f"⏸ #{t['id']} 已暂停{f'（{fmt_size(size)}）' if size else ''}")
        return

    # 用户主动点"停止"和"直播结束/地址过期"要分开：前者不该标成失败，
    # 否则界面上全是红叉，分不清是出错了还是自己停的。
    by_user = bool(r is not None and getattr(r, "state", "") == "已停止")
    if not os.path.isfile(flv) or os.path.getsize(flv) < 10240:
        if by_user:
            t["status"] = "已停止"
            t["note"] = "手动停止（这段没录到有效数据）"
        else:
            t["status"] = "失败"
            t["err"] = "没有录到有效数据（地址过期或直播已结束）"
            t["note"] = t["err"]
        add_log(f"{'■' if by_user else '✘'} #{t['id']} {t['note']}")
        return

    if not has_ffmpeg():
        t["status"] = "已停止"
        t["err"] = "找不到 ffmpeg，无法转封装（FLV 原文件仍在）"
        t["note"] = t["err"]
        t["mp4"] = ""
        add_log(f"✘ #{t['id']} 找不到 ffmpeg，FLV 已保留：{os.path.basename(flv)}")
        return

    mp4 = flv[:-4] + ".mp4"
    ok, err = ffmpeg_remux(flv, mp4)
    if ok and os.path.isfile(mp4) and os.path.getsize(mp4) > 10240:
        t["mp4"] = mp4
        t["bytes"] = os.path.getsize(mp4)
        add_log(f"✔ #{t['id']} 完成 {os.path.basename(mp4)}　"
                f"{os.path.getsize(mp4) / 1048576:.1f} MB")
        if t["audio"]:
            mp3 = mp4[:-4] + ".mp3"
            ok2, err2 = ffmpeg_to_mp3(mp4, mp3)
            if ok2 and os.path.isfile(mp3):
                t["mp3"] = mp3
                add_log(f"✔ #{t['id']} 音频 {os.path.basename(mp3)}　"
                        f"{os.path.getsize(mp3) / 1048576:.1f} MB")
            else:
                add_log(f"✘ #{t['id']} MP3 导出失败：{err2[:150]}")
        cover = mp4[:-4] + ".jpg"
        if ffmpeg_cover(mp4, cover):
            add_log(f"　 封面 {os.path.basename(cover)}")
        t["status"] = "已完成"
    else:
        t["status"] = "失败"
        t["err"] = err[:200] or "转封装失败"
        t["note"] = t["err"]
        add_log(f"✘ #{t['id']} 转封装失败，FLV 原文件仍在：{err[:150]}")


def _do_finish_async(t):
    try:
        finish_task(t)
    except Exception as e:
        t["status"] = "失败"
        t["err"] = str(e)[:200]
        add_log(f"✘ #{t['id']} 收尾线程异常：{e}")
    finally:
        with LOCK:
            if STATE["active"] == t["id"]:
                STATE["active"] = None
        save_tasks()


def manager():
    """总调度：跑队列 + 盯当前任务 + 蹲守探测"""
    while True:
        time.sleep(0.5)
        try:
            drain_rec_logs()

            aid = STATE["active"]
            if aid:
                t = task_by_id(aid)
                if not t:
                    with LOCK:
                        STATE["active"] = None          # 任务被删掉了，别卡住调度
                    continue
                r = t.get("rec")
                if r:
                    t["bytes"] = r.bytes
                    t["reconnects"] = r.reconnects
                    if getattr(r, "total", 0):
                        t["total"] = r.total
                if t["t0"]:
                    t["elapsed"] = time.time() - t["t0"]

                # 停止条件：只判断，不在这里改状态 —— 以前先置"收尾中"，
                # 导致下面"录制线程结束"的分支永远不成立，任务卡死、队列全停。
                if t["status"] == "录制中":
                    reason = ""
                    if t["limit_min"] > 0 and t["elapsed"] >= t["limit_min"] * 60:
                        reason = f"到达设定时长 {t['limit_min']:g} 分钟"
                    elif r and r.is_alive() and time.time() - r.last_data > STALL_SECONDS:
                        reason = f"超过 {STALL_SECONDS // 60} 分钟没有新数据"
                    if reason:
                        add_log(f"{reason}，停止 #{aid}")
                        stop_task(aid, reason)

                # 线程结束（自然结束 / 被判结束 / 手动停）→ 收尾，且只收一次
                if (t["status"] in RUNNING_STATES and r
                        and not r.is_alive() and not t["finishing"]):
                    t["finishing"] = True
                    t["status"] = "收尾中"
                    add_log(f"#{aid} " + ("下载结束" if t.get("kind") == "replay"
                                          else "录制结束") + f"（{r.state}）")
                    threading.Thread(target=_do_finish_async, args=(t,),
                                     daemon=True).start()
                continue

            # 空闲：挑下一个
            with LOCK:
                if not STATE["queue"]:
                    continue
                tid = STATE["queue"][0]
            t = task_by_id(tid)
            if not t or t.get("status") in FINAL_STATES:
                with LOCK:
                    try:
                        STATE["queue"].remove(tid)
                    except ValueError:
                        pass
                continue
            if t["wait_live"] and t["status"] == "等待开播":
                if time.time() - t["probe_t"] < PROBE_INTERVAL:
                    continue
                t["probe_t"] = time.time()
                ok, info = probe_alive(t["url"])
                if not ok:
                    t["note"] = f"还没开播（{info}）"
                    continue
                add_log(f"✔ #{tid} 检测到可以拉流了，开始录制")
            with LOCK:
                try:
                    STATE["queue"].remove(tid)
                except ValueError:
                    pass
            start_task(t)
            save_tasks()
        except Exception as e:
            add_log(f"调度异常：{e}")


# ----------------------------------------------------------------- 嗅探联动
def api_sniff_start():
    ok, msg = SNIFFER.start()
    if ok:
        add_log("开始嗅探：只解密 channels.weixin.qq.com，其他网站不受影响")
        add_log("去微信里打开「视频号 → 回放」页面，让它播几秒")
    else:
        add_log("嗅探启动失败：" + msg)
    return {"ok": ok, "msg": msg}


def api_sniff_stop():
    SNIFFER.stop(restore=True)
    add_log("已停止嗅探，系统代理已恢复")
    return {"ok": True, "msg": "已停止嗅探，系统代理已恢复"}


def sniff_watch():
    """盯嗅探进度。

    注意：**抓到之后不自动入队**。回放页右侧的推荐列表会把几十个视频的
    url + decode_key 一起报上来，自动入队等于往队列里塞一堆根本没人要的视频。
    这里只负责把结果收进列表 + 写日志，进不进队列由用户在页面上勾。
    """
    started = None
    while True:
        time.sleep(0.5)
        try:
            if SNIFFER.state != "嗅探中":
                started = None
                continue
            if started is None:
                started = time.time()
            if time.time() - started > SNIFF_MAX_SECONDS:
                SNIFFER.stop(restore=True)
                add_log(f"嗅探已满 {SNIFF_MAX_SECONDS // 60} 分钟，自动停止并还原系统代理")
                started = None
                continue
            hits = SNIFFER.poll()
            new_play = [h for h in hits if h.get("played")]
            for h in hits:
                tag = "回放" if h.get("kind") == "replay" else "直播"
                extra = ""
                if h.get("kind") == "replay":
                    extra = "　带 key" if h.get("decode_key") else "　⚠ 没抓到 key"
                elif h.get("expiry"):
                    extra = f"　有效期至 {h['expiry']}"
                add_log(f"＋ 抓到{tag} #{h['id']}　{(h.get('name') or '')[:36]}{extra}")
            for h in new_play:
                add_log(f"▶ #{h['id']} 正在播放（已标记为播放页视频）")
            if len(hits) > 3:
                add_log(f"　本轮新增 {len(hits)} 条，其中 {len(new_play)} 条是正在播放的")
        except Exception as e:
            add_log(f"嗅探线程异常：{e}")


# ----------------------------------------------------------------- 打开文件 / 目录
def _allowed_roots():
    """允许通过 /api/open 打开的目录白名单。

    这个接口能调 os.startfile，等于给本机开了一个"打开任意文件"的洞。
    只允许打开任务输出目录和程序目录，其余一律拒绝。
    """
    roots = {os.path.normcase(os.path.abspath(APP_DIR)),
             os.path.normcase(os.path.abspath(DEFAULT_OUTDIR))}
    for t in list(STATE["tasks"]):
        d = t.get("dir")
        if d:
            roots.add(os.path.normcase(os.path.abspath(d)))
    cfg_dir = load_config().get("last_dir")
    if cfg_dir:
        roots.add(os.path.normcase(os.path.abspath(cfg_dir)))
    return roots


def _is_allowed(path):
    try:
        p = os.path.normcase(os.path.abspath(path))
    except Exception:
        return False
    for r in _allowed_roots():
        if p == r or p.startswith(r.rstrip("\\/") + os.sep):
            return True
    return False


def open_path(path, reveal=False):
    """打开文件/文件夹。返回 (ok, msg)，失败原因要能显示给用户。"""
    if not path:
        return False, "没有指定路径"
    path = os.path.normpath(path)
    if not os.path.exists(path):
        return False, f"路径不存在：{path}"
    is_dir = os.path.isdir(path)
    # 目录一律放行：用户完全可能把保存位置设在别的盘（E:\课程），
    # 而白名单只认任务/配置里出现过的目录，那样点"打开目录"会被莫名拒绝。
    # 文件则必须落在白名单里 —— 这才是真正有风险的那一半（防止被诱导
    # 打开本机任意可执行文件）。
    if not is_dir and not _is_allowed(path):
        return False, "这个文件不在允许打开的目录范围内"
    try:
        if os.name != "nt":
            return False, "仅支持 Windows"
        if is_dir:
            os.startfile(path)
        elif reveal:
            subprocess.Popen(["explorer", "/select,", path],
                             creationflags=CREATE_NO_WINDOW)
        else:
            os.startfile(path)
        return True, "已打开"
    except Exception as e:
        return False, f"打开失败：{e}"


# ----------------------------------------------------------------- HTTP
class RecorderServer(ThreadingHTTPServer):
    allow_reuse_address = False     # Windows 上 SO_REUSEADDR 会允许绑定已被监听的端口
    daemon_threads = True


def already_running(port):
    try:
        with _DIRECT_OPENER.open(f"http://127.0.0.1:{port}/api/status?since=0",
                                 timeout=1.2) as r:
            if r.status != 200:
                return False
            data = json.loads(r.read().decode("utf-8", "ignore"))
            return "tasks" in data
    except Exception:
        return False


def running_instance(port):
    """返回在跑的实例信息（版本号），没在跑返回 None"""
    try:
        with _DIRECT_OPENER.open(f"http://127.0.0.1:{port}/api/status?since=0",
                                 timeout=1.5) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
        if "tasks" not in data:
            return None
        return {"version": data.get("version", "?"), "port": port}
    except Exception:
        return None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # 浏览器关页面/刷新时很常见，别让它刷一屏 traceback
            pass

    # -- 同源校验 ------------------------------------------------------
    def _same_origin(self, strict=False):
        """挡掉外部网页对本机端口的乱调。

        127.0.0.1 上的服务不是"外网访问不到"就安全：任意网页都能
        用 <img src>/fetch 打到本机端口。所以要校验请求来源。
        """
        site = (self.headers.get("Sec-Fetch-Site") or "").lower()
        if site in ("same-origin", "none"):
            return True
        origin = self.headers.get("Origin") or ""
        ref = self.headers.get("Referer") or ""
        pat = re.compile(r"^https?://(127\.0\.0\.1|localhost)(:\d+)?(/|$)")
        if origin:
            return bool(pat.match(origin))
        if ref:
            return bool(pat.match(ref))
        return not strict      # 地址栏直接敲（GET 导航）没有 Origin/Referer

    def _json_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except Exception:
            n = 0
        if n <= 0 or n > 8 * 1024 * 1024:
            return {}
        try:
            data = json.loads(self.rfile.read(n).decode("utf-8", "ignore") or "{}")
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    # -- GET ----------------------------------------------------------
    def do_GET(self):
        path = self.path.split("?")[0]
        qs = urllib.parse.parse_qs(self.path.split("?")[1] if "?" in self.path else "")
        try:
            if path in ("/", "/index.html"):
                self._send(200, ui_mod.HTML, "text/html; charset=utf-8")
            elif path == "/api/status":
                since = 0
                try:
                    since = int((qs.get("since") or ["0"])[0])
                except Exception:
                    pass
                self._send(200, json.dumps(api_status(since), ensure_ascii=False))
            elif path in ("/api/open", "/api/reveal"):
                if not self._same_origin(strict=True):
                    self._send(403, json.dumps({"ok": False, "msg": "来源不受信任"},
                                               ensure_ascii=False))
                    return
                target = (qs.get("path") or [""])[0]
                ok, msg = open_path(target, reveal=(path == "/api/reveal"))
                self._send(200, json.dumps({"ok": ok, "msg": msg}, ensure_ascii=False))
            elif path == "/api/health":
                self._send(200, json.dumps(api_health(), ensure_ascii=False))
            else:
                self._send(404, json.dumps({"ok": False, "msg": "not found"},
                                           ensure_ascii=False))
        except Exception as e:
            self._send(500, json.dumps({"ok": False, "msg": str(e)}, ensure_ascii=False))

    # -- POST ---------------------------------------------------------
    def do_POST(self):
        if not self._same_origin(strict=True):
            self._send(403, json.dumps({"ok": False, "msg": "forbidden"},
                                       ensure_ascii=False))
            return
        body = self._json_body()
        p = self.path.split("?")[0]
        try:
            res = self._dispatch(p, body)
        except Exception as e:
            add_log(f"接口异常 {p}：{e}")
            res = {"ok": False, "msg": str(e)}
        self._send(200, json.dumps(res, ensure_ascii=False))

    def _dispatch(self, p, body):
        cfg = load_config()
        opts = dict(dir=body.get("dir") or cfg.get("last_dir") or DEFAULT_OUTDIR,
                    limit=body.get("limit", 0),
                    audio=bool(body.get("audio", False)),
                    silence=bool(body.get("silence", False)),
                    wait_live=bool(body.get("wait_live", False)))

        if p == "/api/task/add":
            url = (body.get("url") or "").strip()
            if not url.lower().startswith("http"):
                return {"ok": False, "msg": "地址要以 http/https 开头"}
            # 手动粘地址：flv 直播流走录制，其余（含回放 mp4）走下载+解密
            low = url.lower()
            auto_kind = "live" if ("wxlivecdn" in low or ".flv" in low) else "replay"
            t = new_task(body.get("name", ""), url, opts["dir"], opts["limit"],
                         opts["audio"], opts["silence"], opts["wait_live"],
                         body.get("kind") or auto_kind,
                         body.get("decode_key", ""))
            save_config({"last_url": url, "last_dir": opts["dir"],
                         "last_name": safe_filename(body.get("name", "")),
                         "last_audio": opts["audio"], "ffmpeg": find_ffmpeg()})
            return {"ok": True, "id": t["id"], "msg": f"已加入队列 #{t['id']}"}

        if p == "/api/task/stop":
            return stop_task(body.get("id"), "手动停止")

        if p == "/api/task/retry":
            return retry_task(body.get("id"))

        if p == "/api/task/pause_all":
            return pause_all()

        if p == "/api/task/resume_all":
            return resume_all()

        if p == "/api/task/move":
            return move_task(int(body.get("id") or 0), body.get("dir") or "")

        if p == "/api/task/del":
            tid = body.get("id")
            t = task_by_id(tid)
            if not t:
                return {"ok": False, "msg": "任务不存在"}
            stop_task(tid, "删除")
            # 正在跑的先等它把文件句柄放开，否则删了记录线程还在写盘
            r = t.get("rec")
            if r and r.is_alive():
                r.join(timeout=5)
            with LOCK:
                STATE["tasks"] = [x for x in STATE["tasks"] if x["id"] != t["id"]]
                try:
                    STATE["queue"].remove(t["id"])
                except ValueError:
                    pass
                if STATE["active"] == t["id"]:
                    STATE["active"] = None
            save_tasks()
            return {"ok": True, "msg": "已移除"}

        if p == "/api/task/clear":
            keep = [x for x in STATE["tasks"]
                    if x["status"] in ("录制中", "下载中", "收尾中", "排队中",
                                       "等待开播", PAUSED)]
            n = len(STATE["tasks"]) - len(keep)
            with LOCK:
                STATE["tasks"] = keep
                ids = {x["id"] for x in keep}
                STATE["queue"] = [i for i in STATE["queue"] if i in ids]
            save_tasks()
            return {"ok": True, "n": n, "msg": f"清理了 {n} 条已结束的记录"}

        if p == "/api/task/dedup":
            return dedup_tasks(apply=bool(body.get("apply", True)))

        if p == "/api/task/queue":
            n = 0
            last = None
            for hid in (body.get("ids") or []):
                h = SNIFFER.hit_by_id(hid)
                if not h:
                    continue
                t = new_task(h.get("name", ""), h["url"], opts["dir"],
                             opts["limit"], opts["audio"], opts["silence"],
                             opts["wait_live"], h.get("kind", "live"),
                             h.get("decode_key", ""))
                SNIFFER.mark(hid, "已入队")
                last = t
                n += 1
            return {"ok": True, "n": n, "msg": f"已入队 {n} 条"}

        if p == "/api/hit/record":
            h = SNIFFER.hit_by_id(body.get("id"))
            if not h:
                return {"ok": False, "msg": "结果不存在（可能已被清理）"}
            t = new_task(h.get("name", ""), h["url"], opts["dir"], opts["limit"],
                         opts["audio"], opts["silence"], opts["wait_live"],
                         h.get("kind", "live"), h.get("decode_key", ""))
            SNIFFER.mark(body.get("id"), "已入队")
            return {"ok": True, "id": t["id"], "msg": f"已加入队列 #{t['id']}"}

        if p == "/api/hit/rename":
            ok = SNIFFER.rename(body.get("id"), body.get("name", ""))
            return {"ok": ok}

        if p == "/api/hit/del":
            SNIFFER.remove(body.get("id"))
            return {"ok": True}

        if p == "/api/hit/clear":
            n = SNIFFER.clear()
            return {"ok": True, "n": n, "msg": f"已清空 {n} 条嗅探结果"}

        if p == "/api/hit/dedup":
            n = SNIFFER.dedup()
            return {"ok": True, "n": n, "msg": f"清理了 {n} 条重复结果"}

        if p == "/api/sniff/start":
            return api_sniff_start()

        if p == "/api/sniff/stop":
            return api_sniff_stop()

        if p == "/api/sniff/poll":
            SNIFFER.poll()
            return {"ok": True}

        if p == "/api/app/restart":
            return api_restart()

        if p == "/api/app/shutdown":
            add_log("收到退出指令，正在关闭…")
            threading.Thread(target=_shutdown, daemon=True).start()
            return {"ok": True, "msg": "正在退出"}

        if p == "/api/config":
            save_config({"last_dir": body.get("dir") or cfg.get("last_dir") or "",
                         "ffmpeg": body.get("ffmpeg") or find_ffmpeg()})
            return {"ok": True, "msg": "已保存"}

        return {"ok": False, "msg": "未知接口"}


def api_health():
    """给前端的"体检"结果：缺什么就直说，别让按钮点了没反应还找不到原因"""
    items = []
    ff = find_ffmpeg()
    items.append({"name": "ffmpeg", "ok": has_ffmpeg(), "detail": ff,
                  "hint": "没有 ffmpeg 就无法转封装/导出 MP3/抽封面"})
    ok_node, node_msg = wxdecrypt.available()
    items.append({"name": "node（回放解密）", "ok": ok_node, "detail": node_msg,
                  "hint": "回放文件头加密，需要 node 加载官方 wasm 才能解密"})
    mitm = sniff_mod.find_mitmdump()
    items.append({"name": "mitmdump（嗅探）", "ok": bool(mitm), "detail": mitm or "未找到",
                  "hint": "没有它就嗅探不到地址，只能手动粘链接"})
    outdir = load_config().get("last_dir") or DEFAULT_OUTDIR
    free = disk_free(outdir if os.path.isdir(outdir) else APP_DIR)
    items.append({"name": "磁盘空间", "ok": free < 0 or free > 1024 ** 3,
                  "detail": ("%.1f GB 可用" % (free / 1024 ** 3)) if free >= 0 else "未知",
                  "hint": "剩余空间不足 1GB 时长录制会失败"})
    return {"ok": True, "version": VERSION, "build": BUILD,
            "port": PORT, "items": items,
            "dir": outdir, "uptime": int(time.time() - STATE["started_at"])}


def api_status(since):
    drain_rec_logs()
    now = time.time()
    tasks = []
    for t in list(STATE["tasks"]):          # 快照遍历，避免与调度线程并发修改冲突
        if t["status"] in RUNNING_STATES and t.get("t0"):
            t["elapsed"] = now - t["t0"]
            r = t.get("rec")
            if r:
                t["bytes"] = r.bytes
        tasks.append(public_task(t))
    with LOCK:
        logs = [x for i, x in STATE["logs"] if i >= since]
        nxt = STATE["log_seq"]
    sn = SNIFFER.status()
    warnings = []
    if not has_ffmpeg():
        warnings.append("找不到 ffmpeg：转封装、MP3、封面都用不了")
    if not wxdecrypt.available()[0]:
        warnings.append("回放解密不可用（缺 node.exe）")
    if sn.get("state") == "已中断":
        warnings.append("嗅探进程已退出，系统代理已自动还原")
    return {
        "version": VERSION, "build": BUILD,
        "tasks": tasks, "active": STATE["active"], "queued": len(STATE["queue"]),
        "queue": list(STATE["queue"]),
        "dir": load_config().get("last_dir") or DEFAULT_OUTDIR,
        "default_dir": DEFAULT_OUTDIR,
        "sniff": sn, "logs": logs, "next": nxt,
        "warnings": warnings,
        "uptime": int(now - STATE["started_at"]),
        "restart_count": STATE["restart_count"],
    }


# ----------------------------------------------------------------- 重启 / 退出
def _shutdown():
    try:
        save_tasks()
    except Exception:
        pass
    try:
        SNIFFER.stop(restore=True)
    except Exception:
        pass
    time.sleep(0.3)
    os._exit(0)


def api_restart():
    """重启服务：起一个新进程，然后自己退出。

    新进程带 --from-restart：它不去做"已经有一个在跑"的检查，
    而是轮询等端口释放（旧进程要 1 秒后才真正退出）。
    """
    save_tasks()
    try:
        SNIFFER.stop(restore=True)
    except Exception:
        pass
    STATE["restart_count"] += 1
    script = os.path.abspath(__file__)
    args = [sys.executable, script, "--no-browser", "--from-restart"]
    kw = {"close_fds": True, "cwd": APP_DIR}
    if os.name == "nt":
        # 脱离当前进程，否则父进程退出会把子进程一起带走
        kw["creationflags"] = (subprocess.DETACHED_PROCESS
                               | subprocess.CREATE_NEW_PROCESS_GROUP)
    try:
        subprocess.Popen(args, **kw)
    except Exception as e:
        return {"ok": False, "msg": f"启动新进程失败：{e}（可以用 启动录制器.bat 手动重开）"}
    add_log("🔄 正在重启服务…")

    def later():
        time.sleep(1.0)
        os._exit(0)

    threading.Thread(target=later, daemon=True).start()
    return {"ok": True, "msg": "正在重启，页面会自动刷新"}


# ----------------------------------------------------------------- 启动
def serve():
    from_restart = "--from-restart" in sys.argv
    no_browser = "--no-browser" in sys.argv

    threading.Thread(target=manager, daemon=True).start()

    add_log(f"视频号下载器 v{VERSION} 启动")
    add_log(f"ffmpeg: {find_ffmpeg()}")
    ok_node, node_msg = wxdecrypt.available()
    add_log(f"回放解密：{'就绪' if ok_node else '不可用 — ' + node_msg}")

    if sniff_mod.restore_stale_proxy():
        add_log("上次退出时系统代理没恢复，已自动修好")

    # 回放的 decode_key 只能从页面的 WeixinJSBridge 拿，起一个接收端
    try:
        bp = bridge_server.start()
        if bp:
            add_log(f"JSBridge 接收端：127.0.0.1:{bp}")
        else:
            add_log("⚠ JSBridge 接收端端口都被占用，回放可能抓不到 decode_key")
    except Exception as e:
        add_log(f"⚠ JSBridge 接收端启动失败：{e}")

    # 嗅探统计：上次遗留了多少结果
    try:
        left = len(SNIFFER.hits)
        if left:
            add_log(f"嗅探结果列表里有上次留下的 {left} 条，可直接使用")
    except Exception:
        pass

    load_tasks()
    threading.Thread(target=sniff_watch, daemon=True).start()
    atexit.register(lambda: (save_tasks(), SNIFFER.stop(restore=True)))

    # 端口：先看有没有旧实例
    port = PORT
    if not from_restart:
        info = running_instance(port)
        if info and info["version"] != VERSION:
            print(f"检测到旧版本 v{info['version']} 正在 {port} 端口运行。"
                  f"请在页面上点「重启服务」，或先关闭它再启动本版本 v{VERSION}。")
        elif info:
            url = f"http://127.0.0.1:{port}/"
            print("已经有一个在跑，直接复用：", url)
            if not no_browser:
                webbrowser.open(url)
            return

    httpd = None
    for i in range(40 if from_restart else 10):
        try:
            httpd = RecorderServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            # 重启时旧进程还占着端口，等它退干净
            time.sleep(0.4 if from_restart else 0.05)
            port = port if from_restart else port + 1
    if httpd is None:
        print("端口都被占用了，退出")
        return

    url = f"http://127.0.0.1:{port}/"
    add_log(f"服务就绪：{url}")
    print("录制器已启动：", url)
    if not no_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        save_tasks()
        try:
            SNIFFER.stop(restore=True)
        except Exception:
            pass


def run_cli():
    url = sys.argv[2]
    dur = float(sys.argv[3]) if len(sys.argv) > 3 else 0
    outdir = DEFAULT_OUTDIR
    os.makedirs(outdir, exist_ok=True)
    flv = os.path.join(outdir, f"视频号直播_{time.strftime('%Y%m%d_%H%M%S')}.flv")
    r = Recorder(url, flv)
    t0 = time.time()
    r.start()
    print("输出:", flv)
    try:
        while r.is_alive():
            time.sleep(1)
            el = time.time() - t0
            print(f"\r{int(el):>3}s  {r.bytes / 1048576:6.1f} MB  {r.state}",
                  end="", flush=True)
            while not r.logs.empty():
                print("\n  " + r.logs.get(), end="")
            if dur and el >= dur:
                r.stop()
                break
    except KeyboardInterrupt:
        r.stop()
    r.join(timeout=30)
    print()
    if os.path.isfile(flv) and os.path.getsize(flv) > 10240:
        mp4 = flv[:-4] + ".mp4"
        ok, err = ffmpeg_remux(flv, mp4)
        print(("完成: " + mp4) if ok else ("转封装失败: " + err[:200]))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--cli" and len(sys.argv) > 2:
        run_cli()
    elif len(sys.argv) > 1 and sys.argv[1] == "--version":
        print(VERSION)
    else:
        serve()
