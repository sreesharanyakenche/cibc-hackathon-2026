# Data notes (working log for the data quality report)

Measured on `warehouse/maple.duckdb` (built by `data_pipeline/load_raw.py`, curated by `data_pipeline/curate.py`).

| # | Issue | Records | Handling |
|---|---|---:|---|
| 1 | `contact_history.channel` renamed `channel_v2` on 2026-09-26 (REL-0926) | 19,463 rows have only `channel_v2` | `cur_contacts.channel_eff = coalesce(channel_v2, channel)` |
| 2 | `account_monthly_snapshot.month_end_date` in 4 formats (ISO, DD/MM/YYYY, MM/DD/YYYY, YYYYMMDD) | 837,071 slash-format rows (about half each way), 418,081 YYYYMMDD | `cur_snapshot.month_end_parsed`; unambiguous because month-end days are >= 28 |
| 3 | `account_monthly_snapshot.dpd` missing | 1,005,143 of 12,569,692 | left NULL; `dpd_bucket_calc` NULL where dpd NULL |
| 4 | `account_monthly_snapshot.dpd_bucket` disagrees with `dpd` | 461,396 of 11,564,549 rows with dpd | `dpd_bucket_calc` added |
| 5 | `collections_cases.current_bucket` disagrees with `current_dpd` | 3,807 of 96,000 open cases | `current_bucket_calc` and `bucket_mismatch_flag` added; raw column untouched |
| 6 | Open cases: `outcome IS NULL` (96,000) vs `case_status='open'` (86,059) | 9,941 | `cur_cases.is_open = outcome IS NULL` (matches dev gold) |
| 7 | Open PTPs already past due + 2 days at the snapshot date | 5,286 | `cur_promises.open_past_grace_flag` |

## Open decisions caused by these findings
* **BQ-035 (open cases by bucket).** Dev gold uses the raw `current_bucket` (33,639 / 25,389 / ...).
  Recomputing from `current_dpd` gives round numbers (35,000 / 25,000 / 16,000 / ...). The organiser note says a
  strong answer recomputes. Gold and note disagree; undecided which the scorer uses.
* **BQ-017 (roll rate).** The gold SQL parses only ISO, DD/MM/YYYY and YYYYMMDD, so it silently drops the
  MM/DD/YYYY rows. Parsing all four formats changes the result (for example auto_loan 8.2% over 466 accounts vs gold
  7.4% over 430). Undecided whether to match gold or be correct.

## Validation
* `tests/check_gold_sql.py` runs the organisers' reference SQL on our raw tables: 12 of 12 dev questions reproduce.
* `tests/test_curated.py` pins the curated views to known counts.
