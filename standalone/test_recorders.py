# -*- coding: utf-8 -*-
"""本地回归测试；不访问真实直播地址。可用 RECORDER_ROOT 指定被测脚本目录。"""
import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


def default_bash():
    """Windows 下优先找 Git 自带的 bash，避免误用 System32 的 WSL 启动器。"""
    if os.name != "nt":
        return "bash"
    git = shutil.which("git")
    if git:
        for parent in Path(git).resolve().parents:
            for relative in ("bin/bash.exe", "usr/bin/bash.exe"):
                candidate = parent / relative
                if candidate.is_file():
                    return str(candidate)
    found = shutil.which("bash")
    if found and "system32" not in found.lower():
        return found
    return "bash"


def find_ffmpeg():
    configured = os.environ.get("FFMPEG")
    if configured and Path(configured).is_file():
        return Path(configured)
    found = shutil.which("ffmpeg")
    return Path(found) if found else None


def find_timeout(bash):
    if os.name != "nt" or bash == "bash":
        return "timeout"
    executable = Path(bash)
    candidates = (
        executable.with_name("timeout.exe"),
        executable.parent.parent / "usr/bin/timeout.exe",
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return "timeout"


ROOT = Path(os.environ.get("RECORDER_ROOT", Path(__file__).resolve().parent))
BASH = os.environ.get("BASH_EXE") or default_bash()
TIMEOUT = find_timeout(BASH)
FFMPEG = find_ffmpeg()
FLV_HEADER = b"FLV\x01\x05\x00\x00\x00\x09\x00\x00\x00\x00"
PAYLOAD = b"test-flv-tag-data"
LOCAL_ENV = dict(os.environ, NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.requests += 1
        if self.server.mode == "http403":
            self.send_response(403)
            body = b"FORBIDDEN"
        elif self.server.mode == "empty_then_flv" and self.server.requests == 1:
            self.send_response(200)
            body = b""
        elif self.server.mode == "flv_then_empty" and self.server.requests > 1:
            self.send_response(200)
            body = b""
        else:
            self.send_response(200)
            body = self.server.body
        self.send_header("Content-Type", "video/x-flv")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, *_args):
        pass


@contextlib.contextmanager
def server(mode, body=FLV_HEADER + PAYLOAD):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.mode = mode
    httpd.body = body
    httpd.requests = 0
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd, f"http://127.0.0.1:{httpd.server_port}/live.flv"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def load_video_module():
    spec = importlib.util.spec_from_file_location("video_rec_under_test", ROOT / "video_rec.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if FFMPEG:
        module.FFMPEG = str(FFMPEG)
    return module


def output_paths(text):
    return [line.split(":", 1)[1].strip()
            for line in text.splitlines() if line.startswith("输出文件:")]


class PythonRecorderTests(unittest.TestCase):
    def run_main(self, module, args, ffmpeg_rc=0):
        written = []

        def fake_run(cmd, check=False):
            out = Path(cmd[-1])
            out.write_bytes(b"fake fragmented mp4")
            written.append(out)
            return SimpleNamespace(returncode=ffmpeg_rc)

        stdout = io.StringIO()
        with mock.patch.object(sys, "argv", ["video_rec.py"] + args), \
             mock.patch.object(module.time, "strftime", return_value="FIXED"), \
             mock.patch.object(module, "_find_ffmpeg", return_value="ffmpeg"), \
             mock.patch.object(module.subprocess, "run", side_effect=fake_run) as run, \
             contextlib.redirect_stdout(stdout):
            rc = module.main()
        return rc, stdout.getvalue(), written, run

    def test_ffmpeg_failure_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = load_video_module()
            module.__file__ = str(Path(tmp) / "video_rec.py")
            rc, text, _, _ = self.run_main(module, ["http://127.0.0.1/live.flv"], 1)
            self.assertNotEqual(rc, 0, text)
            self.assertIn("失败", text)

    def test_invalid_duration_is_rejected(self):
        for value in ("abc", "0", "-1", "nan", "inf"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                module = load_video_module()
                module.__file__ = str(Path(tmp) / "video_rec.py")
                rc, text, _, run = self.run_main(module, ["http://127.0.0.1/live.flv", value])
                self.assertEqual(rc, 2, text)
                run.assert_not_called()

    def test_same_timestamp_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = load_video_module()
            module.__file__ = str(Path(tmp) / "video_rec.py")
            first = self.run_main(module, ["http://127.0.0.1/live.flv"])
            second = self.run_main(module, ["http://127.0.0.1/live.flv"])
            paths = output_paths(first[1] + second[1])
            self.assertEqual(len(paths), 2)
            self.assertNotEqual(paths[0], paths[1], "同名输出会覆盖上一段录像")

    @unittest.skipUnless(FFMPEG is not None, "ffmpeg 不存在")
    def test_real_flv_to_fragmented_mp4(self):
        with tempfile.TemporaryDirectory() as tmp:
            sample = Path(tmp) / "sample.flv"
            subprocess.run([
                str(FFMPEG), "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=5",
                "-t", "0.4", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-f", "flv", str(sample),
            ], check=True, timeout=20)
            module = load_video_module()
            module.__file__ = str(Path(tmp) / "video_rec.py")
            with server("flv", sample.read_bytes()) as (_httpd, url), \
                 mock.patch.object(sys, "argv", ["video_rec.py", url, "2"]), \
                 contextlib.redirect_stdout(io.StringIO()) as stdout:
                rc = module.main()
            self.assertEqual(rc, 0, stdout.getvalue())
            paths = output_paths(stdout.getvalue())
            self.assertEqual(len(paths), 1)
            data = Path(paths[0]).read_bytes()
            self.assertGreater(len(data), 100)
            self.assertEqual(data[4:8], b"ftyp")


class ShellRecorderTests(unittest.TestCase):
    def prepare(self, tmp, url):
        tmp = Path(tmp)
        source = (ROOT / "record_live.sh").read_text(encoding="utf-8")
        old_cd = "cd /d/WorkBuddy/2026-10-29-10-35-18 || exit 1"
        source = source.replace(old_cd, f"cd {tmp.as_posix()} || exit 1")
        script = tmp / "record_live.sh"
        script.write_text(source, encoding="utf-8", newline="\n")
        (tmp / "live_url.txt").write_text(url + "\n", encoding="utf-8")
        return script, tmp / "out.flv"

    def test_http_error_stops_without_polluting_flv(self):
        with tempfile.TemporaryDirectory() as tmp, server("http403") as (httpd, url):
            script, out = self.prepare(tmp, url)
            result = subprocess.run(
                [TIMEOUT, "5", BASH, script.as_posix(), out.as_posix()],
                capture_output=True, text=True, errors="replace", timeout=8, env=LOCAL_ENV,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(out.read_bytes(), b"", "HTTP 错误页被写进 FLV")
            self.assertEqual(httpd.requests, 1, "永久 HTTP 错误不应无限重试")
            self.assertFalse((Path(tmp) / ".recording").exists(), "停止标记未清理")

    def test_first_empty_response_does_not_lose_flv_header(self):
        with tempfile.TemporaryDirectory() as tmp, server("empty_then_flv") as (_httpd, url):
            script, out = self.prepare(tmp, url)
            proc = subprocess.Popen(
                [BASH, script.as_posix(), out.as_posix()],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                env=LOCAL_ENV,
            )
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if out.exists() and out.stat().st_size >= len(PAYLOAD):
                        break
                    if proc.poll() is not None:
                        break
                    time.sleep(0.05)
                stop = Path(tmp) / ".recording"
                if stop.exists():
                    stop.unlink()
                stdout, stderr = proc.communicate(timeout=5)
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    proc.communicate(timeout=3)
            self.assertEqual(proc.returncode, 0, stdout + stderr)
            self.assertEqual(out.read_bytes(), FLV_HEADER + PAYLOAD)

    def test_normal_eof_stops_after_empty_reconnects(self):
        with tempfile.TemporaryDirectory() as tmp, server("flv_then_empty") as (httpd, url):
            script, out = self.prepare(tmp, url)
            result = subprocess.run(
                [BASH, script.as_posix(), out.as_posix()],
                capture_output=True, text=True, errors="replace", timeout=10, env=LOCAL_ENV,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(out.read_bytes(), FLV_HEADER + PAYLOAD)
            self.assertEqual(httpd.requests, 5)
            self.assertFalse((Path(tmp) / ".recording").exists())

    def test_nonempty_output_is_not_appended(self):
        with tempfile.TemporaryDirectory() as tmp, server("flv") as (_httpd, url):
            script, out = self.prepare(tmp, url)
            out.write_bytes(b"existing recording")
            result = subprocess.run(
                [TIMEOUT, "4", BASH, script.as_posix(), out.as_posix()],
                capture_output=True, text=True, errors="replace", timeout=7, env=LOCAL_ENV,
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(out.read_bytes(), b"existing recording")


if __name__ == "__main__":
    unittest.main(verbosity=2)
