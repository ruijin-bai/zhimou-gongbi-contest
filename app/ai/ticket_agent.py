"""材料Agent：混凝土小票 / 材料进场单 / 合格证照片的字段提取与规则层校验。

分工（严格分开）：
- 模型只“照着画面抄”：看不清的字段返回 null，不推测；
- 规则层再校一遍：数值范围、单位换算、强度等级/牌号规范化，越界的字段置空并标“存疑”；
- 人在草稿上核对、改字段后点确认，才算入账（确认逻辑在 services/tickets.py）。
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timedelta
from typing import Any

from .client import AIResult, call_json

PROMPT_VERSION = "ticket-ocr-v1-20261006"

DOC_KINDS = {"concrete_ticket": "混凝土小票", "material_receipt": "材料进场单", "cert": "合格证/检测报告"}
CATEGORIES = {"rebar": "钢筋", "cement": "水泥", "sand": "砂", "stone": "石子", "admixture": "外加剂", "other": "其他"}

# 字段（顺序即界面顺序）；string 型与 number 型
TEXT_FIELDS = ("supplier", "ticket_no", "vehicle", "issued_at", "part", "material", "grade", "spec", "unit", "batch_no", "receiver", "remark")
NUM_FIELDS = ("qty", "slump", "temp")
FIELDS = ("supplier", "ticket_no", "vehicle", "issued_at", "part", "material", "grade", "spec", "qty", "unit", "batch_no", "slump", "temp", "receiver", "remark")
LABEL = {"supplier": "供应商/站名", "ticket_no": "票号", "vehicle": "车牌/车号", "issued_at": "发货/到场时间", "part": "浇筑部位", "material": "材料名称",
         "grade": "强度等级", "spec": "规格/牌号", "qty": "数量", "unit": "单位", "batch_no": "批号/炉批号", "slump": "坍落度(mm)", "temp": "入模温度(℃)",
         "receiver": "签收人", "remark": "备注"}

# 数值合理范围（规则层）
MAX_TRUCK_M3 = 20.0
SLUMP_RANGE = (0.0, 300.0)
TEMP_RANGE = (-20.0, 60.0)
MAX_WEIGHT_T = 5000.0

SYSTEM = """你是工地资料员，正在抄录一张施工材料单据的照片（混凝土发货小票、材料进场单/过磅单、合格证/质保书/检测报告之一）。
铁律：
1. 只录入画面中清楚可见的字段；看不清、被遮挡、没印刷的字段一律返回 null，绝不推测、不补全、不按常识猜数。
2. 数字照抄，保留单位原文（方/m³、吨/t、公斤/kg），不要换算。坍落度、入模温度票面没有就返回 null。
3. 多张照片是同一张单据的不同角度或不同部分，合并成一份结果。
4. unclear 列出你自己觉得看不清或没把握的字段名（用下面的英文键）。
只输出一个JSON对象，不要解释：
{"doc_kind":"concrete_ticket|material_receipt|cert|unknown","supplier":"供应商/搅拌站/厂家名称","ticket_no":"票号/单号","vehicle":"车牌/车号",
 "issued_at":"发货或到场时间，如 2026-10-06 08:30","part":"浇筑部位/工程名称(混凝土票)","material":"材料名称(如 商品混凝土、HRB400E钢筋、P.O42.5水泥)",
 "grade":"混凝土强度等级及抗渗抗冻(如 C35P8)","spec":"规格/牌号(如 HRB400E Φ25、P.O42.5)","qty":数值或null,"unit":"数量单位",
 "batch_no":"批号/炉批号/出厂编号","slump":数值或null,"temp":数值或null,"receiver":"签收人","remark":"备注","unclear":["键名"]}"""


# ----------------------------------------------------------------------------- 工具

def _nk(s: Any) -> str:
    return unicodedata.normalize("NFKC", str(s)).strip()


FREE_TEXT = ("supplier", "part", "material", "receiver", "remark")   # 自由文字只去首尾空白，不做全角转半角


def _txt(v: Any, cap: int = 80, free: bool = False) -> str | None:
    if v is None or isinstance(v, bool):
        return None
    s = str(v).strip() if free else _nk(v)
    if not s or s.lower() in ("null", "none", "n/a", "无", "-", "--", "/", "未知", "看不清"):
        return None
    return s[:cap]


def to_num(v: Any) -> float | None:
    """'8.5m³'、'180 mm'、'1,200' → 浮点；不是数字返回 None。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if v == v and abs(v) != float("inf") else None
    m = re.search(r"-?\d+(?:\.\d+)?", _nk(v).replace(",", ""))
    return float(m.group()) if m else None


