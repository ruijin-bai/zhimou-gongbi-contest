"""P6 日历：解析 XER 的 CALENDAR.clndr_data，给排程用的逐日工时与“工作日加减”。

clndr_data 结构（P6 内部格式）：(0||CalendarData()( (0||DaysOfWeek()( (0||1()(时段…)) … (0||7()(…)) ))
  (0||Exceptions()( (0||0(d|45658)()) (0||1(d|45749)( (0||0(s|08:00|f|12:00)()) )) … )) ))
- 星期 1=周日 … 7=周六；时段 s|开始|f|结束；没有时段＝非工作日；
- 例外 d|序列号（1899-12-30 起的天数）；例外下没有时段＝整天不工作，有时段＝当天只按这些时段工作（减时）。
解析不了的日历返回 None，排程退回旧口径（每周工作天数近似），不猜。
"""
from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from datetime import date, timedelta

_EPOCH = date(1899, 12, 30)
_PERIOD = re.compile(r"s\|(\d{1,2}):(\d{2})\|f\|(\d{1,2}):(\d{2})")


def _node(text: str, i: int):
    """解析一个 (0||名称(属性)(子节点…)) 节点，返回 (名称, 属性, 子节点列表, 结束位置)。"""
    assert text[i] == "("
    j = text.index("||", i) + 2
    k = text.index("(", j)
    name = text[j:k].strip()
    end = text.index(")", k)
    attrs = text[k + 1:end]
    i = end + 1
    while text[i].isspace():
        i += 1
    kids = []
    assert text[i] == "("
    i += 1
    while True:
        while text[i].isspace():
            i += 1
        if text[i] == ")":
            break
        kid = _node(text, i)
        kids.append(kid[:3])
        i = kid[3]
    i += 1
    while i < len(text) and text[i].isspace():
        i += 1
    assert text[i] == ")"
    return name, attrs, kids, i + 1


def _hours(kids) -> float:
    total = 0.0
    for _, attrs, _ in kids:
        m = _PERIOD.search(attrs)
        if m:
            a = int(m.group(1)) + int(m.group(2)) / 60
            b = int(m.group(3)) + int(m.group(4)) / 60
            if b == 0:
                b = 24.0
            total += max(0.0, b - a)
    return total


def parse_calendar(data: str) -> dict | None:
    """→ {"week": [周一..周日 工时], "exceptions": {YYYY-MM-DD: 工时}}；格式不认识返回 None。"""
    if not data or "CalendarData" not in data:
        return None
    try:
        root = _node(data.strip().replace("\x7f", " "), 0)
    except (AssertionError, ValueError, IndexError):
        return None
    week, exc = None, {}
    for name, _, kids in root[2]:
        if name == "DaysOfWeek":
            by = {}
            for dname, _, periods in kids:
                if dname.isdigit():
                    by[int(dname)] = _hours(periods)
            if len(by) == 7:
                # P6：1=周日 … 7=周六 → Python weekday（周一=0）
                week = [by[2], by[3], by[4], by[5], by[6], by[7], by[1]]
        elif name == "Exceptions":
            for _, attrs, periods in kids:
                m = re.search(r"d\|(\d+)", attrs)
                if m:
                    exc[(_EPOCH + timedelta(days=int(m.group(1)))).isoformat()] = _hours(periods)
    if week is None:
        return None
    return {"week": week, "exceptions": exc}


class Calendar:
    """以 origin 为第 0 天的逐日“工作日份额”（当天工时 / 标准日工时），支持按工作日前推、后推。

    时间用距 origin 的日历天（可带小数）表示；工作日份额线性分布在当天内。
    """

    def __init__(self, spec: dict, day_hr: float, origin: date, days: int = 6000, name: str = ""):
        self.name, self.day_hr, self.origin = name, day_hr or 8.0, origin
        week, exc = spec["week"], spec.get("exceptions") or {}
        self.w = []
        for i in range(days):
            d = origin + timedelta(days=i)
            h = exc.get(d.isoformat())
            self.w.append((week[d.weekday()] if h is None else h) / self.day_hr)
        self.cum = [0.0]
        for x in self.w:
            self.cum.append(self.cum[-1] + x)
        if self.cum[-1] <= 0:
            raise ValueError("日历没有工作时间")

    def _c(self, t: float) -> float:
        i = int(t // 1)
        if i < 0:
            return t * (self.cum[-1] / len(self.w))
        if i >= len(self.w):
            return self.cum[-1] + (t - len(self.w))
        return self.cum[i] + (t - i) * self.w[i]

    def _inv_early(self, x: float) -> float:
        if x <= 0:
            return x / (self.cum[-1] / len(self.w))
        if x > self.cum[-1]:
            return len(self.w) + (x - self.cum[-1])
        i = bisect_left(self.cum, x)
        if self.cum[i] == x:
            return float(i)
        return (i - 1) + (x - self.cum[i - 1]) / self.w[i - 1]

    def _inv_late(self, x: float) -> float:
        if x <= 0 or x >= self.cum[-1]:
            return self._inv_early(x)
        i = bisect_right(self.cum, x) - 1
        return i + (x - self.cum[i]) / self.w[i] if self.w[i] > 0 else float(i + 1)

    def add(self, t: float, workdays: float) -> float:
        """从时刻 t 起做 workdays 个工作日，返回最早完成时刻。"""
        if workdays <= 0:
            return t
        return self._inv_early(self._c(t) + workdays)

    def sub(self, t: float, workdays: float) -> float:
        """在时刻 t 之前留出 workdays 个工作日，返回最晚开始时刻。"""
        if workdays <= 0:
            return t
        return self._inv_late(self._c(t) - workdays)

    def work_between(self, a: float, b: float) -> float:
        return self._c(b) - self._c(a)

    def day_share(self, d: date) -> float:
        i = (d - self.origin).days
        return self.w[i] if 0 <= i < len(self.w) else 1.0


def build_calendars(rows: list[dict], origin: date) -> dict[str, Calendar]:
    """p6_calendars 行（clndr_id, name, day_hr, spec_json 已解析为 spec）→ {clndr_id: Calendar}；坏日历跳过。"""
    out = {}
    for r in rows:
        spec = r.get("spec")
        if not spec:
            continue
        try:
            out[str(r["clndr_id"])] = Calendar(spec, r.get("day_hr") or 8.0, origin, name=r.get("name") or "")
        except ValueError:
            continue
    return out
