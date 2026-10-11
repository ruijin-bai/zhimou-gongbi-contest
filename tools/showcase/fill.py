"""补齐脚本：回放跑完、fix 纠正之后，用系统自身接口把回放没覆盖的页面补出数据。

覆盖：周报/月报（重新生成并发布）、英文周月报、索赔意向通知与延误分析、工作联系单与致分包函（含签收、回复、销项）、
例会底稿、三周滚动计划、月度复盘、预控提醒、讨论组、私信、公告、试块与复试、整改照片、隐蔽验收补拍、历史回测。
需要“AI 文字”的地方由 author/texts 仿真撰写（不调用任何外部模型）。每一步按业务发生的日期时间执行（time_machine），
报表、文书按当时的 P6 期次与预测生成（as_of），不用未来数据。

用法（先停掉连同一数据目录的服务）：
  python -m tools.showcase.fill --data-dir data_showcase [--day 2026-10-04] [--only reports,letters]
固定随机种子；每一步先查已有数量，够了就跳过（reports 例外：每次重建）。所有内容都是虚构的仿真内容，不写任何实测成果。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import sys
import traceback
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SEED = 20261011
LOG: list[str] = []
SUPERVISOR = "Engr. Okafor（监理）"


def say(*a):
    s = " ".join(str(x) for x in a)
    LOG.append(s)
    print(datetime.now().strftime("%H:%M:%S"), s, flush=True)


class Ctx:
    def __init__(self, day: str):
        from fastapi.testclient import TestClient
        from app import main as M
        from app.db import connect
        self.M, self.TC = M, TestClient
        self.day = day
        self.rng = random.Random(SEED)
        conn = connect()
        try:
            self.pid = conn.execute("SELECT id FROM projects ORDER BY id LIMIT 1").fetchone()["id"]
            self.users = {r["username"]: r["id"] for r in conn.execute("SELECT id, username FROM users WHERE project_id=?", (self.pid,)).fetchall()}
        finally:
            conn.close()
        self.sess: dict[str, object] = {}

    def who(self, key: str):
        u = key if key.startswith("sim_") else "sim_" + key
        if u not in self.sess:
            c = self.TC(self.M.app, raise_server_exceptions=False, headers={"x-zb": "1"})
            c.post("/api/dev/login", json={"user_id": self.users[u]})
            self.sess[u] = c
        return self.sess[u]

    def call(self, key, method, path, ok=(200,), **kw):
        r = self.who(key).request(method, path, **kw)
        if r.status_code == 401:   # 按业务时间来回穿越，会话可能已过期：按当时时间重新登录一次
            self.sess.pop(key if key.startswith("sim_") else "sim_" + key, None)
            r = self.who(key).request(method, path, **kw)
        try:
            body = r.json()
        except Exception:
            body = r.text[:300]
        if r.status_code not in ok:
            raise RuntimeError(f"{key} {method} {path} -> {r.status_code} {str(body)[:300]}")
        return body

    def q(self, sql, *a):
        from app.db import connect
        conn = connect()
        try:
            return conn.execute(sql, a).fetchall()
        finally:
            conn.close()

    def x(self, sql, *a):
        from app.config import settings
        conn = sqlite3.connect(settings.db_path, isolation_level=None)
        try:
            return conn.execute(sql, a).rowcount
        finally:
            conn.close()


def at(dt: str):
    import time_machine
    return time_machine.travel(datetime.fromisoformat(dt).astimezone(), tick=False)


@contextmanager
def as_of(c: Ctx, when: str):
    """按 when 当时的 P6 期次与进度预测生成：临时把当期计划设为 current、把之后的预测藏起来，结束后复原。"""
    day = when[:10]
    old = [r["id"] for r in c.q("SELECT id FROM p6_imports WHERE project_id=? AND is_current=1", c.pid)]
    tgt = c.q("SELECT id FROM p6_imports WHERE project_id=? AND data_date<=? ORDER BY data_date DESC, id DESC LIMIT 1", c.pid, day)
    if tgt:
        c.x("UPDATE p6_imports SET is_current=0 WHERE project_id=?", c.pid)
        c.x("UPDATE p6_imports SET is_current=1 WHERE id=?", tgt[0]["id"])
    hide = (("forecasts", "created_at"), ("findings", "created_at"), ("sub_letters", "issued_at"), ("alerts", "first_detected"))
    for t, col in hide:
        c.x(f"UPDATE {t} SET project_id=-project_id WHERE project_id=? AND {col}>?", c.pid, when)
    try:
        with at(when):
            yield
    finally:
        for t, _ in hide:
            c.x(f"UPDATE {t} SET project_id=-project_id WHERE project_id=?", -c.pid)
        c.x("UPDATE p6_imports SET is_current=0 WHERE project_id=?", c.pid)
        for i in old:
            c.x("UPDATE p6_imports SET is_current=1 WHERE id=?", i)


def safe(name, fn, *a):
    try:
        fn(*a)
        say("✓", name)
    except Exception as e:  # noqa: BLE001
        say("✗", name, str(e)[:300])
        LOG.append(traceback.format_exc()[-1200:])


def week_starts(day: str) -> list[str]:
    d = date.fromisoformat("2026-09-07")
    end = date.fromisoformat(day)
    out = []
    while d + timedelta(days=6) < end:
        out.append(d.isoformat())
        d += timedelta(days=7)
    return out


def _id(d):
    return d.get("id") or (d.get("doc") or {}).get("id")


def gen_doc(c: Ctx, who, kind, ref, when, confirm_by="pm", confirm_at=None):
    if c.q("SELECT 1 FROM gongbi_docs WHERE project_id=? AND kind=? AND ref=? AND status='confirmed'", c.pid, kind, ref or ""):
        return None
    with as_of(c, when):
        d = c.call(who, "POST", "/api/gongbi/docs", json={"kind": kind, "ref": ref}, ok=(200, 400))
    did = _id(d)
    if not did:
        raise RuntimeError(f"起草失败 {kind} {ref}: {str(d)[:200]}")
    with at(confirm_at or (datetime.fromisoformat(when) + timedelta(hours=2)).isoformat()):
        c.call(confirm_by, "PUT", f"/api/gongbi/docs/{did}", json={"narrative": {}, "confirm": True})
    return did


# ----------------------------------------------------------------------------- 整改照片、隐蔽验收补拍

VERIFY_REASON = {"机械车辆": "整改后照片中挖掘机作业半径内已无人员，作业区设置了警戒隔离和专人指挥。",
                 "起重吊装": "整改后照片显示吊物下方已清场，设置警戒线，指挥人员站位在回转半径以外。",
                 "消防动火": "整改后照片中气瓶已直立放置在专用气瓶架内并加防倾倒链条，远离沟槽，标识清楚。",
                 "临边洞口": "整改后照片显示临边已设置防护栏杆和警示标识，同一位置。",
                 "高处作业": "整改后照片显示钢筋笼顶部作业人员已系挂安全带，增设了操作平台。"}


def hazard_photos(c: Ctx):
    from tools.sim_replay.watermark import stamp
    from tools.showcase.author import writer
    from tools.showcase.rename import NEW_NAME

    @writer("safety.verify")
    def _v(f, base, meta, user):
        row = c.q("SELECT title FROM findings WHERE id=?", meta.get("finding"))
        cat = (row[0]["title"].split("：")[0] if row else "")
        return {"resolved": True, "same_scene": True, "reason": VERIFY_REASON.get(cat, "整改后照片显示原隐患已消除，同一位置。"), "remaining": ""}

    photos = ROOT / "sim_assets" / "photos"
    plan = [  # (照片, 隐患, 整改人, 整改时间, 安全主管复查时间；None=留给演示日待复查)
        ("R08", 14, "zhao", "2026-09-24T10:20:00", "2026-09-24T15:10:00"),
        ("R06", 85, "qian", "2026-09-26T09:50:00", "2026-09-26T16:30:00"),
        ("R12", 167, "sun", "2026-10-04T15:40:00", None),
    ]
    for code, fid, who, t1, t2 in plan:
        f = c.q("SELECT status, title FROM findings WHERE id=?", fid)
        if not f or f[0]["status"] not in ("open", "rejected"):
            continue
        with at(t1):
            raw = stamp(photos / f"{code}.jpg", datetime.fromisoformat(t1), "", "隐患整改完成", NEW_NAME)
            r = c.call(who, "POST", "/api/media", files=[("files", (f"{code}.jpg", raw, "image/jpeg"))],
                       data={"purpose": "rectify", "scan": "0", "taken_at": [t1], "place": [""]})
            mid = int(((r.get("items") if isinstance(r, dict) else r) or [{}])[0]["id"])
            c.call(who, "POST", f"/api/findings/{fid}/rectify", json={"media_id": mid})
        if t2:
            with at(t2):
                c.call("safety", "POST", f"/api/findings/{fid}/status", json={"status": "resolved", "note": "现场复查：整改到位，同意闭环"})
        say("  整改照片", code, "→ S", fid)
    # 已闭环但当时没上传整改照片的，补挂整改照片（只补照片，不改闭环结论）
    for code, fid in (("R02", 48), ("R07", 36)):
        f = c.q("SELECT status, rectify_media_id, resolved_at, assignee_id FROM findings WHERE id=?", fid)
        if not f or f[0]["rectify_media_id"] or f[0]["status"] != "resolved":
            continue
        when = f[0]["resolved_at"][:19]
        uname = {v: k for k, v in c.users.items()}[f[0]["assignee_id"]]
        with at(when):
            raw = stamp(photos / f"{code}.jpg", datetime.fromisoformat(when), "", "隐患整改完成", NEW_NAME)
            r = c.call(uname, "POST", "/api/media", files=[("files", (f"{code}.jpg", raw, "image/jpeg"))],
                       data={"purpose": "rectify", "scan": "0", "taken_at": [when], "place": [""]})
        mid = int(((r.get("items") if isinstance(r, dict) else r) or [{}])[0]["id"])
        c.x("UPDATE findings SET rectify_media_id=? WHERE id=?", mid, fid)
        say("  补挂整改照片", code, "→ S", fid)
    # 早先由日志上报、雨后已处理的“积水/泥泞”类一般问题：复查闭环（只处理 09-28 之前的，近期的保留在台账）
    for f in c.q("""SELECT id, created_at FROM findings WHERE project_id=? AND agent='safety' AND status='open' AND IFNULL(level,'')<>'high'
                    AND created_at<'2026-09-28' AND confirmed_at IS NOT NULL""", c.pid):
        t = (datetime.fromisoformat(f["created_at"][:19]) + timedelta(days=1, hours=2)).isoformat()
        with at(t):
            c.call("safety", "POST", f"/api/findings/{f['id']}/status", json={"status": "resolved", "note": "雨后已排水、铺设防滑垫并设警示，复查通过"}, ok=(200, 400))


ON = {"h10": False}


def h10(c: Ctx):
    """H10（钢筋加工场氧气乙炔瓶混放、暴晒）：回放时照片识别漏判、未派单，整改照片 R10 一直没处挂。
    按真实流程补：安全主管对该照片重新识别 → 确认派单 → 责任人上传 R10 整改 → 复查闭环。"""
    m = c.q("SELECT id, safety_status FROM media WHERE project_id=? AND original_name='H10.jpg'", c.pid)
    if not m or c.q("SELECT 1 FROM findings WHERE ref_type='media' AND ref_id=?", m[0]["id"]):
        return
    mid = m[0]["id"]
    from tools.showcase.author import writer

    @writer("safety.scan")
    def _scan(f, base, meta, user):
        out = []
        for ph in (base.get("photos") or []):
            hz = []
            if ON["h10"]:
                hz = [{"category": "消防动火", "desc": "氧气瓶与乙炔瓶混放在一起，间距不足 5 米，且在阳光下暴晒，无遮阳和防倾倒措施", "severity": "高",
                       "measure": "氧气瓶、乙炔瓶分开存放，间距不小于 5 米，设遮阳棚并加防倾倒链条", "basis": "气瓶存放与使用安全要求"}]
            out.append({"photo": ph["photo"], "scene": "钢筋加工场气瓶存放点", "hazards": hz})
        return {"photos": out}

    c.x("UPDATE media SET safety_status='pending' WHERE id=?", mid)
    ON["h10"] = True
    try:
        with at("2026-09-30T09:40:00"):
            c.call("safety", "POST", "/api/safety/rescan", json={})
    finally:
        ON["h10"] = False
    c.q("SELECT 1")
    cand = c.q("SELECT id FROM findings WHERE ref_type='media' AND ref_id=? AND confirmed_at IS NULL ORDER BY id", mid)
    if not cand:
        say("  H10 重新识别未产生候选")
        return
    fid = cand[0]["id"]
    with at("2026-09-30T10:20:00"):
        c.call("safety", "POST", f"/api/findings/{fid}/confirm", json={"assignee_id": c.users["sim_qian"], "due_date": "2026-09-30"})
    from tools.sim_replay.watermark import stamp
    from tools.showcase.rename import NEW_NAME
    with at("2026-09-30T15:30:00"):
        raw = stamp(ROOT / "sim_assets" / "photos" / "R10.jpg", datetime(2026, 9, 30, 15, 30), "钢筋加工场", "隐患整改完成", NEW_NAME)
        r = c.call("qian", "POST", "/api/media", files=[("files", ("R10.jpg", raw, "image/jpeg"))],
                   data={"purpose": "rectify", "scan": "0", "taken_at": ["2026-09-30T15:30:00"], "place": ["钢筋加工场"]})
        rid = int(((r.get("items") if isinstance(r, dict) else r) or [{}])[0]["id"])
        c.call("qian", "POST", f"/api/findings/{fid}/rectify", json={"media_id": rid})
    with at("2026-09-30T17:10:00"):
        c.call("safety", "POST", f"/api/findings/{fid}/status", json={"status": "resolved", "note": "现场复查：气瓶已分开存放并搭设遮阳棚，同意闭环"})
    say("  H10 → S", fid)


def qr(c: Ctx):
    """甲分包安全员也给钢筋加工场生成一个扫码上报二维码（乙分包已有拌合站发电机房的二维码）。"""
    if c.q("SELECT 1 FROM quick_tokens WHERE project_id=? AND created_by=?", c.pid, c.users["sim_safe_a"]):
        return
    with at("2026-09-20T15:20:00"):
        c.call("safe_a", "POST", "/api/quick/qrs", json={"label": "钢筋加工场", "days": 60})


def conceal(c: Ctx):
    from tools.sim_replay.watermark import stamp
    from tools.showcase.rename import NEW_NAME
    r = c.q("SELECT status, photos_json FROM conceal_records WHERE id=1")
    if not r or r[0]["status"] != "draft":
        return
    photos = ROOT / "sim_assets" / "photos"
    for code, shot, hm in (("C11", "position", "10:05"), ("C12", "diameter", "11:20"), ("C13", "sediment", "13:40")):
        taken = f"2026-09-17T{hm}:00"
        with at("2026-09-18T09:30:00"):
            raw = stamp(photos / f"{code}.jpg", datetime.fromisoformat(taken), "立交桥 1#墩", {"position": "1#桩桩位复核", "diameter": "1#桩孔径检测", "sediment": "1#桩沉渣厚度检测"}[shot], NEW_NAME)
            c.call("qian", "POST", "/api/conceal/1/photos", files=[("files", (f"{code}.jpg", raw, "image/jpeg"))],
                   data={"shot": shot, "taken_at": [taken], "place": ["立交桥 1#墩"]})
    with at("2026-09-18T09:50:00"):
        c.call("qian", "POST", "/api/conceal/1/ready", json={}, ok=(200, 400))
        c.call("qian", "POST", "/api/conceal/1/submit", json={"supervisor_name": SUPERVISOR}, ok=(200, 400))
    with at("2026-09-18T16:20:00"):
        c.call("eng", "POST", "/api/conceal/1/result", json={"result": "passed", "note": "桩位、孔深、孔径、沉渣与钢筋笼均符合设计，监理现场签认", "date": "2026-09-18"}, ok=(200, 400))


# ----------------------------------------------------------------------------- 报表与文书

def _publish(c: Ctx, rid, when):
    with at(when):
        rep = c.call("pm", "GET", f"/api/reports/{rid}")
        body = rep.get("report") or rep
        checks = body.get("number_check") or []
        c.call("pm", "PUT", f"/api/reports/{rid}", json={"narrative": body.get("narrative") or {}, "status": "published",
                                                         "verified": [{"field": x["field"], "number": x["number"]} for x in checks if x.get("number")]})


def reports(c: Ctx):
    """周报（每个完整周）与 9 月月报：按 fix 纠正后的数据重新生成、按当时的 P6 期次撰写并发布（旧稿删除）。"""
    c.x("DELETE FROM knowledge WHERE project_id=? AND kind='report_kpi10'", c.pid)
    c.x("DELETE FROM reports WHERE project_id=?", c.pid)
    for ws in week_starts(c.day):
        mon = (date.fromisoformat(ws) + timedelta(days=7)).isoformat()
        with as_of(c, f"{mon}T09:40:00"):
            rid = c.call("eng", "POST", "/api/reports", json={"kind": "week", "ref_date": ws}).get("id")
        _publish(c, rid, f"{mon}T15:30:00")
    with as_of(c, "2026-10-02T10:00:00"):
        rid = c.call("eng", "POST", "/api/reports", json={"kind": "month", "ref_date": "2026-09-01"}).get("id")
    _publish(c, rid, "2026-10-02T16:00:00")


def english_reports(c: Ctx):
    for ws in week_starts(c.day):
        mon = (date.fromisoformat(ws) + timedelta(days=7)).isoformat()
        gen_doc(c, "eng", "weekly_en", ws, f"{mon}T11:00:00", confirm_at=f"{mon}T16:10:00")
    gen_doc(c, "eng", "monthly_en", "2026-09-01", "2026-10-02T11:00:00", confirm_at="2026-10-02T16:30:00")


def claims(c: Ctx):
    """降雨类事件发索赔意向通知（登记已发出）；设备故障属承包商自身风险，只做延误分析并登记不发。"""
    with at("2026-10-04T19:00:00"):
        refs = c.call("eng", "GET", "/api/gongbi/refs?kind=notice_of_claim")
    refs = refs if isinstance(refs, list) else refs.get("items", [])
    say("索赔事件数", len(refs))
    sent = []
    for ev in refs[:8]:
        k = ev["key"]
        cat = k.split("-")[0]
        d_to = date.fromisoformat(k.split("-", 1)[1][:10])
        t0 = f"{min(d_to + timedelta(days=1), date(2026, 10, 4))}T10:30:00"
        gen_doc(c, "eng", "delay_brief", k, t0, confirm_by="eng")
        if cat == "weather":
            lid = gen_doc(c, "eng", "notice_of_claim", k, t0.replace("10:30", "11:15"))
            with at(t0.replace("10:30", "17:00")):
                c.call("pm", "PUT", "/api/claims/notices", json={"key": k, "status": "sent", "reason": "已按合同通知条款向 Engineer 发出意向通知", "letter_id": lid}, ok=(200, 400))
            sent.append(k)
        else:
            with at(t0.replace("10:30", "17:00")):
                c.call("pm", "PUT", "/api/claims/notices", json={"key": k, "status": "waived", "reason": "设备故障属承包商自身风险，经评估不提索赔意向"}, ok=(200, 400))
    if sent and not c.q("SELECT 1 FROM claim_packs WHERE project_id=?", c.pid):
        with at("2026-10-03T15:00:00"):
            c.call("eng", "POST", "/api/claims/packs", json={"keys": sent[:4], "title": "9 月降雨停工事件索赔证据包（仿真）"})


REPLY = {"safety": "已按要求整改：现场已清理并设置警戒和防护，整改后照片已上传，请复查。",
         "quality": "已组织班组返工并自检合格，自检记录与照片已上传，请复查。",
         "progress": "已增派一台设备并延长作业时间，雨后加班赶工，计划按期追回滞后。"}


def _src_time(c: Ctx, key: str) -> datetime:
    typ, _, sid = key.partition(":")
    if typ == "finding":
        r = c.q("SELECT created_at FROM findings WHERE id=?", int(sid))
    elif typ == "alert":
        r = c.q("SELECT first_detected created_at FROM alerts WHERE id=?", int(sid))
    else:
        r = []
    t = datetime.fromisoformat(r[0]["created_at"][:19]) if r else datetime(2026, 10, 3, 9, 0)
    return max(t + timedelta(hours=2), datetime(2026, 9, 8, 9, 0))


def letters(c: Ctx):
    """工作联系单（安全/进度）与致分包函：起草确认即下发，分包签收、回复，主管复核销项；留一份未签收、一份退回重答。"""
    made = 0
    for who in ("safety", "eng"):
        with at("2026-10-04T19:00:00"):
            refs = c.call(who, "GET", "/api/gongbi/refs?kind=work_contact")
        refs = refs if isinstance(refs, list) else refs.get("items", [])
        for ref in refs[:5]:
            t = _src_time(c, ref["key"])
            if t > datetime(2026, 10, 4, 17, 0):
                t = datetime(2026, 10, 4, 17, 0)
            try:
                made += 1 if gen_doc(c, who, "work_contact", ref["key"], t.isoformat(), confirm_by=who, confirm_at=(t + timedelta(minutes=40)).isoformat()) else 0
            except Exception as e:  # noqa: BLE001
                say("  联系单失败", ref["key"], str(e)[:160])
        with at("2026-10-03T09:00:00"):
            refs = c.call(who, "GET", "/api/gongbi/refs?kind=letter_to_sub")
        refs = refs if isinstance(refs, list) else refs.get("items", [])
        for ref in refs[:2]:
            try:
                made += 1 if gen_doc(c, who, "letter_to_sub", ref["key"], "2026-10-03T09:30:00", confirm_at="2026-10-03T14:00:00") else 0
            except Exception as e:  # noqa: BLE001
                say("  致分包函失败", ref["key"], str(e)[:160])
    say("新发函件", made)
    ack_letters(c)


def ack_letters(c: Ctx, kinds=("work_contact", "letter_to_sub", "precontrol_notice")):
    """签收 → 进度类回复 → 工程主管复核（隔一份退回重答）；安全类按整改单闭环，不走回复。演示日 17 点后下发的留作“待签收”。"""
    rows = c.q(f"""SELECT id, kind, area, to_user_ids, status, issued_at, due_date FROM sub_letters WHERE project_id=? AND status='issued'
                   AND kind IN ({','.join('?' * len(kinds))}) ORDER BY id""", c.pid, *kinds)
    uname = {v: k for k, v in c.users.items()}
    for i, r in enumerate(rows):
        to = json.loads(r["to_user_ids"] or "[]")
        recv = uname.get(to[0]) if to else None
        if not recv or r["issued_at"] >= "2026-10-04T17":
            continue
        t0 = datetime.fromisoformat(r["issued_at"][:19])
        try:
            with at((t0 + timedelta(hours=1, minutes=15)).isoformat()):
                c.call(recv, "POST", f"/api/gongbi/letters/{r['id']}/ack")
            if r["area"] != "progress":
                continue
            t1 = datetime.combine(t0.date() + timedelta(days=1), datetime.min.time()).replace(hour=10, minute=30)
            if t1 > datetime(2026, 10, 4, 18, 0):
                continue
            with at(t1.isoformat()):
                c.call(recv, "POST", f"/api/gongbi/letters/{r['id']}/reply", json={"text": REPLY["progress"], "finish_date": (t1.date() + timedelta(days=5)).isoformat()})
            t2 = t1 + timedelta(hours=4)
            with at(t2.isoformat()):
                if i % 3 == 1:
                    c.call("eng", "POST", f"/api/gongbi/letters/{r['id']}/review", json={"accept": False, "reason": "措施未写明每日完成量和完成时间节点，请补充后再回复"})
                    t3 = t2 + timedelta(hours=20)
                    if t3 < datetime(2026, 10, 4, 18, 0):
                        with at(t3.isoformat()):
                            c.call(recv, "POST", f"/api/gongbi/letters/{r['id']}/reply", json={"text": "补充：增派一台设备，每日两班作业，按每天完成量对表，预计 5 天内追回滞后。", "finish_date": (t3.date() + timedelta(days=5)).isoformat()})
                        with at((t3 + timedelta(hours=3)).isoformat()):
                            c.call("eng", "POST", f"/api/gongbi/letters/{r['id']}/review", json={"accept": True, "reason": ""})
                else:
                    c.call("eng", "POST", f"/api/gongbi/letters/{r['id']}/review", json={"accept": True, "reason": ""})
        except Exception as e:  # noqa: BLE001
            say("  函件处理失败", r["id"], str(e)[:160])


MEETING_ITEMS = {  # 例会纪要里的行动项：(内容, 责任人, 期限, 完成时间或 None)
    1: [("清表推进到 K1+200", "zhao", "2026-09-18", "2026-09-18T17:20:00"), ("1#桩周三前开钻，钢筋笼加工同步", "qian", "2026-09-16", "2026-09-17T09:10:00"),
        ("首批钢筋见证取样送检", "qian", "2026-09-18", "2026-09-18T15:00:00"), ("组织一次全员安全交底", "safe_a", "2026-09-18", "2026-09-17T07:40:00"),
        ("拌合站设备安装调试", "li", "2026-09-25", "2026-09-25T16:30:00")],
    2: [("报增加一台旋挖钻的方案", "qian", "2026-09-24", "2026-09-24T11:00:00"), ("1#盖板涵基坑临边防护与堆土管理", "sun", "2026-09-23", "2026-09-23T17:00:00"),
        ("组织雨季排水与防雨措施检查", "eng", "2026-09-23", "2026-09-23T16:00:00"), ("泥浆池围挡整改盯到位", "safe_a", "2026-09-22", "2026-09-24T10:00:00")],
    3: [("第二台旋挖钻进场，10 月 5 日前双机正常作业", "qian", "2026-10-05", None), ("软基换填开工，压实度每层自检", "zhao", "2026-09-29", "2026-09-30T18:00:00"),
        ("2#圆管涵管节安装", "sun", "2026-09-29", "2026-09-29T17:30:00"), ("独立日节前安全检查", "safety", "2026-09-30", "2026-09-30T16:00:00"),
        ("周报附桩基赶工措施", "eng", "2026-10-02", "2026-10-02T15:00:00")],
}


def meeting_items(c: Ctx):
    if c.q("SELECT COUNT(*) n FROM meeting_items WHERE project_id=?", c.pid)[0]["n"]:
        return
    for mid, items in MEETING_ITEMS.items():
        m = c.q("SELECT meeting_date FROM meetings WHERE id=? AND project_id=?", mid, c.pid)
        if not m:
            continue
        d = m[0]["meeting_date"]
        ids = []
        with at(f"{d}T11:05:00"):
            for k, (text, who, due, _done) in enumerate(items):
                r = c.call("pm", "POST", f"/api/meetings/{mid}/items", json={"idx": k, "text": text, "owner_id": c.users["sim_" + who], "due_date": due})
                ids.append(r.get("id") or (r.get("item") or {}).get("id"))
            c.call("pm", "POST", f"/api/meetings/{mid}/save", ok=(200, 400))
        for iid, (text, who, due, done) in zip(ids, items):
            if iid and done:
                with at(done):
                    c.call(who, "POST", f"/api/meetings/items/{iid}/done", json={"done": True}, ok=(200, 400, 403))


def more_docs(c: Ctx):
    """例会底稿（09-28 会前；10-05 周一例会的底稿 10-04 傍晚备好）、三周滚动计划（09-28；10-04 编下周起）、9 月月度复盘。"""
    gen_doc(c, "eng", "meeting_pack", "2026-09-28", "2026-09-28T08:50:00", confirm_by="eng", confirm_at="2026-09-28T09:40:00")
    gen_doc(c, "eng", "meeting_pack", "2026-10-05", "2026-10-04T18:00:00", confirm_by="eng", confirm_at="2026-10-04T18:40:00")
    gen_doc(c, "eng", "three_week_plan", "2026-09-28", "2026-09-28T14:00:00", confirm_by="pm", confirm_at="2026-09-28T17:30:00")
    gen_doc(c, "eng", "three_week_plan", "2026-10-05", "2026-10-04T16:30:00", confirm_by="pm", confirm_at="2026-10-04T17:50:00")
    gen_doc(c, "safety", "monthly_review", "2026-09-01", "2026-10-02T15:00:00", confirm_by="eng", confirm_at="2026-10-02T17:20:00")


def precontrol(c: Ctx):
    """高风险作业预控提醒：2#圆管涵管节靠吊车吊装就位，放行归类为吊装作业（AI 归类缓存），安全主管向负责单位发预控提醒。"""
    c.x("UPDATE gate_class SET category='lifting' WHERE project_id=? AND activity_code='CV120'", c.pid)
    with as_of(c, "2026-09-25T15:30:00"):   # 管节 9-28 起吊装，开工前三天发预控提醒
        refs = c.call("safety", "GET", "/api/gongbi/refs?kind=precontrol_notice")
    refs = refs if isinstance(refs, list) else refs.get("items", [])
    say("预控对象", len(refs))
    for ref in refs[:2]:
        gen_doc(c, "safety", "precontrol_notice", ref["key"], "2026-09-25T16:10:00", confirm_by="safety", confirm_at="2026-09-25T16:40:00")
    ack_letters(c, ("precontrol_notice",))
    with at("2026-10-04T19:00:00"):
        refs = c.call("eng", "GET", "/api/gongbi/refs?kind=docdebt_notice")
    refs = refs if isinstance(refs, list) else refs.get("items", [])
    say("资料欠账对象", len(refs))
    for ref in refs[:2]:
        gen_doc(c, "eng", "docdebt_notice", ref["key"], "2026-10-04T16:20:00", confirm_by="eng", confirm_at="2026-10-04T16:50:00")


