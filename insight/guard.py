"""SQL guard: nothing runs unless it passes. Used for template SQL (defence in depth)
and, later, for LLM-drafted SQL.

Rules:
  * exactly one statement, and it is a SELECT / WITH ... SELECT / UNION of SELECTs
  * only allow-listed relations (curated views and reference tables); CTE names are fine
  * no table functions (read_csv, read_parquet, ...)
  * no column that schema.json marks 'Direct PII' or 'Protected attribute'
  * no bare SELECT * (it could smuggle denied columns); COUNT(*) is fine
"""
from __future__ import annotations

import json
from functools import lru_cache

import sqlglot
from sqlglot import exp

from data_pipeline.config import SCHEMA_JSON

ALLOWED_RELATIONS = {
    "cur_cases", "cur_promises", "cur_contacts", "cur_snapshot", "cur_agents",
    "meta_snapshot",
    "raw_card_accounts", "raw_loan_accounts", "raw_offers", "raw_hardship_programs",
    "raw_ops_events", "raw_metric_definitions", "raw_channel_capacity", "raw_code_lookups",
    "raw_case_assignment_history", "raw_agent_shifts", "raw_qa_checklist",
}

BLOCKED_SENSITIVITY = {"Direct PII", "Protected attribute"}


class GuardError(ValueError):
    pass


@lru_cache(maxsize=1)
def denied_columns() -> frozenset[str]:
    schema = json.loads(SCHEMA_JSON.read_text(encoding="utf-8"))
    out = set()
    for meta in schema.values():
        for col in meta["columns"]:
            if col.get("sensitivity") in BLOCKED_SENSITIVITY:
                out.add(col["name"].lower())
    # Free text is only reachable through the masked, citation-checked document path.
    out |= {"note_text", "sms_text", "sms_reply_text", "email_subject"}
    return frozenset(out)


def validate(sql: str) -> str:
    """Return the SQL if it is safe, else raise GuardError."""
    try:
        statements = sqlglot.parse(sql, read="duckdb")
    except sqlglot.errors.ParseError as e:
        raise GuardError(f"SQL does not parse: {e}") from e
    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise GuardError("exactly one statement is allowed")
    tree = statements[0]
    if not isinstance(tree, (exp.Select, exp.Union, exp.Subquery)):
        raise GuardError(f"only SELECT statements are allowed, got {type(tree).__name__}")

    cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    for tbl in tree.find_all(exp.Table):
        if not isinstance(tbl.this, exp.Identifier):
            raise GuardError("table functions are not allowed")
        name = tbl.name.lower()
        if name in cte_names:
            continue
        if tbl.db or tbl.catalog:
            raise GuardError(f"qualified relation not allowed: {tbl.sql()}")
        if name not in ALLOWED_RELATIONS:
            raise GuardError(f"relation not allowed: {name}")

    for func in tree.find_all(exp.Anonymous):
        if func.name.lower().startswith(("read_", "glob", "copy", "pragma")):
            raise GuardError(f"function not allowed: {func.name}")

    denied = denied_columns()
    for col in tree.find_all(exp.Column):
        if col.name.lower() in denied:
            raise GuardError(f"column not allowed: {col.name}")
    for star in tree.find_all(exp.Star):
        if not isinstance(star.parent, exp.Count):
            raise GuardError("SELECT * is not allowed; list columns explicitly")
    return sql
