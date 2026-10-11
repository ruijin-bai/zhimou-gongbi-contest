"""T-10b Service Worker 与证书（P0-1 AC-5 的服务端与证书部分）。

- routers/offline.py：/sw.js（Service-Worker-Allowed: /、不缓存）、/manifest.webmanifest、实测自检页 /offline-check、
  根证书下载 /tls/zhimou-ca.crt（只给公开的根证书，不给私钥；没有生成时 404）。
- static/sw.js：node 里用最小桩跑一遍行为（安装预缓存、网络优先、离线回退），并静态检查 /api 不经过 SW、不进缓存。
- __main__._self_signed：本地根 CA（CA:TRUE、名称约束只放行 localhost 与内网 IP）+ 叶子证书（SAN、签发链），
  旧自签名证书平滑替换，局域网 IP 变化只重签叶子、根证书不变（手机不用重装）。用 OpenSSL 实际握手验证。
"""
import datetime as dt
import ipaddress
import json
import re
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from fastapi.testclient import TestClient

from app.__main__ import _self_signed, main
from app.config import settings
from app.main import app

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"
NODE = shutil.which("node")


# ============================================================================ 路由与响应头

def test_sw_route_headers():
    r = TestClient(app).get("/sw.js")
    assert r.status_code == 200
    assert r.headers["service-worker-allowed"] == "/"
    assert "javascript" in r.headers["content-type"]
    assert r.headers["cache-control"] == "no-cache"
    assert r.text == (STATIC / "sw.js").read_text(encoding="utf-8")


def test_manifest_route():
    c = TestClient(app)
    r = c.get("/manifest.webmanifest")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/manifest+json")
    m = r.json()
    assert m["start_url"] == "/" and m["scope"] == "/" and m["display"] == "standalone"
    assert m["name"] == "智眸工笔"
    assert m["icons"]
    for icon in m["icons"]:
        assert c.get(icon["src"]).status_code == 200, icon["src"]


def test_offline_check_page_no_login():
    r = TestClient(app).get("/offline-check")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "register('/sw.js'" in r.text
    assert 'rel="manifest" href="/manifest.webmanifest"' in r.text
    assert "/tls/zhimou-ca.crt" in r.text


def test_ca_download_only_public_cert():
    c = TestClient(app)
    tls = settings.data_dir / "tls"
    ca = tls / "zhimou-ca.crt"
    if ca.exists():
        ca.unlink()
    assert c.get("/tls/zhimou-ca.crt").status_code == 404
    info = _self_signed(tls, ips=["192.168.50.7"])
    r = c.get("/tls/zhimou-ca.crt")
    assert r.status_code == 200
    # 以附件下载（不让浏览器当场调起安装器；Android 11 起只能在系统设置里装 CA）
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["content-disposition"].startswith("attachment") and "zhimou-ca.crt" in r.headers["content-disposition"]
    assert r.content == info["ca"].read_bytes()
    x509.load_der_x509_certificate(r.content)  # DER，Android 可直接安装
    # 私钥和其他文件不经这个路由出去
    for p in ("/tls/ca.key.pem", "/tls/key.pem", "/tls/cert.pem", "/tls/../.secret"):
        assert c.get(p).status_code == 404, p


# ============================================================================ sw.js 静态检查

def test_sw_static_never_caches_api():
    src = (STATIC / "sw.js").read_text(encoding="utf-8")
    # 判定函数里显式排除 /api，fetch 处理在判定为不可缓存时直接 return（不调用 respondWith）
    assert re.search(r"p === '/api' \|\| p\.startsWith\('/api/'\)\) return false", src)
    assert re.search(r"if \(!cacheable\(url\)\) return;", src)
    # cache.put 只出现在 cacheable 之后的网络优先与预缓存里
    assert src.count("cache.put(") >= 2
    assert "/api" not in src.replace("'/api'", "").replace("'/api/'", "").split("*/", 1)[1]


