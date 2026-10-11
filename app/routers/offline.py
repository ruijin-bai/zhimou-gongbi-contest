"""离线打开（T-10b，P0-1 AC-5）：Service Worker、manifest、根证书下载、实测自检页。

- GET /sw.js：放在根路径并带 Service-Worker-Allowed: /，作用域覆盖整个站点；每次按 ETag 校验，发版后浏览器能及时拿到新 SW。
- GET /manifest.webmanifest：添加到主屏用。
- GET /tls/zhimou-ca.crt：`serve --https` 生成的本地根证书（DER，公开内容），供手机下载后在系统设置里安装；
  只出这一个文件，私钥不经任何路由；没有生成过（HTTP 实例）就 404。
- GET /offline-check：实测自检页（T-10a 用）。注册 SW，逐项显示安全上下文、SW 状态、离线缓存、麦克风、定位是否可用。
  不需要登录，不读写业务数据；应用首页里的 SW 注册由 T-10c 加。
"""
from __future__ import annotations

from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "static"

CHECK_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0f2a44">
<title>离线自检 · 智眸工笔</title>
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" type="image/svg+xml" href="/favicon.ico">
<style>
  :root { --brand: #0d5ea8; --ink: #18212f; --ink3: #7b8597; --line: #e2e6ec; --ok: #1f8a4c; --bad: #c0392b; --warn: #d97706; }
  body { margin: 0; background: #f3f5f8; color: var(--ink); font: 15px/1.55 -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; }
  main { max-width: 560px; margin: 0 auto; padding: 16px; }
  h1 { font-size: 18px; margin: 4px 0 12px; }
  .row { display: flex; justify-content: space-between; gap: 12px; padding: 10px 12px; background: #fff; border: 1px solid var(--line); border-radius: 8px; margin-bottom: 8px; }
  .row b { font-weight: 600; }
  .v { text-align: right; word-break: break-all; }
  .ok { color: var(--ok); } .bad { color: var(--bad); } .warn { color: var(--warn); }
  button, a.btn { display: inline-block; margin: 4px 6px 4px 0; padding: 8px 14px; border-radius: 6px; border: 1px solid var(--brand); background: #fff; color: var(--brand); font-size: 15px; text-decoration: none; }
  button.main, a.main { background: var(--brand); color: #fff; }
  p.note { color: var(--ink3); font-size: 13px; }
</style>
</head>
<body>
<main>
  <h1>离线自检</h1>
  <div id="rows"></div>
  <div>
    <button class="main" id="mic">测麦克风</button>
    <button class="main" id="geo">测定位</button>
    <button id="again">重新检测</button>
    <button id="reset">清除离线缓存</button>
  </div>
  <div>
    <a class="btn main" href="/">打开系统首页</a>
    <a class="btn" href="/tls/zhimou-ca.crt" download="zhimou-ca.crt">下载根证书</a>
  </div>
  <p class="note">本页只检查浏览器能力，不读写任何业务数据。添加到主屏：浏览器菜单 → 添加到主屏幕（或“安装应用”）。</p>
</main>
<script>
const rows = {};
function put(key, label, text, cls) {
  let el = rows[key];
  if (!el) {
    el = rows[key] = document.createElement('div');
    el.className = 'row';
    el.innerHTML = '<b></b><span class="v"></span>';
    el.firstChild.textContent = label;
    document.getElementById('rows').appendChild(el);
  }
  el.lastChild.textContent = text;
  el.lastChild.className = 'v ' + (cls || '');
}
async function check() {
  put('online', '网络', navigator.onLine ? '在线' : '离线', navigator.onLine ? 'ok' : 'warn');
  put('secure', '安全上下文（HTTPS 证书被信任）', window.isSecureContext ? '是' : '否', window.isSecureContext ? 'ok' : 'bad');
  if (!('serviceWorker' in navigator)) {
    put('sw', 'Service Worker', '浏览器不提供（证书不被信任或不是 HTTPS）', 'bad');
    return;
  }
  try {
    const reg = await navigator.serviceWorker.register('/sw.js', { scope: '/' });
    put('sw', 'Service Worker', '注册成功，作用域 ' + new URL(reg.scope).pathname, 'ok');
    const ready = await Promise.race([navigator.serviceWorker.ready, new Promise((r) => setTimeout(() => r(null), 8000))]);
    put('active', 'SW 已激活', ready && ready.active ? '是（' + ready.active.state + '）' : '还没有，稍后点“重新检测”', ready && ready.active ? 'ok' : 'warn');
  } catch (e) {
    put('sw', 'Service Worker', '注册失败：' + (e && e.message || e), 'bad');
    return;
  }
  put('ctl', '本页由 SW 接管', navigator.serviceWorker.controller ? '是' : '否（刷新一次后应为是）', navigator.serviceWorker.controller ? 'ok' : 'warn');
  if (window.caches) {
    let n = 0, home = false;
    for (const name of await caches.keys()) {
      if (!name.startsWith('zb-')) continue;
      const c = await caches.open(name);
      n += (await c.keys()).length;
      home = home || !!(await c.match('/'));
    }
    put('cache', '离线缓存', n + ' 个文件，首页' + (home ? '已缓存' : '未缓存'), home ? 'ok' : 'warn');
  }
  const standalone = window.matchMedia('(display-mode: standalone)').matches;
  put('mode', '打开方式', standalone ? '主屏图标（独立窗口）' : '浏览器标签页', '');
}
document.getElementById('mic').onclick = async () => {
  try {
    const s = await navigator.mediaDevices.getUserMedia({ audio: true });
    s.getTracks().forEach((t) => t.stop());
    put('mic', '麦克风', '可用', 'ok');
  } catch (e) { put('mic', '麦克风', '不可用：' + (e && (e.name || e.message) || e), 'bad'); }
};
document.getElementById('geo').onclick = () => {
  if (!navigator.geolocation) { put('geo', '定位', '浏览器不提供', 'bad'); return; }
  put('geo', '定位', '获取中…', 'warn');
  navigator.geolocation.getCurrentPosition(
    (p) => put('geo', '定位', '可用（精度约 ' + Math.round(p.coords.accuracy) + ' 米）', 'ok'),
    (e) => put('geo', '定位', '不可用：' + e.message, 'bad'), { timeout: 15000 });
};
document.getElementById('again').onclick = check;
document.getElementById('reset').onclick = async () => {
  if (navigator.serviceWorker) for (const r of await navigator.serviceWorker.getRegistrations()) await r.unregister();
  if (window.caches) for (const n of await caches.keys()) if (n.startsWith('zb-')) await caches.delete(n);
  put('sw', 'Service Worker', '已注销并清除缓存，点“重新检测”重新注册', 'warn');
};
window.addEventListener('online', check);
window.addEventListener('offline', check);
check();
</script>
</body>
</html>
"""


def register(app) -> None:
    from fastapi import HTTPException
    from fastapi.responses import FileResponse, HTMLResponse

    from ..config import settings

    @app.get("/sw.js", include_in_schema=False)
    def service_worker():
        return FileResponse(STATIC / "sw.js", media_type="text/javascript",
                            headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest():
        return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json", headers={"Cache-Control": "no-cache"})

    @app.get("/tls/zhimou-ca.crt", include_in_schema=False)
    def ca_cert():
        path = settings.data_dir / "tls" / "zhimou-ca.crt"
        if not path.is_file():
            raise HTTPException(404, "没有根证书：用 serve --https 启动后才会生成")
        return FileResponse(path, media_type="application/octet-stream", filename="zhimou-ca.crt", headers={"Cache-Control": "no-cache"})

    @app.get("/offline-check", include_in_schema=False)
    def offline_check():
        return HTMLResponse(CHECK_PAGE, headers={"Cache-Control": "no-cache"})
