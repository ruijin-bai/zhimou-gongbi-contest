"""仿真回放引擎：把时钟拨回 2026-09-07，逐日以各岗位身份调用真实接口，让真实 AI Agent 产出。

- 人的输入（随手记、照片、单据、纪要、提问……）来自剧本底账与 prepare 生成的素材；
- 人的决定（确认/修改/驳回/闭环）由本引擎按底账判定；
- AI 的产出全部由系统在回放中现场生成（--mock 时用离线模拟模型，只用于调试引擎）。

用法：
  python -m tools.sim_replay.replay --mock --data-dir <临时目录>     # 调试
  python -m tools.sim_replay.replay                                  # 正式：写入 data/（先自动备份数据库）
  可加 --to 2026-09-13 只回放到某天；中断后再次运行会从断点继续（状态存于 数据目录/sim_replay_state.json）。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import random
import re
import shutil
import sys
import time
import traceback
import wave
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "sim_assets"
PHOTOS = Path(os.environ.get("SIM_PHOTOS") or (ASSETS / "photos"))
TZ = datetime.now().astimezone().tzinfo
PASSWORD = "sim12345"
SUPERVISOR = "Engr. Okafor（监理）"
COMMENTS = ["收到", "注意安全", "辛苦了", "这段推进得不错", "雨后注意边坡和排水", "明天继续保持", "照片拍得清楚", "注意机械和人员分开作业"]
GEO = (6.4655, 3.8496)  # 莱基疏港路起点附近（仿真）


def log(*a):
    print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- 环境（必须在导入 app 之前）
def setup_env(data_dir: Path, mock: bool) -> None:
    os.environ["ZB_DATA_DIR"] = str(data_dir)
    os.environ["ZB_SCHEDULER"] = "0"
    os.environ["ZB_DEV_LOGIN"] = "1"
    if mock:
        os.environ["ZB_AI_PROVIDER"] = "mock"


class Actor:
    """某个岗位的人：独立会话，按需登录。"""

    def __init__(self, eng: "Engine", key: str):
        from fastapi.testclient import TestClient
        self.eng, self.key = eng, key
        self.c = TestClient(eng.app, raise_server_exceptions=False, headers={"x-zb": "1"})
        self.uid = eng.uids[key]
        self.logged = None

    def _login(self):
        today = date.today().isoformat()
        if self.logged != today:
            self.c.post("/api/dev/login", json={"user_id": self.uid})
            self.logged = today

    def call(self, method: str, path: str, ok: tuple = (200,), **kw):
        self._login()
        r = self.c.request(method, path, **kw)
        if r.status_code == 401:
            self.logged = None
            self._login()
            r = self.c.request(method, path, **kw)
        try:
            body = r.json()
        except Exception:
            body = r.text[:300]
        if r.status_code not in ok:
            raise RuntimeError(f"{self.key} {method} {path} → {r.status_code} {str(body)[:300]}")
        return body

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, **kw):
        return self.call("POST", path, **kw)

    def put(self, path, **kw):
        return self.call("PUT", path, **kw)


class Engine:
    def __init__(self, data_dir: Path, mock: bool, organize_each_note: bool = False):
        self.data_dir, self.mock = data_dir, mock
        import time_machine
        self.tm = time_machine
        from app import main as M
        from app.db import init_db
        init_db()
        self.M, self.app = M, M.app
        if not organize_each_note:
            # 提速：白天每条随手记后的后台重整（每条一次 AI 起草）不跑，日志由每晚 8 点的统一整理生成（周日在周确认前先整理）。
            # 最终日志内容一致，只是 AI 起草次数少；产品本身仍是随记随整理。
            M._organize_bg = lambda *a, **k: None
        from . import scenario as S
        self.S = S
        self.truth = S.daily_truth()
        self.notes = json.loads((ASSETS / "cache" / "notes.json").read_text("utf-8")) if (ASSETS / "cache" / "notes.json").exists() else {}
        self.docs = json.loads((ASSETS / "cache" / "docs.json").read_text("utf-8")) if (ASSETS / "cache" / "docs.json").exists() else []
        self.state_file = data_dir / "sim_replay_state.json"
        self.st = json.loads(self.state_file.read_text("utf-8")) if self.state_file.exists() else {"done": [], "ids": {}, "errors": []}
        self.rng = random.Random(S.SEED + 99)
        self.uids: dict[str, int] = {}
        self.actors: dict[str, Actor] = {}
        self.coords = None
        self.traveller = None
        self.photo_owner: dict[int, str] = {int(k): v for k, v in self.st["ids"].get("photo_owner", {}).items()}

    # ---------------------------------------------------------- 基础
    def save(self):
        self.st["ids"]["photo_owner"] = {str(k): v for k, v in self.photo_owner.items()}
        self.state_file.write_text(json.dumps(self.st, ensure_ascii=False, indent=1), "utf-8")

    def ids(self, kind: str) -> dict:
        return self.st["ids"].setdefault(kind, {})

    def at(self, dt: datetime):
        if self.traveller is None:
            self.traveller = self.tm.travel(dt, tick=True)
            self.coords = self.traveller.start()
        else:
            self.coords.move_to(dt)

    def who(self, key: str) -> Actor:
        if key not in self.actors:
            self.actors[key] = Actor(self, key)
        return self.actors[key]

    def run(self, key: str, when: datetime, fn, *a):
        if key in self.st["done"]:
            return
        self.at(when)
        try:
            fn(*a)
            log("✓", when.strftime("%m-%d %H:%M"), key)
        except Exception as exc:
            log("✗", when.strftime("%m-%d %H:%M"), key, str(exc)[:400])
            self.st["errors"].append({"key": key, "at": when.isoformat(), "error": str(exc)[:600], "tb": traceback.format_exc()[-1500:]})
        self.st["done"].append(key)
        self.save()

    def dt(self, day: str, hm: str) -> datetime:
        h, m = map(int, hm.split(":"))
        return datetime.fromisoformat(day).replace(hour=h, minute=m, second=self.rng.randint(0, 50), tzinfo=TZ)

    def geo(self, place: str) -> tuple[float, float, int]:
        m = re.search(r"K(\d)\+(\d{3})", place or "")
        ch = (int(m.group(1)) * 1000 + int(m.group(2))) if m else self.rng.randint(300, 1800)
        return round(GEO[0] + ch * 0.0000065 + self.rng.uniform(-0.0002, 0.0002), 6), round(GEO[1] + ch * 0.0000085 + self.rng.uniform(-0.0002, 0.0002), 6), self.rng.choice([5, 6, 8, 10, 12])

    # ---------------------------------------------------------- 项目与账号（直接写库：系统没有建项目接口）
    def setup_project(self):
        from app.db import connect, dumps, insert, now_iso, one
        from app.security import hash_password
        S = self.S
        conn = connect()
        try:
            p = one(conn, "SELECT id FROM projects WHERE code=?", (S.PROJECT["code"],))
            if p:
                pid = p["id"]
            else:
                old = one(conn, "SELECT id FROM projects WHERE code='LEKKI-SIM'")
                if old:  # 旧仿真项目保留，改名并停用其演示账号（腾出 sim_* 账号名给新项目）
                    conn.execute("UPDATE projects SET name='莱基深水港疏港路（仿真·旧版）' WHERE id=?", (old["id"],))
                    for u in conn.execute("SELECT id, username FROM users WHERE project_id=? AND username LIKE 'sim\\_%' ESCAPE '\\'", (old["id"],)).fetchall():
                        u = dict(u)  # 行工厂可能是字典或元组，统一按列名取
                        if not u["username"].endswith("_v1"):
                            conn.execute("UPDATE users SET username=?, active=0 WHERE id=?", (u["username"] + "_v1", u["id"]))
                cfg = {"simulation": True, "workdays_per_week": 7, "cadence": "week", "daily_log": True}
                pid = insert(conn, "projects", {"name": S.PROJECT["name"], "code": S.PROJECT["code"], "config_json": dumps(cfg), "created_at": now_iso()})
            for key, (uname, name, role, company, scope) in S.PEOPLE.items():
                u = one(conn, "SELECT id FROM users WHERE username=?", (uname,))
                if u:
                    self.uids[key] = u["id"]
                    continue
                self.uids[key] = insert(conn, "users", {"username": uname, "name": name, "role": role, "password_hash": hash_password(PASSWORD),
                                                        "project_id": pid, "wbs_scope": scope, "email": "", "active": 1, "created_at": now_iso(),
                                                        "company": company})
            self.pid = pid
        finally:
            conn.close()

    # ---------------------------------------------------------- 照片
    def photo_bytes(self, code: str, when: datetime, place: str, work: str) -> bytes | None:
        from .watermark import stamp
        src = next((p for p in PHOTOS.glob(f"{code}.*") if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")), None)
        if not src:
            return None
        return stamp(src, when, place, work, self.S.PROJECT["name"])

    def upload(self, actor: Actor, purpose: str, blobs: list[tuple[str, bytes]], place: str, when: datetime, scan: bool = True, extra: dict | None = None) -> list[int]:
        lat, lon, acc = self.geo(place)
        files = [("files", (name, b, "image/jpeg")) for name, b in blobs]
        data = {"purpose": purpose, "scan": "1" if scan else "0", "taken_at": [when.isoformat()] * len(blobs), "geo_lat": [str(lat)] * len(blobs),
                "geo_lon": [str(lon)] * len(blobs), "geo_acc": [str(acc)] * len(blobs), "place": [place] * len(blobs), **(extra or {})}
        r = actor.post("/api/media", files=files, data=data)
        items = r.get("items") if isinstance(r, dict) else r
        return [int(x["id"]) for x in (items or [])]

    # ---------------------------------------------------------- 随手记
    def do_note(self, tech: str, day: str, i: int, n: dict):
        a = self.who(tech)
        S = self.S
        when = self.dt(day, n["time"])
        ph = next((p for p in S.PHOTOS if p[0] == day and p[2] == n.get("photo")), None) if n.get("photo") else None
        place = ph[3] if ph else ""
        lat, lon, acc = self.geo(place or "K0+500")
        body = {"lat": lat, "lon": lon, "acc": acc, "place": place}
        if n["mode"] == "voice":
            wav = ASSETS / "voice" / f"{day}_{tech}_{i}.wav"
            if wav.exists():
                with wave.open(str(wav)) as w:
                    secs = round(w.getnframes() / w.getframerate(), 1)
                try:
                    r = a.post("/api/asr", files={"file": ("note.wav", wav.read_bytes(), "audio/wav")})
                    text = (r.get("text") or "").strip() or n["text"]
                except RuntimeError as exc:  # 语音识别失败：像真人一样改用打字记下这句话
                    log("  语音识别失败，改为打字", str(exc)[:120])
                    body.update(text=n["text"], source="text", seconds=round(len(n["text"]) / 3 + 10, 1))
                    a.post("/api/notes", json=body)
                    return
            else:
                text, secs = n["text"], round(len(n["text"]) / 4, 1)
            body.update(text=text, source="voice", seconds=secs)
        elif n["mode"] == "photo" and ph:
            b = self.photo_bytes(ph[2], when, ph[3], ph[4])
            if b is None:
                log("  缺照片，跳过", ph[2])
                return
            mids = self.upload(a, "log", [(f"{ph[2]}.jpg", b)], ph[3], when, scan=True)
            self.ids("media")[ph[2]] = mids[0]
            self.photo_owner[mids[0]] = tech
            body.update(text=n["text"], source="photo", media_ids=mids, seconds=round(self.rng.uniform(6, 15), 1))
        else:
            body.update(text=n["text"], source="text", seconds=round(len(n["text"]) / 3 + self.rng.uniform(5, 20), 1))
        r = a.post("/api/notes", json=body)
        nid = (r or {}).get("id") or ((r or {}).get("note") or {}).get("id")
        if n["mode"] == "photo" and nid:
            self.review_classify(tech, nid, ph)

    def review_classify(self, tech: str, nid: int, ph):
        """随手记归类建议：底账是隐患（H 照片）就点“是”，否则点“不是”。"""
        a = self.who(tech)
        r = a.get(f"/api/notes/classify?ids={nid}")
        items = r.get("items") if isinstance(r, dict) else r
        sug = next((x for x in (items or []) if int(x.get("note_id") or x.get("id") or 0) == nid), None) or ((items or [None])[0])
        if not sug or not (sug.get("kind") or sug.get("suggest")):
            return
        kind = sug.get("kind") or (sug.get("suggest") or {}).get("kind")
        is_hazard = ph and ph[5] == "hazard"
        if kind in ("safety", "hazard") and is_hazard:
            a.post(f"/api/notes/{nid}/classify/accept", json={"kind": kind})
            self.ids("hazard_note")[ph[2]] = nid
        else:
            a.post(f"/api/notes/{nid}/classify/dismiss", json={})

    # ---------------------------------------------------------- 隐患
    def candidates_for(self, actor: Actor, media_id: int) -> list[dict]:
        r = actor.get("/api/findings?agent=safety&candidates=only&status=")
        items = r.get("items") if isinstance(r, dict) else r
        return [f for f in (items or []) if f.get("ref_type") == "media" and int(f.get("ref_id") or 0) == media_id]

    def snap_hazard(self, code: str, day: str, hm: str):
        S = self.S
        ph = next(p for p in S.PHOTOS if p[2] == code)
        who = ph[1]
        a = self.who(who)
        when = self.dt(day, hm)
        b = self.photo_bytes(code, when, ph[3], ph[4])
        if b is None:
            raise RuntimeError(f"缺照片 {code}")
        mids = self.upload(a, "safety", [(f"{code}.jpg", b)], ph[3], when, scan=True)
        self.ids("media")[code] = mids[0]

    def decide_candidates(self, day: str):
        """分包安全员/安全主管处理当天所有候选：底账隐患照片 → 确认派单（按剧本可“修改后确认”）；其它 → 不采纳。"""
        S = self.S
        media_to_code = {v: k for k, v in self.ids("media").items()}
        for dec in ("safe_a", "safe_b", "safety"):
            a = self.who(dec)
            r = a.get("/api/findings?agent=safety&candidates=only&status=")
            for f in (r.get("items") if isinstance(r, dict) else r) or []:
                fid = int(f["id"])
                if str(fid) in self.ids("finding_done"):
                    continue
                code = media_to_code.get(int(f.get("ref_id") or 0)) if f.get("ref_type") == "media" else None
                code = code or self.ids("qr_media").get(str(f.get("ref_id")))
                hz = S.HAZARDS.get(code or "")
                title = f.get("title") or ""
                if hz and str(code) not in self.ids("hazard") and re.search(S.HAZARD_KW.get(code, "$^"), title):
                    due = (date.fromisoformat(day) + timedelta(days=hz["fix_days"])).isoformat()
                    body = {"assignee_id": self.uids[hz["owner"]], "due_date": due}
                    if hz.get("level_fix"):
                        body["level"] = {"高": "high", "中": "medium", "低": "low"}[hz["level_fix"]]
                    a.post(f"/api/findings/{fid}/confirm", json=body)
                    self.ids("hazard")[code] = fid
                elif hz and re.search(S.HAZARD_KW.get(code, "$^"), title):
                    a.post(f"/api/findings/{fid}/dismiss", json={"reason": "与同一照片已确认的隐患重复，已合并处理"})
                elif f.get("level") == "high":
                    # 剧本外、AI 在进度照片里发现的高风险隐患：安全员核实后确认派单（人没注意到的，AI 发现了）
                    owner = (hz or {}).get("owner") or self.photo_owner.get(int(f.get("ref_id") or 0)) or ("zhao" if dec == "safe_a" else "sun")
                    days = (hz["fix_days"] + hz.get("late", 0)) if hz else 1   # 剧本照片上的：与剧本整改同日；兜底闭环晚一天，让整改流程先走
                    due = (date.fromisoformat(day) + timedelta(days=days)).isoformat()
                    a.post(f"/api/findings/{fid}/confirm", json={"assignee_id": self.uids[owner], "due_date": due})
                    self.ids("extra_hazard")[str(fid)] = (date.fromisoformat(due) + timedelta(days=1 if hz else 0)).isoformat()
                    self.ids("extra_media")[str(fid)] = int(f.get("ref_id") or 0)
                else:
                    a.post(f"/api/findings/{fid}/dismiss", json={"reason": "现场已当场纠正，不另派单"})
                self.ids("finding_done")[str(fid)] = 1

    def rescan_failed(self):
        """照片安全识别失败不自动补跑：安全主管看到“识别失败”后点“重新识别”。"""
        self.who("safety").post("/api/safety/rescan", json={})

    def rectify(self, code: str, day: str, hm: str, bad: bool = False):
        S = self.S
        fid = self.ids("hazard").get(code)
        if not fid:  # 剧本隐患没按关键词认出：用同一张照片上已确认派单的那条走整改
            mid = self.ids("media").get(code)
            fid = next((int(k) for k, v in self.ids("extra_media").items() if mid and v == mid and self.ids("extra_hazard").get(k) != "done"), None)
            if fid:
                self.ids("hazard")[code] = fid
                self.ids("extra_hazard")[str(fid)] = "done"   # 交给整改流程，不再由兜底闭环
        if not fid:
            raise RuntimeError(f"{code} 没有派单记录，无法整改")
        hz = S.HAZARDS[code]
        a = self.who(hz["owner"])
        ph = next((p for p in S.PHOTOS if p[2] == code), None)
        place = ph[3] if ph else hz.get("place", "")
        when = self.dt(day, hm)
        src = code if bad else "R" + code[1:]
        b = self.photo_bytes(src, when, place, "隐患整改完成" if not bad else "隐患整改")
        if b is None:  # 没有整改照片：安全主管现场复查后人工闭环（如实写明未上传照片）
            self.who("safety").post(f"/api/findings/{fid}/status", json={"status": "resolved", "note": "现场复查已整改到位（整改照片未上传）"})
            log("  缺整改照片，安全主管现场复查闭环", code)
            return
        mids = self.upload(a, "rectify", [(f"{src}.jpg", b)], place, when, scan=False)
        a.post(f"/api/findings/{fid}/rectify", json={"media_id": mids[0]})

    def close_hazards(self, day: str):
        """安全主管复核：AI 复核通过、处于“整改中”的，人工确认闭环；AI 认为未整改的退回。"""
        a = self.who("safety")
        for fid, due in list(self.ids("extra_hazard").items()):
            if due <= day and due != "done":
                try:
                    a.post(f"/api/findings/{fid}/status", json={"status": "resolved", "note": "现场复查已整改到位（整改照片未上传）"})
                except Exception as exc:
                    log("  剧本外隐患闭环失败", fid, str(exc)[:150])
                self.ids("extra_hazard")[fid] = "done"
        r = a.get("/api/findings?agent=safety&status=")
        seen = self.ids("rejected_seen")
        for f in (r.get("items") if isinstance(r, dict) else r) or []:
            fid = str(f["id"])
            if f.get("status") == "rejected" or (f.get("status") == "open" and f.get("rectify_media_id")):
                # AI 复核认为未整改到位：次日分包现场补整改，安全主管到场复查后闭环（人做最终决定）
                first = seen.setdefault(fid, day)
                if first < day and self.ids("hazard_rectify_code").get(fid) != "pending":
                    a.post(f"/api/findings/{fid}/status", json={"status": "resolved", "note": "现场复查：上次未到位之处已补整改"})
                continue
            if f.get("status") != "rectifying":
                continue
            v = f.get("verify") or {}
            if isinstance(v, str):
                try:
                    v = json.loads(v)
                except Exception:
                    v = {}
            if v.get("resolved") is False or v.get("ok") is False:
                a.post(f"/api/findings/{f['id']}/status", json={"status": "open", "note": "整改照片未体现整改到位，请重新整改"})
            else:
                a.post(f"/api/findings/{f['id']}/status", json={"status": "resolved", "note": "现场复核已整改"})

    # ---------------------------------------------------------- 周确认 / 预审
    def week_confirm(self, tech: str, monday: str):
        S = self.S
        d0 = date.fromisoformat(monday)
        if not any(self.notes.get(f"{(d0 + timedelta(days=i)).isoformat()}|{tech}") for i in range(7)):
            log("  本周没有随手记，不做周确认", tech, monday)
            return
        a = self.who(tech)
        r = a.post("/api/logs", json={"log_date": monday, "period": "week", "mode": "ai"})
        lid = int((r or {}).get("id") or (r.get("log") or {}).get("id"))
        for attempt in range(2):  # AI 汇总偶尔超时：像真人一样再点一次
            try:
                a.post(f"/api/logs/{lid}/draft", json={})
                break
            except RuntimeError as exc:
                if attempt:
                    raise
                log("  周确认汇总失败，重试", tech, str(exc)[:100])
        lg = a.get(f"/api/logs/{lid}")
        rows = ((lg.get("week_review") or {}).get("rows")) or []
        wk = {}
        d0 = date.fromisoformat(monday)
        for i in range(7):
            for w in self.truth.get((d0 + timedelta(days=i)).isoformat(), {}).get(tech, []):
                wk[w.code] = wk.get(w.code, 0) + w.qty
        decisions = []
        for row in rows:
            key = row.get("key")
            ai = row.get("ai_value", row.get("ai", row.get("value")))
            try:
                aiv = float(re.search(r"-?\d+(\.\d+)?", str(ai)).group())
            except Exception:
                aiv = None
            true = wk.get(key)
            if true is None or true <= 0:
                decisions.append({"key": key, "decision": "reject", "reason": "本周该作业没有实际完成量"})
            elif aiv is not None and abs(aiv - true) <= max(0.01 * true, 0.5):
                decisions.append({"key": key, "decision": "confirm"})
            else:
                decisions.append({"key": key, "decision": "modify", "final": round(true, 2), "reason": "按每日记录核对后修正"})
        secs = self.rng.randint(240, 720)
        a.post(f"/api/logs/{lid}/submit", json={"version": lg.get("version") or (lg.get("log") or {}).get("version"), "active_seconds": secs, "decisions": decisions})
        self.ids("week_log")[f"{monday}|{tech}"] = lid

    def week_confirm_if_missing(self, tech: str, monday: str):
        if f"{monday}|{tech}" not in self.ids("week_log"):
            self.week_confirm(tech, monday)

    def eng_review(self, monday: str):
        """工程主管看上周周确认的 AI 预审：底账里有的问题判“属实”并退回，其余“误报”；没有问题的低风险一键通过。"""
        a = self.who("eng")
        for key, lid in list(self.ids("week_log").items()):
            if not key.startswith(monday) or key in self.ids("reviewed"):
                continue
            tech = key.split("|")[1]
            lg = a.get(f"/api/logs/{lid}")
            rows = ((lg.get("dq_review") or {}).get("rows")) or []
            trap = any(t[1] == tech and date.fromisoformat(monday) <= date.fromisoformat(t[0]) < date.fromisoformat(monday) + timedelta(days=7) for t in self.S.DQ_TRAPS)
            dec, hit = [], False
            for row in rows:
                txt = json.dumps(row, ensure_ascii=False)
                real = trap and any(k in txt for k in ("数量", "方量", "异常", "偏大", "重复", "照片"))
                hit = hit or real
                dec.append({"key": row.get("key"), "decision": "true" if real else "false", **({} if real else {"reason": "核对现场与每日记录，无此问题"})})
            if hit:
                a.post(f"/api/logs/{lid}/review", json={"action": "return", "note": "请核对本周工程量与照片后重新提交", "decisions": dec})
            else:
                a.post(f"/api/logs/{lid}/review", json={"action": "approve", "decisions": dec})
            self.ids("reviewed")[key] = 1

    # ---------------------------------------------------------- 其它岗位动作
    def p6_import(self, dd: date | None):
        from . import xer
        a = self.who("eng")
        text = xer.build(self.truth, dd) if dd else xer.build()
        name = f"LEKKI-SIM2-{'baseline' if not dd else 'update-' + dd.isoformat()}.xer"
        a.post("/api/p6/import", files={"file": (name, text.encode("utf-8"), "application/octet-stream")}, data={"data_date": (dd or self.S.START).isoformat()})

    def boq(self):
        import openpyxl
        S = self.S
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["编码", "名称", "单位", "单价", "数量"])
        for code, name, wbs, dur, qty, unit, price, *_ in S.PLAN:
            if dur:
                ws.append([f"B-{code[:2]}-{code[2:]}", name.replace("K0+000~K2+000 ", "").replace("K2+000~K4+500 ", "") + ("（K0~K2）" if "K0" in name else ("（K2~K4.5）" if "K2" in name else "")), unit, price, qty])
        buf = io.BytesIO()
        wb.save(buf)
        a = self.who("eng")
        a.post("/api/output/boq", files={"file": ("工程量清单.xlsx", buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        a.post("/api/output/map/suggest", json={})

    def confirm_maps(self):
        a = self.who("eng")
        r = a.get("/api/output/maps")
        items = r.get("items") if isinstance(r, dict) else r
        ids = [m["id"] for m in (items or []) if m.get("status") in ("suggested", "pending", "ai")]
        if ids:
            a.post("/api/output/maps", json={"ids": ids, "status": "confirmed"})

    def report(self, kind: str, ref: str, publish_by: str | None = None):
        a = self.who("eng")
        r = a.post("/api/reports", json={"kind": kind, "ref_date": ref})
        rid = (r or {}).get("id") or ((r or {}).get("report") or {}).get("id")
        self.ids("report")[f"{kind}|{ref}"] = rid

    def publish(self, kind: str, ref: str):
        rid = self.ids("report").get(f"{kind}|{ref}")
        if not rid:
            raise RuntimeError("没有报告")
        a = self.who("pm")
        rep = a.get(f"/api/reports/{rid}")
        narr = rep.get("narrative") or (rep.get("report") or {}).get("narrative") or ""
        checks = rep.get("number_check") or (rep.get("report") or {}).get("number_check") or []   # 剧本里项目经理逐条核实后发布
        a.put(f"/api/reports/{rid}", json={"narrative": narr, "status": "published",
                                           "verified": [{"field": x["field"], "number": x["number"]} for x in checks if x.get("number")]})

    def meeting(self, day: str, text: str):
        S = self.S
        a = self.who("pm")
        r = a.post("/api/cmd/minutes", json={"text": text, "title": f"生产例会 {day}", "date": day})
        mid = r.get("id") or r.get("meeting_id") or (r.get("meeting") or {}).get("id")
        items = r.get("items") or []
        names = {v[1]: k for k, v in S.PEOPLE.items()}
        for idx, it in enumerate(items):
            owner = it.get("owner_name") or ""
            ok = next((k for n, k in names.items() if n and n in (owner + it.get("text", ""))), None)
            m = re.search(r"(\d{1,2})月(\d{1,2})日", it.get("text", ""))
            due = it.get("due_date") or (f"2026-{int(m.group(1)):02d}-{int(m.group(2)):02d}" if m else (date.fromisoformat(day) + timedelta(days=4)).isoformat())
            a.post(f"/api/meetings/{mid}/items", json={"idx": idx, "text": it.get("text", ""), "owner_id": self.uids.get(ok) if ok else None,
                                                      "owner_name": S.PEOPLE[ok][1] if ok else owner, "due_date": due})
        a.post(f"/api/meetings/{mid}/save", json={})
        self.ids("meeting")[day] = mid

    def meeting_items_done(self):
        for key in ("zhao", "qian", "sun", "li", "eng", "safe_a", "safe_b", "safety"):
            a = self.who(key)
            try:
                r = a.get("/api/meetings/mine")
            except Exception:
                continue
            for it in (r.get("items") if isinstance(r, dict) else r) or []:
                if it.get("done_at") or it.get("status") == "done":
                    continue
                if self.rng.random() < 0.8 and (it.get("due_date") or "9999") <= date.today().isoformat():
                    a.post(f"/api/meetings/items/{it['id']}/done", json={"note": "已完成"})

    def command(self, who: str, text: str):
        a = self.who(who)
        r = a.post("/api/cmd/parse", json={"text": text})
        card = r.get("card") or r.get("action") or r
        if not card or card.get("type") not in ("assign", "nudge"):
            raise RuntimeError(f"指令未解析出可执行动作：{str(r)[:300]}")
        if not card.get("finding_id") and card.get("finding_candidates"):
            card["finding_id"] = card["finding_candidates"][0]["id"]
        if card["type"] == "assign" and not card.get("assignee_id") and card.get("assignee_candidates"):
            card["assignee_id"] = card["assignee_candidates"][0]["id"]
        body = {k: card.get(k) for k in ("type", "finding_id", "assignee_id", "due_date", "note") if card.get(k) is not None}
        body["source"] = text
        a.post("/api/cmd/execute", json=body)

    def ask(self, who: str, q: str):
        self.who(who).post("/api/ask", json={"question": q, "agent": ""})

    def announce(self, who: str, text: str):
        self.who(who).post("/api/circle/notices", json={"title": text[:16], "body": text, "pinned": False, "audience": "all", "company": ""})

    def circle(self, day: str):
        """同事点赞/评论 + 触发 Agent 点评与精选。"""
        a = self.who("pm")
        r = a.get("/api/feed")
        items = (r.get("items") if isinstance(r, dict) else r) or []
        today = [x for x in items if str(x.get("at") or x.get("created_at") or "").startswith(day)]
        keys = [x["key"] for x in today if x.get("key")][:30]
        if keys:
            try:
                a.post("/api/circle/review", json={"keys": keys})
            except Exception as exc:
                log("  点评失败", str(exc)[:200])
        for x in today[:8]:
            for who in self.rng.sample(["eng", "pm", "safety", "zhao", "qian", "sun", "li"], 2):
                try:
                    self.who(who).post("/api/feed/like", json={"key": x["key"]})
                    if self.rng.random() < 0.3:
                        self.who(who).post("/api/feed/comments", json={"key": x["key"], "text": self.rng.choice(COMMENTS)})
                except Exception:
                    pass

    def alerts_feedback(self):
        a = self.who("eng")
        r = a.get("/api/alerts?status=open")
        for al in (r.get("items") if isinstance(r, dict) else r) or []:
            if al.get("feedback_status") or str(al["id"]) in self.ids("alert_fb"):
                continue
            a.post(f"/api/alerts/{al['id']}/feedback", json={"status": "confirmed", "note": "已核实，按措施推进"})
            self.ids("alert_fb")[str(al["id"])] = 1

    def quality_tick(self, day: str):
        for t in self.S.TECHS:
            a = self.who(t)
            r = a.get("/api/findings?agent=quality&status=open")
            for f in (r.get("items") if isinstance(r, dict) else r) or []:
                if (f.get("created_at") or "9")[:10] < day and self.rng.random() < 0.6:
                    a.post(f"/api/findings/{f['id']}/status", json={"status": "resolved", "note": "已按质控点完成自检"})

    def tickets(self, it: dict, hm: str):
        a = self.who(it["by"])
        day = it["date"]
        when = self.dt(day, hm)
        from PIL import Image
        from .watermark import stamp
        b = stamp(Image.open(ASSETS / "docs" / it["file"]), when, "立交桥" if it["kind"] == "concrete_ticket" else "钢筋加工场", "单据拍照", self.S.PROJECT["name"])
        lat, lon, acc = self.geo("")
        r = a.post("/api/tickets/upload", files=[("files", (it["file"], b, "image/jpeg"))],
                   data={"doc_kind": it["kind"], "taken_at": [when.isoformat()], "geo_lat": [str(lat)], "geo_lon": [str(lon)], "geo_acc": [str(acc)], "place": ["现场"]})
        tid = r.get("id") or (r.get("ticket") or {}).get("id") or ((r.get("items") or [{}])[0]).get("id")
        t = a.get(f"/api/tickets/{tid}")
        tk = t.get("ticket") or t
        ai = tk.get("ai_fields") or {}
        truth = {k: v for k, v in it["doc"].items() if k in ("supplier", "ticket_no", "vehicle", "issued_at", "part", "material", "grade", "spec", "qty", "unit", "batch_no", "slump", "temp", "receiver", "remark")}
        if it["kind"] == "concrete_ticket":
            truth.setdefault("material", "商品混凝土")
            truth.setdefault("unit", "m³")
        norm = lambda v: re.sub(r"\s|³|3$", "", str(v)).replace(".0", "").lower()  # noqa: E731
        fields, dec = {}, []
        for k in set(truth) | {k for k, v in ai.items() if v is not None}:
            v, got = truth.get(k), ai.get(k)
            has_truth = v is not None and str(v).strip() != ""
            if has_truth:
                fields[k] = v
            if got is None:
                continue  # AI 没读出来：人直接填，不需要决定
            if not has_truth:  # 票面没有这一项（或人不确定）：不采纳 AI 值
                dec.append({"key": k, "decision": "reject", "reason": "票面无此项"})
            elif norm(got) == norm(v):
                dec.append({"key": k, "decision": "confirm"})
            else:
                dec.append({"key": k, "decision": "modify", "final": v})
        body = {"doc_kind": it["kind"], "fields": fields, "decisions": dec, "category": "rebar" if "钢筋" in str(truth.get("material", "")) else None}
        if it["kind"] == "concrete_ticket":
            body["new_pour"] = {"part": truth["part"].replace("(水下)", "").strip(), "grade": "C30", "pour_date": day}
        a.put(f"/api/tickets/{tid}", json={k: v for k, v in body.items() if v is not None})

    def conceal(self, day: str, part: str, tpl: str, code: str, shots: list[tuple[str, str]], title: str, values: dict):
        a = self.who(self.S.PHOTOS[[p[2] for p in self.S.PHOTOS].index(shots[0][0])][1])
        r = a.post("/api/conceal", json={"part": part, "template_key": tpl, "activity_code": code, "inspection_date": day, "title": title})
        rid = r.get("id") or (r.get("record") or {}).get("id")
        for pcode, shot in shots:
            ph = next(p for p in self.S.PHOTOS if p[2] == pcode)
            when = self.dt(day, f"{self.rng.randint(9, 15):02d}:{self.rng.randint(0, 59):02d}")
            b = self.photo_bytes(pcode, when, ph[3], ph[4])
            if b is None:
                continue
            lat, lon, acc = self.geo(ph[3])
            a.post(f"/api/conceal/{rid}/photos", files=[("files", (f"{pcode}.jpg", b, "image/jpeg"))],
                   data={"shot": shot, "taken_at": [when.isoformat()], "geo_lat": [str(lat)], "geo_lon": [str(lon)], "geo_acc": [str(acc)], "place": [ph[3]]})
        a.put(f"/api/conceal/{rid}/fields", json={"fields": {kk: {"value": vv} for kk, vv in values.items()}})
        try:
            a.post(f"/api/conceal/{rid}/analyze", json={"narration": title})
        except Exception as exc:
            log("  验收分析失败", str(exc)[:200])
        try:
            a.post(f"/api/conceal/{rid}/ready", json={})
            a.post(f"/api/conceal/{rid}/submit", json={"supervisor_name": SUPERVISOR})
        except Exception as exc:
            log("  报验未完成（缺项）", str(exc)[:200])
        self.ids("conceal")[f"{day}|{tpl}"] = rid

    def conceal_result(self, key: str, note: str):
        rid = self.ids("conceal").get(key)
        if rid and (self.who("eng").get(f"/api/conceal/{rid}").get("status") or (self.who("eng").get(f"/api/conceal/{rid}").get("record") or {}).get("status")) in ("submitted", "报验"):
            self.who("eng").post(f"/api/conceal/{rid}/result", json={"result": "passed", "note": note, "date": date.today().isoformat()})

    def gate(self, codes: list[str], blocked: dict[str, str] | None = None):
        a = self.who("eng")
        for code in codes:
            g = a.get(f"/api/gate/{code}")
            items = g.get("items") or (g.get("gate") or {}).get("items") or []
            for i, it in enumerate(items):
                if it.get("state") in ("ready", "na"):
                    continue
                key = it.get("key") or it.get("cond_key")
                if blocked and i == 0 and code in blocked:
                    a.put(f"/api/gate/{code}/items/{key}", json={"state": "blocked", "note": blocked[code]})
                else:
                    a.put(f"/api/gate/{code}/items/{key}", json={"state": "ready", "note": "现场核实已具备"})

    def qr_create(self):
        r = self.who("safe_b").post("/api/quick/qrs", json={"label": "拌合站发电机房", "days": 60})
        self.ids("qr")["token"] = (r.get("path") or r.get("url") or "").rsplit("/", 1)[-1] or r.get("token")

    def qr_submit(self):
        tok = self.ids("qr").get("token")
        if not tok:
            raise RuntimeError("没有二维码")
        from fastapi.testclient import TestClient
        from app.services import quick as qsvc
        try:
            qsvc.LIMITER.reset()
        except Exception:
            pass
        c = TestClient(self.app, raise_server_exceptions=False, headers={"x-zb": "1"})
        when = datetime.now(TZ)
        b = self.photo_bytes("H11", when, "拌合站发电机房", "发电机")
        r = c.post(f"/q/{tok}/submit", files=[("files", ("H11.jpg", b, "image/jpeg"))], data={"note": "发电机旁边没有灭火器", "team": "拌合站班组", "name": "Musa", "cid": os.urandom(6).hex()})
        if r.status_code != 200:
            raise RuntimeError(f"扫码上报失败 {r.status_code} {r.text[:200]}")
        a = self.who("safe_b")
        subs = a.get("/api/quick/submissions")
        for s in (subs.get("items") if isinstance(subs, dict) else subs) or []:
            for ph in s.get("photos") or []:
                self.ids("qr_media")[str(ph["id"])] = "H11"
            if s.get("status") == "new" and not s.get("findings"):
                try:
                    a.post(f"/api/quick/submissions/{s['id']}/claim", json={})
                except Exception:
                    pass
        self.ids("media")["H11"] = int(next(iter(self.ids("qr_media")), 0) or 0)

    def recovery(self):
        self.who("pm").post("/api/schedule/recovery", json={})

    def output_records(self, monday: str):
        """财务口径的周产值（上周实际，按底账×单价，约 98%），工程主管导入。"""
        import openpyxl
        d0 = date.fromisoformat(monday) - timedelta(days=7)
        actual = 0.0
        for i in range(7):
            for works in self.truth.get((d0 + timedelta(days=i)).isoformat(), {}).values():
                for w in works:
                    actual += w.qty * self.S.PLAN_BY[w.code][6]
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["开始日期", "实际产值", "计划产值"])
        ws.append([d0.isoformat(), round(actual * 0.98, 2), None])
        buf = io.BytesIO()
        wb.save(buf)
        self.who("eng").post("/api/output/records", files={"file": ("周产值.xlsx", buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})

    # ---------------------------------------------------------- 调度器（按时手动调用）
    def sched(self, what: str):
        M = self.M
        if what == "morning":
            M._detect_bg(self.pid, None)
            M._daily_agents(self.pid)
            M._ai_retries()
        elif what == "reminders":
            M._due_reminders(self.pid)
        elif what == "evening":
            M._evening(self.pid)
        elif what in ("organize", "organize-yesterday"):
            # 逐人整理（一个人失败不影响其他人）：当天（周日周确认前、晚间失败后的补跑）或前一天（晚间整理失败的次日早上补整理）
            from app.db import all_, connect
            from app.services import logs as log_svc, site_agents
            day = date.today() - timedelta(days=1 if what == "organize-yesterday" else 0)
            conn = connect()
            try:
                for u in all_(conn, "SELECT * FROM users WHERE project_id=? AND role='tech' AND active=1", (self.pid,)):
                    try:
                        lg = log_svc.organize_day(conn, u, day.isoformat())
                        if lg:
                            site_agents.quality_from_log(conn, log_svc.get_log(conn, lg["id"]))
                    except Exception as exc:
                        log("  整理失败", u["username"], day, str(exc)[:120])
            finally:
                conn.close()
        elif what == "nudge":
            from app.services import nudge
            nudge.tick(now=datetime.now(TZ).replace(tzinfo=None))

    # ---------------------------------------------------------- 一天
    def day(self, d: date):
        S = self.S
        k = d.isoformat()
        E = []  # (时间, 键, 函数, 参数…)
        add = lambda hm, key, fn, *a: E.append((hm, f"{k}|{key}", fn, a))
        if d == S.START:
            add("06:30", "p6-baseline", self.p6_import, None)
        add("07:05", "sched-morning", self.sched, "morning")
        add("08:05", "sched-reminders", self.sched, "reminders")
        for hm in ("08:35", "12:35", "18:35"):
            add(hm, f"nudge-{hm}", self.sched, "nudge")
        for tech in S.TECHS:
            for i, n in enumerate(self.notes.get(f"{k}|{tech}", [])):
                p = n.get("photo")
                if p and next((x for x in S.PHOTOS if x[2] == p and x[0] == k and x[5] == "conceal"), None):
                    continue
                add(n["time"], f"note-{tech}-{i}", self.do_note, tech, k, i, n)
        # 分包安全员随手拍隐患（技术员随手记里拍到的隐患走归类建议）
        for ph in S.PHOTOS:
            if ph[0] == k and ph[5] == "hazard" and ph[1] in ("safe_a", "safe_b"):
                add(f"{self.rng.randint(9, 15):02d}:{self.rng.randint(0, 59):02d}", f"snap-{ph[2]}", self.snap_hazard, ph[2], k, "10:00")
            # 技术员当天拍的照片（含拍到的隐患）若随手记里没提到，补一条照片随手记
            if ph[0] == k and ph[5] != "conceal" and ph[1] in S.TECHS and not any(n.get("photo") == ph[2] for n in self.notes.get(f"{k}|{ph[1]}", [])):
                hm = f"{self.rng.randint(10, 17):02d}:{self.rng.randint(0, 59):02d}"
                add(hm, f"note-photo-{ph[2]}", self.do_note, ph[1], k, 90, {"time": hm, "mode": "photo", "text": ph[4], "photo": ph[2]})
        add("16:20", "rescan-failed", self.rescan_failed)
        add("16:40", "candidates", self.decide_candidates, k)
        # 整改
        for code, hz in S.HAZARDS.items():
            found = hz.get("date") or next((p[0] for p in S.PHOTOS if p[2] == code), None)
            if not found:
                continue
            fix_day = date.fromisoformat(found) + timedelta(days=hz["fix_days"] + hz.get("late", 0))
            if code == "H12":
                continue  # 到回放结束仍未整改
            if hz.get("first_fix_bad") and d == fix_day - timedelta(days=1):
                add("10:30", f"rectify-bad-{code}", self.rectify, code, k, "10:30", True)
            if d == fix_day:
                add("10:40", f"rectify-{code}", self.rectify, code, k, "10:40")
        add("17:30", "close-hazards", self.close_hazards, k)
        # 二维码
        if k == "2026-09-20":
            add("15:00", "qr-create", self.qr_create)
        if k == "2026-09-26":
            add("09:20", "qr-submit-H11", self.qr_submit)
        # 单据
        for j, it in enumerate(self.docs):
            if it["date"] == k:
                hm = "11:30" if it["kind"] != "concrete_ticket" else f"{int(it['doc']['issued_at'][11:13]):02d}:{min(59, int(it['doc']['issued_at'][14:16]) + 25):02d}"
                add(hm, f"ticket-{it['file']}", self.tickets, it, hm)
        # 隐蔽验收
        for x in S.CONCEAL.get(k, []):
            add("14:10", f"conceal-{x[1]}", self.conceal, k, *x)
        for key, note in S.CONCEAL_RESULT.get(k, []):
            add("10:10", f"conceal-result-{key}", self.conceal_result, key, note)
        # 港湾侧
        if k == "2026-09-10":
            add("15:00", "boq", self.boq)
        if k == "2026-09-11":
            add("10:00", "boq-confirm", self.confirm_maps)
        if k == "2026-09-26":
            add("15:30", "gate-1", self.gate, ["RD120", "CV120"])
        if k == "2026-10-03":
            add("15:30", "gate-2", self.gate, ["RD210"], {"RD210": "K1+600~K2+300 段社区占道尚未协调完毕"})
        if d.weekday() == 0 and d > S.START:
            prev = (d - timedelta(days=7)).isoformat()
            for tech in S.TECHS:  # 上周周确认没交上（模型超时等）：周一早上补交
                if f"{prev}|{tech}" not in self.ids("week_log") and not any(k[0] == (d - timedelta(days=1)).isoformat() and k[1] == tech for k in S.LATE_WEEK):
                    add("08:40", f"week-catchup-{tech}", self.week_confirm_if_missing, tech, prev)
            add("09:00", "eng-review", self.eng_review, prev)
            add("09:40", "report-week", self.report, "week", prev)
            add("10:20", "meeting-items-done", self.meeting_items_done)
            add("15:30", "report-publish", self.publish, "week", prev)
            add("11:00", "output-records", self.output_records, k)
        if k in S.MEETINGS:
            add("10:30", "meeting", self.meeting, k, S.MEETINGS[k])
        if k == "2026-10-01":
            add("16:00", "report-month", self.report, "month", "2026-09-01")
        if k == "2026-10-02":
            add("11:00", "report-month-publish", self.publish, "month", "2026-09-01")
        if k == "2026-09-28":
            add("11:40", "recovery", self.recovery)
        for qd, who, q in S.QUESTIONS:
            if qd == k:
                add(f"{self.rng.randint(9, 17):02d}:{self.rng.randint(0, 59):02d}", f"ask-{q[:8]}", self.ask, who, q)
        for cd, who, t in S.COMMANDS:
            if cd == k:
                add("10:50", f"cmd-{t[:8]}", self.command, who, t)
        for ad, who, t in S.ANNOUNCEMENTS:
            if ad == k:
                add("07:40", f"notice-{who}", self.announce, who, t)
        add("10:05", "alerts-feedback", self.alerts_feedback)
        add("18:10", "quality", self.quality_tick, k)
        add("12:45", "circle-noon", self.circle, k)
        add("21:15", "circle-night", self.circle, k)
        add("20:00", "sched-evening", self.sched, "evening")
        add("21:30", "sched-organize-retry", self.sched, "organize")   # 晚间整理若有人因模型超时没整理出来，逐人补跑（已整理的会跳过）
        add("06:50", "sched-organize-yesterday", self.sched, "organize-yesterday")
        # 周日：周确认 + P6 更新
        if d.weekday() == 6:
            monday = (d - timedelta(days=6)).isoformat()
            add("18:40", "sched-organize", self.sched, "organize")
            for j, tech in enumerate(S.TECHS):
                late = S.LATE_WEEK.get((k, tech))
                if late:
                    continue
                add(f"{19 + j // 2}:{10 + 20 * (j % 2):02d}", f"week-{tech}", self.week_confirm, tech, monday)
            add("22:40", "p6-update", self.p6_import, d + timedelta(days=1))
        for (sun, tech), when in S.LATE_WEEK.items():
            if when[:10] == k:
                add(when[11:16], f"week-late-{tech}", self.week_confirm, tech, (date.fromisoformat(sun) - timedelta(days=6)).isoformat())
        for hm, key, fn, a in sorted(E, key=lambda e: e[0]):
            self.run(key, self.dt(k, hm), fn, *a)


def reset(data_dir: Path) -> None:
    """删除新仿真项目（LEKKI-SIM2）的全部业务数据与 sim_* 账号，以便补齐素材后重新回放；不动其它项目。"""
    from app.db import connect, init_db
    from . import scenario as S
    init_db()
    conn = connect()
    try:
        p = conn.execute("SELECT id FROM projects WHERE code=?", (S.PROJECT["code"],)).fetchone()
        if not p:
            log("没有新仿真项目，无需重置")
            return
        pid = p[0]
        conn.execute("PRAGMA foreign_keys=OFF")
        imps = [r[0] for r in conn.execute("SELECT id FROM p6_imports WHERE project_id=?", (pid,)).fetchall()]
        for t in ("activities", "relations"):
            for i in imps:
                conn.execute(f"DELETE FROM {t} WHERE import_id=?", (i,))
        users = [r[0] for r in conn.execute("SELECT id FROM users WHERE project_id=?", (pid,)).fetchall()]
        for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            cols = [c[1] for c in conn.execute(f"PRAGMA table_info({t})").fetchall()]
            if t not in ("projects", "users") and "project_id" in cols:
                conn.execute(f"DELETE FROM {t} WHERE project_id=?", (pid,))
        for uid in users:  # 无 project_id 的从表（会话、个人设置等）
            for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                cols = [c[1] for c in conn.execute(f"PRAGMA table_info({t})").fetchall()]
                if t != "users" and "project_id" not in cols and "user_id" in cols:
                    conn.execute(f"DELETE FROM {t} WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM users WHERE project_id=?", (pid,))
        conn.execute("DELETE FROM projects WHERE id=?", (pid,))
        conn.execute("PRAGMA foreign_keys=ON")
    finally:
        conn.close()
    st = data_dir / "sim_replay_state.json"
    if st.exists():
        st.rename(data_dir / f"sim_replay_state.{datetime.now():%Y%m%d-%H%M%S}.old.json")
    log("已删除新仿真项目", pid)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=str(ROOT / "data"))
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--to", default="2026-10-05")
    ap.add_argument("--reset", action="store_true", help="先删除新仿真项目（先自动备份数据库），再从头回放")
    ap.add_argument("--organize-each-note", action="store_true", help="每条随手记后都让 AI 重整当日日志（与产品一致，但慢约 3 倍）")
    ap.add_argument("--ai-timeout", type=float, default=360, help="本回放进程内的单次 AI 调用超时秒数（不改系统设置；高峰期长请求常超过默认 180 秒）")
    a = ap.parse_args()
    data_dir = Path(a.data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    db = data_dir / "zhimou.db"
    if db.exists() and (a.reset or not (data_dir / "sim_replay_state.json").exists()):
        bak = data_dir / f"zhimou.before-sim2.{datetime.now():%Y%m%d-%H%M}.db.bak"
        shutil.copy2(db, bak)
        log("已备份数据库 →", bak.name)
    setup_env(data_dir, a.mock)
    sys.path.insert(0, str(ROOT))
    if a.reset:
        reset(data_dir)
    eng = Engine(data_dir, a.mock, a.organize_each_note)
    from app.ai import pool
    pool.timeout = lambda: float(a.ai_timeout)   # 只影响本进程
    eng.at(datetime(2026, 9, 6, 18, 0, tzinfo=TZ))
    eng.setup_project()
    log("项目", eng.pid, "账号", eng.uids)
    d = eng.S.START
    end = date.fromisoformat(a.to)
    while d <= end:
        log("====", d, "====")
        eng.day(d)
        d += timedelta(days=1)
    log("完成。错误数：", len(eng.st["errors"]))


if __name__ == "__main__":
    main()