# ============================================================================ sw.js 行为（node 最小桩）

SW_HARNESS = r"""
const vm = require('vm'), fs = require('fs');
const [swFile] = process.argv.slice(2);
const ORIGIN = 'https://192.168.50.7:8621';
const store = new Map();               // 缓存：url -> Response
const norm = (r) => (typeof r === 'string' ? new URL(r, ORIGIN).href : r.url);
const strip = (u) => u.split('?')[0];
const cacheObj = {
  put: async (r, res) => { store.set(norm(r), res); },
  match: async (r, o) => { const u = norm(r); if (store.has(u)) return store.get(u).clone();
    if (o && o.ignoreSearch) for (const [k, v] of store) if (strip(k) === strip(u)) return v.clone(); return undefined; },
  keys: async () => [...store.keys()].map((u) => ({ url: u })),
  delete: async (r) => store.delete(norm(r)),
};
const names = new Set(['zb-old-shell']);
const caches = { open: async (n) => { names.add(n); return cacheObj; }, keys: async () => [...names], delete: async (n) => names.delete(n) };
let online = true; const fetched = [];
const HTML = '<link rel="stylesheet" href="/static/style.css?v=42"><script src="/static/vendor/vue.global.prod.js"></script>' +
             '<script src="/static/core.js?v=39"></script>';
const fetchStub = async (r) => {
  const u = norm(r); fetched.push(u);
  if (online === 'hang') return new Promise(() => {});  // 连着网但服务器不回
  if (!online) throw new TypeError('Failed to fetch');
  const p = new URL(u).pathname;
  if (p === '/') return new Response(HTML, { status: 200, headers: { 'content-type': 'text/html' } });
  if (p === '/static/missing.js') return new Response('nf', { status: 404 });
  return new Response('body:' + u, { status: 200 });
};
const handlers = {};
const self = { location: { origin: ORIGIN, href: ORIGIN + '/sw.js' }, addEventListener: (t, f) => { handlers[t] = f; },
  skipWaiting: async () => {}, clients: { claim: async () => {} } };
const ctx = { self, caches, fetch: fetchStub, URL, Response, Request, Headers, console, Promise, Set, Map, setTimeout, clearTimeout };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(swFile, 'utf8'), ctx);

async function fire(type, ev) { const waits = []; ev.waitUntil = (p) => waits.push(p); handlers[type](ev); await Promise.all(waits); }
async function req(path, init) {
  const { mode, ...rest } = init || {}; const r = new Request(ORIGIN + path, rest); let rw = null;
  if (mode) Object.defineProperty(r, 'mode', { value: mode });  // node 的 Request 不允许直接构造 navigate
  const ev = { request: r, respondWith: (p) => { rw = p; }, waitUntil: () => {} };
  handlers.fetch(ev);
  if (!rw) return { intercepted: false };
  try { const res = await rw; return { intercepted: true, status: res.status, body: await res.text() }; }
  catch (e) { return { intercepted: true, error: String(e) }; }
}
(async () => {
  const out = {};
  await fire('install', {});
  await fire('activate', {});
  out.cacheNames = [...names];
  out.afterInstall = [...store.keys()].sort();
  out.api = await req('/api/me');
  out.apiPost = await req('/api/quick/sync', { method: 'POST', body: '{}' });
  out.staticPost = await req('/static/core.js', { method: 'POST', body: '{}' });
  out.healthz = await req('/healthz');
  out.online = await req('/static/core.js?v=40');
  out.notFound = await req('/static/missing.js');
  out.afterOnline = [...store.keys()].sort();
  online = false;
  out.offStatic = await req('/static/style.css?v=42');
  out.offOtherVer = await req('/static/core.js?v=41');
  out.offNav = await req('/', { mode: 'navigate' });
  out.offNavQuery = await req('/?from=home', { mode: 'navigate' });
  out.offMissing = await req('/static/never.js');
  out.offApi = await req('/api/me');
  // 刚判定连不上：有缓存的直接给缓存，不再发请求
  online = 'hang'; let n0 = fetched.length, t0 = Date.now();
  out.downCached = await req('/static/style.css?v=42');
  out.downCachedMs = Date.now() - t0; out.downCachedFetches = fetched.length - n0;
  // 恢复联网：没缓存的请求走网络成功，解除判定
  online = true;
  out.fresh = await req('/static/fresh.js');
  // 服务器不回：有缓存的最多等 4 秒就用缓存
  online = 'hang'; n0 = fetched.length; t0 = Date.now();
  out.hang = await req('/static/style.css?v=42');
  out.hangMs = Date.now() - t0; out.hangFetches = fetched.length - n0;
  out.fetchedApi = fetched.filter((u) => u.includes('/api'));
  out.keysFinal = [...store.keys()];
  process.stdout.write(JSON.stringify(out));
})().catch((e) => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
"""


