/* 水印相机：按公司模板（今日水印相机）在照片左上角印 logo + 项目名 + 工作地点 + 工作内容 + 拍摄时间（标签统一中文）。
   - 拍摄时间取服务器时钟，自动生成，不可修改；工作地点、工作内容由拍摄人填写（必填，记住上次的）。
   - 横竖屏（D4）：小票、隐患照片（会进识别队列的随手拍/日志/随手记）、整改照片横拍竖拍都收；其余照片（隐蔽验收、私信）只收横屏。
     系统相机路径下非现场刚拍（>15 分钟）的照片一律拒收。
   takePhotos() 是全站唯一的拍照入口：有 HTTPS/本机时打开应用内相机（取景即所见），否则先填水印信息再调系统相机补印。
   水印直接烧进像素；拍摄时间随文件上传（taken_at），供数据审核判断“照片是否当天所拍”。 */

const pad2 = (n) => String(n).padStart(2, "0");
const wmIso = (d) => `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}T${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
const wmStamp = (d) => `${d.getFullYear()}${pad2(d.getMonth() + 1)}${pad2(d.getDate())}_${pad2(d.getHours())}${pad2(d.getMinutes())}${pad2(d.getSeconds())}`;
const wmGeo = { cur: null, id: null };
const WM_PLACE_KEY = () => "zb_wm_place_" + (store.me ? store.me.user.id : 0);
const WM_WORK_KEY = () => "zb_wm_work_" + (store.me ? store.me.user.id : 0);
let wmCamBad = false;

// 水印上由人填写的两项；改动即记住，下次打开沿用
const wmForm = Vue.reactive({ area: "", work: "" });
const wmFormLoad = () => { wmForm.area = lsGet(WM_PLACE_KEY(), "") || ""; wmForm.work = lsGet(WM_WORK_KEY(), "") || ""; };
const wmFormSave = () => { lsSet(WM_PLACE_KEY(), wmForm.area); lsSet(WM_WORK_KEY(), wmForm.work); };
const wmFormOk = () => !!(wmForm.area.trim() && wmForm.work.trim());

// 拍摄时间只认服务器时钟（取响应头 Date，秒级精度），手机时间改了也不影响水印。
// 优先用 core.js 的 serverNow()（偏移存在 localStorage，离线启动也能用）；没有该函数、或它抛错/给出非法值时，退回本文件的 wmClock（每次拍照取一次 /api/me 的响应头）
const wmClock = { off: 0 };
async function wmSync() {
  try {
    const t0 = Date.now(), r = await GET("/api/me", { raw: true, quiet: true }), t1 = Date.now(), sd = Date.parse(r.headers.get("date"));
    if (sd) wmClock.off = sd + 500 - (t0 + t1) / 2;
  } catch (_) { /* 取不到就用本机时钟 */ }
}
// 服务器时钟 − 本机时钟（毫秒）；serverNow() 返回 Date 或毫秒数都可以
function wmOff() {
  if (typeof serverNow === "function") {
    try { const s = +serverNow(); if (Number.isFinite(s) && s > 0) return s - Date.now(); } catch (_) { /* 退回 wmClock */ }
  }
  return wmClock.off;
}
function wmNow() { return new Date(Date.now() + wmOff()); }

// 每张照片都带拍摄时间、手机定位（无定位传空串，服务端不补默认值）与地点/部位，数组与 files 按序对应
// scan 为 false 表示拍摄者选了“不识别”（只在会进识别队列的上传里传）
function addPhotos(fd, files, scan) {
  if (scan !== undefined) fd.set("scan", scan ? 1 : 0);
  files.forEach((f) => {
    const g = f.geo || null;
    fd.append("files", f); fd.append("taken_at", f.takenAt || "");
    fd.append("geo_lat", g ? g.lat : ""); fd.append("geo_lon", g ? g.lon : ""); fd.append("geo_acc", g && g.acc != null ? g.acc : "");
    fd.append("place", f.place || "");
  });
}

function wmSetGeo(p) { wmGeo.cur = { lat: p.coords.latitude, lon: p.coords.longitude, acc: p.coords.accuracy, t: Date.now() }; return wmGeo.cur; }
const wmGeoOk = () => !!navigator.geolocation && window.isSecureContext;
// 手机浏览器只在 HTTPS（或本机）下开放定位。取不到定位只是弱提示：记录行上显示灰色“未取得定位”，不弹红色错误；
// 只有用户主动点“未取得定位”时才告诉怎么开启（wmGeoHelp）。不补默认坐标。
function wmGeoHint() { /* 不再主动弹提示 */ }
function wmGeoHelp() {
  toast(wmGeoOk() ? "未取得定位：请在浏览器的网站设置里允许“位置信息”，并确认手机已开启定位服务" : "当前为 http 访问，手机浏览器不开放定位：请改用 HTTPS 地址访问（python -m app serve --https）");
}
function wmGeoWatch(on) {
  if (!wmGeoOk()) return;
  if (on && wmGeo.id === null) {
    wmGeo.id = navigator.geolocation.watchPosition(wmSetGeo, () => { /* 拒绝定位时只是不印坐标 */ }, { enableHighAccuracy: true, maximumAge: 30000, timeout: 20000 });
  } else if (!on && wmGeo.id !== null) { navigator.geolocation.clearWatch(wmGeo.id); wmGeo.id = null; }
}
const wmGeoNow = () => (wmGeo.cur && Date.now() - wmGeo.cur.t < 600000 ? wmGeo.cur : null);
function wmGeoOnce(timeout = 2500) {
  if (wmGeoNow() || !wmGeoOk()) { wmGeoHint(); return Promise.resolve(wmGeoNow()); }
  return new Promise((res) => navigator.geolocation.getCurrentPosition((p) => res(wmSetGeo(p)), () => res(null), { enableHighAccuracy: true, timeout, maximumAge: 60000 }));
}
// 位置显示文字：只显示地点文字，有定位没地点写“现场”；经纬度只随记录入库（用于同地点查询），不显示。没有定位如实写“未取得定位”
function geoText(g) {
  if (!g) return "未取得定位";
  return (g.place || "").trim() || (g.lat != null ? "现场" : "未取得定位");
}

// geo 只作元数据随照片入库，不印在水印上
function wmInfo(when) {
  const me = store.me;
  return { when, place: wmForm.area.trim(), work: wmForm.work.trim(), geo: wmGeoNow(), proj: me && me.project ? me.project.name : "" };
}

function wmWrap(ctx, text, maxW) {
  // 英文按词换行，中文逐字换行；单词本身比一行还宽时才拆开
  const out = []; let cur = "";
  const put = (tok) => {
    if (!cur && /^\s+$/.test(tok)) return;
    if (cur && ctx.measureText(cur + tok).width > maxW) { out.push(cur.trimEnd()); cur = ""; if (/^\s+$/.test(tok)) return; }
    if (!cur && ctx.measureText(tok).width > maxW) { for (const ch of tok) { if (cur && ctx.measureText(cur + ch).width > maxW) { out.push(cur); cur = ""; } cur += ch; } return; }
    cur += tok;
  };
  (text.match(/[A-Za-z0-9.,:;'’\-+#/()%]+|\s+|[^\sA-Za-z0-9]/g) || []).forEach(put);
  if (cur.trim()) out.push(cur.trimEnd());
  return out;
}

const WM_LOGO = new Image();
WM_LOGO.src = "/static/chec-logo.png?v=1";

function wmRoundRect(ctx, x, y, w, h, r) {
  ctx.beginPath(); ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r); ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

// 公司模板：左上角面板 = logo + 蓝色项目名标题栏 + 浅色正文（工作地点 / 工作内容 / 拍摄时间）。尺寸按 2000px 宽的稿子等比缩放
function wmDraw(ctx, w, h, info) {
  const u = w / 2000, x0 = 30 * u, y0 = 30 * u, pw = Math.min(746 * u, w * 0.5), hh = 174 * u, pad = 22 * u;
  const fs = 46 * u, lh = 57 * u, gap = 14 * u;
  const fam = '"Helvetica Neue",Arial,"PingFang SC","Microsoft YaHei","Noto Sans SC",sans-serif';
  const d = info.when, stamp = `${d.getFullYear()}.${pad2(d.getMonth() + 1)}.${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
  ctx.save(); ctx.textBaseline = "alphabetic";
  ctx.font = `600 ${52 * u}px ${fam}`;
  const title = wmWrap(ctx, info.proj || "", pw - hh - 48 * u).slice(0, 2);
  ctx.font = `400 ${fs}px ${fam}`;
  // 标签统一中文（U-40、D4），三行取值左对齐
  const labW = ctx.measureText("工作内容：").width + 16 * u, valW = pw - pad * 2 - labW;
  const rows = [["工作地点：", wmWrap(ctx, info.place || "", valW).slice(0, 2)], ["工作内容：", wmWrap(ctx, info.work || "", valW).slice(0, 5)]]
    .map(([k, ls]) => [k, ls.length ? ls : [""]]);
  const bodyH = rows.reduce((n, r) => n + r[1].length * lh + gap, 0) + lh + gap + 6 * u, ph = hh + bodyH;
  wmRoundRect(ctx, x0, y0, pw, ph, 22 * u); ctx.save(); ctx.clip();
  ctx.fillStyle = "rgba(240,242,246,.88)"; ctx.fillRect(x0, y0, pw, ph);
  ctx.fillStyle = "#639bee"; ctx.fillRect(x0 + hh, y0, pw - hh, hh);
  ctx.fillStyle = "#fff"; ctx.fillRect(x0, y0, hh, hh);
  if (WM_LOGO.complete && WM_LOGO.naturalWidth) ctx.drawImage(WM_LOGO, x0 + 7 * u, y0 + 7 * u, hh - 14 * u, hh - 14 * u);
  ctx.restore();
  ctx.fillStyle = "#fff"; ctx.font = `600 ${52 * u}px ${fam}`;
  const tl = 62 * u, ty = y0 + hh / 2 - (title.length - 1) * tl / 2 + 18 * u;
  title.forEach((l, i) => ctx.fillText(l, x0 + hh + 28 * u, ty + i * tl));
  ctx.fillStyle = "#111"; ctx.font = `400 ${fs}px ${fam}`;
  let y = y0 + hh + gap + fs * 0.95;
  rows.forEach(([k, ls]) => {
    ctx.fillText(k, x0 + pad, y);
    ls.forEach((l, i) => ctx.fillText(l, x0 + pad + labW, y + i * lh));
    y += ls.length * lh + gap;
  });
  ctx.fillText("拍摄时间：", x0 + pad, y); ctx.fillText(stamp, x0 + pad + labW, y);
  ctx.restore();
}

