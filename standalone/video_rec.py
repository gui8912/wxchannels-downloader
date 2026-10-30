# -*- coding: utf-8 -*-
"""
微信视频号直播流录制工具（FLV 直播流 → MP4）

用法：
    python video_rec.py <直播流URL> [时长秒数]
        时长省略 = 一直录到直播结束 / Ctrl+C 停止

说明：
    - 视频号直播的 pull 地址是 FLV over HTTP（video/x-flv），可以直接用 ffmpeg 拉流。
    - 输出用「分片 MP4」(fragmented mp4)，即使中途被打断，已录部分仍能正常播放。
    - 同名输出自动追加 (2)(3)，不会覆盖已有录像；ffmpeg 失败时程序返回非 0。
    - 优先用 FFMPEG 环境变量，其次从 PATH 查找；本机已有 ffmpeg 路径存在时自动使用。
    - 地址带 txTime/txSecret 签名，过期后返回 403，需要重新抓一次地址。
"""
import math
import os
import shutil
import subprocess
import sys
import time

DEFAULT_FFMPEG = r"D:\WorkBuddy\工具\ffmpeg\package\ffmpeg.exe"
FFMPEG = os.environ.get("FFMPEG") or (
    DEFAULT_FFMPEG if os.path.isfile(DEFAULT_FFMPEG) else "ffmpeg"
)


def _unique_path(path):
    """同名文件已存在时追加序号，避免覆盖已经录好的视频。"""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    number = 2
    while True:
        candidate = f"{stem}({number}){ext}"
        if not os.path.exists(candidate):
            return candidate
        number += 1


def _find_ffmpeg():
    if os.path.isfile(FFMPEG):
        return FFMPEG
    resolved = shutil.which(FFMPEG)
    if resolved:
        return resolved
    print("错误：找不到 ffmpeg，请安装后加入 PATH，或用 FFMPEG 环境变量指定完整路径。")
    return None


def main():
    if len(sys.argv) not in (2, 3):
        print(__doc__)
        return 2

    url = sys.argv[1].strip()
    if not url:
        print("错误：直播流 URL 不能为空。")
        return 2

    dur = None
    if len(sys.argv) == 3:
        try:
            dur = float(sys.argv[2])
        except ValueError:
            print("错误：时长必须是大于 0 的秒数。")
            return 2
        if not math.isfinite(dur) or dur <= 0:
            print("错误：时长必须是大于 0 的秒数。")
            return 2

    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        return 1

    out_dir = os.path.dirname(os.path.abspath(__file__))
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out = _unique_path(os.path.join(out_dir, f"视频号直播_{stamp}.mp4"))

    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "warning",
        "-rw_timeout", "15000000",           # 15s 无数据即判定断流
        "-i", url,
        "-c", "copy",
        "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
        "-n", out,                            # 即使并发同名，也禁止覆盖
    ]
    if dur is not None:
        cmd[-2:-2] = ["-t", str(dur)]

    print("输出文件:", out)
    print("命令:", subprocess.list2cmdline(cmd)[:120], "...")
    t0 = time.time()
    interrupted = False
    rc = 1
    try:
        rc = subprocess.run(cmd, check=False).returncode
    except KeyboardInterrupt:
        interrupted = True
        rc = 130
    except OSError as exc:
        print(f"错误：无法启动 ffmpeg：{exc}")
    finally:
        size = os.path.getsize(out) / 1024 / 1024 if os.path.exists(out) else 0
        label = "已停止" if interrupted else ("完成" if rc == 0 else "失败")
        print(f"\n{label}：{out}\n大小 {size:.1f} MB，耗时 {time.time() - t0:.0f} 秒")
    return rc


if __name__ == "__main__":
    sys.exit(main())