# ----------------------------------------------------------------------------- 讨论组、私信、公告

TALK = [
    ("topic", "雨季施工怎么保安全和进度", "9 月中旬起进入强降雨期，聊聊排水、停工判定和复工检查。", "", "", "2026-09-16T18:20:00",
     [("eng", "今天大雨，K1+300 便道积水，机械进出受影响，各分包先报一下复工条件。"), ("zhao", "K0~K1 段边沟已疏通，雨停两小时可以复工，土方运输还要等便道晾干。"),
      ("qian", "桩基泥浆池水位偏高，已加高围挡，旋挖钻明早复钻。"), ("safety", "复工前各班组先做班前检查，临边、用电、边坡都要看，照片发群里。")]),
    ("topic", "混凝土小票与取样怎么对得上", "小票、取样、试块编号一一对应，大家分享做法。", "", "", "2026-09-19T20:10:00",
     [("qian", "每根桩第一车拍小票，试块编号写进随手记，方便对。"), ("eng", "好做法，周报里按小票汇总方量，月底和拌合站对账。"),
      ("li", "拌合站这边出站单和小票同号，试块 7 天和 28 天到期会提前提醒。")]),
    ("issue", "立交桥桩基工效偏低", "钻机进场晚加上故障停钻，累计比计划少，讨论怎么追。", "立交桥 1#墩", "BR110", "2026-09-25T17:40:00",
     [("pm", "桩基目前比计划落后多少？第二台钻机什么时候能上？"), ("qian", "第二台旋挖钻原定 9 月 28 日进场，双机后日均可以提上来，力争 10 月底追回。"),
      ("eng", "钢筋笼加工提前一天，避免钻机等笼；混凝土按桩位提前预约。"), ("li", "拌合站这边混凝土供应没问题，夜间也能保供。")]),
    ("special", "独立日节前安全检查专题", "国庆独立日放假前的节前检查要点和值班安排。", "", "", "2026-09-29T08:30:00",
     [("safety", "节前检查清单已发，重点：用电、动火、吊装、临时设施。"), ("safe_a", "甲分包已完成自查，发现气瓶存放问题，已整改。"),
      ("safe_b", "乙分包自查完成，拌合站发电机房灭火器已补配。")]),
    ("issue", "1#盖板涵基坑临边防护", "基坑边堆土过近，需整改并保持。", "1#盖板涵 K0+860", "CV110", "2026-09-30T16:10:00",
     [("safe_b", "今天复查，堆土已清到基坑 3 米以外，护栏补齐。"), ("sun", "收到，后续弃土统一运到指定堆场，不再堆在坑边。"), ("safety", "好，保持，周五再复查一次。")]),
]
DM = [
    ("pm", "eng", "桩基滞后的赶工方案今天能给我吗？", "2026-09-25T10:12:00"), ("eng", "pm", "下午三点前发您，包含双机作业和夜间浇筑安排。", "2026-09-25T10:20:00"),
    ("safety", "safe_a", "气瓶存放整改完了吗？麻烦拍张照片。", "2026-09-29T15:05:00"), ("safe_a", "safety", "已整改，照片已上传随手拍。", "2026-09-29T16:40:00"),
    ("eng", "zhao", "K2 段清表 10 月 6 日开始，社区占道协调我来跟，你们先备好机械。", "2026-10-03T18:30:00"), ("zhao", "eng", "好的郑工，挖机和自卸车已调度到位。", "2026-10-03T19:02:00"),
    ("pm", "safety", "节前检查结果周五前给我一版摘要。", "2026-09-28T11:00:00"), ("safety", "pm", "收到，周四下班前发您。", "2026-09-28T11:25:00"),
    ("sun", "eng", "圆管涵管节已到，明天开始安装，请安排验收。", "2026-09-28T17:45:00"), ("eng", "sun", "明天上午九点我到现场，吊装警戒提前拉好。", "2026-09-28T18:05:00"),
]
NOTICES = [
    ("pm", "生产例会安排", "每周一上午 10:30 在项目部会议室召开生产例会，各分包技术员和安全员参加，会前一天看系统例会底稿。", True, "2026-09-12T17:00:00"),
    ("safety", "雨季施工安全提示", "强降雨期间，边坡、基坑、临时用电需每日检查；暴雨停工，雨后复工须经安全员确认。", False, "2026-09-15T07:30:00"),
    ("eng", "资料报验提醒", "隐蔽工程先报验后覆盖，桩基、涵洞基坑、钢筋均须有照片和实测数据，缺拍的在系统里补齐。", False, "2026-09-20T18:00:00"),
    ("pm", "独立日放假与值班安排", "10 月 1 日独立日本地工放假一天，中方人员正常值班；节后 10 月 2 日全面复工，复工前做班前检查。", False, "2026-09-29T17:30:00"),
]