// 把画面（视频帧或图片）绘到 canvas 并印上水印；长边限制在 maxEdge 内
function wmRender(cv, src, sw, sh, info, maxEdge) {
  const k = Math.min(1, maxEdge / Math.max(sw, sh)), w = Math.round(sw * k), h = Math.round(sh * k);
  if (cv.width !== w) cv.width = w;
  if (cv.height !== h) cv.height = h;
  const ctx = cv.getContext("2d");
  ctx.drawImage(src, 0, 0, w, h);
  wmDraw(ctx, w, h, info);
}
const wmBlob = (cv) => new Promise((res) => cv.toBlob(res, "image/jpeg", 0.9));
function wmFile(blob, when, info) {
  const f = new File([blob], `wm_${wmStamp(when)}.jpg`, { type: "image/jpeg", lastModified: +when });
  f.takenAt = wmIso(when);
  f.geo = info.geo ? { lat: info.geo.lat, lon: info.geo.lon, acc: Math.round(info.geo.acc) } : null;
  f.place = info.place || "";
  return f;
}

// 竖拍规则（D4）：调用方显式传 allowPortrait 为准；没传时，单张拍摄（multi:false，目前只有整改照片在用）允许竖拍，其余只收横屏。
// 小票传 allowPortrait:true；takePhotosToUpload（隐患照片：随手拍/日志/随手记）默认允许。归类由 tests/test_capture_front.py 逐个调用方核对
function wmPortraitOk(opts = {}) { return opts.allowPortrait !== undefined ? !!opts.allowPortrait : opts.multi === false; }
const wmOrientOk = (w, h, portrait) => !!portrait || w >= h;

