#!/usr/bin/env python3
"""
Generate contracts/data_contract.yaml for the *actual* golden C360 table (warehouse/c360.duckdb: c360).

Adapted from the reference implementation (CIBC_Hackathon_CodeBlooded-main/scripts/make_data_contract.py).

Types and null rates come from the built table; meaning/source/validation come from the FIELDS dictionary below.
The script fails if a column of customer_360 has no entry (or an entry has no column), so the contract cannot drift.

This file documents what the pipeline really produces and how it differs from the Round 1 design (see `differences_from_design_contract`).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from data_pipeline.config import ROOT, WAREHOUSE

C360_DB = WAREHOUSE / "c360.duckdb"

# name: (source, meaning, validation)
CRM = "customers (CRM)"
F = {
 "customer_key": (f"identity_matching: crm_clusters.customer_key", "Stable key of the person = anchor CRM id of the cluster of duplicate CRM records. The CRM id is what notes, PTPs and contacts already carry.", "unique, not null"),
 "anchor_crm_customer_id": ("identity_matching: cluster anchor", "CRM record chosen as survivor (a record that is not a declared duplicate, then lowest id). Equals customer_key.", "not null; = customer_key"),
 "member_crm_ids": ("identity_matching: crm_clusters", "All CRM ids merged into this customer_key (provenance of the merge; see table id_crm_pairs in warehouse/c360.duckdb for evidence).", "contains anchor; length = n_crm_records"),
 "n_crm_records": ("identity_matching", "Number of CRM records in the cluster (1 or 2 in current data).", ">= 1; <= 4"),
 "first_name": (f"{CRM}.first_name (anchor)", "Legal first name of the anchor record. Direct PII.", "trimmed"),
 "last_name": (f"{CRM}.last_name (anchor)", "Legal last name of the anchor record. Direct PII.", "trimmed"),
 "date_of_birth": (f"{CRM}.date_of_birth (anchor)", "Date of birth (ISO). Direct PII.", "1900-01-01 <= value <= as_of_date; null allowed (0.5%)"),
 "province_code": (f"{CRM}.province_code_norm (anchor)", "Province/territory, normalised to 2-letter codes (rule C-CUST-02).", "in {ON,QC,BC,AB,MB,SK,NS,NB,NL,PE,NT,YT,NU}"),
 "city": (f"{CRM}.city (anchor)", "Municipality. Quasi-identifier; proxy-risk field (requires disparity test per design contract).", ""),
 "fsa": (f"{CRM}.fsa (anchor)", "Forward Sortation Area (first 3 postal characters). Quasi-identifier; proxy-risk field.", "3 chars"),
 "preferred_language": (f"{CRM}.preferred_language (anchor)", "Preferred service language. Proxy-risk field per design contract; for contact routing only.", ""),
 "segment": (f"{CRM}.segment (anchor)", "Customer segment (mass, emerging_affluent, affluent, private_hnw).", ""),
 "customer_status": (f"{CRM}.customer_status (anchor)", "CRM status (active, closed, closed_by_bank, deceased, written_off).", ""),
 "customer_since_date": (f"{CRM}.customer_since_date (anchor)", "First product opened with the bank.", "<= as_of_date"),
 "tenure_months": (f"{CRM}.tenure_months (anchor)", "Customer tenure in months as supplied by CRM.", ">= 0"),
 "phone_last4": (f"{CRM}.primary_phone_e164 (anchor), masked", "Last four digits of primary phone. Full numbers are not carried into the C360 (PII masked by default).", "4 digits"),
 "consent_sms": (f"{CRM}.consent_sms", "Consent to SMS. AND across merged CRM records (conservative).", "boolean"),
 "consent_call": (f"{CRM}.consent_call", "Consent to calls. AND across merged records.", "boolean"),
 "consent_email": (f"{CRM}.consent_email", "Consent to email. AND across merged records.", "boolean"),
 "consent_autodialer": (f"{CRM}.consent_autodialer", "Consent to autodialer. AND across merged records.", "boolean"),
 "national_dncl_flag": (f"{CRM}.national_dncl_flag", "On the National DNCL (applies to telemarketing, not collections). OR across merged records.", "boolean"),
 "internal_do_not_call_flag": (f"{CRM}.internal_do_not_call_flag", "Internal do-not-call. OR across merged records.", "boolean"),
 "cease_communication_flag": (f"{CRM}.cease_communication_flag", "Customer asked to cease communication. OR across merged records.", "boolean; must stop contact"),
 "bankruptcy_flag": (f"{CRM}.bankruptcy_flag", "Bankruptcy on record. OR across merged records.", "boolean"),
 "consumer_proposal_flag": (f"{CRM}.consumer_proposal_flag", "Consumer proposal on record. OR across merged records.", "boolean"),
 "deceased_flag": (f"{CRM}.deceased_flag", "Customer deceased. OR across merged records.", "boolean"),
 "insolvency_filed_date": (f"{CRM}.insolvency_filed_date", "Earliest insolvency filing date among merged records.", "<= as_of_date"),
 "credit_counselling_agency_flag": (f"{CRM}.credit_counselling_agency_flag", "Represented by a credit counselling agency. OR across merged records.", "boolean"),
 "third_party_authorised_flag": (f"{CRM}.third_party_authorised_flag", "A third party is authorised to act. OR across merged records.", "boolean"),
 "n_card_accounts": ("card_accounts via identity_links (linked only)", "Number of card accounts attributed to this customer (tier A-C, unique best customer).", ">= 0"),
 "n_card_active": ("card_accounts.account_status = 'active'", "Active card accounts among those linked.", "<= n_card_accounts"),
 "card_balance_total": ("sum(card_accounts.current_balance)", "Total card balance (CAD) of linked cards at their snapshot_date. Null if none.", "decimal(.,2)"),
 "card_max_dpd": ("max(card_accounts.dpd)", "Worst days-past-due across linked cards. Null if none or dpd null.", ">= 0"),
 "card_any_chargeoff": ("bool_or(card_accounts.chargeoff_flag)", "Any linked card charged off.", "boolean"),
 "n_loan_accounts": ("loan_accounts via identity_links", "Number of loan accounts attributed.", ">= 0"),
 "loan_principal_total": ("sum(loan_accounts.principal_balance)", "Total principal balance (CAD) of linked loans. Null if none.", ""),
 "loan_max_dpd": ("max(loan_accounts.dpd)", "Worst days-past-due across linked loans.", ">= 0"),
 "loan_any_chargeoff": ("bool_or(loan_accounts.chargeoff_flag)", "Any linked loan charged off.", "boolean"),
 "n_deposit_accounts": ("deposit_accounts via identity_links", "Number of deposit accounts attributed.", ">= 0"),
 "n_deposit_active": ("deposit_accounts.account_status = 'active'", "Active deposit accounts among those linked.", "<= n_deposit_accounts"),
 "deposit_balance_total": ("sum(deposit_accounts.current_balance)", "Total deposit balance (CAD); may be negative (overdrafts).", ""),
 "overdraft_max_dpd": ("max(deposit_accounts.overdraft_dpd)", "Worst overdraft days-past-due; null for almost everyone.", ">= 0"),
 "n_accounts_pending_review": ("identity_links.link_status in (review, ambiguous)", "Accounts whose best candidate is this customer but whose link is too weak/ambiguous to attribute. Their balances are NOT included above.", ">= 0"),
 "n_cases_total": ("collections_cases via coll_customer_ref -> customer_key", "All collections cases ever (orphan CRM refs excluded).", ">= 0"),
 "case_promises_total": ("sum(collections_cases.promises_total)", "Promises recorded across all cases.", ">= 0"),
 "case_promises_kept": ("sum(collections_cases.promises_kept)", "Promises kept across all cases.", ">= 0"),
 "case_promises_broken": ("sum(collections_cases.promises_broken)", "Promises broken across all cases.", ">= 0"),
 "first_case_open_date": ("min(collections_cases.case_open_date)", "", "<= as_of_date"),
 "latest_case_open_date": ("max(collections_cases.case_open_date)", "", "<= as_of_date"),
 "n_active_cases": ("collections_cases where outcome IS NULL", "Active cases. 'Active' = outcome is null, as defined in the dataset README example query.", ">= 0"),
 "max_current_dpd": ("max(collections_cases.current_dpd) over active cases", "Worst current DPD.", ">= 0"),
 "total_overdue": ("sum(collections_cases.total_overdue) over active cases", "Overdue amount (CAD).", ""),
 "total_exposure": ("sum(collections_cases.total_exposure) over active cases", "Exposure (CAD).", ""),
 "worst_bucket": ("collections_cases.current_bucket of the max-DPD active case", "Delinquency bucket: current,1-30,31-60,61-90,91-120,121-150,151-180,180+.", ""),
 "hardship_flag": ("bool_or(collections_cases.hardship_flag) over active cases", "Hardship indicated on an active case. Route-only: may soften treatment, never intensify (design contract).", "boolean"),
 "hardship_program_accepted": ("bool_or(collections_cases.hardship_program_accepted)", "A hardship programme was accepted on an active case.", "boolean"),
 "case_cease_contact_flag": ("bool_or(collections_cases.cease_contact_flag)", "Cease-contact flag on an active case.", "boolean; must stop contact"),
 "insolvency_hold_flag": ("bool_or(collections_cases.insolvency_hold_flag)", "Insolvency hold on an active case.", "boolean"),
 "deceased_hold_flag": ("bool_or(collections_cases.deceased_hold_flag)", "Deceased hold on an active case.", "boolean"),
 "case_chargeoff_flag": ("bool_or(collections_cases.chargeoff_flag)", "Charge-off on an active case.", "boolean"),
 "contacts_last_7d": ("max(collections_cases.contacts_last_7d)", "Contacts in the last 7 days as maintained by the collections platform.", ">= 0"),
 "contact_cap_7d": ("min(collections_cases.contact_cap_7d)", "Contact cap per 7 days (most restrictive across active cases).", ">= 0"),
 "last_contact_ts_case": ("max(collections_cases.last_contact_ts)", "Last contact timestamp per collections platform (local time).", ""),
 "case_id_latest": ("latest active case by case_open_date", "Most recently opened active case.", ""),
 "current_treatment": ("collections_cases.current_treatment of latest active case", "Current treatment stage.", ""),
 "queue": ("collections_cases.queue of latest active case", "Work queue.", ""),
 "current_ptp_id": ("collections_cases.current_ptp_id of latest active case", "Current open PTP id.", "exists in promises_to_pay (verified, 0 orphans)"),
 "current_ptp_amount": ("collections_cases.current_ptp_amount", "Promised amount (CAD).", "> 0 when present"),
 "current_ptp_due_date": ("collections_cases.current_ptp_due_date", "Promised payment date.", ""),
 "n_contacts_total": ("contact_history via crm_customer_id -> customer_key", "All contact_history rows incl. internal system events. Orphan CRM refs excluded.", ">= 0"),
 "n_contacts_90d": ("contact_history, direction <> 'internal', last 90 days to as_of_date", "Customer-facing contacts in 90 days (inbound + outbound).", ">= 0"),
 "n_rpc_90d": ("contact_history.rpc_flag", "Right-party contacts in 90 days.", ">= 0"),
 "n_outbound_7d": ("contact_history.direction = 'outbound'", "Outbound contacts in last 7 days from the contact log (compare contacts_last_7d).", ">= 0"),
 "last_contact_ts_utc": ("contact_history.contact_ts_utc", "Latest customer-facing contact (UTC).", ""),
 "last_contact_channel": ("contact_history.channel_effective", "Channel of the latest contact; channel_effective = COALESCE(channel_v2, channel) per REL-0926.", ""),
 "n_notes": ("agent_notes via crm_customer_id", "Number of agent notes.", ">= 0"),
 "last_note_ts_utc": ("max(agent_notes.note_ts_utc)", "", ""),
 "n_ptp_total": ("promises_to_pay via crm_customer_id", "All PTPs.", ">= 0"),
 "n_ptp_kept": ("promises_to_pay.ptp_status = 'kept'", "", ">= 0"),
 "n_ptp_broken": ("promises_to_pay.ptp_status = 'broken'", "", ">= 0"),
 "n_ptp_partially_kept": ("promises_to_pay.ptp_status = 'partially_kept'", "", ">= 0"),
 "n_ptp_open": ("promises_to_pay.ptp_status = 'open'", "", ">= 0"),
 "last_ptp_status": ("promises_to_pay.ptp_status of latest ptp_created_ts", "", ""),
 "last_ptp_created_ts": ("max(promises_to_pay.ptp_created_ts)", "", ""),
 "identity_basis": ("identity_matching", "'crm_only' = no account could be attributed; 'crm+linked_accounts' = at least one tier A-C link.", "in {crm_only, crm+linked_accounts}"),
 "match_confidence": ("identity_matching tiers", "Minimum of cluster-merge confidence and linked-account tier confidences (A .97, B .90, C .88; merges .99/.95/.93); 1.0 when CRM-only singleton. Ordinal, not a calibrated probability.", "0 <= x <= 1"),
 "n_linked_accounts": ("identity_links", "Accounts attributed through tier A-C links.", ">= 0"),
 "shared_mobile_blocked": ("identity_matching: crm_phone_sharing / link flag", "A phone of this customer is also used by a different person; phone evidence was neutralised and trust reduced.", "boolean"),
 "conflict_flags": ("identity_matching", "Distinct conflict types: phone_mismatch, postal_mismatch, first_name_conflict, multiple_candidate_people, crm_conflicting_identity_evidence, crm_possible_duplicate_unresolved, accounts_pending_identity_review.", "subset of known values"),
 "open_conflicts": ("len(conflict_flags)", "Count of distinct conflict types.", ">= 0"),
 "trust_score": ("data_pipeline/c360.py", "match_confidence - min(0.05*open_conflicts, 0.25) - 0.30*shared_mobile_blocked, clamped [0,1]. NOT a credit score; identity-data trust only.", "0 <= x <= 1, not null"),
 "as_of_date": ("dataset README snapshot date", "Date the C360 is as-of (2026-09-28).", "constant"),
 "identity_review_required": ("trust_score < 0.70", "Identity review threshold 0.70 (assumption); below it a human confirms identity before action.", "boolean"),
}


def main() -> int:
    con = duckdb.connect()
    con.execute(f"ATTACH '{C360_DB.as_posix()}' AS c (READ_ONLY)")
    con.execute("CREATE VIEW c360 AS SELECT * FROM c.c360")
    cols = con.execute(f"DESCRIBE SELECT * FROM c360").fetchall()
    names = [c[0] for c in cols]
    missing = [n for n in names if n not in F]
    extra = [n for n in F if n not in names]
    if missing or extra:
        print(f"contract/table mismatch: undocumented={missing} stale={extra}", file=sys.stderr)
        return 1
    n = con.execute(f"SELECT count(*) FROM c360").fetchone()[0]
    nulls = con.execute("SELECT " + ", ".join(f'count(*) - count("{c}")' for c in names) + f" FROM c360").fetchone()

    def y(s: str) -> str:
        return json.dumps(s, ensure_ascii=False)

    L = []
    w = L.append
    w("# Data Contract: Golden Customer 360 (as built)")
    w("# Generated by data_pipeline/data_contract.py from warehouse/c360.duckdb (table c360). Do not edit by hand.")
    w("# Format modelled on the organisers' DC-COLL-001 example. Differences from our Round 1 design are listed below.")
    w("")
    w("id: C360-MAPLE-AS-BUILT")
    w('version: "0.1"')
    w("status: draft")
    w(f"generated_utc: {y(datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M'))}")
    w(f"as_of_date: \"2026-09-28\"")
    w(f"grain: one row per customer_key")
    w(f"row_count: {n}")
    w("owner: {team: TBC, named_owner: TBC}")
    w("")
    w("sources:")
    for s_ in ("customers", "card_accounts", "loan_accounts", "deposit_accounts", "external (bureau name/DOB for CIF matching only)",
               "collections_cases", "contact_history", "promises_to_pay", "agent_notes"):
        w(f"  - {y(s_)}")
    w("lineage: >")
    w("  raw CSV/Parquet (immutable) -> data_pipeline/load_raw.py -> warehouse/maple.duckdb raw_* (typed, IDs as text) -> data_pipeline/curate.py cur_* views (flags added, no rows dropped)")
    w("  -> data_pipeline/identity.py -> warehouse/c360.duckdb id_* tables (clusters, pair evidence, account links)")
    w("  -> data_pipeline/c360.py -> warehouse/c360.duckdb c360 + c360_attributes.")
    w("  Data issues and handling: docs/data_notes.md and outputs/DATA_QUALITY_REPORT.md. Identity accuracy: outputs/IDENTITY_METRICS.md.")
    w("")
    w("fields:")
    for (name, typ, *_), nn in zip(cols, nulls):
        src, meaning, rule = F[name]
        w(f"  - name: {name}")
        w(f"    type: {y(typ)}")
        w(f"    required: {'true' if nn == 0 else 'false'}")
        w(f"    null_rate_pct: {round(100 * nn / n, 2)}")
        w(f"    source: {y(src)}")
        if meaning:
            w(f"    meaning: {y(meaning)}")
        if rule:
            w(f"    validation: {y(rule)}")
    w("")
    w("companion_tables:")
    w("  c360_attributes:")
    w("    grain: customer_key x field x source x value")
    w("    purpose: conflict-preserving provenance for postal_fsa and phone_last4 across CRM/CARDS/LENDING/CORE; conflict_status = 'conflict' when sources disagree. Disagreeing values are never overwritten.")
    w("    columns: [customer_key, field, value, source, confidence, conflict_status]")
    w("  id_identity_links (warehouse/c360.duckdb):")
    w("    grain: source account")
    w("    columns: [source_system, account_id, src_customer_ref, customer_key, link_status, tier, match_confidence, match_method, match_evidence, supporting_identity_records, bureau_supported, candidate_people, shared_mobile, conflict_flags]")
    w("  id_crm_pairs (warehouse/c360.duckdb):")
    w("    grain: candidate duplicate CRM pair")
    w("    columns: [id_a, id_b, blocks, same_nh, diff_nh, same_dob, diff_dob, same_ln, same_fn, same_postal, same_phone, declared, decision, method, confidence]")
    w("")
    w("quality_rules:")
    for rid, d, sev in [("QR-01", "customer_key unique and not null", "error"),
                        ("QR-02", "trust_score in [0,1] and not null", "error"),
                        ("QR-03", "no protected attribute column in c360", "error"),
                        ("QR-04", "every source account has exactly one identity_links row (asserted in data_pipeline/identity.py)", "error"),
                        ("QR-05", "auto-linked accounts only for tiers A-C with a unique best customer; D/E and ties are withheld for review", "error"),
                        ("QR-06", "orphan customer references are retained, never silently dropped or fixed", "warn")]:
        w(f"  - {{id: {rid}, severity: {sev}, description: {y(d)}}}")
    w("")
    w("protected_and_pii_handling:")
    w("  excluded_protected: [gender_code, marital_status, citizenship_status, household_size, newcomer_program_flag, accessibility_needs_flag, vulnerability_type, accent]")
    w("  excluded_pending_decision: [vulnerability_flag]   # see differences_from_design_contract #3")
    w("  excluded_direct_pii: [email, street address, postal_code, full phone numbers, national_id_hash, middle/preferred name]")
    w("  retained_pii_note: first_name, last_name, date_of_birth are retained for the agent desktop; restrict access (RBAC) before any sharing.")
    w("  proxy_risk_fields_present: [city, fsa, preferred_language]   # require a logged disparity test before use in decisions")
    w("")
    w("differences_from_design_contract:")
    w("  - id: 1")
    w("    topic: scope")
    w("    detail: >")
    w("      The Round 1 design lists PTP lifecycle, promise_quality, hardship_status (none/possible/confirmed), break_risk_t3/t1 and model_version.")
    w("      These are produced at runtime by the decisioning layer (features, model, gates), not by the dataset, so they are absent here.")
    w("      This table is the golden identity/account/delinquency/contact/PTP-history input.")
    w("  - id: 2")
    w("    topic: customer_key")
    w("    detail: >")
    w("      customer_key is the anchor CRM id of the duplicate cluster (no new surrogate), because notes, PTPs and contacts already carry CRM ids.")
    w("  - id: 3")
    w("    topic: vulnerability_flag")
    w("    detail: >")
    w("      The dataset README says vulnerability fields must not feed decisions. It is excluded from the C360; the decisioning layer uses it")
    w("      only as a protective gate and audit attribute, never as a model feature.")
    w("  - id: 4")
    w("    topic: hardship_status")
    w("    detail: >")
    w("      Only a boolean hardship_flag from collections_cases is available; possible/recorded/confirmed is derived in the decisioning layer.")
    w("  - id: 5")
    w("    topic: freshness")
    w("    detail: >")
    w("      Snapshot as of 2026-09-28; the C360 is a batch build, not streaming. Refresh by re-running data_pipeline.identity then data_pipeline.c360.")
    out = ROOT / "contracts" / "data_contract.yaml"
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(L) + "\n")
    print(f"wrote {out} ({len(names)} fields)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
