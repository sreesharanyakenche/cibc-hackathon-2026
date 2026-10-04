"""Light-weight question understanding: periods, dimensions, flags.

Deterministic on purpose: the same question always yields the same parse, and the
parse is shown to the user ("what I understood"). An LLM parser can replace
`parse_period` / `detect_dimension` later behind the same signatures.
"""
from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

from .types import Period

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
MONTH_RE = "|".join(sorted(MONTHS, key=len, reverse=True))


def _month_end(y: int, m: int) -> date:
    return date(y, m, calendar.monthrange(y, m)[1])


def parse_period(q: str, as_of: date) -> Period | None:
    """Parse the period in the question; the end is clamped to the snapshot date (no data exists after it)."""
    p = _parse_period(q, as_of)
    if p and p.end > as_of:
        p = Period(p.start, as_of, p.label)
    return p


def _parse_period(q: str, as_of: date) -> Period | None:
    s = q.lower()
    # "12 months to 28 September 2026"
    m = re.search(rf"(\d+) months? to (\d{{1,2}}) ({MONTH_RE}) (\d{{4}})", s)
    if m:
        end = date(int(m.group(4)), MONTHS[m.group(3)], int(m.group(2)))
        k = int(m.group(1))
        yy, mm = end.year, end.month - k
        while mm <= 0:
            mm += 12; yy -= 1
        start = date(yy, mm, min(end.day, calendar.monthrange(yy, mm)[1])) + timedelta(days=1)
        return Period(start, end, f"{k} months to {end.isoformat()}")
    # "between 1 July and 28 September 2026" (different months)
    m = re.search(rf"(\d{{1,2}}) ({MONTH_RE})(?: (\d{{4}}))? ?(?:and|-|to|–|through) ?(\d{{1,2}}) ({MONTH_RE})(?: (\d{{4}}))?", s)
    if m:
        y2 = int(m.group(6) or as_of.year); y1 = int(m.group(3) or y2)
        a = date(y1, MONTHS[m.group(2)], int(m.group(1))); b = date(y2, MONTHS[m.group(5)], int(m.group(4)))
        return Period(a, b, f"{a.isoformat()} to {b.isoformat()}")
    # "between 15 and 22 September 2026" / "on 23-24 September 2026"
    m = re.search(rf"(\d{{1,2}}) ?(?:and|-|to|–) ?(\d{{1,2}}) ({MONTH_RE})(?: (\d{{4}}))?", s)
    if m:
        y = int(m.group(4) or as_of.year); mo = MONTHS[m.group(3)]
        return Period(date(y, mo, int(m.group(1))), date(y, mo, int(m.group(2))),
                      f"{m.group(1)}-{m.group(2)} {calendar.month_name[mo]} {y}")
    # "July-September 2026" / "July to September 2026"
    m = re.search(rf"({MONTH_RE})\s*(?:-|to|–|through)\s*({MONTH_RE})(?: (\d{{4}}))?", s)
    if m:
        y = int(m.group(3) or as_of.year)
        a, b = MONTHS[m.group(1)], MONTHS[m.group(2)]
        return Period(date(y, a, 1), _month_end(y, b), f"{calendar.month_name[a]}-{calendar.month_name[b]} {y}")
    # "September 2026" / "in August"
    m = re.search(rf"\b({MONTH_RE})\b(?: (\d{{4}}))?", s)
    if m and not re.search(r"end of", s[max(0, m.start() - 8):m.start()]):
        y = int(m.group(2) or as_of.year); mo = MONTHS[m.group(1)]
        return Period(date(y, mo, 1), _month_end(y, mo), f"{calendar.month_name[mo]} {y}")
    m = re.search(r"\b(?:in|during|opened in|since)\s+(20\d\d)\b", s)
    if m:
        y = int(m.group(1))
        return Period(date(y, 1, 1), min(date(y, 12, 31), as_of), str(y))
    if "this month" in s:
        return Period(date(as_of.year, as_of.month, 1), as_of, "this month")
    if "last month" in s:
        first = date(as_of.year, as_of.month, 1) - timedelta(days=1)
        return Period(date(first.year, first.month, 1), first, "last month")
    return None


def parse_month_ends(q: str, as_of: date) -> list[date]:
    """'end of March 2026' -> 2026-03-31 (in order of appearance)."""
    out = []
    for m in re.finditer(rf"end of ({MONTH_RE})(?: (\d{{4}}))?", q.lower()):
        y = int(m.group(2) or as_of.year)
        out.append(_month_end(y, MONTHS[m.group(1)]))
    return out


DIMENSIONS = [  # (name, regex) - first match in the text after "by"/"per" wins
    ("prior_broken", r"previously broken|prior broken|number of (?:previous|prior)"),
    ("hour", r"hour"),
    ("site", r"\bsite\b"),
    ("queue", r"\bqueue"),
    ("channel", r"\bchannel"),
    ("bucket", r"bucket"),
    ("province", r"province"),
    ("strategy", r"strateg"),
    ("product", r"product|card type"),
    ("test_cell", r"test cell|champion|challenger"),
]


def detect_dimension(q: str) -> str | None:
    s = q.lower()
    for name in ("prior_broken",):
        if re.search(DIMENSIONS[0][1], s):
            return name
    for m in re.finditer(r"\b(?:by|per|for each|across)\s+([^?.,;]*)", s):
        for name, rx in DIMENSIONS[1:]:
            if re.search(rx, m.group(1)):
                return name
    return None


def n_days_before(q: str) -> int | None:
    m = re.search(r"(\d+) days? before", q.lower())
    return int(m.group(1)) if m else None


def within_days(q: str) -> int | None:
    m = re.search(r"within (\d+) days", q.lower())
    return int(m.group(1)) if m else None


def start_bucket(q: str) -> str | None:
    m = re.search(r"(\d+-\d+|\b180\+)\s*days past due", q.lower())
    return m.group(1) if m else None
