/* x_quick：一线减压三件套的前端。
   ① 现场扫码上报：部位二维码（纯 JS 生成）、待认领、A4 海报；② 弱网暂存补传：IndexedDB 队列 + 全局状态条；
   ③ 拍完即时反馈的气泡在 camera.js（photoFeedback），此处只提供样式（x_quick.css）。 */

// ============================================================================ 最小 QR 码生成器（字节模式，纠错 L/M，版本 1~14，约可容纳 360 字节）
// 无外部依赖、不访问外网；按 ISO/IEC 18004 的结构：功能图形 → 数据与 Reed-Solomon 纠错码字交织 → 蛇形填充 → 选掩码 → 格式/版本信息。
const QRGen = (function () {
  const FMT = { L: 1, M: 0 };  // 格式信息里的纠错等级位
  const EPB = { L: [-1, 7, 10, 15, 20, 26, 18, 20, 24, 30, 18, 20, 24, 26, 30], M: [-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24] };  // 每块纠错码字数
  const NBK = { L: [-1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4, 4, 4, 4, 4], M: [-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9] };  // 纠错块数
  const MAXV = 14;

  const rawModules = (v) => { let r = (16 * v + 128) * v + 64; if (v >= 2) { const n = Math.floor(v / 7) + 2; r -= (25 * n - 10) * n - 55; if (v >= 7) r -= 36; } return r; };
  const dataWords = (v, l) => Math.floor(rawModules(v) / 8) - EPB[l][v] * NBK[l][v];
  const gfMul = (x, y) => { let z = 0; for (let i = 7; i >= 0; i--) { z = (z << 1) ^ ((z >>> 7) * 0x11d); z ^= ((y >>> i) & 1) * x; } return z; };
  function rsDivisor(deg) {
    const r = new Array(deg).fill(0); r[deg - 1] = 1; let root = 1;
    for (let i = 0; i < deg; i++) { for (let j = 0; j < r.length; j++) { r[j] = gfMul(r[j], root); if (j + 1 < r.length) r[j] ^= r[j + 1]; } root = gfMul(root, 2); }
    return r;
  }
  function rsRemainder(data, div) {
    const r = div.map(() => 0);
    for (const b of data) { const f = b ^ r.shift(); r.push(0); div.forEach((c, i) => { r[i] ^= gfMul(c, f); }); }
    return r;
  }
  const bit = (x, i) => ((x >>> i) & 1) !== 0;

  function codewords(bytes, v, l) {
    const bits = [], put = (val, n) => { for (let i = n - 1; i >= 0; i--) bits.push((val >>> i) & 1); };
    put(4, 4); put(bytes.length, v < 10 ? 8 : 16); bytes.forEach((b) => put(b, 8));
    const cap = dataWords(v, l) * 8;
    put(0, Math.min(4, cap - bits.length)); put(0, (8 - (bits.length % 8)) % 8);
    for (let pad = 0xec; bits.length < cap; pad ^= 0xec ^ 0x11) put(pad, 8);
    const data = [];
    for (let i = 0; i < bits.length; i += 8) data.push(parseInt(bits.slice(i, i + 8).join(""), 2));
    // 分块算纠错码，再按列交织
    const nb = NBK[l][v], eccLen = EPB[l][v], raw = Math.floor(rawModules(v) / 8), nShort = nb - (raw % nb), shortLen = Math.floor(raw / nb);
    const div = rsDivisor(eccLen), blocks = [];
    for (let i = 0, k = 0; i < nb; i++) {
      const dat = data.slice(k, k + shortLen - eccLen + (i < nShort ? 0 : 1)); k += dat.length;
      const ecc = rsRemainder(dat, div);
      if (i < nShort) dat.push(0);
      blocks.push(dat.concat(ecc));
    }
    const out = [];
    for (let i = 0; i < blocks[0].length; i++) blocks.forEach((blk, j) => { if (i !== shortLen - eccLen || j >= nShort) out.push(blk[i]); });
    return out;
  }

  function penalty(m, n) {
    let r = 0, dark = 0;
    const lineScore = (get) => {  // 连续同色（>=5）与类定位图形
      let s = 0, run = 1, str = "";
      for (let i = 0; i < n; i++) {
        const c = get(i); str += c ? "1" : "0";
        if (i > 0 && c === get(i - 1)) { run++; if (run === 5) s += 3; else if (run > 5) s++; } else run = 1;
      }
      for (const pat of ["10111010000", "00001011101"]) for (let i = str.indexOf(pat); i >= 0; i = str.indexOf(pat, i + 1)) s += 40;
      return s;
    };
    for (let y = 0; y < n; y++) { r += lineScore((x) => m[y][x]); for (let x = 0; x < n; x++) if (m[y][x]) dark++; }
    for (let x = 0; x < n; x++) r += lineScore((y) => m[y][x]);
    for (let y = 0; y < n - 1; y++) for (let x = 0; x < n - 1; x++) { const c = m[y][x]; if (c === m[y][x + 1] && c === m[y + 1][x] && c === m[y + 1][x + 1]) r += 3; }
    const k = Math.ceil(Math.abs(dark * 20 - n * n * 10) / (n * n)) - 1;
    return r + Math.max(0, k) * 10;
  }

  function build(text, level) {
    const l = level in FMT ? level : "M";
    const bytes = Array.from(new TextEncoder().encode(text));
    let v = 1;
    for (; v <= MAXV; v++) if (4 + (v < 10 ? 8 : 16) + bytes.length * 8 <= dataWords(v, l) * 8) break;
    if (v > MAXV) throw new Error("二维码内容太长");
    const n = v * 4 + 17, m = Array.from({ length: n }, () => new Array(n).fill(false)), fn = Array.from({ length: n }, () => new Array(n).fill(false));
    const setF = (x, y, d) => { m[y][x] = d; fn[y][x] = true; };
    // 定位/定时/校正图形
    for (let i = 0; i < n; i++) { setF(6, i, i % 2 === 0); setF(i, 6, i % 2 === 0); }
    for (const [cx, cy] of [[3, 3], [n - 4, 3], [3, n - 4]]) {
      for (let dy = -4; dy <= 4; dy++) for (let dx = -4; dx <= 4; dx++) {
        const d = Math.max(Math.abs(dx), Math.abs(dy)), x = cx + dx, y = cy + dy;
        if (x >= 0 && x < n && y >= 0 && y < n) setF(x, y, d !== 2 && d !== 4);
      }
    }
    const na = v === 1 ? 0 : Math.floor(v / 7) + 2, step = v === 32 ? 26 : Math.ceil((v * 4 + 4) / (na * 2 - 2)) * 2, pos = v === 1 ? [] : [6];
    for (let p = n - 7; pos.length < na; p -= step) pos.splice(1, 0, p);
    pos.forEach((px, i) => pos.forEach((py, j) => {
      if ((i === 0 && j === 0) || (i === 0 && j === na - 1) || (i === na - 1 && j === 0)) return;
      for (let dy = -2; dy <= 2; dy++) for (let dx = -2; dx <= 2; dx++) setF(px + dx, py + dy, Math.max(Math.abs(dx), Math.abs(dy)) !== 1);
    }));
    const drawFormat = (mask) => {
      const d = (FMT[l] << 3) | mask; let rem = d;
      for (let i = 0; i < 10; i++) rem = (rem << 1) ^ ((rem >>> 9) * 0x537);
      const bits = ((d << 10) | rem) ^ 0x5412;
      for (let i = 0; i <= 5; i++) setF(8, i, bit(bits, i));
      setF(8, 7, bit(bits, 6)); setF(8, 8, bit(bits, 7)); setF(7, 8, bit(bits, 8));
      for (let i = 9; i < 15; i++) setF(14 - i, 8, bit(bits, i));
      for (let i = 0; i < 8; i++) setF(n - 1 - i, 8, bit(bits, i));
      for (let i = 8; i < 15; i++) setF(8, n - 15 + i, bit(bits, i));
      setF(8, n - 8, true);
    };
    drawFormat(0);
    if (v >= 7) {
      let rem = v;
      for (let i = 0; i < 12; i++) rem = (rem << 1) ^ ((rem >>> 11) * 0x1f25);
      const bits = (v << 12) | rem;
      for (let i = 0; i < 18; i++) { const b = bit(bits, i), a = n - 11 + (i % 3), c = Math.floor(i / 3); setF(a, c, b); setF(c, a, b); }
    }
    // 蛇形填充数据位
    const cw = codewords(bytes, v, l);
    for (let i = 0, right = n - 1; right >= 1; right -= 2) {
      if (right === 6) right = 5;
      for (let vert = 0; vert < n; vert++) for (let j = 0; j < 2; j++) {
        const x = right - j, up = ((right + 1) & 2) === 0, y = up ? n - 1 - vert : vert;
        if (!fn[y][x] && i < cw.length * 8) { m[y][x] = bit(cw[i >>> 3], 7 - (i & 7)); i++; }
      }
    }
    const MASKS = [(x, y) => (x + y) % 2 === 0, (x, y) => y % 2 === 0, (x, y) => x % 3 === 0, (x, y) => (x + y) % 3 === 0,
      (x, y) => (Math.floor(x / 3) + Math.floor(y / 2)) % 2 === 0, (x, y) => ((x * y) % 2) + ((x * y) % 3) === 0,
      (x, y) => ((((x * y) % 2) + ((x * y) % 3)) % 2) === 0, (x, y) => ((((x + y) % 2) + ((x * y) % 3)) % 2) === 0];
    const apply = (mk) => { for (let y = 0; y < n; y++) for (let x = 0; x < n; x++) if (!fn[y][x] && MASKS[mk](x, y)) m[y][x] = !m[y][x]; };
    let best = 0, bestScore = Infinity;
    for (let mk = 0; mk < 8; mk++) { apply(mk); drawFormat(mk); const sc = penalty(m, n); if (sc < bestScore) { best = mk; bestScore = sc; } apply(mk); }
    apply(best); drawFormat(best);
    return { size: n, version: v, m };
  }

  // 输出 SVG 字符串（含 4 模块静区，黑模块合并成横条以减小体积）
  function svg(text, o = {}) {
    const q = build(text, o.level || "M"), pad = 4, w = q.size + pad * 2;
    let d = "";
    for (let y = 0; y < q.size; y++) for (let x = 0; x < q.size;) {
      if (!q.m[y][x]) { x++; continue; }
      let e = x; while (e < q.size && q.m[y][e]) e++;
      d += `M${x + pad},${y + pad}h${e - x}v1h-${e - x}z`; x = e;
    }
    return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${w} ${w}" shape-rendering="crispEdges" role="img" aria-label="二维码"><rect width="${w}" height="${w}" fill="#fff"/><path d="${d}" fill="#000"/></svg>`;
  }
  return { build, svg, dataWords };
})();
window.QRGen = QRGen;

