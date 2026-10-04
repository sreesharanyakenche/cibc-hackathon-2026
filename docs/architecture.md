# Architecture

Diagram: [architecture.png](architecture.png) · [architecture.pdf](architecture.pdf) (the PDF is built from the same image).
Layer details: [layer1_identity_c360.md](layer1_identity_c360.md), [insight_layer.md](insight_layer.md), [decisioning.md](decisioning.md).

## Components and data stores

| Store (git-ignored, rebuilt from the data) | Written by | Read by |
|---|---|---|
| `warehouse/maple.duckdb`: typed `raw_*` tables, `cur_*` views | `data_pipeline/load_raw.py`, `curate.py` | everything (read-only) |
| `warehouse/c360.duckdb`: `id_*` identity tables, `c360`, `c360_attributes` | `data_pipeline/identity.py`, `c360.py` | decisioning (identity gate), API (customer search and 360) |
| `warehouse/features.duckdb`: derived feature tables | `decisioning/features.py` (`build_derived_tables`), training | decisioning |
| `warehouse/decisions.duckdb`: Decision Records, reviews, review attempts | `decisioning/records.py` | decisioning, API |
| `models/ptp_break_v1.*` (committed) | `decisioning/train.py` | `decisioning/model.py` |

DuckDB allows one writer per file. `maple.duckdb`, `c360.duckdb` and `features.duckdb` are opened read-only at run time; only the API process (or a test) writes `decisions.duckdb`.

## Flow 1: recommend (`POST /api/recommend {ptp_id}`)
1. `features.compute_features` evaluates the registry for that promise (the same SQL used in training).
2. The model returns a calibrated break probability and the three largest contributions.
3. The orchestrator builds a context from the case, the CRM record and the golden C360 (`identity_for`: customer key, match confidence, trust score, conflict flags).
4. Gates run in a fixed order and can only restrict allowed actions or demand review; the action ranking picks the lightest allowed option for the risk band (or no action); review triggers are evaluated.
5. The explanation (three reasons, call-note evidence, what would change this, policy citations) and a contact window are added.
6. A Decision Record is written: feature vector, gate inputs, model file hash, thresholds, holiday calendar, code commit, identity block. Payload hash and chain hash make it tamper-evident.

No LLM takes part. Nothing contacts a customer: the output goes to a person.

## Flow 2: human review (`POST /api/decisions/{id}/review`)
The stored record is hash-verified, then the gates are **re-evaluated with current data**. *Accept* is refused if the recommendation is no longer permitted; *change* must be inside the allowed set both at record time and now,
and an agent cannot raise contact intensity; *escalate* routes to a specialist. *Confirm hardship* (reviewer, specialist or team lead only) computes a new, softer record and writes it with the review in one transaction.
Every attempt, including refused ones, is logged in `review_attempts`. Reviews never edit the record.

## Flow 3: ask the data (`POST /api/ask {question}`)
Refusal rules → transcript summary → certified SQL template → policy retrieval → optional LLM planner (off) → refuse. Every SQL statement passes the sqlglot guard and runs read-only with a row cap. The response carries the answer, the SQL or sources, the route, and the refusal reason if any.

## API surface (`app/main.py`)

| Method and path | Purpose |
|---|---|
| `GET /api/health` | component status, demo reviewer ids, decision-chain check |
| `GET /api/customers?q=` | search the golden C360 by name, customer key or CRM id |
| `GET /api/customers/{id}` | Customer 360: identity evidence, linked accounts, attribute conflicts, promises, earlier decisions |
| `GET /api/promises/upcoming?days=&limit=` | open promises due soon, scored in full, riskiest first (about 2.4 s for 3 days) |
| `POST /api/recommend` | flow 1 |
| `GET /api/decisions`, `GET /api/decisions/{id}` | records; one record with review state, reviews and attempts |
| `POST /api/decisions/{id}/review` | flow 2 |
| `GET /api/audit/verify` | recompute the hash chain |
| `POST /api/ask` | flow 3 |

Request bodies are strict: unknown fields are rejected (so a caller cannot submit a risk score or threshold). Errors are `{"error": {code, message, request_id, details}}`.

## Operational notes and limits
* The API serialises engine calls behind one lock because DuckDB connections are not safe for concurrent use. Fine for a demo, not for production load.
* Reviewer identities come from a fixed directory in `app/main.py` (`AGT-01`, `REV-01`, `SPC-01`, `TL-01`). It binds an id to a role but is **not authentication**; there is no login, no RBAC and no PII masking in the API.
* The UI is served by the API from `frontend/dist` once built (`npm run build`); `npm run dev` proxies `/api` to port 8000.
* The model scores at promise creation; T-3 and T-1 scoring from the Round 1 design is not implemented (see the README and `decisioning.md`).
