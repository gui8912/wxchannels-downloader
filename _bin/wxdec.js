/**
 * 微信视频号「加密 MP4」解密器（回放专用）
 *
 * 原理（来自微信官方播放器的 WASM 模块）：
 *   · 视频号只对文件前 131072 字节（128 KB）做流加密，其余部分是明文
 *   · 密钥流 = Isaac64 PRNG(decode_key)，生成后需要整体 reverse()
 *   · 解密 = 逐字节 XOR
 *   · 校验：解密后 offset 4..8 必须是 "ftyp"
 *
 * 用法：
 *   node wxdec.js --check    <文件>
 *   node wxdec.js --decrypt  <源> <目标> <decode_key> [加密长度]
 *   node wxdec.js --keystream <decode_key> <输出文件>
 *
 * 结果以一行 JSON 输出到 stdout。
 */
const fs = require('fs');
const path = require('path');

const DIR = __dirname;
const KEYSTREAM_SIZE = 131072;

/* ---------------------------------------------------------------- WASM 装载 */
function loadWasm() {
    const src = fs.readFileSync(path.join(DIR, 'wvd.js'), 'utf8');
    const fakeSelf = {
        location: { href: 'file:///' + path.join(DIR, 'wvd.js').replace(/\\/g, '/') }
    };
    let resolveReady;
    const ready = new Promise(r => { resolveReady = r; });
    const cfg = {
        wasmBinary: fs.readFileSync(path.join(DIR, 'wvd.wasm')),
        print() {}, printErr() {},
        onRuntimeInitialized: () => resolveReady(true),
        onAbort: () => resolveReady(false),
    };
    const box = { captured: null, Module: null };
    // wasm 通过这个全局函数把生成的密钥流回传（与官方 worker 一致）
    function wasm_isaac_generate(ptr, len) {
        const view = new Uint8Array(box.Module.HEAPU8.buffer, ptr, len);
        const copy = new Uint8Array(len);
        for (let i = 0; i < len; i++) copy[i] = view[len - 1 - i];   // 整体 reverse
        box.captured = copy;
    }
    box.Module = new Function(
        'Module', 'self', 'VTS_WASM_URL', 'fetch', 'wasm_isaac_generate',
        src + '\n;return Module;'
    )(cfg, fakeSelf, 'wvd.wasm', undefined, wasm_isaac_generate);
    box.ready = ready;
    return box;
}

let BOX = null;
async function getKeystream(decodeKey) {
    if (!BOX) BOX = loadWasm();
    const ok = await BOX.ready;
    if (!ok) throw new Error('WASM 模块初始化失败');
    const M = BOX.Module;
    if (typeof M.WxIsaac64 !== 'function') throw new Error('WASM 未导出 WxIsaac64');
    BOX.captured = null;
    // 构造函数要求字符串（decode_key 可能是数字或字符串）
    const inst = await new M.WxIsaac64(String(decodeKey));
    await inst.generate(KEYSTREAM_SIZE);
    try { await inst.delete(); } catch (e) { /* 老版本没有 delete */ }
    if (!BOX.captured) throw new Error('未捕获到密钥流');
    return Buffer.from(BOX.captured);
}

/* -------------------------------------------------------------------- 工具 */
function isMp4(buf) {
    return buf.length >= 8 && buf.toString('latin1', 4, 8) === 'ftyp';
}

function readHead(file, n) {
    const fd = fs.openSync(file, 'r');
    const buf = Buffer.alloc(n);
    const got = fs.readSync(fd, buf, 0, n, 0);
    fs.closeSync(fd);
    return buf.slice(0, got);
}

/* -------------------------------------------------------------------- 动作 */
async function doCheck(file) {
    const head = readHead(file, 16);
    return {
        ok: true,
        encrypted: !isMp4(head),
        size: fs.statSync(file).size,
        head: head.slice(0, 16).toString('hex'),
    };
}

async function doDecrypt(src, dst, decodeKey, encLen) {
    const size = fs.statSync(src).size;
    let n = parseInt(encLen, 10);
    if (!n || n <= 0) n = KEYSTREAM_SIZE;
    n = Math.min(n, size, KEYSTREAM_SIZE);

    const head = readHead(src, n);
    if (isMp4(head)) {
        // 已经是明文，直接复制
        fs.copyFileSync(src, dst);
        return { ok: true, decrypted: false, reason: '文件未加密，已直接复制',
                 size: fs.statSync(dst).size };
    }

    const ks = await getKeystream(decodeKey);
    const out = Buffer.alloc(n);
    for (let i = 0; i < n; i++) out[i] = head[i] ^ ks[i];

    if (!isMp4(out)) {
        return {
            ok: false,
            reason: '解密失败：解密后没有 ftyp 签名，decode_key 与文件不匹配',
            head: out.slice(0, 16).toString('hex'),
        };
    }

    // 先整体复制，再就地改写头部（大文件用系统级 copy，比流式快）
    fs.copyFileSync(src, dst);
    const fd = fs.openSync(dst, 'r+');
    fs.writeSync(fd, out, 0, n, 0);
    fs.closeSync(fd);
    return { ok: true, decrypted: true, size: fs.statSync(dst).size, encLen: n };
}

async function doKeystream(decodeKey, outFile) {
    const ks = await getKeystream(decodeKey);
    fs.writeFileSync(outFile, ks);
    return { ok: true, size: ks.length };
}

/* -------------------------------------------------------------------- 入口 */
(async () => {
    const a = process.argv.slice(2);
    const mode = a[0];
    let r;
    try {
        if (mode === '--check') {
            r = await doCheck(a[1]);
        } else if (mode === '--decrypt') {
            if (!a[1] || !a[2] || !a[3]) throw new Error('参数不足');
            r = await doDecrypt(a[1], a[2], a[3], a[4]);
        } else if (mode === '--keystream') {
            r = await doKeystream(a[1], a[2]);
        } else {
            throw new Error('未知模式：' + mode);
        }
    } catch (e) {
        r = { ok: false, reason: (e && e.message) || String(e) };
    }
    process.stdout.write(JSON.stringify(r) + '\n');
    process.exit(r.ok ? 0 : 1);
})();
