"""AI 调用可靠性：有界重试（网络/超时/429/5xx）、宽容 JSON 解析、截断重答、格式修复、降级、失败照片自动补跑。
全部离线：httpx 被替换为可编程的假响应，退避等待被替换为空操作，不联网、不调用真实模型。"""
import json
from datetime import datetime, timedelta

import httpx
import pytest

from conftest import jpeg
from app import db as dbmod
from app.ai import client, pool, qa_agent, report_agent, safety_agent
from app.config import settings
from app.services import site_agents

SK = "sk-test-key-reliability0001"
GOOD = json.dumps({"items": [{"name": "A", "points": []}]}, ensure_ascii=False)


def _validate(d):
    if not isinstance(d, dict) or not isinstance(d.get("items"), list):
        raise ValueError("缺少items")
    return d["items"]


class Resp:
    def __init__(self, code=200, content="", finish="stop", headers=None, text="err"):
        self.status_code, self.text, self.headers = code, text, headers or {}
        self._body = {"choices": [{"message": {"content": content}, "finish_reason": finish}], "usage": {"total_tokens": 10}}

    def json(self):
        return self._body


@pytest.fixture()
def fake(env, monkeypatch):
    """一把假 Key + 可编程的 httpx。script 里按顺序放行为：Resp 实例（返回）或异常实例（抛出）；用完后默认返回 GOOD。"""
    monkeypatch.setattr(settings, "ai_provider", "jiaorong")
    conn = dbmod.connect()
    pool.add_key(conn, SK, "K1", ["vision", "core", "agent", "asr", "admin"])
    conn.close()
    st = {"script": [], "posts": [], "sleeps": []}

    class FakeClient:
        def __init__(self, *a, **kw):
            st["timeout"] = kw.get("timeout")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None, **kw):
            st["posts"].append(json)
            step = st["script"].pop(0) if st["script"] else Resp(200, GOOD)
            if isinstance(step, Exception):
                raise step
            return step

    def fake_sleep(sec):
        st["sleeps"].append(sec)
        pool._cool.clear()  # 等待期间 Key 的冷却自然结束
        if st.get("pause_on_sleep"):
            c = dbmod.connect()
            pool.set_config(c, {"paused": 1})
            c.close()

    monkeypatch.setattr(client.httpx, "Client", FakeClient)
    monkeypatch.setattr(client, "_sleep", fake_sleep)
    pool._cool.clear()
    pool._cfg_cache["val"] = None
    yield st
    pool._cool.clear()
    c = dbmod.connect()
    pool.set_config(c, {"paused": 0})
    c.close()


def _last_run():
    conn = dbmod.connect()
    try:
        r = dbmod.one(conn, "SELECT * FROM ai_runs ORDER BY id DESC LIMIT 1")
        return {**r, "meta": dbmod.loads(r["input_meta_json"], {}), "usage": dbmod.loads(r["tokens_json"], {})}
    finally:
        conn.close()


def _call(**kw):
    base = dict(system="按格式输出", user="问", prompt_version="t-1", mock=lambda: {"items": []}, validate=_validate, max_tokens=500)
    base.update(kw)
    return client.call_json("qa.answer", **base)


# ----------------------------------------------------------------------------- 宽容 JSON 解析

def test_extract_json_lenient():
    assert client.extract_json('```json\n{"a": [1, 2,], "b": {"c": 1,},}\n```') == {"a": [1, 2], "b": {"c": 1}}
    assert client.extract_json('结果如下：{"ok": True, "x": None, "s": "True,}"}') == {"ok": True, "x": None, "s": "True,}"}
    assert client.extract_json('```\n{"a": 1}\n```后面还有说明') == {"a": 1}
    assert client.extract_json('[{"a": 1}]') == [{"a": 1}]
    with pytest.raises(ValueError, match="不完整"):
        client.extract_json('{"items": [{"name": "A", "points": [{"stage": "开')
    with pytest.raises(ValueError):
        client.extract_json("<think>只有思考没有结果")
    with pytest.raises(ValueError, match="没有 JSON"):
        client.extract_json("抱歉，我无法完成")


# ----------------------------------------------------------------------------- 有界重试

def test_server_error_retries_then_succeeds(fake):
    fake["script"] = [Resp(502, text="bad gateway"), Resp(503, text="busy"), Resp(200, GOOD)]
    res = _call()
    assert res.data == [{"name": "A", "points": []}]
    assert len(fake["posts"]) == 3 and len(fake["sleeps"]) == 2
    run = _last_run()
    assert run["ok"] == 1 and run["meta"]["http_calls"] == 3 and len(run["meta"]["events"]) == 2
    assert "HTTP 502" in run["meta"]["events"][0]


