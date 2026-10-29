# -*- coding: utf-8 -*-
"""
接收视频号页面里注入脚本回传的 WeixinJSBridge 数据。

为什么需要它：视频号的信息（视频地址、decode_key、标题）不是通过 HTTP 接口
下发的，而是 WebView 通过 WeixinJSBridge 向微信原生层请求的。这一层在网络上
完全看不到，只能在页面里挂钩子把数据捞出来。

流程：
    capture_addon 注入 JS → 页面每次调 WeixinJSBridge.invoke →
    POST 到 127.0.0.1:18002/add → 这里存盘并解析出 url + decode_key
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_DIR = os.path.dirname(os.path.abspath(__file__))
BRIDGE_FILE = os.path.join(APP_DIR, "bridge.jsonl")
OUT_FILE = os.path.join(APP_DIR, "sniffed.jsonl")
PORT_FILE = os.path.join(APP_DIR, "bridge_port.json")
PORT = int(os.environ.get("WX_BRIDGE_PORT", "18002"))
MAX_BODY = 4 * 1024 * 1024
MAX_BRIDGE_FILE = 24 * 1024 * 1024      # 原始记录超过这个大小就滚存一份
# 必须是 RLock：handle_payload 持锁时会调 _append，而 _append 自己也要拿锁，
# 普通 Lock 在这里会直接把自己锁死。
_lock = threading.RLock()
_stats = {"count": 0, "hits": 0, "last_cmd": "", "pings": 0, "last_ping": ""}


def _rotate(path, limit):
    """文件超限就截断。

    以前这里是纯 append：每抓一次回放都要写几百 KB，bridge.jsonl 两小时就
    涨到几十 MB，既拖慢 reparse 又占盘。文件里存的是"原始上报"，挖出来的
    结果已经落到 sniffed.jsonl 了，截断不会丢有效数据。
    """
    try:
        if os.path.getsize(path) > limit:
            os.replace(path, path + ".old")
    except Exception:
        pass


def _append(path, rec):
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    try:
        with _lock:
            _rotate(path, MAX_BRIDGE_FILE)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        pass


def _find(obj, out, meta=None, depth=0):
    """递归找同时带 decode_key 和 url 的节点

    微信把真正的响应塞在 `jsapi_resp.resp_json` 这种字段里，值是 **JSON 字符串**
    而不是对象。所以遇到长得像 JSON 的字符串必须再解一层递归进去 ——
    否则明明抓到了数据也挖不出 decode_key（第一版就栽在这）。
    """
    meta = meta or {}
    if depth > 14:
        return
    if isinstance(obj, dict):
        m = dict(meta)
        for k in ("description", "title", "shortTitle", "short_title", "desc"):
            v = obj.get(k)
            if isinstance(v, str) and v.strip() and not m.get("title"):
                m["title"] = v.strip()[:150]
        for k in ("nickname", "author", "username", "nickName"):
            v = obj.get(k)
            if isinstance(v, str) and v.strip() and not m.get("author"):
                m["author"] = v.strip()[:80]
        dk = obj.get("decode_key") or obj.get("decodeKey")
        if dk not in (None, "", 0, "0"):
            u = ""
            for k in ("url", "videoUrl", "video_url", "full_url", "downloadUrl", "src"):
                v = obj.get(k)
                if isinstance(v, str) and v.startswith("http"):
                    u = v
                    break
            if u:
                # ★ url 只是半截，token/sign/svrnonce 全在 url_token 里。
                # 不拼上去服务端直接返回 400 Bad Request（代价：白下 8 次重试）。
                tok = obj.get("url_token") or obj.get("urlToken") or ""
                if isinstance(tok, str) and tok.strip():
                    u = u + (tok if tok.startswith("&") else "&" + tok)
                out.append({"url": u, "decode_key": str(dk),
                            "title": m.get("title", ""), "author": m.get("author", ""),
                            "size": obj.get("file_size") or obj.get("fileSize") or 0,
                            "duration": obj.get("video_play_len") or obj.get("duration") or 0,
                            "width": int(obj.get("width") or 0),
                            "height": int(obj.get("height") or 0)})
        for v in obj.values():
            _descend(v, out, m, depth)
    elif isinstance(obj, list):
        for v in obj:
            _descend(v, out, meta, depth)


def _descend(v, out, meta, depth):
    if isinstance(v, (dict, list)):
        _find(v, out, meta, depth + 1)
    elif isinstance(v, str) and len(v) > 16 and v[:1] in "{[":
        try:
            sub = json.loads(v)
        except Exception:
            return
        if isinstance(sub, (dict, list)):
            _find(sub, out, meta, depth + 1)


def handle_payload(payload):
    """处理一条 JS 上报的数据，返回是否解析出了可用结果"""
    _stats["count"] += 1
    cmd = payload.get("cmd") or ""
    _stats["last_cmd"] = str(cmd)[:60]
    rec = {"t": round(time.time(), 3), "cmd": cmd,
           "page": (payload.get("page") or "")[:200], "raw": payload}
    _append(BRIDGE_FILE, rec)

    hits = []
    _find(payload, hits)
    seen = set()
    added = 0
    with _lock:
        for h in hits:
            if h["url"] in seen:
                continue
            seen.add(h["url"])
            h["t"] = time.strftime("%H:%M:%S")
            h["kind"] = "replay"
            h["via"] = "bridge"
            _append(OUT_FILE, h)
            added += 1
        _stats["hits"] += added
    return added


def reparse(path=None):
    """把已经落盘的 bridge.jsonl 重新解析一遍。

    解析器修好之后不必让用户再刷一次页面 —— 数据早就抓在盘上了，
    只是当时不会挖。返回 (读了多少条, 提取出多少条回放)。
    """
    path = path or BRIDGE_FILE
    if not os.path.isfile(path):
        return 0, 0
    total = added = 0
    seen = set()

    def keyof(d):
        """认人的键：有标题就按标题+大小，没标题只能退回 url。
        以前一律用 (标题, 大小)，标题为空时所有记录都撞成同一个 key，
        结果只保留第一条，后面的全被当成重复丢了。"""
        t = (d.get("title") or "").strip()
        return (t, d.get("size") or 0) if t else ("url:" + (d.get("url") or ""), 0)

    # 已有的结果先读进来，否则每跑一次 reparse 就把同样的条目再堆一遍
    if os.path.isfile(OUT_FILE):
        try:
            with open(OUT_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    if d.get("url"):
                        seen.add(keyof(d))
        except Exception:
            pass
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                total += 1
                hits = []
                _find(rec, hits)
                for h in hits:
                    # 按「标题 + 大小」认人：同一个视频每次请求都换 token 和
                    # decode_key，按 url 去重等于没去重
                    k = keyof(h)
                    if k in seen:
                        continue
                    seen.add(k)
                    ts = rec.get("t") or time.time()
                    try:
                        h["t"] = time.strftime("%H:%M:%S", time.localtime(float(ts)))
                    except Exception:
                        h["t"] = time.strftime("%H:%M:%S")
                    h["kind"] = "replay"
                    h["via"] = "bridge-reparse"
                    _append(OUT_FILE, h)
                    added += 1
    except Exception:
        pass
    return total, added


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        # 页面用 Content-Type: application/json，浏览器会先发预检
        self.send_response(200)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
        except Exception:
            n = 0
        if n <= 0 or n > MAX_BODY:
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":false}')
            return
        try:
            payload = json.loads(self.rfile.read(n).decode("utf-8", "ignore"))
        except Exception:
            payload = {}
        added = 0
        try:
            added = handle_payload(payload)
        except Exception:
            pass
        body = json.dumps({"ok": True, "hits": added}).encode()
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # /ping：注入脚本一装上就打一张图片过来，用来确认"页面确实重新加载了、
        # 钩子确实进去了"。没有它，抓不到数据时无法区分是没刷新还是没注入。
        if self.path.startswith("/ping"):
            _stats["pings"] += 1
            _stats["last_ping"] = time.strftime("%H:%M:%S")
            _append(BRIDGE_FILE, {"t": round(time.time(), 3),
                                  "cmd": "__ping", "page": self.path[:200]})
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        # 方便排查：浏览器打开 http://127.0.0.1:18002/ 能看到收了多少
        body = json.dumps(_stats, ensure_ascii=False).encode()
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start(port=PORT):
    """启动接收服务（后台线程），返回实际端口；失败返回 None

    实际端口要落盘：注入的 JS 跑在 mitmdump 的独立进程里，拿不到这里的变量，
    端口一旦被占用改用 18003，脚本还发到 18002 就全丢了。
    """
    for p in range(port, port + 8):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), Handler)
        except OSError:
            continue
        try:
            with open(PORT_FILE, "w", encoding="utf-8") as f:
                json.dump({"port": p}, f)
        except Exception:
            pass
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return p
    return None


def stats():
    return dict(_stats)


if __name__ == "__main__":
    import sys
    if "--reparse" in sys.argv:
        t, a = reparse()
        print(f"重解析 {t} 条记录，提取出 {a} 条回放")
    else:
        p = start()
        print("bridge 接收端监听:", p)
        while True:
            time.sleep(3)
            print(stats())
