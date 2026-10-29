# -*- coding: utf-8 -*-
"""
mitmproxy 插件：把命中的视频号地址实时写进 jsonl，供录制器的「嗅探」使用。

抓两类：
  1) 直播流        —— wxlivecdn.com 的 .flv
  2) 直播回放（点播）—— 从接口 JSON 里提取 url + decode_key + 标题
     回放下载下来前 128KB 是加密的，必须靠 decode_key 解密才能播放。

只写文件、不做任何请求改写，抓到后由 sniffer.py 负责还原系统代理。
"""
import json
import os
import re
import time

APP_DIR = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(APP_DIR, "sniffed.jsonl")
OUT_ENC = os.path.join(APP_DIR, "sniffed_enc.jsonl")
DBG_DIR = os.path.join(APP_DIR, "_debug")
RAW = os.path.join(APP_DIR, "sniffed_raw.jsonl")
ALL = os.path.join(APP_DIR, "sniffed_all.jsonl")
_SEEN_URL = set()

LIVE_HOST = "wxlivecdn.com"
MEDIA_HOSTS = ("finder.video.qq.com", "mpvideo.qpic.cn", "video.qq.com")
MAX_JSON = 4 * 1024 * 1024      # 只看 4MB 以内的响应，避免把视频流读进内存
MAX_DBG = 40                    # 调试转储最多留 40 份，别把磁盘塞满


def _append(path, rec):
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


_SEEN_ALL = set()


def _append_all(url, method="GET"):
    """诊断用的"都经过了哪些接口"，按 base url 去重 —— 否则一次页面加载
    就能写进去几百行一模一样的记录。"""
    base = url.split("?")[0][:200]
    key = method + " " + base
    if key in _SEEN_ALL:
        return
    if len(_SEEN_ALL) > 5000:
        _SEEN_ALL.clear()
    _SEEN_ALL.add(key)
    _append(ALL, {"t": round(time.time(), 3), "method": method, "url": url[:300]})


def _is_live(url):
    low = url.lower()
    return LIVE_HOST in low or (".flv" in low and "txsecret" in low)


def _is_media(url):
    low = url.lower()
    if "/stodownload" in low:
        return True
    return any(h in low for h in MEDIA_HOSTS) and any(
        k in low for k in ("/251/", "/20302/", "/20304/", ".mp4", "encfilekey"))


def _walk(obj, out, meta):
    """递归找同时带 decode_key 和 url 的节点，顺便继承标题/作者"""
    if isinstance(obj, dict):
        m = dict(meta)
        for k in ("nickname", "author", "username"):
            v = obj.get(k)
            if isinstance(v, str) and v.strip() and not m.get("author"):
                m["author"] = v.strip()[:80]
        for k in ("description", "title", "short_title"):
            v = obj.get(k)
            if isinstance(v, str) and v.strip() and not m.get("title"):
                m["title"] = v.strip()[:150]

        dk = obj.get("decode_key")
        if dk not in (None, "", 0, "0"):
            u = ""
            for k in ("url", "video_url", "full_url", "download_url", "media_url"):
                v = obj.get(k)
                if isinstance(v, str) and v.startswith("http"):
                    u = v
                    break
            if u:
                # url_token 里的 token/sign/svrnonce 必须拼上，否则 400
                tok = obj.get("url_token") or obj.get("urlToken") or ""
                if isinstance(tok, str) and tok.strip():
                    u = u + (tok if tok.startswith("&") else "&" + tok)
                out.append({
                    "url": u,
                    "decode_key": str(dk),
                    "title": m.get("title", ""),
                    "author": m.get("author", ""),
                    "size": obj.get("file_size") or 0,
                    "duration": obj.get("video_play_len") or obj.get("duration") or 0,
                    "width": obj.get("width") or 0,
                    "height": obj.get("height") or 0,
                })
        for v in obj.values():
            _descend(v, out, m)
    elif isinstance(obj, list):
        for v in obj:
            _descend(v, out, meta)


def _descend(v, out, meta):
    if isinstance(v, (dict, list)):
        _walk(v, out, meta)
    elif isinstance(v, str) and len(v) > 16 and v[:1] in "{[":
        # 微信用 resp_json 这种字段装 JSON 字符串，得再解一层
        try:
            sub = json.loads(v)
        except Exception:
            return
        if isinstance(sub, (dict, list)):
            _walk(sub, out, meta)


