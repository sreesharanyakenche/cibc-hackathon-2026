import json
from datetime import date

import duckdb
import numpy as np
import polars as pl
import pytest

from decisioning import actions, config, features as F, gates
from decisioning.gates import Context
from decisioning.model import PtpModel
from decisioning.orchestrator import Decisioner
from decisioning.records import RecordStore, TamperError

THRESH = config.BANDS


def ctx(**kw) -> Context:
    base = dict(ptp_id="PTP-1", as_of=date(2026, 9, 28), due_date=date(2026, 10, 2), risk=0.6, band="medium")
    base.update(kw)
    return Context(**base)


def decide(**kw):
    c = ctx(**kw)
    c.band = actions.band_for(c.risk, THRESH) if "band" not in kw else kw["band"]
    res = gates.run_gates(c)
    return c, res, actions.choose(c, res)


def fired(c, res, ch, kind=None):
    return {t["trigger"] for t in actions.review_triggers(c, res, ch, THRESH)
            if t["fired"] and (kind is None or t.get("kind", "review") == kind)}


# =================================================================== registry / leakage
def test_registry_audit_is_clean():
    assert F.audit_registry() == []


def test_model_uses_no_excluded_gate_only_or_diagnostic_features():
    names = set(F.MODEL_FEATURE_NAMES)
    assert not names & {f.name for f in F.FEATURES if f.purpose in ("excluded", "gate_only", "diagnostic")}
    assert not names & {"ptp_amount", "ptp_to_overdue_ratio", "overdue_at_creation", "g_hardship_text", "g_hardship_flag_case"}


def test_txn_benefit_is_excluded_from_the_production_model():
    f = next(x for x in F.FEATURES if x.name == "txn_benefit_3m")
    assert f.purpose == "excluded" and f.tag == "proxy_risk"
    assert "txn_benefit_3m" not in F.MODEL_FEATURE_NAMES
    assert "txn_benefit_3m" not in PtpModel("v1").names                      # the artifact that production loads
    assert "txn_benefit_3m" not in json.loads((config.MODEL_DIR / "ptp_break_v1.json").read_text())["features"]


def test_feature_sql_never_touches_outcome_columns_or_the_stored_prior_broken_count():
    sql = F.feature_sql().lower()
    for col in ("ptp_status", "amount_paid_against", "paid_date", "broken_reason_agent_coded", "promises_kept",
                "promises_broken", "cure_flag", "vulnerab", "gender", "marital", "citizenship"):
        assert col not in sql.replace("is_broken", ""), col
    assert "prior_broken_ptp_count" not in sql.replace(" as prior_broken_ptp_count", "")   # the leaky stored column is not read
    assert sql.count("cast(q.status_ts as date) < b.created_date") == 2                      # customer and case history are point-in-time
    assert "q.is_resolved" in sql
    assert any("b.prior_broken_ptp_count" in c for c in F.FORBIDDEN_SOURCE_COLUMNS)


def test_trained_models_exclude_amount_features_and_protected_attributes():
    m = PtpModel("v1")
    assert not set(m.names) & {"ptp_amount", "ptp_to_overdue_ratio", "overdue_at_creation", "entry_overdue"}
    assert m.meta["uses_protected_attributes"] is False and not m.meta.get("reference_only")
    ref = json.loads((config.MODEL_DIR / "ptp_break_v1_reference_amounts.json").read_text())
    assert ref["reference_only"] is True
    assert set(ref["features"]) - set(m.names) == {"ptp_amount", "ptp_to_overdue_ratio", "overdue_at_creation", "entry_overdue"}


def test_time_split_is_strictly_ordered():
    assert config.TRAIN_END < config.VALID_END


@pytest.fixture(scope="module")
def con():
    return F.connect(read_only=True)


def test_batch_and_single_row_features_are_identical(con):
    ids = [r[0] for r in con.execute(
        "select ptp_id from cur_promises where ptp_created_ts >= timestamp '2026-06-01' order by hash(ptp_id) limit 30").fetchall()]
    batch = F.compute_features(con, "p.ptp_id in (" + ",".join(f"'{i}'" for i in ids) + ")").sort("ptp_id")
    for i in ids[:12]:
        one = F.compute_features(con, f"p.ptp_id = '{i}'")
        row_b = batch.filter(pl.col("ptp_id") == i)
        for name in F.MODEL_FEATURE_NAMES:
            a, b = one[name][0], row_b[name][0]
            assert (a is None and b is None) or (a is not None and b is not None and abs(float(a) - float(b)) < 1e-9), (i, name, a, b)


def test_scores_are_identical_batch_vs_single(con):
    m = PtpModel("v1")
    ids = [r[0] for r in con.execute("select ptp_id from cur_promises where ptp_status = 'open' order by hash(ptp_id) limit 20").fetchall()]
    batch = F.compute_features(con, "p.ptp_id in (" + ",".join(f"'{i}'" for i in ids) + ")")
    sb = dict(zip(batch["ptp_id"], m.score(batch)))
    for i in ids[:8]:
        assert abs(float(m.score(F.compute_features(con, f"p.ptp_id = '{i}'"))[0]) - sb[i]) < 1e-9


