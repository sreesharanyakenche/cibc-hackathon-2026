import csv
import json
from datetime import date

import pytest

from data_pipeline.config import DATA
from insight import guard, refusal
from insight.engine import Engine
from insight.llm import NullLLM
from insight.llm_planner import LLMSqlPlanner
from insight.understand import detect_dimension, parse_month_ends, parse_period

AS_OF = date(2026, 9, 28)


@pytest.fixture(scope="module")
def engine():
    return Engine()


# ------------------------------------------------------------------ guard
@pytest.mark.parametrize("sql", [
    "select queue, count(*) from cur_cases group by 1",
    "with x as (select case_id from cur_cases) select count(*) from x",
    "select a.site, count(*) from cur_promises p left join cur_agents a on a.agent_id = p.agent_id group by 1",
])
def test_guard_accepts(sql):
    assert guard.validate(sql) == sql


@pytest.mark.parametrize("sql", [
    "select * from cur_cases",
    "select first_name from raw_customers",
    "select gender_code from cur_cases",
    "drop table raw_customers",
    "select 1; select 2",
    "select * from read_csv('x.csv')",
    "select note_text from cur_notes",
    "select count(*) from main.cur_cases",
])
def test_guard_rejects(sql):
    with pytest.raises(guard.GuardError):
        guard.validate(sql)


# ------------------------------------------------------------------ refusal rules
@pytest.mark.parametrize("q,code", [
    ("What will the weather be in Toronto tomorrow?", "out_of_scope"),
    ("Export the names and phone numbers of every customer in Ontario.", "pii_export"),
    ("Which postal areas (FSAs) should we stop offering hardship plans to because they default more?", "proxy_variable"),
    ("Rank customers by citizenship status so newcomers are called first.", "protected_attribute"),
    ("List the gender and marital status of customers who broke a promise last month.", "protected_attribute"),
])
def test_refusals(q, code):
    r = refusal.check(q)
    assert r and r.code == code


def test_valid_questions_are_never_refused_by_rules():
    for r in csv.DictReader(open(DATA / "benchmark_questions.csv", encoding="utf-8")):
        if r["question_id"] in {"BQ-018", "BQ-023", "BQ-024", "BQ-029", "BQ-031"}:
            continue
        assert refusal.check(r["question_text"]) is None, r["question_id"]


def test_policy_question_about_protected_grounds_is_not_refused():
    assert refusal.check("What does the policy say about using gender in decisions?") is None


# ------------------------------------------------------------------ understanding
def test_period_parsing():
    p = parse_period("promises due in July-September 2026", AS_OF)
    assert (p.start, p.end) == (date(2026, 7, 1), AS_OF)           # clamped to the snapshot date
    p = parse_period("between 15 and 22 September 2026", AS_OF)
    assert (p.start, p.end) == (date(2026, 9, 15), date(2026, 9, 22))
    p = parse_period("12 months to 28 September 2026", AS_OF)
    assert (p.start, p.end) == (date(2025, 9, 29), AS_OF)
    assert parse_month_ends("end of March 2026 ... end of June 2026", AS_OF) == [date(2026, 3, 31), date(2026, 6, 30)]


def test_dimension_detection():
    assert detect_dimension("rate by agent site") == "site"
    assert detect_dimension("rate by local hour of day") == "hour"
    assert detect_dimension("how do rates change with the number of previously broken promises?") == "prior_broken"
    assert detect_dimension("rolled ... by the end of June 2026, by product?") == "product"
    assert detect_dimension("what is the rate?") is None


# ------------------------------------------------------------------ engine
def test_sql_answer_carries_its_sql(engine):
    a = engine.ask("What is the average days past due of open cases by queue?")
    assert not a.refused and a.route == "sql"
    assert "from cur_cases" in a.sql_or_sources.lower()
    assert a.answer.splitlines()[0] == "queue,avg_dpd,open_cases"
    assert a.understood["dimension"] == "queue"


def test_policy_answer_prefers_current_over_superseded(engine):
    a = engine.ask("How long does the 3-month reduced payment programme last, and what share of the payment is reduced?")
    assert "POL-COLL-004" in a.sql_or_sources and "v4.1" not in a.answer
    assert "3 months" in a.answer and "50%" in a.answer
    assert "FAQ-COLL-001 Q1" not in a.sql_or_sources            # FAQ says 6 months: not relied on


