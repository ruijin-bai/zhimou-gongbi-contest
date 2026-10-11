"""命令行：python -m app init | serve | adduser | backtest"""
from __future__ import annotations

import argparse
import getpass
import ipaddress
import json
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app", description="智眸工笔 B 版")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="初始化数据库、项目和管理员账号")
    p_init.add_argument("--project", required=True, help="项目名称")
    p_init.add_argument("--code", default="P1", help="项目编码")
    p_init.add_argument("--admin", default="admin")
    p_init.add_argument("--name", default="管理员")

    p_user = sub.add_parser("adduser", help="新增账号")
    p_user.add_argument("username")
    p_user.add_argument("--name", required=True)
    from .roles import ROLES
    p_user.add_argument("--role", choices=list(ROLES), default="tech")
    p_user.add_argument("--company", default="", help="分包单位名称（技术员、安全员）")
    p_user.add_argument("--scope", default="", help="技术员负责的 WBS/作业代码前缀，逗号分隔")
    p_user.add_argument("--project-code", default=None)

    p_serve = sub.add_parser("serve", help="启动服务")
    p_serve.add_argument("--host", default=None)
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.add_argument("--https", action="store_true", help="用本机根证书签发的证书启用 HTTPS（手机录音、离线打开需要；根证书在数据目录 tls/ 下，手机装一次）")

    p_roles = sub.add_parser("demo-roles", help="给已有仿真项目补齐各岗位演示账号（只新增缺失账号，不改其他数据）")
    p_roles.add_argument("--password", default="sim12345")

    p_sim = sub.add_parser("simulate", help="生成莱基疏港路仿真项目（独立项目，界面标注仿真）")
    p_sim.add_argument("--records", help="真实周产值记录 xlsx（周起、实际产值、计划产值）")
    p_sim.add_argument("--weeks", type=int, default=18)
    p_sim.add_argument("--reset", action="store_true", help="删除已有仿真项目后重建")
    p_sim.add_argument("--agents", action="store_true", help="生成后在仿真数据上运行各 Agent")
    p_sim.add_argument("--password", default="sim12345")

    sub.add_parser("check", help="检查交融大模型连通性（文本 + 多模态各一次，使用合成图片）")

    p_bt = sub.add_parser("backtest", help="用多期 P6 .xer 更新文件离线回测进度预警（不写库、不调用 AI）")
    p_bt.add_argument("files", nargs="+")
    p_bt.add_argument("--tolerance", type=float, default=0.0, help="判定“确实延误”的容差天数")
    p_bt.add_argument("--exclude", default="", help="剔除的期次（数据日期，逗号分隔），如疑似大范围重排的期次")
    p_bt.add_argument("--rows", action="store_true", help="输出逐条样本与误报/漏报作业的各期原始值")

    args = parser.parse_args(argv)

    if args.cmd == "serve":
        import uvicorn

        from .config import settings
        ssl = {}
        if args.https:
            tls = _self_signed(settings.data_dir / "tls")
            ssl = {"ssl_keyfile": str(tls["key"]), "ssl_certfile": str(tls["cert"])}
            _print_https(tls, args.port or settings.port)
        uvicorn.run("app.main:app", host=args.host or settings.host, port=args.port or settings.port, log_level="info", **ssl)
        return 0

    if args.cmd == "simulate":
        from . import sim
        if args.reset:
            print("已删除旧仿真项目" if sim.reset() else "没有旧仿真项目")
        records = None
        if args.records:
            records = _read_records(args.records)
            print(f"读取周产值记录 {len(records)} 周")
        res = sim.build(records, weeks=args.weeks, password=args.password)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        if args.agents:
            print(json.dumps(sim.run_agents(res["project_id"]), ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "demo-roles":
        from . import sim
        from .db import connect, init_db, one
        init_db()
        conn = connect()
        try:
            proj = one(conn, "SELECT id FROM projects WHERE code=?", (sim.SIM_CODE,))
            if not proj:
                print("没有仿真项目，请先运行 simulate", file=sys.stderr)
                return 2
            added = sim.ensure_role_accounts(conn, proj["id"], args.password)
            print("已新增账号：" + "、".join(added) if added else "各岗位演示账号已齐全")
        finally:
            conn.close()
        return 0

    if args.cmd == "check":
        return _check()

    if args.cmd == "backtest":
        from pathlib import Path

        from .services import backtest as bt
        try:
            snaps = bt.parse_files([(Path(f).name, Path(f).read_bytes()) for f in args.files])
            res = bt.run(snaps, tolerance_days=args.tolerance, exclude=bt.parse_exclude(args.exclude))
        except bt.BacktestError as exc:
            print(exc.msg, file=sys.stderr)
            return 2
        if not args.rows:
            for k in ("rows", "gap_rows", "history"):
                res.pop(k)
            res["lead_time"].pop("items")
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    from .db import connect, init_db, insert, now_iso, one
    from .security import hash_password
    init_db()
    conn = connect()
    try:
        if args.cmd == "init":
            proj = one(conn, "SELECT * FROM projects WHERE code=?", (args.code,))
            pid = proj["id"] if proj else insert(conn, "projects", {"name": args.project, "code": args.code, "config_json": "{}", "created_at": now_iso()})
            if one(conn, "SELECT 1 FROM users WHERE username=?", (args.admin,)):
                print(f"项目 {args.project} 已就绪；账号 {args.admin} 已存在")
                return 0
            pw = getpass.getpass(f"设置 {args.admin} 的密码（至少6位）：")
            if len(pw) < 6:
                print("密码太短", file=sys.stderr)
                return 2
            insert(conn, "users", {"username": args.admin, "name": args.name, "role": "admin", "password_hash": hash_password(pw),
                                   "project_id": pid, "wbs_scope": "", "email": "", "active": 1, "created_at": now_iso()})
            print(f"已创建项目「{args.project}」与管理员 {args.admin}")
            return 0
        if args.cmd == "adduser":
            proj = one(conn, "SELECT * FROM projects WHERE code=?", (args.project_code,)) if args.project_code else one(conn, "SELECT * FROM projects ORDER BY id LIMIT 1")
            if not proj:
                print("请先运行 init", file=sys.stderr)
                return 2
            pw = getpass.getpass(f"设置 {args.username} 的密码（至少6位）：")
            if len(pw) < 6:
                print("密码太短", file=sys.stderr)
                return 2
            insert(conn, "users", {"username": args.username, "name": args.name, "role": args.role, "password_hash": hash_password(pw),
                                   "project_id": proj["id"], "wbs_scope": args.scope, "email": "", "active": 1, "created_at": now_iso(),
                                   "company": args.company})
            print(f"已创建账号 {args.username}（{args.role}）")
            return 0
    finally:
        conn.close()
    return 1


def _read_records(path: str) -> list[dict]:
    """读取周产值记录 xlsx，复用产值记录导入的表头识别。"""
    from datetime import date as _date, timedelta as _td
    from pathlib import Path

    from .services.output import _REC_ALIASES, _num, _sheet_rows
    from .services.p6 import _d
    rows, m = _sheet_rows(Path(path).read_bytes(), _REC_ALIASES)
    out = []
    for r in rows:
        g = lambda k: r[m[k]] if k in m and m[k] < len(r) else None  # noqa: E731
        s = _d(g("start"))
        if not s or _num(g("actual")) is None:
            continue
        d = _date.fromisoformat(s)
        out.append({"week": (d - _td(days=d.weekday())).isoformat(), "actual": _num(g("actual")), "planned": _num(g("planned")),
                    "currency": str(g("currency") or "") or None})
    return out


# 本地根 CA 只用于内网：名称约束只放行 localhost 与内网 IP 段，即使根私钥外泄也签不出外网网站的可信证书
_TLS_NETS = tuple(ipaddress.ip_network(n) for n in ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10"))


def _lan_ips() -> list[str]:
    """本机 IPv4：首个是出网网卡（手机访问用它），其余来自主机名解析。"""
    import socket

    ips: list[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            if info[4][0] not in ips:
                ips.append(info[4][0])
    except OSError:
        pass
    return ips


def _self_signed(folder, ips=None) -> dict:
    """本地根 CA + 由它签发的叶子证书（HTTPS 局域网访问；手机装一次根证书后浏览器不再报错，Service Worker 才能注册）。

    folder 下的文件：ca.key.pem / ca.pem（根证书私钥与证书）、zhimou-ca.crt（同一张根证书的 DER 格式，给手机安装）、
    key.pem / cert.pem（叶子，uvicorn 使用）。根证书已存在就沿用（手机不用重装）；叶子在缺失、不是这张根证书签发、
    IP 不全或 30 天内到期时重签。旧版自签名证书替换前备份为 *.bak。
    返回 key、cert、ca（DER）、ca_pem、ips（叶子含的 IP）、skipped（非内网未纳入的 IP）、ca_new、leaf_new。
    """
    import datetime as dt
    import os
    import socket

    from cryptography import x509
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    folder.mkdir(parents=True, exist_ok=True)
    ca_key_p, ca_pem_p, ca_der_p = folder / "ca.key.pem", folder / "ca.pem", folder / "zhimou-ca.crt"
    key_p, cert_p = folder / "key.pem", folder / "cert.pem"
    now = dt.datetime.now(dt.timezone.utc)

    want, skipped = ["127.0.0.1"], []
    for ip in (_lan_ips() if ips is None else ips):
        addr = ipaddress.ip_address(ip)
        if str(addr) in want:
            continue
        if addr.version == 4 and any(addr in n for n in _TLS_NETS):
            want.append(str(addr))
        else:
            skipped.append(str(addr))

    def _write_key(path, key):
        path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def _load(cert_path, key_path):
        try:
            return (x509.load_pem_x509_certificate(cert_path.read_bytes()),
                    serialization.load_pem_private_key(key_path.read_bytes(), None))
        except (OSError, ValueError, TypeError):
            return None, None

    def _same_key(cert, key):
        return cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo) == \
            key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)

    ca, ca_key = _load(ca_pem_p, ca_key_p)
    try:
        ok_ca = (ca is not None and ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
                 and ca.not_valid_after_utc > now + dt.timedelta(days=30) and _same_key(ca, ca_key))
    except x509.ExtensionNotFound:
        ok_ca = False
    ca_new = not ok_ca
    if ca_new:
        ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        ca_name = x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "智眸工笔（本机）"),
                             x509.NameAttribute(NameOID.COMMON_NAME, f"智眸工笔 本地根证书 {socket.gethostname()}"[:64])])
        permitted = [x509.DNSName("localhost")] + [x509.IPAddress(n) for n in _TLS_NETS]
        ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
              .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=3650))
              .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
              .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False, data_encipherment=False,
                                           key_agreement=False, key_cert_sign=True, crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
              .add_extension(x509.NameConstraints(permitted_subtrees=permitted, excluded_subtrees=None), critical=False)
              .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
              .sign(ca_key, hashes.SHA256()))
        _write_key(ca_key_p, ca_key)
        ca_pem_p.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    ca_der = ca.public_bytes(serialization.Encoding.DER)
    if not ca_der_p.exists() or ca_der_p.read_bytes() != ca_der:
        ca_der_p.write_bytes(ca_der)

    leaf, leaf_key = _load(cert_p, key_p)
    fresh = False
    if leaf is not None and not ca_new:
        try:
            leaf.verify_directly_issued_by(ca)
            san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            fresh = (set(want) <= {str(i) for i in san.get_values_for_type(x509.IPAddress)}
                     and "localhost" in san.get_values_for_type(x509.DNSName)
                     and leaf.not_valid_after_utc > now + dt.timedelta(days=30) and _same_key(leaf, leaf_key))
        except (ValueError, TypeError, InvalidSignature, x509.ExtensionNotFound):
            fresh = False
    if not fresh:
        if leaf is not None and leaf.issuer != ca.subject:  # 旧版自签名证书：留一份备份，可回退
            cert_p.replace(folder / "cert.pem.bak")
            key_p.replace(folder / "key.pem.bak")
        leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        san = x509.SubjectAlternativeName([x509.DNSName("localhost")] + [x509.IPAddress(ipaddress.ip_address(i)) for i in want])
        leaf = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
                .issuer_name(ca.subject).public_key(leaf_key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=397))
                .add_extension(san, critical=False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=True, data_encipherment=False,
                                             key_agreement=False, key_cert_sign=False, crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(leaf_key.public_key()), critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca.public_key()), critical=False)
                .sign(ca_key, hashes.SHA256()))
        _write_key(key_p, leaf_key)
        cert_p.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return {"key": key_p, "cert": cert_p, "ca": ca_der_p, "ca_pem": ca_pem_p, "ca_new": ca_new, "leaf_new": not fresh,
            "ips": [str(i) for i in san.get_values_for_type(x509.IPAddress)], "skipped": skipped}