def _dump_debug(url, body):
    """把含 decode_key 的响应原样存一份，便于分析真实结构"""
    try:
        os.makedirs(DBG_DIR, exist_ok=True)
        olds = sorted(
            (os.path.join(DBG_DIR, f) for f in os.listdir(DBG_DIR)),
            key=lambda p: os.path.getmtime(p))
        for p in olds[:max(0, len(olds) - MAX_DBG + 1)]:
            try:
                os.remove(p)
            except Exception:
                pass
        stamp = time.strftime("%H%M%S") + "_%03d" % (int(time.time() * 1000) % 1000)
        name = os.path.join(DBG_DIR, stamp + ".bin")
        with open(name, "wb") as f:
            f.write(body)
        _append(RAW, {"t": round(time.time(), 3), "url": url, "file": name,
                      "size": len(body)})
    except Exception:
        pass


def _scan_body(url, body):
    """不看 content-type，直接在响应体里找 decode_key，并尽力配对视频地址"""
    txt = body
    hit = (b"decode_key" in txt) or (b"decodeKey" in txt)
    if not hit:
        return
    _dump_debug(url, body)

    # 先按 JSON 解析，成功就用结构化的方式配对
    try:
        data = json.loads(body.decode("utf-8", "ignore"))
        hits = []
        _walk(data, hits, {})
        seen = set()
        for h in hits:
            if h["url"] in seen:
                continue
            seen.add(h["url"])
            h["t"] = round(time.time(), 3)
            h["kind"] = "replay"
            _append(OUT, h)
        if hits:
            return
    except Exception:
        pass

    # JSON 解析不了（或结构不匹配）时退化为正则：在 key 附近找 http 地址
    text = body.decode("utf-8", "ignore")
    for m in re.finditer(r'"?decode_?[Kk]ey"?\s*[:=]\s*"?(\d{1,12})"?', text):
        key = m.group(1)
        if key in ("0", ""):
            continue
        seg = text[max(0, m.start() - 4000):m.end() + 4000]
        urls = re.findall(r'https?://[^\s"\\\']+', seg)
        vid = next((u for u in urls if "stodownload" in u or "encfilekey" in u), "")
        if not vid:
            continue
        ttl = re.search(r'"(?:description|title)"\s*:\s*"([^"]{2,80})"', seg)
        _append(OUT, {"t": round(time.time(), 3), "kind": "replay", "url": vid,
                      "decode_key": key, "title": ttl.group(1) if ttl else "",
                      "author": "", "size": 0, "duration": 0,
                      "width": 0, "height": 0})


def request(flow):
    """记录所有经过代理的请求（按路径去重），用来定位接口到底在哪

    Chromium 会复用已建立的长连接 —— 嗅探开始前就连上的连接不经过代理，
    所以必须让微信重启一次，才能真正抓到接口。
    """
    try:
        url = flow.request.pretty_url
    except Exception:
        return
    if _is_live(url) or _is_media(url):
        return
    base = url.split("?")[0]
    if base in _SEEN_URL:
        return
    if len(_SEEN_URL) > 5000:
        _SEEN_URL.clear()
    _SEEN_URL.add(base)
    _append(ALL, {"t": round(time.time(), 3),
                  "method": flow.request.method, "url": url[:300]})


BRIDGE_PORT_FILE = os.path.join(APP_DIR, "bridge_port.json")


def bridge_port():
    """接收端实际端口。

    注入脚本跑在 mitmdump 进程里，拿不到主程序的变量，所以端口由
    bridge_server 落盘、这里读回来 —— 否则 18002 被占用改用 18003 时，
    页面还在往 18002 发，数据全丢。
    """
    env = os.environ.get("WX_BRIDGE_PORT", "")
    if env.isdigit():
        return int(env)
    try:
        with open(BRIDGE_PORT_FILE, "r", encoding="utf-8") as f:
            v = int(json.load(f).get("port") or 0)
        if v:
            return v
    except Exception:
        pass
    return 18002


# 注入去重用的标记。必须用页面绝不可能自己出现的字符串 ——
# 上一版拿 `__wxBridgeHooked` 判断，结果测试页里恰好有同名字符串，
# 被误判成"已注入"而直接跳过，白折腾一轮。
INJECT_MARK = b"<!--wxbridge-inject-v2-->"