// 系统相机路径：只收刚拍（15 分钟内）的照片，竖拍按 portrait 决定；水印时间 = 拍摄时刻（文件时间）按服务器时钟校正，不接受相册旧图
async function wmStampFile(file, portrait) {
  let url = "";
  try {
    if (!file.lastModified || Math.abs(Date.now() - file.lastModified) > 15 * 60000) { toast("须现场拍照，不接受相册或旧照片", true); return null; }
    url = URL.createObjectURL(file);
    const img = new Image(); img.src = url; await img.decode();
    if (!wmOrientOk(img.naturalWidth, img.naturalHeight, portrait)) { toast("请横屏拍摄：竖拍照片已丢弃", true); return null; }
    const when = new Date(file.lastModified + wmOff()), cv = document.createElement("canvas"), info = wmInfo(when);
    wmRender(cv, img, img.naturalWidth, img.naturalHeight, info, 2560);
    return wmFile(await wmBlob(cv), when, info);
  } catch (_) { toast("照片无法处理，请重拍", true); return null; }
  finally { if (url) URL.revokeObjectURL(url); }
}

function wmPickFiles(multi, portrait) {
  return new Promise((resolve) => {
    const inp = wmPickFiles.inp = document.createElement("input");  // 挂到函数上，避免 iOS 回收导致 change 不触发
    inp.type = "file"; inp.accept = "image/*"; inp.multiple = multi;
    if (matchMedia("(pointer: coarse)").matches) inp.setAttribute("capture", "environment");
    inp.addEventListener("change", async () => {
      const files = [...inp.files]; wmPickFiles.inp = null;
      if (!files.length) return resolve([]);
      await wmGeoOnce();
      resolve((await Promise.all(files.map((f) => wmStampFile(f, portrait)))).filter(Boolean));
    });
    inp.addEventListener("cancel", () => { wmPickFiles.inp = null; resolve([]); });
    inp.click();
  });
}

// opts.voice：允许边拍边录一段语音说明（随手记拍照用）。录到的语音挂在返回数组的 .voice 上
function takePhotos(opts = {}) {
  const multi = opts.multi !== false, portrait = wmPortraitOk(opts);
  wmFormLoad(); wmSync();
  const cam = !wmCamBad && window.isSecureContext && navigator.mediaDevices && navigator.mediaDevices.getUserMedia;
  return new Promise((resolve) => { store.cam = { multi, portrait, resolve, mode: cam ? "cam" : "sheet", voice: !!opts.voice }; });
}

