"""体检 F 组（一线手机端）文案与展示：

- F2 拍照文案与“AI 先候选、安全员确认后再派单”一致；
- F4 技术员收到的进度预警是大白话（作业名 + 慢几天 + 请说原因 + 去随手记），管理岗原文不变；
- F6 一线页不出现“回填 P6”；现场圈只显示地点文字不显示经纬度；分包岗位看 Agent 点评时作业编码换成作业名；
- F5 周确认日期标签统一“周X（MM-DD）”，无内容的日子不放“查看”；首页周条有内容才点亮。
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.services import alerts as alert_svc

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
NODE = shutil.which("node")


def read(n):
    return (STATIC / n).read_text(encoding="utf-8")


def fn_src(src: str, name: str) -> str:
    """从脚本里截出一个顶层 function 的源码（到下一个列首的 }）。"""
    m = re.search(rf"^function {name}\(.*?^\}}", src, re.S | re.M)
    assert m, f"找不到函数 {name}"
    return m.group(0)


def run_node(code: str):
    if not NODE:
        pytest.skip("没有 node")
    r = subprocess.run([NODE, "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


# ----------------------------------------------------------------------------- F2
def test_f2_snap_copy_matches_candidate_flow():
    tech, cam = read("tech.js"), read("camera.js")
    for bad in ("定级、派单", "生成整改单", "派发整改", "自动识别隐患、定级"):
        assert bad not in tech and bad not in cam, bad
    assert "拍完 AI 识别，疑似隐患等安全员确认后再派单" in tech
    assert "疑似隐患等安全员确认后再派单" in cam


# ----------------------------------------------------------------------------- F4
def _notify(monkeypatch, rule="behind", slip=7.0):
    row = {"id": 1, "project_id": 1, "level": "red", "rule": rule, "activity_code": "RD220", "activity_name": "路基填筑",
           "title": "RD220 路基填筑：进度滞后 7 天，超出总浮时 7 天", "as_of": "2026-10-06"}
    facts = {"planned_pct": 60, "actual_pct": 40, "actual_source": "日报确认", "slip_days": slip, "total_float_d": 0.0,
             "forecast_finish": "2026-11-01", "milestones": [{"name": "路基交验"}]}
    users = [{"id": 10, "name": "经理", "role": "manager", "email": ""}, {"id": 20, "name": "技术员", "role": "tech", "email": ""}]
    sent = {}
    monkeypatch.setattr(alert_svc, "one", lambda conn, sql, args=(): row)
    monkeypatch.setattr(alert_svc, "serialize_alert", lambda r: {"detail": facts, "explanation": {"judgement": "关键线路受影响"}})
    monkeypatch.setattr(alert_svc, "recipients", lambda conn, pid, code: users)
    monkeypatch.setattr(alert_svc, "_send_email", lambda to, s, b: "skipped")
    monkeypatch.setattr(alert_svc, "_send_webhook", lambda t, b: "skipped")
    monkeypatch.setattr(alert_svc, "insert", lambda conn, table, data: sent.__setitem__(data["user_id"], data))
    alert_svc.notify(None, 1)
    return sent[10], sent[20]


PLANNER = ("总浮时", "判定日", "浮时", "里程碑", "RD220", "预测完成")


def test_f4_tech_alert_is_plain_language(monkeypatch):
    mgr, tech = _notify(monkeypatch)
    text = tech["title"] + tech["body"]
    for w in PLANNER:
        assert w not in text, w
    assert "《路基填筑》比计划慢约 7 天，请说一下原因（缺料/天气/设备/人手…）" in tech["body"]
    assert "随手记" in tech["body"]
    # 管理岗原文不变
    assert "总浮时" in mgr["body"] and "判定日" in mgr["body"] and mgr["title"].startswith("【红色预警】")
    for w in ("产值", "填报率"):
        assert w not in mgr["body"] + tech["body"]


def test_f4_tech_alert_rounds_and_handles_no_slip(monkeypatch):
    _, tech = _notify(monkeypatch, slip=2.6)
    assert "比计划慢约 3 天" in tech["body"]
    _, tech = _notify(monkeypatch, rule="risk_signal", slip=0)
    assert "慢约 0 天" not in tech["body"] and "请说一下" in tech["body"] and "《路基填筑》" in tech["body"]


# ----------------------------------------------------------------------------- F6
def test_f6_no_p6_backfill_jargon_on_frontline():
    for f in ("tech.js", "circle.js"):
        s = read(f)
        assert "回填 P6" not in s and "回填P6" not in s, f
    assert "提交后更新进度计划" in read("tech.js")


def test_f6_geo_text_shows_place_not_coordinates():
    src = fn_src(read("camera.js"), "geoText")
    out = run_node(src + """
