# CodeBlooded: from customer data to the right next action

Team CodeBlooded · CIBC Collections Hackathon, build phase · use case: **PTP-break prevention and next best action**
(every person, account and call here is synthetic Maple Bank data).

A customer says "I'll pay on Friday". This system unifies the fragmented customer data, answers plain-English questions about it
with evidence, scores whether the promise will break, and recommends the next action. A person decides whenever policy says so,
and every recommendation is stored as a tamper-evident Decision Record.

```
5 source systems ─► Layer 1  identity match + golden Customer 360 + data quality ─┐
                 ─► Layer 2  ask the data: SQL/sources shown, out-of-scope refused │
                 ─► Layer 3  one feature registry for training and live scoring    ├─► API ─► React UI
                 ─► Layer 4  risk model ─► policy gates ─► action ─► human review ─┘     (queue · customer · decision · ask · audit)
```

## What it does, and how well (measured, not hoped)

| Area | Result |
|---|---|
| Identity (Layer 1) | 1,020,000 CRM records resolve to 1,000,729 customers; 98.3% of 1,844,500 source accounts auto-linked, 3,547 withheld for review. **Precision 99.84%, recall 97.4%**, measured on the 283,851 accounts the collections system itself linked to a customer (contacted or promised on, not a random sample). 97.9% of declared CRM duplicates merged. |
| Data quality | [Report](outputs/DATA_QUALITY_REPORT.md): every number measured live; 8 of 8 error-severity checks pass. Findings include 156,837 deposit CIFs that lost leading zeros (20.1%), 4 snapshot date formats, a column renamed mid-release, 2.8% orphan customer references. Nothing dropped, nothing imputed. |
| Ask the data (Layer 2) | **21/21** dev benchmark questions pass our approximation of the scorer. All 50 questions (the 35 released plus the 15 extra ones released at 19:00) are answered or refused in [outputs/benchmark_answers.csv](outputs/benchmark_answers.csv) (9 refused), produced by one unedited run of the system. Test-split and extra-question gold answers are hidden, so the real score is unknown. |
| Model (Layer 4) | LightGBM, calibrated: held-out **AUC 0.6445**, calibration error 0.041. **It does not beat the incumbent** `ptp_break v2.1` (0.712 against our 0.670 on the same 16,531 promises). Use it to rank and choose light-touch actions, not as a strong predictor. |
| Review load | Human review required on about 14% of open promises (400 random). Low-trust identity adds 0.8% of the queue. |
| Tests | 152 tests pass (about 4 to 7 minutes on the real data). |

## Run it

**Prerequisites:** Windows or Linux, Python 3.14 (what we used), Node 20.19+ for the UI (we used 24), about 10 GB free disk.
Commands below are PowerShell, run from the repo root.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

**1. Data.** Not in the repo (about 13 GB, synthetic, CC-BY-4.0). Put the release at `data/maple_data/maple_collections_release/`:

```powershell
pip install -U huggingface_hub
hf download nuxsh/maple-collections-hackathon --repo-type dataset --local-dir data/maple_data
cd data/maple_data; unzip maple_collections_release.zip; cd ../..
```

**2. Warehouse and Layer 1** (DuckDB files are written under `warehouse/`, which is git-ignored):

```powershell
cd data_pipeline
python load_raw.py                  # typed raw_* tables (about 1.5 GB), IDs kept as text
python curate.py                    # cur_* views: the data traps handled once, in one place
cd ..
python -m data_pipeline.identity    # identity matching, about 90 s
python -m data_pipeline.c360        # golden Customer 360, about 2 min
python -m data_pipeline.identity_validate   # precision/recall -> outputs/IDENTITY_METRICS.md
python -m data_pipeline.data_contract       # -> contracts/data_contract.yaml
python -m data_pipeline.quality_report      # -> outputs/DATA_QUALITY_REPORT.md
```

**3. Layer 3 derived tables.** The trained model is committed (`models/ptp_break_v1.*`), so you do not need to retrain.
Build the derived tables it scores from (14 seconds; identical to ours up to float summation order):

```powershell
python -c "from decisioning import features as F; F.build_derived_tables(F.connect())"
```

(`python -m decisioning.train` retrains and **overwrites** the model; run it only to reproduce training. `python -m decisioning.fairness` needs the tables training writes.)

**4. API and UI.**

```powershell
cd frontend; npm install; npm run build; cd ..
python -m uvicorn app.main:app --port 8000
```

Open <http://localhost:8000>. For UI development run `npm run dev` in `frontend/` (port 5173, proxies `/api` to 8000).
The API process is the only writer of `warehouse/decisions.duckdb`: stop it before running the tests.

**5. Benchmark and tests.**

