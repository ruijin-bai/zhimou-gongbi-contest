"""体检 D2/D3：AI 渗透率不显示假 0%；“智能应用”与“应用成效”两页的调用数同一函数同一口径；内部英文 key 换中文用途名。"""
import json
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from conftest import login

from app import db as dbmod
from app.agents import AGENTS
from app.services import ai_trace

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
NODE = shutil.which("node")


def _runs(env, kind, n, ok=1, days_ago=0, meta=None):
    at = (datetime.now().astimezone() - timedelta(days=days_ago)).isoformat(timespec="seconds")
    conn = dbmod.connect()
    try:
        for _ in range(n):
            dbmod.insert(conn, "ai_runs", {"project_id": env["project_id"], "kind": kind, "provider": "jiaorong", "model": "m", "prompt_version": "v1",
                                           "input_meta_json": dbmod.dumps(meta or {}), "ok": ok, "latency_ms": 100, "created_at": at})
    finally:
        conn.close()


def _set_cfg(env, **kw):
    conn = dbmod.connect()
    try:
        cfg = dbmod.loads(dbmod.one(conn, "SELECT config_json FROM projects WHERE id=?", (env["project_id"],))["config_json"], {}) or {}
        conn.execute("UPDATE projects SET config_json=? WHERE id=?", (dbmod.dumps({**cfg, **kw}), env["project_id"]))
    finally:
        conn.close()


# ----------------------------------------------------------------------------- D3 同口径

def test_agents_page_and_metrics_page_same_call_numbers(env):
    _runs(env, "feed.comment", 23)
    _runs(env, "feed.comment", 3, ok=0)
    _runs(env, "feed.summary", 2, ok=0)          # 登记之外的调用类型：两页都归现场圈Agent
    _runs(env, "log.week", 2)                     # 周确认草稿：两页都归现场记录Agent
    _runs(env, "ai.test", 4)                      # 连通测试：两页都不计
    _runs(env, "safety.scan", 5, days_ago=20)     # 近 7 天之外：总数计入，“近 7 天”不计
    _runs(env, "safety.scan", 1)
    adm = login("admin")
    cards = {a["key"]: a for a in adm.get("/api/agents").json()["agents"]}
    rows = {r["key"]: r for r in adm.get("/api/metrics").json()["ai_adoption"]["agents"]}
    assert cards["circle"]["runs"] == rows["circle"]["calls"] == 28
    assert cards["circle"]["ok"] == rows["circle"]["success"]["k"] == 23
    for k, r in rows.items():                     # 每个 Agent 两页数字一致
        assert (cards[k]["runs"], cards[k]["ok"]) == (r["calls"], r["success"]["k"]), k
    assert cards["scribe"]["runs"] == 2
    assert "other" not in cards and not any(a["key"] == "other" for a in cards.values())
    assert cards["safety"]["runs"] == 6 and cards["safety"]["runs_7d"] == 1 and cards["circle"]["runs_7d"] == 28
    assert "avg_latency_s" not in cards["circle"]   # 耗时等完整调用统计只放应用成效页


def test_one_kind_to_agent_mapping():
    assert ai_trace.agent_of_kind("feed.summary") == "circle" and ai_trace.agent_of_kind("log.week") == "scribe"
    assert ai_trace.agent_of_kind("ai.test") is None and ai_trace.agent_of_kind("ai.check.text") is None
    assert ai_trace.agent_of_kind("log.draft") == "scribe"


# ----------------------------------------------------------------------------- D2 渗透率

def test_penetration_zero_sample_is_dash_not_zero(env):
    pen = login("admin").get("/api/agents").json()["penetration"]
    assert pen and all(v["n"] == 0 and v["rate"] is None and v["note"] == "暂无样本" for v in pen.values())


def test_penetration_simulation_project_not_counted(env):
    from test_agents import _setup, _submit_today
    _, tech = _setup()
    _submit_today(tech)                           # 有真实样本（n>0）也不算渗透率
    _set_cfg(env, simulation=True)
    adm = login("admin").get("/api/agents").json()["penetration"]
    assert adm["日志由AI起草"]["n"] >= 1
    for who in ("admin", "pm"):
        d = login(who).get("/api/agents").json()
        assert d["penetration"] and all(v["rate"] is None and v["note"] == "仿真项目不计" for v in d["penetration"].values()), who
        assert d["simulation"] is True
    pm = login("pm").get("/api/agents").json()["penetration"]
    assert not any("日志" in k or "产值" in k for k in pm)      # 项目经理口径保持


def test_penetration_real_sample_has_rate_and_no_note(env):
    _set_cfg(env, simulation=False)
    from test_agents import _setup, _submit_today
    _, tech = _setup()
    _submit_today(tech)
    pen = login("admin").get("/api/agents").json()["penetration"]
    assert pen["日志由AI起草"]["rate"] == 1.0 and pen["日志由AI起草"]["note"] is None


