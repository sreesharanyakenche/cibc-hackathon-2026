# Layer 3/4: feature registry, PTP-break model, policy gates, decisions

Status: library, served by the demo API and UI in `app/` and `frontend/` (see `docs/architecture.md`). Layer 1's golden Customer 360 now feeds the identity gate (see below). Everything below was measured on the synthetic dataset; "assumption" marks anything the provided
documents do not state.

## Run
```
# derived tables only (14 s, no retraining; identical to the original up to float summation order, max 2e-10):
.venv\Scripts\python -c "from decisioning import features as F; F.build_derived_tables(F.connect())"
.venv\Scripts\python -m decisioning.train       # builds derived tables, trains + calibrates, OVERWRITES models/ and reports AUCs
.venv\Scripts\python -m decisioning.fairness    # disparity check -> docs/fairness_check.md
.venv\Scripts\python -m pytest tests -q
```
```python
from decisioning.orchestrator import Decisioner
d = Decisioner()                                     # needs warehouse/c360.duckdb for the identity block (without it identity is None and only the duplicate flags apply); optional: role_directory={"REV-17": "reviewer"} to bind reviewer ids to roles
d.upcoming(days=3, limit=25)                         # whole horizon scored, riskiest first (limit=None for everything)
rec = d.recommend("PTP-5511630")                     # score -> gates -> ranking -> explanation -> hash-chained Decision Record
d.review(rec["decision_id"], "REV-17", "reviewer", "accept", reason="agreed")
d.review(rec["decision_id"], "REV-17", "reviewer", "accept", reason="customer confirmed hours cut", confirm_hardship=True)
d.store.verify()                                     # re-checks every record's hash and the chain
```

## Pipeline
```
promise created -> features (one registry) -> calibrated risk -> gates (deterministic, ordered) -> action ranking
   -> review triggers -> explanation (3 reasons, note for context, what-would-change) -> immutable Decision Record -> human
```
The model supplies one number. Everything that decides what may happen to a customer is rule-based and logged. Nothing here contacts a
customer; output goes to a human. The LLM has no role in Layer 4.

## Layer 3: feature registry (`decisioning/features.py`)
* Each feature is one SQL expression in `FEATURES`. Training calls `compute_features(con, "p.is_resolved")`, live scoring calls it with
  `ptp_id = '...'`. There is no second implementation. Tests check batch == single-row for features and scores, and that the result does not
  depend on the thread count (the only difference is float summation order, about 2e-12 in `sal_avg_3m`).
* **Point-in-time rules, stated exactly:**
  * contact / payment history: day-level cut-offs strictly before the promise's creation date (promise and contact timestamps differ by up to ~13 h);
  * the customer's earlier promises and the case's prior-broken count: only promises RESOLVED (`status_ts`) before the creation date;
  * snapshots, bureau pulls, salary and transaction months: the last period that ended before the creation date;
  * **exception - text features** read the agent note written at the promise's own contact. That note is saved a median of ~8 minutes (up to 13 hours)
    AFTER the promise timestamp, so it is learned just after creation, not before. About 0.8% of such notes were edited after the outcome was
    recorded, so the text may then reflect the outcome. This is a known, small gap, not a claim of strict point-in-time for text.
* `prior_broken_ptp_count` is now computed from history. The stored `promises_to_pay.prior_broken_ptp_count` holds final outcomes: for repeat
  promises created since June 2026, 908 of 21,701 (4.2%) carried a count larger than what was known at creation. The stored column is forbidden in the registry.
* `t_pressure` now matches whole words. The old unanchored `sue` matched inside "issue" (6,316 notes): the old pattern flagged 10,546 notes, the whole-word pattern (English plus a few French legal terms) flags 4,063.
* **Excluded from the model** (still computed and audited): the four amount fields (see the finding below) and `txn_benefit_3m` (benefit deposits
  - EI, CPP/OAS, child benefit, disability - can reveal age, disability or family status). Tags: `model`, `gate_only`, `excluded`, `diagnostic`.
  `audit_registry()` fails if a protected attribute, proxy field or outcome column appears in a model feature.
