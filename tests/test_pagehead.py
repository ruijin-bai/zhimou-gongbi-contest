"""T08：页头 PageHead（目的 + 步骤条 + 主操作）、空状态 EmptyState（原因 + 一个可执行按钮）、边界说明 AiNote、仿真角标。

纯前端：node 直接跑 x_ui.js 里的文案表与“当前步骤”计算函数，并对各页面模板做静态核对：
- 页头目的 ≤25 字、不含术语；面向项目经理的页头/说明不出现填报率、缺报、产值（产值页除外）；
- 步骤条当前步骤由真实状态算出（周确认 / 隐患 / 周报）；
- 逐页的“暂无…”都换成 EmptyState，非紧凑的空状态必须带按钮，按钮的路由与锚点真实存在；
- 同一页面只有一个页头（旧的 h1 / role-hero / 页头里的仿真横幅都已去掉）。
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
NODE = shutil.which("node")


def read(n):
    return (STATIC / n).read_text(encoding="utf-8")


HARNESS = r"""
const vm = require('vm'), fs = require('fs');
const ctx = { console, isSim: () => false, go: () => {}, store: {}, document: {}, setTimeout };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8') + '\nthis.__o = { PAGE_HEADS, STEP_FLOWS, AI_NOTES, weekStep, hazardStep, hazardCounts, reportStep };', ctx);
const o = ctx.__o;
const week = (st, ai, rows, left) => o.weekStep({ status: st, has_ai_draft: ai, week_review: { rows: Array(rows).fill({}) } }, left);
process.stdout.write(JSON.stringify({
  heads: o.PAGE_HEADS, flows: o.STEP_FLOWS, notes: o.AI_NOTES,
  week: { fresh: week('draft', false, 0, 0), checking: week('draft', true, 3, 2), ready: week('draft', true, 3, 0), noTable: week('draft', true, 0, 0),
          submitted: week('submitted', true, 3, 0), approved: week('approved', true, 3, 0), returned: week('returned', true, 3, 1) },
  hazard: { cand: o.hazardStep({ candidate: true, status: 'open', assignee_id: null }), unassigned: o.hazardStep({ status: 'open', assignee_id: null }),
            fixing: o.hazardStep({ status: 'open', assignee_id: 3 }), rejected: o.hazardStep({ status: 'rejected', assignee_id: 3 }), review: o.hazardStep({ status: 'rectifying', assignee_id: 3 }),
            done: o.hazardStep({ status: 'resolved', assignee_id: 3 }), dismissed: o.hazardStep({ status: 'dismissed', assignee_id: 3 }) },
  counts: o.hazardCounts([{ status: 'open', assignee_id: null }, { status: 'open', assignee_id: 2 }, { status: 'rejected', assignee_id: 2 }, { status: 'rectifying', assignee_id: 2 }, { status: 'open', candidate: true }], 4),
  report: { draftClean: o.reportStep({ status: 'draft' }, 0), draftWarn: o.reportStep({ status: 'draft' }, 2), published: o.reportStep({ status: 'published' }, 0) },
}));
"""


@pytest.fixture(scope="module")
def ui():
    if not NODE:
        pytest.skip("没有 node，跳过 x_ui.js 静态校验")
    h = Path(__file__).with_name("_pagehead_harness.js")
    h.write_text(HARNESS, encoding="utf-8")
    try:
        r = subprocess.run([NODE, str(h), str(STATIC / "x_ui.js")], capture_output=True, text=True, encoding="utf-8", timeout=60)
    finally:
        h.unlink(missing_ok=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


JARGON = ["同口径偏差", "批准实绩", "口径核查", "来源治理"]
# 项目经理看得到的页面：不出现日志/周确认填报率、缺报、谁没记录、数据审核；产值只在产值页
PM_BANNED = ["填报", "缺报", "没记录", "数据审核", "确认率"]


def test_page_head_copy_short_plain_and_pm_scoped(ui):
    for k, (title, purpose) in ui["heads"].items():
        assert len(purpose) <= 25, (k, len(purpose), purpose)
        assert purpose.strip() and (title or k == "desk"), k
        text = title + purpose
        assert not any(j in text for j in JARGON), k
        assert not any(w in text for w in PM_BANNED), (k, text)
        if k != "output":
            assert "产值" not in text, (k, text)
    for k, t in ui["notes"].items():
        assert t.strip() and not any(j in t for j in JARGON), k
        assert not any(w in t for w in PM_BANNED), (k, t)
        assert "产值" not in t or k == "output", (k, t)
    # 边界说明不夸大：不出现“准确率”“保证”“自动审批”之类的说法
    for t in ui["notes"].values():
        assert not any(w in t for w in ("准确率", "保证", "自动审批", "100%")), t


def test_step_flows_match_spec(ui):
    assert ui["flows"]["week"] == ["汇总", "核对", "提交", "项目部核定"]
    assert ui["flows"]["hazard"] == ["候选", "确认派单", "整改", "复查闭环"]
    assert ui["flows"]["report"] == ["起草", "数字守卫检查", "审核", "发布"]


def test_step_current_is_computed_from_real_state(ui):
    w = ui["week"]
    assert (w["fresh"], w["checking"], w["ready"], w["noTable"]) == (0, 1, 2, 2)
    assert (w["submitted"], w["approved"], w["returned"]) == (3, 4, 1)          # 提交后等港湾核定；核定后全部完成；被退回回到核对
    h = ui["hazard"]
    assert (h["cand"], h["unassigned"], h["fixing"], h["rejected"], h["review"], h["done"], h["dismissed"]) == (1, 1, 2, 2, 3, 4, -1)
    assert ui["counts"] == [4, 1, 2, 1]                                          # 候选单独计数；退回重改算“整改”
    r = ui["report"]
    assert (r["draftClean"], r["draftWarn"], r["published"]) == (2, 1, 4)


def test_page_keys_exist(ui):
    """各页面用到的 page 键都在文案表里（含 ProgressHub / AgentsHub 按页签取键）；含 AI 输出的页面都挂了边界说明。"""
    keys, notes = set(ui["heads"]), set(ui["notes"])
    used_heads, used_notes = set(), set()
    for f in STATIC.glob("*.js"):
        txt = f.read_text(encoding="utf-8")
        for tag in re.findall(r"<(?:page-head|ai-note)\b[^>]*>", txt):
            found = set(re.findall(r'(?<![:\w])page="([a-z_]+)"', tag))
            m = re.search(r':page="([^"]+)"', tag)
            if m:
                found |= set(re.findall(r"'([a-z_]+)'", m.group(1)))
            (used_heads if tag.startswith("<page-head") else used_notes).update(found)
    used_heads |= {"forecast", "alerts", "plan", "agents", "ask", "metrics"}
    used_notes |= {"alerts", "forecast", "agents", "ask", "metrics"}
    assert used_heads <= keys, used_heads - keys
    assert used_notes <= notes, used_notes - notes
    for k in ("desk", "week", "alerts", "forecast", "output", "safety", "quality", "gate", "reports", "ask", "agents", "metrics"):
        assert k in notes, k


CONVERTED = ["todo.js", "cockpit.js", "x_gate.js", "mgr.js", "roles.js", "tech.js", "views.js"]


def test_no_bare_zanwu_in_converted_pages():
    for n in CONVERTED:
        for i, line in enumerate(read(n).splitlines(), 1):
            if "暂无" in line:
                assert n == "todo.js" and ("replace(" in line or line.lstrip().startswith("//")), f"{n}:{i} 仍有“暂无”：{line.strip()[:80]}"


def test_empty_states_have_a_button_and_it_leads_somewhere_real():
    routes = set(re.findall(r"r\.name === '([a-z]+)'", read("main.js"))) | {"home", "week", "logs", "work", "cockpit", "progress", "shome"}
    for f in STATIC.glob("x_*.js"):
        routes |= set(re.findall(r'EXT\.route\("([a-z]+)"', f.read_text(encoding="utf-8")))
    allsrc = "".join(f.read_text(encoding="utf-8") for f in STATIC.glob("*.js"))
    ids = set(re.findall(r'\bid="([a-z0-9-]+)"', allsrc))
    for n in CONVERTED:
        txt = read(n)
        for tag in re.findall(r"<empty-state\b[^>]*>", txt):
            if "compact" not in tag:
                assert ":action" in tag, f"{n}: 非紧凑空状态必须带按钮：{tag[:100]}"
            assert re.search(r':?reason="', tag), f"{n}: 空状态要写原因：{tag[:100]}"
        for m in re.finditer(r"route: ['\"]([a-z]+)(?:\?[^'\"]*)?['\"]", txt):
            assert m.group(1) in routes, (n, m.group(1))
        for m in re.finditer(r"\bid: ['\"]([a-z0-9-]+)['\"]", txt):
            assert m.group(1) in ids, (n, m.group(1))
        for m in re.finditer(r"focus: ['\"]([a-z]+)['\"]", txt):
            assert f"todo-focus-{m.group(1)}" in allsrc, (n, m.group(1))
    # 后端给的待办空状态按钮（services/todo.py _empty）也都指向真实页面
    backend = (STATIC.parent / "services" / "todo.py").read_text(encoding="utf-8")
    seg = backend[backend.index("def _empty"):backend.index("def build")]
    for m in re.finditer(r'"route": "([a-z]+)', seg):
        assert m.group(1) in routes | {"qrcodes"}, m.group(1)


def test_single_page_head_per_page():
    # 旧的页头写法都已换掉：页头里不再有仿真横幅、role-hero、手写 h1（扩展页 x_* 不在本次范围）
    for n in CONVERTED:
        txt = read(n)
        assert "role-hero" not in txt and "sim-banner" not in txt, n
        assert "<h1" not in txt.replace('<h1 class="mb">随手拍隐患</h1>', ""), n          # 随手拍隐患的非嵌入模式没有入口，保持原样
    main = read("main.js")
    assert main.count("hubbar") == 1 and 'app.component("page-head", PageHead)' in main and 'app.component("empty-state", EmptyState)' in main
    # 待办首页：页头与一行边界说明各只有一处
    todo = read("todo.js")
    assert todo.count("<page-head") == 1 and todo.count('<ai-note page="desk"') == 1 and "todo-boundary" not in todo
    # 仿真角标只有一种写法
    assert "仿真项目 · 不计入应用成效" not in read("core.js") and read("x_ui.js").count(">仿真项目 · 不计入应用成效<") == 1


def test_frontend_syntax_and_cache_bump():
    html = read("index.html")
    assert "x_ui.css?v=17" in html and "x_ui.js?v=13" in html
    if NODE:
        for n in CONVERTED + ["x_ui.js", "main.js", "core.js"]:
            r = subprocess.run([NODE, "--check", str(STATIC / n)], capture_output=True, text=True, timeout=60)
            assert r.returncode == 0, f"{n}: {r.stderr}"


def test_all_component_templates_compile():
    """用 Vue 自带编译器编译全部组件模板（node 直接加载 index.html 里的脚本）：能挡住标签不配对、v-else 没有紧邻 v-if 这类“页面整体白屏”的错误。"""
    if not NODE:
        pytest.skip("没有 node，跳过模板编译检查")
    r = subprocess.run([NODE, str(Path(__file__).with_name("_compile_harness.js")), str(STATIC)], capture_output=True, text=True, encoding="utf-8", timeout=90)
    assert r.returncode == 0, r.stderr[:500]
    out = json.loads(r.stdout)
    assert out["compiled"] >= 60, out
    assert not out["errs"], out["errs"]