# 注入到视频号页面里的钩子：
# 视频号的信息（含 decode_key）不是走 HTTP 接口的，而是 WebView 通过
# WeixinJSBridge 向微信原生层要的。所以在页面里替换 invoke，把参数和回调结果
# 回传到本地服务，才能拿到这层数据。
#
# ⚠ 上一版在 <head> 里立即 hook，结果页面白屏（日志里 report-error 连发 3 次）。
# 原因是抢在页面自己初始化之前替换了 invoke，打乱了它的启动时序。现在改成
# 等 load 事件之后再挂，首屏渲染完全不碰。
INJECT_TMPL = """
<script>
(function () {
  if (window.__wxBridgeHooked) return;
  window.__wxBridgeHooked = true;
  var PORT = %d;
  var MAXLEN = 1500000;   // 单条上报上限，别把大对象序列化卡死主线程

  // 先用一张图片打点：只要 HTML 被真正重新加载，这边立刻就能看到。
  // 否则"没抓到"到底是页面没刷新、还是钩子没生效，根本分不清。
  try { new Image().src = 'http://127.0.0.1:' + PORT + '/ping?t=' + Date.now(); } catch (e) {}

  function post(payload) {
    try {
      var body = JSON.stringify(payload);
      if (!body || body.length > MAXLEN) return;
      fetch('http://127.0.0.1:' + PORT + '/add', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: body
      }).catch(function () {});
    } catch (e) {}
  }

  // 页面自己报错时把错误原文送回来 —— 白屏时这是唯一能看的东西
  window.addEventListener('error', function (e) {
    post({cmd: '__error', page: location.href,
          args: {msg: String((e && e.message) || ''), src: String((e && e.filename) || ''),
                 line: (e && e.lineno) || 0, col: (e && e.colno) || 0}});
  }, true);
  window.addEventListener('unhandledrejection', function (e) {
    var r = e && e.reason;
    post({cmd: '__reject', page: location.href,
          args: {msg: String((r && r.message) || r || '')}});
  });

  function safe(v) {
    try { return JSON.parse(JSON.stringify(v)); } catch (e) { return null; }
  }

  function hook() {
    var B = window.WeixinJSBridge;
    if (!B || !B.invoke || B.invoke.__hooked) return false;
    var orig = B.invoke;
    var wrapped = function (cmd, args, cb) {
      var self = this;
      // 用真数组转发，不直接改 arguments —— 严格模式下那种改法有副作用
      var a = Array.prototype.slice.call(arguments);
      try { post({cmd: String(cmd), args: safe(args), page: location.href}); } catch (e) {}
      if (typeof cb === 'function') {
        a[2] = function () {
          var ret;
          try {
            ret = cb.apply(self, arguments);
          } finally {
            try {
              var r0 = arguments[0];
              if (r0 && typeof r0 === 'object') {
                post({cmd: String(cmd), result: safe(r0), page: location.href});
              }
            } catch (e) {}
          }
          return ret;
        };
      }
      return orig.apply(self, a);
    };
    wrapped.__hooked = true;
    try { B.invoke = wrapped; } catch (e) { return false; }
    return true;
  }

  function later() {
    if (hook()) return;
    var n = 0;
    var t = setInterval(function () { if (hook() || ++n > 100) clearInterval(t); }, 100);
  }
  if (document.readyState === 'complete') later();
  else window.addEventListener('load', later);
})();
</script>
"""


def inject_js():
    return INJECT_TMPL % bridge_port()


def _inject_hosts():
    """往哪些页面的 HTML 里注入钩子。

    默认只注入视频号页面。做成可配置是为了能离线验证注入逻辑（本地搭一个
    gzip + CSP 的页面，把 127.0.0.1 配进来），不必每次都拿真微信试。
    """
    raw = os.environ.get("WX_INJECT_HOSTS", "channels.weixin.qq.com")
    return [h.strip() for h in raw.split(",") if h.strip()]


INJECT_HOSTS = _inject_hosts()

# 诊断开关：设 WX_NO_INJECT=1 只解密不注入，用来判断"白屏到底是注入害的还是解密害的"
NO_INJECT = os.environ.get("WX_NO_INJECT", "") not in ("", "0", "false")


def _should_inject(url):
    return any(h in url for h in INJECT_HOSTS)


