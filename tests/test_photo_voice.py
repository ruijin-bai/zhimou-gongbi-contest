"""拍照时同时录语音（RJ 2026-10-09 意见 3）：前端部分。

- 水印相机里可边拍边说：只录音、存本机，不当场调用语音模型；拍完随照片带出（files.voice），上传确认里可听、可删、可补录；
- 暂存队列：语音带 note_cid（那条照片随手记的 client_id），照片随手记还在本机排队时语音先等，传上去后语音才补传，
  这样服务器转写后能把文字补进那条记录；照片随手记被放弃（失败）时语音照常上传、单独成条；
- 随手记页：照片记录用自己生成的 client_id，录音跟在后面补传；照片没记上也不丢录音。
服务端（补进照片随手记、录音保留、回放权限）见 tests/test_offline_sync.py。
"""
import re

from test_capture_front import cam, read
from test_offline_front import run_app


def test_cam_recorder_hands_over_audio_with_server_time():
    out = cam("""
      class MR { constructor(s) { this.s = s; this.mimeType = 'audio/webm'; } start() {} stop() { this.ondataavailable({ data: { size: 5 } }); this.onstop(); } }
      MediaRecorder = MR; navigator.mediaDevices = { getUserMedia: async () => ({ getTracks: () => [{ stop() { stopped++; } }] }) };
      Blob = function (parts, o) { this.size = parts.reduce((n, p) => n + p.size, 0); this.type = o.type; };
      const r = camRecorder();
      await r.start(); out.on = r.st.on;
      const v = await r.stop();
      out.v = { name: v.name, mime: v.mime, size: v.blob.size, sec: v.seconds, skew: Math.round((v.recordedAt - Date.now() - 3600000) / 1000) };
      out.off = r.st.on; out.stopped = stopped;
      await r.start(); out.aborted = await r.abort();
      out.none = await camRecorder().stop();
    """, "var stopped = 0; var SN = Date.now() + 3600000; this.serverNow = () => new Date(SN); this.isSecureContext = true;")
    assert out["on"] is True and out["off"] is False and out["stopped"] == 1
    assert out["v"] == {"name": "voice.webm", "mime": "audio/webm", "size": 5, "sec": 1, "skew": 0}  # 录音时间取服务器校正时钟
    assert out["aborted"] is None and out["none"] is None


def test_camera_finish_attaches_voice_and_mic_only_for_note_photos():
    out = cam("""
      const W = WatermarkCamera;
      let got = null;
      store.cam = { multi: true, voice: true, resolve: (f) => { got = f; } };
      const me = { shots: [{ file: { name: 'a.jpg' } }], voice: null, rec: { st: { on: true }, stop: async () => ({ seconds: 4 }) } };
      await W.methods.finish.call(me);
      out.files = got.length; out.voice = got.voice;
      isSecureContext = true; MediaRecorder = function () {}; navigator.mediaDevices = {};
      store.cam = { voice: true }; out.micNote = W.computed.voiceOk.call({});
      store.cam = { voice: false }; out.micOther = W.computed.voiceOk.call({});
    """)
    assert out == {"files": 1, "voice": {"seconds": 4}, "micNote": True, "micOther": False}
    tpl = re.search(r"const WatermarkCamera = \{\s*template: `(.*?)`", read("camera.js"), re.S).group(1)
    assert 'v-if="voiceOk"' in tpl and "录音中" in tpl


def test_photo_confirm_returns_voice_only_when_allowed():
    out = cam("""
      const P = PhotoConfirm.methods; const res = [];
      const mk = (allow) => ({ c: { allowVoice: allow }, scan: true, voice: { seconds: 3 }, rec: { st: { on: false } }, stopPlay() {}, done(v) { res.push(v); } });
      photoConfirm.cur = { resolve() {} };
      await P.ok.call(mk(true)); await P.ok.call(mk(false));
      out.res = res;
    """)
    assert out["res"] == [{"scan": True, "voice": {"seconds": 3}}, {"scan": True, "voice": None}]


def test_queued_voice_waits_for_its_photo_note_then_carries_note_cid():
    out = run_app("""
const sent = [];
let netUp = false;
fetch = async (u, init) => {
  if (!netUp) throw new TypeError('Failed to fetch');
  const f = init.body instanceof FormData ? fields(init.body) : JSON.parse(init.body);
  sent.push([u, f]);
  if (u === '/api/quick/sync/media') return J({ id: 41, client_id: f.client_id });
  if (u === '/api/quick/sync/note') return J({ id: 77, client_id: f.client_id });
  return J({ pending_id: 5, status: 'pending', recorded_at: '', late_days: 0, client_id: f.client_id });
};
const cid = QUICK.newCid('n');
await qEnqueueMedia((() => { const fd = new FormData(); fd.append('purpose', 'log'); fd.append('files', new Blob(['jpg']), 'a.jpg'); fd.append('taken_at', ''); return fd; })());
const mid = QQ.items.find((x) => x.kind === 'media').cid;
await qEnqueueNote({ client_id: cid, text: '', media_ids: ['local:' + mid], source: 'photo' });
// 语音比照片记录先排进来也要等（离线时先录完、后点上传的情况）
await QUICK.addVoice({ blob: new Blob([new Uint8Array([0x1a, 0x45, 0xdf, 0xa3, 1])], { type: 'audio/webm' }), name: 'voice.webm', seconds: 4, noteCid: cid });
const v = (await qrun('q', 'readonly', (s) => s.getAll())).find((r) => r.kind === 'voice');
v.seq = 0; await qPut(v);
netUp = true;
await qSync(true);
const firstRound = sent.map((s) => s[0]);
await qSync(true);
return { firstRound, order: sent.map((s) => s[0]), voice: sent.find((s) => s[0] === '/api/quick/sync/voice')[1].note_cid, cid, left: QQ.n };
""")
    assert out["firstRound"] == ["/api/quick/sync/media", "/api/quick/sync/note"]  # 语音在照片记录之前也先等
    assert out["order"][-1] == "/api/quick/sync/voice" and out["voice"] == out["cid"] and out["left"] == 0


def test_voice_goes_alone_when_its_photo_note_failed():
    out = run_app("""
const sent = [];
fetch = async (u, init) => { sent.push([u, fields(init.body)]); return J({ pending_id: 5, status: 'pending', recorded_at: '', late_days: 0, client_id: init.body.get('client_id') }); };
await qPut({ cid: 'nDEAD0001', kind: 'note', uid: qMyUid(), seq: 1, created: new Date().toISOString(), status: 'failed', tries: 0, next: 0, err: 'x', body: { text: '', media_ids: [] } });
await QUICK.addVoice({ blob: new Blob([new Uint8Array([0x1a, 0x45, 0xdf, 0xa3, 1])], { type: 'audio/webm' }), name: 'voice.webm', seconds: 4, noteCid: 'nDEAD0001' });
await qSync(true);
return { sent: sent.map((s) => [s[0], s[1].note_cid]) };
""")
    assert out["sent"] == [["/api/quick/sync/voice", "nDEAD0001"]]  # 服务器找不到那条记录就单独成条


def test_tech_photo_note_uses_own_client_id_and_keeps_voice_when_upload_fails():
    src = read("tech.js")
    body = re.search(r"async savePhoto\(\) \{(.*?)\n    \},", src, re.S).group(1)
    assert "takePhotosToUpload({ voice: true })" in body and "client_id: cid" in body
    assert re.search(r"finally \{.*if \(voice\) this\.saveAudio\(\{ \.\.\.voice, noteCid \}\)", body, re.S)
    assert "playAudio" in src and "/api/quick/voice/${pid}/audio" in src
