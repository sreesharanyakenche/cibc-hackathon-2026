"""Layer 1 (identity / golden C360) invariants and the demo API.

Needs warehouse/maple.duckdb and warehouse/c360.duckdb (python -m data_pipeline.load_raw / identity / c360).
"""
import duckdb
import pytest
from fastapi.testclient import TestClient

from data_pipeline.config import MAIN_DB, WAREHOUSE

C360 = WAREHOUSE / "c360.duckdb"
pytestmark = pytest.mark.skipif(not C360.exists(), reason="golden C360 not built")


@pytest.fixture(scope="module")
def con():
    c = duckdb.connect(":memory:")
    c.execute(f"ATTACH '{C360.as_posix()}' AS c (READ_ONLY)")
    c.execute(f"ATTACH '{MAIN_DB.as_posix()}' AS m (READ_ONLY)")
    return c


# ------------------------------------------------------------------ identity / C360
def test_every_crm_record_has_exactly_one_customer_key(con):
    n_crm = con.execute("select count(*) from m.raw_customers").fetchone()[0]
    n, d = con.execute("select count(*), count(distinct crm_customer_id) from c.id_crm_clusters").fetchone()
    assert n == d == n_crm


def test_c360_one_row_per_customer_key_and_trust_in_range(con):
    n, d, nn = con.execute("select count(*), count(distinct customer_key), count(customer_key) from c.c360").fetchone()
    assert n == d == nn
    assert con.execute("select count(*) from c.c360 where trust_score is null or trust_score < 0 or trust_score > 1").fetchone()[0] == 0
    assert n == con.execute("select count(distinct customer_key) from c.id_crm_clusters").fetchone()[0]


def test_c360_has_no_protected_attribute_columns(con):
    cols = {r[0] for r in con.execute("describe c.c360").fetchall()}
    banned = {"gender_code", "marital_status", "citizenship_status", "household_size", "newcomer_program_flag",
              "accessibility_needs_flag", "vulnerability_type", "vulnerability_flag", "accent"}
    assert not cols & banned


def test_one_link_row_per_source_account_and_only_strong_tiers_auto_link(con):
    total = sum(con.execute(f"select count(*) from m.{t}").fetchone()[0] for t in ("raw_card_accounts", "raw_deposit_accounts"))
    total += con.execute("select count(distinct loan_id) from m.raw_loan_accounts").fetchone()[0]
    assert con.execute("select count(*) from c.id_identity_links").fetchone()[0] == total
    assert con.execute("select count(*) from c.id_identity_links where link_status in ('linked','linked_with_alternatives') "
                       "and tier not in ('A','B','C')").fetchone()[0] == 0
    # weak tiers are withheld, not linked
    assert con.execute("select count(*) from c.id_identity_links where tier in ('D','E') and link_status not in ('review','ambiguous')").fetchone()[0] == 0


def test_declared_duplicates_are_mostly_merged_and_conflicts_never_merged(con):
    merged, total = con.execute("""
        select count(*) filter (where a.customer_key = b.customer_key), count(*)
        from m.raw_customers d join c.id_crm_clusters a on a.crm_customer_id = d.crm_customer_id
        join c.id_crm_clusters b on b.crm_customer_id = d.duplicate_of_crm_id where d.duplicate_of_crm_id is not null""").fetchone()
    assert total > 5000 and merged / total > 0.95
    assert con.execute("""select count(*) from c.id_crm_pairs p join c.id_crm_clusters a on a.crm_customer_id = p.id_a
        join c.id_crm_clusters b on b.crm_customer_id = p.id_b
        where p.decision = 'conflict' and a.customer_key = b.customer_key and p.blocks = 'national_id_hash'""").fetchone()[0] == 0


def test_identity_precision_on_collections_recorded_links(con):
    """Ground truth = the collections system's own account -> CRM pairs (independent of name/DOB/phone matching)."""
    p, ok = con.execute("""
        with truth as (select account_id, min(crm_customer_id) t from (
              select h.account_id, h.crm_customer_id from m.raw_contact_history h join c.id_crm_clusters k using (crm_customer_id) where h.account_id is not null
              union all select p.account_id, p.crm_customer_id from m.raw_promises_to_pay p join c.id_crm_clusters k using (crm_customer_id) where p.account_id is not null)
              group by 1 having count(distinct crm_customer_id) = 1)
        select count(*), count(*) filter (where l.customer_key = tc.customer_key)
        from c.id_identity_links l join truth t using (account_id) join c.id_crm_clusters tc on tc.crm_customer_id = t.t
        where l.link_status in ('linked','linked_with_alternatives')""").fetchone()
    assert p > 250_000 and ok / p >= 0.99


