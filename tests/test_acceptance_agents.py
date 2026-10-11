"""AI 智能体专项验收（集成）：逐个 Agent 走“触发 → 取数 → 模型（client.call_json）→ 守卫 → 降级 → 落库 → 角色可见”。

全部离线、合成数据：
- 正常路径用离线模拟模型（provider=mock），核对 ai_runs 留痕与业务表落库；
- 模型故障 / 输出不合格 / 自造数字：把 httpx 换成可编程的假“交融”响应（同 test_ai_reliability 的做法），
  真实经过 client 的重试、修复、降级与 ai_runs 留痕，不联网、不调用真实模型；
- 已有用例已覆盖的同一断言不重复（见各节注释里的“已覆盖”）。

真实缺陷用 xfail(strict=True, reason="BUG: …") 标出，不在用例里绕过。
"""
import json
import re
from pathlib import Path

import pytest
from conftest import TODAY, confirm_hazards, login
from test_doc_debt import _finish_a1010
from test_site import D, _world
from test_slow_diag import _alert

from app import db as dbmod
from app.agents import AGENTS
from app.ai import client, pool
from app.ai import docs_registry as reg
from app.ai import qa_agent, report_agent
from app.config import settings
from app.services import ai_trace
from app.services import digest as digest_svc
from app.services import findings as find_svc
from app.services import gongbi_docs  # noqa: F401  注册全部工笔文书类型（含增补）
from app.services import qa as qa_svc
from app.services import site_agents

ROOT = Path(__file__).resolve().parent.parent
MARK = "987654"                       # 事实里不可能出现的数字：模型写了它就是自造
SK = "sk-test-key-acceptance-0001"
GARBAGE = "抱歉，我暂时无法按要求完成。"


# ============================================================================ 公共：数据与假模型

def _full_world(env):
    """test_site 的合成世界（甲/乙两家分包、P6、日志、隐患、质控）+ 预控用的吊装/导改作业 + A1010 完工（资料欠账）。"""
    ids = _world(env)
    conn = dbmod.connect()
    conn.execute("UPDATE activities SET name='3#墩预制梁吊装' WHERE code='A1030'")
    conn.execute("UPDATE activities SET name='路基段夜间交通导改' WHERE code='A3020'")
    find_svc.create(conn, env["project_id"], "safety", "hazard", "high", "吊装作业警戒缺失", {"category": "吊装"}, fingerprint="t:acc1",
                    assignee_id=env["users"]["tech1"], due_days=2)
    conn.close()
    confirm_hazards()
    _finish_a1010(env)
    return ids


def _runs(prefix: str) -> list[dict]:
    conn = dbmod.connect()
    try:
        return dbmod.all_(conn, "SELECT * FROM ai_runs WHERE kind LIKE ? ORDER BY id", (prefix + "%",))
    finally:
        conn.close()


def _one(sql: str, args=()):
    conn = dbmod.connect()
    try:
        return dbmod.one(conn, sql, args)
    finally:
        conn.close()


class _Resp:
    def __init__(self, content: str):
        self.status_code, self.text, self.headers = 200, "", {}
        self._body = {"choices": [{"message": {"content": content}, "finish_reason": "stop"}], "usage": {"total_tokens": 10}}

    def json(self):
        return self._body


@pytest.fixture()
def model(env, monkeypatch):
    """假“交融”模型：st["reply"](system, payload) 返回字符串（模型输出）或异常实例（网络故障）。默认输出不合格的自然语言。"""
    monkeypatch.setattr(settings, "ai_provider", "jiaorong")
    conn = dbmod.connect()
    pool.add_key(conn, SK, "ACC", ["vision", "core", "agent", "asr", "admin"])
    conn.close()
    st = {"reply": lambda system, payload: GARBAGE, "calls": [], "env": env}

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None, **kw):
            msgs = (json or {}).get("messages") or []
            first = msgs[0]["content"] if msgs else ""
            system = first if isinstance(first, str) else str(first[0].get("text", "")) if first else ""
            st["calls"].append(system[:40])
            out = st["reply"](system, json)
            if isinstance(out, Exception):
                raise out
            return _Resp(out)

    monkeypatch.setattr(client.httpx, "Client", FakeClient)
    monkeypatch.setattr(client, "_sleep", lambda s: None)
    pool._cool.clear()
    pool._cfg_cache["val"] = None
    yield st
    pool._cool.clear()


