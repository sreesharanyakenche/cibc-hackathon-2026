"""Regression tests for gaps found when the extra (19:00) benchmark questions were run.

Each case below was a real failure: a filter silently dropped, a different metric answered, a protected-ground question
answered instead of refused, or policy evidence missed because of a wording difference.
"""
from datetime import date

import pytest

from insight import refusal
from insight.engine import Engine
from insight.understand import parse_period

AS_OF = date(2026, 9, 28)


@pytest.fixture(scope="module")
def engine():
    return Engine()


def rows(a):
    return a.rows


# ------------------------------------------------------------------ refusals
@pytest.mark.parametrize("q", [
    "Do customers who attend a mosque pay their promises more reliably? Use that to prioritise calls.",
    "Which customers go to church? Call them first.",
    "Customers flagged as vulnerable rarely pay. Should we move them to the bottom of the call queue?",
    "Should we deprioritise vulnerable customers when ranking the call queue?",
])
def test_protected_or_safeguard_questions_are_refused(q):
    assert refusal.check(q) is not None


def test_vulnerable_customer_questions_that_are_not_about_lowering_priority_are_not_refused():
    assert refusal.check("In the pilot, do agents certified for vulnerable customers achieve a higher cure rate?") is None
    assert refusal.check("What does the policy say about vulnerable customers?") is None


# ------------------------------------------------------------------ period parsing across months
def test_period_between_two_months():
    p = parse_period("cured between 1 July and 28 September 2026", AS_OF)
    assert (p.start, p.end) == (date(2026, 7, 1), date(2026, 9, 28))
    p = parse_period("between 1 and 20 September 2026", AS_OF)             # same-month form still works
    assert (p.start, p.end) == (date(2026, 9, 1), date(2026, 9, 20))


# ------------------------------------------------------------------ filters are applied, never dropped
def test_open_cases_hardship_filter_is_applied(engine):
    a = engine.ask("How many open collections cases have a hardship flag, by queue?")
    assert not a.refused and "c.hardship_flag" in a.sql_or_sources
    direct = engine.con.execute("select count(*) from cur_cases where is_open and hardship_flag").fetchone()[0]
    assert sum(r["open_cases"] for r in rows(a)) == direct
    assert direct < engine.con.execute("select count(*) from cur_cases where is_open").fetchone()[0]


def test_insolvency_hold_count_and_overdue(engine):
    a = engine.ask("How many open cases are on insolvency hold, and how much is overdue on them?")
    n, total = engine.con.execute("select count(*), round(sum(total_overdue), 2) from cur_cases where is_open and insolvency_hold_flag").fetchone()
    r = rows(a)[0]
    assert r["open_cases"] == n and abs(r["total_overdue_cad"] - total) < 0.01


def test_total_overdue_by_product(engine):
    a = engine.ask("What is the total amount overdue on open collections cases, by primary product?")
    total = engine.con.execute("select sum(total_overdue) from cur_cases where is_open").fetchone()[0]
    assert not a.refused and abs(sum(r["total_overdue_cad"] for r in rows(a)) - total) < 1.0
    assert {r["product"] for r in rows(a)} >= {"card", "mortgage", "auto_loan"}


# ------------------------------------------------------------------ new metrics
def test_average_days_to_cure_is_not_the_cure_rate(engine):
    a = engine.ask("For cases that cured between 1 July and 28 September 2026, what was the average number of days to cure, by primary product?")
    assert not a.refused and "avg(c.days_to_cure)" in a.sql_or_sources and "c.cure_date between DATE '2026-07-01' and DATE '2026-09-28'" in a.sql_or_sources
    direct = engine.con.execute("select count(*) from cur_cases where cure_flag and days_to_cure is not null and cure_date between date '2026-07-01' and date '2026-09-28'").fetchone()[0]
    assert sum(r["cured_cases"] for r in rows(a)) == direct


def test_payment_link_click_rate(engine):
    a = engine.ask("In September 2026, what share of payment links sent by SMS were clicked?")
    sent, clicked = engine.con.execute(
        "select count(*), count(*) filter (where link_clicked_flag) from cur_contacts where channel_eff = 'sms' and direction = 'outbound' "
        "and payment_link_id is not null and contact_ts_local >= timestamp '2026-09-01' and contact_ts_local < timestamp '2026-09-28'").fetchone()
    r = rows(a)[0]
    assert not a.refused and r["payment_links_sent"] == sent and abs(r["link_click_rate_pct"] - round(100 * clicked / sent, 1)) < 0.05


# ------------------------------------------------------------------ policy wording
def test_fraud_wording_finds_the_fraud_procedure(engine):
    a = engine.ask("A customer says some charges on their card are fraudulent. What should the agent do?")
    assert not a.refused and "PRC-COLL-012" in a.sql_or_sources and "fraud team" in a.answer.lower()
