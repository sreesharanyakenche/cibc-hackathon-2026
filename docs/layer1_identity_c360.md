# Layer 1: identity resolution and the golden Customer 360

Everything below was measured on the Maple data (snapshot 2026-09-28) by the code in `data_pipeline/`. Numbers come from
`outputs/identity_report.json`, `outputs/c360_report.json`, `outputs/IDENTITY_METRICS.md` and `outputs/DATA_QUALITY_REPORT.md`.

Credit: the matching approach, C360 build, validation and contract generator were adapted from a reference implementation
(`CIBC_Hackathon_CodeBlooded-main`) and re-pointed at our `raw_*` tables. The rules and tiers are unchanged; every number here was re-measured on our own run.

## Run
```
cd data_pipeline && python load_raw.py && python curate.py && cd ..
python -m data_pipeline.identity            # ~90 s  -> warehouse/c360.duckdb: id_crm_pairs, id_crm_clusters, id_identity_links, id_phone_sharing
python -m data_pipeline.c360                # ~2 min -> c360, c360_attributes
python -m data_pipeline.identity_validate   # outputs/IDENTITY_METRICS.md, identity_metrics.json
python -m data_pipeline.data_contract       # contracts/data_contract.yaml
python -m data_pipeline.quality_report      # outputs/DATA_QUALITY_REPORT.md
```
`warehouse/maple.duckdb` is opened read-only; results go to `warehouse/c360.duckdb`.

## The problem
The five source systems share no customer key. Each formats name, date of birth, phone and postal code differently
(cards `DD-MM-YYYY`, lending `YYYY/MM/DD`, core `YYYYMMDD`; names as `M. DESJARDINS`, `DESJARDINS M F`, `Desjardins, Michel Francois`; phones with masks like `819364xxx00`).
CRM itself holds duplicates (5,950 flagged with `duplicate_of_crm_id`; 18,496 national-ID hashes appear on more than one record), and deposit CIFs lose leading zeros in 20.1% of rows.

## Stage 1: CRM duplicates to one customer key
* Candidate pairs come from three blocks: equal national-ID hash, the declared `duplicate_of_crm_id` pointer, or equal DOB + last name + postal code.
* A pair is **merged** only on strong evidence: national-ID hash + DOB (23,993 pairs), a declared pointer with at least three corroborating fields (360), or DOB + name + postal + phone (835).
  A pair with equal hashes but different DOB, or different hashes, is a **conflict** (2,658) and is never merged. Weaker pairs are **review** (827): listed with their evidence, not merged.
* `customer_key` is the anchor CRM id of the cluster (a record that is not a declared duplicate, then the lowest id), because notes, promises and contacts already carry CRM ids. Other member ids are listed in `member_crm_ids`, never discarded.
* Result: 1,020,000 CRM records become 1,000,729 customer keys (38,542 records sit in merged clusters; none over the size limit of 4).
* Of the 5,950 declared duplicates whose both ids exist, 5,826 (97.9%) were merged by evidence; the rest were withheld, not forced.