def norm_grade(s: Any) -> str | None:
    """混凝土强度等级规范化：c35p8 → C35P8；C30 → C30；认不出返回 None。"""
    if s is None:
        return None
    t = _nk(s).upper().replace(" ", "")
    m = re.search(r"C(\d{2,3})(?:\(?(P\d{1,2})\)?)?(?:\(?(F\d{2,3})\)?)?", t)
    if not m:
        return None
    strength = int(m.group(1))
    if not 10 <= strength <= 100:
        return None
    return f"C{strength}" + (m.group(2) or "") + (m.group(3) or "")


def grade_strength(g: str | None) -> int | None:
    """强度等级里的 MPa 数（C35P8 → 35）。"""
    m = re.match(r"C(\d{2,3})", (g or "").upper())
    return int(m.group(1)) if m else None


def norm_rebar(s: Any) -> str | None:
    """钢筋牌号/直径规范化：hrb400e φ25 → HRB400E Φ25。"""
    if s is None:
        return None
    t = _nk(s).upper()
    mark = re.search(r"(HRBF|HRB|HPB|CRB)\s*(\d{3,4})\s*(E|F)?", t)
    dia = re.search(r"[ΦФ∅Ø]\s*(\d{1,2})|(\d{1,2})\s*MM", t)
    parts = []
    if mark:
        parts.append(f"{mark.group(1)}{mark.group(2)}{mark.group(3) or ''}")
    if dia:
        parts.append("Φ" + next(g for g in dia.groups() if g))
    return " ".join(parts) if parts else None


def norm_cement(s: Any) -> str | None:
    t = _nk(s).upper().replace(" ", "")
    m = re.search(r"P\.?([OCSFB])\.?(\d{2}\.\d)(R?)", t)
    return f"P.{m.group(1)} {m.group(2)}{m.group(3)}" if m else None


KEYWORDS = {
    "admixture": ("外加剂", "减水剂", "早强剂", "缓凝剂", "引气剂", "膨胀剂", "防冻剂", "速凝剂", "泵送剂"),
    "rebar": ("钢筋", "螺纹钢", "盘条", "盘圆", "圆钢", "HRB", "HPB", "CRB"),
    "cement": ("水泥",),
    "sand": ("砂", "沙"),
    "stone": ("碎石", "石子", "卵石", "骨料", "石料"),
}


def infer_category(material: str | None, spec: str | None = None) -> str:
    text = _nk(f"{material or ''} {spec or ''}").upper()
    for cat in ("admixture", "rebar", "cement", "sand", "stone"):
        if any(k.upper() in text for k in KEYWORDS[cat]):
            return cat
    return "other"


def match_category(name: str, cat: str) -> bool:
    """日志里的材料名是否属于某类别（关键词匹配；砂石混称同时算砂和石）。"""
    text = _nk(name).upper()
    if cat == "sand" and "砂石" in text:
        return True
    if cat == "stone" and "砂石" in text:
        return True
    return any(k.upper() in text for k in KEYWORDS.get(cat, ()))


