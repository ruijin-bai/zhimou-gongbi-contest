"""资料Agent（kind=conceal.pack）：隐蔽工程验收照片的识别与口述整理。

边界：
- 只写画面里清楚可见的内容，不推测、不编造尺寸、桩号、型号；读不清返回 null；
- shot 只能从模板给定的 key 里选，看不出就 null；
- fields 只收“技术员口述里逐字出现”的数值/文字，来源固定标“口述”；
- 服务端规则层再核：shot 合法性、数据项单位、数值是否在录入合理性范围内（范围只防识别/口误离谱，不是验收标准），越界置空并标“存疑”。
AI 的输出只写入 ai_json，不会自动成为人工确认的数据，也不会改变记录状态。
"""
from __future__ import annotations

import math
import re

PROMPT_VERSION = "conceal-v1-20261006"
MAX_PER_CALL = 6

SYSTEM = """你是中国港湾海外工程项目的质检资料员，正在整理一份隐蔽工程验收的现场照片（验收项目见下）。
给你该验收的“必拍照片清单(shot)”“应记录的数据项(item)”、若干张照片P1..Pn，以及技术员的口述文字（可能为空）。
规则：
1. 逐张照片，只描述画面中清楚可见的内容(scene，25字内)；不推测画面外情况，不编造尺寸、桩号、型号、规格。
2. shot：这张照片对应清单中的哪一项，只能填给定的shot key；看不出就填null。
3. clear：照片是否清晰可用（对焦、光线、遮挡）；不确定填null。
4. readings：画面里清楚读得出的数据（如卷尺/钢尺/水准尺读数、标牌文字）。item填给定的数据项key，对不上填null；value为数字、unit为画面所示单位、text为标牌文字；读不清或没有就返回空数组，不要猜。
5. fields：只写【技术员口述】里明确说出的数值或文字（必须逐字出自口述），item必须是给定的数据项key，value为数字或文字、unit为口述的单位；口述没提到的一律不写；不要用照片推算任何数。
6. 允许偏差、设计要求你不知道也不要写。
只输出JSON：{"photos":[{"photo":"P1","scene":"画面简述","shot":null,"clear":true,"readings":[{"item":null,"label":"卷尺读数","value":null,"unit":"mm","text":null}]}],"fields":[{"item":"数据项key","value":0,"unit":"单位"}]}"""

_UNIT_ALIAS = {"毫米": "mm", "㎜": "mm", "MM": "mm", "厘米": "cm", "公分": "cm", "CM": "cm", "米": "m", "M": "m", "公里": "km", "KM": "km"}


def unit_norm(u) -> str:
    s = str(u or "").strip()
    return _UNIT_ALIAS.get(s, _UNIT_ALIAS.get(s.upper(), s))


def num(v) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else None
    if isinstance(v, str) and re.fullmatch(r"\s*-?\d+(?:\.\d+)?\s*", v):
        return float(v)
    return None


def build_user(tpl: dict, refs: list[tuple[str, dict]], narration: str) -> str:
    L = [f"验收项目：{tpl['name']}", "必拍照片清单(shot)："]
    for s in tpl["shots"]:
        L.append(f"- {s['key']}：{s['label']}" + (f"（{s['hint']}）" if s.get("hint") else "") + ("" if s.get("required", True) else "［可选］"))
    L.append("应记录的数据项(item)：")
    for i in tpl["items"]:
        L.append(f"- {i['key']}：{i['label']}（{'数字' if i['type'] == 'number' else '文字'}" + (f"，单位{i['unit']}" if i.get("unit") else "") + "）")
    if refs:
        L.append("照片：" + "、".join(r for r, _ in refs))
    L.append("技术员口述：" + (narration.strip() if narration and narration.strip() else "（无）"))
    return "\n".join(L)