// ============================================================================ 弱网暂存补传
// 一线的随手记、录音、照片与整改提交：网络层失败或超时（core.js api() 的超时）时存本机（IndexedDB，照片、录音存 Blob），有网自动补传。
// 随手记“说一句”的录音一律先存本机再传：说完就是“已记下，待上传”，不等转写；服务器收到后在后台转写（/api/quick/voice-pending 看进展）。
// 补传走带 client_id 的幂等接口（/api/quick/sync/*）：重复发送不产生重复记录；先传照片，再传引用照片的随手记和整改提交；
// 失败指数退避重试；补传请求自身也有超时，挂住的请求不卡住整个队列；登录过期时保留队列并提示重新登录。
// 暂存记录 kind：note 随手记 | media 照片 | voice 录音 | action 整改提交（带一份照片留底：拍摄时间早于派单被拒时可“改作随手拍”，照片不丢）。
// core.js 的 api() 只在这里注册的钩子 window.__zbOffline 上留了入口，在线行为不变。
const QQ = Vue.reactive({ items: [], n: 0, bad: 0, sync: "idle", online: navigator.onLine !== false, open: false, showDone: false, db: true });
const QV = Vue.reactive({ items: [] });  // 服务器上本人还没转写完的录音
const isLocalRef = (x) => typeof x === "string" && x.startsWith("local:");
const newCid = (p) => { const a = new Uint8Array(10); (window.crypto || window.msCrypto).getRandomValues(a); return p + Array.from(a, (x) => x.toString(16).padStart(2, "0")).join(""); };
const qMyUid = () => (store.me ? store.me.user.id : 0);
const qThumbs = new Map();  // 暂存照片的本地预览地址
const qRectFiles = new Map();  // 刚上传成功的整改照片：服务器编号 → 照片留底（只在内存，30 分钟）；提交整改超时转暂存时随记录带上
let qSeq = 0;
const qNextSeq = () => Date.now() * 1000 + (qSeq++ % 1000);
const Q_KIND = { media: "照片", note: "随手记", voice: "语音", action: "整改提交" };
const qHHMM = (iso) => { const d = new Date(iso || ""); return isNaN(d.getTime()) ? "" : d.toTimeString().slice(0, 5); };

const qReady = new Promise((res) => {
  if (!window.indexedDB) return res(null);
  let rq; try { rq = indexedDB.open("zb_quick", 1); } catch (_) { return res(null); }
  rq.onupgradeneeded = () => { const d = rq.result; d.createObjectStore("q", { keyPath: "cid" }); d.createObjectStore("done", { keyPath: "cid" }); };
  rq.onsuccess = () => res(rq.result); rq.onerror = () => res(null); rq.onblocked = () => res(null);
}).then((db) => { QQ.db = !!db; return db; });