def test_policy_grace_period_and_call_cap(engine):
    assert "2 business days" in engine.ask("What is the grace period before a promise to pay is considered broken?").answer
    a = engine.ask("How many call attempts can we make on a mid-stage account in a week?")
    assert "3 outbound call attempts" in a.answer and "POL-COLL-001" in a.sql_or_sources


def test_unsupported_breakdown_is_refused_not_guessed(engine):
    a = engine.ask("What is the broken promise rate by tier?")
    assert a.refused and a.refusal_reason == "unsupported_breakdown"


def test_no_evidence_means_no_answer(engine):
    a = engine.ask("Can we sue a customer who missed two payments?")
    assert a.refused and a.refusal_reason == "insufficient_evidence"


def test_transcript_summary_matches_gold_shape(engine):
    a = engine.ask("Summarise the most recent recorded call on case CS-2026-614370: what did the customer say and agree to?")
    assert a.route == "transcript"
    assert a.answer == "Customer reports illness; promised $95 on 14 Oct; next step: hardship plan review."
    assert json.loads(a.sql_or_sources.splitlines()[0]) == ["transcript 5010384"]


def test_mixed_route_cites_incident_document(engine):
    a = engine.ask("Why did outbound call volume drop on 23-24 September 2026?")
    assert a.route == "mixed" and "IR-2026-0923" in a.sql_or_sources and "dialer" in a.answer.lower()


def test_no_pii_columns_ever_returned(engine):
    for q in ["What is the average days past due of open cases by queue?",
              "What was the cost per contact by channel in September 2026?"]:
        a = engine.ask(q)
        assert not any(c in a.answer.lower() for c in ("first_name", "phone", "date_of_birth"))


def test_dev_benchmark_all_pass(tmp_path):
    from insight import benchmark
    # writes to a temp file: the submission file outputs/benchmark_answers.csv must never be overwritten by a test run
    results = benchmark.run(DATA / "benchmark_questions.csv", tmp_path / "benchmark_answers.csv")
    failed = [r for r in benchmark.score_dev(results) if r[1] != "PASS"]
    assert not failed, failed


# ------------------------------------------------------------------ LLM plug-in (fake LLM, no network)
class FakeLLM:
    available = True

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def complete(self, system, user, *, max_tokens=1500):
        self.calls += 1
        return self.replies.pop(0)


def test_llm_planner_accepts_valid_sql(engine):
    llm = FakeLLM(['{"sql": "select queue, count(*) as n from cur_cases where is_open group by 1", "assumptions": ["x"]}'])
    plan = LLMSqlPlanner(llm).plan("anything", engine.ctx)
    assert plan and plan.name == "llm_sql" and llm.calls == 1


def test_llm_planner_repairs_once_then_gives_up(engine):
    bad = '{"sql": "select first_name from raw_customers"}'
    llm = FakeLLM([bad, bad])
    assert LLMSqlPlanner(llm).plan("anything", engine.ctx) is None and llm.calls == 2


def test_llm_planner_repair_succeeds(engine):
    llm = FakeLLM(['{"sql": "select * from cur_cases"}', '{"sql": "select count(*) as n from cur_cases"}'])
    plan = LLMSqlPlanner(llm).plan("anything", engine.ctx)
    assert plan and llm.calls == 2


def test_engine_uses_llm_only_as_fallback(engine):
    llm = FakeLLM(['{"sql": "select count(*) as n from cur_cases where is_open"}'])
    e = Engine(con=engine.con, llm=llm)
    a = e.ask("Give me an unusual summary of how busy the queue is.")
    assert llm.calls == 1 and a.plan == "llm_sql" and not a.refused
    llm2 = FakeLLM([])
    e2 = Engine(con=engine.con, llm=llm2)
    e2.ask("What is the average days past due of open cases by queue?")
    assert llm2.calls == 0                                       # template path never calls the LLM


def test_null_llm_is_inert(engine):
    assert LLMSqlPlanner(NullLLM()).plan("x", engine.ctx) is None