```powershell
python -m insight.benchmark --score                                  # writes outputs/benchmark_answers.csv, scores the dev split
python -m insight.benchmark --questions <released+extra questions.csv> --out outputs/benchmark_answers.csv   # all 50 in one run (same columns as benchmark_questions.csv)
python -m pytest tests -q
```

A walkthrough with real customers for each case (low risk, high risk, uncertain identity, hardship, policy stop) is in
[docs/demo_customers.md](docs/demo_customers.md).

## How it is built

| Layer | What | Where | Detail |
|---|---|---|---|
| 1 Data product | Raw to curated to golden C360, identity matching with per-field evidence and a match confidence, shared-phone protection, conflict-preserving attributes, trust score, data contract, quality report | `data_pipeline/` | [docs/layer1_identity_c360.md](docs/layer1_identity_c360.md), [contract](contracts/data_contract.yaml) |
| 2 Insight | Certified-metric SQL templates, a sqlglot guard (single SELECT, allow-listed relations, no protected or PII columns), BM25 policy retrieval with citations, refusal rules | `insight/` | [docs/insight_layer.md](docs/insight_layer.md) |
| 3 Features | One registry of SQL feature definitions used for both training and live scoring, point-in-time rules, text features from agent notes, audit that blocks protected attributes and outcome columns | `decisioning/features.py` | [docs/decisioning.md](docs/decisioning.md) |
| 4 Decisioning | Calibrated model, ordered policy gates, action ranking, review triggers, plain-English explanation, hash-chained Decision Records, human accept / change / escalate with gates re-checked at review time | `decisioning/` | [docs/decisioning.md](docs/decisioning.md), [fairness](docs/fairness_check.md) |
| API and UI | FastAPI over the engines (nothing in it decides anything); React UI | `app/`, `frontend/` | [docs/architecture.md](docs/architecture.md) |

Architecture diagram: [docs/architecture.png](docs/architecture.png) (PDF: [docs/architecture.pdf](docs/architecture.pdf)).

Design rules that hold throughout: the model supplies one number; everything that decides what may happen to a customer is rule-based and logged;
hardship can only soften treatment; protected attributes are excluded from the C360, the model and the gates' risk logic (audited);
"no action" is a valid answer; a person can review anything.

## What changed from our Round 1 design, and why

| Round 1 design | What we built | Why |
|---|---|---|
| Scores every promise at **T-3 and T-1** | Scores **once, from features known at promise creation**. Gates re-run at the as-of date. | Engineering simplification under time pressure, **not** leakage avoidance. It discards useful information: 61% of kept promises are paid by T-1, and the "already paid" gate sees only about half of early payers. T-1 is possible for all promises and T-3 for 92.8%. H2 ("acting at T-3/T-1 helps") is untested. |
| LLM text-to-SQL, then rules decide if it may run | Certified SQL templates first, a validated guard on every statement, an LLM planner only as an optional fallback (**off** in our benchmark run) | Auditable and deterministic, needs no key, and the 21/21 dev result does not depend on a model. |
| pgvector RAG over notes, transcripts and policy | BM25 over policy text, preferring current versions, with an evidence threshold and citations | No vector database needed for 19 policy documents; "no evidence, no answer" is enforced. |
| Postgres feature store with nightly batch-vs-online parity job | One SQL registry used by both paths; **tests** check batch equals single-row | Same guarantee, no job to run. Nothing runs nightly. |
| Data Trust Score from match confidence, open conflicts **and freshness** | Match confidence minus conflict penalty minus shared-mobile penalty. **No freshness term.** | Freshness was not defined in the data. |
| Hardship: none / possible / confirmed | Adds **recorded** (a person already set the flag) | 15,750 of 16,050 hardship-flagged open promises were recorded by a person; routing them as "possible" would send almost all to review. |
| Eight human-review triggers | Seven evaluated; "conflicting agent outputs" is **not evaluated** (single recommender) | There is only one recommender. The fairness trigger is the feature audit. |
| Promise Ledger and Context Card as objects | **Not built as such.** The Decision Record stores the full feature vector and context snapshot; the Customer 360 page shows identity, accounts, promises and decisions. | Time. |
| Whisper speech-to-text (stretch) | **Not built.** Voice files were not used; the 25,000 text transcripts are summarised by rules. | Stretch goal. |
| Learning loop with shadow retrain | **Not built** | Out of scope for the build window. |
| RBAC and masked PII | Insight guard denies PII and protected columns; reviewer identities come from a **fixed demo directory** in `app/main.py` | Not real authentication. |
| LLM "agents" per layer | Deterministic modules and one fixed-sequence orchestrator | Keeps every decision replayable. |

**Hypotheses.** H1 (the promise beats the customer) is not supported once the leaky amount fields are removed: promise-only and customer-only are both 0.62 AUC.
H3 and H4 (Context Card, contact budget) are untested.