function qrun(store, mode, fn) {
  return qReady.then((db) => new Promise((res, rej) => {
    if (!db) return rej(new Error("本机存储不可用"));
    let out; const tx = db.transaction(store, mode), r = fn(tx.objectStore(store));
    if (r) r.onsuccess = () => { out = r.result; };
    tx.oncomplete = () => res(out); tx.onerror = tx.onabort = () => rej(tx.error || new Error("本机存储失败"));
  }));
}
const qPut = (rec) => qrun("q", "readwrite", (s) => s.put(rec));
const qDel = (cid) => qrun("q", "readwrite", (s) => s.delete(cid));

// 照片表单里第 i 张照片的留底（文件与水印信息）：暂存、整改留底、改作随手拍都用它
function qPhotoOf(fd, i) {
  const col = (k) => fd.getAll(k)[i], num = (k) => (col(k) === "" || col(k) === undefined ? null : +col(k)), f = fd.getAll("files")[i];
  return { blob: f, name: (f && f.name) || "photo.jpg", taken_at: col("taken_at") || "", lat: num("geo_lat"), lon: num("geo_lon"), acc: num("geo_acc"), place: col("place") || "" };
}

function qView(r, doneMap) {
  const thumbOf = (id) => (isLocalRef(id) ? (qThumbs.get(id.slice(6)) || (doneMap.has(id.slice(6)) ? `/api/media/${doneMap.get(id.slice(6))}/thumb` : "")) : `/api/media/${id}/thumb`);
  const v = { cid: r.cid, kind: r.kind, status: r.status, tries: r.tries || 0, err: r.err || "", code: r.code || "", next: r.next || 0, created: r.created };
  if (r.kind === "media") { v.label = r.purpose === "rectify" ? "整改照片" : r.name || "照片"; v.thumb = qThumbs.get(r.cid) || ""; }
  else if (r.kind === "voice") { v.label = r.seconds ? `录音 ${Math.round(r.seconds)} 秒` : "录音"; v.seconds = r.seconds || null; }
  else if (r.kind === "action") { v.fid = r.body.finding_id; v.label = `S${v.fid} 的整改照片`; v.thumb = qThumbs.get(r.cid) || thumbOf(String(r.body.media_id)); v.canSafety = !!(r.photo && r.photo.blob); }
  else { v.label = (r.body.text || "").slice(0, 30) || "（照片）"; v.thumbs = (r.body.media_ids || []).map(thumbOf).filter(Boolean); }
  return v;
}

async function qRefresh() {
  if (!QQ.db) return;
  const uid = qMyUid();
  const [recs, dones] = await Promise.all([qrun("q", "readonly", (s) => s.getAll()), qrun("done", "readonly", (s) => s.getAll())]);
  const doneMap = new Map(dones.map((d) => [d.cid, d.sid]));
  for (const r of recs) {
    const b = r.kind === "media" ? r.blob : r.kind === "action" && r.photo ? r.photo.blob : null;
    if (b && !qThumbs.has(r.cid)) qThumbs.set(r.cid, URL.createObjectURL(b));
  }
  const live = new Set(recs.map((r) => r.cid));
  for (const [cid, u] of qThumbs) if (!live.has(cid) && !recs.some((r) => r.kind === "note" && (r.body.media_ids || []).includes("local:" + cid))) { URL.revokeObjectURL(u); qThumbs.delete(cid); }
  const mine = recs.filter((r) => r.uid === uid).sort((a, b) => a.seq - b.seq);
  QQ.items = mine.map((r) => qView(r, doneMap));
  QQ.n = mine.filter((r) => r.status !== "failed").length;
  QQ.bad = mine.filter((r) => r.status === "failed").length;
  if (!mine.length) QQ.open = false;
  const old = Date.now() - 7 * 86400000;  // 已回写的"本地编号→服务器编号"对照只留一周
  dones.filter((d) => d.ts < old).forEach((d) => qrun("done", "readwrite", (s) => s.delete(d.cid)).catch(() => {}));
}

async function qEnqueueMedia(fd) {
  const files = fd.getAll("files"), purpose = fd.get("purpose") || "log", scan = fd.get("scan") === "0" ? 0 : 1;
  if (!files.length) return null;
  const out = [];
  try {
    for (let i = 0; i < files.length; i++) {
      const p = qPhotoOf(fd, i), cid = newCid("m");
      const rec = { cid, kind: "media", uid: qMyUid(), seq: qNextSeq(), created: serverNow().toISOString(), status: "pending", tries: 0, next: 0, err: "",
        blob: p.blob, name: p.name, purpose, scan, log_id: fd.get("log_id") || null, taken_at: p.taken_at, lat: p.lat, lon: p.lon, acc: p.acc, place: p.place };
      await qPut(rec);
      qThumbs.set(cid, URL.createObjectURL(p.blob));
      out.push({ id: "local:" + cid, thumb: qThumbs.get(cid), url: qThumbs.get(cid), taken_at: rec.taken_at, name: rec.name, purpose, log_id: rec.log_id, safety_status: "queued", queued: true,
        geo: rec.lat != null ? { lat: rec.lat, lon: rec.lon, acc: rec.acc, place: rec.place } : null });
    }
  } catch (e) { toast("本机存储不足，照片未能暂存：" + e.message, true); return null; }
  await qRefresh(); qKick();
  return { data: out };
}

async function qEnqueueNote(body) {
  const cid = body.client_id || newCid("n");
  const rec = { cid, kind: "note", uid: qMyUid(), seq: qNextSeq(), created: body.client_time || serverNow().toISOString(), status: "pending", tries: 0, next: 0, err: "",
    body: { text: body.text || "", media_ids: body.media_ids || [], seconds: body.seconds, source: body.source || "text", lat: body.lat, lon: body.lon, acc: body.acc, place: body.place || "" } };
  try { await qPut(rec); } catch (e) { toast("本机存储不足，记录未能暂存：" + e.message, true); return null; }
  await qRefresh(); qKick();
  const v = QQ.items.find((x) => x.cid === cid);
  return { data: { id: "local:" + cid, time: qHHMM(rec.created), text: rec.body.text, media: [], thumbs: v ? v.thumbs : [], source: rec.body.source, geo: null, queued: true, client_id: cid } };
}

