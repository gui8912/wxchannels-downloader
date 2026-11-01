# wxchannels-downloader v5.6.0 源代码审计报告

日期：2026-11-01  
范围：`D:\WorkBuddy\工具\视频号录制器`，含直播录制、回放下载/解密、嗅探、Bridge、Web UI  
备份：`D:\WorkBuddy\工具\_audit_backup_20261101\`

## 1. 同类方案调研小结

### 1.1 主要样本

| 项目 | 规模/活跃度 | 技术栈 | 可借鉴点 |
|---|---:|---|---|
| `nobiyou/wx_channel` | 2863 stars，2026-10-28 推送 | Go + SunnyNet + Gopeed | 下载后校验、任务级暂停/恢复、断点续传、失败重试、文件名模板/长度控制、本地 API 鉴权、数据库持久化 |
| `dhvbjvvb/jicun` | 189 stars，2026-10-10 推送 | Flutter + Kotlin | 大文件分段 Range、段级重试、按进展而非固定次数重试、文件头嗅探真实格式、失败/取消不留垃圾、仓库卫生检查 |
| `ltaoo/wx_channels_download` | 10286 stars，2026-10-29 推送 | Go | 代理/证书安装流程、页面下载按钮注入、按画质选择下载 |
| `putyy/res-downloader` | 21014 stars，2026-10-27 推送 | Go + 桌面壳 | 多资源类型抽象、统一下载调度、跨平台打包 |
| `qiye45/wechatVideoDownload` | 5995 stars，2026-10-05 推送 | 抓包代理 + 前端注入 | 直播、回放、图片统一嗅探；回放解密链路 |

### 1.2 采纳的做法

1. **重试按“连续无进展”计数**：`jicun` 的 `ChunkAttempts.noteFailure(progress)` 和本项目实际网络表现一致。已用于 `Downloader`，避免大文件续传 8 次后被误判失败。
2. **HTTP 200 忽略 Range 是正常情况**：`jicun` 明确处理 RFC 7233 的 200/206 分支，微信 CDN 也可能返回 200。本项目保留单连接策略，在 200 时安全地从 0 覆盖写，避免按偏移写坏文件。
3. **失败不留下最终成品垃圾**：解密先写临时文件，成功后再原子改名。
4. **文件名按 Windows 规则收口**：控制字符、保留设备名、尾点和非法字符统一处理。
5. **本机接口不能只靠“监听 127.0.0.1”**：增加 Host、固定端口 Origin、CORS 白名单和文件类型白名单。
6. **持久化写入必须原子化并留回退点**：唯一临时文件 + `fsync` + 原子替换 + `.bak`。
7. **任务状态迁移必须原子化**：出队、置运行状态和启动线程放进同一临界区。

### 1.3 暂不采纳

- **分段并行下载**：收益主要是速度，但会显著增加 Range 边界、CDN 限速、分片拼接和取消清理的复杂度。当前稳定性优先，保留单连接断点续传。
- **把任务持久化迁到 SQLite**：项目当前任务量小，`tasks.json + .bak` 足够；引入数据库会扩大迁移和兼容面。
- **多平台解析**：偏离“录制/下载自己有权访问的视频号内容”的核心目标。
- **直接复制 `jicun`/`nobiyou` 代码**：技术栈不同（Dart/Kotlin、Go），本轮只采纳可验证的设计判据，没有复制第三方源码，因此无需新增代码归属声明。

### 1.4 需要规避的坑

- 只信任 `Content-Type`，不验证实际文件头。
- 固定次数重试导致大文件在 80% 处被判失败。
- 取消/失败后留下 `.mp4` 半成品。
- `allow_hosts` 用过宽的子串正则，把仿冒域名也解密。
- 本机 HTTP 服务只校验 `Sec-Fetch-Site`，未校验 Host，存在 DNS rebinding 风险。
- 固定 `.tmp` 文件名并发写导致 JSON 损坏。

## 2. 项目理解小结

这是一个 Windows 本机个人工具，核心目标不是“全网下载器”，而是：

1. **直播录制**：从已授权的视频号 FLV 地址拉流，断流重连，停止后用 ffmpeg 转封装 MP4。
2. **回放下载**：HTTP Range 下载，保留 `.part` 续传；对前 128 KB 被 Isaac64 密钥流 XOR 的 MP4 调用官方 wasm 解密。
3. **地址嗅探**：mitmproxy 只解密视频号页面和直播 CDN；页面 HTML 注入 JSBridge 钩子，拿 `url + decode_key`。
4. **队列与 UI**：串行任务调度、本地 Web 页面、状态持久化、失败恢复。
5. **边界**：仅监听 `127.0.0.1`，不提供内容，不绕过付费/权限；录制他人内容需授权。

本次没有改变上述定位、接口路径、页面操作方式或输出格式。

## 3. 审计问题汇总

### 3.1 本轮已修复

| 级别 | 问题 | 后果 |
|---|---|---|
| P1 | 启动时先开调度/加载任务，再检查已有实例 | 第二个进程可抢同一任务、双写 FLV、互相覆盖 `tasks.json` |
| P1 | `stop_task` 在线程刚退出时写“已取消”但不释放 `active` | 队列永久停摆，只能重启 |
| P1 | 出队与 `start_task` 非原子 | “界面已取消、后台仍开录” |
| P1 | `save_tasks` 锁外写固定 `.tmp` | 并发写坏 JSON，任务表下次启动整体消失 |
| P1 | `load_tasks` 对字段类型零校验 | 一条脏数据导致进程启动崩溃 |
| P1 | 下载遇 HTTP 416 后每次都用同一偏移重试 | 已下完文件永远无法转正 |
| P1 | 下载重试固定 8 次 | 有进展的大文件也会被误判失败 |
| P1 | 本机 API 无条件信任 `Sec-Fetch-Site`，不校验 Host | DNS rebinding 可调用全部接口 |
| P1 | `/api/open` 可打开任意可执行文件/UNC 路径，白名单根可由请求写入 | 任意程序执行/SMB 认证风险 |
| P1 | Bridge 写入口 `Access-Control-Allow-Origin: *` 且不校验 Origin | 任意网页可伪造嗅探结果、覆盖地址 |
| P2 | 解密直接写最终 `.mp4` | 失败/超时留下半截成品 |
| P2 | Windows 保留名只匹配无扩展名形式 | `COM1.txt` 等仍可能打开设备/失败 |
| P2 | 前端把任务/结果 `id` 直接拼进 HTML/onclick | 被篡改的 JSON 可形成存储型 XSS |
| P2 | 采集器递归无深度上限 | 深层 JSON 触发 `RecursionError`，整条响应丢失 |
| P2 | Bridge 宽度字段直接 `int()` | 一个脏字段丢掉整批命中 |
| P2 | `allow_hosts` 仅含页面域名 | 直播 `.flv` 地址嗅探静默失效 |
| P2 | 嗅探重复点击会覆盖“原代理备份” | 停止后用户原代理丢失 |
| P2 | 停止嗅探时还原代理异常会跳过进程清理 | 残留 mitmdump、代理悬空 |
| P2 | 崩溃自愈只依赖前端轮询 | 页面关掉后代理可长期指向死端口 |
| P2 | Bridge 使用 `SO_REUSEADDR`，无请求超时 | 端口占用误判、半截请求长期占线程 |
| P2 | 嗅探 `dedup` 把所有直播塌缩成一条 | 清理重复时误删直播结果 |

### 3.2 已确认但本轮不修改（保留待办）

| 级别 | 问题 | 原因 |
|---|---|---|
| P2 | `retry_task` 不区分“下载失败/解密失败/转封装失败” | 需要新增阶段字段和收尾重入路径，属于独立小重构；当前先保证不丢数据，用户可重新入队 |
| P2 | 未知总大小时，连接提前结束仍可能被当完成 | 无长度时缺少可靠的 MP4 完整性判据，贸然判断会误杀正常流 |
| P2 | 分段并行下载未实现 | 稳定性优先，且会显著扩大取消/拼接/限速处理面 |
| P3 | 任务列表无数量上限 | 个人使用量小；清理入口已存在，贸然自动删记录会丢历史 |
| P3 | `ffmpeg` 配置接口未做可执行文件签名校验 | 本机接口已加 Host/Origin，本机恶意进程本来就有同等权限 |

## 4. 变更总览

### 修复

- 单实例与启动顺序。
- `active` 泄漏和取消/启动竞态。
- `tasks.json` 原子写、`.bak` 回退、逐条容错。
- 下载 416 自愈、按进展重试、永久 HTTP 错误快速失败。
- 本机 API Host/Origin/文件类型/路径校验。
- Bridge Origin/CORS/超时/端口绑定保护。
- 嗅探直播域名、重复启动、代理还原、进程自愈。
- 文件名保留名与控制字符。
- 解密临时文件。
- 前端 ID/数字字段防御。
- 采集器递归深度与调试文件清理。

### 重构

- 抽出 `_write_json`、`_read_json_file`、`_as_int`、`_as_float`。
- 任务启动改为在同一临界区复核状态、占用 active、启动线程。
- 嗅探代理还原逻辑统一为同一套 enable/port 检查。

### 新增功能

无独立业务功能。新增的是稳定性能力：单实例保护、任务文件自动备份恢复、HTTP 416 自动纠错、接口来源防护。未新增依赖。

## 5. 分点变更详情

### 5.1 单实例与启动顺序

- **代码位置**：`wxlive_recorder.py:2123 serve()`
- **原始问题**：`manager` 和 `load_tasks()` 在实例检查之前启动；第二个进程即使发现已有实例，也已经可能抢到任务；端口占用时自动换到 8766，形成双实例。
- **修改思路**：先检查已有实例；再绑定固定 8765；绑定成功后才恢复任务、启动调度和 Bridge。重启模式只等待端口，不再换端口。
- **代码片段**：

```python
# modified: 单实例检查必须在 load_tasks/manager/bridge 之前。
if not from_restart:
    info = running_instance(port)
    if info:
        ...
        return