# ---- fix: prior_broken_ptp_count is point-in-time
def test_prior_broken_count_uses_only_outcomes_known_at_creation(con):
    ids = [r[0] for r in con.execute(
        "select ptp_id from cur_promises where ptp_sequence_in_case > 1 and is_resolved and ptp_created_ts >= timestamp '2026-06-01' "
        "order by hash(ptp_id) limit 4000").fetchall()]
    feats = F.compute_features(con, "p.ptp_id in (" + ",".join(f"'{i}'" for i in ids) + ")", names=["prior_broken_ptp_count"])
    con.register("sample_ids", pl.DataFrame({"ptp_id": ids}).to_arrow())
    # independent recomputation: broken promises on the same case whose outcome was recorded before the creation date
    known = con.execute("""
        select p.ptp_id,
               count(q.ptp_id) filter (where q.is_broken and cast(q.status_ts as date) < cast(p.ptp_created_ts as date)) as known,
               any_value(p.prior_broken_ptp_count) as stored
        from cur_promises p join sample_ids s using(ptp_id)
        left join cur_promises q on q.case_id = p.case_id and q.ptp_id <> p.ptp_id
        group by 1""").pl()
    j = feats.join(known, on="ptp_id")
    assert len(j) > 3000
    assert (j["prior_broken_ptp_count"].cast(pl.Int64) == j["known"].cast(pl.Int64)).all()
    leaked = j.filter(pl.col("stored") > pl.col("prior_broken_ptp_count"))
    assert len(leaked) > 0, "the stored count should include outcomes that were not yet known for some promises"
    assert (j["prior_broken_ptp_count"] <= j["stored"]).all()          # the corrected count is never larger than the leaky one


# ---- fix: t_pressure regex
@pytest.mark.parametrize("text,expected", [
    ("this is an issue with the late fee", 0), ("tissue", 0), ("pursue the matter", 0), ("courte durée", 0), ("a courtesy call", 0),
    ("we will sue and garnish your wages", 1), ("going to court next week", 1), ("legal action will follow", 1),
    ("garnishment started", 1), ("on vous poursuit", 1), ("on saisit votre salaire", 1),
])
def test_t_pressure_matches_whole_words_only(text, expected):
    expr = next(f.expr for f in F.FEATURES if f.name == "t_pressure")
    c = duckdb.connect()
    got = c.execute(f"select {expr} from (select ? as note_text, 'N-1' as note_id, 5 as word_count) n", [text]).fetchone()[0]
    assert got == expected, text


# ---- fix: deterministic joins
def test_derived_tables_have_unique_keys(con):
    assert F.duplicate_keys(con) == {t: 0 for t in F.DERIVED_KEYS}


def test_features_are_deterministic_across_thread_counts_and_unique_per_promise(con):
    known = ["PTP-5008471", "PTP-1009631"]       # promises of customers with two CIFs / duplicate bureau rows before the fix
    ids = known + [r[0] for r in con.execute("select ptp_id from cur_promises where ptp_created_ts >= timestamp '2026-07-01' "
                                             "order by hash(ptp_id) limit 600").fetchall()]
    where = "p.ptp_id in (" + ",".join(f"'{i}'" for i in ids) + ")"
    results = []
    for threads in (1, 8, 3):
        con.execute(f"pragma threads={threads}")
        results.append(F.compute_features(con, where).sort("ptp_id"))
    con.execute("pragma threads=8")
    assert results[0]["ptp_id"].n_unique() == len(results[0])                  # one row per promise
    from polars.testing import assert_frame_equal
    for other in results[1:]:
        # identical except for float summation order (sal_avg_3m differs by ~2e-12 with another thread count)
        assert_frame_equal(results[0], other, check_exact=False, rel_tol=1e-9, abs_tol=1e-9)
    assert len(results[0].filter(pl.col("ptp_id").is_in(known))) == 2


# =================================================================== gates and ranking (pure)
def test_low_risk_with_reminder_is_restraint():
    assert decide(risk=0.3, reminder_scheduled=True)[2].action == "no_action"


def test_low_risk_without_reminder_gets_a_sms():
    assert decide(risk=0.3, reminder_scheduled=False)[2].action == "sms_payment_link"


def test_bands_pick_increasing_intensity():
    assert decide(risk=0.55)[2].action == "sms_payment_link"
    assert decide(risk=0.70)[2].action == "call_evening"
    assert decide(risk=0.90)[2].action == "call_evening"


@pytest.mark.parametrize("flag", ["insolvency_hold", "deceased", "cease_contact", "representative"])
def test_hard_stops_block_all_contact(flag):
    c, res, ch = decide(risk=0.95, **{flag: True})
    assert ch.action == "no_action"
    assert not (gates.allowed_actions(res) & gates.CONTACT_ACTIONS)


# ---- fix: agency / legal / charged-off / settled gates
@pytest.mark.parametrize("kw", [
    dict(case_status="placed_agency"), dict(case_status="legal"), dict(case_status="charged_off"), dict(case_status="settled"),
    dict(case_status="closed_paid"), dict(case_status="cured"),
    dict(agency_placed=True), dict(legal_handling=True), dict(charged_off=True),
])
def test_case_stage_blocks_all_direct_contact(kw):
    c, res, ch = decide(risk=0.95, **kw)
    gate = next(r for r in res if r.gate == "case_stage")
    assert gate.status == "stop" and gate.citation is None and "do not state" in gate.detail     # honest: not a documented policy
    assert ch.action == "no_action" and not (gates.allowed_actions(res) & gates.CONTACT_ACTIONS)