process.stdout.write(JSON.stringify([
  geoText({ place: '3#墩', lat: 6.46551, lon: 3.40001, acc: 7 }),
  geoText({ place: '', lat: 6.46551, lon: 3.40001, acc: 7 }),
  geoText(null),
]));""")
    assert out == ["3#墩", "现场", "未取得定位"]
    assert not any("°" in x or "±" in x for x in out)


def test_f6_circle_hides_activity_codes_for_sub_roles():
    src = read("circle.js")
    out = run_node(fn_src(src, "actLearn") + "\n" + fn_src(src, "actName") + """
const m = {};
actLearn(m, 'RD220', 'RD220 路基填筑，本次确认 120m3 累计 40%');
actLearn(m, '隐患 #3', '隐患 #3 某某');
process.stdout.write(JSON.stringify([m,
  actName('RD220 路基填筑 进度正常', m),
  actName('这是路基填筑（RD220），质量Agent 已派发质控提醒', m),
  actName('本期确认：RD220 累计40%', m),
  actName('相关日志 L12，P6 计划', m),
  actName('这是基槽开挖（A1020）', m),
  actName('', m),
]));""")
    m, a, b, c, d, e, f = out
    assert m == {"RD220": "路基填筑"}
    assert a == "路基填筑 进度正常"
    assert b == "这是路基填筑，质量Agent 已派发质控提醒"
    assert c == "本期确认：路基填筑 累计40%"
    assert d == "相关日志 L12，P6 计划"
    assert e == "这是基槽开挖"
    assert f == ""
    # 只对分包岗位替换：模板里 Agent 点评、依据、接力文字都走 tx()
    assert "isSubOrg()" in src and "tx(c.text)" in src and "tx(e.text)" in src and "tx(st.text)" in src


# ----------------------------------------------------------------------------- F5
def test_f5_week_day_labels_and_content():
    src = read("tech.js")
    out = run_node(fn_src(src, "weekDayName") + "\n" + fn_src(src, "weekDayLabel") + "\n" + fn_src(src, "dayHasContent") + """
process.stdout.write(JSON.stringify([
  weekDayLabel('2026-10-05'), weekDayLabel('2026-10-07'), weekDayLabel('2026-10-11'),
  dayHasContent({ log_id: 5, notes: 0, photos: 0, summary: '' }),
  dayHasContent({ log_id: null, notes: 2, photos: 0, summary: '' }),
  dayHasContent({ log_id: 5, notes: 0, photos: 1, summary: '' }),
  dayHasContent({ log_id: 5, notes: 0, photos: 0, summary: '路基填筑 120m3' }),
  dayHasContent({ log_id: 5, notes: 0, photos: 0, summary: '  ' }),
]));""")
    assert out == ["周一（10-05）", "周三（10-07）", "周日（10-11）", False, True, True, True, False]
    assert "'D' +" not in src and 'D{{ dayRef(d) }}' in src           # 主标签是“周X（MM-DD）”，D 编号只作小号对照（与日志来源标签一致）
    # 周确认列表：标签统一、无内容不放“查看”；首页周条按内容点亮
    assert "weekDayLabel(d.date)" in src
    assert 'v-if="d.log_id && dayHasContent(d)"' in src
    assert "{on: dayHasContent(w)}" in src


# ----------------------------------------------------------------------------- U-10 扫码页
def test_u10_qr_page_title_by_scope_and_phone_wording():
    js = read("x_quick.js")
    qr = js[js.index("const QuickQr"):]
    # 列表按实际范围起名（分包只见本单位的码，港湾岗位见全项目），每个码写明生成人
    assert "我的二维码" not in qr and "本单位二维码" in qr and "全部二维码" in qr and "q.created_by }} 生成" in qr
    # 在手机上看的分包安全员不再看到“电脑”；占位不截断
    assert "工友手机要连工地 Wi-Fi 才能扫开" in qr
    assert 'placeholder="如 3号墩"' in qr and "部位说明，如" not in qr