def talk(c: Ctx):
    if c.q("SELECT COUNT(*) n FROM talk_posts WHERE project_id=?", c.pid)[0]["n"] >= sum(len(t[-1]) for t in TALK):
        return
    c.x("DELETE FROM talk_likes WHERE post_id IN (SELECT id FROM talk_posts WHERE project_id=?)", c.pid)   # 上次只建成一半的讨论组整体重建
    for t in ("talk_comments", "talk_posts", "talk_groups"):
        c.x(f"DELETE FROM {t} WHERE project_id=?", c.pid)
    mine = {}
    for r in c.q("SELECT id, uploader_id FROM media WHERE project_id=? AND purpose IN ('log','safety') ORDER BY id", c.pid):
        mine.setdefault(r["uploader_id"], []).append(r["id"])
    for kind, title, intro, part, code, t0, posts in TALK:
        t = datetime.fromisoformat(t0)
        with at(t.isoformat()):
            g = c.call(posts[0][0], "POST", "/api/talk/groups", json={"kind": kind, "title": title, "intro": intro, "part": part, "activity_code": code})
        gid = _id(g) or (g.get("group") or {}).get("id")
        pids = []
        for who, text in posts:
            t += timedelta(minutes=c.rng.randint(6, 55))
            own = mine.get(c.users["sim_" + who], [])
            mids = [own[c.rng.randrange(len(own))]] if own and c.rng.random() < 0.5 else []   # 只能附自己上传的照片
            with at(t.isoformat()):
                r = c.call(who, "POST", f"/api/talk/groups/{gid}/posts", json={"text": text, "media_ids": mids}, ok=(200, 400))
            pids.append(_id(r) or (r.get("post") or {}).get("id"))
        for pid in pids:
            if not pid:
                continue
            t += timedelta(minutes=c.rng.randint(3, 30))
            with at(t.isoformat()):
                for who in c.rng.sample(["pm", "eng", "safety", "zhao", "qian", "sun", "li"], 3):
                    c.call(who, "POST", f"/api/talk/posts/{pid}/like", ok=(200, 400))
                if c.rng.random() < 0.5:
                    c.call(c.rng.choice(["pm", "eng", "safety"]), "POST", f"/api/talk/posts/{pid}/comments", json={"text": c.rng.choice(["收到，按此执行。", "同意，落实到人。", "辛苦了，继续保持。"])})
        if kind == "special":
            with at(t.isoformat()):
                c.call("safety", "POST", f"/api/talk/groups/{gid}/pin", json={"pinned": True}, ok=(200, 400))