## Stage 2: source accounts to customers
Accounts (cards, lending, core deposit, plus the bureau file's full name and DOB for core CIFs) are matched to CRM customers by blocking on **date of birth** then comparing last name,
first name, postal code and phone, with per-field evidence kept on every link. Distinct identity tuples are matched, then rolled up to source customers.

| Tier | Rule | Confidence | Outcome |
|---|---|---:|---|
| A | DOB + last name + first name exact + at least one corroborator (postal exact, or a non-shared phone exact/partial) | 0.97 | auto-link |
| B | DOB + last name + first name exact, no corroborator available, nothing contradicting | 0.90 | auto-link |
| C | DOB + last name + first initial / middle / preferred name + a corroborator | 0.88 | auto-link |
| D | DOB + last name + first initial only | 0.65 | review, not linked |
| E | DOB + last name, first name contradicts, two corroborators | 0.60 | review, not linked |

`match_confidence` is an ordinal confidence by tier, **not** a calibrated probability. An A to C account is also withheld (`ambiguous`) when two different customers tie at the best tier.

**Shared mobile protection.** A phone number used by two or more different customers (4,662 numbers; 10 used by three or more) has its evidence neutralised and the link is flagged. A phone is never decisive on its own.
This matters most in collections: the worst error is contacting the wrong person about a debt.

**Link outcome (1,844,500 accounts):** 1,812,472 auto-linked (98.26%); 3,547 withheld for review; 28,481 unmatched (19,788 have no date of birth to block on, the rest had no candidate customer).
By tier: A 767,691, B 727,430, C 318,058, D 468, E 2,372. `identity_links` has exactly one row per account (asserted in the code).

## Accuracy (what was measured, and what was not)
Ground truth is the collections system's own account to CRM-customer record: an account that appears in `contact_history` or `promises_to_pay` with exactly one non-orphan CRM id (215 accounts with conflicting ids excluded).
It was created without any name, DOB, phone or postal matching, so it is independent of the matcher, but it is **not an audited gold label**.

| Metric | Value |
|---|---:|
| Accounts with a ground truth | 283,851 (15.4% of all accounts) |
| Auto-link rate on those | 97.54% |
| **Precision of auto-links** | **99.84%** (276,424 of 276,856) |
| Recall | 97.38% |
| Precision by system | cards 99.85%, core 99.87%, lending 99.83% |
| Precision when the true CRM record is a singleton (independent of merge decisions) | 99.85% |
| Precision when the true CRM record sits in a merged cluster | 99.47% |

**Limit:** the measured accounts are the ones that were contacted or promised on, mostly tier A (65% of them against 42% of all accounts). Accounts outside that set have **no ground truth**, so their precision is unmeasured.
Tier-by-tier detail, including how many withheld accounts would have been right, is in `outputs/IDENTITY_METRICS.md`.

## Golden Customer 360 (`warehouse/c360.duckdb: c360`)
One row per `customer_key` (1,000,729 rows, 93 columns), documented field by field in [contracts/data_contract.yaml](../contracts/data_contract.yaml); a test checks the contract matches the table.
* **Accounts:** only auto-linked accounts contribute balances and delinquency. Withheld accounts are counted (`n_accounts_pending_review`) but their balances are **not** attributed to anyone (2,671 customers have some).
* **Merged records:** restrictive flags are OR-ed (cease contact, do-not-call, bankruptcy, deceased); consents are AND-ed. Conservative by construction.
* **Conflicts are kept, not overwritten.** `c360_attributes` holds value, source, confidence and conflict status per customer, field and source.
  Postal area conflicts exist for 5,546 customers and phone last-4 for 338,427 (phone differences between systems are common; they are shown, not resolved).
* **Trust score** (0 to 1, **not** a credit score) = match confidence (weakest link) minus 0.05 per open conflict (capped at 0.25) minus 0.30 if a shared mobile is involved.
  There is no freshness term. Distribution: 272,496 at 0.95 or above; 709,761 at 0.85 to 0.95; 9,798 at 0.70 to 0.85; 8,674 below 0.70 (`identity_review_required`).
* **Excluded:** protected attributes (gender, marital status, citizenship, household, newcomer, accessibility, vulnerability, accent) and direct PII (full phone, email, street address, national-ID hash). First name, last name and date of birth are kept for the agent desktop and need access control before any sharing.
* 986,322 customers have linked accounts; 14,407 are CRM-only. 277,973 have at least one case and 93,320 an active one.

## How Layer 1 feeds decisions
The Decision Record carries `customer_key`, `match_confidence`, `trust_score` and conflict flags. A trust score below **0.70** (an assumption: no document gives a threshold) fires the `uncertain_identity`
review trigger, so a human confirms who the customer is before any contact. This affects 269 of 32,106 open promises (0.8%) and changes nothing else: on 340 scored promises, risk and recommended action were identical with the gate on and off.

## Known limits
* The 0.70 threshold, the tier confidences and the cluster size limit are assumptions.
* CRM-only customers (no linked account) get no account balances. Accounts with no date of birth cannot be blocked on and stay unmatched.
* The identity ground truth only covers contacted or promised accounts (above).
* The model's bureau and transaction features still use the older account-to-CRM bridge (90.7% agreement with the promise's own CRM id), not the golden C360 links. See `docs/decisioning.md`.