* Bureau and transaction features reach customers through a bridge: the snapshot table carries both `account_id` and `crm_customer_id`, so
  CIF -> CRM goes through `deposit_accounts.deposit_account_id` (no name matching; CIF zero-padded to 10 digits).
  **Bridge accuracy is limited:** for promises that carry an account id it agrees with the promise's own CRM id only 90.7% of the time (145,845 of 160,880),
  and 328,520 accounts (about 20%) map to more than one CRM id (mostly joint holders; only about 5% of such pairs are flagged duplicates).
  When a CIF maps to several CRM ids the lowest id is used, and when two CIFs of one CRM id both have a bureau pull on the same date the lowest CIF wins
  (this replaced an arbitrary pick). About 45% of promises have bureau / transaction coverage.
  This bridge is separate from Layer 1's identity matching: the model's bureau and transaction features still use it, while the identity gate and Decision Record use the golden C360 (`docs/layer1_identity_c360.md`). Moving these features onto the C360 links is not done.

## IMPORTANT FINDING: the promised-amount fields look generated after the outcome
Adding `ptp_to_overdue_ratio` and `ptp_amount` to the production feature set lifts held-out AUC from 0.6445 to 0.963 (an amount-only model was not tested), but they are excluded from the production model:
* Promises with ratio = 1.0 (the full overdue amount) are 38% of resolved promises and break 2.2% of the time; partial-amount promises break 44-85%.
* Among promises created since 1 Sep: 25% of those that turned out kept have ratio 1.0, 0.2% of those that turned out broken do, and
  only **0.3% of the 33,005 open promises** do. If the ratio were a real creation-time signal, a large share of the open promises would be full-amount.
* Scored with an amount-trained model, 68% of the open queue lands in the top band (median risk 0.997). It does not transfer to promises whose outcome is not yet known.

Conclusion (an inference, not something the organisers state): the amount fields were set after the outcome in the synthetic generator.
The amount-trained model is kept only as a labelled reference (`models/ptp_break_v1_reference_amounts.*`, `reference_only: true`) and is never loaded by the decision code.

## Model results (held-out window: promises due after 2026-08-15, n = 36,349, break rate 59.2%)
Production model `models/ptp_break_v1.txt` (the only artifact `Decisioner` loads; 50 features, 456 trees; its SHA-256 is stored in every Decision Record).

| Model | Promises | Test AUC |
|---|---:|---:|
| customer features only | 36,349 | 0.620 |
| promise features only (no amounts) | 36,349 | 0.621 |
| promise + customer + contact | 36,349 | 0.636 |
| all production features, no text | 36,349 | 0.638 |
| **all production features (v1, shipped)** | 36,349 | **0.6445** |
| REFERENCE only: + amount features (not used) | 36,349 | 0.963 |

**Like-for-like with the incumbent `ptp_break v2.1`** (which has scores for only 16,531 of those promises): incumbent **0.712** vs shipped model **0.670** on those same promises.
The shipped model does not beat the incumbent. The incumbent's AUC is the same whether it was scored before or after the due date (0.713 vs 0.711), so there is no sign of
leakage in it; its reason fields are internally inconsistent, which only suggests (does not prove) that its score is synthetic.

Before / after the correctness fixes in this pass: AUC 0.6443 -> 0.6445, calibration error (ECE) 0.0432 -> 0.0406, Brier 0.2256 -> 0.2254, features 51 -> 50.
Fixing the point-in-time leak, the regex and the bureau join, and removing the proxy feature, cost no accuracy.

**Calibration** (isotonic, fitted on the validation window 2026-07-01 to 2026-08-15; held-out window):

| Band (thresholds are an assumption) | n | mean predicted | observed break rate |
|---|---:|---:|---:|
| low (< 0.50) | 8,261 | 0.354 | 0.379 |
| medium (0.50-0.65) | 7,506 | 0.588 | 0.579 |
| high (0.65-0.75) | 11,061 | 0.717 | 0.662 |
| very_high (>= 0.75) | 9,521 | 0.773 | 0.706 |

The top two bands over-predict by about 5-7 points, and the isotonic calibrator's last steps (0.872, then 0.999) rest on very few validation promises, which is
why scores above 0.90 are treated as a low-confidence tail (see review triggers). Hypothesis H1 (promise beats customer) is not supported once the amount fields are removed:
promise-only and customer-only are equal (0.62); together 0.64; text adds about +0.006.
Split: train due <= 2026-06-30, validation (2026-06-30, 2026-08-15] for early stopping and calibration, test after. The break rate moves from 33% (to April) to 59% (August-September).
The risk-band thresholds were chosen after looking at observed break rates on this test window, so the band table above is not an independent confirmation.

