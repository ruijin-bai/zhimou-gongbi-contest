"""公司领导多项目只读总览（cockpit.js）与系统管理员系统概况（roles.js SysHome）：RJ 2026-10-10 前端改版线程 F。

- 领导只读：所有写接口对领导要么拒绝，要么只是本人会话/提问类动作（白名单）；
- 管理员不参与业务：不能生成、编辑、发布报表；
- 前端静态核对：领导页支持多项目结构、只有一个项目时如实写“尚未接入”、不硬编码设计稿示例、只调用只读接口；
  系统概况读真实账号表，不放没有来源的系统公告，扫码网址写“免登录现场上报”。
"""
import re
from pathlib import Path

from conftest import login
from test_roles import _add

from app.main import app

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"

# 领导可调用的写方法：只涉及本人会话或提问，不改业务数据
LEADER_OK = {"/api/notifications/read", "/api/ask", "/api/ai/intent", "/api/me/project", "/api/circle/review"}


def _body(schema: dict, spec: dict, depth: int = 0):
    """按 OpenAPI 结构造一份能过校验的最小请求体，让请求走到权限判断。"""
    if "$ref" in schema:
        schema = spec["components"]["schemas"][schema["$ref"].split("/")[-1]]
    if "anyOf" in schema:
        return _body(next((x for x in schema["anyOf"] if x.get("type") != "null"), schema["anyOf"][0]), spec, depth)
    if schema.get("default") is not None:
        return schema["default"]
    if schema.get("type") == "object" or "properties" in schema:
        return {} if depth > 3 else {k: _body(v, spec, depth + 1) for k, v in schema.get("properties", {}).items()}
    return {"string": "测试", "integer": 1, "number": 1, "boolean": False, "array": []}.get(schema.get("type"), "测试")


def test_leader_write_routes_are_rejected(env):
    _add(env, "boss", "leader", "领导")
    ld = login("boss")
    spec = app.openapi()
    passed = []
    for path, ops in spec["paths"].items():
        for m, op in ops.items():
            if m not in ("post", "put", "delete", "patch") or path in ("/api/login", "/api/logout", "/api/dev/login"):
                continue
            content = op.get("requestBody", {}).get("content", {})
            kw = {"json": _body(content["application/json"]["schema"], spec)} if "application/json" in content else {}
            res = ld.request(m.upper(), re.sub(r"\{[^}]+\}", "1", path), **kw)
            if res.status_code < 300 and path not in LEADER_OK:
                passed.append(f"{m.upper()} {path} {res.status_code}")
    assert not passed, passed
    assert ld.post("/api/brief").status_code == 403
    assert ld.post("/api/reports", json={"kind": "week"}).status_code == 403


def test_admin_does_not_generate_or_publish_reports(env):
    adm = login("admin")
    assert adm.post("/api/reports", json={"kind": "week"}).status_code == 403
    assert adm.put("/api/reports/1", json={"status": "published"}).status_code == 403
    assert adm.get("/api/admin/overview").status_code == 200


def test_leader_overview_page_is_read_only_and_multi_project():
    cp = (STATIC / "cockpit.js").read_text(encoding="utf-8")
    assert "其他项目尚未接入" in cp and "store.me" in cp and "projects" in cp            # 多项目结构，只有一个项目时如实空态
    assert re.findall(r"\b(?:POST|DEL)\(", cp) == []                                  # 不调用任何写接口
    assert re.findall(r"\bPUT\(\"([^\"]+)\"", cp) == ["/api/me/project"]              # 唯一的写是切换当前查看的项目
    for word in ("确认", "派单", "审批", "下指令", "生成简报"):
        assert f">{word}<" not in cp, word
    for sample in ("25.1", "6.7 万", "陈立军", "2024-09-22", "尼日利亚"):            # 设计稿示例不硬编码
        assert sample not in cp, sample
    assert "不能相加" in cp and "系统核定不等于业主计量认证" in cp
    assert "AI按当前事实整理" in cp and "source === 'ai'" in cp                         # 只有 AI 来源才写 AI 标签


def test_sys_home_reads_real_accounts_without_fake_notices():
    roles = (STATIC / "roles.js").read_text(encoding="utf-8")
    sys_home = roles[roles.index("const SysHome"):roles.index("const CAP_NAME")]
    assert "/api/admin/overview" in sys_home and "o.users.by_role" in sys_home
    assert "系统公告" not in sys_home and "新增账号" not in sys_home                     # 无来源的公告不放；新增账号只在账号页
    assert "免登录现场上报" in sys_home and "扫码登录" not in sys_home
    assert "历史" in sys_home                                                           # 失败数写明是历史记录
    assert "/api/reports" not in sys_home and "/api/brief" not in sys_home
