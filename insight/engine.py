"""Insight engine: one entry point, `Engine.ask(question, role)`.

Routing order (first that applies wins):
  1. refusal rules          -> refused (protected attribute, proxy, PII export, off-topic)
  2. transcript summary     -> 'transcript' (a case id + a call/summary request)
  3. template SQL planner   -> 'sql' (or 'mixed' when the plan also cites documents)
  4. policy retrieval       -> 'policy' (needs enough evidence, else falls through)
  5. LLM SQL planner        -> 'sql' (only if an LLM is configured; always guard-validated)
  6. refuse                 -> 'refused' (no evidence, no answer)

Every answer carries what it used: SQL, or document+section, or transcript id.
"""
from __future__ import annotations

import json
import re
from datetime import date

import duckdb

from . import config, executor, refusal, templates
from .llm import LLM
from .llm_planner import LLMSqlPlanner
from .policy import PolicyIndex
from .templates import Ctx
from .transcripts import CASE_RE, RuleSummarizer, Summarizer, latest_for_case_sql, load_transcript
from .types import Answer, Plan


MIXED_PLANS = {"explain_change", "dispute_comparison", "pilot_vulnerable_certified"}   # data + document evidence


class Engine:
    def __init__(self, con: duckdb.DuckDBPyConnection | None = None, llm: LLM | None = None,
                 summarizer: Summarizer | None = None, as_of: date = config.AS_OF) -> None:
        self.errors: list[str] = []          # swallowed planner/SQL errors, for debugging and tests
        self.con = con or executor.connect()
        self.ctx = Ctx(self.con, as_of=as_of)
        self.policy = PolicyIndex(self.con)
        self.summarizer = summarizer or RuleSummarizer()
        self.llm_planner = LLMSqlPlanner(llm) if llm is not None and getattr(llm, "available", False) else None

    # ------------------------------------------------------------------ public
    def ask(self, question: str, role: str = "analyst") -> Answer:
        q = " ".join(question.split())
        r = refusal.check(q)
        if r:
            return Answer(q, r.reason, "", True, "refused", refusal_reason=r.code,
                          understood={"refusal_rule": r.code})
        if CASE_RE.search(q) and re.search(r"call|transcript|conversation|recorded|summari[sz]e", q, re.I):
            return self._transcript(q)

        recognised = templates.recognise(q)
        plan = None
        try:
            plan = templates.plan(q, self.ctx)
        except Exception as e:  # a planner bug must never take the demo down
            self.errors.append(f"template planner: {e}")
        if plan:
            ans = self._run_plan(q, plan, "sql")
            if ans:
                return ans
        if recognised is None:
            pol = self._policy(q)
            if pol:
                return pol
        if self.llm_planner:
            lp = self.llm_planner.plan(q, self.ctx)
            if lp:
                ans = self._run_plan(q, lp, "sql")
                if ans:
                    return ans
        if recognised is not None:
            return Answer(q, "I recognised this as a data question but cannot answer this breakdown or filter reliably "
                             "with the certified metrics.", "", True, "refused", refusal_reason="unsupported_breakdown",
                          understood={"recognised_metric_family": recognised})
        return Answer(q, "I could not find enough evidence in the collections data or policy documents to answer this.",
                      "", True, "refused", refusal_reason="insufficient_evidence")

    # ------------------------------------------------------------------ routes
    def _run_plan(self, q: str, plan: Plan, route: str) -> Answer | None:
        try:
            cols, rows = executor.run(self.con, plan.sql)
        except Exception as e:  # guard or SQL error: record it, let the router fall through (never crash the demo)
            self.errors.append(f"{plan.name}: {e}")
            return None
        table = executor.to_csv(cols, rows)
        text = table
        if plan.summarize:
            extra = plan.summarize(cols, rows)
            if extra:
                text = f"{table}\n\n{extra}"
        evidence, src_ids = [], []
        for src in plan.sources:
            doc, _, sec = src.partition(" ")
            for c in (self.policy.get(doc, sec or None) or self.policy.get(doc))[:1]:
                evidence.append({"doc": c.doc_id, "section": c.section, "version": c.version, "text": c.text[:400]})
                src_ids.append(c.cite)
        sql_or_sources = plan.sql if not src_ids else plan.sql + "\n-- documents: " + json.dumps(src_ids, ensure_ascii=False)
        return Answer(q, text, sql_or_sources, False, "mixed" if plan.name in MIXED_PLANS else route, plan=plan.name,
                      understood=plan.understood, evidence=evidence, notes=plan.notes,
                      rows=[dict(zip(cols, r)) for r in rows])

    def _policy(self, q: str) -> Answer | None:
        res = self.policy.answer(q)
        if not res:
            return None
        text, hits, cites, notes = res
        top = hits[0].chunk
        by_cite = {h.chunk.cite: h for h in hits}
        evidence = [{"doc": by_cite[c].chunk.doc_id, "section": by_cite[c].chunk.section, "version": by_cite[c].chunk.version,
                     "text": by_cite[c].chunk.text[:600], "score": round(by_cite[c].score, 2)} for c in cites]
        return Answer(q, text, json.dumps(cites, ensure_ascii=False), False, "policy", plan="policy_retrieval",
                      understood={"retrieved": top.cite, "version": top.version}, evidence=evidence, notes=notes)

    def _transcript(self, q: str) -> Answer:
        case_id = CASE_RE.search(q).group(0)
        row = self.con.execute(latest_for_case_sql(), [case_id]).fetchone()
        sql = latest_for_case_sql().replace("?", f"'{case_id}'")
        if not row:
            return Answer(q, f"No recorded call transcript exists for case {case_id}.", sql, True, "refused",
                          refusal_reason="insufficient_evidence", understood={"case_id": case_id})
        transcript_id, _, call_ts, file_path = row
        ex = self.summarizer.summarize(load_transcript(file_path))
        evidence = [{"transcript_id": transcript_id, "speaker": e["speaker"], "start_sec": e["start_sec"], "text": e["text"]}
                    for e in ex.evidence[:6]]
        return Answer(q, ex.summary(), json.dumps([f"transcript {transcript_id}"]) + "\n-- " + sql, False, "transcript",
                      plan="transcript_summary",
                      understood={"case_id": case_id, "transcript_id": str(transcript_id), "call_start": str(call_ts),
                                  "hardship_reason": ex.reason_code, "next_step": ex.next_step},
                      evidence=evidence,
                      notes=["Rule-based extraction, evaluated against 500 gold-labelled transcripts; "
                             "possible hardship is never confirmed without a human."])