def social(c: Ctx):
    name = c.q("SELECT name FROM projects WHERE id=?", c.pid)[0]["name"]
    with at("2026-09-11T09:00:00"):
        c.call("admin", "PUT", "/api/project", json={"name": name, "social_enabled": True})   # 现场圈社交层演示开启
    if c.q("SELECT COUNT(*) n FROM dm_messages WHERE project_id=?", c.pid)[0]["n"] < len(DM):
        for a, b, text, t in sorted(DM, key=lambda x: x[3]):
            with at(t):
                c.call(a, "POST", "/api/social/dm/send", json={"to": c.users["sim_" + b], "text": text})
    have = {r["title"] for r in c.q("SELECT title FROM circle_notices WHERE project_id=?", c.pid)}
    for who, title, body, pinned, t in NOTICES:
        if title not in have:
            with at(t):
                c.call(who, "POST", "/api/circle/notices", json={"title": title, "body": body, "pinned": pinned, "audience": "all", "company": ""})


def quality_lab(c: Ctx):
    """试块（每个浇筑部位两组，7 天强度到期回填）、材料复试结论。仿真值，落在 C30 合理区间。"""
    with at("2026-10-04T19:00:00"):
        pours = c.call("eng", "GET", "/api/pours")
    pours = pours if isinstance(pours, list) else pours.get("items", [])
    for p in pours:
        pid = p["id"]
        if c.q("SELECT COUNT(*) n FROM specimens WHERE pour_id=?", pid)[0]["n"]:
            continue
        d0 = (p.get("pour_date") or c.day)[:10]
        for g in ("A", "B"):
            with at(f"{d0}T18:30:00"):
                c.call("qian", "POST", f"/api/pours/{pid}/specimens", json={"group_no": f"{pid}-{g}", "molded_date": d0, "curing": "标准养护", "note": "仿真试块"}, ok=(200, 400))
    for r in c.q("SELECT s.id, s.due_7 FROM specimens s WHERE s.project_id=? AND s.r7_mpa IS NULL", c.pid):
        if (r["due_7"] or "9999") <= c.day:
            with at(f"{r['due_7']}T15:00:00"):
                c.call("eng", "POST", f"/api/specimens/{r['id']}/result",
                       json={"age": 7, "mpa": round(c.rng.uniform(23.5, 28.5), 1), "report_no": f"SIM-SP7-{r['id']:03d}", "tested_at": r["due_7"]}, ok=(200, 400))
    for b in c.q("SELECT id, arrived_at FROM material_batches WHERE project_id=? AND retest_status='pending'", c.pid):
        t = (datetime.fromisoformat(b["arrived_at"][:19]) + timedelta(days=4)).replace(hour=16, minute=0)
        with at(t.isoformat()):
            c.call("eng", "PUT", f"/api/batches/{b['id']}/retest", json={"status": "pass", "report_no": f"SIM-RT-{b['id']:03d}", "reported_at": t.date().isoformat(), "note": "仿真：复试合格"})


