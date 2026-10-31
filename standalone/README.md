# 独立命令行录制脚本

不需要网页界面，也不需要安装 Python 第三方包。适合临时录一场直播，或放进已有脚本里调用。

| 脚本 | 适用场景 | 输出 |
|---|---|---|
| `video_rec.py` | 用 ffmpeg 直接转封装，支持限时录制 | 分片 MP4，中断后已录部分通常仍可播放 |
| `record_live.sh` | 纯 curl 拉流，断流自动重连续录 | FLV，录完需再转封装 MP4 |

两个脚本都不访问网络以外的本地服务，也不会上传任何数据。直播地址含签名，请勿把 `live_url.txt` 或完整地址提交到仓库。

## 获取代码

```bash
git clone https://github.com/gui8912/wxchannels-downloader.git
cd wxchannels-downloader/standalone
```

Windows 运行 Shell 版需要 Git Bash（安装 Git for Windows 时自带）。`live_url.txt`、`.recording` 和录制产物已加入 `.gitignore`。

## video_rec.py

依赖 Python 3.8+ 和 ffmpeg。ffmpeg 默认从 `PATH` 查找，也可以显式指定：

```bash
# Windows CMD
set FFMPEG=D:\tools\ffmpeg\bin\ffmpeg.exe
python video_rec.py "<直播流URL>" 3600

# Windows PowerShell
$env:FFMPEG = "D:\tools\ffmpeg\bin\ffmpeg.exe"
python video_rec.py "<直播流URL>" 3600

# 不限时，录到直播结束或按 Ctrl+C
python video_rec.py "<直播流URL>"
```

输出保存在脚本所在目录，文件名形如 `视频号直播_20261031_064500.mp4`。同一秒内重名会自动变成 `(2)`、`(3)`，不会覆盖已有录像。ffmpeg 失败时程序返回非 0 退出码。

## record_live.sh

依赖 Bash、curl，以及 `tail` / `wc`（Git Bash 自带）。先把地址写入脚本同目录的 `live_url.txt`：

```bash
bash record_live.sh 我的直播.flv
```

- 断流后自动重连，重连时跳过服务端重发的 13 字节 FLV 文件头。
- 地址返回 403/404 时停止，不会把错误页写进录像。
- 连续 4 次连接都没有新数据时，判定直播结束。
- Ctrl+C 停止，也可以删除脚本目录的 `.recording`；退出时会自动清理该标记。
- 输出文件必须是新文件或空文件。已有内容时脚本会拒绝追加，避免拼入第二个 FLV 头。

录完后转封装为 MP4（不重编码）：

```bash
ffmpeg -i 我的直播.flv -c copy 我的直播.mp4
```

## 回归测试

```bash
python test_recorders.py
```

测试使用本地模拟 HTTP 服务，不会访问真实直播地址，覆盖：

- 真实 H.264 FLV 转分片 MP4
- ffmpeg 失败退出码
- 非法时长、同名输出防覆盖
- 首次空响应后保留 FLV 头
- HTTP 403 停止且不污染录像
- 正常播完后自动退出

需要本机有 Python、bash 和 ffmpeg；缺少 ffmpeg 时真实转封装用例会跳过。

## 使用边界

仅用于备份自己有权访问的内容。录制他人课程、讲座或直播前请取得授权，并遵守平台条款及《著作权法》《个人信息保护法》等规定。完整免责声明见仓库根目录 [README](../README.md#免责声明)。