def test_server_error_gives_up_after_cap(fake):
    fake["script"] = [Resp(500, text="x")] * 5
    with pytest.raises(client.AIError, match="HTTP 500（已重试 2 次）") as ei:
        _call()
    assert ei.value.kind == "server" and len(fake["posts"]) == 1 + client.MAX_SERVER_RETRIES
    run = _last_run()
    assert run["ok"] == 0 and "已重试 2 次" in run["error"] and run["meta"]["http_calls"] == 3


def test_connect_error_and_read_timeout_are_retried(fake):
    fake["script"] = [httpx.ConnectError("boom"), httpx.ConnectTimeout("slow"), Resp(200, GOOD)]
    assert _call().data
    assert len(fake["posts"]) == 3
    # 读超时只再试一次（本身就要等很久）
    fake["posts"].clear()
    fake["script"] = [httpx.ReadTimeout("t"), httpx.ReadTimeout("t"), Resp(200, GOOD)]
    with pytest.raises(client.AIError, match="响应超时（已重试 1 次）") as ei:
        _call()
    assert ei.value.kind == "timeout" and len(fake["posts"]) == 2
    # 连接错误超过上限后明确报网络错误
    fake["posts"].clear()
    fake["script"] = [httpx.ConnectError("x")] * 5
    with pytest.raises(client.AIError, match="无法连接交融模型：ConnectError") as ei:
        _call()
    assert ei.value.kind == "net" and len(fake["posts"]) == 1 + client.MAX_NET_RETRIES


def test_4xx_is_not_retried(fake):
    fake["script"] = [Resp(413, text="too large")]
    with pytest.raises(client.AIError) as ei:
        _call()
    assert ei.value.kind == "bad_request" and len(fake["posts"]) == 1 and not fake["sleeps"]


def test_single_key_429_waits_then_retries(fake):
    fake["script"] = [Resp(429, headers={"retry-after": "3"}), Resp(200, GOOD)]
    assert _call().data
    assert len(fake["posts"]) == 2 and len(fake["sleeps"]) == 1 and 3 <= fake["sleeps"][0] <= 4
    assert "Key 限流冷却中" in " ".join(_last_run()["meta"]["events"])


def test_persistent_429_stops_after_bounded_rounds(fake):
    fake["script"] = [Resp(429)] * 10
    with pytest.raises(client.AIError) as ei:
        _call()
    assert ei.value.kind == "rate" and len(fake["posts"]) == 1 + client.MAX_RATE_ROUNDS


def test_long_cooldown_is_not_waited_for(fake):
    fake["script"] = [Resp(402)]  # 额度类：冷却 300 秒，不让用户干等
    with pytest.raises(client.AIError):
        _call()
    assert not fake["sleeps"] and len(fake["posts"]) == 1


def test_emergency_pause_stops_retries(fake):
    fake["script"] = [Resp(502), Resp(502), Resp(200, GOOD)]
    fake["pause_on_sleep"] = True
    with pytest.raises(client.AIError, match="暂停") as ei:
        _call()
    assert ei.value.kind == "paused" and len(fake["posts"]) == 1


def test_read_timeout_scales_with_output_budget(fake):
    _call(max_tokens=3500)
    assert fake["timeout"].read >= 200
    _call(max_tokens=100)
    assert fake["timeout"].read == pool.timeout()


# ----------------------------------------------------------------------------- 截断重答 / 修复 / 降级

def test_truncated_output_is_regenerated_with_bigger_budget(fake):
    fake["script"] = [Resp(200, '{"items": [{"name": "A", "points": [{"stage": "开', finish="length"), Resp(200, GOOD)]
    res = _call(max_tokens=500)
    assert res.data and not res.repaired
    assert fake["posts"][1]["max_tokens"] > fake["posts"][0]["max_tokens"]
    assert fake["posts"][1]["messages"] == fake["posts"][0]["messages"]  # 同一请求重答，不是“修复”
    run = _last_run()
    assert run["ok"] == 1 and run["meta"]["regen"] is True and set(run["usage"]) == {"first", "regen"}
    assert pool.tokens_of(run["usage"]) == 20


def test_bad_json_is_repaired_with_original_requirements(fake):
    fake["script"] = [Resp(200, "好的，这是结果：items 有一条"), Resp(200, GOOD)]
    res = _call(system="SYSTEM-SCHEMA-MARK", user="USER-REQ-MARK")
    assert res.data and res.repaired
    rep = fake["posts"][1]["messages"]
    assert "SYSTEM-SCHEMA-MARK" in rep[0]["content"] and "USER-REQ-MARK" in rep[1]["content"]  # 修复请求带着原始格式要求
    run = _last_run()
    assert run["ok"] == 1 and run["meta"]["repaired"] is True and set(run["usage"]) == {"first", "repair"}