def normalize(data, refs: list[str], tpl: dict) -> dict:
    """把模型原始输出规整为 {"photos": {ref: {...}}, "fields": [...]}，不合法的字段丢弃（不报错，除非结构根本不对）。"""
    if not isinstance(data, dict) or not isinstance(data.get("photos"), list):
        raise ValueError("缺少photos数组")
    shot_keys = {s["key"] for s in tpl["shots"]}
    item_by = {i["key"]: i for i in tpl["items"]}
    photos: dict[str, dict] = {}
    for p in data["photos"]:
        if not isinstance(p, dict):
            continue
        ref = str(p.get("photo") or "").upper()
        if ref not in refs:
            continue
        shot = p.get("shot")
        shot = shot if isinstance(shot, str) and shot in shot_keys else None
        clear = p.get("clear") if isinstance(p.get("clear"), bool) else None
        readings = []
        for r in p.get("readings") or []:
            if not isinstance(r, dict):
                continue
            value = num(r.get("value"))
            text = str(r.get("text") or "").strip()[:40] or None
            if value is None and not text:
                continue
            item = r.get("item") if isinstance(r.get("item"), str) and r.get("item") in item_by else None
            readings.append({"item": item, "label": str(r.get("label") or "")[:30], "value": value, "unit": unit_norm(r.get("unit")) or None, "text": text})
        photos[ref] = {"scene": str(p.get("scene") or "").strip()[:60], "shot": shot, "clear": clear, "readings": readings[:6]}
    fields = []
    for f in data.get("fields") or []:
        if not isinstance(f, dict) or f.get("item") not in item_by:
            continue
        it = item_by[f["item"]]
        if it["type"] == "number":
            v = num(f.get("value"))
        else:
            v = str(f.get("value") or "").strip()[:80] or None
        if v is None:
            continue
        fields.append({"item": f["item"], "value": v, "unit": unit_norm(f.get("unit")) or it.get("unit", ""), "src": "口述"})
    return {"photos": photos, "fields": fields}


def _in_range(it: dict, v: float) -> bool:
    lo, hi = it.get("min"), it.get("max")
    return not ((lo is not None and v < lo) or (hi is not None and v > hi))


def apply_rules(tpl: dict, norm: dict, narration: str) -> dict:
    """服务端规则层：单位一致、数值在合理性范围内、口述候选必须真出现在口述里。不合格的值置空并写 doubt。"""
    item_by = {i["key"]: i for i in tpl["items"]}
    for p in norm["photos"].values():
        for r in p["readings"]:
            it = item_by.get(r["item"]) if r["item"] else None
            if not it or r["value"] is None or it["type"] != "number":
                continue
            if r["unit"] and it.get("unit") and unit_norm(r["unit"]) != unit_norm(it["unit"]):
                r["value"], r["doubt"] = None, f"读数单位({r['unit']})与数据项单位({it['unit']})不一致，未采用"
            elif not _in_range(it, r["value"]):
                r["value"], r["doubt"] = None, "读数超出常见范围，疑似识别错误，已置空"
    said = {float(x) for x in re.findall(r"\d+(?:\.\d+)?", narration or "")}
    out = []
    for f in norm["fields"]:
        it = item_by[f["item"]]
        f["label"] = it["label"]
        if not (narration or "").strip():
            f["value"], f["doubt"] = None, "没有口述文字，未采用"
        elif it["type"] == "number":
            if f["unit"] and it.get("unit") and unit_norm(f["unit"]) != unit_norm(it["unit"]):
                f["value"], f["doubt"] = None, f"口述单位({f['unit']})与数据项单位({it['unit']})不一致，请人工录入"
            elif f["value"] not in said:
                f["value"], f["doubt"] = None, "口述里没有这个数，未采用"
            elif not _in_range(it, f["value"]):
                f["value"], f["doubt"] = None, "数值超出常见范围，疑似口误或识别错误，已置空"
        elif f["value"] not in (narration or ""):
            f["value"], f["doubt"] = None, "口述里没有这段文字，未采用"
        out.append(f)
    norm["fields"] = out
    return norm


def mock_assign(tpl: dict, photos: list[dict]) -> dict[str, str | None]:
    """离线模拟：已有人工归类的沿用，其余按清单顺序依次分配（确定性，仅供开发测试）。"""
    used = {p["shot"] for p in photos if p.get("shot")}
    free = [s["key"] for s in tpl["shots"] if s["key"] not in used]
    out: dict[str, str | None] = {}
    for p in photos:
        out[p["ref"]] = p["shot"] if p.get("shot") else (free.pop(0) if free else None)
    return out


def mock_fields(tpl: dict, narration: str) -> list[dict]:
    out = []
    for it in tpl["items"]:
        if it["type"] != "number" or not it.get("kw"):
            continue
        m = re.search(r"(?:" + "|".join(re.escape(k) for k in it["kw"]) + r")[^\d\n]{0,6}(\d+(?:\.\d+)?)", narration or "")
        if m:
            out.append({"item": it["key"], "value": float(m.group(1)), "unit": it.get("unit", "")})
    return out


def mock_response(tpl: dict, assign: dict[str, str | None], narration: str | None) -> dict:
    label = {s["key"]: s["label"] for s in tpl["shots"]}
    return {"photos": [{"photo": ref, "scene": "（离线模拟）" + (label.get(shot, "") + "画面" if shot else "无法归类"), "shot": shot, "clear": True, "readings": []}
                       for ref, shot in assign.items()],
            "fields": mock_fields(tpl, narration or "") if narration else []}
