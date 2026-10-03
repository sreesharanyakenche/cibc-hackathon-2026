# Layer 2: Insight engine (template path + LLM plug-in)

## Run
```
.venv\Scripts\python -m insight.benchmark --score          # writes outputs/benchmark_answers.csv, scores the dev split
.venv\Scripts\python -m insight.benchmark --questions <csv> --out outputs/benchmark_answers_extra.csv   # 19:00 extras
.venv\Scripts\python -m pytest tests -q
```
```python
from insight.engine import Engine
a = Engine().ask("What is the average days past due of open cases by queue?")
a.answer, a.sql_or_sources, a.refused, a.route, a.understood, a.evidence, a.notes
```

## Flow (first rule that applies wins)
1. `refusal.py`: protected attribute, proxy variable (FSA), PII export, off-topic -> refused, with a named rule and reason.
2. `transcripts.py`: a case id plus a call/summary request -> latest transcript for the case -> rule-based summary + transcript id.
3. `templates.py`: certified-metric SQL templates (promise kept/broken rate, avg DPD, open cases, hardship rate, cure rate,
   RPC rate, out-of-hours calls, cost per contact/RPC, delinquency, roll rate, write-offs, collected amount, dispute comparison,
   explain-a-drop, champion/challenger, vulnerable-certified pilot). Unknown breakdowns return no plan (never a silent guess).
4. `policy.py`: BM25 over section/bullet chunks of the policy documents, superseded versions excluded, authority-weighted
   (policy > procedure > terms > scripts > FAQ), evidence gate, extractive answer + citations. A FAQ that gives a different
   figure from the policy is reported in `notes` and not cited.
5. `llm_planner.py`: only if an LLM is configured.
6. Otherwise refuse ("no evidence, no answer").

Every SQL statement, template or LLM-drafted, goes through `guard.validate` (single SELECT, allow-listed relations, no table
functions, no star, no PII/protected/free-text columns) and runs read-only with a row cap.

## Plugging in the LLM (no code change in the engine)
```
set ANTHROPIC_API_KEY=...            # optional: set INSIGHT_LLM_MODEL
```
```python
from insight.llm import get_llm
Engine(llm=get_llm())
```
* Used only as a fallback when no template matches. The template path never calls it (tested).
* Prompt = allow-listed schema (denied columns hidden) + certified metric definitions + the data-trap rules.
* Draft -> guard -> `EXPLAIN` -> one repair attempt with the error -> otherwise refuse.
* Other plug-in points: `Summarizer` protocol in `transcripts.py` (LLM transcript summary, same `Extraction` fields) and
  `PolicyIndex.answer` (an LLM may rephrase the extract, but the citation list comes from retrieval).

## Dev benchmark result
21 of 21 dev questions pass against `labels/benchmark_dev_answers.csv` (`tests/test_insight.py::test_dev_benchmark_all_pass`).
The scorer in `insight/benchmark.py` is an approximation of the organisers' (numeric tolerance from `benchmark_questions.tolerance`,
refusal flag, cited document, key numbers). Test-split answers are hidden, so the real score is unknown.

## Decisions and assumptions to review
| # | Item | Current behaviour | Switch / note |
|---|---|---|---|
| 1 | BQ-035 / any "by bucket" | Raw `current_bucket` (matches reference answer). 3,807 of 96,000 open cases disagree with `current_dpd`. | `insight/config.py: PREFER_CORRECTED = True` groups by the recomputed bucket. Every answer carries a note. |
| 2 | BQ-017 roll rate | Parses 3 month-end formats like the reference SQL; MM/DD/YYYY rows dropped. | Same flag; corrected gives auto_loan 8.2% (466 accounts) vs gold 7.4% (430). |
| 3 | BQ-025 attempts per open case | Uses `collections_cases.contact_attempts_total` (1.1 to 27 by bucket). `contact_history` has about 1 outbound row per open case, so it under-counts. | ASSUMPTION; test-split gold unknown. |
| 4 | BQ-020 "Can we sue a customer..." | Refused: no supporting passage in the documents (evidence gate). | If gold expects an answer, lower the gate or add a legal-advice policy answer. |
| 5 | BQ-014 pilot | Certified agents' 30-day cure rate on randomised hardship cases is lower (4.9% vs 7.8%). Direction is reported as computed. | "30-day cure" = `cure_flag and days_to_cure <= 30`. |
| 6 | BQ-008 cure rate | Cohort of cases opened in the period, cured within 30 days (28.5% for July 2026). | Differs from the certified metric wording (cases open at period start); question wording followed. |
| 7 | Contact windows | Clamped to the snapshot date (exclusive), matching the reference SQL for BQ-011. | |

## Known limits
* Question understanding is rule-based: unusual phrasings fall to refusal unless an LLM is configured.
* Policy retrieval can return a related-but-not-exact passage for vague questions (for example "minimum hours between contacts").
* Transcript extraction vs 500 gold labels: promises 93%, next step 81%, hardship yes/no 84%, hardship reason 63% (rules, not a model).
* French questions are not supported beyond the Quebec annex lookup.
* PII masking beyond "never select it" and role-based access are the API layer's job (not built yet).