// 录音：录音时间（服务器校正时钟）、时长、定位随录音一起存；补传表单见 qVoiceForm。
// note_cid：拍照时同时录的语音，指向那条照片随手记的 client_id，服务器转写后把文字补进那条记录（那条不在了就单独成一条）
function qVoiceRec(v) {
  const g = v.geo || null;
  return { cid: newCid("v"), kind: "voice", uid: qMyUid(), seq: qNextSeq(), created: (v.recordedAt ? new Date(v.recordedAt) : serverNow()).toISOString(), status: "pending", tries: 0, next: 0, err: "",
    blob: v.blob, name: v.name || "voice.webm", seconds: v.seconds || null, lat: g ? g.lat : null, lon: g ? g.lon : null, acc: g && g.acc != null ? g.acc : null, place: (g && g.place) || "",
    note_cid: v.noteCid || "" };
}
function qVoiceForm(r) {
  const fd = new FormData();
  fd.append("client_id", r.cid); fd.append("recorded_at", r.created); fd.append("seconds", r.seconds ?? "");
  fd.append("geo_lat", r.lat ?? ""); fd.append("geo_lon", r.lon ?? ""); fd.append("geo_acc", r.acc ?? ""); fd.append("place", r.place || "");
  if (r.note_cid) fd.append("note_cid", r.note_cid);
  fd.append("file", r.blob, r.name || "voice.webm");
  return fd;
}
// 录音先在本机看一眼：空的、超过 25MB、AMR/3GP（部分手机录音机，服务器转写不了）都不收，当场说清楚，不让人以为记下了
const Q_VOICE_MAX = 25 * 1024 * 1024;
async function qVoiceCheck(blob, name) {
  if (!blob || !blob.size) return "录音是空的，请重录";
  if (blob.size > Q_VOICE_MAX) return "录音超过 25MB，请分几段说";
  let h = "";
  try { h = String.fromCharCode(...new Uint8Array(await blob.slice(0, 12).arrayBuffer())); } catch (_) { /* 读不了就只看类型和文件名 */ }
  if (/amr|3gpp/i.test(blob.type || "") || /\.(amr|3gp|3ga|3g2)$/i.test(name || "") || h.startsWith("#!AMR") || (h.slice(4, 8) === "ftyp" && h.slice(8, 11) === "3gp"))
    return "这段录音是 AMR/3GP 格式，系统转写不了。请把手机录音机的格式改成 m4a 或 mp3 再录，或者改打字";
  return "";
}
// 说一句：先存本机（不论有没有网），马上算“已记下”；本机存储不可用时才直接上传（同样有超时），失败如实提示
async function qAddVoice(v) {
  const bad = await qVoiceCheck(v.blob, v.name);
  if (bad) throw new Error(bad);
  const rec = qVoiceRec(v);
  try { await qPut(rec); await qRefresh(); qKick(300); return { queued: true, cid: rec.cid }; }
  catch (_) { /* 本机存储不可用 */ }
  const fd = qVoiceForm(rec);
  const data = await api("POST", "/api/quick/sync/voice", fd, { timeout: apiTimeout(fd), noQueue: true });
  qVoiceLoad(true);
  return { queued: false, data };
}

// 整改提交入队：照片还在本机的，等照片传完再提交；同时留一份照片，被拒收时可改作随手拍
async function qEnqueueAction(b) {
  const cid = b.client_id || newCid("a"), mid = b.media_id, fid = +b.finding_id;
  try {
    let photo = null;
    if (isLocalRef(mid)) { const m = await qrun("q", "readonly", (s) => s.get(mid.slice(6))); if (m && m.blob) photo = { blob: m.blob, name: m.name, taken_at: m.taken_at, lat: m.lat, lon: m.lon, acc: m.acc, place: m.place }; }
    else if (qRectFiles.has(mid)) { const { at, ...p } = qRectFiles.get(mid); photo = p; }
    await qPut({ cid, kind: "action", act: "rectify", uid: qMyUid(), seq: qNextSeq(), created: serverNow().toISOString(), status: "pending", tries: 0, next: 0, err: "", code: "",
      body: { finding_id: fid, media_id: mid }, photo });
  } catch (e) { toast("本机存储不足，整改提交未能暂存：" + e.message, true); return null; }
  await qRefresh(); qKick();
  return { data: { queued: true, client_id: cid, finding_id: fid } };
}
function qKeepRect(fd, list) {
  const now = Date.now();
  list.forEach((m, i) => { if (m && Number.isInteger(m.id) && fd.getAll("files")[i]) qRectFiles.set(m.id, { ...qPhotoOf(fd, i), at: now }); });
  for (const [k, v] of qRectFiles) if (now - v.at > 1800000 || qRectFiles.size > 6) qRectFiles.delete(k);
}

// 整改照片被拒（拍摄时间早于派单超过 24 小时）：同一张照片改作随手拍隐患重新入队（purpose=safety，进安全识别），照片不丢。
// x 可以是暂存里那条整改提交的编号，也可以是手里的照片文件（在线提交当场被拒时）
async function qAsSafety(x) {
  let photo = null, drop = "";
  if (typeof x === "string") {
    const r = await qrun("q", "readonly", (s) => s.get(x.replace(/^local:/, ""))).catch(() => null);
    if (r && r.kind === "action" && r.photo && r.photo.blob) { photo = r.photo; drop = r.cid; }
  } else if (x && x.size !== undefined) {
    const g = x.geo || null;
    photo = { blob: x, name: x.name || "photo.jpg", taken_at: x.takenAt || "", lat: g ? g.lat : null, lon: g ? g.lon : null, acc: g ? g.acc : null, place: x.place || "" };
  }
  if (!photo) { toast("找不到这张照片，无法改作随手拍", true); return false; }
  try {
    await qPut({ cid: newCid("m"), kind: "media", uid: qMyUid(), seq: qNextSeq(), created: serverNow().toISOString(), status: "pending", tries: 0, next: 0, err: "",
      blob: photo.blob, name: photo.name || "photo.jpg", purpose: "safety", scan: 1, log_id: null, taken_at: photo.taken_at || "",
      lat: photo.lat ?? null, lon: photo.lon ?? null, acc: photo.acc ?? null, place: photo.place || "" });
    if (drop) await qDel(drop);
  } catch (e) { toast("本机存储不足，未能改作随手拍：" + e.message, true); return false; }
  await qRefresh(); qKick(300);
  return true;
}

// 哪些写请求可暂存（纯函数）：null 不介入；{url, body, queue} 改写为幂等接口并加超时；{queue} 原样发送并加超时；{enqueue} 直接入队
function qPlan(method, url, body) {
  if (String(method).toUpperCase() !== "POST") return null;
  if (url === "/api/notes" && body && !(body instanceof FormData)) {
    const b = { ...body, client_id: body.client_id || newCid("n"), client_time: body.client_time || serverNow().toISOString() };
    return (b.media_ids || []).some(isLocalRef) ? { enqueue: "note", body: b } : { url: "/api/quick/sync/note", body: b, queue: true };  // 引用了还没上传的照片：排在照片之后
  }
  const m = /^\/api\/findings\/(\d+)\/rectify$/.exec(url);
  if (m && body && !(body instanceof FormData)) {
    const b = { client_id: body.client_id || newCid("a"), finding_id: +m[1], media_id: body.media_id };
    return isLocalRef(b.media_id) ? { enqueue: "action", body: b } : { url: "/api/quick/sync/rectify", body: b, queue: true };
  }
  if (url === "/api/media" && body instanceof FormData) return { queue: true };
  return null;
}