## Scoring point (T-3 / T-1): what changed from Round 1 and why
* Neither organiser document mentions T-3 or T-1. They come from our own Round 1 design (summary, hypothesis H2, Promise Ledger). The build instructions allow design changes if the README says what changed and why.
* **What the code does:** the model scores once, from features known at promise creation. `recommend(as_of=...)` re-runs the gates at the as-of date (payments received, consent, case stage,
  contact budget), but the risk itself is the same for any as-of date. T-3 and T-1 scoring are NOT implemented.
* **What the data allows:** T-1 is possible for 100% of promises and T-3 for 92.8% (`days_to_due_at_creation` is 1-21). The 17,267 promises created only 1-2 days before due are 7.2%, not a reason to drop the idea.
* **This is an engineering simplification, not leakage mitigation.** Using events dated up to T-1 is legitimate; leakage comes from outcome / status fields or events after the scoring point. Creation-time
  scoring throws away the most decision-relevant information: 61.1% of kept promises are already paid by T-1, and promises still unpaid at T-1 break 63.3% of the time (42.5% overall).
* **Consequence:** the "already paid" gate relies on `payment_received` events in the contact history, which appear for only about 31% of kept promises between creation and T-1 (against 61% actually paid).
  The system can therefore still recommend a reminder to a customer who has already paid. H2 ("acting at T-3/T-1 helps") is untested.

## Layer 4: gates (`gates.py`), ranking (`actions.py`), orchestrator (`orchestrator.py`)
Gates run in a fixed order and can only restrict the allowed actions or demand human review.

| Gate | Rule | Source |
|---|---|---|
| protected / proxy block | feature audit must be clean | POL-COLL-004 §4.6 |
| hard stop | insolvency / consumer proposal, deceased, cease request: no contact | POL-COLL-001 §5.4, §7.3 |
| representative | counsellor / lawyer / POA: no direct contact | POL-COLL-001 §5.4 |
| **case stage** | case placed with an agency, in legal handling (status, `legal_referral_flag` or a `legal_status`), charged off, settled, or otherwise not an active in-house case: no direct contact | **engineering rule: no provided document states it** |
| open dispute | no payment-request actions | PRC-COLL-012 §6.1 |
| identity & data quality | missing or duplicate-flagged customer record, **golden C360 trust score below 0.70** (`config.IDENTITY_TRUST_REVIEW`, an assumption), over half the features missing -> human review (flag only) | Layer 1, `docs/layer1_identity_c360.md` |
| hardship (route-only) | see statuses below | POL-COLL-004 §4.2, §4.4 |
| vulnerability | safeguard: certified agent; never a risk input | POL-COLL-004 §4.5 |
| consent | no servicing consent for a channel removes it | POL-COLL-001 §5.3 |
| contact budget | at most 3 outbound attempts (calls and payment-asking reminders) in a rolling 7 days | POL-COLL-001 §5.2 |
| payment received | promise already being honoured: no action | |

Evidence for the case-stage rule: among the 33,005 open promises, 331 are on `placed_agency` cases, 544 on `legal` cases and 332 more carry a legal flag / legal status. Before this rule the engine recommended calls on some of them.

**Hardship statuses** (`gates.hardship_status`): `none`; `possible` = an unconfirmed signal only (a model-sourced flag, or hardship wording in the note with no recorded flag) -> a human confirms first;
`recorded` = the case flag was set by a person (`agent_coded` or `customer_form`; 15,750 of the 16,050 hardship-flagged open promises) -> offer an eligible option before asking for payment (§4.2);
`confirmed` = a reviewer confirmed it in this system (needs a reviewer id; also unlocks specialist-level programmes). If an accepted plan is in force (`hardship_program_accepted` and the plan has not ended; 6,143 open promises have an accepted plan, 5,822 of them still running) the specialist queue owns
contact (§4.4) and the recommendation is no action. Hardship can only soften treatment, never add contact. "Recorded" is our term: it is not Round 1's "confirmed".

