"""LLM-backed SQL planner (fallback when no template matches).

Contract (same as the template planner): `plan(question, ctx) -> Plan | None`.

Safety pipeline for every LLM draft:
  1. prompt contains ONLY allow-listed relations/columns (denied PII/protected columns are not shown)
  2. the draft is parsed and validated by `guard.validate` (single SELECT, allow-list, no star, no denied columns)
  3. it is dry-run with EXPLAIN against the read-only DuckDB connection
  4. on failure, ONE repair attempt with the error message; then give up (engine refuses)
The prompt carries the known data traps so drafts inherit the same fixes as the templates.
"""
from __future__ import annotations

import json
import re

from . import guard
from .llm import LLM, LLMUnavailable
from .types import Plan

TRAP_RULES = """\
Data rules you MUST follow:
- Contact channel: use cur_contacts.channel_eff (channel was renamed channel_v2 on 2026-09-26).
- Delinquency bucket: use cur_cases.current_bucket_calc / cur_snapshot.dpd_bucket_calc, not the raw bucket columns.
- Snapshot month-end: use cur_snapshot.month_end_parsed (the raw month_end_date has 4 formats).
- Promise rates: only resolved promises (cur_promises.is_resolved); kept = ptp_status 'kept'; exclude open and cancelled.
- Open case = cur_cases.is_open (outcome IS NULL).
- Strategy / champion-challenger comparisons: only randomised cases (cur_cases.assignment_method = 'random').
- Snapshot date is 2026-09-28; there is no data after it.
- Never select personal data, protected attributes, or free text. List columns explicitly (no SELECT *).
- Output exactly one DuckDB SELECT statement.
"""


def schema_context(con, relations: set[str] | None = None) -> str:
    rel = sorted(relations or guard.ALLOWED_RELATIONS)
    denied = guard.denied_columns()
    lines = []
    for r in rel:
        cols = con.execute(
            "select column_name, data_type from information_schema.columns where table_name = ? order by ordinal_position",
            [r]).fetchall()
        shown = [f"{c} {t}" for c, t in cols if c.lower() not in denied]
        if shown:
            lines.append(f"{r}({', '.join(shown)})")
    return "\n".join(lines)


def metrics_context(ctx) -> str:
    rows = ctx.con.execute("select metric_id, business_definition, formula, default_filters, source_tables "
                           "from raw_metric_definitions").fetchall()
    return "\n".join(f"- {m}: {d} | formula: {f} | filters: {flt} | tables: {t}" for m, d, f, flt, t in rows)


SYSTEM = ("You translate collections-analytics questions into one safe DuckDB SELECT. "
          "Reply with JSON only: {\"sql\": str, \"understood\": {...}, \"assumptions\": [str]}.\n" + TRAP_RULES)


class LLMSqlPlanner:
    def __init__(self, llm: LLM, max_repairs: int = 1) -> None:
        self.llm = llm
        self.max_repairs = max_repairs

    def plan(self, question: str, ctx) -> Plan | None:
        if not getattr(self.llm, "available", True):
            return None
        user = (f"Certified metrics:\n{metrics_context(ctx)}\n\nRelations:\n{schema_context(ctx.con)}\n\n"
                f"Question: {question}\nAs-of date: {ctx.as_of.isoformat()}")
        error = None
        for attempt in range(self.max_repairs + 1):
            prompt = user if error is None else f"{user}\n\nYour previous SQL failed: {error}\nFix it and reply with JSON only."
            try:
                raw = self.llm.complete(SYSTEM, prompt)
            except LLMUnavailable:
                return None
            try:
                data = self._parse(raw)
                sql = guard.validate(data["sql"])
                ctx.con.execute("explain " + sql)
            except Exception as e:  # guard error, parse error, or DuckDB binder error
                error = str(e)[:300]
                continue
            notes = list(data.get("assumptions") or []) + ["SQL drafted by an LLM, then validated by the SQL guard and dry-run."]
            return Plan("llm_sql", sql, {"metric": "llm_drafted", **(data.get("understood") or {})}, notes)
        return None

    @staticmethod
    def _parse(raw: str) -> dict:
        m = re.search(r"\{.*\}", raw, flags=re.S)
        if not m:
            raise ValueError("no JSON object in reply")
        data = json.loads(m.group(0))
        if "sql" not in data:
            raise ValueError("JSON has no 'sql'")
        return data
