"""增强能力的“启用/隐藏”开关（项目级，存 projects.config_json 的 features 段，不加表）。

默认全部启用（保持既有行为）；系统管理员在“项目与规则 → 增强能力”里按需隐藏，演示时可收敛界面。
“隐藏”只影响入口与首页待办/提醒来源：对应页面的旧路由仍可直达（书签、通知里的旧链接不失效），已有数据不删除。
前端读 /api/me 的 project.features；后端各待办来源（services/todo.py）按同一份开关决定是否出条目。
"""
from __future__ import annotations

from ..db import loads, one

# (键, 名称, 说明)：顺序即设置页展示顺序
FEATURES: list[tuple[str, str, str]] = [
    ("circle_agent", "现场圈 Agent", "动态下的 Agent 点评、智能体接力时间线、AI精选、@工笔问答"),
    ("cmd", "指挥", "输入框的“指令/纪要”模式、指令动作卡与例会行动项"),
    ("nudge", "催办", "催办台与首页的催办动态、固定的提问"),
    ("archive", "档案", "部位档案与索赔证据包"),
    ("gate", "放行", "放行清单（开工/浇筑前条件核对）"),
    ("tickets", "材料", "材料与砼（小票识别、试块与复试台账）"),
    ("conceal", "资料", "隐蔽验收（验收照片归集与成册）"),
    ("qr", "二维码", "部位二维码与现场扫码上报"),
]
KEYS = [k for k, _, _ in FEATURES]


def flags_from_cfg(cfg: dict | None) -> dict[str, bool]:
    """配置里没写的项一律视为启用；非布尔值按未设置处理。"""
    raw = (cfg or {}).get("features")
    raw = raw if isinstance(raw, dict) else {}
    return {k: (raw[k] if isinstance(raw.get(k), bool) else True) for k in KEYS}


def flags(conn, project_id: int) -> dict[str, bool]:
    p = one(conn, "SELECT config_json FROM projects WHERE id=?", (project_id,))
    return flags_from_cfg(loads(p["config_json"], {}) if p else {})


def enabled(conn, project_id: int, key: str) -> bool:
    return flags(conn, project_id).get(key, True)


def merge(cfg: dict, patch: dict[str, bool]) -> dict:
    """把前端提交的开关并入 config：只认已知键；全部启用时清掉该段，保持旧配置原样。"""
    cur = flags_from_cfg(cfg)
    for k, v in (patch or {}).items():
        if k in cur:
            cur[k] = bool(v)
    off = {k: False for k, v in cur.items() if not v}
    if off:
        cfg["features"] = off
    else:
        cfg.pop("features", None)
    return cfg