def test_active_in_house_case_passes_the_stage_gate():
    c, res, ch = decide(risk=0.7, case_status="open")
    assert next(r for r in res if r.gate == "case_stage").status == "pass" and ch.action == "call_evening"
    assert next(r for r in gates.run_gates(ctx(case_status="insolvency_hold", insolvency_hold=True)) if r.gate == "case_stage").status == "pass"


# ---- hardship statuses
def test_hardship_status_levels():
    assert gates.hardship_status(ctx()) == "none"
    assert gates.hardship_status(ctx(hardship_text=True)) == "possible"                                   # note wording only
    assert gates.hardship_status(ctx(hardship_flag=True, hardship_source="model")) == "possible"          # model-sourced flag
    assert gates.hardship_status(ctx(hardship_flag=True, hardship_source="agent_coded")) == "recorded"    # a person recorded it
    assert gates.hardship_status(ctx(hardship_flag=True, hardship_source="customer_form")) == "recorded"
    assert gates.hardship_status(ctx(hardship_flag=True, hardship_source="model", hardship_confirmed=True)) == "confirmed"


def test_hardship_is_route_only_and_never_adds_pressure():
    prog = [{"program_id": "HP-LOAN-RED3", "approver": "agent"}]
    c, res, ch = decide(risk=0.95, hardship_text=True, eligible_programmes=prog)
    assert ch.action == "specialist_review"                                  # unconfirmed -> a human confirms first
    assert not (gates.allowed_actions(res) & gates.CONTACT_ACTIONS)
    c, res, ch = decide(risk=0.95, hardship_flag=True, hardship_source="agent_coded", eligible_programmes=prog)
    assert ch.action == "offer_hardship_plan" and not (gates.allowed_actions(res) & gates.CONTACT_ACTIONS)
    c, res, ch = decide(risk=0.95, hardship_text=True, hardship_confirmed=True, eligible_programmes=prog)
    assert ch.action == "offer_hardship_plan"


def test_recorded_hardship_with_a_plan_in_force_means_no_new_request():
    c, res, ch = decide(risk=0.95, hardship_flag=True, hardship_source="agent_coded", hardship_plan_active=True,
                        eligible_programmes=[{"program_id": "X", "approver": "agent"}])
    assert ch.action == "no_action" and "specialist queue owns contact" in ch.rationale


def test_recorded_hardship_without_an_eligible_programme_goes_to_a_specialist():
    assert decide(risk=0.95, hardship_flag=True, hardship_source="agent_coded")[2].action == "specialist_review"


def test_vulnerability_is_a_safeguard_not_a_risk_input():
    assert decide(risk=0.6, vulnerable=False)[0].risk == decide(risk=0.6, vulnerable=True)[0].risk
    assert decide(risk=0.95, vulnerable=True)[2].action == "specialist_review"


def test_consent_and_budget_gates():
    assert decide(risk=0.9, consent_call=False)[2].action == "sms_payment_link"
    assert decide(risk=0.9, consent_call=False, consent_sms=False, consent_email=False)[2].action == "no_action"
    c, res, ch = decide(risk=0.9, contacts_7d=3, cap_7d=3)
    assert ch.action == "no_action" and any(r.gate == "contact_budget" and r.status == "restrict" for r in res)


def test_open_dispute_pauses_payment_requests():
    c, res, ch = decide(risk=0.9, dispute_open=True)
    assert ch.action != "call_evening" and not (gates.allowed_actions(res) & gates.PAYMENT_REQUEST_ACTIONS)


def test_payment_received_means_no_action():
    assert decide(risk=0.9, paid_since_creation=True)[2].action == "no_action"


def test_rationale_does_not_claim_effectiveness():
    for risk in (0.55, 0.7, 0.9):
        assert "effective" not in decide(risk=risk)[2].rationale


# ---- fix: realistic review triggers
def test_routine_routing_does_not_demand_review():
    prog = [{"program_id": "HP-LOAN-RED3", "approver": "agent"}]
    c, res, ch = decide(risk=0.7, hardship_flag=True, hardship_source="agent_coded", eligible_programmes=prog)
    assert fired(c, res, ch, "review") == set()                              # a recorded hardship being offered an option is routine
    c, res, ch = decide(risk=0.7, vulnerable=True)
    assert fired(c, res, ch, "review") == set() and fired(c, res, ch, "routing") == {"vulnerable_customer"}
    assert not actions.review_is_required(actions.review_triggers(c, res, ch, THRESH))
    c, res, ch = decide(risk=0.9, insolvency_hold=True)
    assert fired(c, res, ch, "review") == set()                              # a hard stop already means no contact