@pytest.fixture(scope="module")
def sw_run():
    if not NODE:
        pytest.skip("没有 node")
    with tempfile.TemporaryDirectory() as td:
        h = Path(td) / "sw_harness.js"
        h.write_text(SW_HARNESS, encoding="utf-8")
        r = subprocess.run([NODE, str(h), str(STATIC / "sw.js")], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stderr[-3000:]
    return json.loads(r.stdout)


O = "https://192.168.50.7:8621"


def test_sw_install_precaches_shell(sw_run):
    keys = sw_run["afterInstall"]
    for p in ("/", "/static/style.css?v=42", "/static/vendor/vue.global.prod.js", "/static/core.js?v=39",
              "/manifest.webmanifest", "/favicon.ico", "/offline-check"):
        assert O + p in keys, p
    assert "zb-old-shell" not in sw_run["cacheNames"]  # 旧版本缓存在 activate 时清掉


def test_sw_never_touches_api(sw_run):
    for k in ("api", "apiPost", "offApi", "healthz", "staticPost"):
        assert sw_run[k] == {"intercepted": False}, k
    assert sw_run["fetchedApi"] == []
    assert not [k for k in sw_run["keysFinal"] if "/api" in k]


def test_sw_network_first_and_offline_fallback(sw_run):
    assert sw_run["online"] == {"intercepted": True, "status": 200, "body": f"body:{O}/static/core.js?v=40"}
    assert O + "/static/core.js?v=40" in sw_run["afterOnline"]
    assert O + "/static/core.js?v=39" not in sw_run["afterOnline"]  # 同一文件只留最新版本
    assert sw_run["notFound"]["status"] == 404 and O + "/static/missing.js" not in sw_run["afterOnline"]
    assert sw_run["offStatic"]["status"] == 200 and sw_run["offStatic"]["body"].endswith("/static/style.css?v=42")
    assert sw_run["offOtherVer"]["status"] == 200 and sw_run["offOtherVer"]["body"].endswith("core.js?v=40")
    assert sw_run["offNav"]["status"] == 200 and "/static/core.js?v=39" in sw_run["offNav"]["body"]
    assert sw_run["offNavQuery"]["status"] == 200 and sw_run["offNavQuery"]["body"] == sw_run["offNav"]["body"]
    assert "error" in sw_run["offMissing"]  # 没缓存过的资源离线时照常失败，不编造内容


def test_sw_unreachable_server_falls_back_fast(sw_run):
    # 刚失败过：15 秒内有缓存的直接用缓存，不发请求、不等待
    assert sw_run["downCached"]["status"] == 200 and sw_run["downCached"]["body"].endswith("/static/style.css?v=42")
    assert sw_run["downCachedFetches"] == 0 and sw_run["downCachedMs"] < 1000
    assert sw_run["fresh"]["status"] == 200
    # 服务器挂住不回：等满 4 秒后给缓存（不无限转圈）
    assert sw_run["hang"]["status"] == 200 and sw_run["hang"]["body"].endswith("/static/style.css?v=42")
    assert sw_run["hangFetches"] == 1 and 3900 <= sw_run["hangMs"] < 8000


# ============================================================================ 证书

@pytest.fixture()
def tdir():
    """自建临时目录（本机 pytest 的 basetemp 目录无权限，不用内置 tmp_path）。"""
    d = Path(tempfile.mkdtemp(prefix="zb_tls_"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _load(folder):
    ca = x509.load_pem_x509_certificate((folder / "ca.pem").read_bytes())
    leaf = x509.load_pem_x509_certificate((folder / "cert.pem").read_bytes())
    return ca, leaf


def _san(cert):
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return set(san.get_values_for_type(x509.DNSName)), {str(i) for i in san.get_values_for_type(x509.IPAddress)}


def test_cert_ca_and_leaf(tdir):
    info = _self_signed(tdir / "tls", ips=["127.0.0.1", "192.168.50.7"])
    folder = tdir / "tls"
    assert info["key"] == folder / "key.pem" and info["cert"] == folder / "cert.pem" and info["ca"] == folder / "zhimou-ca.crt"
    assert info["ca_new"] is True
    ca, leaf = _load(folder)
    bc = ca.extensions.get_extension_for_class(x509.BasicConstraints)
    assert bc.critical and bc.value.ca is True
    ku = ca.extensions.get_extension_for_class(x509.KeyUsage).value
    assert ku.key_cert_sign and ku.crl_sign
    nc = ca.extensions.get_extension_for_class(x509.NameConstraints).value
    assert x509.DNSName("localhost") in nc.permitted_subtrees
    assert x509.IPAddress(ipaddress.ip_network("192.168.0.0/16")) in nc.permitted_subtrees
    # 导出给 Android 的 .crt 是 DER，与 ca.pem 是同一张
    assert x509.load_der_x509_certificate(info["ca"].read_bytes()) == ca
    # 叶子：SAN、签发链、用途
    dns, ips = _san(leaf)
    assert dns == {"localhost"} and ips == {"127.0.0.1", "192.168.50.7"}
    leaf.verify_directly_issued_by(ca)
    assert leaf.issuer == ca.subject
    assert leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False
    assert ExtendedKeyUsageOID.SERVER_AUTH in leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert leaf.not_valid_after_utc - leaf.not_valid_before_utc <= dt.timedelta(days=398)
    key = serialization.load_pem_private_key((folder / "key.pem").read_bytes(), None)
    assert key.public_key().public_numbers() == leaf.public_key().public_numbers()


def test_cert_reuse_and_ip_change_keeps_ca(tdir):
    folder = tdir / "tls"
    _self_signed(folder, ips=["192.168.50.7"])
    ca1, cert1 = (folder / "zhimou-ca.crt").read_bytes(), (folder / "cert.pem").read_bytes()
    again = _self_signed(folder, ips=["192.168.50.7"])
    assert again["ca_new"] is False and again["leaf_new"] is False
    assert (folder / "cert.pem").read_bytes() == cert1  # 什么都没变就原样复用
    moved = _self_signed(folder, ips=["10.0.0.23"])
    assert moved["ca_new"] is False and moved["leaf_new"] is True
    assert (folder / "zhimou-ca.crt").read_bytes() == ca1  # 换了 IP 只重签叶子，手机不用重装根证书
    ca, leaf = _load(folder)
    assert "10.0.0.23" in _san(leaf)[1]
    leaf.verify_directly_issued_by(ca)


def test_cert_drops_ip_outside_constraints(tdir):
    info = _self_signed(tdir / "tls", ips=["192.168.50.7", "8.8.8.8", "169.254.3.4"])
    _, leaf = _load(tdir / "tls")
    assert _san(leaf)[1] == {"127.0.0.1", "192.168.50.7"}
    assert set(info["skipped"]) == {"8.8.8.8", "169.254.3.4"}


def _old_self_signed(folder):
    """旧版 _self_signed 生成的证书：自签名、没有 CA:TRUE。"""
    folder.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "zhimou-local")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=825))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("192.168.50.7"))]), critical=False)
            .sign(key, hashes.SHA256()))
    (folder / "key.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    (folder / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert


def test_cert_replaces_old_self_signed(tdir):
    folder = tdir / "tls"
    old = _old_self_signed(folder)
    info = _self_signed(folder, ips=["192.168.50.7"])
    assert info["leaf_new"] is True
    ca, leaf = _load(folder)
    assert leaf != old
    leaf.verify_directly_issued_by(ca)
    # 旧证书留一份备份，可回退
    assert x509.load_pem_x509_certificate((folder / "cert.pem.bak").read_bytes()) == old
    assert (folder / "key.pem.bak").exists()


def test_cert_survives_garbage_files(tdir):
    folder = tdir / "tls"
    folder.mkdir(parents=True)
    for n in ("ca.pem", "ca.key.pem", "cert.pem", "key.pem"):
        (folder / n).write_text("坏文件", encoding="utf-8")
    info = _self_signed(folder, ips=["192.168.50.7"])
    assert info["ca_new"] is True and info["leaf_new"] is True
    ca, leaf = _load(folder)
    leaf.verify_directly_issued_by(ca)


def _handshake(info, server_hostname, cert=None, key=None):
    """用 OpenSSL 实际握手：服务端出示叶子证书，客户端只信任导出的根证书。"""
    srv = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    srv.load_cert_chain(str(cert or info["cert"]), str(key or info["key"]))
    lsock = socket.socket()
    lsock.bind(("127.0.0.1", 0))
    lsock.listen(1)
    port = lsock.getsockname()[1]

    def serve():
        conn, _ = lsock.accept()
        try:
            with srv.wrap_socket(conn, server_side=True) as s:
                s.sendall(b"ok")
        except (ssl.SSLError, OSError):
            pass

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    cli = ssl.create_default_context(cadata=info["ca"].read_bytes())
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=10) as raw, cli.wrap_socket(raw, server_hostname=server_hostname) as s:
            return s.recv(2)
    finally:
        t.join(5)
        lsock.close()


