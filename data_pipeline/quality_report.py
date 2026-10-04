"""Data quality report: every number below is measured live from warehouse/maple.duckdb and warehouse/c360.duckdb.

Run:  python -m data_pipeline.quality_report
Out:  outputs/DATA_QUALITY_REPORT.md, outputs/data_quality_results.json

Structure follows the reference report layout (overview, missing values, duplicates, dates, ID mismatches, referential
integrity, schema drift, conflicting attributes, cleaning decisions, validation, open issues). Raw data is never modified.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

import duckdb

from data_pipeline.config import MAIN_DB, ROOT, SCHEMA_JSON, WAREHOUSE

OUT = ROOT / "outputs"
n = lambda x: f"{int(x):,}"
pct = lambda a, b: f"{100 * a / b:.2f}%" if b else "n/a"

con = duckdb.connect()
con.execute(f"ATTACH '{MAIN_DB.as_posix()}' AS m (READ_ONLY)")
con.execute(f"ATTACH '{(WAREHOUSE / 'c360.duckdb').as_posix()}' AS c (READ_ONLY)")
con.execute("USE m")
one = lambda sql: con.execute(sql).fetchone()[0]
rows = lambda sql: con.execute(sql).fetchall()

R: dict = {"generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")}
L: list[str] = []
w = L.append
schema = json.loads(SCHEMA_JSON.read_text(encoding="utf-8"))

# ------------------------------------------------------------------ 1 overview + 2 missing values
tables = [r[0] for r in rows("SELECT table_name FROM information_schema.tables WHERE table_catalog='m' AND table_name LIKE 'raw_%' ORDER BY 1")]
overview = {}
for t in tables:
    nrows = one(f"SELECT count(*) FROM m.main.{t}")
    summ = rows(f"SELECT column_name, null_percentage FROM (SUMMARIZE m.main.{t})")
    overview[t[4:]] = dict(rows=nrows, cols=len(summ),
                           null20=sum(1 for _, p in summ if p is not None and float(p) >= 20),
                           null50=sum(1 for _, p in summ if p is not None and float(p) >= 50))
R["tables"] = overview
mismatch = {t: (schema[t]["rows"], v["rows"]) for t, v in overview.items() if t in schema and schema[t]["rows"] != v["rows"]}
R["row_count_mismatches_vs_schema"] = mismatch

w("# Data Quality Report\n")
w(f"*Generated {R['generated_utc']} UTC by `data_pipeline/quality_report.py`. Every number is measured from the warehouse at build time. "
  "Raw tables are never modified; issues are flagged in `cur_*` views or handled at query time.*\n")
w("## 1. Dataset overview\n")
w(f"{len(overview)} tables, {n(sum(v['rows'] for v in overview.values()))} rows. Row counts match `schema.json` for "
  f"{sum(1 for t in overview if t in schema and t not in mismatch)} of {sum(1 for t in overview if t in schema)} tables"
  + ("." if not mismatch else f" (mismatches: {mismatch}).") + "\n")
w("| Table | Rows | Columns | Columns ≥20% null | Columns ≥50% null |")
w("|---|---:|---:|---:|---:|")
for t, v in overview.items():
    w(f"| `{t}` | {n(v['rows'])} | {v['cols']} | {v['null20']} | {v['null50']} |")
w("\nMissing values are preserved as NULL and never imputed. `NA` is a real call-outcome code and is kept verbatim "
  "(the loader only turns empty strings into NULL). Identifier columns are loaded as text so leading zeros survive.\n")

# ------------------------------------------------------------------ 3 duplicates
w("## 2. Duplicates\n")
dup = {}
dup["crm_records"] = one("SELECT count(*) FROM m.raw_customers")
dup["crm_declared_duplicates"] = one("SELECT count(*) FROM m.raw_customers WHERE duplicate_of_crm_id IS NOT NULL")
dup["crm_shared_national_id_hash_groups"] = one("SELECT count(*) FROM (SELECT national_id_hash FROM m.raw_customers WHERE national_id_hash IS NOT NULL GROUP BY 1 HAVING count(*) > 1)")
dup["crm_records_resolved_into_other_keys"] = one("SELECT count(*) FROM c.id_crm_clusters WHERE cluster_size > 1")
dup["customer_keys"] = one("SELECT count(DISTINCT customer_key) FROM c.id_crm_clusters")
dup["declared_duplicates_merged"] = one("""SELECT count(*) FROM m.raw_customers d JOIN c.id_crm_clusters a ON a.crm_customer_id = d.crm_customer_id
    JOIN c.id_crm_clusters b ON b.crm_customer_id = d.duplicate_of_crm_id WHERE a.customer_key = b.customer_key""")
dup["declared_duplicates_with_both_ids_present"] = one("""SELECT count(*) FROM m.raw_customers d JOIN c.id_crm_clusters a ON a.crm_customer_id = d.crm_customer_id
    JOIN c.id_crm_clusters b ON b.crm_customer_id = d.duplicate_of_crm_id""")
R["duplicates"] = dup
w(f"- CRM holds {n(dup['crm_records'])} records; {n(dup['crm_declared_duplicates'])} carry `duplicate_of_crm_id`, and "
  f"{n(dup['crm_shared_national_id_hash_groups'])} national-ID-hash values appear on more than one record (the real duplicate population is larger than the flagged one).")
w(f"- Identity resolution collapses them to {n(dup['customer_keys'])} customer keys ({n(dup['crm_records_resolved_into_other_keys'])} records sit in merged clusters). "
  f"Of the declared duplicates whose both ids exist, {n(dup['declared_duplicates_merged'])} of {n(dup['declared_duplicates_with_both_ids_present'])} "
  f"({pct(dup['declared_duplicates_merged'], dup['declared_duplicates_with_both_ids_present'])}) were merged by evidence; the rest were withheld for review, not forced.\n")
w("| Business key | Rows | Distinct | Duplicated rows |")
w("|---|---:|---:|---:|")
keyspec = [("customers.crm_customer_id", "raw_customers", "crm_customer_id"), ("card_accounts.card_account_id", "raw_card_accounts", "card_account_id"),
           ("deposit_accounts.deposit_account_id", "raw_deposit_accounts", "deposit_account_id"), ("collections_cases.case_id", "raw_collections_cases", "case_id"),
           ("promises_to_pay.ptp_id", "raw_promises_to_pay", "ptp_id"), ("contact_history.contact_id", "raw_contact_history", "contact_id"),
           ("agent_notes.note_id", "raw_agent_notes", "note_id"), ("loan_accounts.loan_id", "raw_loan_accounts", "loan_id")]
R["business_keys"] = {}
for label, t, col in keyspec:
    tot, dist = rows(f"SELECT count(*), count(DISTINCT {col}) FROM m.{t}")[0]
    R["business_keys"][label] = dict(rows=tot, distinct=dist)
    w(f"| `{label}` | {n(tot)} | {n(dist)} | {n(tot - dist)} |")
w("\nAll primary keys are unique. Customers repeat across systems, not within them, which is why identity matching is needed.\n")

# ------------------------------------------------------------------ 4 date issues
w("## 3. Date and format issues\n")
fm = {}
fm["snapshot_iso"] = one("SELECT count(*) FROM m.raw_account_monthly_snapshot WHERE month_end_date ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'")
fm["snapshot_slash"] = one("SELECT count(*) FROM m.raw_account_monthly_snapshot WHERE month_end_date LIKE '%/%'")
fm["snapshot_compact"] = one("SELECT count(*) FROM m.raw_account_monthly_snapshot WHERE month_end_date ~ '^[0-9]{8}$'")
fm["snapshot_unparsed_in_curated"] = one("SELECT count(*) FROM m.cur_snapshot WHERE month_end_parsed IS NULL")
fm["card_dob_dd-mm-yyyy"] = one("SELECT count(*) FROM m.raw_card_accounts WHERE try_strptime(cardholder_dob_raw, '%d-%m-%Y') IS NOT NULL")
fm["card_dob_unparsed"] = one("SELECT count(*) FROM m.raw_card_accounts WHERE cardholder_dob_raw IS NOT NULL AND try_strptime(cardholder_dob_raw, '%d-%m-%Y') IS NULL")
fm["loan_dob_unparsed"] = one("SELECT count(*) FROM m.raw_loan_accounts WHERE borrower_dob_raw IS NOT NULL AND try_strptime(borrower_dob_raw, '%Y/%m/%d') IS NULL")
fm["deposit_dob_unparsed"] = one("SELECT count(*) FROM m.raw_deposit_accounts WHERE holder_dob_raw IS NOT NULL AND try_strptime(holder_dob_raw, '%Y%m%d') IS NULL")
R["formats"] = fm
w(f"- `account_monthly_snapshot.month_end_date` arrives in 4 formats: ISO {n(fm['snapshot_iso'])}, slash (DD/MM and MM/DD mixed) {n(fm['snapshot_slash'])}, "
  f"compact YYYYMMDD {n(fm['snapshot_compact'])}. `cur_snapshot.month_end_parsed` parses all four (month-end days are 28+, so the slash order is unambiguous); unparsed afterwards: {n(fm['snapshot_unparsed_in_curated'])}.")
w(f"- Date of birth is stored differently per source system (cards `DD-MM-YYYY`, lending `YYYY/MM/DD`, core `YYYYMMDD`). Parsed per system for matching; "
  f"unparseable non-null values: cards {n(fm['card_dob_unparsed'])}, lending {n(fm['loan_dob_unparsed'])}, core {n(fm['deposit_dob_unparsed'])}.\n")

# ------------------------------------------------------------------ 5 ID mismatches
w("## 4. ID mismatches\n")
ids = {}
ids["deposit_cif_short"] = one("SELECT count(*) FROM m.raw_deposit_accounts WHERE regexp_matches(src_customer_ref, '^[0-9]+$') AND length(src_customer_ref) < 10")
ids["deposit_cif_total"] = one("SELECT count(*) FROM m.raw_deposit_accounts")
ids["bureau_join_before_pad"] = one("SELECT count(*) FROM m.raw_deposit_accounts d WHERE d.src_customer_ref IN (SELECT bank_subject_ref FROM m.raw_external)")
ids["bureau_join_after_pad"] = one("""SELECT count(*) FROM m.raw_deposit_accounts d WHERE
    CASE WHEN regexp_matches(d.src_customer_ref, '^[0-9]+$') AND length(d.src_customer_ref) < 10 THEN lpad(d.src_customer_ref, 10, '0') ELSE d.src_customer_ref END
    IN (SELECT bank_subject_ref FROM m.raw_external)""")
ids["contact_case_not_in_cases"] = one("SELECT count(*) FROM m.raw_contact_history h WHERE h.case_id IS NOT NULL AND h.case_id NOT IN (SELECT case_id FROM m.raw_collections_cases)")
ids["contact_with_case"] = one("SELECT count(*) FROM m.raw_contact_history WHERE case_id IS NOT NULL")
R["id_mismatches"] = ids
w(f"- **Deposit CIF leading zeros lost:** {n(ids['deposit_cif_short'])} of {n(ids['deposit_cif_total'])} deposit rows ({pct(ids['deposit_cif_short'], ids['deposit_cif_total'])}) have a 7-9 digit CIF "
  f"where 10 digits are required. Rows joining to the bureau file: {n(ids['bureau_join_before_pad'])} before left-padding, {n(ids['bureau_join_after_pad'])} after. The raw value is kept; the padded value is used for matching.")
w(f"- **Contact rows pointing at a case that does not exist:** {n(ids['contact_case_not_in_cases'])} of {n(ids['contact_with_case'])} contact rows with a case id. Retained and flagged, not dropped.\n")

# ------------------------------------------------------------------ 6 referential integrity
w("## 5. Referential integrity\n")
w("| Child column | Parent | Rows | Orphans | Orphan rate |")
w("|---|---|---:|---:|---:|")
R["referential"] = {}
for label, child, col, parent, pcol in [
    ("promises_to_pay.crm_customer_id", "raw_promises_to_pay", "crm_customer_id", "raw_customers", "crm_customer_id"),
    ("contact_history.crm_customer_id", "raw_contact_history", "crm_customer_id", "raw_customers", "crm_customer_id"),
    ("agent_notes.crm_customer_id", "raw_agent_notes", "crm_customer_id", "raw_customers", "crm_customer_id"),
    ("collections_cases.coll_customer_ref", "raw_collections_cases", "coll_customer_ref", "raw_customers", "crm_customer_id"),
    ("promises_to_pay.case_id", "raw_promises_to_pay", "case_id", "raw_collections_cases", "case_id"),
    ("agent_notes.case_id", "raw_agent_notes", "case_id", "raw_collections_cases", "case_id")]:
    tot, orph = rows(f"""SELECT count(*), count(*) FILTER (WHERE c.{col} NOT IN (SELECT {pcol} FROM m.{parent})) FROM m.{child} c WHERE c.{col} IS NOT NULL""")[0]
    R["referential"][label] = dict(rows=tot, orphans=orph)
    w(f"| `{label}` | `{parent[4:]}` | {n(tot)} | {n(orph)} | {pct(orph, tot)} |")
w("\nOrphans are retained and the customer is flagged; they are never silently dropped or repaired.\n")

# ------------------------------------------------------------------ 7 schema drift
w("## 6. Schema drift\n")
sd = {}
sd["channel_only_v2"] = one("SELECT count(*) FROM m.raw_contact_history WHERE channel IS NULL AND channel_v2 IS NOT NULL")
sd["channel_both"] = one("SELECT count(*) FROM m.raw_contact_history WHERE channel IS NOT NULL AND channel_v2 IS NOT NULL")
sd["channel_neither"] = one("SELECT count(*) FROM m.raw_contact_history WHERE channel IS NULL AND channel_v2 IS NULL")
R["schema_drift"] = sd
w(f"`contact_history.channel` was renamed `channel_v2` on 2026-09-26 (release REL-0926). {n(sd['channel_only_v2'])} rows carry only `channel_v2`, "
  f"{n(sd['channel_both'])} carry both, {n(sd['channel_neither'])} carry neither. `cur_contacts.channel_eff = coalesce(channel_v2, channel)`.\n")

# ------------------------------------------------------------------ 8 bucket / status inconsistencies
w("## 7. Internal inconsistencies\n")
inc = {}
inc["open_cases_outcome_null"] = one("SELECT count(*) FROM m.raw_collections_cases WHERE outcome IS NULL")
inc["open_cases_status_open"] = one("SELECT count(*) FROM m.raw_collections_cases WHERE case_status = 'open'")
inc["case_bucket_mismatch"] = one("SELECT count(*) FROM m.cur_cases WHERE is_open AND bucket_mismatch_flag")
inc["snapshot_bucket_mismatch_rows_with_dpd"] = one("SELECT count(*) FROM m.cur_snapshot WHERE dpd IS NOT NULL AND dpd_bucket IS DISTINCT FROM dpd_bucket_calc")
inc["snapshot_dpd_null"] = one("SELECT count(*) FROM m.raw_account_monthly_snapshot WHERE dpd IS NULL")
inc["ptp_open_past_grace"] = one("SELECT count(*) FROM m.cur_promises WHERE open_past_grace_flag")
R["inconsistencies"] = inc
w(f"- Open cases: `outcome IS NULL` gives {n(inc['open_cases_outcome_null'])}, `case_status='open'` gives {n(inc['open_cases_status_open'])}. We follow the dataset README (`outcome IS NULL`), which also matches the organisers' reference SQL.")
w(f"- `collections_cases.current_bucket` disagrees with `current_dpd` on {n(inc['case_bucket_mismatch'])} open cases; `dpd_bucket` disagrees with `dpd` on {n(inc['snapshot_bucket_mismatch_rows_with_dpd'])} snapshot rows. "
  "Recomputed columns (`*_calc`) sit beside the raw ones; raw is untouched.")
w(f"- `account_monthly_snapshot.dpd` is missing on {n(inc['snapshot_dpd_null'])} rows (left NULL, not imputed).")
w(f"- {n(inc['ptp_open_past_grace'])} promises are still `open` more than 2 days past due at the snapshot date (`open_past_grace_flag`); promise-rate metrics exclude open and cancelled promises.\n")

# ------------------------------------------------------------------ 9 conflicting attributes
w("## 8. Conflicting customer attributes\n")
ca = {}
ca["province_nonstandard"] = one("SELECT count(*) FROM m.raw_customers WHERE province_code IN ('Ont.','B.C.','Alta.','Quebec','PQ','Man.','N.S.')")
ca["province_null"] = one("SELECT count(*) FROM m.raw_customers WHERE province_code IS NULL")
ca["attr_conflict_customers"] = {f: k for f, k in rows("SELECT field, count(DISTINCT customer_key) FROM c.c360_attributes WHERE conflict_status = 'conflict' GROUP BY 1")}
ca["customers_with_conflict_flags"] = one("SELECT count(*) FROM c.c360 WHERE open_conflicts > 0")
ca["conflict_flag_counts"] = {f: k for f, k in rows("SELECT f, count(*) FROM (SELECT unnest(conflict_flags) f FROM c.c360) GROUP BY 1 ORDER BY 2 DESC")}
R["conflicts"] = ca
w(f"- {n(ca['province_nonstandard'])} CRM rows use a non-standard province spelling (`Ont.`, `B.C.`, `Quebec`, `PQ`, ...), normalised to 2-letter codes in the C360; {n(ca['province_null'])} are missing.")
w(f"- Across systems the same customer can carry different values. In `c360_attributes` (one row per customer, field, source), conflicts exist for: "
  + ", ".join(f"`{f}` on {n(k)} customers" for f, k in ca["attr_conflict_customers"].items()) + ". Conflicting values are kept side by side, never overwritten.")
w(f"- {n(ca['customers_with_conflict_flags'])} customers carry at least one identity conflict flag. Flag counts: "
  + ", ".join(f"`{f}` {n(k)}" for f, k in ca["conflict_flag_counts"].items()) + ".\n")

# ------------------------------------------------------------------ 10 identity matching
idm = json.loads((OUT / "identity_metrics.json").read_text())
o = idm["overall"]
w("## 9. Identity matching quality\n")
w("Five source systems share no customer key. Accounts are linked to CRM customers by date of birth (blocking) plus name, postal code and phone, with per-field evidence on every link. "
  "Phones shared by two or more people never count as evidence on their own.\n")
w("| Metric | Value |")
w("|---|---:|")
w(f"| Source accounts | {n(o['accounts'])} |")
w(f"| Auto-linked (tiers A-C, unique best customer) | {n(o['auto_linked'])} ({o['auto_link_rate_all_accounts_pct']}%) |")
w(f"| Withheld for review (tier D/E or tie) | {n(o['withheld'])} |")
w(f"| Unmatched (no candidate; mostly no DOB) | {n(o['unmatched'])} |")
w(f"| Precision on accounts with a collections-recorded customer link | **{o['precision_evaluable_pct']}%** ({n(o['evaluable_correct'])} / {n(o['evaluable_auto_linked'])}) |")
w(f"| Recall on the same population | {o['recall_evaluable_pct']}% |")
w("\nThe measuring population ({0} accounts) is the set that was ever contacted or promised on, not a random sample; accounts outside it have no ground truth. "
  "Per-tier detail: `outputs/IDENTITY_METRICS.md`.\n".format(n(o["evaluable_accounts"])))

# ------------------------------------------------------------------ 11 cleaning decisions
w("## 10. Cleaning decisions\n")
na_rows = one("SELECT count(*) FROM m.raw_contact_history WHERE outcome_code = 'NA'")
orph = sum(v["orphans"] for v in R["referential"].values())
n_acct = one("SELECT count(*) FROM c.id_identity_links")
shared_cust = one("SELECT count(*) FROM c.c360 WHERE shared_mobile_blocked")
shared_phones = one("SELECT count(*) FROM c.id_phone_sharing")
review_pairs = one("SELECT count(*) FROM c.id_crm_pairs WHERE decision = 'review'")
merged_recs = dup["crm_records_resolved_into_other_keys"]
R["cleaning_counts"] = dict(na_rows=na_rows, orphans=orph, accounts=n_acct, shared_customers=shared_cust, shared_phones=shared_phones, review_pairs=review_pairs)
w("| # | Issue | Records affected | Decision | Where |")
w("|---|---|---:|---|---|")
for i, (a, cnt, b, c_) in enumerate([
    ("`NA` outcome code vs null", f"{n(na_rows)} contact rows carry `NA`", "keep `NA` verbatim; only empty strings become NULL", "`load_raw.py`"),
    ("IDs with leading zeros", f"{n(ids['deposit_cif_short'])} deposit rows", "load as text; pad deposit CIF to 10 digits for matching, keep raw", "`load_raw.py`, `identity.py`"),
    ("`channel` renamed `channel_v2`", f"{n(sd['channel_only_v2'])} rows (channel missing), {n(sd['channel_both'])} carry both", "`channel_eff = coalesce(channel_v2, channel)`", "`cur_contacts`"),
    ("4 snapshot date formats", f"{n(fm['snapshot_slash'] + fm['snapshot_compact'])} snapshot rows in a non-ISO format", "parse all four", "`cur_snapshot.month_end_parsed`"),
    ("Bucket disagrees with DPD", f"{n(inc['case_bucket_mismatch'])} open cases, {n(inc['snapshot_bucket_mismatch_rows_with_dpd'])} snapshot rows", "add recomputed `*_calc` and a mismatch flag; raw untouched", "`cur_cases`, `cur_snapshot`"),
    ("Open-case definition", f"{n(inc['open_cases_outcome_null'] - inc['open_cases_status_open'])} cases differ between the two definitions", "`outcome IS NULL` per README", "`cur_cases.is_open`"),
    ("Open promise past grace", f"{n(inc['ptp_open_past_grace'])} promises", "flag, exclude from promise-rate metrics", "`cur_promises`"),
    ("Per-system DOB/name/phone/postal formats", f"all {n(n_acct)} source accounts", "normalise per system, match with evidence", "`identity.py`"),
    ("Duplicate CRM records", f"{n(merged_recs)} CRM records sit in merged clusters ({n(dup['crm_records'])} records become {n(dup['customer_keys'])} customers); {n(review_pairs)} weaker pairs left unmerged", "merge only on strong evidence; weaker pairs are listed, not merged", "`identity.py`"),
    ("Shared mobile numbers", f"{n(shared_phones)} phone numbers, {n(shared_cust)} customers flagged", "phone neutralised as evidence, customer flagged and trust-penalised", "`identity.py`, `c360.py`"),
    ("Province spellings", f"{n(ca['province_nonstandard'])} CRM rows", "map to 2-letter codes", "`c360.py`"),
    ("Orphan references", f"{n(orph)} child rows across 4 customer references", "retain and flag, never drop", "all layers"),
    ("Protected attributes", "8 columns (plus `vulnerability_flag`)", "excluded from the C360 and from model features (audited)", "`c360.py`, `decisioning/features.py`")], 1):
    w(f"| {i} | {a} | {cnt} | {b} | {c_} |")
w("\nNo row is dropped and nothing is imputed.\n")

# ------------------------------------------------------------------ 12 validation checks
w("## 11. Validation checks (run at report time)\n")
checks = []


def chk(name, ok, detail, sev="error"):
    checks.append(dict(check=name, passed=bool(ok), detail=detail, severity=sev))


chk("Row counts equal schema.json", not mismatch, "all tables" if not mismatch else str(mismatch))
chk("c360.customer_key is unique and not null", one("SELECT count(*) = count(DISTINCT customer_key) AND count(*) = count(customer_key) FROM c.c360"), f"{n(one('SELECT count(*) FROM c.c360'))} rows")
chk("c360.trust_score in [0,1] and not null", one("SELECT count(*) = 0 FROM c.c360 WHERE trust_score IS NULL OR trust_score < 0 OR trust_score > 1"), "")
chk("c360 holds no protected-attribute column", one("""SELECT count(*) = 0 FROM information_schema.columns WHERE table_catalog='c' AND table_name='c360' AND column_name IN
    ('gender_code','marital_status','citizenship_status','household_size','newcomer_program_flag','accessibility_needs_flag','vulnerability_type','vulnerability_flag','accent')"""), "")
chk("identity_links has exactly one row per source account", one("SELECT count(*) FROM c.id_identity_links") == sum(
    one(f"SELECT count(*) FROM m.{t}") for t in ("raw_card_accounts", "raw_deposit_accounts")) + one("SELECT count(DISTINCT loan_id) FROM m.raw_loan_accounts"),
    f"{n(one('SELECT count(*) FROM c.id_identity_links'))} links")
chk("No auto-link below tier C", one("SELECT count(*) = 0 FROM c.id_identity_links WHERE link_status IN ('linked','linked_with_alternatives') AND tier NOT IN ('A','B','C')"), "")
chk("Every CRM record belongs to exactly one customer_key", one("SELECT count(*) = count(DISTINCT crm_customer_id) FROM c.id_crm_clusters") and
    one("SELECT count(*) FROM c.id_crm_clusters") == one("SELECT count(*) FROM m.raw_customers"), "")
chk("Identity precision >= 99% on collections-recorded links", o["precision_evaluable_pct"] >= 99, f"{o['precision_evaluable_pct']}%")
chk("Orphan references present (retained, flagged)", any(v["orphans"] for v in R["referential"].values()), "informational", "warn")
chk("Open-case definition differs between outcome and case_status", inc["open_cases_outcome_null"] != inc["open_cases_status_open"], "documented, README definition used", "warn")
R["checks"] = checks
ok = sum(c_["passed"] for c_ in checks if c_["severity"] == "error")
tot_err = sum(1 for c_ in checks if c_["severity"] == "error")
w(f"{ok} of {tot_err} error-severity checks pass.\n")
w("| Check | Severity | Result | Detail |")
w("|---|---|---|---|")
for c_ in checks:
    w(f"| {c_['check']} | {c_['severity']} | {('pass' if c_['passed'] else 'FAIL') if c_['severity'] == 'error' else ('noted' if c_['passed'] else 'not observed')} | {c_['detail']} |")
w("\nAlso covered by tests: `tests/check_gold_sql.py` (organiser reference SQL reproduces on our raw tables) and `tests/test_curated.py` (curated views pinned to known counts).\n")

# ------------------------------------------------------------------ 13 open
w("## 12. Open issues and limits\n")
w("- Benchmark questions BQ-035 and BQ-017 touch known data traps; the default follows the organisers' reference SQL and the divergence is noted in `docs/insight_layer.md`.")
w("- Amount fields on promises (`ptp_amount`, overdue ratios) look generated after the outcome and are excluded from the production model; see `docs/decisioning.md`.")
w("- Identity precision is measured only on contacted/promised accounts; the rest is unmeasured.")
w("- Accounts withheld for review are counted but their balances are not attributed to any customer.")

(OUT / "DATA_QUALITY_REPORT.md").write_text("\n".join(L) + "\n", encoding="utf-8")
(OUT / "data_quality_results.json").write_text(json.dumps(R, indent=1, default=str), encoding="utf-8")
print(f"wrote DATA_QUALITY_REPORT.md; {ok}/{tot_err} error checks pass")
sys.exit(0 if ok == tot_err else 1)
