# -*- coding: utf-8 -*-
"""
微信视频号「回放加密 MP4」解密（Python 封装）

实际解密由 _bin/wxdec.js 调微信官方 WASM 完成 —— 用官方模块是为了保证
Isaac64 密钥流和微信播放器逐字节一致，避免自己实现带来偏差。

对外只暴露三个函数：
    available()                      -> (bool, 说明)
    check(path)                      -> dict(encrypted=?, size=?)
    decrypt(src, dst, key, enc_len)  -> dict(ok=?, decrypted=?, reason=?)
"""
import glob
import json
import os
import shutil
import subprocess

APP_DIR = os.path.dirname(os.path.abspath(__file__))
JS = os.path.join(APP_DIR, "_bin", "wxdec.js")
CREATE_NO_WINDOW = 0x08000000

_NODE = None


def find_node():
    """找一个可用的 node.exe；找不到返回 None"""
    global _NODE
    if _NODE:
        return _NODE
    cand = shutil.which("node")
    if cand and os.path.isfile(cand):
        _NODE = cand
        return _NODE
    pats = [
        r"C:\Users\%s\.workbuddy\binaries\node\versions\*\node.exe" % os.environ.get("USERNAME", ""),
        r"C:\Program Files\nodejs\node.exe",
        r"C:\Program Files (x86)\nodejs\node.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\nodejs\node.exe"),
    ]
    for p in pats:
        hits = sorted(glob.glob(p), reverse=True)
        if hits:
            _NODE = hits[0]
            return _NODE
    return None


def available():
    node = find_node()
    if not node:
        return False, "没找到 node.exe，无法解密回放（可安装 Node.js 或用自带 node 环境）"
    if not os.path.isfile(JS):
        return False, "缺少 _bin/wxdec.js"
    if not os.path.isfile(os.path.join(APP_DIR, "_bin", "wvd.wasm")):
        return False, "缺少 _bin/wvd.wasm"
    return True, "就绪（%s）" % node


def _run(args, timeout=300):
    node = find_node()
    if not node:
        return {"ok": False, "reason": "没找到 node.exe"}
    cmd = [node, JS] + [str(a) for a in args]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "解密超时（%d 秒）" % timeout}
    except Exception as e:
        return {"ok": False, "reason": "调用失败：%s" % e}
    out = (p.stdout or b"").decode("utf-8", "ignore").strip()
    line = out.splitlines()[-1] if out else ""
    try:
        return json.loads(line)
    except Exception:
        err = (p.stderr or b"").decode("utf-8", "ignore").strip()
        return {"ok": False, "reason": (err or out or "无输出")[:300]}


def check(path):
    """判断一个文件是否处于加密状态（offset 4 不是 ftyp 即为加密）"""
    return _run(["--check", path], timeout=60)


def decrypt(src, dst, decode_key, enc_len=0):
    """解密 src 到 dst。若源文件本身未加密则直接复制。"""
    return _run(["--decrypt", src, dst, decode_key, enc_len or 131072], timeout=900)


def keystream(decode_key, out_file):
    return _run(["--keystream", decode_key, out_file], timeout=120)


if __name__ == "__main__":
    print("可用性:", available())
    d = os.path.dirname(os.path.abspath(__file__))
    for f in glob.glob(os.path.join(d, "录制", "*.mp4"))[:1]:
        print("检查:", os.path.basename(f), check(f))