def test_triggers_that_should_demand_review():
    c, res, ch = decide(risk=0.7, hardship_text=True)
    assert "possible_hardship" in fired(c, res, ch)
    c, res, ch = decide(risk=0.6, duplicate_record=True)
    assert "uncertain_identity" in fired(c, res, ch)
    c, res, ch = decide(risk=0.6, customer_found=False)
    assert "uncertain_identity" in fired(c, res, ch)
    c, res, ch = decide(risk=0.6, identity_trust=0.55, identity_low_trust=True, identity_conflicts=["shared_mobile"])
    assert "uncertain_identity" in fired(c, res, ch)
    gate = next(r for r in res if r.gate == "identity_and_data_quality")
    assert gate.status == "review" and "0.55" in gate.detail
    c, res, ch = decide(risk=0.6, identity_trust=0.97, identity_low_trust=False)
    assert "uncertain_identity" not in fired(c, res, ch)
    c, res, ch = decide(risk=0.6, missing_feature_share=0.35)
    assert "low_model_confidence" in fired(c, res, ch) and "insufficient_data_quality" not in fired(c, res, ch)
    c, res, ch = decide(risk=0.6, missing_feature_share=0.55)
    assert "insufficient_data_quality" in fired(c, res, ch)
    c, res, ch = decide(risk=0.95)
    assert "low_model_confidence" in fired(c, res, ch)                       # calibration tail
    c, res, ch = decide(risk=0.8, consent_call=False, consent_sms=False, consent_email=False)
    assert "policy_conflict" in fired(c, res, ch)                            # high risk, every contact blocked by consent
    c, res, ch = decide(risk=0.8, hardship_flag=True, hardship_source="agent_coded",
                        eligible_programmes=[{"program_id": "P", "approver": "hardship_specialist"}])
    assert "high_impact_action" in fired(c, res, ch)                         # lead option needs approval above agent level


def test_closeness_to_a_band_edge_alone_does_not_demand_review():
    for risk in (0.51, 0.499, 0.651, 0.749):
        c, res, ch = decide(risk=risk)
        assert "low_model_confidence" not in fired(c, res, ch), risk


# ---- fix: Sunday window
@pytest.mark.parametrize("action", ["sms_payment_link", "email_reminder", "call_daytime", "call_evening"])
def test_sunday_window_is_non_empty_and_inside_the_permitted_hours(action):
    sun = date(2026, 10, 4)
    s = gates.contact_slot(ctx(as_of=sun, due_date=sun, sunday_rule="13:00-17:00"), action)
    assert s["date"] == "2026-10-04"
    assert "13:00" <= s["from_local"] < s["to_local"] <= "17:00", s


def test_sunday_evening_call_is_moved_into_the_sunday_window():
    sun = date(2026, 10, 4)
    s = gates.contact_slot(ctx(as_of=sun, due_date=sun, sunday_rule="13:00-17:00"), "call_evening")
    assert (s["from_local"], s["to_local"]) == ("15:00", "17:00") and any("moved" in n for n in s["notes"])


def test_non_sunday_windows_are_unchanged():
    mon = date(2026, 10, 5)
    for a, w in gates.DESIRED_WINDOWS.items():
        s = gates.contact_slot(ctx(as_of=mon, due_date=mon), a)
        assert (int(s["from_local"][:2]), int(s["to_local"][:2])) == w and not s["notes"]


def test_fit_window_always_returns_a_non_empty_window_inside_the_allowed_one():
    for s in range(7, 21):
        for e in range(s + 1, 22):
            for a, b in [(7, 21), (13, 17), (9, 12), (15, 16)]:
                x, y = gates.fit_window((s, e), (a, b))
                assert a <= x < y <= b, ((s, e), (a, b), (x, y))


def test_sunday_rule_none_and_holidays_move_the_day():
    sun = date(2026, 10, 4)
    assert gates.contact_slot(ctx(as_of=sun, due_date=sun, sunday_rule="none"), "call_evening")["date"] == "2026-10-05"
    assert gates.contact_slot(ctx(as_of=date(2026, 9, 30), due_date=date(2026, 9, 30)), "sms_payment_link")["date"] == "2026-10-01"
    assert gates.contact_slot(ctx(as_of=date(2026, 9, 29), due_date=date(2026, 9, 30)), "sms_payment_link")["date"] == "2026-09-29"
    assert gates.contact_slot(ctx(), "no_action") is None


# =================================================================== orchestrator on real promises
@pytest.fixture(scope="module")
def dec(tmp_path_factory):
    return Decisioner(store=RecordStore(tmp_path_factory.mktemp("rec") / "decisions.duckdb"))


def find(dec, where, n=1, required=True):
    rows = dec.con.execute(
        f"select p.ptp_id from cur_promises p join cur_cases k on k.case_id = p.case_id "
        f"left join raw_customers u on u.crm_customer_id = p.crm_customer_id where {where} order by hash(p.ptp_id) limit {n}").fetchall()
    assert rows or not required, where
    return [r[0] for r in rows]


PLAIN = ("p.ptp_status = 'open' and k.case_status = 'open' and not k.cease_contact_flag and not k.hardship_flag "
         "and not k.vulnerable_customer_flag and u.consent_sms and u.consent_call and u.consent_email "
         "and not k.third_party_rep_flag and not k.insolvency_hold_flag and not k.dispute_flag and not k.agency_placement_flag "
         "and not k.legal_referral_flag and k.legal_status is null and not k.chargeoff_flag "
         "and not u.credit_counselling_agency_flag and not u.legal_representative_flag "
         "and not u.power_of_attorney_flag and not u.deceased_flag and not u.bankruptcy_flag "
         "and not u.consumer_proposal_flag and not k.deceased_hold_flag and not u.cease_communication_flag "
         "and u.duplicate_of_crm_id is null and not u.is_deleted")


