"""Template SQL planner: deterministic, certified-metric-first.

Each builder returns a `Plan`. If a question is recognised as a data question but the
requested breakdown/filter is not supported, the planner returns None (never a
silently different answer) so that the engine can fall back to the LLM planner or refuse.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

import duckdb

from . import config
from .types import Period, Plan
from .understand import (detect_dimension, n_days_before, parse_month_ends,
                         parse_period, start_bucket, within_days)

# ----------------------------------------------------------------------------- context


@dataclass
class Ctx:
    con: duckdb.DuckDBPyConnection
    as_of: date = config.AS_OF
    metric_defs: dict | None = None

    def metric(self, metric_id: str) -> dict:
        if self.metric_defs is None:
            rows = self.con.execute(
                "select metric_id, metric_name, business_definition, formula, default_filters "
                "from raw_metric_definitions").fetchall()
            self.metric_defs = {r[0]: dict(zip(("metric_id", "name", "definition", "formula", "filters"), r)) for r in rows}
        return self.metric_defs.get(metric_id, {})


def _ts(d: date) -> str:
    return f"TIMESTAMP '{d.isoformat()} 00:00:00'"


def _d(d: date) -> str:
    return f"DATE '{d.isoformat()}'"


def _contact_window(col: str, p: Period | None, as_of: date) -> str:
    """Half-open window on a timestamp column, clamped to the snapshot date (exclusive)."""
    if not p:
        return ""
    end_excl = min(p.end + timedelta(days=1), as_of)
    return f" and {col} >= {_ts(p.start)} and {col} < {_ts(end_excl)}"


def _date_window(col: str, p: Period | None) -> str:
    return f" and {col} between {_d(p.start)} and {_d(p.end)}" if p else ""


def _understood(metric: str, ctx: Ctx, dim, period: Period | None, **extra) -> dict:
    m = ctx.metric(metric)
    out = {"metric": metric, "dimension": dim, "period": period.label if period else None}
    if m:
        out["definition"] = m.get("definition")
        out["formula"] = m.get("formula")
        out["default_filters"] = m.get("filters")
    out.update({k: v for k, v in extra.items() if v is not None})
    return out


def unsupported_breakdown(q: str, dim: str | None) -> bool:
    """True when the question asks 'by <something we cannot break down by>'."""
    if dim:
        return False
    # 'cost per RPC', 'attempts per case' are metric names, not breakdowns
    text = re.sub(r"\bper (contact|case|rpc|right-party contact|attempt|account|call)\b", " ", q.lower())
    for m in re.finditer(r"\b(?:by|per|for each)\s+([^?.,;]*)", text):
        tail = m.group(1).strip()
        if tail and not re.search(r"\b(day|week|month|year|date|today|time|end|the)\b", tail):
            return True
    return False


# ----------------------------------------------------------------------------- dimensions

PROMISE_DIMS = {
    "queue": ("c.queue", "join cur_cases c on c.case_id = p.case_id"),
    "site": ("coalesce(a.site, 'self_serve')", "left join cur_agents a on a.agent_id = p.agent_id"),
    "channel": ("p.ptp_channel", ""),
    "product": ("p.product", ""),
    "prior_broken": ("least(p.prior_broken_ptp_count, 3)", ""),
    "test_cell": ("c.test_cell", "join cur_cases c on c.case_id = p.case_id"),
    "province": ("c.province_code", "join cur_cases c on c.case_id = p.case_id"),
    "strategy": ("c.strategy_code", "join cur_cases c on c.case_id = p.case_id"),
}


def _case_bucket_expr() -> str:
    return "c.current_bucket_calc" if config.PREFER_CORRECTED else "c.current_bucket"


def case_dims() -> dict:
    return {
        "queue": "c.queue", "product": "c.primary_product", "bucket": _case_bucket_expr(),
        "province": "c.province_code", "strategy": "c.strategy_code", "test_cell": "c.test_cell",
    }


# Qualifiers a question can put on the OPEN-CASE population. Each is an explicit, named boolean column; a recognised
# qualifier is always applied (and reported in `understood`), never silently dropped.
CASE_FILTERS = [
    (r"hardship flag|flagged (?:as )?(?:in )?hardship|on hardship|with hardship|hardship cases?", "c.hardship_flag", "hardship_flag"),
    (r"insolvency hold|on insolvency", "c.insolvency_hold_flag", "insolvency_hold_flag"),
    (r"deceased hold", "c.deceased_hold_flag", "deceased_hold_flag"),
    (r"cease[- ]contact", "c.cease_contact_flag", "cease_contact_flag"),
    (r"charged[- ]off", "c.chargeoff_flag", "chargeoff_flag"),
    (r"legal referral", "c.legal_referral_flag", "legal_referral_flag"),
]


def case_filters(q: str) -> tuple[str, list[str]]:
    """(extra SQL conditions, names of the filters applied) for the open-case population."""
    s = q.lower()
    conds, names = [], []
    for rx, expr, name in CASE_FILTERS:
        if re.search(rx, s):
            conds.append(expr)
            names.append(name)
    return "".join(f" and {c}" for c in conds), names


CONTACT_DIMS = {
    "hour": ("h.contact_hour_local", ""),
    "channel": ("h.channel_eff", ""),
    "province": ("c.province_code", "join cur_cases c on c.case_id = h.case_id"),
    "site": ("coalesce(a.site, 'none')", "left join cur_agents a on a.agent_id = h.agent_id"),
    "bucket": ("h.bucket_at_contact", ""),
    "strategy": ("h.strategy_code", ""),
}


def _group(dim_expr: str | None, name: str | None) -> tuple[str, str, str]:
    """(select-prefix, group by, order by)"""
    if not dim_expr:
        return "", "", ""
    return f"{dim_expr} as {name}, ", "group by 1", "order by 1"


BUCKET_NOTE_CORRECTED = ("Buckets recomputed from current_dpd: the raw current_bucket disagrees with current_dpd "
                         "for 3,807 of 96,000 open cases.")
BUCKET_NOTE_REFERENCE = ("Raw current_bucket used (matches the reference answer). Data-quality caveat: it disagrees with "
                         "current_dpd for 3,807 of 96,000 open cases; set insight.config.PREFER_CORRECTED = True to group by "
                         "the recomputed bucket instead.")


def bucket_note() -> str:
    return BUCKET_NOTE_CORRECTED if config.PREFER_CORRECTED else BUCKET_NOTE_REFERENCE

# ----------------------------------------------------------------------------- builders


def _promise_rate(q: str, ctx: Ctx, kind: str, force_random: bool = False) -> Plan | None:
    dim = detect_dimension(q)
    period = parse_period(q, ctx.as_of)
    randomised = force_random or "random" in q.lower()
    if randomised and not dim:
        dim = "test_cell"
    if dim and dim not in PROMISE_DIMS:
        return None
    joins, dim_expr = "", None
    if dim:
        dim_expr, joins = PROMISE_DIMS[dim]
    if randomised and "cur_cases c" not in joins:
        joins += " join cur_cases c on c.case_id = p.case_id"
    pref, grp, order = _group(dim_expr, dim)
    flag = "p.is_broken" if kind == "broken" else "p.is_kept"
    col = "broken_rate_pct" if kind == "broken" else "promise_kept_rate_pct"
    where = "p.is_resolved" + _date_window("p.ptp_due_date", period)
    if randomised:
        where += " and c.assignment_method = 'random' and c.test_cell in ('champion','challenger_A','challenger_B')"
    count_mode = bool(re.search(r"how many|number of|count of", q.lower())) and not re.search(r"rate|share|percent|proportion", q.lower())
    if count_mode:
        cname = "broken_promises" if kind == "broken" else "kept_promises"
        sql = (f"select {pref}count(*) filter (where {flag}) as {cname}, count(*) as resolved_promises "
               f"from cur_promises p {joins} where {where} {grp} {order}")
    else:
        sql = (f"select {pref}round(100.0 * avg(case when {flag} then 1 else 0 end), 1) as {col}, count(*) as promises "
               f"from cur_promises p {joins} where {where} {grp} {order}")
    metric = "broken_promise_rate" if kind == "broken" else "promise_kept_rate"
    notes = ["Open and cancelled promises are excluded (certified metric definition)."]
    if randomised:
        notes.append("Randomised cases only (assignment_method = 'random'); rule-based assignment is confounded.")
    return Plan(metric, sql, _understood(metric, ctx, dim, period, randomised_only=randomised or None), notes,
                ["PRC-COLL-010 §3.3"])


def _case_metric(q: str, ctx: Ctx, kind: str) -> Plan | None:
    dim = detect_dimension(q)
    period = parse_period(q, ctx.as_of)
    dims = case_dims()
    if dim and dim not in dims:
        return None
    pref, grp, order = _group(dims.get(dim), dim)
    notes = []
    if dim == "bucket":
        notes.append(bucket_note())
    fcond, fnames = case_filters(q)
    if kind == "avg_dpd":
        sql = (f"select {pref}round(avg(c.current_dpd), 1) as avg_dpd, count(*) as open_cases "
               f"from cur_cases c where c.is_open{fcond} {grp} {order}")
        return Plan("average_dpd", sql, _understood("average_dpd", ctx, dim, None, filters=fnames or None), notes)
    if kind == "open_cases":
        overdue = bool(re.search(r"overdue|amount owed|owing", q.lower()))
        measure = "count(*) as open_cases"
        if overdue:
            measure += ", round(sum(c.total_overdue), 2) as total_overdue_cad"
            notes.append("Overdue = collections_cases.total_overdue summed over open cases (outcome IS NULL), in CAD.")
        sql = f"select {pref}{measure} from cur_cases c where c.is_open{fcond} {grp} {order}"
        return Plan("open_cases", sql, _understood("open_cases", ctx, dim, None, open_definition="outcome IS NULL",
                                                   filters=fnames or None, measure="total_overdue" if overdue else None), notes)
    if kind == "avg_days_to_cure":
        sql = (f"select {pref}round(avg(c.days_to_cure), 1) as avg_days_to_cure, count(*) as cured_cases "
               f"from cur_cases c where c.cure_flag and c.days_to_cure is not null"
               f"{_date_window('c.cure_date', period)} {grp} {order}")
        notes.append("Cohort = cases that cured (cure_flag) with a cure_date in the period; days_to_cure is as recorded on the case.")
        return Plan("average_days_to_cure", sql, _understood("average_days_to_cure", ctx, dim, period, period_applies_to="cure_date"),
                    notes)
    if kind == "hardship_rate":
        sql = (f"select {pref}round(100.0 * avg(case when c.hardship_flag then 1 else 0 end), 1) as hardship_rate_pct, "
               f"count(*) as cases from cur_cases c where true{_date_window('c.case_open_date', period)} {grp} {order}")
        notes.append("Hardship is the agent/customer/model-recorded flag; it under-records true hardship (about 60% recall).")
        return Plan("hardship_rate", sql, _understood("hardship_rate", ctx, dim, period, period_applies_to="case_open_date"),
                    notes, ["POL-COLL-004 §4.2"])
    if kind == "cure_rate":
        days = within_days(q) or 30
        sql = (f"select {pref}round(100.0 * avg(case when c.cure_flag and c.days_to_cure <= {days} then 1 else 0 end), 1) "
               f"as cure_rate_{days}d_pct, count(*) as cases from cur_cases c where true"
               f"{_date_window('c.case_open_date', period)} {grp} {order}")
        notes.append(f"Cohort = cases opened in the period; cured = cure_flag and days_to_cure <= {days}.")
        return Plan("cure_rate", sql, _understood("cure_rate", ctx, dim, period, within_days=days,
                                                 period_applies_to="case_open_date"), notes)
    return None


def _contact_metric(q: str, ctx: Ctx, kind: str) -> Plan | None:
    dim = detect_dimension(q)
    period = parse_period(q, ctx.as_of)
    if kind == "attempts":
        dims = case_dims()
        if dim and dim not in dims:
            return None
        pref, grp, order = _group(dims.get(dim), dim)
        sql = (f"select {pref}round(avg(c.contact_attempts_total), 2) as avg_outbound_attempts, "
               f"count(*) as open_cases from cur_cases c where c.is_open {grp} {order}")
        notes = ["Uses the case-level counter collections_cases.contact_attempts_total (open cases only). contact_history "
                 "holds only about 1 outbound row per open case, so it under-counts attempts; ASSUMPTION: the counter is "
                 "the intended source."]
        if dim == "bucket":
            notes.append(bucket_note())
        return Plan("contact_attempts_per_case", sql, _understood("contact_attempts_per_case", ctx, dim, None), notes)
    if dim and dim not in CONTACT_DIMS:
        return None
    if kind == "out_of_hours":
        dim = dim or "province"
    if kind in ("cost_per_contact", "cost_per_rpc") and not dim and re.search(r"channel", q.lower()):
        dim = "channel"
    dim_expr, joins = CONTACT_DIMS.get(dim, (None, "")) if dim else (None, "")
    pref, grp, order = _group(dim_expr, dim)
    window = _contact_window("h.contact_ts_local", period, ctx.as_of)
    call_filter = "h.channel_eff = 'call' and h.direction = 'outbound'"
    if kind == "link_click":
        sql = (f"select {pref}round(100.0 * avg(case when h.link_clicked_flag then 1 else 0 end), 1) as link_click_rate_pct, "
               f"count(*) as payment_links_sent from cur_contacts h {joins} "
               f"where h.channel_eff = 'sms' and h.direction = 'outbound' and h.payment_link_id is not null{window} {grp} {order}")
        return Plan("payment_link_click_rate", sql, _understood("payment_link_click_rate", ctx, dim, period, scope="outbound SMS with a payment link"),
                    ["Sent = outbound SMS carrying a payment link (payment_link_id present); clicked = link_clicked_flag. "
                     "Rows flagged clicked that carry no payment_link_id are not counted. "
                     "Channel read as COALESCE(channel_v2, channel); the window ends before the snapshot date."], ["REL-0926"])
    if kind == "rpc_rate":
        sql = (f"select {pref}round(100.0 * avg(case when h.rpc_flag then 1 else 0 end), 1) as rpc_rate_pct, "
               f"count(*) as attempts from cur_contacts h {joins} where {call_filter}{window} {grp} {order}")
        return Plan("rpc_rate", sql, _understood("rpc_rate", ctx, dim, period, scope="outbound calls"),
                    ["Channel read as COALESCE(channel_v2, channel): the column was renamed on 2026-09-26 (REL-0926)."],
                    ["REL-0926"])
    if kind == "out_of_hours":
        sql = (f"select {pref}round(100.0 * avg(case when not h.within_permitted_hours_flag then 1 else 0 end), 2) "
               f"as out_of_hours_pct, count(*) as calls from cur_contacts h {joins} where {call_filter}{window} {grp} {order}")
        return Plan("out_of_hours_calls", sql,
                    _understood("out_of_hours_calls", ctx, dim, period, scope="outbound calls",
                                permitted_hours="07:00-21:00 customer local time"),
                    ["Permitted hours per POL-COLL-001 §5.1 (07:00-21:00 customer local time)."], ["POL-COLL-001 §5.1"])
    if kind in ("cost_per_contact", "cost_per_rpc"):
        denom = "nullif(count(h.contact_id), 0)" if kind == "cost_per_contact" else "nullif(sum(case when h.rpc_flag then 1 else 0 end), 0)"
        name = "cost_per_contact_cad" if kind == "cost_per_contact" else "cost_per_rpc_cad"
        sql = (f"select {pref}round(sum(h.contact_cost_cad) / {denom}, 4) as {name}, count(*) as contacts "
               f"from cur_contacts h {joins} where not h.is_system_event{window} {grp} {order}")
        return Plan(kind, sql, _understood(kind, ctx, dim, period, excludes="system events"), ["System events excluded."])
    return None


def _delinquency(q: str, ctx: Ctx) -> Plan | None:
    dim = detect_dimension(q)
    loans = bool(re.search(r"\bloans?\b", q.lower())) and "card" not in q.lower()
    if dim not in (None, "product"):
        return None
    if loans:
        pref, grp, order = _group("product_type" if dim else None, "product_type")
        sql = (f"select {pref}round(100.0 * avg(case when dpd >= 1 then 1 else 0 end), 2) as delinquency_rate_pct, "
               f"count(*) as accounts from raw_loan_accounts where snapshot_date = {_d(ctx.as_of)} "
               f"and chargeoff_flag is not true and dpd is not null {grp} {order}")
    else:
        pref, grp, order = _group("product_code" if dim else None, "product_code")
        sql = (f"select {pref}round(100.0 * avg(case when dpd >= 1 then 1 else 0 end), 2) as delinquency_rate_pct, "
               f"count(*) as accounts from raw_card_accounts where snapshot_date = {_d(ctx.as_of)} "
               f"and account_status in ('active','blocked') and dpd is not null {grp} {order}")
    return Plan("delinquency_rate", sql, _understood("delinquency_rate", ctx, dim, None, as_of=ctx.as_of.isoformat()),
                ["dpd is missing for about 8% of accounts and is excluded; cycles_delinquent counts cycles, not days."])


def _roll_rate(q: str, ctx: Ctx) -> Plan | None:
    ends = parse_month_ends(q, ctx.as_of)
    sb = start_bucket(q)
    dim = detect_dimension(q)
    if len(ends) != 2 or not sb or dim not in (None, "product") or sb == "180+":
        return None
    lo, hi = (int(x) for x in sb.split("-"))
    pref, grp, order = _group("a.product_type" if dim else None, "product_type")
    me_col = "month_end_parsed" if config.PREFER_CORRECTED else "month_end_parsed_ref"
    sql = (f"with s as (select account_id, product_type, {me_col} as me, dpd from cur_snapshot where dpd is not null) "
           f"select {pref}round(100.0 * avg(case when b.dpd > {hi} then 1 else 0 end), 1) as roll_rate_pct, count(*) as accounts "
           f"from s a join s b on a.account_id = b.account_id and b.me = {_d(ends[1])} "
           f"where a.me = {_d(ends[0])} and a.dpd between {lo} and {hi} {grp} {order}")
    notes = ["Accounts with missing dpd are excluded; 'worse bucket' = dpd above the start bucket's upper bound."]
    if config.PREFER_CORRECTED:
        notes.append("month_end_date arrives in four formats (ISO, DD/MM/YYYY, MM/DD/YYYY, YYYYMMDD); all are parsed.")
    else:
        notes.append("Reference-compatible parsing (ISO, DD/MM/YYYY, YYYYMMDD). Data-quality caveat: some snapshot rows use "
                     "MM/DD/YYYY and are dropped this way; set insight.config.PREFER_CORRECTED = True to include them "
                     "(this changes the result, e.g. auto_loan 7.4% over 430 accounts becomes 8.2% over 466).")
    return Plan("roll_rate", sql, _understood("roll_rate", ctx, dim, None, from_month_end=ends[0].isoformat(),
                                             to_month_end=ends[1].isoformat(), start_bucket=sb), notes)


def _writeoffs(q: str, ctx: Ctx) -> Plan | None:
    period = parse_period(q, ctx.as_of)
    if not period:
        return None
    sql = ("with w as (select 'card' as product, chargeoff_date as d, chargeoff_amount as amt from raw_card_accounts "
           "where chargeoff_flag union all select product_type, chargeoff_date, chargeoff_amount from raw_loan_accounts "
           "where chargeoff_flag) "
           f"select product, count(*) as accounts_charged_off, round(sum(amt), 2) as amount_cad from w "
           f"where d between {_d(period.start)} and {_d(period.end)} group by 1 order by 1")
    return Plan("write_offs", sql, _understood("write_off_rate", ctx, "product", period, period_applies_to="chargeoff_date"),
                ["Cards and loans (personal/auto loans, mortgages, lines of credit); deposits have no charge-off fields."])


def _collected(q: str, ctx: Ctx) -> Plan | None:
    period = parse_period(q, ctx.as_of)
    sql = (f"select round(sum(p.amount_paid_against), 2) as amount_paid_cad, count(*) as promises_with_payment "
           f"from cur_promises p where p.paid_date is not null{_date_window('p.paid_date', period)}")
    return Plan("collected_amount", sql, _understood("collected_amount", ctx, None, period, period_applies_to="paid_date"),
                ["Sum of amount_paid_against on promises, by the date the payment was received."])


def _dispute_compare(q: str, ctx: Ctx) -> Plan | None:
    period = parse_period(q, ctx.as_of)
    n = n_days_before(q)
    if not period or not n:
        return None
    prior_start, prior_end = period.start - timedelta(days=n), period.start - timedelta(days=1)
    la = f"{period.start.day}-{period.end.day} {period.start.strftime('%b')}"
    lb = f"{prior_start.day}-{prior_end.day} {prior_end.strftime('%b')}"
    sql = (f"select case when h.contact_ts_local >= {_ts(period.start)} then '{la}' else '{lb}' end as period, "
           f"count(*) as disputes from cur_contacts h where h.dispute_raised_flag "
           f"and h.contact_ts_local >= {_ts(prior_start)} and h.contact_ts_local < {_ts(period.end + timedelta(days=1))} "
           f"group by 1 order by 1")
    notes = ["contact_history records disputes with dispute_raised_flag only (no dispute type), so all dispute types are counted."]
    return Plan("dispute_comparison", sql, _understood("dispute_count", ctx, None, period, compared_with=lb),
                notes, ["IR-2026-0914", "PRC-COLL-012 §6.2"])


def _explain_change(q: str, ctx: Ctx) -> Plan | None:
    period = parse_period(q, ctx.as_of)
    if not period:
        return None
    lo, hi = period.start - timedelta(days=3), period.end + timedelta(days=3)
    sql = ("select h.contact_date_local as day, count(*) as outbound_call_attempts, "
           "sum(case when h.rpc_flag then 1 else 0 end) as rpcs from cur_contacts h "
           f"where h.channel_eff = 'call' and h.direction = 'outbound' "
           f"and h.contact_date_local between {_d(lo)} and {_d(hi)} group by 1 order by 1")
    ev = ctx.con.execute(
        "select event_id, event_type, description, reference_doc_id from raw_ops_events "
        "where cast(start_ts as date) <= ? and cast(end_ts as date) >= ?", [period.end, period.start]).fetchall()
    sources = [e[3] for e in ev if e[3]]
    why = " ".join(f"{e[0]} ({e[1]}): {e[2]}." for e in ev)

    def summarize(cols, rows):
        by = {r[0]: r[1] for r in rows}
        inside = [v for k, v in by.items() if period.start <= k <= period.end]
        outside = [v for k, v in by.items() if not (period.start <= k <= period.end)]
        msg = ""
        if inside and outside:
            msg = (f"Average outbound call attempts were {sum(inside) / len(inside):,.0f}/day during {period.label} "
                   f"versus {sum(outside) / len(outside):,.0f}/day in the surrounding days. ")
        return msg + ("Likely cause: " + why if why else "No logged operational event overlaps this window.")

    notes = ["The drop is an operational outage, not a change in customer behaviour."] if ev else []
    return Plan("explain_change", sql, _understood("outbound_call_attempts", ctx, "day", period,
                                                  matched_events=[e[0] for e in ev]), notes, sources, summarize)


def _pilot_vulnerable(q: str, ctx: Ctx) -> Plan | None:
    sql = ("select case when a.vulnerable_customer_certified then 'certified' else 'not_certified' end as agent_group, "
           "round(100.0 * avg(case when c.cure_flag and c.days_to_cure <= 30 then 1 else 0 end), 1) as cure_rate_30d_pct, "
           "count(*) as cases from cur_cases c join cur_agents a on a.agent_id = c.assigned_agent_id "
           "where c.assignment_method = 'random' and c.hardship_flag group by 1 order by 1")

    def summarize(cols, rows):
        r = {x[0]: x[1] for x in rows}
        if "certified" in r and "not_certified" in r:
            diff = r["certified"] - r["not_certified"]
            direction = "higher" if diff > 0 else "lower" if diff < 0 else "the same"
            return (f"Agents certified for vulnerable customers achieve a {direction} 30-day cure rate on randomised "
                    f"hardship cases ({r['certified']}% vs {r['not_certified']}%, difference {diff:+.1f} pts).")
        return ""

    return Plan("pilot_vulnerable_certified", sql, _understood("cure_rate", ctx, "agent_group", None, randomised_only=True),
                ["Randomised cases only; certification is read from the agent roster (assigned agent)."],
                ["POL-COLL-004 §4.5"], summarize)


# ----------------------------------------------------------------------------- dispatcher

def _kind_promise(q: str) -> str:
    s = q.lower()
    return "kept" if re.search(r"kept|keep", s) and not re.search(r"broken|break", s) else "broken"


RULES = [
    ("pilot", lambda s: "pilot" in s and "vulnerab" in s, lambda q, c: _pilot_vulnerable(q, c)),
    ("champion", lambda s: "champion" in s or "challenger" in s,
     lambda q, c: _promise_rate(q, c, _kind_promise(q), force_random=True)),
    ("explain", lambda s: bool(re.search(r"\bwhy\b.*(drop|fall|fell|decreas|increas|spike|jump)", s)),
     lambda q, c: _explain_change(q, c)),
    ("disputes", lambda s: "dispute" in s and ("compared" in s or "raised" in s), lambda q, c: _dispute_compare(q, c)),
    ("roll", lambda s: "rolled to a worse" in s or "roll rate" in s, lambda q, c: _roll_rate(q, c)),
    ("writeoff", lambda s: bool(re.search(r"charged off|charge-off|write-off|written off", s)), lambda q, c: _writeoffs(q, c)),
    ("linkclick", lambda s: bool(re.search(r"payment links?", s)) and "click" in s, lambda q, c: _contact_metric(q, c, "link_click")),
    ("collected", lambda s: bool(re.search(r"paid against promises|amount collected|how much did we collect|dollars collected", s)),
     lambda q, c: _collected(q, c)),
    ("promise", lambda s: bool(re.search(r"promise|\bptp\b", s)) and bool(re.search(r"kept|keep|broken|break", s))
     and bool(re.search(r"rate|share|percent|proportion|how many|change with", s)),
     lambda q, c: _promise_rate(q, c, _kind_promise(q))),
    ("attempts", lambda s: "attempts" in s and "case" in s, lambda q, c: _contact_metric(q, c, "attempts")),
    ("cost_rpc", lambda s: bool(re.search(r"cost per (right-party|rpc)", s)), lambda q, c: _contact_metric(q, c, "cost_per_rpc")),
    ("cost", lambda s: bool(re.search(r"cost per (contact|attempt)|contact cost", s)),
     lambda q, c: _contact_metric(q, c, "cost_per_contact")),
    ("rpc", lambda s: "right-party contact" in s or "rpc" in s, lambda q, c: _contact_metric(q, c, "rpc_rate")),
    ("ooh", lambda s: bool(re.search(r"outside (of )?(permitted|calling|allowed)|out of hours|outside .*hours", s)),
     lambda q, c: _contact_metric(q, c, "out_of_hours")),
    ("avg_dpd", lambda s: bool(re.search(r"average (days past due|dpd)|mean dpd", s)), lambda q, c: _case_metric(q, c, "avg_dpd")),
    ("days_to_cure", lambda s: bool(re.search(r"(days|time) to cure|(average|mean)\b.*\bcure", s)),
     lambda q, c: _case_metric(q, c, "avg_days_to_cure")),
    ("cure", lambda s: "cure" in s, lambda q, c: _case_metric(q, c, "cure_rate")),
    ("hardship", lambda s: bool(re.search(r"hardship (rate|share)", s)), lambda q, c: _case_metric(q, c, "hardship_rate")),
    ("delinq", lambda s: "delinquen" in s or ("days past due" in s and ("today" in s or "active" in s)),
     lambda q, c: _delinquency(q, c)),
    ("open", lambda s: "open" in s and "case" in s and bool(re.search(r"how many|number of|count|overdue|amount owed", s)),
     lambda q, c: _case_metric(q, c, "open_cases")),
]


def recognise(question: str) -> str | None:
    s = question.lower()
    for name, pred, _ in RULES:
        if pred(s):
            return name
    return None


def plan(question: str, ctx: Ctx) -> Plan | None:
    s = question.lower()
    for name, pred, build in RULES:
        if pred(s):
            if name not in ("explain", "disputes", "roll", "writeoff", "collected", "linkclick") and \
                    unsupported_breakdown(question, detect_dimension(question)):
                return None
            return build(question, ctx)
    return None