// ---------------------------------------------------------------------------- 拍照时的语音说明
// 只录音、存本机，不当场调用语音模型识别（RJ 10-09：没网也不丢）：随记录进暂存队列，联网后补传，服务器后台转文字补进这条照片记录，录音保留可回放。
// 应用内录音需 HTTPS（MediaRecorder）；http 访问时改用系统录音机（文件选择，capture）。最长 3 分钟。
const camVoiceOk = () => !!(window.isSecureContext && navigator.mediaDevices && window.MediaRecorder);
const CAM_VOICE_MAX = 180;
function camRecorder() {
  const st = Vue.reactive({ on: false, sec: 0 });
  let r = null, stream = null, chunks = [], at = null, t0 = 0, timer = 0, done = null;
  const fin = (keep) => new Promise((res) => {
    if (!r) return res(null);
    done = (blob) => res(keep && blob && blob.size ? { blob, name: blob.type.includes("mp4") ? "voice.m4a" : "voice.webm", mime: blob.type || "", seconds: Math.max(1, Math.round((Date.now() - t0) / 1000)), recordedAt: at } : null);
    try { r.stop(); } catch (_) { const d = done; done = null; d(null); }
  });
  const rec = {
    st,
    async start() {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      chunks = []; r = new MediaRecorder(stream); at = wmNow(); t0 = Date.now();
      r.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
      r.onstop = () => {
        clearInterval(timer); st.on = false; if (stream) stream.getTracks().forEach((t) => t.stop());
        const blob = new Blob(chunks, { type: (r && r.mimeType) || "audio/webm" }); r = null; stream = null;
        if (done) { const d = done; done = null; d(blob); }
      };
      r.start(); st.on = true; st.sec = 0;
      timer = setInterval(() => { st.sec = Math.round((Date.now() - t0) / 1000); if (st.sec >= CAM_VOICE_MAX && rec.onMax) rec.onMax(); }, 500);
    },
    stop: () => fin(true),    // 停止并交出录音 {blob, name, mime, seconds, recordedAt}
    abort: () => fin(false),  // 放弃
    onMax: null,
  };
  return Vue.markRaw(rec);
}
// 系统录音机交回的文件：文件时间是录完那一刻；不是刚录的就按现在算
function camVoiceFromFile(f) {
  if (!f || !f.size) return null;
  const fresh = f.lastModified && Math.abs(Date.now() - f.lastModified) < 1800000;
  return { blob: f, name: f.name || "voice.m4a", mime: f.type || "", seconds: null, recordedAt: fresh ? new Date(f.lastModified + wmOff()) : wmNow() };
}

// 拍完先到“上传确认”：看缩略图、选上传后要不要 AI 识别隐患（默认识别，选择按人记住）。
// 识别始终在后台进行：上传一返回就放行，结果由 photoFeedback 气泡提示；选“不识别”则只存照片，不进安全Agent队列。
// 返回 {files, scan} 或 null（取消，照片丢弃）。只用于会进识别队列的照片（随手拍/日志/随手记），整改、票据、隐蔽验收照片不走这里。
// 这类照片即“隐患照片”，横拍竖拍都收（D4）。
const photoConfirm = Vue.reactive({ cur: null });
const scanPrefKey = () => "zb_scan_pref_" + (store.me ? store.me.user.id : 0);
// opts.voice：随手记拍照可附一段语音说明（拍照时录，或在这里补录），返回 {files, scan, voice}
async function takePhotosToUpload(opts = {}) {
  const files = await takePhotos({ allowPortrait: true, ...opts });
  if (!files.length) return null;
  const got = await new Promise((resolve) => { photoConfirm.cur = { files, voice: files.voice || null, allowVoice: !!opts.voice, resolve }; });
  return got === null ? null : { files, scan: got.scan, voice: got.voice };
}