def parse_when(v: Any, today: date | None = None) -> tuple[str | None, str | None]:
    """返回 (规范化时间, 存疑原因)。支持 2026-10-06 08:30 / 2026/10/6 / 2026年10月6日8时30分；日期不存在或明显在未来则置空。"""
    s = _txt(v, 40)
    if s is None:
        return None, None
    today = today or date.today()
    m = re.search(r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?[T\s]*(?:(\d{1,2})\s*[:时点]\s*(\d{1,2})?)?", s)
    if not m:
        return None, f"时间格式无法识别（原文：{s}）"
    try:
        d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None, f"日期不存在（原文：{s}）"
    if d > today + timedelta(days=1):
        return None, f"日期在未来（原文：{s}）"
    if m.group(4) is not None:
        hh, mm = int(m.group(4)), int(m.group(5) or 0)
        if hh > 23 or mm > 59:
            return d.isoformat(), f"时分无法识别（原文：{s}），已只保留日期"
        return f"{d.isoformat()} {hh:02d}:{mm:02d}", None
    return d.isoformat(), None


_VOL = {"m3", "m³", "㎥", "m^3", "立方", "立方米", "方", "cbm"}
_TON = {"t", "吨", "ton", "tons", "公吨"}
_KG = {"kg", "公斤", "千克", "kgs"}


def norm_unit(u: Any) -> str | None:
    s = _txt(u, 10)
    if s is None:
        return None
    k = s.lower().replace(" ", "")
    if k in _VOL:
        return "m³"
    if k in _TON:
        return "吨"
    if k in _KG:
        return "kg"
    return s


# ----------------------------------------------------------------------------- 字段规则层

def normalize_fields(raw: dict, doc_kind: str, *, strict: bool = False, today: date | None = None) -> tuple[dict, dict]:
    """把一组字段收敛成固定结构，返回 (fields, doubts)。

    strict=False（AI 输出）：越界/无法识别的字段置空，并在 doubts 里写明原因；
    strict=True（人工确认）：同样的规则，但越界直接抛 ValueError，让人改，不静默吞掉。
    """
    fields: dict[str, Any] = {k: None for k in FIELDS}
    doubts: dict[str, str] = {}

    def bad(key: str, reason: str) -> None:
        if strict:
            raise ValueError(f"{LABEL[key]}：{reason}")
        fields[key] = None
        doubts[key] = reason

    for k in TEXT_FIELDS:
        fields[k] = _txt(raw.get(k), 200 if k == "remark" else 80, free=k in FREE_TEXT)
    for k in NUM_FIELDS:
        v = raw.get(k)
        fields[k] = to_num(v)
        if v not in (None, "") and fields[k] is None:
            bad(k, f"不是数字（原文：{_txt(v, 20)}）")

    # 时间
    if fields["issued_at"]:
        val, why = parse_when(fields["issued_at"], today)
        fields["issued_at"] = val
        if why:
            if strict and val is None:
                raise ValueError(f"{LABEL['issued_at']}：{why}")
            doubts["issued_at"] = why

    # 单位与数量（模型有时把单位写在数量里：“8.5m³”“24.5吨”）
    if fields["unit"] is None and isinstance(raw.get("qty"), str):
        tail = re.search(r"\d\s*([^\d.\s,]+)\s*$", str(raw["qty"]).strip())
        fields["unit"] = tail.group(1) if tail else None
    fields["unit"] = norm_unit(fields["unit"])
    q, unit = fields["qty"], fields["unit"]
    if doc_kind == "concrete_ticket":
        if q is not None and unit is None:
            if strict:
                unit = fields["unit"] = "m³"
            else:
                doubts["unit"] = "单位未识别，请核对是否为 m³"
        if q is not None and unit not in (None, "m³"):
            bad("qty", f"混凝土方量单位应为 m³（票面单位：{unit}）")
            q = fields["qty"]
        if q is not None and not 0 < q <= MAX_TRUCK_M3:
            bad("qty", f"单车方量应大于 0 且不超过 {MAX_TRUCK_M3:g} m³（读到 {q:g}）")
        if fields["qty"] is not None:
            fields["unit"] = "m³"
    else:
        if unit == "kg" and q is not None:
            q = fields["qty"] = round(q / 1000, 3)
            unit = fields["unit"] = "吨"
        elif unit == "kg":
            unit = fields["unit"] = "吨"
        if q is not None and q <= 0:
            bad("qty", f"数量应大于 0（读到 {q:g}）")
        elif q is not None and unit == "吨" and q > MAX_WEIGHT_T:
            bad("qty", f"重量超过 {MAX_WEIGHT_T:g} 吨，疑似单位错误（读到 {q:g}）")

    # 坍落度、入模温度：只对混凝土票有意义，其余单据不保留
    if doc_kind == "concrete_ticket":
        s = fields["slump"]
        if s is not None and not SLUMP_RANGE[0] <= s <= SLUMP_RANGE[1]:
            bad("slump", f"坍落度应在 {SLUMP_RANGE[0]:g}~{SLUMP_RANGE[1]:g} mm（读到 {s:g}）")
        t = fields["temp"]
        if t is not None and not TEMP_RANGE[0] <= t <= TEMP_RANGE[1]:
            bad("temp", f"入模温度应在 {TEMP_RANGE[0]:g}~{TEMP_RANGE[1]:g} ℃（读到 {t:g}）")
    else:
        fields["slump"] = fields["temp"] = None

    # 强度等级 / 牌号规范化
    if doc_kind == "concrete_ticket":
        src = fields["grade"] or fields["spec"]
        if src:
            g = norm_grade(src)
            if g:
                fields["grade"] = g
            else:
                bad("grade", f"强度等级无法识别（原文：{src}）")
        if fields["material"] is None:
            fields["material"] = "商品混凝土" if fields["grade"] or fields["qty"] else None
    else:
        fields["grade"] = None
        cat = infer_category(fields["material"], fields["spec"])
        if fields["spec"]:
            nr = norm_rebar(fields["spec"]) if cat == "rebar" else norm_cement(fields["spec"]) if cat == "cement" else None
            if nr:
                fields["spec"] = nr
        if cat == "rebar" and not fields["spec"] and fields["material"]:
            nr = norm_rebar(fields["material"])
            if nr:
                fields["spec"] = nr
    if fields["batch_no"]:
        fields["batch_no"] = re.sub(r"\s+", "", fields["batch_no"]).upper()
    return fields, doubts


def normalize_result(data: Any, kind_hint: str = "auto", *, today: date | None = None) -> dict:
    """模型输出 → 固定结构（供 call_json 的 validate 使用）。"""
    if not isinstance(data, dict):
        raise ValueError("顶层不是对象")
    kind = kind_hint if kind_hint in DOC_KINDS else str(data.get("doc_kind") or "")
    if kind not in DOC_KINDS:
        has_concrete = any(data.get(k) not in (None, "") for k in ("slump", "grade", "temp")) or "混凝土" in str(data.get("material") or "")
        kind = "concrete_ticket" if has_concrete else "material_receipt"
    fields, doubts = normalize_fields(data, kind, strict=False, today=today)
    ul = data.get("unclear")
    unclear = [k for k in (ul if isinstance(ul, list) else []) if k in FIELDS]
    for k in unclear:  # 模型自评看不清：值保留供人核对，但显著标存疑
        if fields.get(k) is not None and k not in doubts:
            doubts[k] = "模型自评看不清，请对照原图核对"
    return {"doc_kind": kind, "fields": fields, "doubts": doubts, "unclear": unclear,
            "raw": {k: data.get(k) for k in FIELDS if data.get(k) is not None}}


# ----------------------------------------------------------------------------- 离线模拟

def mock_payload(doc_kind: str, seed: str) -> dict:
    """确定性的离线模拟输出：由第一张图的内容哈希决定，同一张图永远得到同一份草稿。界面上显著标注“离线模拟”。"""
    n = int(re.sub(r"[^0-9a-f]", "", seed.lower())[:6] or "0", 16)
    tag = f"{n % 1000000:06d}"
    now = datetime.now().replace(second=0, microsecond=0)
    when = now.strftime("%Y-%m-%d %H:%M")
    if doc_kind == "material_receipt":
        return {"doc_kind": "material_receipt", "supplier": "（离线模拟）示例钢厂", "ticket_no": f"JC{tag}", "vehicle": "粤A·M" + tag[:4],
                "issued_at": when, "material": "钢筋", "spec": "hrb400e φ25", "qty": float(10 + n % 20), "unit": "吨",
                "batch_no": f"L{tag}", "receiver": None, "remark": "（离线模拟）", "unclear": ["receiver"]}
    if doc_kind == "cert":
        return {"doc_kind": "cert", "supplier": "（离线模拟）示例钢厂", "ticket_no": f"ZS{tag}", "material": "钢筋", "spec": "hrb400e φ25",
                "batch_no": f"L{tag}", "issued_at": when[:10], "remark": "（离线模拟）质保书", "unclear": []}
    return {"doc_kind": "concrete_ticket", "supplier": "（离线模拟）示例商砼站", "ticket_no": f"HT{tag}", "vehicle": "粤A·H" + tag[:4],
            "issued_at": when, "part": None, "material": "商品混凝土", "grade": "c30", "qty": float(6 + n % 5), "unit": "方",
            "slump": float(160 + 10 * (n % 4)), "temp": None, "receiver": None, "remark": "（离线模拟）", "unclear": ["temp", "receiver"]}


# ----------------------------------------------------------------------------- 调用

def extract(images: list[str], doc_kind: str, *, project_id: int, user_id: int | None, seed: str = "") -> AIResult:
    """images：已压缩的 ai_file 本地路径（1~4 张，同一张单据）。结果 data 为 normalize_result 的固定结构。"""
    hint = DOC_KINDS.get(doc_kind)
    user = (f"今天是{date.today().isoformat()}。单据类型提示：{hint or '未指定，请自行判断'}。"
            f"共 {len(images)} 张照片，是同一张单据的不同角度或不同部分。请抄录字段。")
    mock_kind = doc_kind if doc_kind in DOC_KINDS else "concrete_ticket"
    return call_json(
        "ticket.ocr", system=SYSTEM, user=user, prompt_version=PROMPT_VERSION, images=images,
        mock=lambda: mock_payload(mock_kind, seed),
        validate=lambda d: normalize_result(d, doc_kind),
        project_id=project_id, user_id=user_id, input_meta={"doc_kind": doc_kind}, max_tokens=1500, temperature=0.0)