**Review triggers (v2).** `review_required` is true only for triggers of kind *review*; vulnerable-customer routing is kind *routing* (the action already hands the case to a person).
* uncertain identity: customer missing, flagged duplicate / deleted, or C360 trust score below 0.70 (269 of 32,106 open promises, 0.8%; risk and action are unchanged, only a review is added); possible hardship: status `possible` only;
* low model confidence: 30% or more of model features missing, or risk above 0.90 (the calibration tail) - both thresholds are assumptions; closeness to a band edge no longer counts (35% of scores sit within 0.02 of an edge, so it carried no information) and is reported as `band_stability`;
* policy conflict: high risk, no hard stop, yet every contact the band calls for is blocked by consent, budget or dispute;
* high-impact action: the lead hardship option needs approval above agent level; insufficient data quality: over half the features missing; fairness / proxy: feature audit failed; conflicting agent outputs: not evaluated (single recommender).
* **Measured on 400 random open promises: review required on 14% (55), down from 87% under v1** (a test asserts < 25%). Action mix: no action 43%, specialist review 21%, offer hardship option 15%, SMS 10%, evening call 9%, email 3%, daytime call 0.5%.
  The share of "no action" is high because it includes hard stops, case-stage stops, accepted hardship plans and low-risk promises with a reminder already scheduled.

Timing: contact day = later of as-of and the day before due, moved past statutory holidays and a non-permitted Sunday (the case's `contact_rule_set` and the customer's own `sunday_contact_permitted`; the customer flag can only tighten it), inside 07:00-21:00.
Preferred windows: SMS/email 09-10h, daytime call 11-13h, evening call 17-19h (the right-party-contact rate peaks then, BQ-011 gold). On a permitted Sunday the window is fitted inside 13:00-17:00 (an evening call becomes 15:00-17:00; previously it came out as an empty 17:00-17:00).
Action set reuses the dataset's vocabulary (`sms_payment_link`, `call_daytime`, `call_evening`, `offer_hardship_plan`) plus `email_reminder`, `specialist_review`, `no_action`. Hardship programmes come from `hardship_programs` rows effective on the as-of date (product, max DPD, level checked; settlement excluded).
Rationales no longer say an option is "effective": the data has no uplift evidence, so the ranking only picks the lightest option the risk band calls for.

**Queue.** `upcoming()` scores every open promise due in the horizon and then ranks (it used to score only the first 2,000 by due date, so it never reached later due dates such as 30 Sep). 7,254 promises for 28 Sep - 1 Oct, about 4 s.

## Decision Records, reviews and overrides
* Each record stores: recommendation, ranked options, all gate results, reasons, a context note, policy citations (only gates that fired, plus consent / budget when a contact action is recommended), `created_at`, model name / version / **file SHA-256**,
  the **full feature vector** (model inputs, and separately the gate-only / excluded ones), the **context snapshot** the gates saw (consent, flags, case stage, rule set...), the thresholds and holiday calendar used (with their assumption status), trigger version and git commit, identity status, and `parent_decision_id`.
  Records never contain a human decision: reviews are separate rows.
* **Append-only, tamper-evident.** The module has no update or delete. DuckDB cannot forbid them, so each record stores a payload hash and a chain hash; `store.verify()` reports edited, deleted or reordered records and `store.get()` refuses a record that no longer matches its hash. Legacy rows from before the chain are left untouched and reported as `legacy_unhashed`.
* **Reviews.** Every attempt is logged in `review_attempts`, including refused ones. Before accepting or overriding, the gates are re-evaluated with current data, so an override must be inside the allowed set both when the record was written and now (a cease request filed since then blocks it). Accepting a recommendation that is no longer permitted is refused.
  A hardship-confirmation child record and its review are written in one transaction, or not at all. Only reviewer / specialist / team lead can confirm hardship; an agent cannot raise contact intensity above the recommendation. `store.current_state()` returns the latest review's outcome ("pending" if none).
* **Identity of the reviewer is not authenticated here.** Role and reviewer id are whatever the caller passes, unless a `role_directory` is supplied, in which case the id must exist with that role. `identity_verified` records which case applied. The API layer must supply authenticated identities.