const PhotoConfirm = {
  template: `<div class="modal-bg pc-bg" v-if="c" @click.self="cancel"><div class="modal pc">
    <h2>上传 {{ c.files.length }} 张照片</h2>
    <div class="pc-th"><img v-for="(u, i) in urls" :key="i" :src="u"></div>
    <div class="pc-voice" v-if="c.allowVoice">
      <template v-if="voice"><span class="pc-vi"><ic n="mic"></ic></span><span class="grow"><b class="small">语音说明{{ voice.seconds ? ' ' + voice.seconds + ' 秒' : '' }}</b>
          <span class="tiny muted pc-d">随照片保存，联网后转成文字补进这条记录，原声保留</span></span>
        <button class="btn ghost sm" type="button" @click="play">{{ playing ? '停' : '听' }}</button><button class="btn ghost sm" type="button" @click="dropVoice">删</button></template>
      <template v-else><button type="button" :class="['btn', 'sm', rec.st.on ? 'danger' : '']" @click="toggleRec"><ic n="mic"></ic> {{ rec.st.on ? '停止 ' + rec.st.sec + 's' : '补一段语音说明' }}</button>
        <span class="tiny muted grow">可选；只录音，不当场识别</span></template>
      <input ref="vf" type="file" accept="audio/*" capture hidden @change="pickVoice"></div>
    <label class="pc-opt" :class="{on: scan}">
      <input type="checkbox" v-model="scan">
      <span class="pc-sw"><i></i></span>
      <span class="grow"><b>上传后 AI 识别隐患</b>
        <span class="tiny muted pc-d">{{ scan ? '拍完 AI 识别，不用等，可以接着拍；疑似隐患等安全员确认后再派单' : '只保存照片，不做识别' }}</span></span></label>
    <div class="row mt"><button class="btn" @click="cancel">放弃</button><div class="grow"></div><button class="btn primary" @click="ok">上传</button></div></div></div>`,
  data: () => ({ scan: true, urls: [], voice: null, playing: false, rec: camRecorder() }),
  computed: { c() { return photoConfirm.cur; } },
  watch: {
    c: {
      immediate: true,
      handler(c) {
        this.urls.forEach((u) => URL.revokeObjectURL(u));
        this.urls = c ? c.files.map((f) => URL.createObjectURL(f)) : [];
        this.stopPlay(); this.voice = c ? c.voice : null;
        if (c) this.scan = lsGet(scanPrefKey(), true) !== false;
      },
    },
  },
  created() { this.rec.onMax = () => this.toggleRec(); },
  beforeUnmount() { this.urls.forEach((u) => URL.revokeObjectURL(u)); this.stopPlay(); this.rec.abort(); },
  methods: {
    done(v) { const c = photoConfirm.cur; photoConfirm.cur = null; this.stopPlay(); if (c) c.resolve(v); },
    async ok() {
      if (this.rec.st.on) this.voice = await this.rec.stop();  // 还在录：录到哪算哪
      lsSet(scanPrefKey(), this.scan); this.done({ scan: this.scan, voice: this.c.allowVoice ? this.voice : null });
    },
    cancel() { if (confirm(`放弃这 ${this.c.files.length} 张照片${this.voice ? "和语音说明" : ""}？`)) { this.rec.abort(); this.done(null); } },
    async toggleRec() {
      if (!camVoiceOk()) { this.$refs.vf.click(); return; }
      if (this.rec.st.on) { this.voice = await this.rec.stop(); return; }
      try { await this.rec.start(); } catch (e) { toast("无法使用麦克风：" + e.message, true); }
    },
    pickVoice(ev) { const f = ev.target.files[0]; ev.target.value = ""; this.voice = camVoiceFromFile(f); },
    dropVoice() { this.stopPlay(); this.voice = null; },
    play() {
      if (this.playing) { this.stopPlay(); return; }
      this.au = new Audio(URL.createObjectURL(this.voice.blob)); this.au.onended = () => this.stopPlay();
      this.au.play().then(() => { this.playing = true; }).catch(() => toast("这台手机播放不了这段录音，录音已保存，上传后可再听", true));
    },
    stopPlay() { if (this.au) { this.au.pause(); URL.revokeObjectURL(this.au.src); this.au = null; } this.playing = false; },
  },
};
EXT.slot("global", PhotoConfirm);

// 无 HTTPS 时：先填水印信息，再调系统相机（点“开始拍照”本身就是调起相机所需的用户操作）
const WatermarkSheet = {
  template: `<div class="modal-bg" @click.self="cancel"><div class="modal">
    <h2>拍照水印信息</h2>
    <div class="small muted">拍摄时间由系统生成，不可修改{{ portrait ? '。' : '；须横屏拍摄。' }}</div>
    <div class="field"><label class="f">工作地点（必填）</label><input class="in" v-model.trim="f.area" maxlength="30" placeholder="如 CH10+950 / 3#墩"></div>
    <div class="field"><label class="f">工作内容（必填）</label><textarea class="in" rows="3" v-model.trim="f.work" maxlength="120" placeholder="如 轨道梁基础开挖换填"></textarea></div>
    <div class="row mt" style="justify-content:flex-end"><button class="btn" @click="cancel">取消</button><button class="btn primary" :disabled="!ok" @click="go">开始拍照</button></div></div></div>`,
  data: () => ({ f: wmForm }),
  computed: { ok() { return wmFormOk(); }, portrait() { return !!(store.cam && store.cam.portrait); } },
  watch: { f: { deep: true, handler: wmFormSave } },
  methods: {
    cancel() { const c = store.cam; store.cam = null; if (c) c.resolve([]); },
    async go() {
      const c = store.cam; wmFormSave();
      const files = await wmPickFiles(c.multi, c.portrait);
      if (store.cam === c) store.cam = null;
      c.resolve(files);
    },
  },
};

