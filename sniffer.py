# -*- coding: utf-8 -*-
"""
视频号直播地址嗅探（自动抓流地址）
------------------------------------------------------------
原理：
  视频号直播只在微信客户端里播放，流地址是 HTTPS + 签名（txSecret），
  报文加密，唯一的办法是在本机做一次 TLS 中间人（mitmproxy），
  从解密后的请求里把 .flv 地址捞出来。

  ⚠ 本机系统**并不信任** mitmproxy 的根证书（实测 Invoke-WebRequest 走代理会报
    "未能为 SSL/TLS 安全通道建立信任关系"）。所以绝不能全量解密，必须用
    allow_hosts 只对视频号 CDN 解密，其余域名原样隧道转发 —— 否则用户所有
    HTTPS 站点都会报错。视频号播放器对媒体域名不做严格校验，因此仍能拿到明文请求。

安全边界：
  · mitmdump 只监听 127.0.0.1，不暴露到局域网
  · 只解密 wxlivecdn.com，其它域名走隧道
  · 代理改动前先备份，退出/崩溃后自动还原
"""
import ctypes
import json
import os
import re
import socket
import subprocess
import threading
import time
import winreg

APP_DIR = os.path.dirname(os.path.abspath(__file__))
BACKUP_PATH = os.path.join(APP_DIR, "proxy_backup.json")
SNIFF_FILE = os.path.join(APP_DIR, "sniffed.jsonl")
PLAYED_FILE = os.path.join(APP_DIR, "played.jsonl")
CREATE_NO_WINDOW = 0x08000000
DEFAULT_PORT = 4880
MAX_HITS = 400          # 嗅探结果列表最多留多少条

# 解密哪些域名 —— 只解密一个，够了。
#
# 为什么不再全量（曾经用过 .*，代价很实在）：
#   · 视频号的 decode_key 走 JSBridge，不走 HTTP。要拿它只需要往
#     channels.weixin.qq.com 的 HTML 里注入 JS，**其他域名解密一点用都没有**。
#   · 全量解密会把用户所有站点（含内网业务系统、WebSocket 长连接）都拖过
#     mitmproxy，上游证书链不完整就直接失败（实测 bg.yjj.gxzf.gov.cn 的
#     websocket 一直报 Unhandled error，v10.events.data.microsoft.com 报
#     unable to get local issuer certificate）。
#   · 隧道转发的域名 addon 看不到，但我们本来也不需要看。
# 想临时放开（排查用）可以设环境变量 WX_SNIFF_ALLOW_HOSTS。
ALLOW_HOSTS = os.environ.get(
    "WX_SNIFF_ALLOW_HOSTS", r".*channels\.weixin\.qq\.com.*")

REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
# 已知的、自带 mitmdump 的抓包工具（按优先级找）
KNOWN_MITMDUMP = [
    r"D:\@software\视频号下载\wechatVideoDownload\缓存\mitmdump.exe",
]


# ----------------------------------------------------------------- 系统代理
def _refresh_inet():
    try:
        wininet = ctypes.WinDLL("wininet.dll")
        wininet.InternetSetOptionW(0, 39, None, 0)     # SETTINGS_CHANGED
        wininet.InternetSetOptionW(0, 37, None, 0)     # REFRESH
    except Exception:
        pass


