#!/usr/bin/env python3
"""
CURATED: Golden Customer 360  ->  warehouse/c360.duckdb (c360, c360_attributes)

Adapted from the reference implementation in CIBC_Hackathon_CodeBlooded-main (scripts/build_c360.py); logic unchanged,
reads our raw_*/cur_* tables and normalises province codes inline.

One row per `customer_key`.  Every column is derived from a column that exists in the profiled dataset; nothing is invented.
Field-level source and lineage are documented in contracts/data_contract.yaml.

Design rules
 * Identity comes from data_pipeline/identity.py.  Only links with status `linked` / `linked_with_alternatives`
   (tier A-C, unique best customer) contribute accounts.  `review` and `ambiguous` accounts are counted
   (`n_accounts_pending_review`) but their balances are NOT attributed to anyone.
 * When a customer_key merges two CRM records, restrictive flags are OR-ed (cease contact, DNC, bankruptcy, deceased...),
   consents are AND-ed.  Conservative by construction; no value is overwritten without being listed in `member_crm_ids`.
 * Protected attributes (gender, marital status, citizenship, household, newcomer, accessibility, vulnerability*, accent) and
   direct PII (full phone, email, street address, national id hash) are NOT carried into the C360.
   `vulnerability_flag` is excluded pending a policy decision: the existing v_customers view and L1 agent use it while
   the dataset README and data_contract.yaml (protected_fields_excluded) say it must not feed decisions.
 * Trust score reuses the existing L1 formula:  match_confidence - min(0.05 * open_conflicts, 0.25) - 0.30 * shared_mobile,
   clamped to [0,1].  It is an identity-data trust measure, not a credit score.
 * `as_of` date = dataset snapshot date 2026-09-28 (dataset README).
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from data_pipeline.config import MAIN_DB, ROOT, WAREHOUSE

C360_DB = WAREHOUSE / "c360.duckdb"
REPORTS = ROOT / "outputs"
AS_OF = "2026-09-28"
TRUST_REVIEW_THRESHOLD = 0.70  # Decisioner identity gate / review threshold

PROVINCE_FIX = {"Ont.": "ON", "B.C.": "BC", "Alta.": "AB", "Quebec": "QC", "PQ": "QC", "Man.": "MB", "N.S.": "NS"}


def P(t: str) -> str:
    return "cust_norm" if t == "customers" else ("src.cur_contacts" if t == "contact_history" else f"src.raw_{t}")


def I(t: str) -> str:
    return f"out.id_{t}"


def main() -> int:
    t0 = time.time()
    con = duckdb.connect()
    con.execute("SET memory_limit='9GB'")
    con.execute("SET threads=12")
    con.execute(f"SET temp_directory='{(WAREHOUSE / 'tmp_c360').as_posix()}'")
    con.execute(f"ATTACH '{MAIN_DB.as_posix()}' AS src (READ_ONLY)")
    con.execute(f"ATTACH '{C360_DB.as_posix()}' AS out")
    pf = ", ".join(f"('{k}', '{v}')" for k, v in PROVINCE_FIX.items())
    con.execute(f"""CREATE TABLE cust_norm AS SELECT c.*, coalesce(m.v, c.province_code) AS province_code_norm
                    FROM src.raw_customers c LEFT JOIN (VALUES {pf}) m(k, v) ON m.k = c.province_code""")
    con.execute("SET preserve_insertion_order=false")

    con.execute(f"CREATE VIEW clusters AS SELECT * FROM {I('crm_clusters')}")
    con.execute(f"CREATE VIEW links AS SELECT * FROM {I('identity_links')}")

    # ---------------------------------------------------------------- identity block
    con.execute(f"""
    CREATE TABLE id_block AS
    WITH m AS (
      SELECT cc.customer_key, c.*
      FROM clusters cc JOIN {P('customers')} c ON c.crm_customer_id = cc.crm_customer_id),
    agg AS (
      SELECT customer_key,
             list(crm_customer_id ORDER BY crm_customer_id) AS member_crm_ids, count(*) AS n_crm_records,
             bool_or(cease_communication_flag) AS cease_communication_flag,
             bool_or(internal_do_not_call_flag) AS internal_do_not_call_flag,
             bool_or(national_dncl_flag) AS national_dncl_flag,
             bool_or(bankruptcy_flag) AS bankruptcy_flag, bool_or(consumer_proposal_flag) AS consumer_proposal_flag,
             bool_or(deceased_flag) AS deceased_flag, bool_or(credit_counselling_agency_flag) AS credit_counselling_agency_flag,
             bool_or(third_party_authorised_flag) AS third_party_authorised_flag,
             bool_and(coalesce(consent_sms, false)) AS consent_sms, bool_and(coalesce(consent_call, false)) AS consent_call,
             bool_and(coalesce(consent_email, false)) AS consent_email, bool_and(coalesce(consent_autodialer, false)) AS consent_autodialer,
             min(insolvency_filed_date) AS insolvency_filed_date
      FROM m GROUP BY 1),
    anc AS (
      SELECT m.customer_key, m.crm_customer_id AS anchor_crm_customer_id, m.first_name, m.last_name, m.date_of_birth,
             m.province_code_norm AS province_code, m.city, m.fsa, m.preferred_language, m.segment, m.customer_status, m.customer_since_date,
             m.tenure_months, regexp_replace(m.primary_phone_e164, '.*(\\d{{4}})$', '\\1') AS phone_last4
      FROM m JOIN clusters cc ON cc.crm_customer_id = m.crm_customer_id AND cc.is_anchor)
    SELECT anc.*, agg.* EXCLUDE (customer_key) FROM anc JOIN agg USING (customer_key)""")
    n_keys = con.execute("SELECT count(*) FROM id_block").fetchone()[0]

    # ---------------------------------------------------------------- accounts (auto-linked only)
    con.execute(f"""
    CREATE TABLE acct AS
    WITH ok AS (SELECT source_system, account_id, customer_key FROM links WHERE link_status IN ('linked','linked_with_alternatives')),
    cards AS (
      SELECT o.customer_key, count(*) AS n_card_accounts, count(*) FILTER (WHERE a.account_status = 'active') AS n_card_active,
             sum(a.current_balance) AS card_balance_total, max(a.dpd) AS card_max_dpd,
             bool_or(a.chargeoff_flag) AS card_any_chargeoff
      FROM ok o JOIN {P('card_accounts')} a ON o.source_system = 'CARDS' AND a.card_account_id = o.account_id GROUP BY 1),
    loans AS (
      SELECT o.customer_key, count(*) AS n_loan_accounts, sum(a.principal_balance) AS loan_principal_total,
             max(a.dpd) AS loan_max_dpd, bool_or(a.chargeoff_flag) AS loan_any_chargeoff
      FROM ok o JOIN {P('loan_accounts')} a ON o.source_system = 'LENDING' AND a.loan_id = o.account_id GROUP BY 1),
    deps AS (
      SELECT o.customer_key, count(*) AS n_deposit_accounts, count(*) FILTER (WHERE a.account_status = 'active') AS n_deposit_active,
             sum(a.current_balance) AS deposit_balance_total, max(a.overdraft_dpd) AS overdraft_max_dpd
      FROM ok o JOIN {P('deposit_accounts')} a ON o.source_system = 'CORE' AND a.deposit_account_id = o.account_id GROUP BY 1),
    pend AS (
      SELECT customer_key, count(*) AS n_accounts_pending_review FROM links
      WHERE link_status IN ('review','ambiguous') AND customer_key IS NOT NULL GROUP BY 1)
    SELECT i.customer_key, c.n_card_accounts, c.n_card_active, c.card_balance_total, c.card_max_dpd, c.card_any_chargeoff,
           l.n_loan_accounts, l.loan_principal_total, l.loan_max_dpd, l.loan_any_chargeoff,
           d.n_deposit_accounts, d.n_deposit_active, d.deposit_balance_total, d.overdraft_max_dpd,
           coalesce(p.n_accounts_pending_review, 0) AS n_accounts_pending_review
    FROM id_block i LEFT JOIN cards c USING (customer_key) LEFT JOIN loans l USING (customer_key)
    LEFT JOIN deps d USING (customer_key) LEFT JOIN pend p USING (customer_key)""")

    # ---------------------------------------------------------------- collections cases (active = outcome IS NULL, per dataset README)
    con.execute(f"""
    CREATE TABLE casesagg AS
    WITH cs AS (
      SELECT cc.customer_key, k.* FROM {P('collections_cases')} k JOIN clusters cc ON cc.crm_customer_id = k.coll_customer_ref),
    act AS (SELECT * FROM cs WHERE outcome IS NULL),
    latest AS (
      SELECT customer_key, arg_max(case_id, (case_open_date, case_id)) AS case_id_latest,
             arg_max(current_treatment, (case_open_date, case_id)) AS current_treatment,
             arg_max(queue, (case_open_date, case_id)) AS queue,
             arg_max(current_ptp_id, (case_open_date, case_id)) AS current_ptp_id,
             arg_max(current_ptp_amount, (case_open_date, case_id)) AS current_ptp_amount,
             arg_max(current_ptp_due_date, (case_open_date, case_id)) AS current_ptp_due_date
      FROM act GROUP BY 1),
    a AS (
      SELECT customer_key, count(*) AS n_active_cases, max(current_dpd) AS max_current_dpd, sum(total_overdue) AS total_overdue,
             sum(total_exposure) AS total_exposure,
             arg_max(current_bucket, current_dpd) AS worst_bucket,
             bool_or(hardship_flag) AS hardship_flag, bool_or(coalesce(hardship_program_accepted, false)) AS hardship_program_accepted,
             bool_or(cease_contact_flag) AS case_cease_contact_flag, bool_or(insolvency_hold_flag) AS insolvency_hold_flag,
             bool_or(deceased_hold_flag) AS deceased_hold_flag, bool_or(chargeoff_flag) AS case_chargeoff_flag,
             max(contacts_last_7d) AS contacts_last_7d, min(contact_cap_7d) AS contact_cap_7d, max(last_contact_ts) AS last_contact_ts_case
      FROM act GROUP BY 1),
    allc AS (
      SELECT customer_key, count(*) AS n_cases_total, sum(promises_total) AS case_promises_total,
             sum(promises_kept) AS case_promises_kept, sum(promises_broken) AS case_promises_broken, min(case_open_date) AS first_case_open_date,
             max(case_open_date) AS latest_case_open_date
      FROM cs GROUP BY 1)
    SELECT allc.*, a.* EXCLUDE (customer_key), l.* EXCLUDE (customer_key)
    FROM allc LEFT JOIN a USING (customer_key) LEFT JOIN latest l USING (customer_key)""")

    # ---------------------------------------------------------------- contacts / notes / PTP history
    con.execute(f"""
    CREATE TABLE contactagg AS
    SELECT cc.customer_key, count(*) AS n_contacts_total,
           count(*) FILTER (WHERE h.contact_ts_utc >= TIMESTAMP '{AS_OF}' - INTERVAL 90 DAY AND h.direction <> 'internal') AS n_contacts_90d,
           count(*) FILTER (WHERE h.contact_ts_utc >= TIMESTAMP '{AS_OF}' - INTERVAL 90 DAY AND h.rpc_flag) AS n_rpc_90d,
           count(*) FILTER (WHERE h.direction = 'outbound' AND h.contact_ts_utc >= TIMESTAMP '{AS_OF}' - INTERVAL 7 DAY) AS n_outbound_7d,
           max(h.contact_ts_utc) FILTER (WHERE h.direction <> 'internal') AS last_contact_ts_utc,
           arg_max(h.channel_eff, h.contact_ts_utc) FILTER (WHERE h.direction <> 'internal') AS last_contact_channel
    FROM {P('contact_history')} h JOIN clusters cc ON cc.crm_customer_id = h.crm_customer_id GROUP BY 1""")
    con.execute(f"""
    CREATE TABLE noteagg AS
    SELECT cc.customer_key, count(*) AS n_notes, max(n.note_ts_utc) AS last_note_ts_utc
    FROM {P('agent_notes')} n JOIN clusters cc ON cc.crm_customer_id = n.crm_customer_id GROUP BY 1""")
    con.execute(f"""
    CREATE TABLE ptpagg AS
    SELECT cc.customer_key, count(*) AS n_ptp_total,
           count(*) FILTER (WHERE p.ptp_status = 'kept') AS n_ptp_kept,
           count(*) FILTER (WHERE p.ptp_status = 'broken') AS n_ptp_broken,
           count(*) FILTER (WHERE p.ptp_status = 'partially_kept') AS n_ptp_partially_kept,
           count(*) FILTER (WHERE p.ptp_status = 'open') AS n_ptp_open,
           arg_max(p.ptp_status, p.ptp_created_ts) AS last_ptp_status, max(p.ptp_created_ts) AS last_ptp_created_ts
    FROM {P('promises_to_pay')} p JOIN clusters cc ON cc.crm_customer_id = p.crm_customer_id GROUP BY 1""")

    # ---------------------------------------------------------------- identity quality: confidence, conflicts, trust
    con.execute(f"""
    CREATE TABLE idq AS
    WITH link_agg AS (
      SELECT customer_key, min(match_confidence) FILTER (WHERE link_status IN ('linked','linked_with_alternatives')) AS min_link_conf,
             count(*) FILTER (WHERE link_status IN ('linked','linked_with_alternatives')) AS n_linked_accounts,
             bool_or(shared_mobile) AS link_shared_mobile
      FROM links WHERE customer_key IS NOT NULL GROUP BY 1),
    link_flags AS (
      SELECT customer_key, list(DISTINCT f ORDER BY f) AS flags FROM
        (SELECT customer_key, unnest(conflict_flags) AS f FROM links
         WHERE customer_key IS NOT NULL AND link_status IN ('linked','linked_with_alternatives'))
      WHERE f NOT IN ('shared_mobile', 'source_dob_missing') GROUP BY 1),
    merge_conf AS (
      SELECT cc.customer_key, min(p.confidence) AS min_merge_conf
      FROM {I('crm_pairs')} p JOIN clusters cc ON cc.crm_customer_id = p.id_a WHERE p.decision = 'merge' GROUP BY 1),
    pair_flags AS (
      SELECT customer_key, list(DISTINCT f ORDER BY f) AS flags FROM (
        SELECT cc.customer_key, CASE p.decision WHEN 'conflict' THEN 'crm_conflicting_identity_evidence' ELSE 'crm_possible_duplicate_unresolved' END AS f
        FROM {I('crm_pairs')} p JOIN clusters cc ON cc.crm_customer_id IN (p.id_a, p.id_b) WHERE p.decision IN ('conflict','review')) GROUP BY 1),
    phone_shared AS (
      SELECT DISTINCT cc.customer_key FROM {P('customers')} c JOIN clusters cc USING (crm_customer_id)
      JOIN (SELECT phone FROM {I('phone_sharing')}) s ON s.phone = right(regexp_replace(c.primary_phone_e164, '[^0-9]', '', 'g'), 10))
    SELECT i.customer_key,
           least(1.0, coalesce(la.min_link_conf, 1.0), coalesce(mc.min_merge_conf, 1.0)) AS match_confidence,
           CASE WHEN la.n_linked_accounts > 0 THEN 'crm+linked_accounts' ELSE 'crm_only' END AS identity_basis,
           coalesce(la.n_linked_accounts, 0) AS n_linked_accounts,
           coalesce(la.link_shared_mobile, false) OR ps.customer_key IS NOT NULL AS shared_mobile_blocked,
           list_sort(list_distinct(list_concat(coalesce(lf.flags, []), coalesce(pf.flags, []),
                    CASE WHEN a.n_accounts_pending_review > 0 THEN ['accounts_pending_identity_review'] ELSE [] END))) AS conflict_flags
    FROM id_block i LEFT JOIN link_agg la USING (customer_key) LEFT JOIN merge_conf mc USING (customer_key)
    LEFT JOIN link_flags lf USING (customer_key) LEFT JOIN pair_flags pf USING (customer_key)
    LEFT JOIN phone_shared ps USING (customer_key) LEFT JOIN acct a USING (customer_key)""")

    # ---------------------------------------------------------------- assemble
    con.execute(f"""
    CREATE TABLE c360 AS
    SELECT i.customer_key, i.anchor_crm_customer_id, i.member_crm_ids, i.n_crm_records,
           i.first_name, i.last_name, i.date_of_birth, i.province_code, i.city, i.fsa, i.preferred_language, i.segment,
           i.customer_status, i.customer_since_date, i.tenure_months, i.phone_last4,
           i.consent_sms, i.consent_call, i.consent_email, i.consent_autodialer,
           i.national_dncl_flag, i.internal_do_not_call_flag, i.cease_communication_flag,
           i.bankruptcy_flag, i.consumer_proposal_flag, i.deceased_flag, i.insolvency_filed_date,
           i.credit_counselling_agency_flag, i.third_party_authorised_flag,
           a.* EXCLUDE (customer_key),
           cs.* EXCLUDE (customer_key),
           coalesce(ct.n_contacts_total, 0) AS n_contacts_total, coalesce(ct.n_contacts_90d, 0) AS n_contacts_90d,
           coalesce(ct.n_rpc_90d, 0) AS n_rpc_90d, coalesce(ct.n_outbound_7d, 0) AS n_outbound_7d,
           ct.last_contact_ts_utc, ct.last_contact_channel,
           coalesce(nt.n_notes, 0) AS n_notes, nt.last_note_ts_utc,
           coalesce(pt.n_ptp_total, 0) AS n_ptp_total, coalesce(pt.n_ptp_kept, 0) AS n_ptp_kept, coalesce(pt.n_ptp_broken, 0) AS n_ptp_broken,
           coalesce(pt.n_ptp_partially_kept, 0) AS n_ptp_partially_kept, coalesce(pt.n_ptp_open, 0) AS n_ptp_open,
           pt.last_ptp_status, pt.last_ptp_created_ts,
           q.identity_basis, q.match_confidence, q.n_linked_accounts, q.shared_mobile_blocked, q.conflict_flags,
           len(q.conflict_flags) AS open_conflicts,
           round(greatest(0.0, least(1.0, q.match_confidence - least(0.05 * len(q.conflict_flags), 0.25)
                 - CASE WHEN q.shared_mobile_blocked THEN 0.30 ELSE 0 END)), 4) AS trust_score,
           DATE '{AS_OF}' AS as_of_date
    FROM id_block i
    LEFT JOIN acct a USING (customer_key) LEFT JOIN casesagg cs USING (customer_key)
    LEFT JOIN contactagg ct USING (customer_key) LEFT JOIN noteagg nt USING (customer_key) LEFT JOIN ptpagg pt USING (customer_key)
    JOIN idq q USING (customer_key)""")
    # A count of "nothing linked" is 0, not unknown.  Sums/maxima/flags stay NULL when there is nothing to aggregate.
    zero_fill = ["n_card_accounts", "n_card_active", "n_loan_accounts", "n_deposit_accounts", "n_deposit_active",
                 "n_cases_total", "n_active_cases", "case_promises_total", "case_promises_kept", "case_promises_broken"]
    for col in zero_fill:
        con.execute(f"ALTER TABLE c360 ALTER COLUMN {col} SET DATA TYPE BIGINT")
        con.execute(f"UPDATE c360 SET {col} = coalesce({col}, 0)")
    con.execute(f"ALTER TABLE c360 ADD COLUMN identity_review_required BOOLEAN")
    con.execute(f"UPDATE c360 SET identity_review_required = trust_score < {TRUST_REVIEW_THRESHOLD}")

    # ---------------------------------------------------------------- attribute provenance (conflict-preserving)
    con.execute(f"""
    CREATE TABLE src_vals AS
    SELECT 'CARDS' AS source_system, card_account_id AS account_id, upper(replace(cardholder_postal_raw,' ','')) AS postal,
           CASE WHEN cardholder_phone_raw !~ '[xX]' THEN right(regexp_replace(cardholder_phone_raw,'[^0-9]','','g'),10) END AS phone FROM {P('card_accounts')}
    UNION ALL SELECT 'LENDING', loan_id, upper(replace(borrower_postal_raw,' ','')), right(regexp_replace(borrower_phone_raw,'[^0-9]','','g'),10) FROM {P('loan_accounts')}
    UNION ALL SELECT 'CORE', deposit_account_id, upper(replace(holder_postal_raw,' ','')), right(regexp_replace(holder_phone_raw,'[^0-9]','','g'),10) FROM {P('deposit_accounts')}""")
    con.execute(f"""
    CREATE TABLE attr_long AS
    WITH crmv AS (
      SELECT i.customer_key, 'CRM' AS source_system, upper(replace(c.postal_code,' ','')) AS postal,
             right(regexp_replace(c.primary_phone_e164,'[^0-9]','','g'),10) AS phone, 1.0 AS source_confidence
      FROM id_block i JOIN {P('customers')} c ON c.crm_customer_id = i.anchor_crm_customer_id),
    srcv AS (
      SELECT l.customer_key, l.source_system, v.postal, v.phone, any_value(l.match_confidence) AS source_confidence
      FROM links l JOIN src_vals v ON v.source_system = l.source_system AND v.account_id = l.account_id
      WHERE l.link_status IN ('linked','linked_with_alternatives') GROUP BY 1, 2, 3, 4),
    u AS (SELECT customer_key, source_system, postal, phone, source_confidence FROM crmv UNION ALL SELECT * FROM srcv),
    f AS (
      SELECT customer_key, 'postal_fsa' AS field, source_system, left(postal, 3) AS value_key, left(postal, 3) AS value_shown, source_confidence
      FROM u WHERE postal IS NOT NULL
      UNION ALL
      SELECT customer_key, 'phone_last4', source_system, phone, right(phone, 4), source_confidence FROM u WHERE phone IS NOT NULL)
    SELECT customer_key, field, value_shown AS value, source_system AS source, any_value(source_confidence) AS confidence,
           CASE WHEN count(DISTINCT value_key) OVER (PARTITION BY customer_key, field) > 1 THEN 'conflict' ELSE 'ok' END AS conflict_status
    FROM f GROUP BY customer_key, field, value_shown, value_key, source_system""")

    con.execute("CREATE OR REPLACE TABLE out.c360 AS SELECT * FROM c360 ORDER BY customer_key")
    con.execute("CREATE OR REPLACE TABLE out.c360_attributes AS SELECT * FROM attr_long ORDER BY customer_key, field, source")

    # ---------------------------------------------------------------- report numbers
    rep = {"generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"), "customer_keys": n_keys,
           "columns": [c[0] for c in con.execute("DESCRIBE c360").fetchall()]}
    rep["identity_basis"] = dict(con.execute("SELECT identity_basis, count(*) FROM c360 GROUP BY 1").fetchall())
    rep["trust_score_distribution"] = dict(con.execute(
        "SELECT CASE WHEN trust_score >= 0.95 THEN '>=0.95' WHEN trust_score >= 0.85 THEN '0.85-0.95' WHEN trust_score >= 0.70 THEN '0.70-0.85' WHEN trust_score >= 0.5 THEN '0.50-0.70' ELSE '<0.50' END b, count(*) FROM c360 GROUP BY 1 ORDER BY 1").fetchall())
    rep["identity_review_required"] = con.execute("SELECT count(*) FROM c360 WHERE identity_review_required").fetchone()[0]
    rep["shared_mobile_customers"] = con.execute("SELECT count(*) FROM c360 WHERE shared_mobile_blocked").fetchone()[0]
    rep["customers_with_active_case"] = con.execute("SELECT count(*) FROM c360 WHERE n_active_cases > 0").fetchone()[0]
    rep["customers_with_any_case"] = con.execute("SELECT count(*) FROM c360 WHERE n_cases_total > 0").fetchone()[0]
    rep["customers_with_linked_accounts"] = con.execute("SELECT count(*) FROM c360 WHERE n_linked_accounts > 0").fetchone()[0]
    rep["customers_with_pending_review_accounts"] = con.execute("SELECT count(*) FROM c360 WHERE n_accounts_pending_review > 0").fetchone()[0]
    rep["conflict_flag_counts"] = dict(con.execute("SELECT f, count(*) FROM (SELECT unnest(conflict_flags) f FROM c360) GROUP BY 1 ORDER BY 2 DESC").fetchall())
    rep["attribute_conflicts"] = dict(con.execute("SELECT field, count(DISTINCT customer_key) FROM attr_long WHERE conflict_status = 'conflict' GROUP BY 1").fetchall())
    rep["null_rates_pct"] = {c: round(100 * n / n_keys, 2) for c, n in zip(
        rep["columns"], con.execute("SELECT " + ", ".join(f'count(*) - count("{c}")' for c in rep["columns"]) + " FROM c360").fetchone()) if n}
    (REPORTS / "c360_report.json").write_text(json.dumps(rep, indent=1, default=str))
    print(json.dumps({k: v for k, v in rep.items() if k not in ("columns", "null_rates_pct")}, indent=1, default=str))
    print(f"{len(rep['columns'])} columns in {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
