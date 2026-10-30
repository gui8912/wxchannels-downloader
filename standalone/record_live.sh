#!/bin/bash
# 微信视频号 FLV 直播录制（纯 curl，断流自动重连续录）
# 用法：bash record_live.sh 输出.flv
# 停止：Ctrl+C，或删除本脚本目录的 .recording；停止标记会在退出时自动清理。
set -o pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd) || exit 1
cd "$SCRIPT_DIR" || exit 1
URL_FILE="$SCRIPT_DIR/live_url.txt"
STOP_FILE="$SCRIPT_DIR/.recording"

if [ "$#" -ne 1 ] || [ -z "$1" ]; then
  echo "用法：bash record_live.sh 输出.flv" >&2
  exit 2
fi
OUT="$1"
if [ -e "$OUT" ] && [ ! -f "$OUT" ]; then
  echo "错误：输出路径不是普通文件：$OUT" >&2
  exit 2
fi
if [ -s "$OUT" ]; then
  echo "错误：输出文件已有内容；为防止拼入第二个 FLV 头，请换一个新文件。" >&2
  exit 2
fi
if [ ! -r "$URL_FILE" ]; then
  echo "错误：读不到直播地址文件：$URL_FILE" >&2
  exit 2
fi
URL=$(tr -d '\r\n' < "$URL_FILE")
if [ -z "$URL" ]; then
  echo "错误：直播地址为空，请先更新 live_url.txt。" >&2
  exit 2
fi
if ! command -v curl >/dev/null 2>&1; then
  echo "错误：找不到 curl。" >&2
  exit 1
fi
if ! : >> "$OUT"; then
  echo "错误：无法写入输出文件：$OUT" >&2
  exit 1
fi

touch "$STOP_FILE" || exit 1
trap 'rm -f -- "$STOP_FILE"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

first=1
empty_connects=0
while [ -f "$STOP_FILE" ]; do
  before=$(wc -c < "$OUT")
  if [ "$first" = "1" ]; then
    curl -sS --fail --connect-timeout 10 --speed-limit 1 --speed-time 30 -o - "$URL" >> "$OUT"
    rc=$?
  else
    # 重连后要跳过 13 字节 FLV 文件头（9 字节头 + 4 字节 PrevTagSize）
    curl -sS --fail --connect-timeout 10 --speed-limit 1 --speed-time 30 -o - "$URL" | tail -c +14 >> "$OUT"
    rc=$?
  fi
  after=$(wc -c < "$OUT")

  # 只有真正写入过数据，后续连接才按“重连”处理并跳过 FLV 头。
  if [ "$after" -gt "$before" ]; then
    first=0
    empty_connects=0
  elif [ "$rc" -eq 0 ]; then
    empty_connects=$((empty_connects + 1))
  else
    empty_connects=0
  fi

  if [ "$rc" -eq 22 ]; then
    echo "HTTP 地址已失效或直播已结束（curl 22），停止重连；请更新 live_url.txt。" >&2
    exit 1
  fi
  if [ "$empty_connects" -ge 4 ]; then
    echo "连续 4 次连接没有新数据，判定直播结束。"
    if [ ! -s "$OUT" ]; then
      exit 1
    fi
    break
  fi
  if [ -f "$STOP_FILE" ]; then
    sleep 1
  fi
done