def _print_https(info, port: int) -> None:
    lan = [i for i in info["ips"] if i != "127.0.0.1"]
    print("HTTPS 已启用：证书由本机根证书签发，覆盖 localhost、" + "、".join(info["ips"]))
    if info["skipped"]:
        print("  这些地址不是内网地址，证书未包含：" + "、".join(info["skipped"]))
    if lan:
        print(f"  手机访问：https://{lan[0]}:{port}/    离线实测自检页：https://{lan[0]}:{port}/offline-check")
    else:
        print("  没找到局域网 IP：手机连上同一 Wi-Fi 后重新启动")
    print(f"  根证书（DER 格式，Android 可直接安装）：{info['ca']}")
    print("  Android 安装一次：把该文件传到手机，或在手机浏览器打开 https://电脑IP:端口/tls/zhimou-ca.crt 下载，然后")
    print("    设置 → 安全 → 加密与凭据 → 安装证书 → CA 证书 → 选这个文件（各品牌菜单名略有不同，可在设置里搜“CA 证书”）")
    if info["ca_new"]:
        print("  根证书是本次新生成的：手机上装过旧根证书的，需要改装这一张")
    elif info["leaf_new"]:
        print("  已按当前 IP 重签服务器证书；根证书没变，手机不用重装")
    sys.stdout.flush()  # 输出重定向到日志时也立即可见（uvicorn 随后接管进程）


