"""HTTP API for the demo UI. Thin layer over the existing engines: nothing here makes a decision.

    uvicorn app.main:app --port 8000

  GET  /api/health
  GET  /api/customers?q=            search the golden C360 (name, customer key or CRM id)
  GET  /api/customers/{id}          Customer 360: identity evidence, accounts, promises, past decisions
  GET  /api/promises/upcoming       open promises due soon, scored in full, riskiest first
  POST /api/recommend               score -> gates -> ranking -> explanation -> Decision Record
  GET  /api/decisions               recent Decision Records
  GET  /api/decisions/{id}          record + review state + review attempts
  POST /api/decisions/{id}/review   human accept / change / escalate (gates re-checked)
  GET  /api/audit/verify            recompute the hash chain
  POST /api/ask                     plain-English question over the data: answer + SQL/sources, or a refusal

Structure (strict request schemas, structured errors, request-id middleware) follows the reference implementation in
CIBC_Hackathon_CodeBlooded-main/apps/backend/api; the handlers wrap OUR Decisioner and Insight Engine.
"""
from __future__ import annotations

import dataclasses
import os
import threading
import uuid
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Literal

import duckdb
from fastapi import FastAPI, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from data_pipeline.config import MAIN_DB, WAREHOUSE
from decisioning.orchestrator import Decisioner
from decisioning.records import TamperError

C360_DB = WAREHOUSE / "c360.duckdb"
request_id: ContextVar[str] = ContextVar("request_id", default="-")

# Demo reviewer directory: with it the Decisioner verifies that a reviewer id really holds the role claimed.
DEMO_ROLES = {"AGT-01": "agent", "REV-01": "reviewer", "SPC-01": "specialist", "TL-01": "team_lead"}

_lock = threading.RLock()          # DuckDB connections are not safe for concurrent use; the demo serialises engine calls
_state: dict[str, Any] = {}


def decisioner() -> Decisioner:
    with _lock:
        if "dec" not in _state:
            _state["dec"] = Decisioner(role_directory=DEMO_ROLES)
        return _state["dec"]


def engine():
    with _lock:
        if "eng" not in _state:
            from insight.engine import Engine
            _state["eng"] = Engine()
        return _state["eng"]


def _c360() -> duckdb.DuckDBPyConnection:
    with _lock:
        if "c360" not in _state:
            con = duckdb.connect(":memory:")
            con.execute(f"ATTACH '{C360_DB.as_posix()}' AS c (READ_ONLY)")
            con.execute(f"ATTACH '{MAIN_DB.as_posix()}' AS m (READ_ONLY)")
            _state["c360"] = con
        return _state["c360"]


def q(sql: str, params: list | None = None) -> list[dict]:
    with _lock:
        cur = _c360().execute(sql, params or [])
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


# ------------------------------------------------------------------ errors
class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details


def _err(status: int, code: str, message: str, details: Any = None) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message, "request_id": request_id.get(), "details": details}})


