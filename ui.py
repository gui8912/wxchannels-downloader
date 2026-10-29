# -*- coding: utf-8 -*-
"""前端页面（单文件 HTML）。

单独拆出来是为了不用在 Python 源码里翻几百行字符串 —— 改样式、加按钮
都只在 ui.py 里动，主程序逻辑一行不用碰。
"""

HTML = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>视频号直播 / 回放下载器</title>
<style>
*{box-sizing:border-box}
body{margin:0;padding:20px 18px 60px;background:#0d1117;color:#e6edf3;
     font:14px/1.6 "Microsoft YaHei UI","PingFang SC",system-ui,sans-serif}
.wrap{max-width:1000px;margin:0 auto}
h1{font-size:19px;margin:0;font-weight:600;display:flex;align-items:center;gap:9px;flex-wrap:wrap}
.sub{color:#7d8590;font-size:12.5px;margin:6px 0 16px}
.ver{font:11px/1 Consolas,monospace;color:#6e7681;background:#21262d;
     border:1px solid #30363d;border-radius:5px;padding:3px 7px}
.card{background:#161b22;border:1px solid #21262d;border-radius:10px;padding:15px 17px;margin-bottom:13px}
.card.alert{border-color:#9e6a03;background:#1e1a0d}
label{display:block;font-size:12.5px;color:#8b949e;margin-bottom:6px}
textarea,input[type=text],input[type=number]{width:100%;background:#0d1117;color:#e6edf3;
  border:1px solid #30363d;border-radius:6px;padding:8px 11px;font:12.5px/1.5 Consolas,monospace;outline:none}
textarea{height:68px;resize:vertical;word-break:break-all}
textarea:focus,input:focus{border-color:#1f6feb;box-shadow:0 0 0 3px rgba(31,111,235,.15)}
.hint{font-size:12px;color:#8b949e;margin-top:7px}
.hint.warn{color:#d29922}.hint.bad{color:#f85149}.hint.ok{color:#3fb950}
.row{display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap}
.row>div{flex:1;min-width:180px}
button{cursor:pointer;border:1px solid #30363d;background:#21262d;color:#e6edf3;
  border-radius:6px;padding:7px 14px;font-size:13px;font-family:inherit;transition:.15s;white-space:nowrap}
button:hover:not(:disabled){background:#30363d;border-color:#484f58}
button:disabled{opacity:.4;cursor:not-allowed}
button.primary{background:#238636;border-color:#2ea043;font-weight:600}
button.primary:hover:not(:disabled){background:#2ea043}
button.danger{background:#8b1d1d;border-color:#b62324}
button.danger:hover:not(:disabled){background:#b62324}
button.ghost{background:transparent}
button.mini{padding:3px 8px;font-size:12px;border-radius:5px}
#pill{display:inline-block;padding:3px 11px;border-radius:99px;font-size:12px;
  background:#21262d;color:#8b949e;margin-left:2px}
#pill.on{background:#1a7f37;color:#fff}
#pill.rec{background:#b62324;color:#fff;animation:pulse 1.6s infinite}
@keyframes pulse{50%{opacity:.5}}
table{width:100%;border-collapse:collapse;font-size:13px;margin-top:4px}
th{text-align:left;font-weight:500;color:#7d8590;font-size:11.5px;
  padding:6px 7px;border-bottom:1px solid #21262d;white-space:nowrap}
th.sortable{cursor:pointer;user-select:none}
th.sortable:hover{color:#c9d1d9}
.ar{font-size:9px;color:#58a6ff;margin-left:1px;opacity:.9}
td{padding:6px 7px;border-bottom:1px solid #161b22;vertical-align:middle}
tr:hover td{background:#1c2128}
table input[type=text]{background:#0d1117;border:1px solid #30363d;border-radius:4px;
  padding:4px 8px;color:#e6edf3;font:12.5px "Microsoft YaHei UI",sans-serif;width:100%}
.tag{font-size:11.5px;color:#8b949e;white-space:nowrap}
.tag.rp{color:#79c0ff;border:1px solid #1f3a5f;border-radius:4px;padding:0 5px}
.tag.lv{color:#3fb950;border:1px solid #1f4a2a;border-radius:4px;padding:0 5px}
.tag.pl{color:#d2a8ff;border:1px solid #4c2d6b;border-radius:4px;padding:0 4px;background:#1d1526}
.filt{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:7px 0 9px;
  border-bottom:1px solid #21262d;margin-bottom:2px}
.filt input[type=number]{width:78px;padding:4px 8px;font-size:12.5px}
.chk{display:flex;align-items:center;gap:5px;cursor:pointer;color:#8b949e;
  margin:0;font-size:12.5px;user-select:none}
.st{font-size:11.5px;padding:2px 8px;border-radius:99px;white-space:nowrap;background:#21262d;display:inline-block}
.st.rec{background:#8b1d1d;color:#fff}
.st.ok{background:#1a7f37;color:#fff}
.st.wait{background:#3d2f00;color:#d29922}
.st.err{background:#3d1414;color:#f85149}
.qpos{font:11px/1 Consolas,monospace;color:#58a6ff;background:#132239;
      border:1px solid #1f3a5f;border-radius:4px;padding:2px 5px}
.note{font-size:11.5px;color:#6e7681;display:block;margin-top:2px;word-break:break-all}
.note.bad{color:#f85149}.note.warn{color:#d29922}
.bar{height:4px;background:#21262d;border-radius:2px;margin-top:4px;overflow:hidden}
.bar>i{display:block;height:100%;background:#1f6feb;transition:width .4s}
#log{height:200px;overflow:auto;background:#010409;border:1px solid #21262d;border-radius:8px;
  padding:10px 12px;font:12px/1.65 Consolas,monospace;color:#8b949e;white-space:pre-wrap;word-break:break-all}
#log .ok{color:#3fb950}#log .bad{color:#f85149}#log .warn{color:#d29922}#log .blue{color:#58a6ff}
.foot{color:#484f58;font-size:11.5px;text-align:center;margin-top:16px}
.mono{font-family:Consolas,monospace}
.sp{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
/* toast */
#toasts{position:fixed;right:18px;bottom:18px;display:flex;flex-direction:column;
  gap:8px;z-index:99;max-width:360px}
#toasts div{background:#1f242c;border:1px solid #30363d;border-left:3px solid #1f6feb;
  border-radius:7px;padding:9px 13px;font-size:13px;box-shadow:0 6px 18px rgba(0,0,0,.45);
  animation:in .18s ease-out}
#toasts div.bad{border-left-color:#f85149}#toasts div.ok{border-left-color:#3fb950}
#toasts div.warn{border-left-color:#d29922}
@keyframes in{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
/* 体检弹层 */
#modal{position:fixed;inset:0;background:rgba(1,4,9,.72);display:none;
  align-items:center;justify-content:center;z-index:100;padding:20px}
#modal.show{display:flex}
#modal .box{background:#161b22;border:1px solid #30363d;border-radius:12px;
  padding:18px 20px;max-width:520px;width:100%;max-height:80vh;overflow:auto}
#modal h3{margin:0 0 12px;font-size:15px}
.hi{display:flex;gap:9px;padding:7px 0;border-bottom:1px solid #21262d;font-size:12.5px}
.hi b{font-weight:600}.hi .d{color:#8b949e;font-family:Consolas,monospace;
  font-size:11.5px;word-break:break-all}
.dot{width:8px;height:8px;border-radius:50%;flex:0 0 auto;margin-top:6px}
.dot.g{background:#3fb950}.dot.r{background:#f85149}
</style></head><body><div class="wrap">

<h1>视频号直播 / 回放下载器 <span id="pill">空闲</span>
  <span class="ver" id="ver">v—</span>
  <span style="flex:1"></span>
  <button class="mini ghost" onclick="showHealth()">🩺 体检</button>
  <button class="mini ghost" onclick="restartSvc()">🔄 重启服务</button>
</h1>
<div class="sub">嗅探地址 → 勾选入队 → 直播边录边存、回放下载后自动解密 → MP4</div>

<div class="card alert" id="warn-card" style="display:none">
  <div id="warn-list" class="hint bad" style="margin:0"></div>
</div>

<!-- ① 嗅探 -->
<div class="card">
  <div class="row" style="align-items:center">
    <div style="flex:1">
      <label style="margin-bottom:2px">① 拿到地址 · 自动嗅探</label>
      <div class="hint" id="sniff-msg">点「开始嗅探」→ 在微信里打开视频号直播间 / 回放并播放几秒 → 地址会进下面的列表</div>
    </div>
    <div style="flex:0 0 auto;min-width:0">
      <button id="b-sniff" onclick="toggleSniff()">📡 开始嗅探</button>
    </div>
  </div>
</div>

<!-- ② 嗅探结果 -->
<div class="card" id="hits-card" style="display:none">
  <div class="row" style="align-items:center;margin-bottom:4px">
    <label style="margin:0;flex:1">② 嗅探结果 <span class="tag" id="hit-count"></span></label>
    <div class="sp">
      <button class="mini" onclick="dedupHits()">🧹 清理重复</button>
      <button class="mini" onclick="clearHits()">清空列表</button>
      <button class="mini primary" onclick="queueSelected()">批量入队</button>
    </div>
  </div>
  <div class="filt">
    <label class="chk"><input type="checkbox" id="only-playing"> 只看播放页的视频</label>
    <span class="tag">体积 ≥</span>
    <input type="number" id="min-mb" value="0" min="0" step="50">
    <span class="tag">MB</span>
    <button class="mini ghost" onclick="setMinMb(0)">不限</button>
    <button class="mini ghost" onclick="setMinMb(150)">150</button>
    <button class="mini ghost" onclick="setMinMb(300)">300</button>
    <button class="mini ghost" onclick="setMinMb(500)">500</button>
    <span class="tag" id="filter-info"></span>
  </div>
  <table>
    <thead><tr><th style="width:28px"><input type="checkbox" id="all" onclick="toggleAll()"></th>
      <th style="width:64px" class="sortable" onclick="setSort('t')" title="点击按时间排序">时间<span class="ar" data-k="t"></span></th>
      <th style="width:86px">类型</th>
      <th class="sortable" onclick="setSort('name')" title="点击按名称排序">名称（可改）<span class="ar" data-k="name"></span></th>
      <th style="width:134px" class="sortable" onclick="setSort('size')" title="点击按大小排序（未知大小的始终排在最后）">大小 / 有效期<span class="ar" data-k="size"></span></th>
      <th style="width:60px">状态</th>
      <th style="width:120px">操作</th></tr></thead>
    <tbody id="hits"></tbody>
  </table>
  <div class="hint" id="hits-empty" style="display:none"></div>
</div>

<!-- ③ 手动地址 -->
<div class="card">
  <label>③ 手动填地址（不走嗅探时用）</label>
  <textarea id="url" placeholder="直播流：https://pull-m1.wxlivecdn.com/trtc_xxx/orig_xxx_orig.flv?...&#10;回放：https://finder.video.qq.com/251/20302/stodownload?...（加密的会自动解密）"></textarea>
  <div class="hint" id="exp">链接有效期：—</div>
</div>

<!-- ④ 保存设置 -->
<div class="card">
  <div class="row">
    <div><label>保存目录</label><input type="text" id="dir" onchange="saveDir()"></div>
    <div style="flex:0 0 200px;min-width:150px"><label>课程名称（决定文件名）</label>
      <input type="text" id="cname" placeholder="留空 = 视频号直播"></div>
    <div style="flex:0 0 90px;min-width:74px"><label>限时(分)</label>
      <input type="number" id="limit" value="0" min="0" step="5"></div>
    <div style="flex:0 0 auto"><button onclick="openDir()">打开目录</button></div>
  </div>
  <div class="hint" id="fname">文件名预览：—</div>
  <div class="row" style="margin-top:10px;gap:18px">
    <label style="margin:0;display:flex;align-items:center;gap:5px;cursor:pointer">
      <input type="checkbox" id="audio"> 同时导出 MP3</label>
    <label style="margin:0;display:flex;align-items:center;gap:5px;cursor:pointer">
      <input type="checkbox" id="silence"> 静音 5 分钟自动停</label>
    <label style="margin:0;display:flex;align-items:center;gap:5px;cursor:pointer">
      <input type="checkbox" id="waitlive"> 未开播就蹲守（20 秒探测）</label>
    <label style="margin:0;display:flex;align-items:center;gap:5px;cursor:pointer"
           title="默认不加日期。勾上后在名称末尾追加当天日期，如「课程名2026-10-29」">
      <input type="checkbox" id="datetag"> 文件名加日期</label>
  </div>
  <button class="primary" style="margin-top:12px" onclick="startManual()">▶ 加入队列并开始</button>
</div>

<!-- ⑤ 任务 -->
<div class="card">
  <div class="row" style="align-items:center;margin-bottom:4px">
    <label style="margin:0;flex:1">⑤ 任务与进度 <span class="tag" id="qinfo"></span></label>
    <div class="sp">
      <button class="mini" onclick="pauseAll()" title="停下所有录制和下载，文件保留，随时可以恢复">⏸ 全部暂停</button>
      <button class="mini" onclick="resumeAll()" title="把暂停的任务重新排进队列">▶ 全部恢复</button>
      <button class="mini" onclick="dedupTasks()">🧹 清理重复项</button>
      <button class="mini" onclick="clearDone()">清理已结束</button>
    </div>
  </div>
  <table>
    <thead><tr><th style="width:40px">队列</th><th style="width:78px">状态</th><th>名称</th>
      <th style="width:66px">时长</th><th style="width:150px">大小 / 进度</th>
      <th style="width:190px">操作</th></tr></thead>
    <tbody id="tasks"></tbody>
  </table>
  <div class="hint" id="empty-tasks">还没有任务。</div>
</div>

<div class="card"><label>日志</label><div id="log"></div></div>
<div class="foot">本地运行 · 只监听 127.0.0.1 · 关掉网页不影响后台录制，关掉程序才会停</div>
</div>

<div id="toasts"></div>
<div id="modal" onclick="if(event.target===this)this.classList.remove('show')">
  <div class="box"><h3>环境体检</h3><div id="health-body"></div>
    <div style="margin-top:14px;text-align:right">
      <button onclick="document.getElementById('modal').classList.remove('show')">关闭</button></div>
  </div>
</div>

<script>
let since = 0, sniffing = false, hitsSig = "", taskSig = "", myVer = "";
const $ = id => document.getElementById(id);
const pad = n => String(n).padStart(2, "0");
const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmt = s => { s = Math.max(0, s|0); return pad(Math.floor(s/3600))+":"+pad(Math.floor(s%3600/60))+":"+pad(s%60); };
const size = b => b > 1073741824 ? (b/1073741824).toFixed(2)+" GB"
              : b > 1048576 ? (b/1048576).toFixed(1)+" MB" : (b/1024).toFixed(0)+" KB";

/* ---------- 通用 ---------- */
function toast(msg, type){
  if (!msg) return;
  const d = document.createElement("div");
  d.className = type || "";
  d.textContent = msg;
  $("toasts").appendChild(d);
  setTimeout(() => { d.style.opacity = "0"; d.style.transition = ".3s";
                     setTimeout(() => d.remove(), 320); }, 3200);
}
async function post(p, d){
  try{
    const r = await fetch(p, {method:"POST", headers:{"Content-Type":"application/json"},
                              body: JSON.stringify(d || {})});
    if (!r.ok) throw new Error("HTTP " + r.status);
    return await r.json();
  }catch(e){
    toast("请求失败：" + e.message + "（服务可能已停止，试试「重启服务」）", "bad");
    return {ok:false, msg:e.message};
  }
}
async function apiGet(p){
  try{
    const r = await fetch(p);
    return await r.json();
  }catch(e){ return {ok:false, msg:e.message}; }
}
/* 统一处理 {ok,msg}：失败弹红、成功弹绿（有 msg 才弹） */
function say(d, okMsg){
  if (!d) return false;
  if (d.ok){
    const m = okMsg || d.msg;
    if (m) toast(m, "ok");
    return true;
  }
  toast(d.msg || "操作失败", "bad");
  return false;
}

/* ---------- 有效期 / 文件名 ---------- */
function expiry(){
  const u = $("url").value.trim();
  const m = /[?&]txTime=([0-9a-fA-F]+)/.exec(u);
  const e = $("exp");
  if (!m){ e.className = "hint"; e.textContent = "链接有效期：—"; return; }
  const d = new Date(parseInt(m[1],16)*1000), left = d - Date.now();
  const s = d.toLocaleString("zh-CN",{hour12:false});
  if (left <= 0){ e.className = "hint bad"; e.textContent = "链接已于 " + s + " 过期，需要重新嗅探"; }
  else if (left < 3600000){ e.className = "hint warn";
    e.textContent = "链接有效期至 " + s + "（还剩 " + Math.floor(left/60000) + " 分钟）"; }
  else { e.className = "hint"; e.textContent = "链接有效期至 " + s; }
}
function previewName(){
  const raw = ($("cname").value || "").trim().replace(/[\\/:*?"<>|]/g, "_").slice(0,80);
  const d = new Date();
  const ds = d.getFullYear()+"-"+pad(d.getMonth()+1)+"-"+pad(d.getDate());
  /* 日期改成可选：默认不带，勾了才追加，且直接接在尾部不加分隔符 */
  const tail = $("datetag").checked ? ds : "";
  const shown = raw || "视频号直播";
  $("fname").textContent = "文件名预览：" + shown + tail + ".mp4";
}
function currentOpts(){
  return {dir:$("dir").value.trim(), limit:+$("limit").value||0,
          audio:$("audio").checked, silence:$("silence").checked,
          wait_live:$("waitlive").checked, with_date:$("datetag").checked};
}

/* ---------- 嗅探结果 ---------- */
function kindTag(h){
  const t = h.kind === "replay" ? '<span class="tag rp">回放</span>'
                                : '<span class="tag lv">直播</span>';
  return '<span style="white-space:nowrap;display:inline-flex;gap:3px;align-items:center">'
    + t + (h.played
      ? '<span class="tag pl" title="播放器真的请求过这个视频 —— 就是你在看的那条">▶</span>'
      : "") + '</span>';
}
function hitInfo(h){
  if (h.kind === "replay"){
    const mb = h.filesize ? size(h.filesize) : "—";
    return h.decode_key ? mb+' <span style="color:#3fb950">🔑</span>'
                        : mb+' <span style="color:#d29922">无key</span>';
  }
  return esc(h.expiry || "—");
}
function hitsSignature(hits){
  return hits.map(h => [h.id, h.kind, h.name, h.status, h.decode_key ? 1 : 0,
                        h.url ? h.url.length : 0].join("~")).join("|");
}

/* ---------- 嗅探结果排序 ----------
   回放列表动辄二三十条，从大到小排一下才知道哪个占地方。
   大小未知（接口没给 file_size）的一律沉底，不参与升降序。
   排序偏好记在 localStorage，下次打开还是这个排法。 */
let SORT = {key:"", dir:1};
try {
  const s = JSON.parse(localStorage.getItem("wxlive_sort") || "null");
  if (s && typeof s.key === "string") SORT = s;
} catch(e){}
function renderSortMarks(){
  document.querySelectorAll("#hits-card .ar").forEach(el => {
    el.textContent = (el.dataset.k === SORT.key) ? (SORT.dir > 0 ? " ▲" : " ▼") : " ⇅";
  });
}
function setSort(k){
  if (SORT.key === k) SORT.dir = -SORT.dir;
  else { SORT.key = k; SORT.dir = (k === "size" || k === "t") ? -1 : 1; }
  localStorage.setItem("wxlive_sort", JSON.stringify(SORT));
  hitsSig = "";
  renderSortMarks();
}
function sortHits(list){
  if (!SORT.key) return list;
  const k = SORT.key, d = SORT.dir, arr = list.slice();
  arr.sort((a, b) => {
    if (k === "size"){
      const va = a.filesize || 0, vb = b.filesize || 0;
      if (!va && !vb) return 0;
      if (!va) return 1;
      if (!vb) return -1;
      return (va - vb) * d;
    }
    if (k === "t") return String(a.t || "").localeCompare(String(b.t || "")) * d;
    return String(a.name || "").localeCompare(String(b.name || ""), "zh-CN") * d;
  });
  return arr;
}
/* ---------- 嗅探结果筛选 ----------
   回放页右侧的推荐列表会把几十个视频一起报上来，列表里混进一堆没人要的。
   两个筛子：① 只看播放器真的拉过流的那条（就是在看的那个）
             ② 体积下限（小视频多半是推荐位的碎片）*/
function filterSig(){
  return ($("only-playing").checked ? "1" : "0") + ":" + (+$("min-mb").value || 0);
}
function setMinMb(v){
  $("min-mb").value = v;
  onFilterChange();
}
function onFilterChange(){
  localStorage.setItem("wxlive_filter", filterSig());
  hitsSig = "";
  const all = (window.__hits || []);
  renderHits(all);
}
function applyFilter(list){
  const onlyPlay = $("only-playing").checked;
  const minMb = +$("min-mb").value || 0;
  return (list || []).filter(h => {
    if (onlyPlay && !h.played) return false;
    if (minMb > 0 && (h.filesize || 0) < minMb * 1048576) return false;
    return true;
  });
}
function renderHits(raw){
  const all = raw || [];
  window.__hits = all;
  const hits = sortHits(applyFilter(all));
  $("hits-card").style.display = all.length ? "" : "none";
  $("hit-count").textContent = all.length ? "（" + all.length + " 条）" : "";
  const shown = (hits.length !== all.length);
  $("filter-info").textContent = shown
    ? `筛选后 ${hits.length} / ${all.length} 条` : "";
  if (all.length && !hits.length){
    $("hits-empty").style.display = "";
    $("hits-empty").textContent =
      "当前筛选条件下没有结果。共抓到 " + all.length +
      " 条 —— 试试把体积下限调小，或者勾掉「只看播放页的视频」。";
  } else {
    $("hits-empty").style.display = "none";
  }
  const sig = hitsSignature(hits) + "#" + filterSig();
  if (sig === hitsSig) return;
  hitsSig = sig;
  const tb = $("hits");
  // 重建会丢勾选状态，先记下来再还原，否则一刷新勾选就白点了
  const checked = new Set([...tb.querySelectorAll(".pick:checked")].map(c => c.value));
  tb.innerHTML = "";
  hits.forEach(h => {
    const tr = document.createElement("tr");
    const on = checked.has(String(h.id)) ? " checked" : "";
    const joined = h.status === "已入队";
    tr.innerHTML = '<td><input type="checkbox" class="pick" value="'+h.id+'"'+on+'></td>'+
      "<td>"+esc(h.t||"")+"</td>"+
      "<td>"+kindTag(h)+"</td>"+
      '<td><input type="text" class="hname" data-id="'+h.id+'" value="'+esc(h.name)+'" placeholder="点这里填名称"></td>'+
      '<td><span class="tag">'+hitInfo(h)+"</span></td>"+
      '<td><span class="tag">'+esc(h.status||"")+"</span></td>"+
      '<td><button class="mini'+(joined?" ghost":"")+'" onclick="recHit('+h.id+')"'+
        (joined?' title="已在队列里，再点会复用同一条任务，不会重复下载"':' title="加入下载队列"')+'>'+
        (h.kind==="replay"?"入队下载":"入队录制")+
      '</button> <button class="mini ghost" onclick="delHit('+h.id+')" title="删除这条结果">✕</button></td>';
    tb.appendChild(tr);
  });
  tb.querySelectorAll(".hname").forEach(inp => {
    inp.addEventListener("change", async e => {
      await post("/api/hit/rename", {id:+e.target.dataset.id, name:e.target.value});
      hitsSig = "";
    });
  });
}
function toggleAll(){ document.querySelectorAll(".pick").forEach(c => c.checked = $("all").checked); }
async function recHit(id){
  const d = await post("/api/hit/record", Object.assign({id}, currentOpts()));
  say(d); hitsSig = ""; taskSig = "";
}
async function delHit(id){
  const d = await post("/api/hit/del", {id});
  if (!d.ok) toast(d.msg||"删除失败","bad");
  hitsSig = "";
}
async function clearHits(){
  const d = await post("/api/hit/clear", {});
  say(d); hitsSig = "";
}
async function dedupHits(){
  const d = await post("/api/hit/dedup", {});
  say(d); hitsSig = "";
}
async function queueSelected(){
  const ids = [...document.querySelectorAll(".pick:checked")].map(c => +c.value);
  if (!ids.length){ toast("先勾选几条结果", "warn"); return; }
  const d = await post("/api/task/queue", Object.assign({ids}, currentOpts()));
  say(d); hitsSig = ""; taskSig = "";
}

/* ---------- 任务列表 ---------- */
const ST_CLASS = {"录制中":"rec","下载中":"rec","收尾中":"rec","已完成":"ok",
                  "等待开播":"wait","排队中":"wait","已暂停":"wait",
                  "失败":"err","地址失效":"err","已停止":"wait","已取消":"","已中断":"err"};
function taskAction(t){
  if (t.status === "录制中" || t.status === "下载中") return "stop";
  if (t.status === "收尾中") return "busy";
  if (t.status === "已完成") return "done";
  if (t.status === "排队中" || t.status === "等待开播") return "queued";
  return "ended";                  // 失败/已停止/已中断/已取消 → 可继续
}
function opHTML(t){
  const k = taskAction(t), id = t.id;
  if (k === "stop")
    return '<button class="mini danger" onclick="stopTask('+id+')">■ 停止</button>';
  if (k === "busy")
    return '<span class="tag">收尾中…</span>';
  if (k === "done"){
    let h = "";
    if (t.has_mp4)
      h += '<button class="mini" onclick="playFile('+id+')" title="用系统默认播放器打开">▶ 播放</button> '+
           '<button class="mini ghost" onclick="revealFile('+id+')" title="在资源管理器里定位">📁</button> ';
    else
      h += '<span class="tag" title="记录还在，但输出文件已被移动或删除">文件已不在</span> ';
    return h + '<button class="mini ghost" onclick="delTask('+id+')" title="移除记录">✕</button>';
  }
  if (k === "queued")
    return '<button class="mini ghost" onclick="moveTask('+id+',\'up\')" title="上移">▲</button>'+
           '<button class="mini ghost" onclick="moveTask('+id+',\'down\')" title="下移">▼</button> '+
           '<button class="mini danger" onclick="stopTask('+id+')" title="取消">取消</button> '+
           '<button class="mini ghost" onclick="delTask('+id+')" title="移除">✕</button>';
  return '<button class="mini primary" onclick="retryTask('+id+')">↻ 继续</button> '+
         '<button class="mini ghost" onclick="delTask('+id+')" title="移除">✕</button>';
}
function renderTasks(tasks){
  // 结构变了才重建行（避免每 700ms 重建导致 hover/滚动位置丢失）
  const sig = tasks.map(t => t.id + "~" + t.status + "~" + t.mp4 + "~" + t.qpos).join("|");
  if (sig !== taskSig){
    taskSig = sig;
    const tb = $("tasks"); tb.innerHTML = "";
    tasks.forEach(t => {
      const tr = document.createElement("tr");
      tr.id = "t-" + t.id;
      tr.innerHTML = '<td><span data-k="q"></span></td>'+
        '<td><span class="st" data-k="st"></span></td>'+
        '<td><span data-k="name"></span><span class="note" data-k="note"></span></td>'+
        '<td class="mono" data-k="el"></td>'+
        '<td class="mono" data-k="sz"></td>'+
        '<td><span data-k="op"></span></td>';
      tb.appendChild(tr);
    });
    $("empty-tasks").style.display = tasks.length ? "none" : "";
  }
  tasks.forEach(t => {
    const tr = $("t-" + t.id); if (!tr) return;
    const q = tr.querySelector('[data-k=q]');
    q.innerHTML = t.qpos ? '<span class="qpos">' + t.qpos + "</span>" : "";
    const st = tr.querySelector('[data-k=st]');
    st.textContent = t.status;
    st.className = "st " + (ST_CLASS[t.status] || "");
    tr.querySelector('[data-k=name]').textContent =
      (t.kind === "replay" ? "回放 · " : "") + t.name;
    const nt = tr.querySelector('[data-k=note]');
    nt.textContent = t.note || t.err || "";
    nt.className = "note" + (/失败|异常|不可用/.test(t.note || t.err || "") ? " bad"
                             : /断点|还没开播|中断|未下载|停止/.test(t.note || t.err || "") ? " warn" : "");
    tr.querySelector('[data-k=el]').textContent = fmt(t.elapsed);
    const sz = tr.querySelector('[data-k=sz]');
    if (t.kind === "replay" && t.total)
      sz.innerHTML = size(t.bytes) + " / " + size(t.total) + "　" + (t.pct||0).toFixed(0) +
        '%<div class="bar"><i style="width:' + Math.min(100,t.pct||0) + '%"></i></div>';
    else
      sz.textContent = size(t.bytes);
    const op = tr.querySelector('[data-k=op]');
    const want = taskAction(t) + ":" + (t.has_mp4 ? 1 : 0);
    if (op.dataset.kind !== want){ op.dataset.kind = want; op.innerHTML = opHTML(t); }
  });
}
let TASKS = {};
function findTask(id){ return TASKS[id]; }
async function stopTask(id){ const d = await post("/api/task/stop",{id}); say(d); taskSig=""; }
async function delTask(id){
  const t = findTask(id);
  if (t && (t.status === "录制中" || t.status === "下载中") &&
      !confirm("任务还在运行，确定停止并移除？")) return;
  const d = await post("/api/task/del",{id}); say(d); taskSig="";
}
async function retryTask(id){ const d = await post("/api/task/retry",{id}); say(d); taskSig=""; }
async function pauseAll(){
  const d = await post("/api/task/pause_all",{});
  say(d); taskSig="";
}
async function resumeAll(){
  const d = await post("/api/task/resume_all",{});
  say(d); taskSig="";
}
async function moveTask(id, dir){ const d = await post("/api/task/move",{id,dir}); if(!d.ok) toast(d.msg||"","warn"); taskSig=""; }
async function clearDone(){ const d = await post("/api/task/clear",{}); say(d); taskSig=""; }
async function dedupTasks(){ const d = await post("/api/task/dedup",{}); say(d); taskSig=""; }

function playFile(id){
  const t = findTask(id);
  if (!t || !t.mp4){ toast("这条任务没有可播放的 MP4（可能还没完成）", "warn"); return; }
  apiGet("/api/open?path=" + encodeURIComponent(t.mp4)).then(d => say(d));
}
function revealFile(id){
  const t = findTask(id);
  if (!t) return;
  const p = t.mp4 || t.mp3;
  if (!p){ toast("这条任务还没有产出文件", "warn"); return; }
  apiGet("/api/reveal?path=" + encodeURIComponent(p)).then(d => say(d));
}
function openDir(){
  const d = $("dir").value.trim();
  apiGet("/api/open?path=" + encodeURIComponent(d)).then(r => say(r));
}
async function saveDir(){
  const d = $("dir").value.trim();
  if (d) await post("/api/config", {dir:d});
}
function playDone(id){ playFile(id); }

/* ---------- 体检 / 重启 ---------- */
async function showHealth(){
  const d = await apiGet("/api/health");
  if (!d || !d.ok){ toast("拿不到体检信息", "bad"); return; }
  let h = '<div class="hint" style="margin:0 0 10px">版本 v' + esc(d.version) +
          " · 编译 " + esc(d.build) + " · 端口 " + d.port + " · 已运行 " +
          Math.floor(d.uptime/60) + " 分钟</div>";
  (d.items||[]).forEach(it => {
    h += '<div class="hi"><span class="dot ' + (it.ok ? "g" : "r") + '"></span>' +
         '<div><b>' + esc(it.name) + "</b>　" + (it.ok ? "正常" : "不可用") +
         '<div class="d">' + esc(it.detail) + "</div>" +
         (it.ok ? "" : '<div class="note bad">' + esc(it.hint) + "</div>") +
         "</div></div>";
  });
  $("health-body").innerHTML = h;
  $("modal").classList.add("show");
}
async function restartSvc(){
  if (!confirm("重启服务会中断正在进行的录制/下载（断点会保留，可点「继续」）。\n确定重启？")) return;
  const d = await post("/api/app/restart", {});
  if (!d.ok){ toast(d.msg || "重启失败", "bad"); return; }
  toast("正在重启…", "ok");
  setTimeout(() => location.reload(), 2600);
}

/* ---------- 主循环 ---------- */
let failCount = 0;
async function poll(){
  try{
    const r = await fetch("/api/status?since=" + since);
    if (!r.ok) throw new Error("HTTP " + r.status);
    const d = await r.json();
    failCount = 0;
    (d.logs||[]).forEach(logline);
    if (d.next !== undefined) since = d.next;
    if (d.version && !myVer){
      myVer = d.version;
      $("ver").textContent = "v" + d.version + " · " + (d.build||"");
    }
    if (!$("dir").value && d.dir) $("dir").value = d.dir;

    const sn = d.sniff || {};
    sniffing = (sn.state === "嗅探中");
    $("b-sniff").textContent = sniffing ? "■ 停止嗅探" : "📡 开始嗅探";
    $("b-sniff").className = sniffing ? "danger" : "";
    if (sn.msg){
      $("sniff-msg").textContent = sn.msg;
      $("sniff-msg").className = "hint" + (sn.state === "失败" || sn.state === "已中断"
        ? " bad" : (sn.state === "嗅探中" ? " ok" : ""));
    }
    renderHits(sn.hits || []);

    TASKS = {};
    (d.tasks||[]).forEach(t => TASKS[t.id] = t);
    renderTasks(d.tasks || []);
    const running = (d.tasks||[]).filter(t => t.status === "录制中" || t.status === "下载中");
    $("qinfo").textContent = (running.length ? "进行中 " + running.length : "") +
      (d.queued ? (running.length ? " · " : "") + "队列 " + d.queued : "");
    $("pill").textContent = running.length ? (running[0].kind === "replay" ? "下载中" : "录制中")
                          : (d.queued ? "排队中" : "空闲");
    $("pill").className = running.length ? "rec" : (d.queued ? "on" : "");

    const warn = d.warnings || [];
    $("warn-card").style.display = warn.length ? "" : "none";
    $("warn-list").innerHTML = warn.map(w => "⚠ " + esc(w)).join("<br>");
  }catch(e){
    failCount++;
    if (failCount === 3) toast("和后台服务失去连接了，试试右上角「重启服务」", "bad");
  }
  setTimeout(poll, failCount ? 2000 : 700);
}
function logline(t){
  const d = document.createElement("div");
  d.textContent = t;
  if (t.includes("✔")) d.className = "ok";
  else if (t.includes("✘")) d.className = "bad";
  else if (t.includes("⚠") || t.includes("断开") || t.includes("重连")) d.className = "warn";
  else if (t.includes("▶") || t.includes("⬇") || t.includes("🔄")) d.className = "blue";
  const box = $("log"); box.appendChild(d); box.scrollTop = box.scrollHeight;
  while (box.children.length > 400) box.removeChild(box.firstChild);
}

async function toggleSniff(){
  if (!sniffing){
    $("b-sniff").disabled = true;
    const d = await post("/api/sniff/start", {});
    $("b-sniff").disabled = false;
    if (!d.ok) toast(d.msg || "启动失败", "bad");
    else toast("开始嗅探，去微信里打开回放页并播放", "ok");
  } else {
    const d = await post("/api/sniff/stop", {});
    say(d);
  }
}
async function startManual(){
  const url = $("url").value.trim();
  if (!url.startsWith("http")){ toast("先填地址，或者用嗅探自动获取", "warn"); return; }
  const d = await post("/api/task/add",
    Object.assign({url, name:$("cname").value.trim()}, currentOpts()));
  say(d); taskSig = "";
}

$("url").addEventListener("input", expiry);
$("cname").addEventListener("input", previewName);
$("cname").addEventListener("change", () => localStorage.setItem("wxlive_name", $("cname").value));
$("datetag").addEventListener("change", () => {
  localStorage.setItem("wxlive_datetag", $("datetag").checked ? "1" : "0");
  previewName();
});
$("dir").addEventListener("change", () => localStorage.setItem("wxlive_dir", $("dir").value));
$("url").value = localStorage.getItem("wxlive_url") || "";
$("cname").value = localStorage.getItem("wxlive_name") || "";
$("datetag").checked = localStorage.getItem("wxlive_datetag") === "1";
const dl = localStorage.getItem("wxlive_dir"); if (dl) $("dir").value = dl;
$("url").addEventListener("change", () => localStorage.setItem("wxlive_url", $("url").value));
$("only-playing").addEventListener("change", onFilterChange);
$("min-mb").addEventListener("input", onFilterChange);
/* 恢复上次的筛选条件：0:0 表示"不限 + 看全部" */
try {
  const f = (localStorage.getItem("wxlive_filter") || "").split(":");
  if (f.length === 2){
    $("only-playing").checked = (f[0] === "1");
    $("min-mb").value = Math.max(0, +f[1] || 0);
  }
} catch(e){}
expiry(); previewName(); renderSortMarks(); poll();
</script></body></html>
"""