const WatermarkCamera = {
  template: `<div class="cam">
    <canvas ref="cv" class="cam-cv"></canvas>
    <video ref="v" class="cam-src" playsinline muted autoplay></video>
    <div class="cam-rot" v-if="ready && !land && !portrait"><span><ic n="refresh"></ic></span>请横屏拍摄</div>
    <div class="cam-tip" v-if="!ready">相机启动中</div>
    <div class="cam-hint" v-else-if="(land || portrait) && !ok">请先填写工作地点和工作内容</div>
    <div class="cam-geo" v-if="ready">{{ geoState }}</div>
    <div class="cam-rec" v-if="rec.st.on"><i></i>录音中 {{ rec.st.sec }}s，再点麦克风停止</div>
    <div class="cam-rec ok" v-else-if="voice">已录语音 {{ voice.seconds }} 秒</div>
    <div class="cam-fields">
      <input class="cam-in" v-model.trim="f.area" maxlength="30" placeholder="工作地点（必填）">
      <input class="cam-in wide" v-model.trim="f.work" maxlength="120" placeholder="工作内容（必填）"></div>
    <div class="cam-ctl">
      <button class="cam-btn" @click="cancel"><ic n="x"></ic></button>
      <button class="cam-btn" @click="flip"><ic n="refresh"></ic></button>
      <button :class="['cam-btn', 'cam-mic', {on: rec.st.on}]" v-if="voiceOk" :aria-label="rec.st.on ? '停止录音' : '边拍边说'" @click="toggleRec"><ic n="mic"></ic></button>
      <div class="grow"></div>
      <img class="cam-last" v-if="shots.length" :src="shots[shots.length - 1].url">
      <button class="cam-shutter" :disabled="!canShoot" @click="shoot"><i></i></button>
      <button class="cam-done" v-if="store.cam.multi" :disabled="!shots.length" @click="finish">完成{{ shots.length ? ' ' + shots.length : '' }}</button></div>
    <div class="cam-flash" v-if="flash"></div></div>`,
  data: () => ({ store, f: wmForm, shots: [], ready: false, land: true, flash: false, saving: false, facing: "environment", rt: Vue.markRaw({ stream: null, raf: 0, alive: true }),
    rec: camRecorder(), voice: null }),
  computed: {
    voiceOk() { return !!(store.cam && store.cam.voice) && camVoiceOk(); },  // 边拍边说：只在随手记拍照、且能应用内录音时出现
    ok() { return wmFormOk(); },
    portrait() { return !!(store.cam && store.cam.portrait); },  // 本次拍摄允许竖拍（D4）
    canShoot() { return this.ready && (this.land || this.portrait) && this.ok && !this.saving; },
    geoState() { const g = wmGeoNow(); return g ? `已定位 ±${Math.round(g.acc)}m` : wmGeoOk() ? "定位中" : "无法定位（需 HTTPS）"; },
  },
  watch: { f: { deep: true, handler: wmFormSave } },
  mounted() {
    wmGeoWatch(true); wmGeoHint(); this.open(); this.geoTick = setInterval(() => this.$forceUpdate(), 1500);
    if (this.rec) this.rec.onMax = () => this.toggleRec();  // 录满 3 分钟自动停
    // 只收横屏的照片：手机上尽量锁横屏（Android Chrome 需全屏；iOS 不支持，靠“请横屏”提示与拍照门槛兜底）。允许竖拍时不锁，随手机方向
    if (!this.portrait && matchMedia("(pointer: coarse)").matches && document.documentElement.requestFullscreen) {
      document.documentElement.requestFullscreen().then(() => screen.orientation && screen.orientation.lock("landscape")).catch(() => { /* 不支持就算了 */ });
    }
  },
  beforeUnmount() {
    clearInterval(this.geoTick); this.rt.alive = false; this.stop(); wmGeoWatch(false); this.shots.forEach((s) => URL.revokeObjectURL(s.url)); this.rec.abort();
    try { if (screen.orientation && screen.orientation.unlock) screen.orientation.unlock(); if (document.fullscreenElement) document.exitFullscreen(); } catch (_) { /* 忽略 */ }
  },
  methods: {
    stop() { cancelAnimationFrame(this.rt.raf); if (this.rt.stream) this.rt.stream.getTracks().forEach((t) => t.stop()); this.rt.stream = null; this.ready = false; },
    async open() {
      this.stop();
      try {
        this.rt.stream = await navigator.mediaDevices.getUserMedia({ audio: false, video: { facingMode: { ideal: this.facing }, width: { ideal: 1920 }, height: { ideal: 1080 } } });
        if (!this.rt.alive) return this.stop();
        const v = this.$refs.v; v.srcObject = this.rt.stream; await v.play();
        this.ready = true; this.loop();
      } catch (e) {
        wmCamBad = true; const c = store.cam; store.cam = null;
        toast("相机不可用：" + (e.name === "NotAllowedError" ? "未授权" : e.message) + "。已切换系统相机，请重新拍照", true);
        c.resolve([]);
      }
    },
    loop() {
      const v = this.$refs.v, cv = this.$refs.cv;
      const tick = () => {
        if (!this.rt.alive) return;
        if (v.videoWidth) {
          const land = v.videoWidth >= v.videoHeight; if (land !== this.land) this.land = land;
          wmRender(cv, v, v.videoWidth, v.videoHeight, wmInfo(wmNow()), 1280);
        }
        this.rt.raf = requestAnimationFrame(tick);
      };
      tick();
    },
    flip() { this.facing = this.facing === "environment" ? "user" : "environment"; this.open(); },
    async shoot() {
      const v = this.$refs.v; if (!this.canShoot || !v.videoWidth || !wmOrientOk(v.videoWidth, v.videoHeight, this.portrait)) return;
      this.saving = true;
      try {
        const when = wmNow(), cv = document.createElement("canvas"), info = wmInfo(when);
        wmRender(cv, v, v.videoWidth, v.videoHeight, info, 2560);
        const blob = await wmBlob(cv);
        this.shots.push({ file: wmFile(blob, when, info), url: URL.createObjectURL(blob) });
        this.flash = true; setTimeout(() => (this.flash = false), 120);
      } finally { this.saving = false; }
      if (!store.cam.multi) this.finish();
    },
    async toggleRec() {
      if (this.rec.st.on) { this.voice = await this.rec.stop(); return; }
      if (this.voice && !confirm("重录会替换刚才那段语音，继续？")) return;
      try { await this.rec.start(); } catch (e) { toast("无法使用麦克风：" + e.message, true); }
    },
    async finish() {
      const c = store.cam, files = this.shots.map((s) => s.file);
      if (this.rec.st.on) this.voice = await this.rec.stop();  // 拍完还在录：录到哪算哪
      if (this.voice) files.voice = this.voice;
      store.cam = null; c.resolve(files);
    },
    cancel() {
      const v = this.voice || this.rec.st.on;
      if ((this.shots.length || v) && !confirm(`放弃已拍的 ${this.shots.length} 张照片${v ? "和语音" : ""}？`)) return;
      this.rec.abort(); const c = store.cam; store.cam = null; c.resolve([]);
    },
  },
};