httpd = RecorderServer(("127.0.0.1", port), Handler)
```

### 5.2 active 泄漏与启动竞态

- **代码位置**：`wxlive_recorder.py:978 start_task()`、`1024 stop_task()`、`manager()`
- **原始问题**：active 任务线程刚退出时点停止，会写“已取消”却不释放 `active`；manager 只对 `RUNNING_STATES` 收尾，任务永久卡死。
- **修改思路**：active 任务只发停止信号，状态由 manager 收尾；出队、状态切换、线程启动放在同一把锁内，启动前再次校验状态。
- **代码片段**：

```python
# modified: active 任务只发停止信号，状态必须留给 manager 收尾
if STATE["active"] == t["id"]:
    if r:
        r.stop()
    return {"ok": True}
```

### 5.3 任务文件原子持久化与容错恢复

- **代码位置**：`wxlive_recorder.py:88 _write_json()`、`754 save_tasks()`、`777 load_tasks()`
- **原始问题**：固定 `.tmp` 并发写会生成坏 JSON；`load_tasks` 类型转换异常会让进程起不来；一条坏记录导致整个列表中断。
- **修改思路**：唯一临时文件名 + `flush/fsync` + 原子替换；保存前把旧文件复制为 `.bak`；加载时主文件失败回退 `.bak`，逐条 try/跳过，数值字段统一转换。
- **代码片段**：

```python
# modified: 原子落盘统一入口；唯一临时名 + fsync
tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp"
```

### 5.4 下载 416 与重试策略

- **代码位置**：`wxlive_recorder.py:479 Downloader.run()`、`521 _fetch_once()`
- **原始问题**：`.part` 已完整时发 Range 得到 416，原逻辑继续用同一偏移重试；总尝试次数固定 8 次。
- **修改思路**：解析 416 的 `Content-Range: bytes */N`；已达总大小直接完成；偏移超过总大小或未知时，只清零重下一次；重试按连续无进展计数，403/404/410 直接失败。
- **代码片段**：

```python
# modified: 重试按“连续无进展”计数，成功续传不会消耗重试次数
if remote_total and start == remote_total:
    self.state = "下载完成"
    return