// core.js api() 的钩子：phase "pre"（发请求前）/ "err"（网络层失败或超时后）/ "ok"（成功后）。返回 null 表示不介入。
window.__zbOffline = function (phase, method, url, body, info) {
  if (!QQ.db || !store.me || String(method).toUpperCase() !== "POST") return null;
  if (phase === "pre") {
    const p = qPlan(method, url, body);
    if (!p) return null;
    if (p.enqueue === "note") return qEnqueueNote(p.body);
    if (p.enqueue === "action") return qEnqueueAction(p.body);
    return p;
  }
  if (phase === "err") {
    if (url === "/api/quick/sync/note") return qEnqueueNote(body);
    if (url === "/api/quick/sync/rectify") return qEnqueueAction(body);
    if (url === "/api/media" && body instanceof FormData) return qEnqueueMedia(body);
  }
  if (phase === "ok" && url === "/api/media" && body instanceof FormData && body.get("purpose") === "rectify" && Array.isArray(info)) qKeepRect(body, info);
  return null;
};

// ---- 补传
const qBackoff = (tries) => Math.min(300000, 4000 * 2 ** Math.min(tries, 7)) * (0.8 + Math.random() * 0.4);
async function qFetch(url, body) {
  const init = { method: "POST", headers: { "x-zb": "1" }, credentials: "same-origin" };
  if (body instanceof FormData) init.body = body; else { init.body = JSON.stringify(body); init.headers["Content-Type"] = "application/json"; }
  const ctl = typeof AbortController === "function" ? new AbortController() : null;
  if (ctl) init.signal = ctl.signal;
  const timer = ctl ? setTimeout(() => ctl.abort(), apiTimeout(body)) : 0;  // 补传也有超时：挂住的请求不卡住整个队列，下一轮按原 client_id 重发
  try {
    let res; try { res = await fetch(url, init); } catch (_) { return { net: true }; }
    let data = null; try { data = await res.json(); } catch (_) { if (ctl && ctl.signal.aborted) return { net: true }; }
    const i = errInfo(data, res.status);
    return { status: res.status, ok: res.ok, data, msg: i.msg, code: i.code };
  } finally { clearTimeout(timer); }
}
// 一次补传的结局：net 网络不通（停下等网）| retry 稍后再试 | auth 登录过期 | ok | fix 整改照片早于派单被拒 | fail 服务器明确拒绝
function qOutcome(out) {
  if (out.net) return "net";
  if (out.status === 401) return "auth";
  if (out.ok) return "ok";
  if (out.status === 422 && out.code === "photo_before_dispatch") return "fix";
  if ([408, 409, 425, 429].includes(out.status) || out.status >= 500) return "retry";
  return "fail";
}

async function qStep(r) {
  let out;
  if (r.kind === "media") {
    const fd = new FormData();
    fd.append("client_id", r.cid); fd.append("purpose", r.purpose || "log"); fd.append("scan", r.scan === 0 ? 0 : 1); if (r.log_id) fd.append("log_id", r.log_id);
    fd.append("taken_at", r.taken_at || ""); fd.append("geo_lat", r.lat ?? ""); fd.append("geo_lon", r.lon ?? ""); fd.append("geo_acc", r.acc ?? ""); fd.append("place", r.place || "");
    fd.append("file", r.blob, r.name || "photo.jpg");
    out = await qFetch("/api/quick/sync/media", fd);
  } else if (r.kind === "voice") {
    // 拍照时录的语音：等那条照片随手记先传上去（还在本机排队就等一等），这样转写的文字能补进那条记录
    if (r.note_cid) { const n = await qrun("q", "readonly", (s) => s.get(r.note_cid)).catch(() => null); if (n && n.status !== "failed") return "wait"; }
    out = await qFetch("/api/quick/sync/voice", qVoiceForm(r));
  } else {
    // 把"本地照片编号"换成服务器编号：照片还没传完则等一等（undefined），照片已被放弃则不再引用（null）
    const [dones, recs] = await Promise.all([qrun("done", "readonly", (s) => s.getAll()), qrun("q", "readonly", (s) => s.getAll())]);
    const dm = new Map(dones.map((d) => [d.cid, d.sid])), pend = new Set(recs.map((x) => x.cid));
    const real = (id) => (!isLocalRef(id) ? id : dm.has(id.slice(6)) ? dm.get(id.slice(6)) : pend.has(id.slice(6)) ? undefined : null);
    if (r.kind === "action") {
      const mid = real(r.body.media_id);
      if (mid === undefined) return "wait";
      if (mid === null) { r.status = "failed"; r.err = "整改照片已放弃，无法提交"; await qPut(r); return "fail"; }
      out = await qFetch("/api/quick/sync/rectify", { client_id: r.cid, finding_id: r.body.finding_id, media_id: mid });
    } else {
      const ids = [];
      for (const id of r.body.media_ids || []) { const x = real(id); if (x === undefined) return "wait"; if (x !== null) ids.push(x); }
      if (!ids.length && !(r.body.text || "").trim()) { r.status = "failed"; r.err = "照片已放弃且无文字，无可上传内容"; await qPut(r); return "fail"; }
      out = await qFetch("/api/quick/sync/note", { ...r.body, media_ids: ids, client_id: r.cid, client_time: r.created });
    }
  }
  const res = qOutcome(out);
  if (res === "net" || res === "retry") { r.tries = (r.tries || 0) + 1; r.err = res === "net" ? "网络不通" : out.msg; r.next = Date.now() + qBackoff(r.tries); await qPut(r); return res; }
  if (res === "auth") return "auth";
  if (res === "ok") {
    if (r.kind === "media") await qrun("done", "readwrite", (s) => s.put({ cid: r.cid, sid: out.data.id, ts: Date.now() }));
    await qDel(r.cid);
    r.okData = out.data || {};
    return "ok";
  }
  // 服务器明确拒绝（其余 4xx），重试无用，留给人处理：重试 / 放弃；整改照片早于派单的可“改作随手拍”
  r.status = "failed"; r.err = out.msg; r.code = res === "fix" ? "photo_before_dispatch" : ""; await qPut(r);
  return "fail";
}

