"""安全Agent：照片隐患识别与整改复核。照片识别安全问题比识别工程量可靠，是照片的主要用途。"""
from __future__ import annotations

import re

PROMPT_VERSION = "safety-v1-20261006"

CATEGORIES = ("个人防护", "临边洞口", "高处作业", "起重吊装", "基坑边坡", "临时用电", "机械车辆", "脚手架模板", "消防动火", "文明施工", "其他")

SCAN_SYSTEM = """你是中国港湾海外工程项目的专职安全工程师，正在审阅现场照片。请逐张检查明显可见的安全隐患，类别限于：
个人防护（安全帽、反光衣、安全带、防护鞋）、临边洞口、高处作业、起重吊装、基坑边坡、临时用电、机械车辆、脚手架模板、消防动火、文明施工、其他。
规则：
1. 只报告画面中清楚可见的隐患；看不清、被遮挡或无法判断的不报告，不要推测画面外的情况。
2. severity：高=可能立即导致伤亡（如吊物下站人、临边无防护、未系安全带高处作业）；中=明显违规但无即时危险；低=文明施工、标识类问题。
3. measure 写可立即执行的整改措施；basis 写通用安全要求（如“高处作业须系挂安全带”），不要编造具体规范条号。
4. 某张照片无隐患则 hazards 为空数组。
只输出JSON：{"photos":[{"photo":"P1","scene":"场景简述(15字内)","hazards":[{"category":"类别","desc":"隐患描述","severity":"高|中|低","measure":"整改措施","basis":"安全要求"}]}]}"""

VERIFY_SYSTEM = """你是安全工程师，复核隐患整改。第一张图是整改前，第二张图是整改后。原隐患：{hazard}
判断整改后照片是否已消除该隐患（同一位置/对象），看不清或不是同一场景要如实说明。
只输出JSON：{"resolved":true或false,"same_scene":true或false,"reason":"判断依据","remaining":"仍存在的问题，无则空"}"""

SEV = {"高": "high", "中": "medium", "低": "low"}
DUE = {"high": 0, "medium": 2, "low": 6}


def _ref_of(v) -> str:
    """P1 / p1 / 1 / 照片1 / Photo 1 → P1。"""
    m = re.search(r"\d+", str(v or ""))
    return f"P{int(m.group())}" if m else ""


def normalize_scan(data, refs: list[str]) -> list[dict]:
    """模型输出 → [{photo, scene, hazards}]。只返回模型实际给出结果的照片，缺失的照片由调用方按“识别失败”处理，不当作“无隐患”。
    容忍：顶层直接是数组、单张照片时直接给 hazards、照片编号写成 1/照片1、hazards 是单个对象、缺少 photo 编号但条数与照片数一致。"""
    if isinstance(data, list):
        data = {"photos": data}
    if not isinstance(data, dict):
        raise ValueError("顶层不是对象")
    photos = data.get("photos")
    if photos is None:
        photos = next((data[k] for k in ("results", "images", "items") if isinstance(data.get(k), list)), None)
    if photos is None and "hazards" in data and len(refs) == 1:
        photos = [{"photo": refs[0], "scene": data.get("scene"), "hazards": data.get("hazards")}]
    if not isinstance(photos, list):
        raise ValueError("缺少photos数组")
    out, seen = [], set()
    for idx, p in enumerate(photos):
        if not isinstance(p, dict):
            continue
        ref = _ref_of(p.get("photo") or p.get("id") or p.get("ref"))
        if ref not in refs:
            if ref or len(photos) != len(refs):
                continue
            ref = refs[idx]  # 没写编号，但条数与照片数一致：按顺序对应
        if ref in seen:
            continue
        seen.add(ref)
        raw = p.get("hazards")
        raw = [raw] if isinstance(raw, dict) else raw if isinstance(raw, list) else []
        hz = []
        for h in raw:
            if not isinstance(h, dict) or not str(h.get("desc") or "").strip():
                continue
            cat = str(h.get("category") or "其他")
            hz.append({"category": cat if cat in CATEGORIES else "其他", "desc": str(h["desc"]).strip()[:200],
                       "severity": SEV.get(str(h.get("severity")), "medium"), "measure": str(h.get("measure") or "").strip()[:200],
                       "basis": str(h.get("basis") or "").strip()[:120]})
        out.append({"photo": ref, "scene": str(p.get("scene") or "")[:40], "hazards": hz})
    if not out:
        raise ValueError("没有可对应到照片的识别结果")
    return out


def normalize_verify(data) -> dict:
    if not isinstance(data, dict) or "resolved" not in data:
        raise ValueError("缺少resolved")
    return {"resolved": bool(data.get("resolved")) and data.get("same_scene", True) is not False,
            "same_scene": data.get("same_scene", True) is not False, "reason": str(data.get("reason") or "")[:300],
            "remaining": str(data.get("remaining") or "")[:200]}


def mock_scan(photos: list[dict]) -> dict:
    out = []
    for p in photos:
        hz = []
        if p.get("purpose") == "safety":
            hz.append({"category": "个人防护", "desc": "（离线模拟）作业人员未佩戴安全帽", "severity": "中",
                       "measure": "立即佩戴安全帽，班前教育", "basis": "进入施工现场必须佩戴安全帽"})
        out.append({"photo": p["ref"], "scene": "（离线模拟）", "hazards": hz})
    return {"photos": out}