## AI tools used, and for what

* **Claude Code (Anthropic Claude models)**: wrote and refactored most of the code (pipeline, Insight engine, feature registry, gates, API, UI), the tests and these docs,
  and did the exploratory data analysis that found the traps and the leakage below. All changes were reviewed by the team and verified by running them.
* **No generative AI runs at decision time.** The model is LightGBM (classical ML). The optional LLM planner (`ANTHROPIC_API_KEY`) is wired in but was off for every number reported here.
* **Reference implementation.** Identity matching, the C360 build, identity validation, the data-contract generator, the API structure and the frontend scaffolding were adapted
  from a reference implementation (`CIBC_Hackathon_CodeBlooded-main`), re-pointed at our warehouse and re-measured on our data. We did **not** use its model, benchmark or decisioning code.
  The adapted modules say so in their headers.

## Findings worth knowing

* **The promised-amount fields look generated after the outcome.** Adding `ptp_amount` and the overdue ratio to the model lifts held-out AUC from 0.645 to 0.963 (we did not test them alone), but full-amount promises are 38% of resolved promises and only 0.3% of the 33,005 open ones. They are excluded from the production model and kept only as a labelled reference (`models/ptp_break_v1_reference_amounts.*`, never loaded by the decision code). [Details](docs/decisioning.md).
* Leaks found and fixed: the stored `prior_broken_ptp_count` carried final outcomes for 4.2% of repeat promises; a regex matched "sue" inside "issue"; a bureau join picked rows arbitrarily; `upcoming()` scored only the first 2,000 promises.

## Limitations and assumptions (please read)

* **The model is weak** (AUC 0.6445) and below the incumbent. The top two bands over-predict by 5 to 7 points; scores above 0.90 rest on few validation promises.
* **Assumptions not stated in any provided document:** risk bands 0.50 / 0.65 / 0.75, the 0.70 identity-trust review threshold, the case-stage rule (no direct contact on agency, legal, charged-off or settled cases), four of the five holiday dates (30 Sep is documented), and the low-confidence triggers. All are recorded in each Decision Record or in [docs/decisioning.md](docs/decisioning.md).
* **Identity precision is measured only on contacted or promised accounts.** Accounts outside that set have no ground truth. 28,481 accounts stay unmatched: 19,788 have no date of birth to block on, the rest had no candidate customer.
* **Benchmark:** BQ-035 and BQ-017 follow the organisers' reference SQL by default (`insight/config.py: PREFER_CORRECTED = False`) although it hides a data trap; the corrected answer is one flag away and noted on the answer. BQ-020 is refused for lack of evidence and BQ-025 uses an assumed column. Running the 15 extra questions exposed real gaps (a filter silently dropped, a different metric answered, protected-ground questions answered); we fixed the engine, added regression tests, and re-ran everything unedited. The extra-question definitions we chose (for example overdue = `total_overdue` on open cases, and SMS payment-link clicks counted only on SMS that carry a link) are assumptions: see [docs/insight_layer.md](docs/insight_layer.md).
* **Not authenticated:** reviewer roles come from a demo directory. Do not read the audit trail as proof of who clicked.
* `internal_do_not_call_flag` is not enforced (about 470 open promises; its meaning is not defined), and the national DNCL flag is correctly ignored for collection calls (POL-COLL-001 §5.3).
* The evidence note shown with a decision is the note written at the promise's contact; it is not selected to support the three reasons and can contain remarks about protected grounds.

## Repository map

```
app/                 FastAPI service (serves the built UI too)          frontend/   React + Vite UI (npm run build -> dist/)
data_pipeline/       load_raw, curate, identity, c360, validation,      decisioning/  features, train, model, gates, actions, orchestrator, records, fairness
                     data_contract, quality_report                      insight/      Layer 2 engine
contracts/           data_contract.yaml (golden C360)                   models/       production model (+ labelled reference model)
docs/                architecture, layer docs, demo walkthrough         outputs/      benchmark answers, data quality + identity reports
tests/               152 tests                                          warehouse/    DuckDB files (git-ignored, rebuilt by the steps above)
```

## Submission checklist

| Item | File |
|---|---|
| Code repository | this repo |
| Architecture diagram | `docs/architecture.png`, `docs/architecture.pdf` |
| Data contract | `contracts/data_contract.yaml` |
| Data quality report | `outputs/DATA_QUALITY_REPORT.md` (identity detail: `outputs/IDENTITY_METRICS.md`) |
| Benchmark answers | `outputs/benchmark_answers.csv` (columns `question_id, answer, sql_or_sources, refused`) |
| Demo video, 10-slide deck | submitted through the form (not in the repo) |