def test_missing_field_is_repaired_and_validate_crash_is_not_a_500(fake):
    def strict(d):
        return d["items"][0]["name"].upper()  # 字段缺失/类型不对会抛 KeyError、AttributeError、IndexError

    fake["script"] = [Resp(200, '{"items": [1]}'), Resp(200, '{"items": []}'), Resp(200, '{"items": [{"name": "a"}]}')]
    with pytest.raises(client.AIError, match="无法解析为约定结构") as ei:  # 不是未捕获的 AttributeError/IndexError
        _call(validate=strict)
    assert ei.value.kind == "format"
    run = _last_run()
    assert run["ok"] == 0 and run["error"] and len(fake["posts"]) == 2  # 首次 + 一次修复，不无限重试


def test_repair_and_regen_failures_are_traced_and_bounded(fake):
    fake["script"] = [Resp(200, "x", finish="length"), Resp(200, "还是不行", finish="length"), Resp(200, "仍然不行")]
    with pytest.raises(client.AIError):
        _call()
    assert len(fake["posts"]) == 3  # 首次 + 扩大重答 + 修复，各一次
    run = _last_run()
    assert run["ok"] == 0 and run["meta"]["regen"] is True and run["meta"]["http_calls"] == 3


def test_fallback_degrades_explicitly(fake):
    fake["script"] = [Resp(200, "现有记录中没有找到相关内容 [L12]"), Resp(200, "仍然是纯文本 [L12]")]
    res = _call(fallback=qa_agent.fallback_answer, validate=qa_agent.normalize_answer)
    assert res.data["answer"].startswith("仍然是纯文本") and res.data["refs"] == ["L12"]
    assert res.degraded and "降级" in res.degraded
    assert _last_run()["meta"]["degraded"] and _last_run()["ok"] == 1


def test_fallback_refuses_half_json(fake):
    fake["script"] = [Resp(200, '{"answer": "写到一半'), Resp(200, '{"answer": "还是写到一半')]
    with pytest.raises(client.AIError):
        _call(fallback=qa_agent.fallback_answer, validate=qa_agent.normalize_answer)
    assert qa_agent.fallback_answer('{"answer": "完整的\\n回答 [W3]", "refs": [') == {"answer": "完整的\n回答 [W3]", "refs": ["W3"]}


def test_unreadable_photo_is_a_traced_ai_error(fake):
    with pytest.raises(client.AIError, match="照片文件无法读取") as ei:
        _call(images=["/不存在/x.jpg"])
    assert ei.value.kind == "bad_request" and not fake["posts"]
    assert _last_run()["ok"] == 0