def test_recommend_returns_a_complete_hardened_decision_record(dec):
    rec = dec.recommend(find(dec, "p.ptp_status = 'open'")[0])
    for key in ("decision_id", "created_at", "risk", "band", "gates", "recommended_action", "explanation", "review_triggers", "model",
                "inputs", "feature_vector", "context_snapshot", "policy_config", "identity", "routing_notes"):
        assert key in rec, key
    assert "human_decision" not in rec and "final_action" not in rec            # records never change; reviews live in their own table
    assert rec["model"]["uses_protected_attributes"] is False and len(rec["model"]["file_sha256"]) == 64
    assert rec["policy_config"]["risk_band_thresholds"] == list(config.BANDS) and "assumption" in rec["policy_config"]["risk_band_thresholds_status"]
    assert "2026-09-30" in rec["policy_config"]["holiday_calendar"] and rec["policy_config"]["review_trigger_version"] == actions.TRIGGER_VERSION
    assert set(rec["feature_vector"]["model"]) == set(F.MODEL_FEATURE_NAMES)    # replayable: every model input is stored
    assert "txn_benefit_3m" not in rec["feature_vector"]["model"] and "txn_benefit_3m" in rec["feature_vector"]["gate_only_and_excluded"]
    assert rec["context_snapshot"]["case_status"] and "consent_sms" in rec["context_snapshot"]
    ident = rec["identity"]                                                     # Golden C360 identity is part of the record
    assert ident["customer_key"] and 0 < ident["match_confidence"] <= 1 and 0 <= ident["trust_score"] <= 1
    assert ident["trust_review_threshold"] == config.IDENTITY_TRUST_REVIEW and rec["context_snapshot"]["identity_trust"] == ident["trust_score"]
    assert len(rec["explanation"]["top_reasons"]) == 3 and rec["explanation"]["what_would_change_this"]
    assert "callback" not in " ".join(rec["explanation"]["what_would_change_this"]).lower()
    assert rec["recommended_action"] in rec["allowed_actions"]
    assert dec.store.get(rec["decision_id"])["decision_id"] == rec["decision_id"]
    assert dec.store.verify()["ok"]


def test_policy_citations_only_cite_documents_that_apply(dec):
    saw_contact = False
    for pid in find(dec, PLAIN, 12):
        rec = dec.recommend(pid, save=False)
        cites = rec["explanation"]["policy_citations"]
        assert all("README" not in c for c in cites)
        assert "POL-COLL-001 §7.3" not in " ".join(cites)                   # no insolvency citation when no insolvency applies
        if rec["recommended_action"] in gates.CONTACT_ACTIONS:
            saw_contact = True
            assert "POL-COLL-001 §5.2" in cites and "POL-COLL-001 §5.3" in cites
    assert saw_contact


def test_hard_stop_cases_in_real_data_never_get_a_contact_action(dec):
    for where in ("k.cease_contact_flag", "k.insolvency_hold_flag", "k.deceased_hold_flag", "u.deceased_flag",
                  "k.third_party_rep_flag", "u.bankruptcy_flag"):
        for pid in find(dec, where, 5, required=False):
            rec = dec.recommend(pid, save=False)
            assert rec["recommended_action"] in ("no_action", "specialist_review"), (where, pid, rec["recommended_action"])
            assert not set(rec["allowed_actions"]) & gates.CONTACT_ACTIONS


def test_agency_legal_and_charged_off_cases_never_get_a_contact_action(dec):
    for where in ("k.case_status = 'placed_agency'", "k.case_status = 'legal'", "k.agency_placement_flag",
                  "k.legal_referral_flag", "k.legal_status is not null", "k.chargeoff_flag", "k.case_status = 'settled'",
                  "k.case_status = 'charged_off'"):
        pids = find(dec, where, 25, required=False)
        for pid in pids:
            rec = dec.recommend(pid, save=False)
            assert not set(rec["allowed_actions"]) & gates.CONTACT_ACTIONS, (where, pid)
            assert rec["recommended_action"] in ("no_action", "specialist_review"), (where, pid, rec["recommended_action"])
            assert next(g for g in rec["gates"] if g["gate"] == "case_stage")["status"] == "stop"
    assert find(dec, "k.case_status = 'placed_agency' and p.ptp_status = 'open'", 1)        # the population really exists


def test_vulnerable_cases_are_routed_to_a_human_without_inflating_review(dec):
    for pid in find(dec, "k.vulnerable_customer_flag and not k.cease_contact_flag", 10):
        rec = dec.recommend(pid, save=False)
        assert rec["recommended_action"] in ("specialist_review", "no_action")
        assert "vulnerable_customer" in rec["routing_notes"] or rec["recommended_action"] == "no_action"


def test_recorded_hardship_is_offered_an_option_and_never_a_reminder(dec):
    pids = find(dec, "p.ptp_status = 'open' and k.case_status = 'open' and k.hardship_flag and k.hardship_source in ('agent_coded','customer_form') "
                     "and not k.hardship_program_accepted and not k.cease_contact_flag and not k.vulnerable_customer_flag "
                     "and not k.third_party_rep_flag and not k.insolvency_hold_flag and not k.dispute_flag", 25)
    acts = set()
    for pid in pids:
        rec = dec.recommend(pid, save=False)
        acts.add(rec["recommended_action"])
        assert rec["hardship_status"] == "recorded"
        assert rec["recommended_action"] in ("offer_hardship_plan", "specialist_review", "no_action")
        assert not set(rec["allowed_actions"]) & gates.CONTACT_ACTIONS
    assert "offer_hardship_plan" in acts


def test_review_rate_is_realistic_on_open_promises(dec):
    ids = [r[0] for r in dec.con.execute("select ptp_id from cur_promises where ptp_status = 'open' order by hash(ptp_id) limit 300").fetchall()]
    recs = [dec.recommend(i, save=False) for i in ids]
    rate = sum(r["review_required"] for r in recs) / len(recs)
    assert rate < 0.25, f"review required on {rate:.0%} of open promises (v1 was 87%)"
    assert sum(1 for r in recs if r["review_required"]) > 0          # but the triggers do still fire


