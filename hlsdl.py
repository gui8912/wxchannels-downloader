# -*- coding: utf-8 -*-
"""HLS / m3u8 下载模块 —— 对 N_m3u8DL-RE.exe 的封装
============================================================================
为什么单独拆一个模块：
    主程序 90 多 KB 已经够长了，HLS 下载是"另一条产线"，逻辑上跟视频号的
    直播录制/回放解密没有交集。拆出来以后 ui.py 加标签页、这里加能力，
    互不影响。

这个模块负责四件事：
    1. 找得到 N_m3u8DL-RE.exe，并读出版本号（找不到要说清楚，别让按钮点了没反应）
    2. 「粘贴地址 → 先探一次」：把播放列表抓下来，告诉用户切片多少、多长、
       是不是加密、密钥在哪，以及 403 到底是被谁拦的（UA / Referer / auth_key）
    3. 起子进程下载，实时解析进度条
    4. 停止时按 PID 杀进程树 —— 绝不按进程名全局杀，避免误伤用户自己开的程序

关于"自动解密"：
    HLS 的 AES-128 / SAMPLE-AES，N_m3u8DL-RE 会自己按 #EXT-X-KEY 里的 URI
    去取密钥再解，不需要我们插手；`-H` 传的请求头它也会带给取密钥的请求。
    只有一种情况会失败：密钥接口要 POST、要登录态、或者要特殊的 body ——
    这时界面上"手动密钥"填进去走 --key 就行。
============================================================================
"""
import atexit
import codecs
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
CREATE_NO_WINDOW = 0x08000000

# 浏览器 UA 兜底值。很多 CDN 把非浏览器 UA 直接拉黑（403 denied by UA ACL）
UA_DEFAULT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# N_m3u8DL-RE 的常见落点，找不到再退到 PATH
RE_CANDIDATES = (
    r"D:\@my.program.files\m3u8dl\N_m3u8DL-RE.exe",
    r"D:\WorkBuddy\工具\m3u8dl\N_m3u8DL-RE.exe",
    os.path.join(APP_DIR, "N_m3u8DL-RE.exe"),
    os.path.join(APP_DIR, "bin", "N_m3u8DL-RE.exe"),
)

FINAL_STATES = ("已完成", "失败", "已停止")
RUNNING_STATES = ("下载中", "合并中")

LOCK = threading.RLock()
STATE = {
    "jobs": [],          # 任务列表，新的在前
    "logs": [],          # (seq, text)
    "seq": 0,
    "exe": "",           # 当前使用的可执行文件
    "worker": None,      # 队列线程
    "seq_id": 0,
}

_LOG_CAP = 600
# 进度条形如：Vid Kbps ---- 794/794 100.00%295.63MB - 00:00:00
RE_PROG = re.compile(r"(\d+)\s*/\s*(\d+)\s+([\d.]+)%")
RE_PROG_LABEL = re.compile(r"(?:Vid|Aud|Sub)\s*Kbps")
RE_SIZE = re.compile(r"([\d.]+)\s*([KMG]?B)\s*/\s*([\d.]+)\s*([KMG]?B)")
RE_SPEED = re.compile(r"([\d.]+)\s*([KMG]?B)\s*(?:ps|/s)")
# ETA 是 00:00:42；日志时间戳是 01:20:08.320，靠后面那个点区分开
RE_ETA = re.compile(r"(?<![\d.])(\d{2}:\d{2}:\d{2})(?![\d.])")
# 行首标记：日志时间戳 / 进度条标签。重定向后没有换行符，只能靠它切段
RE_TS = re.compile(r"(?=\d{2}:\d{2}:\d{2}[.,]\d{3}\s+(?:INFO|WARN|ERROR|DEBUG|TRACE)\s*:)"
                   r"|(?=(?:Vid|Aud|Sub)\s*Kbps)")
RE_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


# ------------------------------------------------------------------ 基础工具
def load_cfg():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def log(msg):
    """写进本模块自己的日志环，前端 m3u8 标签页单独显示"""
    with LOCK:
        STATE["seq"] += 1
        STATE["logs"].append((STATE["seq"], msg))
        if len(STATE["logs"]) > _LOG_CAP:
            del STATE["logs"][:len(STATE["logs"]) - _LOG_CAP]