def read_proxy():
    """返回 (ProxyEnable, ProxyServer, ProxyOverride)"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY) as k:
            en = winreg.QueryValueEx(k, "ProxyEnable")[0]
            try:
                srv = winreg.QueryValueEx(k, "ProxyServer")[0]
            except OSError:
                srv = ""
            try:
                ovr = winreg.QueryValueEx(k, "ProxyOverride")[0]
            except OSError:
                ovr = ""
        return int(en), srv, ovr
    except Exception:
        return 0, "", ""


def write_proxy(enable, server, override=None):
    """override=None 表示不动；传空字符串则显式清空（还原时要能把注入的值抹掉）"""
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, "ProxyEnable", 0, winreg.REG_DWORD, int(enable))
        if server:
            winreg.SetValueEx(k, "ProxyServer", 0, winreg.REG_SZ, server)
        if override is not None:
            winreg.SetValueEx(k, "ProxyOverride", 0, winreg.REG_SZ, override)
    _refresh_inet()


def sanitize_override(ovr):
    """生成接管期间的 bypass 列表。

    两个坑：
      · `<-loopback>` 会让**本地环回也走代理**（别的抓包工具爱加它）。一旦带上，
        本工具自己的 8765 轮询、以及用户本机跑的所有服务都会被拖进代理，
        mitm.log 里能看到每 0.8 秒一条 `server connect 127.0.0.1:8765`。
      · 用户原有条目要保留，不要替人家决定绕过哪些站点。
    """
    parts = []
    for x in (ovr or "").replace(",", ";").split(";"):
        x = x.strip()
        if not x or x.lower() == "<-loopback>":
            continue
        if x not in parts:
            parts.append(x)
    for must in ("127.0.0.1", "localhost"):
        if must not in parts:
            parts.append(must)
    return ";".join(parts)


def port_alive(host, port, timeout=0.4):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def media_key(url):
    """从媒体地址里取稳定的 id。

    同一个视频每次请求都会换一整套 token（encfilekey 之外的签名字段），
    只有 encfilekey / filekey / fileid 是稳定的，用它做"是不是同一个视频"的判据。
    和 capture_addon._media_key 必须保持一致。
    """
    for pat in (r"[?&]encfilekey=([^&]+)", r"[?&]filekey=([^&]+)",
                r"[?&]fileid=([^&]+)"):
        m = re.search(pat, url or "")
        if m:
            return m.group(1)
    return (url or "").split("?")[0]


def pick_port(preferred=DEFAULT_PORT, span=30):
    """找一个真正空闲的端口，避免撞上别的程序（原来固定 4880 是个隐患）"""
    for p in range(preferred, preferred + span):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", p))
            return p
        except OSError:
            continue
        finally:
            s.close()
    return preferred


def _pids_listening(port):
    """哪些进程在监听这个端口（netstat 解析，别用 psutil，本机没装）"""
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                             capture_output=True, text=True, errors="ignore",
                             creationflags=CREATE_NO_WINDOW).stdout
    except Exception:
        return []
    pids = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[3].upper() != "LISTENING":
            continue
        if parts[1].rsplit(":", 1)[-1] == str(port) and parts[4].isdigit():
            pids.add(int(parts[4]))
    return pids


def _proc_name(pid):
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, errors="ignore",
                             creationflags=CREATE_NO_WINDOW).stdout.strip()
        return out.split(",")[0].strip('"').lower() if out else ""
    except Exception:
        return ""


def kill_pid(pid):
    try:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       capture_output=True, creationflags=CREATE_NO_WINDOW)
        return True
    except Exception:
        return False


def kill_stray_mitm(port):
    """把占着这个端口的 mitmdump 残留干掉。

    PyInstaller 打包的 mitmdump.exe terminate 之后，子进程有时还在，
    继续占着端口、继续以旧参数运行。之前就出现过：嗅探点了「停止」、系统代理
    也还原了，但一个 allow_hosts=.* 的旧实例还在后台跑，下次嗅探端口打架。
    只杀进程名确实是 mitmdump 的，避免误伤别的程序。
    """
    killed = []
    for pid in _pids_listening(port):
        if _proc_name(pid) == "mitmdump.exe":
            if kill_pid(pid):
                killed.append(pid)
    return killed


def parse_server(server):
    """把 127.0.0.1:4546 这种解析成 (host, port)；支持 http=host:port;https=... 形式"""
    if not server:
        return None
    for part in str(server).replace(",", ";").split(";"):
        part = part.strip()
        if "=" in part:
            part = part.split("=", 1)[1]
        if ":" in part:
            h, _, p = part.rpartition(":")
            if p.isdigit():
                return h or "127.0.0.1", int(p)
    return None


# ----------------------------------------------------------------- mitmdump
def find_mitmdump():
    try:
        with open(os.path.join(APP_DIR, "config.json"), "r", encoding="utf-8") as f:
            p = json.load(f).get("mitmdump", "")
        if p and os.path.isfile(p):
            return p
    except Exception:
        pass
    for p in KNOWN_MITMDUMP:
        if os.path.isfile(p):
            return p
    for name in ("mitmdump.exe", "mitmdump"):
        for d in os.environ.get("PATH", "").split(os.pathsep):
            cand = os.path.join(d, name)
            if os.path.isfile(cand):
                return cand
    return ""


# ----------------------------------------------------------------- 嗅探器
class Sniffer:
    """起 mitmdump 接管系统代理，把命中的 .flv 地址收进列表"""

    def __init__(self, port=DEFAULT_PORT):
        self.port = port
        self.proc = None
        self.state = "空闲"          # 空闲 / 嗅探中 / 失败
        self.msg = ""
        self.caught = None
        self.hits = []               # 命中的地址列表
        self._seen = set()
        self._next_id = 1
        self._backup = None
        self._locate = 0
        self._lock = threading.Lock()      # 保护 hits / _seen / _locate
        self._io_lock = threading.Lock()   # 保护文件读取位置
        self._played = set()               # 真正被请求过的媒体 id
        self._played_loc = 0
        self._played_lock = threading.Lock()

    # --- 收集结果
    def _add_hit(self, url, t=None, meta=None):
        meta = meta or {}
        with self._lock:
            if url in self._seen:
                return None
            self._seen.add(url)
            exp = ""
            # t 可能是 unix 时间戳（float），也可能已经是 "HH:MM:SS" 字符串
            # （reparse 从盘上读回来的就是字符串）。直接丢给 localtime 会抛
            # TypeError，把整个 poll 打断。
            if isinstance(t, (int, float)) and t:
                when = time.strftime("%H:%M:%S", time.localtime(t))
            elif isinstance(t, str) and t:
                when = t
            else:
                when = time.strftime("%H:%M:%S")
            try:
                m = re.search(r"[?&]txTime=([0-9a-fA-F]+)", url)
                if m:
                    exp = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(m.group(1), 16)))
            except Exception:
                pass
            kind = meta.get("kind") or "live"
            fsize = meta.get("size") or 0
            title = str(meta.get("title") or "").strip()[:80]
            # 同一个视频每请求一次就换一套 decode_key + token（svrnonce 每次不同），
            # 按 url 去重等于没去重，列表里全是一个视频的多个副本。
            # 按「标题 + 文件大小」认人，重复出现时只把地址刷新成最新的那份
            # （旧 token 可能已经过期）。
            #
            # 注意不能要求 fsize 非 0 才去重：接口有时候不给 file_size，
            # 那样同一个视频会被反复堆进列表（用户实测堆了 90 多条）。大小未知时
            # 只要标题一致就认为是同一条，把最新地址刷上去。
            if kind == "replay" and (title or fsize):
                for h in self.hits:
                    if h.get("kind") != "replay":
                        continue
                    same_title = title and (h.get("name") or "") == title
                    same_size = fsize and h.get("filesize") and h["filesize"] == fsize
                    if not (same_title or same_size):
                        continue
                    if title and h.get("name") and not same_title:
                        continue          # 标题不同就不是同一个片子，别误合并
                    h["url"] = url
                    h["decode_key"] = str(meta.get("decode_key") or "") or h.get("decode_key", "")
                    h["t"] = when
                    if fsize and not h.get("filesize"):
                        h["filesize"] = fsize
                    return None
            hit = {"id": self._next_id, "url": url, "t": when, "kind": kind,
                   "expiry": exp, "name": "", "status": "待录制",
                   "decode_key": str(meta.get("decode_key") or ""),
                   "author": meta.get("author") or "",
                   "filesize": fsize,
                   "duration": meta.get("duration") or 0,
                   "resolution": "",
                   "played": False,
                   }
            w, h = meta.get("width") or 0, meta.get("height") or 0
            if w and h:
                hit["resolution"] = "%dx%d" % (int(w), int(h))
            # 回放接口自带标题，直接用它预填名称
            if kind == "replay" and meta.get("title"):
                hit["name"] = str(meta["title"]).strip()[:80]
            self._next_id += 1
            self.hits.append(hit)
            # 列表只留最近 N 条：一晚上连开几十个直播间，无上限会让前端越刷越卡
            if len(self.hits) > MAX_HITS:
                cut = len(self.hits) - MAX_HITS
                self.hits = self.hits[cut:]
                self._seen = {h["url"] for h in self.hits}
            return hit

    def rename(self, hid, name):
        with self._lock:
            for h in self.hits:
                if h["id"] == hid:
                    h["name"] = (name or "").strip()[:80]
                    return True
        return False

    def remove(self, hid):
        with self._lock:
            self.hits = [h for h in self.hits if h["id"] != hid]
        return True

    def mark(self, hid, status):
        with self._lock:
            for h in self.hits:
                if h["id"] == hid:
                    h["status"] = status
                    break

    def clear(self):
        """清空嗅探结果（只清内存列表，不动磁盘上的原始记录）"""
        with self._lock:
            n = len(self.hits)
            self.hits = []
            self._seen = set()
        return n

    # --- "这个视频到底在不在播"
    def _refresh_played(self):
        """增量读 played.jsonl —— capture_addon 把真正被请求过的媒体写在这里"""
        if not os.path.isfile(PLAYED_FILE):
            return
        with self._played_lock:
            try:
                if os.path.getsize(PLAYED_FILE) < self._played_loc:
                    self._played_loc = 0
                    self._played.clear()
                with open(PLAYED_FILE, "r", encoding="utf-8") as f:
                    f.seek(self._played_loc)
                    data = f.read()
                    self._played_loc = f.tell()
            except Exception:
                return
            for line in data.splitlines():
                try:
                    k = json.loads(line).get("key")
                except Exception:
                    continue
                if k:
                    self._played.add(k)

    def _mark_played(self):
        """把「媒体真的被请求过」的视频标出来。

        回放页右侧推荐列表的视频也会走 JSBridge 把 url + decode_key 全拿一遍，
        光看数据分不出是谁，所以用"播放器有没有真的去拉这个媒体"当判据。
        列表里的视频只加载封面图，不会触发 mp4 的 Range 请求。
        """
        try:
            self._refresh_played()
        except Exception:
            return
        if not self._played:
            return
        with self._lock:
            for h in self.hits:
                if h.get("played"):
                    continue
                if media_key(h.get("url", "")) in self._played:
                    h["played"] = True

    def dedup(self):
        """清理重复结果。

        _add_hit 进来时已经在去重了，但历史遗留（旧版本写进 sniffed.jsonl 的
        一视频多份）还在，而且用户可能改了名字导致两条看起来一样。这里按
        「类型 + 名称 + 大小」再收一遍，保留最新抓到的那条（url 最新鲜）。
        """
        with self._lock:
            keep = {}
            order = []
            removed = 0
            for h in self.hits:
                key = (h.get("kind"), (h.get("name") or "").strip(), h.get("filesize") or 0)
                if key in keep:
                    idx = order.index(key)
                    order[idx] = key
                    # 用后来的覆盖（地址更新）
                    keep[key] = h
                    removed += 1
                else:
                    keep[key] = h
                    order.append(key)
            if removed:
                self.hits = [keep[k] for k in order]
                self._seen = {h["url"] for h in self.hits}
        return removed

    def hit_by_id(self, hid):
        with self._lock:
            for h in self.hits:
                if h["id"] == hid:
                    return dict(h)
        return None

    # --- 启动前体检
    def precheck(self):
        if not find_mitmdump():
            return False, ("找不到 mitmdump.exe。可执行 pip install mitmproxy，"
                           "或在本工具 config.json 里填 mitmdump 路径。")
        ca = os.path.join(os.path.expanduser("~"), ".mitmproxy", "mitmproxy-ca.pem")
        if not os.path.isfile(ca):
            return False, ("找不到 mitmproxy 根证书（%USERPROFILE%\\.mitmproxy）。"
                           "需要先用抓包工具生成一次证书。")
        en, srv, _ = read_proxy()
        if en:
            parsed = parse_server(srv)
            if parsed and parsed[1] != self.port and port_alive(*parsed):
                return False, (f"系统代理已被 127.0.0.1:{parsed[1]} 占用"
                               f"（多半是别的抓包工具正在监听）。请先关掉它的「开始监听」。")
        return True, ""

    # --- 开始
    def start(self):
        ok, why = self.precheck()
        if not ok:
            self.state, self.msg = "失败", why
            return ok, why

        exe = find_mitmdump()
        addon = os.path.join(APP_DIR, "capture_addon.py")
        if not os.path.isfile(addon):
            self.state, self.msg = "失败", "缺少 capture_addon.py"
            return False, self.msg

        # 起新的之前先清掉上一轮残留的实例（同端口、旧白名单），
        # 否则新进程会因端口占用被挤到 4881，旧的继续在后台跑
        try:
            kill_stray_mitm(self.port)
        except Exception:
            pass

        self.port = pick_port(self.port)

        # 不清空 sniffed.jsonl：里面可能是刚解析出来的历史结果，删了就得重刷页面
        # 才能再拿到。重复项靠 _seen 去重（_locate 从 0 开始读）。

        logf = None
        try:
            logf = open(os.path.join(APP_DIR, "mitm.log"), "wb")
            # mitmdump 会继承本进程的 http_proxy/https_proxy 环境变量（WorkBuddy 的
            # 沙箱代理 127.0.0.1:49333）。那样它会再套一层代理去连上游，而沙箱
            # 代理返回的证书链它不认，日志里就是 "unable to get local issuer
            # certificate"，该域名的请求直接失败。所以给它一个干净的直连环境。
            env = {k: v for k, v in os.environ.items()
                   if k.lower() not in ("http_proxy", "https_proxy", "all_proxy")}
            self.proc = subprocess.Popen(
                [exe, "-s", addon,
                 "--listen-host", "127.0.0.1",        # 只监听本机，别暴露成局域网代理
                 "-p", str(self.port),
                 # 只解密视频号页面域名，其余流量原样隧道转发
                 "--set", "allow_hosts=" + ALLOW_HOSTS,
                 # 不校验上游证书：我们只是转发，校验失败反而会掐断本来能用的连接
                 "--set", "ssl_insecure=true",
                 "--set", "block_global=false"],
                stdout=logf, stderr=logf, env=env,
                creationflags=CREATE_NO_WINDOW)
        except Exception as e:
            self.state, self.msg = "失败", f"启动 mitmdump 失败：{e}"
            return False, self.msg
        finally:
            if logf:
                logf.close()

        # 等端口就绪：必须确认是"我们起的这个进程"在监听
        for _ in range(40):
            time.sleep(0.25)
            if self.proc.poll() is not None:
                self.state, self.msg = "失败", "mitmdump 启动后立即退出，看 mitm.log"
                self.proc = None
                return False, self.msg
            if port_alive("127.0.0.1", self.port):
                break
        else:
            self.state, self.msg = "失败", f"端口 {self.port} 没起来"
            self._kill_proc()
            return False, self.msg

        # 备份并接管系统代理
        en, srv, ovr = read_proxy()
        self._backup = {"enable": en, "server": srv, "override": ovr,
                        "port": self.port, "ours": True}
        try:
            with open(BACKUP_PATH, "w", encoding="utf-8") as f:
                json.dump(self._backup, f, ensure_ascii=False)
        except Exception:
            pass
        write_proxy(1, f"127.0.0.1:{self.port}", sanitize_override(ovr))

        self.state = "嗅探中"
        self.msg = "已接管系统代理，去微信里打开回放页面（会注入 JS 抓 decode_key）"
        self.caught = None
        with self._io_lock:
            self._locate = 0
        return True, self.msg

    # --- 轮询新抓到的地址
    def poll(self):
        """返回本次新增的 hit 列表（可能为空）"""
        self._mark_played()
        if not os.path.isfile(SNIFF_FILE):
            return []
        # 后台线程和手动刷新会同时读同一个文件，必须串行
        with self._io_lock:
            try:
                # 文件被外部清空/重建时，旧的读取位置会落在文件末尾之后，
                # 从此再也读不到任何新内容 —— 发现文件变小就从头读
                if os.path.getsize(SNIFF_FILE) < self._locate:
                    self._locate = 0
                with open(SNIFF_FILE, "r", encoding="utf-8") as f:
                    f.seek(self._locate)
                    data = f.read()
                    self._locate = f.tell()
            except Exception:
                return []

        new_hits = []
        for line in data.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            u = rec.get("url", "")
            if not u.lower().startswith("http"):
                continue
            kind = rec.get("kind") or "live"
            # 直播只认 flv 流；回放是 mp4（stodownload 之类）
            if kind == "live" and ".flv" not in u.lower():
                continue
            hit = self._add_hit(u, rec.get("t"), rec)
            if hit:
                new_hits.append(hit)
                self.caught = u

        # 不在这里还原代理：用户要连逛几个直播间收集多条，由 stop() 或超时统一还原
        if new_hits and self.state == "嗅探中":
            self.msg = "已抓到地址，可以继续换直播间收集，或点「停止嗅探」"
        return new_hits

    # --- 恢复代理
    def _release_proxy(self):
        b = self._backup
        if not b:
            return
        server, enable = b.get("server", ""), b.get("enable", 0)
        parsed = parse_server(server)
        # 原来那个代理如果已经不在监听，别恢复成它，否则直接断网
        if enable and parsed and not port_alive(*parsed):
            enable = 0
        write_proxy(enable, server, b.get("override", ""))
        try:
            os.remove(BACKUP_PATH)
        except Exception:
            pass
        self._backup = None

    def _kill_proc(self):
        p, self.proc = self.proc, None
        if p:
            try:
                if p.poll() is None:
                    p.terminate()
                    try:
                        p.wait(timeout=5)
                    except Exception:
                        p.kill()
            except Exception:
                pass
        # 兜底：进程句柄有时会跟实际进程脱节（子进程残留、句柄被换掉），
        # 按端口再扫一遍，确保端口真的空出来。
        try:
            kill_stray_mitm(self.port)
        except Exception:
            pass

    def stop(self, restore=True):
        if restore:
            # 内存里没备份不代表磁盘上没有（进程重启过、或上一轮没收尾），
            # 所以无条件再读一次磁盘 —— 否则代理会一直指着已经死掉的端口，
            # 表现就是"所有网页都打不开"。
            if self._backup:
                self._release_proxy()
            else:
                try:
                    restore_stale_proxy()
                except Exception:
                    pass
        self._kill_proc()
        self.state, self.msg = "空闲", ""
        return True

    def status(self):
        alive = bool(self.proc and self.proc.poll() is None)
        # 自愈：mitmdump 自己退了（崩溃 / 被杀 / 被别的清理脚本干掉），
        # 但状态还挂着"嗅探中" —— 此时系统代理仍指向这个死端口，
        # 用户所有网站都会打不开。检测到就立刻还原。
        if self.state == "嗅探中" and not alive:
            self._kill_proc()
            if self._backup:
                self._release_proxy()
            else:
                try:
                    restore_stale_proxy()
                except Exception:
                    pass
            self.state = "已中断"
            self.msg = "嗅探进程退出了，系统代理已自动还原"
        self._mark_played()
        with self._lock:
            hits = [dict(h) for h in self.hits]
        return {"state": self.state, "msg": self.msg, "caught": self.caught or "",
                "alive": alive, "port": self.port, "hits": hits}


# ----------------------------------------------------------------- 自愈
def restore_stale_proxy():
    """上次异常退出导致代理没还原时，下次启动自动修好"""
    if not os.path.isfile(BACKUP_PATH):
        return False
    try:
        with open(BACKUP_PATH, "r", encoding="utf-8") as f:
            b = json.load(f)
        # 备份是自己的嗅探端口 → 一律关掉代理，不能还原成一个孤儿代理
        if b.get("ours"):
            write_proxy(0, b.get("server", ""), b.get("override", ""))
        else:
            server, enable = b.get("server", ""), b.get("enable", 0)
            parsed = parse_server(server)
            if enable and parsed and not port_alive(*parsed):
                enable = 0
            write_proxy(enable, server, b.get("override", ""))
        os.remove(BACKUP_PATH)
        return True
    except Exception:
        return False


if __name__ == "__main__":
    import sys
    if "--restore" in sys.argv:
        print("已还原" if restore_stale_proxy() else "没有待还原的备份")
        print("当前代理：", read_proxy())
    else:
        print("用法：python sniffer.py --restore")