# ---- fix: upcoming() ranks the whole horizon
def test_upcoming_scores_the_whole_horizon_and_ranks_by_risk(dec, con):
    q_all = dec.upcoming(days=3, limit=None)
    n = con.execute("select count(*) from cur_promises where ptp_status = 'open' and ptp_due_date between date '2026-09-28' and date '2026-10-01'").fetchone()[0]
    assert len(q_all) == n > 2000                                           # the old code looked at only the first 2,000
    assert "2026-09-30" in {x["due"] for x in q_all}                         # the old code never reached later due dates
    assert [x["risk"] for x in q_all] == sorted((x["risk"] for x in q_all), reverse=True)
    top = dec.upcoming(days=3, limit=5)
    assert top == q_all[:5]
    feats = F.compute_features(con, "p.ptp_status = 'open' and p.ptp_due_date between date '2026-09-28' and date '2026-10-01'")
    expected_max = float(dec.model.score(feats).max())
    assert abs(q_all[0]["risk"] - round(expected_max, 4)) < 1e-9             # the riskiest promise in the horizon is first
    assert dec.upcoming(days=3, limit=5) == top                              # stable order


def test_customer_level_sunday_setting_can_only_tighten_the_rule(dec):
    pid = find(dec, PLAIN)[0]
    row = F.compute_features(dec.con, f"p.ptp_id = '{pid}'").to_dicts()[0]
    raw = dec._context_rows(pid, date(2026, 9, 28))
    raw["contact_rule_set"] = "ON-STD-2026"
    raw["sunday_contact_permitted"] = False
    assert dec._build_context(row, raw, date(2026, 9, 28), 0.7, False).sunday_rule == "none"
    raw["sunday_contact_permitted"] = True
    assert dec._build_context(row, raw, date(2026, 9, 28), 0.7, False).sunday_rule == "13:00-17:00"
    raw["contact_rule_set"] = "QC-STD-2026"
    assert dec._build_context(row, raw, date(2026, 9, 28), 0.7, False).sunday_rule == "none"


# =================================================================== human review / overrides
def test_human_accept_and_logged_override(dec):
    rec = dec.recommend(find(dec, PLAIN)[0])
    r = dec.review(rec["decision_id"], "REV-1", "agent", "accept", reason="agrees")
    assert r["final_action"] == rec["recommended_action"] and not r["overridden"] and r["identity_verified"] is False
    r = dec.review(rec["decision_id"], "REV-1", "reviewer", "change", reason="customer prefers email", final_action="email_reminder")
    assert r["final_action"] == "email_reminder"
    assert len(dec.store.reviews(rec["decision_id"])) == 2
    state = dec.store.current_state(rec["decision_id"])
    assert state["status"] == "change" and state["final_action"] == "email_reminder" and state["n_reviews"] == 2


def test_current_state_is_pending_until_reviewed(dec):
    rec = dec.recommend(find(dec, "p.ptp_status = 'open'")[0])
    assert dec.store.current_state(rec["decision_id"])["status"] == "pending"


def test_override_cannot_breach_a_hard_gate_and_the_refusal_is_logged(dec):
    rec = dec.recommend(find(dec, "k.cease_contact_flag", 1)[0])
    with pytest.raises(PermissionError, match="override refused"):
        dec.review(rec["decision_id"], "REV-2", "team_lead", "change", reason="try anyway", final_action="call_evening")
    attempts = dec.store.attempts(rec["decision_id"])
    assert len(attempts) == 1 and attempts[0]["outcome"] == "refused" and "override refused" in attempts[0]["detail"]
    assert attempts[0]["reviewer_id"] == "REV-2" and attempts[0]["requested_final_action"] == "call_evening"
    assert dec.store.reviews(rec["decision_id"]) == []                       # nothing was recorded as a review


def test_agency_case_override_to_contact_is_refused(dec):
    rec = dec.recommend(find(dec, "k.case_status = 'placed_agency' and p.ptp_status = 'open'")[0])
    with pytest.raises(PermissionError, match="case_stage"):
        dec.review(rec["decision_id"], "REV-2", "team_lead", "change", reason="x", final_action="sms_payment_link")


def test_successful_reviews_are_also_in_the_attempt_log(dec):
    rec = dec.recommend(find(dec, PLAIN)[0])
    dec.review(rec["decision_id"], "REV-5", "reviewer", "escalate", reason="unsure")
    outcomes = [a["outcome"] for a in dec.store.attempts(rec["decision_id"])]
    assert outcomes == ["recorded"]


def test_review_requires_reason_and_valid_role_and_both_failures_are_logged(dec):
    rec = dec.recommend(find(dec, "p.ptp_status = 'open'")[0])
    with pytest.raises(ValueError):
        dec.review(rec["decision_id"], "REV-3", "agent", "accept", reason=" ")
    with pytest.raises(ValueError):
        dec.review(rec["decision_id"], "REV-3", "janitor", "accept", reason="x")
    assert [a["outcome"] for a in dec.store.attempts(rec["decision_id"])] == ["refused", "refused"]