def _sanitize(name, limit=120):
    """文件名清洗：Windows 不允许的字符一律换掉，尾部的点和空格也要去掉"""
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", str(name or "")).strip()
    s = s.rstrip(" .")
    return s[:limit] or ""


def _origin(url):
    """取 https://host/ 形式，用来给防盗链当 Referer"""
    try:
        p = urllib.parse.urlsplit(url)
        if p.scheme and p.netloc:
            return f"{p.scheme}://{p.netloc}/"
    except Exception:
        pass
    return ""


def parse_headers(txt):
    """把「每行一条 Key: Value」的文本解析成字典，供 -H 使用"""
    d = {}
    for line in (txt or "").replace("\r", "\n").split("\n"):
        if ":" in line:
            k, v = line.split(":", 1)
            if k.strip():
                d[k.strip()] = v.strip()
    return d


# ------------------------------------------------------------------ 可执行文件
def find_exe():
    for p in (load_cfg().get("re_exe") or "",) + RE_CANDIDATES:
        if p and os.path.isfile(p):
            return p
    for n in ("N_m3u8DL-RE", "N_m3u8DL-RE.exe", "n_m3u8dl-re"):
        w = shutil.which(n)
        if w:
            return w
    return ""


def _run_quiet(args, timeout=12, **kw):
    """跑一个短命令，返回 (rc, text)。输出是 GBK —— 这个工具在中文 Windows 上
    按代码页 936 写管道，用 utf-8 解会全是问号。"""
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW, **kw)
        raw = (p.stdout or b"") + (p.stderr or b"")
        return p.returncode, raw.decode("gbk", "replace")
    except Exception as e:
        return -1, str(e)


def exe_info(path=""):
    """校验一个 exe 能不能用，顺手拿版本号"""
    path = path or find_exe()
    if not path or not os.path.isfile(path):
        return {"ok": False, "path": "", "version": "",
                "msg": "没找到 N_m3u8DL-RE.exe，请在上面填它的完整路径"}
    rc, out = _run_quiet([path, "--version"], timeout=15)
    m = re.search(r"(\d+\.\d+\.\d+(?:\.\d+)?)", out or "")
    ver = m.group(1) if m else ""
    # 老版本没有 --version 会直接报参数错误，但能跑出帮助也算可用
    if rc == 0 or "Usage" in out or "Description" in out or ver:
        return {"ok": True, "path": path, "version": ver or "未知", "msg": "可用"}
    return {"ok": False, "path": path, "version": "", "msg": (out or "无法执行").strip()[:300]}


def set_exe(path):
    path = (path or "").strip().strip('"')
    info = exe_info(path)
    if info["ok"]:
        with LOCK:
            STATE["exe"] = path
    return info