app = FastAPI(title="CodeBlooded Collections API", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:8000"],
                   allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def _request_id(req: Request, call_next):
    rid = req.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    token = request_id.set(rid)
    try:
        resp = await call_next(req)
    finally:
        request_id.reset(token)
    resp.headers["x-request-id"] = rid
    return resp


@app.exception_handler(ApiError)
async def _api(_: Request, exc: ApiError):
    return _err(exc.status, exc.code, exc.message, exc.details)


@app.exception_handler(RequestValidationError)
async def _validation(_: Request, exc: RequestValidationError):
    return _err(422, "validation_error", "request validation failed",
                [{"field": ".".join(str(x) for x in e["loc"][1:]) or str(e["loc"][0]), "problem": e["msg"]} for e in exc.errors()])


@app.exception_handler(StarletteHTTPException)
async def _http(_: Request, exc: StarletteHTTPException):
    return _err(exc.status_code, {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error"), str(exc.detail))


@app.exception_handler(Exception)
async def _unhandled(_: Request, exc: Exception):
    return _err(500, "internal_error", f"{type(exc).__name__}: {exc}")


# ------------------------------------------------------------------ schemas (strict: unknown fields are rejected)
class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RecommendRequest(_Strict):
    ptp_id: str = Field(pattern=r"^PTP-\d+$")


class ReviewRequest(_Strict):
    reviewer_id: str = Field(min_length=2, max_length=20)
    role: Literal["agent", "reviewer", "specialist", "team_lead"]
    decision: Literal["accept", "change", "escalate"]
    reason: str = Field(min_length=3, max_length=1000)
    final_action: str | None = None
    confirm_hardship: bool = False


class AskRequest(_Strict):
    question: str = Field(min_length=3, max_length=500)


def J(x: Any) -> Any:
    return jsonable_encoder(x)


# ------------------------------------------------------------------ routes
@app.get("/api/health")
def health():
    ok = C360_DB.exists() and MAIN_DB.exists()
    out: dict[str, Any] = {"status": "ok" if ok else "unhealthy", "c360": C360_DB.exists(), "maple": MAIN_DB.exists(),
                           "demo_reviewers": DEMO_ROLES, "llm_enabled": bool(os.environ.get("ANTHROPIC_API_KEY"))}
    try:
        out["decision_chain"] = decisioner().store.verify()
    except Exception as e:  # noqa: BLE001
        out["decision_chain"] = {"error": str(e)}
    return {**out, "request_id": request_id.get()}


@app.get("/api/customers")
def search(term: str = Query(alias="q", min_length=2, max_length=60), limit: int = Query(20, ge=1, le=50)):
    term = term.strip()
    like = f"%{term.lower()}%"
    rows = q("""select customer_key, first_name, last_name, date_of_birth, province_code, segment, customer_status, trust_score,
                       n_active_cases, n_ptp_open, identity_review_required
                from c.c360
                where customer_key = upper(?) or list_contains(member_crm_ids, upper(?))
                   or lower(last_name || ' ' || first_name) like ? or lower(first_name || ' ' || last_name) like ?
                order by (customer_key = upper(?)) desc, n_ptp_open desc, last_name limit ?""",
             [term, term, like, like, term, limit])
    return J({"query": term, "count": len(rows), "results": rows, "request_id": request_id.get()})


def _resolve(cid: str) -> str:
    r = q("select customer_key from c.id_crm_clusters where crm_customer_id = ?", [cid.upper()])
    if r:
        return r[0]["customer_key"]
    raise ApiError(404, "customer_not_found", f"no customer matches '{cid}'")


@app.get("/api/customers/{customer_id}")
def customer(customer_id: str):
    key = _resolve(customer_id)
    row = q("select * from c.c360 where customer_key = ?", [key])[0]
    members = row["member_crm_ids"]
    links = q("""select source_system, account_id, link_status, tier, match_confidence, match_method, match_evidence,
                        shared_mobile, conflict_flags, bureau_supported
                 from c.id_identity_links where customer_key = ? order by source_system, account_id limit 50""", [key])
    attrs = q("select field, value, source, confidence, conflict_status from c.c360_attributes where customer_key = ? order by field, source", [key])
    promises = q("""select ptp_id, case_id, ptp_status, ptp_created_ts, ptp_due_date, ptp_amount, ptp_channel
                    from m.cur_promises where list_contains(?, crm_customer_id) order by (ptp_status = 'open') desc, ptp_due_date desc limit 15""", [members])
    ids = {p["ptp_id"] for p in promises}
    with _lock:
        decisions = [d for d in decisioner().store.list_records(500) if d.get("ptp_id") in ids]
    return J({"customer_key": key, "queried_id": customer_id, "c360": row, "linked_accounts": links, "attributes": attrs,
              "promises": promises, "decisions": decisions, "request_id": request_id.get()})


@app.get("/api/promises/upcoming")
def upcoming(days: int = Query(3, ge=0, le=14), limit: int = Query(25, ge=1, le=200)):
    with _lock:
        rows = decisioner().upcoming(days=days, limit=limit)
    return J({"as_of": "2026-09-28", "days": days, "count": len(rows), "promises": rows, "request_id": request_id.get()})


@app.post("/api/recommend", status_code=201)
def recommend(req: RecommendRequest):
    with _lock:
        try:
            rec = decisioner().recommend(req.ptp_id)
        except KeyError as e:
            raise ApiError(404, "promise_not_found", str(e)) from e
        state = decisioner().store.current_state(rec["decision_id"])
    return J({"record": rec, "state": state, "request_id": request_id.get()})


@app.get("/api/decisions")
def decisions(limit: int = Query(30, ge=1, le=200)):
    with _lock:
        rows = decisioner().store.list_records(limit)
    return J({"count": len(rows), "decisions": rows, "request_id": request_id.get()})


@app.get("/api/decisions/{decision_id}")
def decision(decision_id: str):
    with _lock:
        st = decisioner().store
        try:
            rec = st.get(decision_id)
        except KeyError:
            raise ApiError(404, "decision_not_found", f"no decision record {decision_id}") from None
        except TamperError as e:
            raise ApiError(409, "record_tampered", str(e)) from e
        return J({"record": rec, "state": st.current_state(decision_id), "reviews": st.reviews(decision_id),
                  "attempts": st.attempts(decision_id), "request_id": request_id.get()})


@app.post("/api/decisions/{decision_id}/review")
def review(decision_id: str, req: ReviewRequest):
    with _lock:
        try:
            out = decisioner().review(decision_id, req.reviewer_id, req.role, req.decision, req.reason, req.final_action, req.confirm_hardship)
        except KeyError:
            raise ApiError(404, "decision_not_found", f"no decision record {decision_id}") from None
        except PermissionError as e:
            raise ApiError(403, "review_refused", str(e)) from e
        except ValueError as e:
            raise ApiError(422, "invalid_review", str(e)) from e
        return J({"review": out, "state": decisioner().store.current_state(decision_id), "request_id": request_id.get()})


@app.get("/api/audit/verify")
def verify():
    with _lock:
        return J({**decisioner().store.verify(), "request_id": request_id.get()})


@app.post("/api/ask")
def ask(req: AskRequest):
    with _lock:
        ans = engine().ask(req.question)
    d = dataclasses.asdict(ans) if dataclasses.is_dataclass(ans) else dict(ans)
    return J({**d, "request_id": request_id.get()})


# Built React UI (frontend/dist) is served from the same origin when present.
_UI = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if _UI.exists():
    app.mount("/", StaticFiles(directory=_UI, html=True), name="ui")