## Fairness / proxy check (`docs/fairness_check.md`)
37 groups, 2 flagged, both on protected attributes used for the audit only: marital status "separated" and vulnerability = true (the newcomer group, flagged before this pass, no longer is; I have not shown that removing `txn_benefit_3m` is the cause).
No proxy field (province, language, urban/rural) was flagged; postal area (FSA) and branch are not tested. The model uses none of these fields; the gaps come from correlated legitimate features (vulnerable customers have higher DPD and a higher observed break rate, 67%).
Vulnerable customers are routed to certified agents by the gate, not scored harder. The audit measures calibration gap and high-band share on held-out resolved promises with current-state customer attributes; it does not measure action mix after the gates or error rates. Thresholds (0.05 gap, n >= 500) are assumptions.
`cust_tenure_months` (3.7% of model gain) could stand in for newcomer status; it is still in the model.

## Assumptions to review (not stated in the provided materials)
| Item | What the code does | Evidence |
|---|---|---|
| Risk bands | low < 0.50 <= medium < 0.65 <= high < 0.75 <= very_high (`config.BANDS`; recorded in every Decision Record) | No document gives thresholds (Round 1: "set by policy, TBC"). The dataset's own `score_band` uses 0.25 / 0.50 / 0.75 for every model, and the incumbent's `threshold_used` is 0.6, but the incumbent's actions do not follow it (12,304 actioned rows above 0.6, 27,241 below): conventions, not policy. |
| Statutory holidays | 30 Sep (named as an example in POL-COLL-001 §5.1) plus 7 Sep, 12 Oct, 11 Nov, 25 Dec 2026 (assumed); used only to move the recommended contact day | The data's `stat_holiday_flag` (national, no outbound contact on any flagged day) shows 30 Sep for 2023-2025 and does NOT flag 11 Nov; it also flags dates this list omits (1 Jan, Family Day, Good Friday, Victoria Day, Canada Day, 3 Aug, Boxing Day). **Not changed in this pass.** |
| Case-stage rule | no direct contact on agency / legal / charged-off / settled / inactive cases | engineering rule, no document |
| `recorded` hardship | a person-set flag is treated as established for routing | POL-COLL-004 §4.2 says the agent records it; the term and treatment are ours |
| Low-confidence trigger | 30% features missing or risk > 0.90 | assumptions, chosen from observed rates |
| Call times | evening 17-19h, SMS/email 09-10h, daytime 11-13h | evening from BQ-011; the rest is a design choice |
| Open dispute | `dispute_flag` and `dispute_status = 'open'` | |
| Low band | no action only when a reminder is already scheduled; the low band still breaks about 38% of the time | |

## Remaining risks and known limits
* AUC 0.6445: use the score to rank and pick light-touch actions, not to claim strong prediction. It is below the incumbent's 0.712 on the same promises.
* The bureau / transaction bridge is ambiguous (above); `cust_tenure_months` may proxy for newcomer status.
* `internal_do_not_call_flag` is ignored (about 470 open promises); its meaning is not defined in the documents. The national DNCL flag is correctly ignored (POL-COLL-001 §5.3).
* The early-payment gate sees only about half of early payers (above). `recommend()` also accepts resolved and cancelled promises.
* The evidence note is the note written at the promise's contact; it is not selected to support the three reasons, and notes can contain remarks about protected grounds or vulnerability that POL-COLL-004 §4.6 says should not be written down. "What would change this" is fixed templates, not counterfactual model runs.
* "Accounts are at X% of limit/original amount" is a mixed measure over all products, not card utilisation.
* Calibration over-predicts the top bands by 5-7 points; scores above 0.90 are thin.
* Text features are regexes on one note; customers without a note have missing text features. Other regexes (for example `try to`, `paid on`) are unanchored and not audited.
* Identity: the gate uses the golden C360 trust score and the duplicate / deleted flags. The 0.70 threshold is an assumption. The record stores `customer_key`, `match_confidence`, `trust_score` and conflict flags; customers the C360 cannot resolve are treated as missing, not as low trust.
* The full test suite (152 tests, about 4 to 7 minutes, real data) no longer touches `outputs/benchmark_answers.csv` (the Layer 2 test writes to a temp file). Stop the API before running it: the API process holds the write lock on `warehouse/decisions.duckdb`.