let qBusy = false, qTimer = 0;
async function qSyncCore(force) {
  const uid = qMyUid();
  const recs = (await qrun("q", "readonly", (s) => s.getAll())).filter((r) => r.uid === uid && r.status !== "failed").sort((a, b) => a.seq - b.seq);
  let auth = false, net = false;
  const done = [];
  for (const r of recs) {
    if (!force && r.next > Date.now()) continue;
    const res = await qStep(r);
    if (res === "ok") done.push({ kind: r.kind, fid: r.kind === "action" ? r.body.finding_id : null, late: +(r.okData.late_days || 0) });
    else if (res === "auth") { auth = true; break; }
    else if (res === "net") { net = true; break; }
    await qRefresh();
  }
  await qRefresh();
  QQ.sync = auth ? "auth" : net ? "net" : "idle";
  if (done.length) {
    window.dispatchEvent(new CustomEvent("zb-quick-synced", { detail: { uploaded: done.length, items: done } }));
    const late = done.filter((d) => d.late >= 1);  // 迟到补传：按记录时间归入当天，告诉本人一声
    if (late.length) toast(late.length === 1 ? `已补传，按记录时间归入当天（迟到补传 ${late[0].late} 天）` : `已补传 ${done.length} 条，其中 ${late.length} 条按记录时间归入当天（迟到补传）`);
    if (done.some((d) => d.kind === "voice")) qVoiceLoad(true);
    if (!QQ.n && !QQ.bad) { QQ.showDone = true; setTimeout(() => (QQ.showDone = false), 3500); }
  }
}
async function qSync(force) {
  if (qBusy || !store.me || !QQ.db) return;
  qBusy = true; QQ.sync = "syncing";
  try {
    if (navigator.locks) await navigator.locks.request("zb_quick_sync", { ifAvailable: true }, async (lk) => { if (lk) await qSyncCore(force); });  // 多个标签页只让一个去传
    else await qSyncCore(force);
  } catch (_) { QQ.sync = "idle"; } finally { qBusy = false; if (QQ.sync === "syncing") QQ.sync = "idle"; }
}
function qKick(ms = 1200) { clearTimeout(qTimer); qTimer = setTimeout(() => qSync(false), ms); }

async function qRetry(cid) {
  const recs = await qrun("q", "readonly", (s) => s.getAll());
  for (const r of recs.filter((x) => x.uid === qMyUid() && (!cid || x.cid === cid))) { r.status = "pending"; r.tries = 0; r.next = 0; r.err = ""; r.code = ""; await qPut(r); }
  await qRefresh(); return qSync(true);
}
async function qDiscard(id) {
  const cid = String(id).replace(/^local:/, "");
  await qDel(cid);
  if (qThumbs.has(cid)) { URL.revokeObjectURL(qThumbs.get(cid)); qThumbs.delete(cid); }
  await qRefresh();
}

// ---- 服务器上还没转写完的录音：与本机待上传的一起并入随手记列表；转完（或失败）通知页面重新拉取。
// 这个接口会顺带清理过期录音、把搁置的转写重新排队，所以看得克制：两次至少隔 4 秒，有在转写的才接着看，间隔逐步拉长到 30 秒，最多看约 15 分钟
const qvBusy = (v) => v.status === "pending" || v.status === "running";
let qvTimer = 0, qvRounds = 0, qvLast = 0;
async function qVoiceLoad(force) {
  clearTimeout(qvTimer);
  if (!store.me || store.me.user.role !== "tech") return;
  if (!force && Date.now() - qvLast < 4000) { qvTimer = setTimeout(() => qVoiceLoad(true), 4000); return; }
  qvLast = Date.now();
  let list;
  try { list = await GET("/api/quick/voice-pending", { quiet: true }); } catch (_) { return; }  // 离线或服务器还没有这个接口：不显示
  const was = QV.items.filter(qvBusy).map((v) => v.pending_id);
  QV.items = Array.isArray(list) ? list : [];
  const busy = QV.items.filter(qvBusy);
  if (was.some((id) => !busy.some((v) => v.pending_id === id))) window.dispatchEvent(new CustomEvent("zb-voice-done"));
  if (busy.length && qvRounds < 40) { qvRounds++; qvTimer = setTimeout(() => qVoiceLoad(true), Math.min(30000, 5000 + qvRounds * 2500)); } else qvRounds = 0;
}
async function qVoiceAct(id, act) { await POST(`/api/quick/voice/${id}/${act}`); qvRounds = 0; await qVoiceLoad(true); }

window.QUICK = {
  retry: qRetry, discard: qDiscard, addVoice: qAddVoice, asSafety: qAsSafety, newCid,
  loadVoice: (force) => qVoiceLoad(force), voiceRetry: (id) => qVoiceAct(id, "retry"), voiceDiscard: (id) => qVoiceAct(id, "discard"),
  thumb: (id) => qThumbs.get(String(id).replace(/^local:/, "")) || "",
  // 随手记页面把待上传的记录与转写中的录音排在列表前面
  pendingNotes: () => [
    ...QQ.items.filter((x) => x.kind === "note" || x.kind === "voice").map((x) => ({ id: "local:" + x.cid, time: qHHMM(x.created), text: x.kind === "voice" || x.label === "（照片）" ? "" : x.label,
      media: [], thumbs: x.thumbs || [], geo: null, queued: true, status: x.status, voice: x.kind === "voice", seconds: x.seconds || null, err: x.err })),
    ...QV.items.map((v) => ({ id: "vp:" + v.pending_id, pid: v.pending_id, time: qHHMM(v.recorded_at), text: "", media: [], thumbs: [], geo: null, voice: true,
      seconds: v.seconds || null, asr: v.status, err: v.last_error || "" })),
  ],
  // 整改单页：这条隐患还在本机排队（或被拒）的整改提交
  pendingActions: (fid) => QQ.items.filter((x) => x.kind === "action" && x.fid === +fid),
};

window.addEventListener("online", () => { QQ.online = true; qRefresh().then(() => qSync(true)); qVoiceLoad(); });
window.addEventListener("offline", () => { QQ.online = false; });
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") qRefresh().then(() => qKick(500)); });
setInterval(() => { if (store.me && QQ.db && (QQ.n || QQ.bad)) qSync(false); }, 12000);
watch(() => store.me && store.me.user.id, (id) => { if (id) { qRefresh().then(() => qKick(800)).catch(() => {}); qVoiceLoad(); } });  // 登录/切换账号后，只处理本人的暂存

