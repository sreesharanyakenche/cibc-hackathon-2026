"""Decision Records and human reviews: append-only, hash-chained and verifiable.

  decision_records   one immutable row per recommendation (full JSON payload + payload hash + chain hash)
  decision_reviews   one immutable row per SUCCESSFUL human review (accept / change / escalate)
  review_attempts    one row per review ATTEMPT, including refused ones (who tried what, and why it was refused)

Append-only in practice: this module has no UPDATE or DELETE. DuckDB cannot forbid them at the database level, so tampering
is made DETECTABLE instead: every record stores the SHA-256 of its payload and a chain hash (previous chain hash + payload
hash). `verify()` recomputes both and checks the sequence is contiguous, so an edited payload, a removed row or a reordered
row is reported. A hardship confirmation creates a NEW linked record (parent_decision_id); the original is never changed.

Legacy rows written before the hash chain existed are left untouched and reported as `legacy_unhashed`.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import uuid
from datetime import datetime

import duckdb

from . import config

GENESIS = "GENESIS"

DDL = [
    """create table if not exists decision_records (
         decision_id varchar primary key, created_at timestamp, ptp_id varchar, case_id varchar, crm_customer_id varchar,
         parent_decision_id varchar, model_name varchar, model_version varchar, feature_version varchar, as_of date,
         risk double, band varchar, hardship_status varchar, recommended_action varchar,
         review_required boolean, payload varchar)""",
    """create table if not exists decision_reviews (
         review_id varchar primary key, decision_id varchar, reviewed_at timestamp, reviewer_id varchar, role varchar,
         decision varchar, final_action varchar, reason varchar, hardship_confirmed boolean, child_decision_id varchar)""",
    """create table if not exists review_attempts (
         attempt_id varchar primary key, attempted_at timestamp, decision_id varchar, reviewer_id varchar, role varchar,
         requested_decision varchar, requested_final_action varchar, confirm_hardship boolean, outcome varchar, detail varchar)""",
]
# columns added by the hardening pass; added in place so an existing database keeps working
MIGRATIONS = [
    "alter table decision_records add column if not exists seq bigint",
    "alter table decision_records add column if not exists payload_hash varchar",
    "alter table decision_records add column if not exists chain_hash varchar",
    "alter table decision_reviews add column if not exists identity_verified boolean",
    "alter table decision_reviews add column if not exists recommended_action varchar",
    "alter table decision_reviews add column if not exists allowed_now varchar",
]


class TamperError(RuntimeError):
    pass


def canonical(rec: dict) -> str:
    return json.dumps(rec, sort_keys=True, default=str, ensure_ascii=False)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class RecordStore:
    def __init__(self, path=config.DECISIONS_DB) -> None:
        config.WAREHOUSE.mkdir(exist_ok=True)
        self.con = duckdb.connect(str(path))
        for ddl in DDL:
            self.con.execute(ddl)
        for m in MIGRATIONS:
            self.con.execute(m)
        self._in_tx = False

    @staticmethod
    def new_id(prefix: str) -> str:
        return f"{prefix}-{datetime.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:6]}"

    # ------------------------------------------------------------------ transactions
    @contextlib.contextmanager
    def transaction(self):
        """All-or-nothing group of writes (for example a child record plus the review that created it)."""
        if self._in_tx:                       # already inside one: join it
            yield
            return
        self.con.execute("begin")
        self._in_tx = True
        try:
            yield
        except BaseException:
            self.con.execute("rollback")
            raise
        else:
            self.con.execute("commit")
        finally:
            self._in_tx = False

    # ------------------------------------------------------------------ records
    def _tail(self) -> tuple[int, str]:
        row = self.con.execute("select seq, chain_hash from decision_records where seq is not null order by seq desc limit 1").fetchone()
        return (row[0], row[1]) if row else (0, GENESIS)

    def save(self, rec: dict) -> str:
        with self.transaction():
            seq, prev = self._tail()
            payload = canonical(rec)
            ph = sha(payload)
            self.con.execute(
                "insert into decision_records (decision_id, created_at, ptp_id, case_id, crm_customer_id, parent_decision_id, "
                "model_name, model_version, feature_version, as_of, risk, band, hardship_status, recommended_action, "
                "review_required, payload, seq, payload_hash, chain_hash) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [rec["decision_id"], datetime.fromisoformat(rec["created_at"]), rec["ptp_id"], rec["case_id"], rec["crm_customer_id"],
                 rec.get("parent_decision_id"), rec["model"]["name"], rec["model"]["version"], rec["model"]["feature_version"],
                 rec["as_of"], rec["risk"], rec["band"], rec["hardship_status"], rec["recommended_action"],
                 rec["review_required"], payload, seq + 1, ph, sha(prev + ph)])
        return rec["decision_id"]

    def get(self, decision_id: str, verify: bool = True) -> dict:
        row = self.con.execute("select payload, payload_hash from decision_records where decision_id = ?", [decision_id]).fetchone()
        if not row:
            raise KeyError(decision_id)
        payload, ph = row
        if verify and ph is not None and sha(payload) != ph:
            raise TamperError(f"decision record {decision_id} does not match its stored hash")
        return json.loads(payload)

    def verify(self) -> dict:
        """Recompute payload hashes and the chain; report edited, missing or reordered records."""
        rows = self.con.execute("select decision_id, seq, payload, payload_hash, chain_hash from decision_records "
                                "where seq is not null order by seq").fetchall()
        bad, prev, expect_seq = [], GENESIS, rows[0][1] if rows else 1
        for did, seq, payload, ph, ch in rows:
            ok = sha(payload) == ph and sha(prev + ph) == ch and seq == expect_seq
            if not ok:
                bad.append(did)
            prev, expect_seq = ch, seq + 1
        legacy = self.con.execute("select count(*) from decision_records where seq is null").fetchone()[0]
        return {"checked": len(rows), "ok": not bad, "bad": bad, "legacy_unhashed": legacy}

    def list_records(self, limit: int = 50) -> list[dict]:
        cur = self.con.execute(
            "select decision_id, created_at, ptp_id, band, risk, recommended_action, hardship_status, review_required, "
            "parent_decision_id from decision_records order by created_at desc limit ?", [limit])
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    # ------------------------------------------------------------------ reviews
    def add_review(self, decision_id: str, reviewer_id: str, role: str, decision: str, final_action: str, reason: str,
                   hardship_confirmed: bool = False, child_decision_id: str | None = None, identity_verified: bool = False,
                   recommended_action: str | None = None, allowed_now: list[str] | None = None) -> str:
        rid = self.new_id("RV")
        self.con.execute(
            "insert into decision_reviews (review_id, decision_id, reviewed_at, reviewer_id, role, decision, final_action, reason, "
            "hardship_confirmed, child_decision_id, identity_verified, recommended_action, allowed_now) values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [rid, decision_id, datetime.now(), reviewer_id, role, decision, final_action, reason, hardship_confirmed, child_decision_id,
             identity_verified, recommended_action, json.dumps(sorted(allowed_now or []))])
        return rid

    def log_attempt(self, decision_id: str, reviewer_id: str, role: str, requested_decision: str,
                    requested_final_action: str | None, confirm_hardship: bool, outcome: str, detail: str = "") -> str:
        aid = self.new_id("AT")
        self.con.execute("insert into review_attempts values (?,?,?,?,?,?,?,?,?,?)",
                         [aid, datetime.now(), decision_id, reviewer_id, role, requested_decision, requested_final_action,
                          confirm_hardship, outcome, detail[:500]])
        return aid

    def reviews(self, decision_id: str) -> list[dict]:
        cur = self.con.execute("select * from decision_reviews where decision_id = ? order by reviewed_at", [decision_id])
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def attempts(self, decision_id: str | None = None) -> list[dict]:
        sql = "select * from review_attempts" + (" where decision_id = ?" if decision_id else "") + " order by attempted_at"
        cur = self.con.execute(sql, [decision_id] if decision_id else [])
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def current_state(self, decision_id: str) -> dict:
        """Latest successful review wins; 'pending' when nobody has reviewed it yet."""
        rec = self.get(decision_id)
        revs = self.reviews(decision_id)
        if not revs:
            return {"decision_id": decision_id, "status": "pending", "final_action": None,
                    "recommended_action": rec["recommended_action"], "review_required": rec["review_required"]}
        last = revs[-1]
        return {"decision_id": decision_id, "status": last["decision"], "final_action": last["final_action"],
                "recommended_action": rec["recommended_action"], "review_required": rec["review_required"],
                "reviewer_id": last["reviewer_id"], "role": last["role"], "reviewed_at": last["reviewed_at"],
                "child_decision_id": last["child_decision_id"], "n_reviews": len(revs)}