def backtest(c: Ctx):
    """历史回测：用仿真剧本各周 P6 更新（XER）做一次“仿真/测试数据”回测；页面标注不计入成效。"""
    if c.q("SELECT COUNT(*) n FROM backtests WHERE project_id=? AND source='test'", c.pid)[0]["n"]:
        return
    from tools.sim_replay import scenario as S, xer
    truth = S.daily_truth()
    dds = [date(2026, 9, 14), date(2026, 9, 21), date(2026, 9, 28)]
    files = [("files", (f"SIM-ROAD-update-{d}.xer", xer.build(truth, d).encode("utf-8"), "application/octet-stream")) for d in dds]
    with at("2026-10-04T18:10:00"):
        c.call("eng", "POST", "/api/backtest/run", files=files, data={"source": "test", "label": "仿真公路项目 9 月 3 期更新回测（仿真数据，不计入成效）", "tolerance_days": "0"})


STEPS = [("hazard_photos", hazard_photos), ("h10", h10), ("qr", qr), ("conceal", conceal), ("meeting_items", meeting_items), ("claims", claims), ("reports", reports),
         ("english", english_reports), ("letters", letters), ("more_docs", more_docs), ("precontrol", precontrol), ("talk", talk), ("social", social),
         ("quality_lab", quality_lab), ("backtest", backtest)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=str(ROOT / "data_showcase"))
    ap.add_argument("--day", default="2026-10-04")
    ap.add_argument("--mock", action="store_true", help="不装仿真撰写，直接用系统离线结果（调试用）")
    ap.add_argument("--only", default="")
    ap.add_argument("--dump", default="", help="把每次 AI 调用的事实与结果写到这个 JSON 文件（撰写前看事实用）")
    a = ap.parse_args()
    data_dir = Path(a.data_dir).resolve()
    if data_dir == (ROOT / "data").resolve() or not (data_dir / "DEMO_COPY.json").exists():
        sys.exit("只能对演示副本运行（需有 DEMO_COPY.json，且不能是 data/）")
    os.environ.update(ZB_DATA_DIR=str(data_dir), ZB_SCHEDULER="0", ZB_DEV_LOGIN="1", ZB_AI_PROVIDER="mock", ZB_ENV_FILE="NUL")
    sys.path.insert(0, str(ROOT))
    import time_machine
    trav = time_machine.travel(datetime.fromisoformat(a.day + "T19:30:00").astimezone(), tick=True)
    trav.start()
    from app.db import init_db
    init_db()
    from tools.showcase import author
    if a.dump:
        author.DUMP = []
    if not a.mock:
        author.install()
    c = Ctx(a.day)
    only = set(filter(None, a.only.split(",")))
    for name, fn in STEPS:
        if only and name not in only:
            continue
        safe(name, fn, c)
    trav.stop()
    if a.dump:
        author.save_dump(Path(a.dump))
    (data_dir / "fill.log").write_text("\n".join(LOG), "utf-8")


if __name__ == "__main__":
    main()