```

### 5.5 本机 API 防护

- **代码位置**：`wxlive_recorder.py:1750 _host_ok()`、`1755 _same_origin()`、`1679 open_path()`
- **原始问题**：`Sec-Fetch-Site: same-origin` 直接放行；Origin 放行任意 localhost 端口；不校验 Host；文件白名单可通过修改任务目录扩大；UNC 目录可触发 SMB。
- **修改思路**：Host 必须等于 `127.0.0.1:8765` 或 `localhost:8765`；Origin/Referer 必须精确匹配固定端口；`Sec-Fetch-Site` 不再单独放行；文件只允许媒体/图片扩展名；路径 `realpath` 解析；拒绝 UNC/设备路径。
- **代码片段**：

```python
# modified: Host 是硬门禁；Sec-Fetch-Site 只作辅助信号
if not self._host_ok():
    return False
```

### 5.6 Bridge 入口防护

- **代码位置**：`bridge_server.py:28 ALLOWED_ORIGINS`、`298 Handler`、`410 start()`
- **原始问题**：CORS 为 `*`，任意网页可 POST；无超时；Windows 下 `SO_REUSEADDR` 端口检测失效；8 个端口全失败仍留旧端口文件。
- **修改思路**：只接受 `https://channels.weixin.qq.com`（可用环境变量增加测试源）；CORS 回显白名单 Origin；校验 Host、路径 `/add`；10 秒超时；`allow_reuse_address=False`；失败清理端口文件。
- **代码片段**：

