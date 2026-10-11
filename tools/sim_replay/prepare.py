"""回放前的准备（在真实当前时间运行，不写业务库）：
1. 让模型扮演技术员，把每天的底账写成手机随手记（口述/打字/照片说明），校验工程量数字与底账一致；
2. 口述条目用 Windows 自带中文语音合成成 wav（回放时走真实的交融语音识别）；
3. 生成单据照片（混凝土小票、进场单、质量证明书）。
结果缓存在 sim_assets/，可重复运行（已有的不重做；--force 重做）。

用法：python -m tools.sim_replay.prepare [--notes] [--voice] [--docs] [--mock] [--force]
"""
from __future__ import annotations

import argparse
import json
import random
import re
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

from . import scenario as S
from .docs_render import as_phone_photo, cert, concrete_ticket, material_receipt

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "sim_assets"
NOTES = ASSETS / "cache" / "notes.json"
VOICE = ASSETS / "voice"
DOCS = ASSETS / "docs"

CREW = {
    "zhao": "普工约12人、机手7人；挖掘机3台、自卸车8台、推土机1台（9月29日起加压路机1台）",
    "qian": "钢筋工约14人、混凝土工8人、电焊工4人；旋挖钻1台（9月28日起2台）、履带吊1台，商混罐车外租",
    "sun": "普工约8人、钢筋工6人、木工5人；挖掘机1台、汽车吊1台",
    "li": "普工约10人、电工2人、焊工3人；汽车吊1台、装载机1台",
}
STYLE = {
    "zhao": "说话干脆，爱用语音，一条一句话，常带桩号和方量",
    "qian": "比较细致，喜欢打字，会写清楚桩号、根数和遇到的问题",
    "sun": "话少，语音为主，偶尔口误或不完整，经常只说结果",
    "li": "语音和打字混用，会顺带提一句材料、设备情况",
}
ROLE_OF = {"zhao": "路基工程（清表、土方、软基换填）", "qian": "立交桥桩基和T梁预制场", "sun": "涵洞（1#盖板涵、2#圆管涵）", "li": "项目驻地、拌合站临建和试验室"}


def _facts(day: str, tech: str, truth: dict) -> dict:
    d = date.fromisoformat(day)
    wtxt, ok = S.weather(d)
    works = []
    for w in truth[day][tech]:
        p = S.PLAN_BY[w.code]
        works.append({"作业": p[1], "今日完成": (f"{w.qty:g}{p[5]}" if w.qty > 0 else "无工程量"), "情况": w.note})
    photos = [{"编号": ph[2], "地点": ph[3], "内容": ph[4]} for ph in S.PHOTOS if ph[0] == day and ph[1] == tech and ph[5] in ("progress", "other", "hazard")]
    return {"日期": day, "星期": "一二三四五六日"[d.weekday()], "天气": wtxt, "节假日": S.HOLIDAY.get(day, ""), "今日作业": works, "今天拍的照片": photos,
            "人员机械": CREW[tech]}


SYSTEM = """你在模拟一位中国港湾海外项目（尼日利亚莱基深水港疏港路）的中方现场技术员，用手机“随手记”记录当天现场。
要求：
1. 写 2~4 条随手记，像真人在工地上随手说/打的：口语、简短、可以不完整，不要写成正式日志，不要用“综上”“特此记录”之类。
2. 必须且只能使用给定的事实；工程量数字必须与“今日完成”完全一致（用阿拉伯数字，单位照写：方、根、个、m、m2）；不要编造其他数量、人名、事故。
3. 没有工程量的作业也可以简单提一句在做什么。下雨、停工、设备故障等情况要说出来。可以顺带提人员机械（按给定情况，可只提一部分）。
4. 每条注明时间（07:00~19:30 之间，按施工节奏）和方式：voice（口述，会被语音转写）或 text（打字）。
5. 若有“今天拍的照片”，为每张照片另写一条很短的照片说明（10 字左右），方式为 photo，并给出照片编号。
只输出 JSON：{"notes":[{"time":"HH:MM","mode":"voice|text|photo","text":"…","photo":"照片编号或空"}]}"""