const QuickBar = {
  template: `<div class="qbar-wrap" v-if="show">
    <div :class="['qbar', cls]" @click="QQ.open = !QQ.open"><span class="qbar-dot"></span><span>{{ text }}</span>
      <a v-if="QQ.sync === 'auth'" @click.stop="relogin">重新登录</a>
      <a v-else-if="QQ.n || QQ.bad" @click.stop="retry">立即重试</a></div>
    <div class="qpanel" v-if="QQ.open && QQ.items.length">
      <div class="qrow" v-for="it in QQ.items" :key="it.cid">
        <img v-if="it.thumb" :src="it.thumb" alt=""><span class="qi" v-else><ic :n="it.kind === 'voice' ? 'mic' : it.kind === 'note' ? 'edit' : 'camera'"></ic></span>
        <div class="grow"><div class="small">{{ Q_KIND[it.kind] || '记录' }}：{{ it.label }}</div>
          <div class="tiny" :class="it.status === 'failed' ? 'bad' : 'muted'">{{ state(it) }}</div></div>
        <button class="btn sm" v-if="it.code === 'photo_before_dispatch' && it.canSafety" @click="asSafety(it)">改作随手拍</button>
        <button class="btn sm" v-else-if="it.status === 'failed' || it.tries" @click="one(it)">重试</button>
        <button class="btn ghost sm" @click="drop(it)">放弃</button></div></div></div>`,
  data: () => ({ QQ, Q_KIND }),
  computed: {
    show() { return !!store.me && store.route.name !== "login" && (QQ.n + QQ.bad > 0 || QQ.showDone); },
    cls() { return QQ.n + QQ.bad === 0 ? "ok" : QQ.sync === "auth" || QQ.bad ? "warn" : QQ.sync === "syncing" ? "busy" : ""; },
    text() {
      const n = QQ.n + QQ.bad;
      if (!n) return "已全部同步";
      if (QQ.sync === "auth") return `登录已过期，${n} 条待上传`;
      if (QQ.sync === "syncing") return `上传中 ${n} 条`;
      const tail = QQ.bad ? `，${QQ.bad} 条失败` : "";
      return (!QQ.online ? "无网络，" : "") + `待上传 ${n} 条` + tail;
    },
  },
  methods: {
    state(it) {
      if (it.status === "failed") return (it.code === "photo_before_dispatch" ? "未提交：" : "失败：") + it.err;
      return it.tries ? `重试中（第 ${it.tries} 次）${it.err ? "：" + it.err : ""}` : QQ.online ? "等待上传" : "等待网络";
    },
    retry() { qRetry(); },
    one(it) { qRetry(it.cid); },
    async asSafety(it) { if (await qAsSafety(it.cid)) toast("已改作随手拍，上传后 AI 识别隐患"); },
    drop(it) { if (confirm("放弃该暂存内容？此操作不可恢复。")) qDiscard(it.cid); },
    async relogin() { try { await POST("/api/logout"); } catch (_) { /* 已过期 */ } store.me = null; go("login"); },
  },
};
EXT.slot("global", QuickBar);

// ============================================================================ 现场扫码上报
const QR_ROLES = ["sub_safety", "safety_sup", "manager"];
// 二维码网址：系统管理员设置的部署地址优先；未设置且本页是 localhost 打开时，用后端给的本机局域网地址（手机连同一网络即可扫开）
const LOCAL_RE = /^https?:\/\/(localhost|127\.|\[::1\])/i;
const LAN_RE = /^https?:\/\/(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)/;
const SCAN_TEXT = { pending: "智能体识别中", hazard: "已识别出隐患", clear: "未发现明显隐患", failed: "识别暂不可用", skipped: "未做识别" };

