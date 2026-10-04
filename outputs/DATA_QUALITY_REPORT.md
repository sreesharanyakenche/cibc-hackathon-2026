# Data Quality Report

*Generated 2026-10-04 15:11 UTC by `data_pipeline/quality_report.py`. Every number is measured from the warehouse at build time. Raw tables are never modified; issues are flagged in `cur_*` views or handled at query time.*

## 1. Dataset overview

31 tables, 68,162,322 rows. Row counts match `schema.json` for 31 of 31 tables.

| Table | Rows | Columns | Columns ≥20% null | Columns ≥50% null |
|---|---:|---:|---:|---:|
| `account_monthly_snapshot` | 12,569,692 | 16 | 4 | 1 |
| `agent_notes` | 760,594 | 26 | 3 | 2 |
| `agent_shifts` | 15,317 | 15 | 2 | 1 |
| `agents` | 1,000 | 30 | 0 | 0 |
| `benchmark_questions` | 35 | 7 | 2 | 1 |
| `bureau_history` | 4,679,562 | 10 | 0 | 0 |
| `call_transcripts` | 25,000 | 29 | 1 | 1 |
| `card_accounts` | 660,000 | 148 | 33 | 23 |
| `card_statements` | 7,531,283 | 26 | 2 | 2 |
| `case_assignment_history` | 423,830 | 23 | 1 | 0 |
| `channel_capacity` | 287 | 11 | 4 | 2 |
| `code_lookups` | 113 | 7 | 2 | 2 |
| `collections_cases` | 367,229 | 132 | 44 | 32 |
| `contact_history` | 2,688,337 | 128 | 83 | 77 |
| `customers` | 1,020,000 | 148 | 35 | 21 |
| `deposit_accounts` | 780,000 | 145 | 16 | 15 |
| `external` | 1,000,000 | 122 | 14 | 14 |
| `geo_reference` | 656 | 15 | 0 | 0 |
| `hardship_programs` | 18 | 21 | 2 | 2 |
| `loan_accounts` | 404,500 | 135 | 43 | 26 |
| `loan_instalments` | 14,832,472 | 19 | 4 | 2 |
| `metric_definitions` | 15 | 14 | 0 | 0 |
| `model_scores` | 1,718,841 | 33 | 8 | 7 |
| `offers` | 40,767 | 19 | 8 | 1 |
| `ops_events` | 6 | 10 | 1 | 0 |
| `promises_to_pay` | 238,815 | 29 | 4 | 2 |
| `qa_checklist` | 10 | 10 | 0 | 0 |
| `reference_documents` | 19 | 17 | 3 | 3 |
| `salary_credit_history` | 6,000,000 | 11 | 2 | 0 |
| `transactions` | 12,401,924 | 25 | 8 | 6 |
| `voice_samples` | 2,000 | 34 | 1 | 0 |

Missing values are preserved as NULL and never imputed. `NA` is a real call-outcome code and is kept verbatim (the loader only turns empty strings into NULL). Identifier columns are loaded as text so leading zeros survive.

## 2. Duplicates

- CRM holds 1,020,000 records; 5,950 carry `duplicate_of_crm_id`, and 18,496 national-ID-hash values appear on more than one record (the real duplicate population is larger than the flagged one).
- Identity resolution collapses them to 1,000,729 customer keys (38,542 records sit in merged clusters). Of the declared duplicates whose both ids exist, 5,826 of 5,950 (97.92%) were merged by evidence; the rest were withheld for review, not forced.

| Business key | Rows | Distinct | Duplicated rows |
|---|---:|---:|---:|
| `customers.crm_customer_id` | 1,020,000 | 1,020,000 | 0 |
| `card_accounts.card_account_id` | 660,000 | 660,000 | 0 |
| `deposit_accounts.deposit_account_id` | 780,000 | 780,000 | 0 |
| `collections_cases.case_id` | 367,229 | 367,229 | 0 |
| `promises_to_pay.ptp_id` | 238,815 | 238,815 | 0 |
| `contact_history.contact_id` | 2,688,337 | 2,688,337 | 0 |
| `agent_notes.note_id` | 760,594 | 760,594 | 0 |
| `loan_accounts.loan_id` | 404,500 | 404,500 | 0 |