def _llm(system: str, user: str) -> str:
    import httpx
    from app.ai import client
    from app.config import settings

    def send(key):
        with httpx.Client(timeout=120, trust_env=settings.ai_trust_env) as c:
            return c.post(f"{settings.ai_base_url}/chat/completions", headers={"Authorization": f"Bearer {key.secret}", "User-Agent": client._UA},
                          json={"model": "jiaorong-deepseek-v3.2", "temperature": 0.9, "max_tokens": 1200,
                                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
    _, r = client._dispatch("core", send, [], [0])
    return r.json()["choices"][0]["message"]["content"]


def _mock_notes(f: dict) -> list[dict]:
    out, t = [], 8
    for w in f["今日作业"]:
        out.append({"time": f"{t:02d}:30", "mode": "voice", "text": f"{w['作业']}今天{w['今日完成']}。{w['情况']}", "photo": ""})
        t += 3
    for ph in f["今天拍的照片"]:
        out.append({"time": f"{t:02d}:10", "mode": "photo", "text": ph["内容"], "photo": ph["编号"]})
        t += 1
    return out


def _check(notes: list[dict], f: dict) -> str:
    """工程量数字必须出现且一致；照片必须都有说明。返回错误说明（空=通过）。"""
    text = " ".join(n.get("text", "") for n in notes)
    for w in f["今日作业"]:
        m = re.match(r"([\d.]+)", w["今日完成"])
        if m and m.group(1) not in text.replace(",", ""):
            return f"缺少或改动了工程量 {w['今日完成']}（{w['作业']}）"
    want = {p["编号"] for p in f["今天拍的照片"]}
    got = {n.get("photo") for n in notes if n.get("mode") == "photo"}
    if want - got:
        return f"照片 {sorted(want - got)} 没有说明"
    for n in notes:
        if not re.fullmatch(r"\d{2}:\d{2}", n.get("time", "")) or n.get("mode") not in ("voice", "text", "photo"):
            return "time/mode 格式不对"
    return ""


def make_notes(mock: bool, force: bool) -> dict:
    from app.ai.client import extract_json
    truth = S.daily_truth()
    cache = json.loads(NOTES.read_text("utf-8")) if NOTES.exists() and not force else {}
    for day in sorted(truth):
        for tech in S.TECHS:
            key = f"{day}|{tech}"
            if key in cache or (day, tech) in S.SILENT:
                continue
            f = _facts(day, tech, truth)
            if not f["今日作业"] and not f["今天拍的照片"]:
                continue
            if mock:
                notes = _mock_notes(f)
            else:
                user = (f"你是{S.PEOPLE[tech][1]}，负责{ROLE_OF[tech]}。说话风格：{STYLE[tech]}。\n今天的事实：\n" + json.dumps(f, ensure_ascii=False, indent=1))
                notes, err = None, ""
                for _ in range(3):
                    try:
                        cand = (extract_json(_llm(SYSTEM, user + (f"\n\n上次的问题：{err}，请改正。" if err else ""))) or {}).get("notes") or []
                    except Exception as exc:  # 网络/格式问题重试
                        err = f"输出无法解析（{type(exc).__name__}）"
                        continue
                    err = _check(cand, f)
                    if not err:
                        notes = cand
                        break
                if notes is None:
                    print("  生成失败，用模板：", key, err, file=sys.stderr)
                    notes = _mock_notes(f)
            trap = S.DQ_TRAPS.get((day, tech))
            if trap == "typo_qty":  # 口误：把方量说大 10 倍（底账不变，留给数据审核与周确认去发现）
                for n in notes:
                    m = re.search(r"(\d{3,5})方", n["text"])
                    if m:
                        n["text"] = n["text"].replace(m.group(0), f"{int(m.group(1)) * 10}方", 1)
                        n["trap"] = "typo_qty"
                        break
            cache[key] = sorted(notes, key=lambda n: n["time"])
            NOTES.parent.mkdir(parents=True, exist_ok=True)
            NOTES.write_text(json.dumps(cache, ensure_ascii=False, indent=1), "utf-8")
            print("notes", key, len(notes))
    return cache


def make_voice(cache: dict, force: bool) -> None:
    """Windows 中文语音（Huihui）合成；一个 PowerShell 进程批量合成。"""
    VOICE.mkdir(parents=True, exist_ok=True)
    jobs = []
    for key, notes in cache.items():
        for i, n in enumerate(notes):
            if n["mode"] != "voice":
                continue
            out = VOICE / f"{key.replace('|', '_')}_{i}.wav"
            if out.exists() and not force:
                continue
            jobs.append((str(out), n["text"]))
    if not jobs:
        return
    lst = ASSETS / "cache" / "tts_jobs.json"
    lst.write_text(json.dumps(jobs, ensure_ascii=False), "utf-8")
    ps = f"""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.SelectVoice("Microsoft Huihui Desktop")
$jobs = Get-Content -Raw -Encoding UTF8 "{lst}" | ConvertFrom-Json
$r = New-Object System.Random 7
foreach ($j in $jobs) {{ $s.Rate = $r.Next(0, 3); $s.SetOutputToWaveFile($j[0]); $s.Speak($j[1]) }}
$s.Dispose()
"""
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
    print("voice", len(jobs))


def make_docs(force: bool) -> list[dict]:
    """单据照片 + 清单（回放引擎据此上传并按底账核对字段）。"""
    DOCS.mkdir(parents=True, exist_ok=True)
    rng = random.Random(S.SEED + 7)
    items = []
    for i, t in enumerate(S.TICKETS):
        items.append({"date": t["date"], "by": t["by"], "kind": t["kind"], "doc": t["doc"], "file": f"T{i + 1:02d}_{t['kind']}.jpg"})
    # 桩基混凝土小票：每根桩拍第一车；9-29 前外购商混，之后用项目拌合站
    truth = S.daily_truth()
    pile_no, n = 0, 0
    for day in sorted(truth):
        for w in truth[day]["qian"]:
            if w.code != "BR110" or w.qty <= 0:
                continue
            for _ in range(int(w.qty)):
                pile_no += 1
                plant = S.LAGOON if day < "2026-09-29" else S.OWN_PLANT
                hh = rng.choice([14, 15, 16, 19, 20])
                doc = dict(plant, ticket_no=f"{'LR' if plant is S.LAGOON else 'BP'}-{day[5:7]}{day[8:]}-{rng.randint(100, 999):04d}",
                           vehicle=rng.choice(["LND 482 XA", "EKY 219 KJ", "APP 655 XB", "KTU 108 AA", "LSR 774 GD"]),
                           issued_at=f"{day} {hh}:{rng.randint(0, 59):02d}", part=f"立交桥 {pile_no}# 桩基 (水下)", grade="C30 水下",
                           qty=8.0, cum=8.0, slump=float(rng.choice([190, 200, 210, 200])), temp=float(rng.choice([28, 29, 30, 31])),
                           driver=rng.choice(["Adebayo", "Chinedu", "Musa", "Emeka", "Tunde"]), receiver="钱志远", remark="加缓凝剂" if rng.random() < 0.5 else "")
                n += 1
                items.append({"date": day, "by": "qian", "kind": "concrete_ticket", "doc": doc, "file": f"C{n:02d}_pile{pile_no}.jpg", "pile_no": pile_no})
    for i, it in enumerate(items):
        out = DOCS / it["file"]
        if out.exists() and not force:
            continue
        doc = it["doc"]
        img = {"concrete_ticket": concrete_ticket, "material_receipt": material_receipt, "cert": cert}[it["kind"]](doc)
        as_phone_photo(img, S.SEED + i).save(out, quality=88)
    (ASSETS / "cache").mkdir(parents=True, exist_ok=True)
    (ASSETS / "cache" / "docs.json").write_text(json.dumps(items, ensure_ascii=False, indent=1, default=list), "utf-8")
    print("docs", len(items))
    return items


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--notes", action="store_true")
    ap.add_argument("--voice", action="store_true")
    ap.add_argument("--docs", action="store_true")
    ap.add_argument("--mock", action="store_true", help="不调模型，用模板生成随手记（调试用）")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    allp = not (a.notes or a.voice or a.docs)
    if a.notes or allp:
        cache = make_notes(a.mock, a.force)
    else:
        cache = json.loads(NOTES.read_text("utf-8")) if NOTES.exists() else {}
    if a.voice or allp:
        make_voice(cache, a.force)
    if a.docs or allp:
        make_docs(a.force)


if __name__ == "__main__":
    main()