# ------------------------------------------------------------------ 探测
def _http_get(url, headers, timeout=12, limit=4 * 1024 * 1024):
    """直连抓取。不走系统代理 —— 嗅探开着的时候系统代理指向 mitmproxy，
    下载不应该绕那一圈。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, headers=headers)
    r = opener.open(req, timeout=timeout)
    return r.status, r.headers, r.read(limit)


def _explain_403(code, tengine, body, url):
    """把 CDN 的拒绝翻译成人话 + 可执行的下一步。

    Tengine 会把理由写在响应头和 HTML 正文里，这是我上次排查 ciopharma
    那个站时踩出来的：503 说"没权限"根本没用，得看 denied by 后面那半句。
    """
    blob = f"{tengine} {body[:3000]}".lower()
    if "referer" in blob:
        return ("被 Referer 防盗链拦了（denied by Referer ACL）：这个 CDN 要求请求必须带 "
                "Referer，把它填成视频所在网页的地址再试")
    if "ua acl" in blob or "user-agent" in blob or "denied by ua" in blob:
        return "这个 CDN 把当前 User-Agent 拉黑了（denied by UA ACL）：换成浏览器 UA 再试"
    if ("invalid md5hash" in blob or "req auth" in blob or "auth_key" in blob
            or "authkey" in blob or "invalid signature" in blob or "expired" in blob):
        return ("链接自带的校验签名（auth_key / md5hash）无效或已过期："
                "回原网页重新复制一条完整地址，问号后面的参数一个都不能少")
    if "refer" in blob:
        return "被防盗链拦了：检查 Referer 是否填对"
    if code == 401:
        return "需要登录（401）：这条地址要求带登录态 Cookie"
    if code == 404:
        return "地址不存在（404）：可能已被删除，或者路径复制少了"
    if code == 403:
        return "服务器拒绝访问（403）：多半是防盗链、地区限制或登录态，把 Referer / UA / Cookie 补上再试"
    if code >= 500:
        return f"服务端出错（{code}）：过一会儿再试，或回网页重新复制地址"
    return f"HTTP {code}"


def _parse_playlist(text):
    """解析 m3u8。返回 dict；不是 m3u8 返回 None。"""
    if "#EXTM3U" not in text[:2048]:
        return None
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    master = any(l.startswith("#EXT-X-STREAM-INF") for l in lines)
    info = {"master": master, "variants": [], "segments": 0,
            "duration": 0.0, "encrypted": False, "method": "NONE",
            "key_uri": "", "key_host": "", "live": False}
    if master:
        cur = None
        for l in lines:
            if l.startswith("#EXT-X-STREAM-INF"):
                cur = {"bandwidth": 0, "resolution": "", "codecs": ""}
                m = re.search(r"BANDWIDTH=(\d+)", l)
                if m:
                    cur["bandwidth"] = int(m.group(1))
                m = re.search(r"RESOLUTION=([0-9x]+)", l)
                if m:
                    cur["resolution"] = m.group(1)
                m = re.search(r'CODECS="([^"]+)"', l)
                if m:
                    cur["codecs"] = m.group(1)
            elif l.startswith("#"):
                continue
            elif cur is not None:
                cur["url"] = l
                info["variants"].append(cur)
                cur = None
        return info
    dur = 0.0
    seg = 0
    for l in lines:
        if l.startswith("#EXT-X-KEY"):
            m = re.search(r"METHOD=([A-Z0-9_\-]+)", l)
            method = (m.group(1) if m else "NONE").upper()
            if method != "NONE":
                info["encrypted"] = True
                info["method"] = method
                mu = re.search(r'URI="([^"]+)"', l)
                if mu:
                    info["key_uri"] = mu.group(1)
                    info["key_host"] = urllib.parse.urlsplit(mu.group(1)).netloc
        elif l.startswith("#EXTINF"):
            seg += 1
            m = re.match(r"#EXTINF:\s*([\d.]+)", l)
            if m:
                try:
                    dur += float(m.group(1))
                except ValueError:
                    pass
        elif l.startswith("#EXT-X-ENDLIST"):
            info["live"] = False
    info["segments"] = seg
    info["duration"] = dur
    info["live"] = not any(l.startswith("#EXT-X-ENDLIST") for l in lines)
    return info


def probe(url, referer="", ua="", headers=None):
    """探测一条 m3u8：先看能不能取到、再解析内容。

    返回的 msg 是给用户看的一句话结论；fix 是如果失败，建议怎么改。
    """
    url = (url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return {"ok": False, "msg": "地址要以 http:// 或 https:// 开头"}
    h = {"User-Agent": ua or UA_DEFAULT, "Accept": "*/*"}
    if referer:
        h["Referer"] = referer
    if headers:
        h.update(headers)

    auto_referer = ""
    for attempt in (1, 2):
        try:
            code, hdrs, raw = _http_get(url, h)
            break
        except urllib.error.HTTPError as e:
            tengine = e.headers.get("X-Tengine-Error", "") if e.headers else ""
            try:
                body = e.read(3000).decode("utf-8", "replace")
            except Exception:
                body = ""
            # 没填 Referer 又正好被 Referer 防盗链拦：自动拿同域地址再试一次。
            # 绝大多数站点的规则就是"必须带 Referer"，带同域的就放行了。
            if (attempt == 1 and not referer and e.code == 403
                    and "referer" in f"{tengine} {body}".lower()):
                auto_referer = _origin(url)
                if auto_referer:
                    h["Referer"] = auto_referer
                    continue
            return {"ok": False, "code": e.code,
                    "msg": _explain_403(e.code, tengine, body, url),
                    "fix": "referer" if "referer" in f"{tengine} {body}".lower() else "",
                    "auto_referer": auto_referer}
        except Exception as e:
            return {"ok": False, "msg": f"连不上：{type(e).__name__} {e}"}
    else:
        return {"ok": False, "msg": "取不到播放列表"}

    text = raw.decode("utf-8", "replace")
    if text.lstrip()[:1] not in ("#", "\ufeff", "\n", " ") and "EXTM3U" not in text[:2048]:
        # 不是文本播放列表：可能是直链 MP4，或者干脆是张错误图
        head = raw[:16]
        if head[4:8] == b"ftyp":
            return {"ok": True, "kind": "mp4", "code": code, "auto_referer": auto_referer,
                    "msg": "这不是 m3u8，而是一个直链 MP4 —— 用浏览器直接下就行，不用走这里"}
        return {"ok": False, "code": code,
                "msg": f"返回的不是播放列表（Content-Type: {hdrs.get('Content-Type', '未知')}）"}

    info = _parse_playlist(text)
    if not info:
        return {"ok": False, "code": code, "msg": "内容不像合法的 m3u8（缺少 #EXTM3U）"}

    # 多码率主列表：挑最高码率那一路再探一次，拿到真实的切片数和时长
    detail = info
    if info["master"] and info["variants"]:
        best = max(info["variants"], key=lambda v: v.get("bandwidth") or 0)
        try:
            base = url.rsplit("/", 1)[0] + "/"
            sub_url = urllib.parse.urljoin(base, best["url"])
            sub_url = urllib.parse.urljoin(url, best["url"])
            c2, _, raw2 = _http_get(sub_url, h)
            detail = _parse_playlist(raw2.decode("utf-8", "replace")) or info
            detail["master"] = True
            detail["variants"] = info["variants"]
            best_url = sub_url
        except Exception:
            best_url = ""
    else:
        best_url = url

    parts = []
    if info["master"]:
        parts.append(f"{len(info['variants'])} 路码率")
        if detail.get("segments"):
            parts.append(f"{detail['segments']} 个切片")
    else:
        parts.append(f"{info['segments']} 个切片")
    dur = detail.get("duration") or info.get("duration") or 0
    if dur:
        parts.append(f"约 {int(dur // 60)} 分 {int(dur % 60)} 秒")
    if info.get("live"):
        parts.append("直播流（无 ENDLIST）")
    if detail.get("encrypted"):
        parts.append(f"{detail.get('method')} 加密，自动解密")
    else:
        parts.append("未加密")
    return {
        "ok": True, "code": code, "auto_referer": auto_referer,
        "msg": " · ".join(parts),
        "info": {k: detail.get(k) for k in
                 ("master", "segments", "duration", "encrypted", "method",
                  "key_uri", "key_host", "live", "variants")},
        "best_url": best_url,
    }


# ------------------------------------------------------------------ 任务
class Job(object):
    def __init__(self, p):
        self.id = 0
        self.url = p["url"]
        self.name = p["name"]
        self.dir = p["dir"]
        self.fmt = p["fmt"]
        self.status = "排队中"
        self.pct = 0.0
        self.seg_done = 0
        self.seg_total = 0
        self.size = ""
        self.total = ""
        self.speed = ""
        self.eta = ""
        self.out = ""
        self.err = ""
        self.note = ""
        self.t0 = 0.0
        self.t1 = 0.0
        self.proc = None
        self.stopped = False
        self.params = p

    def pub(self):
        return {
            "id": self.id, "name": self.name, "url": self.url,
            "dir": self.dir, "fmt": self.fmt, "status": self.status,
            "pct": round(self.pct, 1), "seg_done": self.seg_done,
            "seg_total": self.seg_total, "size": self.size, "total": self.total,
            "speed": self.speed, "eta": self.eta, "out": self.out,
            "err": self.err, "note": self.note,
            "elapsed": int((self.t1 or time.time()) - self.t0) if self.t0 else 0,
        }


def job_by_id(jid):
    try:
        jid = int(jid)
    except (TypeError, ValueError):
        return None
    for t in STATE["jobs"]:
        if t.id == jid:
            return t
    return None


def build_args(job):
    exe = STATE["exe"] or find_exe()
    p = job.params
    args = [exe, job.url,
            "--save-dir", job.dir,
            "--save-name", job.name,
            "--no-ansi-color",              # 去掉颜色控制符，日志才好读
            "--disable-update-check",       # 别每次启动都去问版本
            "--del-after-done", "true" if p.get("del_after_done", True) else "false"]
    if p.get("threads"):
        args += ["--thread-count", str(int(p["threads"]))]
    if p.get("retry"):
        args += ["--download-retry-count", str(int(p["retry"]))]
    if p.get("timeout"):
        args += ["--http-request-timeout", str(int(p["timeout"]))]
    if p.get("auto_select", True):
        args += ["--auto-select"]
    if p.get("concurrent"):
        args += ["-mt"]
    if p.get("skip_merge"):
        args += ["--skip-merge"]
    if p.get("max_speed"):
        args += ["-R", str(p["max_speed"])]
    if p.get("key"):
        args += ["--key", str(p["key"])]
    if p.get("del_after_done", True) is False:
        pass
    if not p.get("use_system_proxy", True):
        args += ["--use-system-proxy", "false"]
    if p.get("fmt"):
        args += ["-M", "format=" + job.fmt]
    ff = p.get("ffmpeg") or ""
    if ff and os.path.isfile(ff):
        args += ["--ffmpeg-binary-path", ff]
    heads = p.get("headers") or {}
    for k, v in heads.items():
        if k and v:
            args += ["-H", f"{k}: {v}"]
    extra = (p.get("extra") or "").strip()
    if extra:
        # 只做简单的按空格切分，引号内的空格保留 —— 够用，也比引 shlex 少一个坑
        for tok in re.findall(r'"[^"]*"|\'[^\']*\'|\S+', extra):
            args.append(tok.strip("\"'"))
    return args


def _pump(proc, job):
    """读子进程输出。

    这里有个坑：N_m3u8DL-RE 的输出被重定向到管道时**不写换行符**，
    进度条靠光标回车原地刷新，而回车在非终端下被丢掉了 —— 于是
    "开始下载...Vid Kbps---- 1/794 0.13%..." 全挤成一行。按 \\r\\n 切行
    永远切不出来，进度就永远是 0（这个 bug 我实测踩到过）。

    所以改按它自己的行首标记切段：每行日志都以 `HH:MM:SS.mmm LEVEL :` 开头，
    进度条以 `Vid/Aud/Sub Kbps` 开头。用前瞻式 split，标记留在段首。
    """
    dec = codecs.getincrementaldecoder("gbk")("replace")
    buf = ""
    while True:
        try:
            chunk = proc.stdout.read(1024)
        except Exception:
            break
        if not chunk:
            break
        buf += dec.decode(chunk)
        # 进度：整段扫一遍取最后一条（进度条会被覆盖，最后一条才是当前值）
        _scan_progress(job, buf)
        parts = RE_TS.split(buf)
        if len(parts) > 1:
            for seg in parts[:-1]:
                _handle_seg(job, seg)
            buf = parts[-1]
        if len(buf) > 20000:          # 只是防止单段无限膨胀
            buf = buf[-4000:]
    for seg in RE_TS.split(buf):
        _handle_seg(job, seg)


def _is_progress(text):
    if text.startswith(("Vid", "Aud", "Sub")):
        return True
    return bool(RE_PROG.search(text)) and not re.match(r"^\d{2}:\d{2}:\d{2}", text)


def _handle_seg(job, seg):
    seg = RE_ANSI.sub("", seg).strip()
    if not seg or _is_progress(seg):
        return
    _on_line(job, seg)


def _scan_progress(job, text):
    """从缓冲区里抓当前进度。

    每个进度状态都以 `Vid Kbps` 开头，取最后一段就是最新值。
    """
    chunks = RE_PROG_LABEL.split(text)
    chunk = chunks[-1] if len(chunks) > 1 else text
    m = RE_PROG.search(chunk)
    if not m:
        return False
    job.seg_done = int(m.group(1))
    job.seg_total = int(m.group(2))
    try:
        job.pct = float(m.group(3))
    except ValueError:
        pass
    ms = RE_SIZE.search(chunk)
    if ms:
        job.size = f"{ms.group(1)}{ms.group(2)}"
        job.total = f"{ms.group(3)}{ms.group(4)}"
    mv = RE_SPEED.search(chunk)
    if mv:
        job.speed = f"{mv.group(1)}{mv.group(2)}/s"
    me = RE_ETA.search(chunk)
    if me:
        job.eta = me.group(1)
    if job.status == "排队中":
        job.status = "下载中"
    return True


def _on_line(job, line):
    up = line.lower()
    # 去掉行首的 `01:20:08.320 INFO : `，只看正文，不然关键词全是误判
    core = re.sub(r"^\d{2}:\d{2}:\d{2}[.,]\d{3}\s+\w+\s*:\s*", "", line).strip()
    low = core.lower()
    # 注意别用 "合并"/"下载完成" 这种宽匹配：日志里有一句
    # "你已开启下载完成后混流，自动开启二进制合并"，两个词都在里面，
    # 一上来就会把状态误判成「合并中」甚至「已完成」。
    if "muxing to" in low or core.startswith(("开始合并", "正在合并", "混流中")):
        job.status = "合并中"
        job.pct = 100.0
    elif core in ("Done", "下载完成") or core.startswith("Done"):
        job.status = "已完成"
        job.pct = 100.0
    elif "error" in up or "exception" in up or core.startswith("失败"):
        job.err = job.err or core[:400]
    if line == getattr(job, "_last_log", ""):
        return
    job._last_log = line
    log(f"[{job.id}] {line}")


def _find_output(job):
    """下载完成后定位成品。优先 名称.格式，其次同名的新文件。"""
    want = os.path.normpath(os.path.join(job.dir, f"{job.name}.{job.fmt}"))
    if os.path.isfile(want):
        return want
    try:
        cands = [os.path.join(job.dir, f)
                 for f in os.listdir(job.dir)
                 if f.lower().startswith(job.name.lower())]
        media = [c for c in cands
                 if os.path.splitext(c)[1].lower() in (".mp4", ".mkv", ".ts", ".m4a", ".flv", ".mov")]
        if media:
            return os.path.normpath(max(media, key=os.path.getmtime))
    except Exception:
        pass
    return ""


def _run_job(job):
    exe = STATE["exe"] or find_exe()
    if not exe:
        job.status = "失败"
        job.err = "没找到 N_m3u8DL-RE.exe"
        job.note = job.err
        log(f"✘ [{job.id}] {job.err}")
        return
    try:
        os.makedirs(job.dir, exist_ok=True)
    except Exception as e:
        job.status = "失败"
        job.err = f"保存目录建不出来：{e}"
        log(f"✘ [{job.id}] {job.err}")
        return
    args = build_args(job)
    job.status = "下载中"
    job.t0 = time.time()
    log(f"▶ [{job.id}] 开始下载 {job.name}")
    log(f"  命令行：{' '.join(a if ' ' not in a else chr(34) + a + chr(34) for a in args[:1] + args[1:9])} …")
    try:
        proc = subprocess.Popen(args, cwd=job.dir, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                creationflags=CREATE_NO_WINDOW)
    except Exception as e:
        job.status = "失败"
        job.err = f"启动失败：{e}"
        log(f"✘ [{job.id}] {job.err}")
        return
    job.proc = proc
    try:
        _pump(proc, job)
        rc = proc.wait(timeout=30)
    except Exception as e:
        rc = -1
        job.err = job.err or f"读取输出出错：{e}"
    job.t1 = time.time()
    job.proc = None

    if job.stopped:
        job.status = "已停止"
        log(f"■ [{job.id}] 已停止")
        return
    if rc == 0 and job.status != "失败":
        job.status = "已完成"
        job.pct = 100.0
        job.out = _find_output(job)
        log(f"✔ [{job.id}] 完成：{job.out or job.dir}")
    else:
        job.status = "失败"
        raw = (job.err or f"N_m3u8DL-RE 退出码 {rc}").strip()
        # 403 时再探一次地址：只给用户看一句 "403 (Forbidden)" 等于没说，
        # 探一次才知道到底是 Referer、UA 还是 auth_key 签名的问题。
        if "403" in raw or "404" in raw:
            heads = job.params.get("headers") or {}
            try:
                pr = probe(job.url, heads.get("Referer", ""), heads.get("User-Agent", ""))
                if not pr.get("ok") and pr.get("msg"):
                    raw = pr["msg"]
            except Exception:
                pass
        job.err = raw.splitlines()[0][:300]
        job.note = job.err
        log(f"✘ [{job.id}] 失败：{job.err}")


def _worker():
    while True:
        job = None
        with LOCK:
            for t in STATE["jobs"]:
                if t.status == "排队中" and not t.stopped:
                    job = t
                    break
        if not job:
            time.sleep(0.5)
            continue
        try:
            _run_job(job)
        except Exception as e:
            job.status = "失败"
            job.err = f"异常：{e}"
            log(f"✘ [{job.id}] 异常：{e}")


def _ensure_worker():
    with LOCK:
        w = STATE["worker"]
        if not w or not w.is_alive():
            w = threading.Thread(target=_worker, daemon=True, name="hls-worker")
            STATE["worker"] = w
            w.start()


def add(p):
    """新建任务。p 来自前端，字段全部可选，这里统一兜底。"""
    url = (p.get("url") or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return {"ok": False, "msg": "地址要以 http:// 或 https:// 开头"}
    if "://" in url and not re.search(r"\.m3u8(\?|$)", url, re.I) and "?" not in url:
        # 不强制，只提示 —— 有些站点把 m3u8 藏在奇怪的路径里
        if not url.lower().endswith((".m3u8", ".m3u")):
            pass

    exe = STATE["exe"] or find_exe()
    if not exe:
        return {"ok": False, "msg": "没找到 N_m3u8DL-RE.exe：先在「程序路径」里填完整路径并点检测"}

    outdir = (p.get("dir") or "").strip() or os.path.join(APP_DIR, "下载")
    outdir = os.path.normpath(outdir)
    fmt = (p.get("fmt") or "mp4").strip().lower()
    if fmt not in ("mp4", "mkv", "ts"):
        fmt = "mp4"

    name = _sanitize(p.get("name"))
    if not name:
        name = "m3u8_" + time.strftime("%Y%m%d_%H%M%S")

    headers = {}
    ref = (p.get("referer") or "").strip()
    if not ref:
        # 没填就给同域地址：绝大多数 CDN 的 Referer 白名单就是"同域即可"
        ref = _origin(url)
    if ref:
        headers["Referer"] = ref
    ua = (p.get("ua") or "").strip() or UA_DEFAULT
    headers["User-Agent"] = ua
    headers.update(parse_headers(p.get("extra_headers")))

    params = {
        "url": url, "name": name, "dir": outdir, "fmt": fmt,
        "headers": headers,
        "threads": p.get("threads") or 20,
        "retry": p.get("retry") if p.get("retry") is not None else 3,
        "timeout": p.get("timeout") or 100,
        "auto_select": bool(p.get("auto_select", True)),
        "concurrent": bool(p.get("concurrent", False)),
        "skip_merge": bool(p.get("skip_merge", False)),
        "del_after_done": bool(p.get("del_after_done", True)),
        "max_speed": (p.get("max_speed") or "").strip(),
        "key": (p.get("key") or "").strip(),
        "use_system_proxy": bool(p.get("use_system_proxy", True)),
        "extra": p.get("extra") or "",
        "ffmpeg": p.get("ffmpeg") or "",
    }
    with LOCK:
        STATE["seq_id"] += 1
        j = Job(params)
        j.id = STATE["seq_id"]
        STATE["jobs"].insert(0, j)
        del STATE["jobs"][40:]
    _ensure_worker()
    log(f"＋ [{j.id}] 已加入队列：{name} → {outdir}")
    return {"ok": True, "id": j.id, "msg": f"已开始 #{j.id}（{name}）"}


def _kill_tree(pid):
    """只杀自己起的这个进程树。

    绝对不能 taskkill /IM N_m3u8DL-RE.exe —— 用户可能自己也开着这个工具在下载，
    一刀切会把他的任务一起杀掉。按 PID + /T 才是安全的收尾方式。
    """
    if not pid:
        return
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, timeout=15,
                       creationflags=CREATE_NO_WINDOW)
    except Exception:
        try:
            os.kill(pid, 9)
        except Exception:
            pass


def stop(jid):
    j = job_by_id(jid)
    if not j:
        return {"ok": False, "msg": "任务不存在"}
    j.stopped = True
    if j.proc and j.proc.poll() is None:
        _kill_tree(j.proc.pid)
    if j.status in ("排队中", "下载中", "合并中"):
        j.status = "已停止"
        j.t1 = time.time()
    return {"ok": True, "msg": f"已停止 #{j.id}"}


def remove(jid):
    j = job_by_id(jid)
    if not j:
        return {"ok": False, "msg": "任务不存在"}
    stop(jid)
    with LOCK:
        try:
            STATE["jobs"].remove(j)
        except ValueError:
            pass
    return {"ok": True, "msg": "已移除"}


def clear():
    with LOCK:
        keep = [t for t in STATE["jobs"] if t.status in RUNNING_STATES or t.status == "排队中"]
        n = len(STATE["jobs"]) - len(keep)
        STATE["jobs"] = keep
    return {"ok": True, "n": n, "msg": f"清理了 {n} 条已结束的任务"}


def status():
    drain = list(STATE["logs"])
    with LOCK:
        jobs = [t.pub() for t in STATE["jobs"]]
        logs = [x for _, x in drain]
        nxt = STATE["seq"]
        STATE["logs"] = []
    return {"jobs": jobs, "logs": logs[-200:], "next": nxt,
            "exe": STATE["exe"] or find_exe(),
            "dir": load_cfg().get("hls_dir") or os.path.join(APP_DIR, "下载"),
            "busy": any(t["status"] in RUNNING_STATES for t in jobs)}


def output_dirs():
    """本模块产生过文件的目录，供主程序 /api/open 的白名单使用"""
    with LOCK:
        return {t.dir for t in STATE["jobs"] if t.dir}


# ------------------------------------------------------------------ 目录选择
def _py_exe():
    """pythonw.exe 没有标准输出，选 python.exe 来跑弹窗脚本"""
    exe = sys.executable or sys.exec_prefix
    d = os.path.dirname(exe)
    cand = os.path.join(d, "python.exe")
    return cand if os.path.isfile(cand) else exe


def pick_dir(initial=""):
    """弹系统原生的文件夹选择框。

    tkinter 在子进程里跑：主服务是 HTTP 线程，任何 GUI 的 mainloop 都不能塞进去，
    塞进去要么卡死要么崩。子进程 + 临时文件回传结果，最稳。
    """
    try:
        import tkinter  # noqa: F401  只检查有没有装
    except Exception:
        return {"ok": False, "msg": "这台机器没装 tkinter，请手动输入或粘贴目录路径"}
    tmp = os.path.join(tempfile.gettempdir(), f"wxpick_{os.getpid()}_{int(time.time())}.txt")
    code = (
        "import tkinter as tk\n"
        "from tkinter import filedialog\n"
        "r=tk.Tk(); r.withdraw(); r.attributes('-topmost',True)\n"
        "p=filedialog.askdirectory(title='选择保存目录', initialdir=%r)\n"
        "open(%r,'w',encoding='utf-8').write(p or '')\n"
        "r.destroy()\n" % ((initial or ""), tmp)
    )
    try:
        subprocess.run([_py_exe(), "-c", code], timeout=300,
                       creationflags=CREATE_NO_WINDOW,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(tmp, "r", encoding="utf-8") as f:
            path = f.read().strip()
        return {"ok": True, "path": path} if path else {"ok": False, "msg": "没有选择目录"}
    except Exception as e:
        return {"ok": False, "msg": f"打开选择框失败：{e}"}
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass


# ------------------------------------------------------------------ 退出清理
def shutdown():
    """程序退出/重启前，把还在跑的子进程收干净，别留孤儿进程占着文件"""
    with LOCK:
        for t in STATE["jobs"]:
            if t.proc and t.proc.poll() is None:
                _kill_tree(t.proc.pid)
                t.status = "已停止"


atexit.register(shutdown)


def init(exe=""):
    """启动时调一次：确定可执行文件，没有也不报错（用户可以在界面上再指定）"""
    p = set_exe(exe) if exe else exe_info("")
    if p.get("ok"):
        log(f"N_m3u8DL-RE {p['version']} · {p['path']}")
        return p
    log("⚠ 没找到 N_m3u8DL-RE.exe，m3u8 下载不可用")
    return p


if __name__ == "__main__":
    print(json.dumps(init(), ensure_ascii=False, indent=2))