def _is_repair(system: str) -> bool:
    return system.startswith("你是JSON格式修复器")


# ============================================================================ (f) 界面入口都有后端接口；卡片墙与注册表一致

def _static_api_urls() -> set[tuple[str, str]]:
    out = set()
    for f in (ROOT / "app" / "static").glob("*.js"):
        s = f.read_text(encoding="utf-8")
        for m in re.finditer(r"""([`'"])(/api/[^`'"\s]*)\1""", s):
            out.add((f.name, re.sub(r"\$\{[^}]*\}", "X", m.group(2)).split("?")[0]))
    return out


def test_every_frontend_api_url_has_backend_route():
    """前端（app/static/*.js）引用的每个 /api/ 地址都能对上一个已注册的后端路由：没有“只有界面没有后端”的智能体入口。"""
    from app.main import app
    paths = [r.path for r in app.routes if getattr(r, "path", "").startswith("/api/")]
    route_rx = [re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", p) + "$") for p in paths]
    as_x = [re.sub(r"\{[^}]+\}", "X", p) for p in paths]
    missing = []
    for fn, u in sorted(_static_api_urls()):
        if fn == "sw.js" and u == "/api/":          # Service Worker 的接口前缀判断，不是调用
            continue
        if u.endswith("/"):                          # 前缀拼接（如 "/api/media/" + id + "/thumb"）
            ok = any(p.startswith(u) for p in paths)
        else:
            url_rx = re.compile("^" + re.escape(u).replace("X", "[^/]+") + "$")
            ok = any(rx.match(u) for rx in route_rx) or any(url_rx.match(p) for p in as_x)
        if not ok:
            missing.append((fn, u))
    assert not missing, missing


def test_agent_card_wall_matches_registry():
    """智能体卡片墙（mgr.js AGENT_GROUPS）、问答范围文字（views.js ASK_SCOPE）与 agents.py / qa.SCOPES 同一份清单。
    （每张卡的“提问”都能打到 /api/ask 已由 test_ask_agents 覆盖。）"""
    mgr = (ROOT / "app/static/mgr.js").read_text(encoding="utf-8")
    views = (ROOT / "app/static/views.js").read_text(encoding="utf-8")
    keys = {a["key"] for a in AGENTS}
    assert len(AGENTS) == 16
    block = mgr[mgr.index("const AGENT_GROUPS"):mgr.index("];", mgr.index("const AGENT_GROUPS"))]
    grouped = set(re.findall(r'"([a-z]+)"', " ".join(re.findall(r"keys:\s*\[([^\]]*)\]", block))))
    assert grouped == keys, (grouped ^ keys)
    scope_block = views[views.index("const ASK_SCOPE"):views.index("};", views.index("const ASK_SCOPE"))]
    assert set(re.findall(r"^\s*([a-z]+):", scope_block, flags=re.M)) == set(qa_svc.SCOPES)


def _code_kinds() -> set[str]:
    """代码里实际发出的模型调用类型（call_json 的第一个参数；f-string 按注册表展开）。"""
    kinds = {"asr"}
    for f in (ROOT / "app").rglob("*.py"):
        s = f.read_text(encoding="utf-8")
        for m in re.finditer(r"call_json\(\s*(f?)\"([a-z_.{}]+)\"", s):
            k = m.group(2)
            if "{" not in k:
                kinds.add(k)
            elif k == "digest.{kind}":
                kinds |= {f"digest.{x}" for x in digest_svc.KINDS}
            elif k == "report.{kind}":
                kinds |= {f"report.{x}" for x in ("week", "month")} | {f"report.{x}" for x, t in reg.TYPES.items() if not t.get("legacy")}
            else:
                raise AssertionError(f"未识别的动态调用类型：{k}（{f.name}）")
    return kinds


def test_every_registered_agent_kind_is_really_called():
    """agents.py 每个 Agent 登记的调用类型都确有代码发出（没有只在卡片上存在的 Agent）。"""
    kinds = _code_kinds()
    dead = {a["key"]: [k for k in a["kinds"] if k not in kinds] for a in AGENTS}
    assert not {k: v for k, v in dead.items() if v}, dead
    assert all(any(k in kinds for k in a["kinds"]) for a in AGENTS)


def test_every_called_kind_is_attributed_to_an_agent():
    unmapped = sorted(k for k in _code_kinds() if not k.startswith("ai.") and not ai_trace.agent_of_kind(k))
    assert not unmapped, unmapped


# ============================================================================ 工笔文书（11 类：英文周/月报、索赔通知、延误简报、联系单、致分包函、
# 例会底稿、三周滚动、月度复盘、预控提醒、资料补齐通知）——同一条起草流水线 gongbi_docs.generate

GONGBI = {   # kind -> 起草岗位
    "weekly_en": "eng", "monthly_en": "eng", "notice_of_claim": "eng", "delay_brief": "eng",
    "work_contact": "ss", "letter_to_sub": "ss", "meeting_pack": "eng", "three_week_plan": "eng",
    "monthly_review": "eng", "precontrol_notice": "ss", "docdebt_notice": "eng",
}


def test_gongbi_kinds_list_is_complete():
    assert set(GONGBI) == {k for k, t in reg.TYPES.items() if not t.get("legacy")}


def _ref(c, kind: str) -> str:
    rt = reg.TYPES[kind]["ref"]
    if rt in ("week", "month"):
        return D(3)
    refs = c.get(f"/api/gongbi/refs?kind={kind}").json()
    assert refs, f"{kind} 没有可选对象（合成世界缺数据）"
    if kind == "precontrol_notice":
        return next(r["key"] for r in refs if r["key"].startswith("A1030|"))
    if kind == "docdebt_notice":
        return next(r["key"] for r in refs if r["key"] == "A1010|甲")
    return refs[0]["key"]


def _draft(kind: str):
    c = login(GONGBI[kind])
    r = c.post("/api/gongbi/docs", json={"kind": kind, "ref": _ref(c, kind)})
    assert r.status_code == 200, (kind, r.status_code, r.text[:300])
    return r.json()


@pytest.mark.parametrize("kind", sorted(GONGBI))
def test_gongbi_mock_happy_path_persists_and_traces(env, kind):
    """(a) 模拟模型：文书落库（gongbi_docs），ai_runs 记 report.<kind>、provider=mock；模拟模型的文字如实标为规则版；规则版过数字守卫。"""
    _full_world(env)
    d = _draft(kind)
    row = _one("SELECT * FROM gongbi_docs WHERE id=?", (d["id"],))
    run = _one("SELECT * FROM ai_runs WHERE id=?", (row["ai_run_id"],))
    assert run and run["kind"] == f"report.{kind}" and run["provider"] == "mock" and run["ok"] == 1
    assert row["status"] == "draft" and d["source"] == "rule" and "离线模拟" in d["note"]
    assert not [x for x in d["check"] if x["issue"] == "数字未在事实中找到"], d["check"]


@pytest.mark.parametrize("kind", sorted(GONGBI))
def test_gongbi_model_failure_degrades_to_rule(model, kind):
    """(b) 模型输出不合格（修复一次仍不合格）：不 500，改用规则版并写明原因；ai_runs 记失败；文书照样落库且不挂 AI 留痕。"""
    _full_world(model["env"])
    d = _draft(kind)
    assert d["source"] == "rule" and "模型暂不可用" in d["note"]
    run = [r for r in _runs(f"report.{kind}") if r["provider"] == "jiaorong"][-1]
    assert run["ok"] == 0 and run["error"]
    assert _one("SELECT ai_run_id FROM gongbi_docs WHERE id=?", (d["id"],))["ai_run_id"] is None
    assert len(model["calls"]) >= 2 and any(_is_repair(s) for s in model["calls"])   # 首次 + 一次修复，有界


def _all_sections_with(kind: str, text: str) -> str:
    out = {}
    for s in reg.template(kind)["sections"]:
        out[s["key"]] = [text] if s["type"] == "list" else text
    return json.dumps(out, ensure_ascii=False)


@pytest.mark.parametrize("kind", sorted(GONGBI))
def test_gongbi_number_guard_rejects_invented_numbers(model, kind):
    """(c)(d) 模型每一栏都写了事实里没有的数字：逐栏退回规则版，最终文字与表格里都没有自造数字，叙述过数字守卫。"""
    _full_world(model["env"])
    en = reg.TYPES[kind]["lang"] == "en"
    model["reply"] = lambda system, payload: _all_sections_with(kind, f"Total {MARK} items." if en else f"本期共完成 {MARK} 项。")
    d = _draft(kind)
    assert MARK not in json.dumps(d["narrative"], ensure_ascii=False), d["narrative"]
    assert MARK not in json.dumps(d["tables"], ensure_ascii=False)
    assert d["fallback"] and "换成规则版" in d["note"]   # 提示写栏目标题（验收 I17）
    assert not [x for x in d["check"] if x["issue"] == "数字未在事实中找到"], d["check"]
    run = _one("SELECT * FROM ai_runs WHERE id=(SELECT ai_run_id FROM gongbi_docs WHERE id=?)", (d["id"],))
    assert run["provider"] == "jiaorong" and run["ok"] == 1 and MARK in run["output_text"]   # 留痕保留模型原文，界面显示的是核对后的文字


# ============================================================================ 周/月报（报表Agent，旧流水线 reports.generate）
# 已覆盖：模板栏目/超字数核验（test_agents）、项目经理口径裁产值（test_pm_scope）

def test_report_mock_happy_path(env):
    _full_world(env)
    r = login("pm").post("/api/reports", json={"kind": "week", "ref_date": D(3)})
    assert r.status_code == 200, r.text
    rep = r.json()
    run = _one("SELECT * FROM ai_runs WHERE id=?", (rep["ai_run_id"],))
    assert run["kind"] == "report.week" and run["provider"] == "mock" and run["ok"] == 1
    assert _one("SELECT status FROM reports WHERE id=?", (rep["id"],))["status"] == "draft"
    assert not [x for x in rep["number_check"] if x.get("number")]          # 模拟稿只用事实数字


def test_report_model_failure_is_explicit_error_not_fabricated(model):
    """(b) 报表Agent 没有降级稿：模型不合格 → 502 明确报错，不落一份空/假的报表。"""
    _full_world(model["env"])
    n0 = _one("SELECT COUNT(*) n FROM reports")["n"]
    r = login("pm").post("/api/reports", json={"kind": "week", "ref_date": D(3)})
    assert r.status_code == 502 and "AI 调用失败" in r.json()["detail"]
    assert _one("SELECT COUNT(*) n FROM reports")["n"] == n0
    assert _runs("report.week")[-1]["ok"] == 0


def test_report_number_guard_flags_invented_numbers(model):
    """(c) 数字守卫：模型写了统计里没有的数字 → 每栏在 number_check 里点名该数字（发布前人逐条核实，界面据此禁用“审核发布”）。"""
    _full_world(model["env"])
    tpl = report_agent.default_template("week")
    model["reply"] = lambda system, payload: json.dumps(
        {s["key"]: ([f"协调 {MARK} 项"] if s["type"] == "list" else f"本周完成 {MARK} 根") for s in tpl["sections"]}, ensure_ascii=False)
    rep = login("eng").post("/api/reports", json={"kind": "week", "ref_date": D(3)}).json()
    flagged = {x["field"] for x in rep["number_check"] if x.get("number") == MARK}
    assert {s["key"] for s in tpl["sections"]} <= flagged


# ============================================================================ 问工笔（/api/ask：qa.plan → 取数 → qa.answer）
# 已覆盖：规划不合格降级为规则检索（test_ai_reliability）、各 Agent 问答范围与岗位（test_ask_agents、test_pm_scope）、
#        纯文本回答降级与半截 JSON 拒绝（test_ai_reliability）

def test_ask_mock_traces_plan_and_answer(env):
    _full_world(env)
    r = login("pm").post("/api/ask", json={"question": "3#墩桩基最近有什么问题？"})
    assert r.status_code == 200 and r.json()["refs"]
    assert [x["provider"] for x in _runs("qa.")][-2:] == ["mock", "mock"]
    assert {x["kind"] for x in _runs("qa.")} == {"qa.plan", "qa.answer"}


def test_ask_network_failure_is_explicit_error(model):
    import httpx
    _full_world(model["env"])
    model["reply"] = lambda system, payload: httpx.ConnectError("down")
    r = login("pm").post("/api/ask", json={"question": "3#墩桩基最近有什么问题？"})
    assert r.status_code == 502 and "AI 调用失败" in r.json()["detail"]
    assert _runs("qa.plan")[-1]["ok"] == 0


def test_ask_garbage_output_is_marked_degraded(model):
    _full_world(model["env"])
    r = login("pm").post("/api/ask", json={"question": "3#墩桩基最近有什么问题？"})
    assert r.status_code == 200
    d = r.json()
    assert d["degraded"] is True and d["refs"] == []


def test_ask_answer_refs_must_come_from_retrieved_records(model):
    _full_world(model["env"])

    def reply(system, payload):
        if system.startswith(qa_agent.PLAN_SYSTEM[:20]):
            return json.dumps({"date_from": "", "date_to": "", "activity_codes": ["A1010"], "keywords": ["桩基"], "need": ["logs"]})
        return json.dumps({"answer": f"3#墩桩基已完成 {MARK} 根 [L99999]", "refs": ["L99999"]}, ensure_ascii=False)
    model["reply"] = reply
    d = login("pm").post("/api/ask", json={"question": "3#墩桩基完成了多少？"}).json()
    assert "L99999" not in d["refs"] and "L99999" not in d["answer"]
    assert MARK not in d["answer"]


# ============================================================================ AI 小结（/api/digest）
# 已覆盖：check() 单元、缓存、项目经理不能对日志列表做小结（test_digest）

DIGEST_FACTS = {"lead": "未闭环安全隐患共 3 项", "key": ["F12：3#墩临边防护缺失（超期 2 天）"], "groups": ["临边：2 项（F12、F15）", "用电：1 项（F18）"]}


def _digest():
    r = login("pm").post("/api/digest", json={"kind": "safety", "facts": DIGEST_FACTS, "fallback": "规则归并文字"})
    assert r.status_code == 200, r.text
    return r.json()


def test_digest_mock_happy_path(env):
    d = _digest()
    assert d["provider"] == "mock" and d["run_id"] and "规则归并文字" != d["text"]
    run = _one("SELECT * FROM ai_runs WHERE id=?", (d["run_id"],))
    assert run["kind"] == "digest.safety" and run["provider"] == "mock"
    assert d["source"] == "mock" and _one("SELECT COUNT(*) n FROM digest_cache")["n"] == 0   # mock 标注统一；只缓存真实模型成功


def test_digest_model_failure_returns_rule_text(model):
    d = _digest()
    assert d["source"] == "rule" and d["text"] == "规则归并文字" and "AI 暂不可用" in d["note"]
    assert _one("SELECT COUNT(*) n FROM digest_cache")["n"] == 0          # 失败不缓存，下次重新试


def test_digest_invented_number_falls_back(model):
    model["reply"] = lambda system, payload: json.dumps({"text": f"先处理 F12，另有 {MARK} 项可合并"}, ensure_ascii=False)
    d = _digest()
    assert d["source"] == "rule" and d["text"] == "规则归并文字" and MARK in d["note"]


def test_digest_numbers_from_facts_pass(model):
    model["reply"] = lambda system, payload: json.dumps({"text": "先处理 F12（超期 2 天）；临边 2 项可合并一次处理。"}, ensure_ascii=False)
    d = _digest()
    assert d["source"] == "ai" and "F12" in d["text"]
    assert _digest().get("cached")                                        # 真实模型成功的才缓存


# ============================================================================ 工效诊断（/api/alerts/{id}/slow-diag，progress.slowdiag）
# 已覆盖：信号统计、guard() 单元、接口与岗位、赶工提示词引用（test_slow_diag）

def _slow(env, user="pm"):
    _world(env)
    aid = _alert(env)
    r = login(user).get(f"/api/alerts/{aid}/slow-diag?run=1")   # 点“AI 归因”才调用模型（验收 I9）
    assert r.status_code == 200, r.text
    return r.json()


def test_slowdiag_mock_traced_but_rule_text(env):
    d = _slow(env)
    assert d["source"] == "mock" and "离线模拟" in d["note"]   # 模拟模型的输出标 mock，不当作 AI 归因
    assert _runs("progress.slowdiag")[-1]["provider"] == "mock"
    assert _one("SELECT COUNT(*) n FROM slowdiag_cache")["n"] == 0


def test_slowdiag_model_failure_rule(model):
    d = _slow(model["env"])
    assert d["source"] == "rule" and "AI 暂不可用" in d["note"] and d["text"].startswith("主因")
    assert _one("SELECT COUNT(*) n FROM slowdiag_cache")["n"] == 0        # 失败不缓存


def test_slowdiag_invented_number_or_ref_rejected(model):
    model["reply"] = lambda system, payload: json.dumps({"text": f"主因设备故障，{MARK} 天日志提到 [L99999]"}, ensure_ascii=False)
    d = _slow(model["env"])
    assert d["source"] == "rule" and "未通过核对" in d["note"] and MARK not in d["text"] and "L99999" not in d["text"]


def test_slowdiag_guarded_ai_text_accepted(model):
    model["reply"] = lambda system, payload: json.dumps({"text": "主因是旋挖钻设备故障，日志有记录。"}, ensure_ascii=False)
    d = _slow(model["env"])
    assert d["source"] == "ai"


# ============================================================================ 分包履约画像（/api/sub-profiles，sub.profile）
# 已覆盖：统计口径、项目经理无日志口径、岗位、guard() 单元（test_sub_profile）

def _profiles(env):
    _world(env)
    r = login("eng").get("/api/sub-profiles?run=1")   # 点“AI 评语”才调用模型（验收 I9）
    assert r.status_code == 200, r.text
    return {p["company"]: p for p in r.json()["items"]}


def test_subprofile_model_failure_rule(model):
    for p in _profiles(model["env"]).values():
        assert p["eval"]["source"] == "rule" and "AI 暂不可用" in p["eval"]["note"]
    assert _one("SELECT COUNT(*) n FROM subprofile_cache")["n"] == 0      # 失败不缓存


def test_subprofile_invented_number_rejected(model):
    model["reply"] = lambda system, payload: json.dumps({"text": f"该单位近 30 天隐患 {MARK} 项，需加强。"}, ensure_ascii=False)
    for p in _profiles(model["env"]).values():
        assert p["eval"]["source"] == "rule" and MARK not in p["eval"]["text"] and "未通过核对" in p["eval"]["note"]


def test_subprofile_guarded_ai_text_accepted_and_traced(model):
    model["reply"] = lambda system, payload: json.dumps({"text": "安全问题需持续跟进闭环。"}, ensure_ascii=False)
    ps = _profiles(model["env"])
    assert all(p["eval"]["source"] == "ai" for p in ps.values())
    assert {r["provider"] for r in _runs("sub.profile")} == {"jiaorong"} and len(_runs("sub.profile")) == len(ps)


# ============================================================================ 高风险作业预控（/api/precontrol，safety.precontrol）
# 已覆盖：风险归类、卡片历史、岗位、提醒下发与签收（test_precontrol）

def _precontrol(env):
    _full_world(env)
    r = login("ss").get("/api/precontrol")
    assert r.status_code == 200, r.text
    return r.json()


def test_precontrol_model_failure_rule(model):
    d = _precontrol(model["env"])
    assert d["items"] and d["source"] == "rule" and "AI 暂不可用" in d["note"]
    assert all(x["focus_source"] == "rule" for x in d["items"])
    assert _one("SELECT COUNT(*) n FROM precontrol_cache")["n"] == 0      # 失败不缓存


def test_precontrol_invented_number_rejected(model):
    def reply(system, payload):
        rows = json.loads(payload["messages"][1]["content"])
        return json.dumps({"items": [{"code": x["code"], "company": x["company"], "focus": f"近 {MARK} 次同类隐患，盯警戒"} for x in rows]}, ensure_ascii=False)
    model["reply"] = reply
    d = _precontrol(model["env"])
    assert d["source"] == "ai" and "统计中没有的数字" in d["note"]
    assert _one("SELECT run_id FROM precontrol_cache")["run_id"]          # 缓存记下对应的 AI 调用，可追溯
    assert all(x["focus_source"] == "rule" and MARK not in x["focus"] for x in d["items"])


# ============================================================================ 简报（/api/brief，brief.daily）
# 已覆盖：与底稿矛盾回退模板（test_facts）、零样本不下结论（test_ai_consistency）、项目经理口径（test_pm_scope）、失效重算（test_brief_stale）

def test_brief_mock_happy_path(env):
    _full_world(env)
    b = login("pm").post("/api/brief").json()
    row = _one("SELECT * FROM briefs ORDER BY id DESC LIMIT 1")
    assert row["ai_run_id"] and _one("SELECT provider FROM ai_runs WHERE id=?", (row["ai_run_id"],))["provider"] == "mock"
    assert b["headline"]


def test_brief_model_failure_says_unavailable_not_fabricated(model):
    _full_world(model["env"])
    r = login("pm").post("/api/brief")
    assert r.status_code == 200
    b = r.json()
    assert b["headline"].startswith("简报Agent暂不可用") and b["items"] == []
    assert _one("SELECT ai_run_id FROM briefs ORDER BY id DESC LIMIT 1")["ai_run_id"] is None


def test_brief_invented_lag_days_fall_back_to_template(model):
    """事实底稿核对：写了底稿里没有的滞后天数 → 重试一次仍不过 → 系统模板，数字全部来自 KPI。"""
    _full_world(model["env"])
    model["reply"] = lambda system, payload: json.dumps({"headline": f"关键作业滞后 {MARK} 天", "items": [
        {"level": "high", "area": "进度", "title": "A1010 滞后", "detail": f"滞后 {MARK} 天", "action": "赶工"}]}, ensure_ascii=False)
    b = login("pm").post("/api/brief").json()
    shown = json.dumps({"headline": b["headline"], "items": b["items"]}, ensure_ascii=False)
    assert b["source"] == "template" and MARK not in shown and MARK in b["note"]      # 正文无自造数字；note 如实写明被拒原因
    assert len([r for r in _runs("brief.daily") if r["provider"] == "jiaorong"]) == 2       # 首次 + 带原因重试一次


# ============================================================================ 预警分析（/api/alerts/{id}/explain，alert.explain）
# 已覆盖：矛盾重试后模板（test_facts）、判定快照过期、措施期限/责任岗位（test_ai_consistency）

def test_alert_explain_mock_happy_path(env):
    _world(env)
    aid = _alert(env)
    r = login("pm").post(f"/api/alerts/{aid}/explain")
    assert r.status_code == 200, r.text
    row = _one("SELECT explanation_json, ai_run_id FROM alerts WHERE id=?", (aid,))
    assert row["explanation_json"] and _one("SELECT provider FROM ai_runs WHERE id=?", (row["ai_run_id"],))["provider"] == "mock"


def test_alert_explain_model_failure_is_explicit_and_keeps_alert_clean(model):
    env = model["env"]
    _world(env)
    aid = _alert(env)
    r = login("pm").post(f"/api/alerts/{aid}/explain")
    assert r.status_code == 502
    d = r.json()   # 不用模板兜底，原因说到位：模型有问题、没生成内容、具体原因与下一步
    assert "模型有问题" in d["detail"] and "不会用模板代替" in d["detail"] and "原因：" in d["detail"] and d["ai_error"]
    assert _one("SELECT explanation_json FROM alerts WHERE id=?", (aid,))["explanation_json"] is None
    assert _runs("alert.explain")[-1]["ok"] == 0


# ============================================================================ 赶工方案（/api/schedule/recovery，progress.recovery）
# 已覆盖：金额/工效口径不过回退模板（test_ai_consistency）、岗位（test_agents）

def test_recovery_model_failure_is_explicit_and_not_saved(model):
    _world(model["env"])
    r = login("pm").post("/api/schedule/recovery")
    assert r.status_code == 502
    assert _one("SELECT 1 x FROM knowledge WHERE kind='recovery'") is None


def test_recovery_mock_days_are_recomputed_by_cpm(env):
    """(d) 工期效果由系统 CPM 复算（verified），模型不给日期。"""
    _world(env)
    rec = login("pm").post("/api/schedule/recovery").json()
    assert rec["verified"]["measures"] and "复算" in rec["note"]
    assert _one("SELECT provider FROM ai_runs WHERE id=?", (rec["ai_run_id"],))["provider"] == "mock"


# ============================================================================ 数据审核Agent（dq.review）

def test_dq_review_model_failure_keeps_rule_conclusion(model):
    env = model["env"]
    ids = _world(env)
    conn = dbmod.connect()
    try:
        dq = site_agents.dq_review(conn, ids["m"], env["users"]["admin"])
    finally:
        conn.close()
    assert "AI 预审未完成" in dq["summary"] and dq.get("source") == "rule" and "{" not in dq["summary"] and dq["risk"] in ("low", "medium", "high") and "score" in dq
    assert _runs("dq.review")[-1]["ok"] == 0


# ============================================================================ (e) 分包岗位不能触发项目级 AI 产出
# 已覆盖：项目经理口径的各条（test_pm_scope、test_ask_agents、test_digest）、分包本单位可见（test_circle_picks、test_precontrol、test_sub_letters）

SUB_FORBIDDEN = [
    ("post", "/api/ask", {"question": "进度怎么样"}),
    ("post", "/api/brief", None),
    ("post", "/api/reports", {"kind": "week", "ref_date": TODAY.isoformat()}),
    ("post", "/api/gongbi/docs", {"kind": "weekly_en", "ref": TODAY.isoformat()}),
    ("post", "/api/schedule/recovery", None),
    ("post", "/api/plan/health/agent", None),
    ("post", "/api/output/analyze", None),
    ("post", "/api/quality/reminders", None),
    ("post", "/api/cmd/minutes", {"text": "例会：3#墩钢筋周五前补齐"}),
    ("get", "/api/sub-profiles", None),
    ("get", "/api/precontrol", None),
    ("get", "/api/doc-debt", None),
]


@pytest.mark.parametrize("who", ["tech1", "sa_a"])
def test_subcontractor_roles_cannot_trigger_project_ai(env, who):
    _world(env)
    aid = _alert(env)
    c = login(who)
    calls = SUB_FORBIDDEN + [("post", f"/api/alerts/{aid}/explain", None), ("get", f"/api/alerts/{aid}/slow-diag", None)]
    bad = []
    for method, url, body in calls:
        r = getattr(c, method)(url, json=body) if body is not None else getattr(c, method)(url)
        if r.status_code != 403:
            bad.append((url, r.status_code))
    assert not bad, bad
    assert _runs("") == [] or all(r["created_by"] != env["users"][who] for r in _runs(""))   # 没有为分包岗位发出任何模型调用
