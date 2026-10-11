/* 智眸工笔 Service Worker（T-10b，P0-1 AC-5：无网时从主屏打开）。
 * 只管应用外壳：首页 / 、静态资源 /static/、manifest、图标、离线自检页。业务接口一律不经过这里、不进缓存——
 * 判定函数 cacheable() 对接口路径直接返回 false，fetch 处理不调用 respondWith，请求原样走网络。
 * 策略：网络优先，成功就更新缓存；网络失败（离线）才读缓存；页面导航没有缓存时回退到首页外壳。
 * 有缓存的请求网络最多等 4 秒；判定连不上后 15 秒内有缓存的直接给缓存（连着 Wi-Fi 但服务器不可达时不逐个干等）。
 * 安装时按首页里引用的 /static/ 资源预缓存一遍，所以第一次打开后即使没再刷新，离线冷启动也有完整外壳。
 */
'use strict';

const CACHE = 'zb-shell-v5';
const SHELL_EXTRA = ['/manifest.webmanifest', '/favicon.ico', '/offline-check'];

function cacheable(url) {
  if (url.origin !== self.location.origin) return false;
  const p = url.pathname;
  if (p === '/api' || p.startsWith('/api/')) return false;
  return p === '/' || p.startsWith('/static/') || p === '/manifest.webmanifest' || p === '/favicon.ico' || p === '/offline-check';
}

// 同一个静态文件只留最新版本（?v= 变了就删旧的），缓存不随发版无限增长
async function store(cache, req, res) {
  const url = new URL(typeof req === 'string' ? req : req.url, self.location.origin);
  if (url.pathname.startsWith('/static/')) {
    for (const k of await cache.keys()) {
      const ku = new URL(k.url);
      if (ku.pathname === url.pathname && ku.href !== url.href) await cache.delete(k);
    }
  }
  await cache.put(req, res);
}

async function precache() {
  const cache = await caches.open(CACHE);
  let html = '';
  try {
    const res = await fetch('/', { cache: 'no-cache' });
    if (res.ok) {
      html = await res.clone().text();
      await cache.put('/', res);
    }
  } catch (e) { /* 安装时离线：跳过，等下次在线访问时再缓存 */ }
  const urls = new Set(SHELL_EXTRA);
  for (const m of html.matchAll(/(?:src|href)="(\/static\/[^"]+)"/g)) urls.add(m[1]);
  await Promise.all([...urls].map(async (u) => {
    try {
      const res = await fetch(u, { cache: 'no-cache' });
      if (res.ok) await store(cache, u, res);
    } catch (e) { /* 单个资源失败不影响安装 */ }
  }));
}

self.addEventListener('install', (event) => {
  event.waitUntil(precache().then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    for (const n of await caches.keys()) if (n.startsWith('zb-') && n !== CACHE) await caches.delete(n);
    await self.clients.claim();
  })());
});

// 连不上服务器时（连着 Wi-Fi 但服务器不可达、信号差），每个请求都要等很久才失败；
// 有缓存的请求最多等 NET_TIMEOUT，一旦判定连不上，DOWN_MS 内有缓存的直接用缓存，不再逐个等
const NET_TIMEOUT = 4000;
const DOWN_MS = 15000;
let downUntil = 0;

async function cachedCopy(cache, req) {
  return (await cache.match(req)) || (await cache.match(req, { ignoreSearch: true }))
    || (req.mode === 'navigate' ? await cache.match('/') : undefined);
}

async function networkFirst(req) {
  const cache = await caches.open(CACHE);
  const cached = await cachedCopy(cache, req);
  if (cached && Date.now() < downUntil) return cached;
  const net = fetch(req).then(async (res) => {
    downUntil = 0;
    if (res.ok && !res.redirected) await store(cache, req, res.clone());
    return res;
  }, (err) => {
    downUntil = Date.now() + DOWN_MS;
    throw err;
  });
  if (!cached) return net;  // 没缓存过的照常走网络，失败就失败，不编造内容
  let timer;
  const late = new Promise((resolve) => { timer = setTimeout(resolve, NET_TIMEOUT, null); });
  try {
    const res = await Promise.race([net, late]);
    if (res) return res;
    downUntil = Date.now() + DOWN_MS;
  } catch (err) { /* 网络失败：用缓存 */ } finally {
    clearTimeout(timer);
  }
  net.catch(() => {});
  return cached;
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET' || req.headers.has('range')) return;
  const url = new URL(req.url);
  if (!cacheable(url)) return;
  event.respondWith(networkFirst(req));
});