def _check() -> int:
    """用一张程序生成的色块图验证多模态通道，不涉及任何业务数据。"""
    import tempfile

    from PIL import Image, ImageDraw

    from .ai.client import AIError, call_json, provider_status
    from .db import init_db
    init_db()
    st = provider_status()
    print(f"模型：{st['label']} 视觉={st['vision_model']} 文本={st['text_model']} 就绪={st['ready']} {st.get('reason', '')}")
    if not st["ready"]:
        return 2
    ok = True
    try:
        r = call_json("ai.check.text", system="只输出JSON。", user='输出 {"ok": true}', prompt_version="check",
                      mock=lambda: {"ok": True}, max_tokens=50)
        print(f"文本通道：成功 {r.latency_ms}ms → {r.data}")
    except AIError as exc:
        ok = False
        print(f"文本通道：失败 {exc}")
    with tempfile.TemporaryDirectory() as d:
        path = f"{d}/check.jpg"
        img = Image.new("RGB", (320, 240), (200, 40, 40))
        ImageDraw.Draw(img).rectangle([160, 0, 320, 240], fill=(40, 60, 200))
        img.save(path, "JPEG")
        try:
            r = call_json("ai.check.vision", system="只输出JSON。", user='图片左右两半分别是什么颜色？输出 {"left": "颜色", "right": "颜色"}',
                          prompt_version="check", images=[path], mock=lambda: {"left": "红", "right": "蓝"}, max_tokens=80)
            print(f"多模态通道：成功 {r.latency_ms}ms → {r.data}")
        except AIError as exc:
            ok = False
            print(f"多模态通道：失败 {exc}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