All primary keys are unique. Customers repeat across systems, not within them, which is why identity matching is needed.

## 3. Date and format issues

- `account_monthly_snapshot.month_end_date` arrives in 4 formats: ISO 11,314,540, slash (DD/MM and MM/DD mixed) 837,071, compact YYYYMMDD 418,081. `cur_snapshot.month_end_parsed` parses all four (month-end days are 28+, so the slash order is unambiguous); unparsed afterwards: 0.
- Date of birth is stored differently per source system (cards `DD-MM-YYYY`, lending `YYYY/MM/DD`, core `YYYYMMDD`). Parsed per system for matching; unparseable non-null values: cards 0, lending 0, core 0.

## 4. ID mismatches

- **Deposit CIF leading zeros lost:** 156,837 of 780,000 deposit rows (20.11%) have a 7-9 digit CIF where 10 digits are required. Rows joining to the bureau file: 623,163 before left-padding, 780,000 after. The raw value is kept; the padded value is used for matching.
- **Contact rows pointing at a case that does not exist:** 26,864 of 2,688,337 contact rows with a case id. Retained and flagged, not dropped.

## 5. Referential integrity

| Child column | Parent | Rows | Orphans | Orphan rate |
|---|---|---:|---:|---:|
| `promises_to_pay.crm_customer_id` | `customers` | 238,815 | 6,744 | 2.82% |
| `contact_history.crm_customer_id` | `customers` | 2,661,633 | 75,500 | 2.84% |
| `agent_notes.crm_customer_id` | `customers` | 760,594 | 21,592 | 2.84% |
| `collections_cases.coll_customer_ref` | `customers` | 367,229 | 10,365 | 2.82% |
| `promises_to_pay.case_id` | `collections_cases` | 238,815 | 0 | 0.00% |
| `agent_notes.case_id` | `collections_cases` | 760,594 | 0 | 0.00% |

Orphans are retained and the customer is flagged; they are never silently dropped or repaired.

## 6. Schema drift

`contact_history.channel` was renamed `channel_v2` on 2026-09-26 (release REL-0926). 19,463 rows carry only `channel_v2`, 11,965 carry both, 0 carry neither. `cur_contacts.channel_eff = coalesce(channel_v2, channel)`.

## 7. Internal inconsistencies

- Open cases: `outcome IS NULL` gives 96,000, `case_status='open'` gives 86,059. We follow the dataset README (`outcome IS NULL`), which also matches the organisers' reference SQL.
- `collections_cases.current_bucket` disagrees with `current_dpd` on 3,807 open cases; `dpd_bucket` disagrees with `dpd` on 461,396 snapshot rows. Recomputed columns (`*_calc`) sit beside the raw ones; raw is untouched.
- `account_monthly_snapshot.dpd` is missing on 1,005,143 rows (left NULL, not imputed).
- 5,286 promises are still `open` more than 2 days past due at the snapshot date (`open_past_grace_flag`); promise-rate metrics exclude open and cancelled promises.

## 8. Conflicting customer attributes

- 10,689 CRM rows use a non-standard province spelling (`Ont.`, `B.C.`, `Quebec`, `PQ`, ...), normalised to 2-letter codes in the C360; 2,007 are missing.
- Across systems the same customer can carry different values. In `c360_attributes` (one row per customer, field, source), conflicts exist for: `phone_last4` on 338,427 customers, `postal_fsa` on 5,546 customers. Conflicting values are kept side by side, never overwritten.
- 31,484 customers carry at least one identity conflict flag. Flag counts: `phone_mismatch` 16,567, `postal_mismatch` 5,522, `crm_conflicting_identity_evidence` 5,132, `accounts_pending_identity_review` 2,671, `crm_possible_duplicate_unresolved` 1,595, `multiple_candidate_people` 958, `first_name_conflict` 171.

## 9. Identity matching quality

Five source systems share no customer key. Accounts are linked to CRM customers by date of birth (blocking) plus name, postal code and phone, with per-field evidence on every link. Phones shared by two or more people never count as evidence on their own.