```python
# modified: 只回白名单 Origin，不再使用通配符 *
if origin in ALLOWED_ORIGINS:
    self.send_header("Access-Control-Allow-Origin", origin)
```

### 5.7 嗅探范围与代理自愈

- **代码位置**：`sniffer.py:51 ALLOW_HOSTS`、`93 write_proxy()`、`548 start()`、`730 stop()`、`819 restore_stale_proxy()`
- **原始问题**：只解密页面域名导致直播 `.flv` 嗅探失效；`allow_hosts` 子串匹配可误伤仿冒域名；重复开始会覆盖原代理备份；停止还原异常会跳过进程清理；无浏览器轮询时不自愈；空 ProxyServer 不写回。
- **修改思路**：锚定正则只放行页面和 `*.wxlivecdn.com`；重复 start 幂等；已死进程先还原再启动；stop 在 finally 杀进程；主程序常驻嗅探线程定期调用 `status()`；空服务器也显式写回。
- **代码片段**：

```python
# modified: 直播地址在 wxlivecdn 上；只解密页面域名会让直播嗅探静默失效。
ALLOW_HOSTS = r"^(?:channels\.weixin\.qq\.com|(?:[a-z0-9-]+\.)*wxlivecdn\.com)(?::\d+)?$"
```

### 5.8 文件名与解密收尾

- **代码位置**：`wxlive_recorder.py:166 safe_filename()`、`1220 _finish_replay()`
- **原始问题**：`COM1.txt`/`LPT9.mp4` 不是合法文件名；控制字符可触发 `ValueError`；解密直接写最终 mp4，失败留半成品。
- **修改思路**：按主文件名判断保留名；删除全部控制字符；解密先写 `.mp4.part`，成功 `os.replace`，失败清理临时文件。
- **代码片段**：

```python
# modified: 保留名按主文件名判断，COM1.txt / LPT9.mp4 同样不能落盘
stem = os.path.splitext(s)[0].upper()
```

### 5.9 前端与采集器防御

- **代码位置**：`ui.py:238`、`502`、`528`；`capture_addon.py:57`、`112`、`171`、`402`；`bridge_server.py` `_int()`
- **原始问题**：前端 ID 未转义；NaN/字符串数字导致渲染异常；采集递归无上限；宽高非数字导致整批丢弃；`_debug` 清理会删非本工具文件。
- **修改思路**：数字统一 `Number.isFinite` 校验；非法 ID 不渲染；递归深度 14；调试文件只清理 `*.bin` 并用纳秒后缀；按主机名精确匹配直播/注入域名。
- **代码片段**：