def test_cert_tls_handshake_trusts_only_ca(tdir):
    info = _self_signed(tdir / "tls", ips=["192.168.50.7"])
    assert _handshake(info, "localhost") == b"ok"
    assert _handshake(info, "127.0.0.1") == b"ok"
    # 根证书带名称约束：即使有人拿到根私钥签外网域名，装了根证书的手机也不认
    ca = x509.load_pem_x509_certificate((tdir / "tls" / "ca.pem").read_bytes())
    ca_key = serialization.load_pem_private_key((tdir / "tls" / "ca.key.pem").read_bytes(), None)
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = dt.datetime.now(dt.timezone.utc)
    evil = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "example.com")]))
            .issuer_name(ca.subject).public_key(k.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("example.com")]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    (tdir / "evil.pem").write_bytes(evil.public_bytes(serialization.Encoding.PEM))
    (tdir / "evil.key").write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    with pytest.raises(ssl.SSLCertVerificationError):
        _handshake(info, "example.com", cert=tdir / "evil.pem", key=tdir / "evil.key")


def test_serve_https_cli(monkeypatch, capsys):
    import uvicorn
    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: seen.update(kw))
    assert main(["serve", "--https", "--port", "8621"]) == 0
    tls = settings.data_dir / "tls"
    assert seen["ssl_certfile"] == str(tls / "cert.pem") and seen["ssl_keyfile"] == str(tls / "key.pem")
    assert seen["port"] == 8621
    out = capsys.readouterr().out
    assert str(tls / "zhimou-ca.crt") in out
    assert "DER" in out
    assert "设置 → 安全 → 加密与凭据 → 安装证书 → CA 证书" in out
    assert "/offline-check" in out