def test_gates_are_re_evaluated_at_review_time(dec, monkeypatch):
    rec = dec.recommend(find(dec, PLAIN)[0])
    assert rec["recommended_action"] in gates.CONTACT_ACTIONS or rec["recommended_action"] == "no_action"
    original = dec._context_rows

    def with_cease_filed_since(ptp_id, as_of):
        r = original(ptp_id, as_of)
        r["cease_contact_flag"] = True               # a cease request arrived after the recommendation was made
        return r

    monkeypatch.setattr(dec, "_context_rows", with_cease_filed_since)
    if rec["recommended_action"] in gates.CONTACT_ACTIONS:
        with pytest.raises(PermissionError, match="no longer permitted"):
            dec.review(rec["decision_id"], "REV-4", "reviewer", "accept", reason="ok")
    stale_allowed = set(rec["allowed_actions"]) & gates.CONTACT_ACTIONS
    assert stale_allowed                                                         # the stored list WOULD have allowed contact
    contact = sorted(stale_allowed)[0]
    with pytest.raises(PermissionError, match="override refused"):
        dec.review(rec["decision_id"], "REV-4", "reviewer", "change", reason="override", final_action=contact)
    assert dec.store.reviews(rec["decision_id"]) == []


def test_an_agent_cannot_raise_contact_intensity(dec):
    for p in find(dec, PLAIN, 40):
        rec = dec.recommend(p, save=False)
        if rec["recommended_action"] == "sms_payment_link" and "call_evening" in rec["allowed_actions"]:
            saved = dec.recommend(p)
            with pytest.raises(PermissionError, match="raise contact intensity"):
                dec.review(saved["decision_id"], "AGT-1", "agent", "change", reason="push harder", final_action="call_evening")
            ok = dec.review(saved["decision_id"], "REV-9", "reviewer", "change", reason="push harder", final_action="call_evening")
            assert ok["final_action"] == "call_evening"
            return
    pytest.skip("no suitable promise in the sample")


def test_role_directory_binds_reviewer_id_to_role(tmp_path):
    d = Decisioner(store=RecordStore(tmp_path / "r.duckdb"), role_directory={"REV-77": "reviewer", "AGT-5": "agent"})
    rec = d.recommend(find(d, PLAIN)[0])
    with pytest.raises(PermissionError, match="not registered"):
        d.review(rec["decision_id"], "REV-77", "team_lead", "accept", reason="claiming a higher role")
    with pytest.raises(PermissionError, match="not registered"):
        d.review(rec["decision_id"], "NOBODY", "reviewer", "accept", reason="unknown reviewer")
    out = d.review(rec["decision_id"], "REV-77", "reviewer", "accept", reason="fine")
    assert out["identity_verified"] is True and d.store.reviews(rec["decision_id"])[0]["identity_verified"] is True


def test_hardship_confirmation_needs_a_reviewer_and_creates_a_new_softer_linked_record(dec):
    pid = find(dec, "p.ptp_status = 'open' and k.case_status = 'open' and k.hardship_flag and k.hardship_source = 'model' "
                    "and not k.cease_contact_flag and not k.vulnerable_customer_flag and not k.insolvency_hold_flag "
                    "and not k.deceased_hold_flag and not k.third_party_rep_flag and not k.agency_placement_flag "
                    "and not k.legal_referral_flag and p.product in ('card','personal_loan','auto_loan','loc') "
                    "and not k.dispute_flag and not k.hardship_program_accepted", 1)[0]
    rec = dec.recommend(pid)
    assert rec["hardship_status"] == "possible" and rec["recommended_action"] == "specialist_review" and "possible_hardship" in rec["review_reasons"]
    with pytest.raises(PermissionError):
        dec.review(rec["decision_id"], "AGT-9", "agent", "accept", reason="customer says hours cut", confirm_hardship=True)
    out = dec.review(rec["decision_id"], "REV-7", "reviewer", "accept", reason="customer confirmed hours cut", confirm_hardship=True)
    child = dec.store.get(out["child_decision_id"])
    assert child["hardship_status"] == "confirmed" and child["parent_decision_id"] == rec["decision_id"]
    assert child["hardship_confirmed_by"] == "REV-7"
    assert child["recommended_action"] == "offer_hardship_plan" and child["eligible_programmes"]
    assert out["final_action"] == "offer_hardship_plan"
    assert dec.store.get(rec["decision_id"])["hardship_status"] == "possible"      # original untouched (append-only)
    assert dec.store.verify()["ok"]


def test_failed_hardship_confirmation_leaves_no_orphan_child_record(dec):
    pid = find(dec, "p.ptp_status = 'open' and k.case_status = 'open' and k.hardship_flag and k.hardship_source = 'model' "
                    "and not k.cease_contact_flag and not k.vulnerable_customer_flag and not k.insolvency_hold_flag "
                    "and not k.deceased_hold_flag and not k.third_party_rep_flag and not k.agency_placement_flag "
                    "and not k.legal_referral_flag and not k.dispute_flag", 1)[0]
    rec = dec.recommend(pid)
    before = dec.store.con.execute("select count(*) from decision_records").fetchone()[0]
    with pytest.raises(PermissionError, match="override refused"):
        dec.review(rec["decision_id"], "REV-8", "reviewer", "change", reason="confirm and then call", final_action="call_evening",
                   confirm_hardship=True)
    assert dec.store.con.execute("select count(*) from decision_records").fetchone()[0] == before
    assert dec.store.con.execute("select count(*) from decision_records where parent_decision_id = ?", [rec["decision_id"]]).fetchone()[0] == 0
    assert dec.store.attempts(rec["decision_id"])[-1]["outcome"] == "refused"