```javascript
// modified: id 必须是正整数，不能把 tasks/sniff 文件里的脏字符串拼进 HTML/onclick
const id = Number(h.id);
```

## 6. 完整整合后的源代码

完整源码已直接落在项目目录，未在报告中重复粘贴 4500+ 行，避免源码与报告双份维护。所有修改/新增代码块均已用单行注释 `# modified` 或 `// modified` 标注（部分说明性文字更新未重复标注）。

| 文件 | 说明 |
|---|---|
| `wxlive_recorder.py` | 主程序；修改了持久化、Downloader、状态机、API 安全、解密收尾、启动顺序 |
| `bridge_server.py` | Bridge 来源校验、CORS、超时、端口绑定 |
| `sniffer.py` | 白名单、代理还原、启动/停止、自愈 |
| `capture_addon.py` | 域名精确匹配、递归深度、调试文件 |
| `ui.py` | ID/数字字段前端防御 |
| `README.md` | v5.6.0 版本记录与嗅探范围说明 |
| `_audit_tests.py` | 12 项本地审计回归（开发自测，不随发布） |
| `_selftest.py` | 28 项接口/安全回归（开发自测，不随发布） |

标注核对：

```bash
grep -rn "modified" wxlive_recorder.py bridge_server.py sniffer.py capture_addon.py ui.py
```

## 7. 风险提示

1. **兼容性变化**
   - 只监听固定 `8765`，不再自动换到 8766；端口被其他程序占用时直接提示退出。
   - `/api/open` 文件类型收紧为媒体/图片；打开 `exe`、`bat`、`lnk` 会拒绝。
   - 非固定端口或非 `127.0.0.1/localhost` 的 Origin 调用 API 会返回 403。
   - Bridge 只接受视频号页面来源；本地测试需设置 `WX_BRIDGE_ALLOW_ORIGIN`。
2. **嗅探范围扩大**：为抓直播地址，新增 `*.wxlivecdn.com` TLS 解密。仍不使用全量解密；但需要系统信任 mitmproxy 根证书。
3. **任务文件**：新增 `tasks.json.bak`；首次升级时会从旧格式逐条容错加载，损坏记录会被跳过并记日志。
4. **未做真实直播回归**：当前没有有效签名地址，所有网络测试均为本地模拟；真实断流、CDN 403、微信注入兼容仍需下次直播验证。
5. **未解决项**：见 3.2；其中“重试阶段区分”和“未知大小完整性”是下一轮优先项。

## 8. 验证方案

### 8.1 自动测试

```bash
cd /d/WorkBuddy/工具/视频号录制器
python -m py_compile wxlive_recorder.py ui.py sniffer.py capture_addon.py bridge_server.py wxdecrypt.py
python _audit_tests.py
python _selftest.py
```

预期：

- `_audit_tests.py`：12/12 通过。
- `_selftest.py`：28/28 通过。
- 前端脚本由 `node --check` 在 `_audit_tests.py` 中校验。

### 8.2 单实例验证

保持服务运行时再执行：

```bash
python wxlive_recorder.py --no-browser
```

预期输出“已经有一个在跑，直接复用”，进程立即退出，任务数不变。

### 8.3 安全验证

```bash
# 伪造 Host：应 403
curl -H "Host: attacker.example:8765" http://127.0.0.1:8765/api/status

# 其它 localhost 端口：应 403
curl -X POST -H "Origin: http://localhost:3000" \
  -H "Content-Type: application/json" -d '{}' \
  http://127.0.0.1:8765/api/task/clear
```

### 8.4 真实断点续传

1. 用有效回放地址下载到中途，停止服务。
2. 重新启动，点“继续”。
3. 确认日志从已有 `.part` 字节继续，不出现 416 死循环。
4. 完成后的 MP4 用播放器检查时长和结尾。

### 8.5 真实直播

1. 取得有效 FLV 地址，录制 2 分钟后停止。
2. 检查 MP4 可播放、时长与录制时间接近。
3. 人为断网 10 秒，确认重连后文件仍可完整播放。