const QuickQr = {
  components: { CandidateBox },
  template: `<div class="rs-qr"><page-head page="qrcodes"><template #meta>{{ isSubOrg() ? '本单位' : '全项目' }} · 扫码人只能提交、不能查看项目数据；提交后进入待认领，由安全员处理</template></page-head>
    <div class="rs-qr-grid">
    <section class="zb-card rs-qr-subs"><div class="zb-ch"><ic n="camera"></ic><h2>扫码上报</h2><span :class="['zb-n', {'rs-n-warn': pending}]" :title="'待认领 ' + pending + ' 条'">待认领 {{ pending }}</span><span class="grow"></span>
        <div class="seg"><button :class="{on: seg === 'all'}" @click="setSeg('all')">全部</button><button :class="{on: seg === 'new'}" @click="setSeg('new')">待认领</button><button :class="{on: seg === 'claimed'}" @click="setSeg('claimed')">已认领</button><button :class="{on: seg === 'invalid'}" @click="setSeg('invalid')">无效</button></div></div>
      <zb-paged :items="subs" :size="5" :m-size="3" :fit="220" unit="条" cls="rs-qs-list"><template #default="{ item: s }"><div class="qsub rs-qsub">
        <div class="ev-grid rs-qs-ph"><ev-photo v-for="p in s.photos" :key="p.id" size="f" :src="p.thumb" :full="p.url" alt="扫码上报照片"></ev-photo><ev-photo v-if="!s.photos.length" size="f"></ev-photo></div>
        <div class="grow rs-qs-b"><div class="row wrap"><b>{{ s.label }}</b><span class="tiny muted">{{ fmtDT(s.created_at) }}</span><span class="tiny muted">回执 {{ s.receipt }}</span></div>
          <div class="small" v-if="s.note">“{{ s.note }}”</div>
          <div class="tiny muted" v-if="s.team || s.name">上报人：{{ [s.team, s.name].filter(Boolean).join(' · ') }}（自填）</div>
          <div class="row wrap mt"><span class="chip" :class="s.scan === 'hazard' ? 'red' : s.scan === 'clear' ? 'green' : ''">{{ SCAN_TEXT[s.scan] }}</span>
            <a class="small" v-for="f in s.findings" :key="f.id" @click="go('finding', {id: f.id})"><span :class="['chip', LEVEL_F[f.level]]">{{ f.level_name }}</span><span class="chip orange" v-if="f.candidate">候选</span> {{ f.title }}</a></div>
          <div class="mt" v-for="f in s.findings.filter((x) => x.candidate)" :key="'c' + f.id"><div class="tiny"><b>待确认：</b>{{ f.title }}</div><candidate-box :f="f" :thumb="false" @changed="loadSubs"></candidate-box></div>
          <div class="row wrap mt" v-if="s.status === 'new'"><button class="btn primary sm" :disabled="busy" @click="act(s, 'claim')">认领</button><button class="btn sm" :disabled="busy" @click="act(s, 'invalid')">标无效</button>
            <span class="tiny muted">认领后{{ isSubOrg() ? '已确认的未处理隐患归入名下' : '仅作标记，责任人不变' }}；AI 候选隐患须先在待我处理里确认</span></div>
          <div class="tiny muted mt" v-else>{{ s.status === 'claimed' ? '已由 ' + (s.claimed_by || '') + ' 认领' : '已标无效' }} {{ fmtDT(s.claimed_at) }}</div></div></div></template></zb-paged>
      <div class="zb-empty" v-if="!subs.length">{{ seg === 'new' ? '暂无待认领上报。工友扫码提交后会出现在这里。' : '这个分类下暂无记录。' }}</div></section>

    <div class="rs-qr-side">
    <section class="zb-card"><div class="zb-ch"><ic n="plus"></ic><h2>生成部位二维码</h2></div>
      <div class="row wrap rs-qr-new"><input class="in grow" v-model.trim="label" maxlength="40" placeholder="如 3号墩" aria-label="部位" @keyup.enter="create">
        <select class="in" v-model.number="days" aria-label="有效期"><option :value="7">有效 7 天</option><option :value="30">有效 30 天</option><option :value="60">有效 60 天</option><option :value="90">有效 90 天</option></select>
        <button class="btn primary" :disabled="!label || busy" @click="create">生成</button></div>
      <div class="tiny muted mt" v-if="base">{{ localBase ? '手机暂时打不开这里生成的二维码，请联系系统管理员设置访问地址。' : lanBase ? (isSubOrg() ? '工友手机要连工地 Wi-Fi 才能扫开。' : '手机需与电脑在同一网络（同一 Wi-Fi）才能打开。') : '' }}</div>
      <div class="tiny muted mt"><help-tip caption="有效期与限次" text="到期自动失效，可随时作废；每码每小时限 12 次提交。"></help-tip></div></section>

    <section class="zb-card"><div class="zb-ch"><ic n="layers"></ic><h2>{{ isSubOrg() ? '本单位二维码' : '全部二维码' }}</h2><span class="zb-n">{{ qrs.length }}</span></div>
      <zb-paged :items="qrs" :size="5" :m-size="3" :fit="160" unit="个" cls="rs-qr-list"><template #default="{ item: q }"><div class="qrow2">
        <button type="button" class="qthumb" v-html="svg(q)" v-if="q.status === 'active'" :aria-label="'打开 ' + q.label + ' 的二维码海报'" @click="poster = q"></button><div class="qthumb off" v-else></div>
        <div class="grow"><div class="row wrap"><b>{{ q.label }}</b><span :class="['chip', q.status === 'active' ? 'green' : '']">{{ {active: '有效', expired: '已过期', revoked: '已作废'}[q.status] }}</span></div>
          <div class="tiny muted">有效期至 {{ q.expires_at.slice(0, 10) }} · 共 {{ q.submissions }} 次上报{{ q.new ? ' · ' + q.new + ' 条待认领' : '' }} · {{ q.created_by }} 生成</div>
          <div class="row wrap mt" v-if="q.status === 'active'"><button class="btn sm primary" @click="poster = q">海报打印</button><button class="btn sm" @click="copy(q)">复制链接</button><button class="btn sm" @click="revoke(q)">作废</button></div></div></div></template></zb-paged>
      <div class="zb-empty" v-if="!qrs.length">暂无二维码。在上方输入部位生成。</div></section></div></div>

    <teleport to="body"><div class="qp-mask" v-if="poster" @click.self="poster = null">
      <div class="qp-bar"><button class="btn primary" @click="printIt">打印 A4 海报</button><button class="btn" @click="poster = null">关闭</button><span class="tiny">建议竖版 A4 打印，关闭页眉页脚</span></div>
      <div class="qp-sheet"><div class="qp-poster">
        <div class="qp-brand">{{ store.me.project ? store.me.project.name : '智眸·工笔' }}</div>
        <div class="qp-title">发现隐患 扫码上报</div>
        <div class="qp-sub">免登录 · 拍摄 1–4 张照片 · 简述情况 · 安全员即时接收</div>
        <div class="qp-qr" v-html="svg(poster)"></div>
        <div class="qp-label">{{ poster.label }}</div>
        <div class="qp-foot">有效期至 {{ poster.expires_at.slice(0, 10) }} · 编号 Q{{ poster.id }}{{ poster.company ? ' · ' + poster.company : '' }}</div>
        <div class="qp-url">{{ url(poster) }}</div></div></div></div></teleport></div>`,
  data: () => ({ label: "", days: 30, busy: false, base: "", qrs: [], subs: [], seg: "new", pending: 0, poster: null, store, SCAN_TEXT, LEVEL_F }),
  computed: { localBase() { return LOCAL_RE.test(this.base); }, lanBase() { return LAN_RE.test(this.base); } },
  async mounted() {
    this.base = location.origin;
    try { const b = await GET("/api/quick/base"); this.base = b.base || (LOCAL_RE.test(location.origin) && b.lan ? b.lan : location.origin); } catch (_) { /* 用当前地址 */ }
    await Promise.all([this.loadQrs(), this.loadSubs()]);
    this.t = setInterval(() => { this.loadSubs(); this.loadQrs(); }, 15000);  // 新上报与识别结果自动刷新
    this.after = () => document.body.classList.remove("qp-printing");
    window.addEventListener("afterprint", this.after);
  },
  unmounted() { clearInterval(this.t); window.removeEventListener("afterprint", this.after); document.body.classList.remove("qp-printing"); },
  methods: {
    go, fmtDT, isSubOrg,
    url(q) { return this.base.replace(/\/+$/, "") + q.path; },
    svg(q) { try { return QRGen.svg(this.url(q)); } catch (_) { return ""; } },
    async loadQrs() { try { this.qrs = await GET("/api/quick/qrs"); } catch (e) { toast(e.message, true); } },
    async loadSubs() {
      try {
        this.subs = await GET("/api/quick/submissions?status=" + this.seg);
        if (this.seg === "new") this.pending = this.subs.length; else this.pending = (await GET("/api/quick/summary")).new;
      } catch (_) { /* 轮询失败忽略 */ }
    },
    setSeg(s) { this.seg = s; this.loadSubs(); },
    async create() {
      if (!this.label || this.busy) return;
      this.busy = true;
      try { const q = await POST("/api/quick/qrs", { label: this.label, days: this.days }); this.label = ""; await this.loadQrs(); this.poster = q; toast("二维码已生成"); }
      catch (e) { toast(e.message, true); } finally { this.busy = false; }
    },
    async revoke(q) {
      if (!confirm(`作废「${q.label}」的二维码？作废后立即失效，已张贴的需更换。`)) return;
      try { await POST(`/api/quick/qrs/${q.id}/revoke`); toast("已作废"); await this.loadQrs(); } catch (e) { toast(e.message, true); }
    },
    async copy(q) {
      const t = this.url(q);
      try { await navigator.clipboard.writeText(t); toast("链接已复制"); } catch (_) { prompt("复制链接：", t); }
    },
    async act(s, what) {
      this.busy = true;
      try { await POST(`/api/quick/submissions/${s.id}/${what}`); toast(what === "claim" ? "已认领" : "已标为无效"); await Promise.all([this.loadSubs(), this.loadQrs()]); }
      catch (e) { toast(e.message, true); } finally { this.busy = false; }
    },
    printIt() { document.body.classList.add("qp-printing"); setTimeout(() => window.print(), 50); },
  },
};
EXT.route("qrcodes", QuickQr);

// 扫码待认领是首页统一待办（/api/todo）的来源之一（有候选隐患的上报以候选条目为准，同一件事只一个入口），这里不再注册首页提示