# ----------------------------------------------------------------------------- 内部 key → 中文

def test_every_ai_kind_has_chinese_name():
    kinds = {k for a in AGENTS for k in a["kinds"]} | set(ai_trace.EXTRA_KIND_AGENT) | {"ai.test", "ai.check.text", "ai.check.vision"}
    missing = [k for k in kinds if not ai_trace.kind_name(k)]
    assert not missing, missing
    assert ai_trace.kind_name("brief.daily") == "每日简报" and ai_trace.kind_name("asr") == "语音转写"
    assert ai_trace.kind_name("no.such.kind") is None


def test_metrics_rows_carry_label_and_definitions_are_chinese(env):
    _runs(env, "brief.daily", 1)
    _runs(env, "quality.checklist", 1)
    _runs(env, "x.unknown", 1)
    m = login("admin").get("/api/metrics").json()
    labels = {r["kind"]: r["label"] for r in m["ai_runs"]}
    assert labels["brief.daily"] == "每日简报" and labels["quality.checklist"] == "质控点清单" and labels["x.unknown"] is None
    text = json.dumps(m["ai_adoption"]["definitions"], ensure_ascii=False)
    for bad in ("ai_runs", "input_meta", "degraded", "repaired", "regen"):
        assert bad not in text, bad


# ----------------------------------------------------------------------------- 前端

HARNESS = r"""
const vm = require('vm'), fs = require('fs'), path = require('path');
const dir = process.argv[2], noop = () => {};
const ctx = { console, setTimeout, clearTimeout, setInterval, clearInterval, navigator: { userAgent: '' }, localStorage: { getItem: () => null, setItem: noop, removeItem: noop },
  location: { hash: '' }, addEventListener: noop, document: { addEventListener: noop, createElement: () => ({ style: {}, setAttribute: noop, appendChild: noop }), querySelector: () => null, documentElement: { style: {} } } };
ctx.window = ctx; ctx.self = ctx;
for (const n of ['Image', 'FormData', 'Blob', 'File', 'XMLHttpRequest', 'MediaRecorder', 'ResizeObserver', 'IntersectionObserver', 'HTMLElement', 'Element', 'Event', 'CustomEvent']) ctx[n] = function () {};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(dir, 'vendor/vue.global.prod.js'), 'utf8'), ctx);
const order = fs.readFileSync(path.join(dir, 'index.html'), 'utf8').match(/\/static\/[\w.\/-]+\.js/g).filter((x) => !x.includes('vendor'));
let src = '';
for (const f of order) src += fs.readFileSync(path.join(dir, f.replace(/^\/static\//, '')), 'utf8') + '\n;';
src += '\nthis.__o = { metrics: Metrics.template, hubMetrics: AgentsHub.components.Metrics === Metrics, agents: AgentsCenter.template, adoption: AdoptionTable.template };';
vm.runInContext(src, ctx);
process.stdout.write(JSON.stringify(ctx.__o), () => process.exit(0));
"""


@pytest.fixture(scope="module")
def tpl():
    if not NODE:
        pytest.skip("没有 node")
    h = Path(__file__).with_name("_ai_stats_harness.js")
    h.write_text(HARNESS, encoding="utf-8")
    try:
        r = subprocess.run([NODE, str(h), str(STATIC)], capture_output=True, text=True, encoding="utf-8", timeout=90)
    finally:
        h.unlink(missing_ok=True)
    assert r.returncode == 0, r.stderr[:800]
    return json.loads(r.stdout)


def test_agents_card_one_line_recent_calls(tpl):
    a = tpl["agents"]
    assert "近 7 天 {{ a.runs_7d }} 次" in a
    for bad in ("调用 {{ a.runs }}", "成功 {{ pct(a.ok / a.runs) }}", "avg_latency_s"):
        assert bad not in a, bad
    assert "v.note" in a and "<details" in a and "口径说明" in a          # 渗透率“—”带说明；口径收进折叠


def test_metrics_page_uses_chinese_kind_names_and_folded_definitions(tpl):
    assert tpl["hubMetrics"] is True
    m = tpl["metrics"]
    assert "kindName[r.kind] || r.kind" not in m and "r.label || r.kind" in m and "内部调用类型" in m
    assert "<b>数据口径</b>" not in m and "<summary" in m and "口径说明" in m
    ad = tpl["adoption"]
    assert "<details" in ad and "口径说明" in ad


def test_mgr_js_syntax():
    if not NODE:
        pytest.skip("没有 node")
    r = subprocess.run([NODE, "--check", str(STATIC / "mgr.js")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