| Metric | Value |
|---|---:|
| Source accounts | 1,844,500 |
| Auto-linked (tiers A-C, unique best customer) | 1,812,472 (98.26%) |
| Withheld for review (tier D/E or tie) | 3,547 |
| Unmatched (no candidate; mostly no DOB) | 28,481 |
| Precision on accounts with a collections-recorded customer link | **99.84%** (276,424 / 276,856) |
| Recall on the same population | 97.38% |

The measuring population (283,851 accounts) is the set that was ever contacted or promised on, not a random sample; accounts outside it have no ground truth. Per-tier detail: `outputs/IDENTITY_METRICS.md`.

## 10. Cleaning decisions

| # | Issue | Records affected | Decision | Where |
|---|---|---:|---|---|
| 1 | `NA` outcome code vs null | 228,124 contact rows carry `NA` | keep `NA` verbatim; only empty strings become NULL | `load_raw.py` |
| 2 | IDs with leading zeros | 156,837 deposit rows | load as text; pad deposit CIF to 10 digits for matching, keep raw | `load_raw.py`, `identity.py` |
| 3 | `channel` renamed `channel_v2` | 19,463 rows (channel missing), 11,965 carry both | `channel_eff = coalesce(channel_v2, channel)` | `cur_contacts` |
| 4 | 4 snapshot date formats | 1,255,152 snapshot rows in a non-ISO format | parse all four | `cur_snapshot.month_end_parsed` |
| 5 | Bucket disagrees with DPD | 3,807 open cases, 461,396 snapshot rows | add recomputed `*_calc` and a mismatch flag; raw untouched | `cur_cases`, `cur_snapshot` |
| 6 | Open-case definition | 9,941 cases differ between the two definitions | `outcome IS NULL` per README | `cur_cases.is_open` |
| 7 | Open promise past grace | 5,286 promises | flag, exclude from promise-rate metrics | `cur_promises` |
| 8 | Per-system DOB/name/phone/postal formats | all 1,844,500 source accounts | normalise per system, match with evidence | `identity.py` |
| 9 | Duplicate CRM records | 38,542 CRM records sit in merged clusters (1,020,000 records become 1,000,729 customers); 827 weaker pairs left unmerged | merge only on strong evidence; weaker pairs are listed, not merged | `identity.py` |
| 10 | Shared mobile numbers | 4,662 phone numbers, 8,933 customers flagged | phone neutralised as evidence, customer flagged and trust-penalised | `identity.py`, `c360.py` |
| 11 | Province spellings | 10,689 CRM rows | map to 2-letter codes | `c360.py` |
| 12 | Orphan references | 114,201 child rows across 4 customer references | retain and flag, never drop | all layers |
| 13 | Protected attributes | 8 columns (plus `vulnerability_flag`) | excluded from the C360 and from model features (audited) | `c360.py`, `decisioning/features.py` |

No row is dropped and nothing is imputed.

## 11. Validation checks (run at report time)

8 of 8 error-severity checks pass.

| Check | Severity | Result | Detail |
|---|---|---|---|
| Row counts equal schema.json | error | pass | all tables |
| c360.customer_key is unique and not null | error | pass | 1,000,729 rows |
| c360.trust_score in [0,1] and not null | error | pass |  |
| c360 holds no protected-attribute column | error | pass |  |
| identity_links has exactly one row per source account | error | pass | 1,844,500 links |
| No auto-link below tier C | error | pass |  |
| Every CRM record belongs to exactly one customer_key | error | pass |  |
| Identity precision >= 99% on collections-recorded links | error | pass | 99.84% |
| Orphan references present (retained, flagged) | warn | noted | informational |
| Open-case definition differs between outcome and case_status | warn | noted | documented, README definition used |

Also covered by tests: `tests/check_gold_sql.py` (organiser reference SQL reproduces on our raw tables) and `tests/test_curated.py` (curated views pinned to known counts).

## 12. Open issues and limits

- Benchmark questions BQ-035 and BQ-017 touch known data traps; the default follows the organisers' reference SQL and the divergence is noted in `docs/insight_layer.md`.
- Amount fields on promises (`ptp_amount`, overdue ratios) look generated after the outcome and are excluded from the production model; see `docs/decisioning.md`.
- Identity precision is measured only on contacted/promised accounts; the rest is unmeasured.
- Accounts withheld for review are counted but their balances are not attributed to any customer.