def _inject(flow):
    """把钩子脚本塞进视频号页面的 HTML 开头

    ⚠ 这里是唯一会改动真实响应的地方，改坏了页面就直接白屏，所以每一步都兜住：
      · 先 decode() 解压，否则在 gzip 字节流里找 <head> 根本找不到
      · 改写后必须把 content-encoding 摘掉，否则浏览器拿明文按 gzip 解 → 白屏
      · 任何一步抛异常都还原原始内容，宁可不注入也不能弄坏页面
    """
    if NO_INJECT:
        return
    r = flow.response
    if not r:
        return
    ct = (r.headers.get("content-type") or "").lower()
    if "html" not in ct:
        return

    # ★ 解压这一步必须能判断成败。
    #
    # 以前的写法是 `try: r.decode() except: pass`，解压失败照样往下走：
    # 于是在 gzip 字节流里找 <head> 找不到，最后 fallback 成
    # `new = js + body` —— 把明文 JS 兑进了压缩数据里，页面必然白屏。
    # 现在改成：解不开就原样返回，宁可不注入。
    enc = (r.headers.get("content-encoding") or "").strip().lower()
    if enc and enc not in ("gzip", "deflate", "br", "zstd", "identity"):
        return
    try:
        r.decode()
    except Exception:
        return
    if (r.headers.get("content-encoding") or "").strip():
        return                      # decode() 没真正解开，别再动它

    body = r.content or b""
    if not body or INJECT_MARK in body:
        return
    js = (INJECT_MARK.decode("utf-8") + inject_js()).encode("utf-8")
    new = None
    # 插在 <head> 之后，让它在 <meta charset> 之后执行（编码已经确定）。
    # 插在 <head> 之前虽然浏览器也能跑，但属于依赖解析器的容错行为。
    for anchor in (b"<head>", b"<HEAD>", b"<head ", b"<HEAD "):
        i = body.find(anchor)
        if i >= 0:
            j = body.find(b">", i) + 1
            if j <= i:
                continue
            new = body[:j] + js + body[j:]
            break
    if new is None:
        for anchor in (b"<body", b"<BODY"):
            i = body.find(anchor)
            if i >= 0:
                new = body[:i] + js + body[i:]
                break
    if new is None:
        new = js + body
    try:
        r.content = new
        # 关键：改写的是明文，头里若还留着 gzip/br 就会解析失败
        for h in ("content-encoding", "Content-Encoding"):
            r.headers.pop(h, None)
        r.headers["content-length"] = str(len(new))
    except Exception:
        try:
            r.content = body
        except Exception:
            pass


ERR_FILE = os.path.join(APP_DIR, "page_errors.jsonl")


def _dump_page_error(flow):
    """视频号页面自己往 /web/report-error 上报的错误，body 里就是确切的报错原因。
    白屏的时候这是最有价值的一条线索 —— 比猜一百遍都准。"""
    try:
        req = flow.request
        body = req.get_text(strict=False) or ""
        if not body:
            raw = req.raw_content or b""
            body = raw[:4000].decode("utf-8", "ignore")
        _append(ERR_FILE, {"t": round(time.time(), 3), "url": req.pretty_url[:300],
                           "body": body[:4000]})
    except Exception:
        pass


def response(flow):
    try:
        url = flow.request.pretty_url
    except Exception:
        return

    # --- 视频号页面：注入 WeixinJSBridge 钩子（decode_key 只能从这层拿）
    if _should_inject(url):
        _inject(flow)
        _append_all(url, flow.request.method)
        if "report-error" in url:
            _dump_page_error(flow)
        return

    # --- 直播流地址
    if _is_live(url):
        _append(OUT, {"t": round(time.time(), 3), "kind": "live", "url": url})
        return

    # --- 回放媒体：记下服务端告知的加密长度
    if _is_media(url):
        h = flow.response.headers if flow.response else {}
        enc_flag = h.get("x-encflag") or h.get("X-encflag") or ""
        enc_len = h.get("x-enclen") or h.get("X-enclen") or ""
        if enc_flag or enc_len:
            _append(OUT_ENC, {"t": round(time.time(), 3), "url": url,
                              "encflag": enc_flag, "enclen": enc_len})
        return

    # --- 其它响应：只要不是视频/图片，都把 body 拿来看有没有 decode_key
    r = flow.response
    if not r:
        return
    ct = (r.headers.get("content-type") or "").lower()
    if any(k in ct for k in ("video/", "image/", "audio/", "font/")):
        return
    cl = r.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > MAX_JSON:
        return
    try:
        r.decode()
    except Exception:
        pass
    body = r.content
    if not body or len(body) > MAX_JSON:
        return
    _scan_body(url, body)