def test_data_contract_matches_the_table():
    import yaml
    from data_pipeline.config import ROOT
    contract = yaml.safe_load((ROOT / "contracts" / "data_contract.yaml").read_text(encoding="utf-8"))
    cols = [r[0] for r in duckdb.connect(str(C360), read_only=True).execute("describe c360").fetchall()]
    assert [f["name"] for f in contract["fields"]] == cols
    assert contract["grain"] == "one row per customer_key"


# ------------------------------------------------------------------ decisioning uses the C360
def test_low_trust_identity_routes_to_review():
    from decisioning import actions, config, gates
    from decisioning.gates import Context
    from decisioning.orchestrator import Decisioner
    d = Decisioner.__new__(Decisioner)
    d._c360 = duckdb.connect(str(C360), read_only=True)
    import threading
    d._c360_lock = threading.Lock()
    low = d._c360.execute("select k.crm_customer_id from c360 c join id_crm_clusters k using (customer_key) where c.identity_review_required limit 1").fetchone()[0]
    ident = d.identity_for(low)
    assert ident["trust_score"] < 0.70 and ident["customer_key"]
    ctx = Context(ptp_id="PTP-1", as_of=__import__("datetime").date(2026, 9, 28), due_date=__import__("datetime").date(2026, 9, 30), risk=0.6, band="medium",
                  identity_trust=ident["trust_score"], identity_low_trust=True, identity_conflicts=ident["conflict_flags"])
    res = gates.run_gates(ctx)
    assert next(r for r in res if r.gate == "identity_and_data_quality").status == "review"
    assert any(t["trigger"] == "uncertain_identity" and t["fired"] for t in actions.review_triggers(ctx, res, actions.choose(ctx, res), config.BANDS))


# ------------------------------------------------------------------ API
@pytest.fixture(scope="module")
def client(tmp_path_factory):
    from app import main
    from decisioning.orchestrator import Decisioner
    from decisioning.records import RecordStore
    main._state["dec"] = Decisioner(store=RecordStore(tmp_path_factory.mktemp("api") / "decisions.duckdb"), role_directory=main.DEMO_ROLES)
    return TestClient(main.app, raise_server_exceptions=False)


def test_api_health_and_request_id(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["status"] == "ok" and r.headers["x-request-id"]


def test_api_search_and_customer_360(client):
    hits = client.get("/api/customers", params={"q": "Desjardins"}).json()["results"]
    assert hits and all("trust_score" in h for h in hits)
    d = client.get(f"/api/customers/{hits[0]['customer_key']}").json()
    assert d["c360"]["customer_key"] == hits[0]["customer_key"] and "linked_accounts" in d and "promises" in d
    assert client.get("/api/customers/NOPE-0000").status_code == 404
    assert client.get("/api/customers", params={"q": "x"}).status_code == 422


def test_api_recommend_review_and_audit_flow(client):
    up = client.get("/api/promises/upcoming", params={"limit": 3}).json()["promises"]
    assert up and up == sorted(up, key=lambda p: (-p["risk"], p["due"], p["ptp_id"]))
    rec = client.post("/api/recommend", json={"ptp_id": up[0]["ptp_id"]})
    assert rec.status_code == 201
    did = rec.json()["record"]["decision_id"]
    assert rec.json()["record"]["identity"]["customer_key"]
    assert client.post("/api/recommend", json={"ptp_id": "PTP-99999999999"}).status_code == 404
    assert client.post("/api/recommend", json={"ptp_id": "x; drop table"}).status_code == 422
    assert client.post("/api/recommend", json={"ptp_id": up[0]["ptp_id"], "risk": 0.1}).status_code == 422      # unknown fields are rejected
    # a reviewer id that does not hold the claimed role is refused (and logged)
    bad = client.post(f"/api/decisions/{did}/review", json={"reviewer_id": "AGT-01", "role": "team_lead", "decision": "accept", "reason": "trying it"})
    assert bad.status_code == 403
    ok = client.post(f"/api/decisions/{did}/review", json={"reviewer_id": "REV-01", "role": "reviewer", "decision": "accept", "reason": "agreed with the model"})
    assert ok.status_code == 200 and ok.json()["review"]["identity_verified"] is True
    d = client.get(f"/api/decisions/{did}").json()
    assert d["state"]["status"] == "accept" and len(d["reviews"]) == 1
    assert {a["outcome"] for a in d["attempts"]} == {"refused", "recorded"}
    v = client.get("/api/audit/verify").json()
    assert v["ok"] and v["checked"] >= 1


def test_api_ask_answers_with_sql_and_refuses_out_of_scope(client):
    a = client.post("/api/ask", json={"question": "How many open collections cases are there by bucket?"}).json()
    assert not a["refused"] and a["sql_or_sources"].lower().startswith("select")
    for q in ("What is the weather in Toronto?", "Which customers are Indigenous?"):
        r = client.post("/api/ask", json={"question": q}).json()
        assert r["refused"] and not r["sql_or_sources"]