// ---------------------------------------------------------------------------- 拍完立刻有反馈（原位气泡）
// photoFeedback.begin(n, scan) 在拍完那一刻先显示"正在上传 n 张…"，慢网时附上已用秒数（F-20）；上传返回后 h.sent(媒体列表) 才改为"已收到 n 张，识别中"
// 并开始轮询安全Agent的识别进展（F-18④：上传完成之前不说"已收到"）；scan=false 时只提示上传结果。出结果后给一句话摘要。气泡只提示，不挡操作；出结果后约 7 秒自动收起，鼠标/手指按住时暂停，也可手动 ✕。
// "不对？"只复用既有的 /api/findings/{id}/status（忽略为误报）及其权限：没有该权限（分包技术员/安全员）时不显示按钮，只提示找安全员复核。
const fbState = Vue.reactive({ items: [] });
let fbSeq = 0;
const FB_POLL_MS = 3000, FB_POLL_MAX = 120, FB_SHOW_MS = 7000, FB_SLOW_S = 3;   // 识别最长等约 6 分钟（后端单次 180 秒含重试），期间显示已等秒数

function fbSummary(st) {
  const mock = store.me && store.me.ai && store.me.ai.provider === "mock";
  const tag = mock ? "（离线模拟识别，仅供演示）" : "";
  const fs = st.findings || [];
  if (fs.length) {
    const scene = (st.scenes || []).find((s) => !/离线模拟/.test(s));
    const parts = fs.slice(0, 2).map((f) => `${f.title.replace(/^[^：]{1,8}：/, "").slice(0, 16)}（${f.level_name}）`);
    return `${scene ? "识别到" + scene + "；" : ""}发现 ${fs.length} 处：${parts.join("、")}${fs.length > 2 ? "等" : ""}，为候选隐患，待安全员确认后再派单${tag}`;
  }
  if (st.failed && !st.items.some((i) => i.status === "clear")) return "识别暂不可用，照片已保存，可由安全员重新识别";
  return `未发现明显隐患${tag}`;
}