def test_hardship_cannot_be_confirmed_without_a_reviewer_id(dec):
    with pytest.raises(ValueError):
        dec.recommend(find(dec, "p.ptp_status = 'open'")[0], hardship_confirmed=True)


def test_invalid_ptp_id_is_rejected(dec):
    with pytest.raises(ValueError):
        dec.recommend("PTP-1'; drop table x; --")


# =================================================================== record store hardening
def _record(i: int) -> dict:
    return {"decision_id": f"DR-T-{i}", "created_at": "2026-10-04T10:00:00", "ptp_id": f"PTP-{i}", "case_id": "CS-1",
            "crm_customer_id": "AAA-1", "as_of": "2026-09-28", "model": {"name": "m", "version": "v", "feature_version": "f"},
            "risk": 0.5, "band": "medium", "hardship_status": "none", "recommended_action": "no_action", "review_required": False}


def test_hash_chain_detects_edited_deleted_and_reordered_records(tmp_path):
    s = RecordStore(tmp_path / "chain.duckdb")
    for i in range(4):
        s.save(_record(i))
    assert s.verify() == {"checked": 4, "ok": True, "bad": [], "legacy_unhashed": 0}
    s.con.execute("""update decision_records set payload = replace(payload, '"risk": 0.5', '"risk": 0.1') where decision_id = 'DR-T-1'""")
    v = s.verify()
    assert not v["ok"] and "DR-T-1" in v["bad"]
    with pytest.raises(TamperError):
        s.get("DR-T-1")
    s2 = RecordStore(tmp_path / "chain2.duckdb")
    for i in range(4):
        s2.save(_record(i))
    s2.con.execute("delete from decision_records where decision_id = 'DR-T-1'")
    assert not s2.verify()["ok"]


def test_transaction_rolls_back_every_write_on_failure(tmp_path):
    s = RecordStore(tmp_path / "tx.duckdb")
    with pytest.raises(RuntimeError):
        with s.transaction():
            s.save(_record(1))
            raise RuntimeError("boom")
    assert s.con.execute("select count(*) from decision_records").fetchone()[0] == 0
    s.save(_record(2))
    assert s.verify()["ok"]


def test_existing_database_is_migrated_in_place_and_legacy_rows_are_reported(tmp_path):
    path = tmp_path / "legacy.duckdb"
    c = duckdb.connect(str(path))
    c.execute("""create table decision_records (decision_id varchar primary key, created_at timestamp, ptp_id varchar, case_id varchar,
        crm_customer_id varchar, parent_decision_id varchar, model_name varchar, model_version varchar, feature_version varchar,
        as_of date, risk double, band varchar, hardship_status varchar, recommended_action varchar, review_required boolean, payload varchar)""")
    c.execute("""create table decision_reviews (review_id varchar primary key, decision_id varchar, reviewed_at timestamp, reviewer_id varchar,
        role varchar, decision varchar, final_action varchar, reason varchar, hardship_confirmed boolean, child_decision_id varchar)""")
    c.execute("insert into decision_records values ('DR-OLD', now(), 'PTP-1', 'CS-1', 'A', null, 'm', 'v', 'f', date '2026-09-28', 0.5, 'low', 'none', 'no_action', false, '{}')")
    c.close()
    s = RecordStore(path)
    cols = {r[0] for r in s.con.execute("select column_name from information_schema.columns where table_name = 'decision_records'").fetchall()}
    assert {"seq", "payload_hash", "chain_hash"} <= cols
    s.save(_record(1))
    assert s.verify() == {"checked": 1, "ok": True, "bad": [], "legacy_unhashed": 1}
    assert s.con.execute("select payload from decision_records where decision_id = 'DR-OLD'").fetchone()[0] == "{}"      # legacy row untouched


def test_store_has_no_update_or_delete_methods():
    names = {n for n in dir(RecordStore) if not n.startswith("_")}
    assert not {n for n in names if any(w in n.lower() for w in ("update", "delete", "remove", "drop"))}


# =================================================================== evaluation
def test_scores_are_calibrated_on_held_out_window(con):
    m = PtpModel("v1")
    t = con.execute("select risk, y from ptp_test_scores").pl()
    for lo, hi in ((0.0, 0.5), (0.5, 0.65), (0.65, 0.75), (0.75, 1.01)):
        sub = t.filter((pl.col("risk") >= lo) & (pl.col("risk") < hi))
        if len(sub) > 500:
            assert abs(sub["risk"].mean() - sub["y"].mean()) < 0.08, (lo, hi)
    assert m.meta["metrics"]["test_auc"] > 0.6


def test_saved_evaluation_scores_match_a_fresh_recomputation(con):
    """The model that production loads is the model that was evaluated (the earlier 2-row mismatch came from the non-deterministic join)."""
    from decisioning import train as T
    m = PtpModel("v1")
    df = T.load_dataset(con)
    te = df.filter(pl.col("ptp_due_date") > config.VALID_END)
    saved = con.execute("select ptp_id, risk from ptp_test_scores").pl()
    j = te.select("ptp_id").with_columns(pl.Series("fresh", m.score(te))).join(saved, on="ptp_id")
    assert len(j) == len(te) and float((j["fresh"] - j["risk"]).abs().max()) < 1e-6