def test_unexpected_exception_is_traced_not_lost(fake, monkeypatch):
    monkeypatch.setattr(client, "_post", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(client.AIError, match="内部错误（RuntimeError）"):
        _call()
    assert _last_run()["ok"] == 0


# ----------------------------------------------------------------------------- 各 Agent 的解析容错

def test_safety_scan_normalize_tolerates_shapes():
    refs = ["P1", "P2"]
    hz = {"category": "个人防护", "desc": "未戴安全帽", "severity": "高", "measure": "佩戴", "basis": "必须佩戴"}
    r = safety_agent.normalize_scan([{"photo": "照片1", "hazards": hz}, {"photo": "p2", "hazards": []}], refs)  # 数组、编号写法、单个对象
    assert [x["photo"] for x in r] == ["P1", "P2"] and r[0]["hazards"][0]["severity"] == "high"
    r = safety_agent.normalize_scan({"photos": [{"hazards": [hz]}, {"hazards": []}]}, refs)  # 缺编号但条数一致：按顺序
    assert [x["photo"] for x in r] == ["P1", "P2"]
    r = safety_agent.normalize_scan({"hazards": [hz], "scene": "基坑"}, ["P1"])  # 单张照片直接给 hazards
    assert r[0]["photo"] == "P1" and r[0]["scene"] == "基坑"
    # 模型只给了一张：只返回给出的这一张，缺的由调用方标失败，不当作“无隐患”
    assert [x["photo"] for x in safety_agent.normalize_scan({"photos": [{"photo": "P2", "hazards": []}]}, refs)] == ["P2"]
    with pytest.raises(ValueError):
        safety_agent.normalize_scan({"photos": []}, refs)
    with pytest.raises(ValueError):
        safety_agent.normalize_scan("没有", refs)


def test_qc_norm_tolerates_shapes_and_rejects_empty():
    names = ["钢筋绑扎", "混凝土浇筑"]
    pts = [{"stage": "开工前", "item": "交底", "record": "交底记录"}, "自检合格", {"stage": "完成后验收", "item": "验收"}]
    out = site_agents._qc_norm({"钢筋绑扎": pts}, names)  # {作业名: [点]} 写法、字符串点、阶段写法不规范
    assert [p["stage"] for p in out["钢筋绑扎"]] == ["开工前", "施工中", "完成后"]
    out = site_agents._qc_norm([{"name": "混凝土浇筑作业", "points": pts}, "杂项"], names)  # 名称部分包含、非对象条目
    assert list(out) == ["混凝土浇筑"]
    with pytest.raises(ValueError):  # 对不上任何作业：不能“成功但什么都没有”
        site_agents._qc_norm({"items": [{"name": "别的", "points": pts}]}, names)
    with pytest.raises(ValueError):
        site_agents._qc_norm({"items": "x"}, names)


def test_report_normalize_tolerates_wrapping_and_arrays():
    tpl = report_agent.default_template("week")
    narr = {s["key"]: "内容" for s in tpl["sections"] if s["type"] == "narrative"}
    out = report_agent.normalize({"report": {**narr, "issues": ["1.问题甲", "2.问题乙"], "coordination": "协调甲\n协调乙"}}, tpl)
    assert out["issues"] == "1.问题甲\n2.问题乙" and out["coordination"] == ["协调甲", "协调乙"] and out["overview"] == "内容"
    out = report_agent.normalize({"overview": "只有概况"}, tpl)  # 缺栏留空，由 template_check 如实标“栏目为空”
    assert out["safety"] == "" and {"field": "safety", "issue": "栏目为空"} in report_agent.template_check(out, tpl)
    with pytest.raises(ValueError):
        report_agent.normalize({"safety": "只有安全"}, tpl)  # 首个叙述栏都没有才算失败，交给修复


def test_qa_plan_degrades_to_rule_plan(env, fake):
    from app.services import qa
    uid = env["users"]["pm"]
    fake["script"] = [Resp(200, "不是JSON"), Resp(200, "还是不是"), Resp(200, '{"answer": "现有记录中没有找到相关内容。", "refs": []}')]
    conn = dbmod.connect()
    try:
        out = qa.ask(conn, {"id": uid, "project_id": env["project_id"], "role": "manager"}, "上周桩基施工情况")
    finally:
        conn.close()
    assert out["degraded"] is True and out["answer"].startswith("现有记录中没有找到")


# ----------------------------------------------------------------------------- 照片识别：拆组与自动补跑

def _photo(conn, env, color):
    from app.services import media as media_svc
    return media_svc.save_photo(conn, env["project_id"], env["users"]["tech1"], "p.jpg", jpeg(color), "safety")["id"]


def _scan_json(*refs, hazard=False):
    return json.dumps({"photos": [{"photo": r, "scene": "场景", "hazards": (
        [{"category": "个人防护", "desc": f"{r}未戴安全帽", "severity": "中", "measure": "佩戴", "basis": "必须佩戴"}] if hazard else [])} for r in refs]}, ensure_ascii=False)


def test_scan_omitted_photo_is_failed_not_clear(env, fake):
    conn = dbmod.connect()
    a, b = _photo(conn, env, (10, 20, 30)), _photo(conn, env, (40, 50, 60))
    fake["script"] = [Resp(200, _scan_json("P1"))]  # 只给了 P1
    res = site_agents.safety_scan(conn, [a, b], None)
    st = {r["id"]: r["safety_status"] for r in dbmod.all_(conn, "SELECT id, safety_status FROM media WHERE id IN (?,?)", (a, b))}
    assert st == {a: "clear", b: "failed"} and res["failed"] == 1 and res["ok"] == 1
    conn.close()


def test_scan_group_splits_into_single_photos_on_format_failure(env, fake):
    conn = dbmod.connect()
    a, b = _photo(conn, env, (11, 21, 31)), _photo(conn, env, (41, 51, 61))
    # 整组：首次、修复都给不出合格结构 → 拆成两张单独识别，各自成功
    fake["script"] = [Resp(200, "无法解析"), Resp(200, "还是无法解析"), Resp(200, _scan_json("P1", hazard=True)), Resp(200, _scan_json("P1"))]
    res = site_agents.safety_scan(conn, [a, b], None)
    assert res["failed"] == 0 and len(res["created"]) == 1
    assert {r["id"]: r["safety_status"] for r in dbmod.all_(conn, "SELECT id, safety_status FROM media WHERE id IN (?,?)", (a, b))} == {a: "hazard", b: "clear"}
    conn.close()


def test_network_failure_is_not_split_and_marks_failed(env, fake):
    conn = dbmod.connect()
    a, b = _photo(conn, env, (12, 22, 32)), _photo(conn, env, (42, 52, 62))
    fake["script"] = [httpx.ConnectError("x")] * 3
    res = site_agents.safety_scan(conn, [a, b], None)
    assert res["failed"] == 2 and len(fake["posts"]) == 3  # 没有为拆组再多发请求
    assert {r["safety_status"] for r in dbmod.all_(conn, "SELECT safety_status FROM media WHERE id IN (?,?)", (a, b))} == {"failed"}
    conn.close()


def _failed_photo(env, color):
    conn = dbmod.connect()
    mid = _photo(conn, env, color)
    conn.execute("UPDATE media SET safety_status='failed' WHERE id=?", (mid,))
    up = dbmod.one(conn, "SELECT uploaded_at FROM media WHERE id=?", (mid,))["uploaded_at"]
    return conn, mid, datetime.fromisoformat(up)


def _status(conn, mid):
    return dbmod.one(conn, "SELECT safety_status FROM media WHERE id=?", (mid,))["safety_status"]


@pytest.fixture()
def clock(monkeypatch):
    """让 db.now_iso（事件、记录的时间戳）跟随测试设定的“现在”，用来模拟补跑间隔。"""
    cur = [datetime.now().astimezone()]
    monkeypatch.setattr(dbmod, "now_iso", lambda: cur[0].isoformat(timespec="seconds"))

    def set_(t):
        cur[0] = t
        return t
    return set_


def test_failed_photo_is_not_auto_retried_and_manual_rescan_fixes_it(env):
    from app import main as app_main
    conn, mid, _ = _failed_photo(env, (1, 2, 3))
    app_main._ai_retries()  # 定时任务不再补跑照片识别
    assert _status(conn, mid) == "failed"
    assert dbmod.one(conn, "SELECT COUNT(*) n FROM events WHERE kind='safety.retry'")["n"] == 0
    res = site_agents.safety_scan(conn, [mid], None)  # 人工“重新识别”（/api/safety/rescan 同一入口）
    site_agents.after_rescan(conn, [mid])
    assert res["ok"] == 1 and _status(conn, mid) in ("hazard", "clear")
    n_find = dbmod.one(conn, "SELECT COUNT(*) n FROM findings WHERE ref_type='media' AND ref_id=?", (mid,))["n"]
    site_agents.safety_scan(conn, [mid], None)  # 再点一次不重复派单
    assert dbmod.one(conn, "SELECT COUNT(*) n FROM findings WHERE ref_type='media' AND ref_id=?", (mid,))["n"] == n_find
    conn.close()


def test_quality_retry_bounded_per_day(env, clock):
    conn = dbmod.connect()
    pid = env["project_id"]
    t = datetime.now().astimezone().replace(hour=9, minute=0, second=0, microsecond=0)
    assert site_agents.retry_quality(conn, pid, now=clock(t))["tried"] == 0  # 当天没有失败，不补跑
    dbmod.event(conn, "qc.fail", project_id=pid, value=1)
    assert site_agents.retry_quality(conn, pid, now=clock(t + timedelta(minutes=10)))["skipped"] == "间隔未到"
    for i in range(site_agents.QC_RETRY_MAX_PER_DAY):
        t = t + timedelta(minutes=31)
        assert site_agents.retry_quality(conn, pid, now=clock(t))["tried"] == 1
    assert site_agents.retry_quality(conn, pid, now=clock(t + timedelta(minutes=31)))["skipped"] == "已达当日补跑上限"
    conn.close()


def test_asr_uses_same_retry_and_key_scheduling(fake):
    conn = dbmod.connect()
    pool.set_config(conn, {"model_asr": "asr-test"})
    conn.close()
    wav = settings.data_dir / "asr_test.wav"
    wav.write_bytes(b"RIFF....")

    class AsrResp(Resp):
        def json(self):
            return {"text": "今天完成2根桩"}

    fake["script"] = [httpx.ConnectError("x"), Resp(503), AsrResp(200)]
    res = client.transcribe(str(wav), "a.wav", project_id=None, user_id=None)
    assert res.data == "今天完成2根桩" and len(fake["posts"]) == 3
    run = _last_run()
    assert run["ok"] == 1 and run["meta"]["http_calls"] == 3 and len(run["meta"]["events"]) == 2
    conn = dbmod.connect()
    pool.set_config(conn, {"model_asr": settings.ai_asr_model})
    conn.close()