const photoFeedback = {
  begin(n, scan = true) {
    const up = `正在上传 ${n} 张…`;
    const b = Vue.reactive({ id: ++fbSeq, state: "upload", text: up, n, sec: 0, findings: [], fixed: {}, hold: false, timer: 0, poll: 0, tick: 0 });
    b.tick = setInterval(() => { b.sec++; if (b.sec >= FB_SLOW_S) b.text = `${up}已用 ${b.sec} 秒`; }, 1000);
    fbState.items.push(b);
    while (fbState.items.length > 3) photoFeedback.close(fbState.items[0]);
    const landed = () => { clearInterval(b.tick); b.tick = 0; };
    return {
      // 上传成功：才说"已收到"并开始轮询；若照片只是暂存在本机（弱网），提示联网后自动上传
      sent: (media) => {
        landed();
        const list = [].concat(media || []);
        if (list.some((m) => m && m.queued)) { b.state = "queued"; b.text = `已暂存 ${list.length} 张，联网后自动上传${scan ? "并识别" : ""}`; photoFeedback.autoClose(b); return; }
        const ids = list.map((m) => m.id).filter((x) => Number.isInteger(x));
        if (!ids.length) { photoFeedback.close(b); return; }
        if (!scan) { b.state = "clear"; b.text = `已上传 ${list.length} 张（未做隐患识别）`; photoFeedback.autoClose(b, 3500); return; }
        b.state = "wait"; b.text = `已收到 ${list.length} 张，识别中`; b.ids = ids;
        const t0 = Date.now(); b.tick = setInterval(() => { if (b.state === "wait") b.text = `已收到 ${list.length} 张，识别中 · 已等 ${Math.round((Date.now() - t0) / 1000)} 秒`; }, 1000);
        photoFeedback.watch(b, ids);
      },
      fail: (msg) => { landed(); b.state = "fail"; b.text = "照片上传失败：" + (msg || "请重试"); photoFeedback.autoClose(b, 9000); },
    };
  },
  watch(b, ids) {
    let k = 0;
    const tick = async () => {
      if (!fbState.items.includes(b)) return;
      k++;
      try {
        const st = await GET("/api/quick/media-status?ids=" + ids.join(","), { quiet: true });
        if (!st.pending) {
          clearInterval(b.tick); b.tick = 0; b.findings = st.findings || [];
          // 识别失败：不自动收起，写明照片已保存并给“重新识别”（验收 I10）
          if (st.failed && !b.findings.length && !st.items.some((i) => i.status === "clear")) { b.state = "fail"; b.retry = true; b.text = `识别失败：${st.failed} 张没有识别出结果，照片已保存，可以重新识别`; return; }
          b.state = b.findings.length ? "hazard" : "clear"; b.text = fbSummary(st); photoFeedback.autoClose(b); return;
        }
      } catch (_) { /* 轮询失败不打扰，下一轮再试 */ }
      if (k >= FB_POLL_MAX) { clearInterval(b.tick); b.tick = 0; b.state = "fail"; b.retry = true; b.text = "识别超时：等了约 6 分钟仍没有结果，照片已保存；可以重新识别，也会在“待我处理”里提示"; return; }
      b.poll = setTimeout(tick, FB_POLL_MS);
    };
    b.poll = setTimeout(tick, 1200);
  },
  autoClose(b, ms = FB_SHOW_MS) {
    clearTimeout(b.timer);
    const fire = () => { if (b.hold) b.timer = setTimeout(fire, 1500); else photoFeedback.close(b); };
    b.timer = setTimeout(fire, ms);
  },
  close(b) { clearTimeout(b.timer); clearTimeout(b.poll); clearInterval(b.tick); const i = fbState.items.indexOf(b); if (i >= 0) fbState.items.splice(i, 1); },
  async rescan(b) {
    if (!b.ids || b.busy) return;
    b.busy = true;
    try {
      await POST("/api/quick/media-rescan", { ids: b.ids }); b.retry = false; b.state = "wait"; b.findings = [];
      const t0 = Date.now(); b.tick = setInterval(() => { if (b.state === "wait") b.text = `重新识别中 · 已等 ${Math.round((Date.now() - t0) / 1000)} 秒`; }, 1000);
      photoFeedback.watch(b, b.ids);
    } catch (e) { b.text = "重新识别没有开始：" + (e.message || "请稍后再试"); } finally { b.busy = false; }
  },
};

const PhotoBubble = {
  template: `<div class="pfb-wrap" v-if="items.length">
    <div v-for="b in items" :key="b.id" :class="['pfb', b.state]" @mouseenter="b.hold = true" @mouseleave="b.hold = false" @touchstart.passive="b.hold = true" @touchend.passive="b.hold = false">
      <div class="pfb-h"><span class="pfb-i"><ic :n="icon(b)"></ic></span><span class="pfb-t">{{ b.text }}</span><button class="pfb-x" @click="close(b)" aria-label="关闭"><ic n="x"></ic></button></div>
      <div class="pfb-row" v-for="f in b.findings" :key="f.id">
        <span :class="['chip', LEVEL_F[f.level]]">{{ f.level_name }}</span>
        <span class="pfb-ft">{{ f.title }}</span>
        <span class="tiny muted" v-if="b.fixed[f.id]">已标为误报</span>
        <template v-else>
          <a class="small" @click="open(f)">查看</a>
          <button class="btn sm" v-if="canFix" :disabled="busy === f.id" @click="wrong(b, f)">标为误报</button></template></div>
      <div class="pfb-row" v-if="b.retry"><button class="btn sm primary" :disabled="b.busy" @click="rescan(b)">{{ b.busy ? '提交中…' : '重新识别' }}</button><button class="btn sm" @click="close(b)">稍后再说</button></div>
      <div class="pfb-hint" v-if="b.findings.length && !canFix">识别有误请告知安全员，由其在“待确认隐患”中处理（确认或不采纳）。</div>
    </div></div>`,
  data: () => ({ LEVEL_F, busy: 0 }),
  computed: {
    items() { return fbState.items; },
    canFix() { return can("site_safety") || roleOf() === "sub_safety"; },
  },
  methods: {
    icon(b) { return { upload: "arrow-up", wait: "hourglass", hazard: "alert", clear: "check-circle", queued: "download", fail: "alert" }[b.state] || "camera"; },
    close: (b) => photoFeedback.close(b),
    rescan: (b) => photoFeedback.rescan(b),
    open(f) { go("finding", { id: f.id }); },
    async wrong(b, f) {
      this.busy = f.id; b.hold = true;
      try { await POST(`/api/findings/${f.id}/status`, { status: "dismissed", note: "拍摄者现场反馈：识别有误（误报）" }); b.fixed[f.id] = true; toast("已标为误报"); photoFeedback.autoClose(b, 4000); }
      catch (e) { toast(e.message, true); } finally { this.busy = 0; b.hold = false; }
    },
  },
};
EXT.slot("global", PhotoBubble);
