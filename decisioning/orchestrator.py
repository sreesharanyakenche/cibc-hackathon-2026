"""Policy-controlled orchestrator for PTP-break prevention and next best action.

    d = Decisioner()
    rec = d.recommend("PTP-0123456")                        # score -> gates -> ranking -> explanation -> Decision Record
    d.review(rec["decision_id"], "REV-17", "reviewer", "accept", reason="agreed")
    d.review(rec["decision_id"], "REV-17", "reviewer", "accept", reason="customer told us", confirm_hardship=True)
                                                           # creates a NEW record (softer treatment), keeps both

A fixed, replayable sequence (no LLM planner): features -> model risk -> deterministic gates -> action ranking ->
review triggers -> explanation -> immutable Decision Record. The model supplies one number; everything that decides
what may happen to a customer is rule-based and logged. Nothing here contacts a customer: output goes to a human.

Human review rules (see `review`):
  * every attempt is logged, including refused ones (review_attempts);
  * gates are RE-EVALUATED at review time, so an override cannot use a stale allowed-action list;
  * an override must be inside the allowed set both when the record was written and now;
  * the stored record is hash-verified before it is used;
  * a hardship-confirmation child record and its review are written together or not at all;
  * only reviewer / specialist / team_lead can confirm hardship; an agent cannot raise contact intensity above the recommendation;
  * the role is whatever the caller passes UNLESS a role directory is supplied (then reviewer id and role must match it).
    The API layer is responsible for authenticated identity; `identity_verified` records which case applied.
"""
from __future__ import annotations

import dataclasses
import json
import re
import subprocess
import threading
from datetime import date, datetime, timedelta

import duckdb

from . import actions, config, features as F, gates
from .gates import ALL_ACTIONS, CONTACT_ACTIONS, Context, contact_slot
from .model import PtpModel
from .records import RecordStore

PTP_ID_RE = re.compile(r"^PTP-\d+$")
REVIEW_ROLES = {"agent", "reviewer", "specialist", "team_lead"}
HARDSHIP_CONFIRM_ROLES = {"reviewer", "specialist", "team_lead"}      # an agent alone cannot confirm hardship
LEVEL = {"none": 0, "possible": 1, "clear": 2}
ACTION_LABEL = {
    "sms_payment_link": "Send an SMS reminder with a payment link", "email_reminder": "Send an email reminder",
    "call_daytime": "Call during the day", "call_evening": "Call in the evening",
    "offer_hardship_plan": "Offer an eligible hardship option", "specialist_review": "Hand to a specialist / certified agent",
    "no_action": "No action (restraint)",
}


def mask(text: str | None) -> str:
    """Light PII masking for evidence snippets shown to users."""
    if not text:
        return ""
    t = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "[email]", text)
    return re.sub(r"\+?\d[\d\-\s().]{7,}\d", "[number]", t)


def _git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=5,
                              cwd=config.ROOT).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _jsonable(v):
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if isinstance(v, float) and v != v:
        return None
    return v


class Decisioner:
    def __init__(self, thresholds: tuple[float, float, float] = config.BANDS, store: RecordStore | None = None,
                 role_directory: dict[str, str] | None = None) -> None:
        self.con = F.connect(read_only=True)
        self.model = PtpModel("v1")
        self.store = store or RecordStore()
        self._c360 = duckdb.connect(str(config.C360_DB), read_only=True) if config.C360_DB.exists() else None
        self._c360_lock = threading.Lock()
        self.thresholds = thresholds
        self.role_directory = role_directory
        self.code_commit = _git_commit()
        self.rule_sets = {code: json.loads(attrs) for code, attrs in self.con.execute(
            "select code, attributes_json from maple.main.raw_code_lookups where code_type = 'contact_rule_set'").fetchall()}

    # ------------------------------------------------------------------ public API
    def upcoming(self, as_of: date = config.AS_OF, days: int = 3, limit: int | None = 25) -> list[dict]:
        """Open promises due within `days` of as_of, SCORED IN FULL, riskiest first (the agent queue).

        The horizon is scored completely and then ranked; `limit` only truncates the finished ranking (None = everything).
        Ties on risk break by due date then promise id, so the order is stable.
        """
        end = as_of + timedelta(days=days)
        feats = F.compute_features(
            self.con, f"p.ptp_status = 'open' and p.ptp_due_date between date '{as_of.isoformat()}' and date '{end.isoformat()}'")
        if feats.is_empty():
            return []
        risk = self.model.score(feats)
        out = [{"ptp_id": p, "due": str(d), "risk": round(float(r), 4), "band": actions.band_for(float(r), self.thresholds)}
               for p, d, r in zip(feats["ptp_id"], feats["ptp_due_date"], risk)]
        out.sort(key=lambda x: (-x["risk"], x["due"], x["ptp_id"]))
        return out if limit is None else out[:limit]

    def recommend(self, ptp_id: str, as_of: date = config.AS_OF, hardship_confirmed: bool = False,
                  confirmed_by: str | None = None, parent_decision_id: str | None = None, save: bool = True) -> dict:
        if not PTP_ID_RE.match(ptp_id):
            raise ValueError("invalid ptp_id")
        if hardship_confirmed and not confirmed_by:
            raise ValueError("hardship_status = confirmed requires a human reviewer id")
        feats = F.compute_features(self.con, f"p.ptp_id = '{ptp_id}'")
        if feats.is_empty():
            raise KeyError(f"promise {ptp_id} not found")
        risk = float(self.model.score(feats)[0])
        reasons = self.model.reasons(feats, k=3)[0]
        row = feats.to_dicts()[0]
        raw = self._context_rows(ptp_id, as_of)
        ident = self.identity_for(raw["crm_customer_id"])
        ctx = self._build_context(row, raw, as_of, risk, hardship_confirmed, ident)
        results = gates.run_gates(ctx)
        allowed = gates.allowed_actions(results)
        choice = actions.choose(ctx, results)
        triggers = actions.review_triggers(ctx, results, choice, self.thresholds)
        review_reasons = [t["trigger"] for t in triggers if t["fired"] and t.get("kind", "review") == "review"]
        routing = [t["trigger"] for t in triggers if t["fired"] and t.get("kind") == "routing"]
        slot = contact_slot(ctx, choice.action)
        hs = gates.hardship_status(ctx)
        rec = {
            "decision_id": RecordStore.new_id("DR"), "created_at": datetime.now().isoformat(timespec="seconds"),
            "parent_decision_id": parent_decision_id,
            "ptp_id": ptp_id, "case_id": raw["case_id"], "crm_customer_id": raw["crm_customer_id"], "as_of": str(as_of),
            "model": {"name": config.MODEL_NAME, "version": self.model.version, "feature_version": config.FEATURE_VERSION,
                      "file_sha256": self.model.sha256, "uses_protected_attributes": False},
            "policy_config": {
                "risk_band_thresholds": list(self.thresholds),
                "risk_band_thresholds_status": "assumption: the provided documents give no thresholds (see docs/decisioning.md)",
                "holiday_calendar": {d.isoformat(): n for d, n in sorted(gates.HOLIDAYS.items())},
                "holiday_calendar_status": "30 Sep is named in POL-COLL-001 §5.1; the other dates are assumptions",
                "review_trigger_version": actions.TRIGGER_VERSION, "code_commit": self.code_commit},
            "risk": round(risk, 4), "band": ctx.band,
            "risk_note": "Calibrated probability that the promise breaks, from a model that does not use promised-amount "
                         "features (see docs/decisioning.md: those fields look generated after the outcome).",
            "identity": {"customer_found": ctx.customer_found, "flagged_duplicate_or_deleted": ctx.duplicate_record,
                         "customer_key": ident["customer_key"] if ident else None,
                         "match_confidence": ident["match_confidence"] if ident else None,
                         "trust_score": ident["trust_score"] if ident else None,
                         "conflict_flags": ident["conflict_flags"] if ident else [],
                         "shared_mobile_blocked": ident["shared_mobile_blocked"] if ident else None,
                         "trust_review_threshold": config.IDENTITY_TRUST_REVIEW,
                         "note": "Golden C360 identity (data_pipeline.c360); trust below the threshold routes to human review."
                                 if ident else "Golden C360 not available for this customer; only the duplicate / deleted flags are checked."},
            "hardship_status": hs, "hardship_confirmed_by": confirmed_by,
            "gates": [g.as_dict() for g in results], "allowed_actions": sorted(allowed),
            "ranking": choice.ranked, "recommended_action": choice.action, "action_label": ACTION_LABEL[choice.action],
            "rationale": choice.rationale, "contact_slot": slot,
            "eligible_programmes": ctx.eligible_programmes[:4] if hs in ("recorded", "confirmed") else [],
            "review_triggers": triggers, "review_required": bool(review_reasons), "review_reasons": review_reasons,
            "routing_notes": routing,
            "explanation": self._explain(row, raw, ctx, choice, reasons, results, slot),
            "inputs": {"promise": {k: raw[k] for k in ("ptp_amount", "overdue_at_creation", "dpd_at_creation", "product")},
                       "due_date": str(ctx.due_date), "contacts_7d": ctx.contacts_7d, "cap_7d": ctx.cap_7d},
            "feature_vector": {
                "model": {n: _jsonable(row.get(n)) for n in F.MODEL_FEATURE_NAMES},
                "gate_only_and_excluded": {f.name: _jsonable(row.get(f.name)) for f in F.FEATURES if f.purpose != "model"}},
            "context_snapshot": {k: _jsonable(v) for k, v in dataclasses.asdict(ctx).items()},
        }
        if save:
            self.store.save(rec)
        return rec

    # ------------------------------------------------------------------ human review
    def review(self, decision_id: str, reviewer_id: str, role: str, decision: str, reason: str,
               final_action: str | None = None, confirm_hardship: bool = False) -> dict:
        """Record a human decision. Every attempt is logged; a refused attempt raises after being logged."""
        try:
            out = self._review(decision_id, reviewer_id, role, decision, reason, final_action, confirm_hardship)
        except Exception as e:
            self.store.log_attempt(decision_id, str(reviewer_id), str(role), str(decision), final_action, confirm_hardship,
                                   "refused", f"{type(e).__name__}: {e}")
            raise
        self.store.log_attempt(decision_id, reviewer_id, role, decision, final_action, confirm_hardship, "recorded", out["review_id"])
        return out

    def _review(self, decision_id, reviewer_id, role, decision, reason, final_action, confirm_hardship) -> dict:
        if role not in REVIEW_ROLES:
            raise ValueError(f"role must be one of {sorted(REVIEW_ROLES)}")
        if decision not in ("accept", "change", "escalate"):
            raise ValueError("decision must be accept, change or escalate")
        if not reviewer_id or not str(reason).strip():
            raise ValueError("reviewer id and a reason are required")
        verified = False
        if self.role_directory is not None:
            if self.role_directory.get(reviewer_id) != role:
                raise PermissionError(f"reviewer '{reviewer_id}' is not registered with role '{role}'")
            verified = True
        if confirm_hardship and role not in HARDSHIP_CONFIRM_ROLES:
            raise PermissionError("only a reviewer, specialist or team lead can confirm hardship")

        rec = self.store.get(decision_id)                       # hash-verified
        as_of = date.fromisoformat(rec["as_of"])
        if confirm_hardship:
            # computed but NOT saved yet: it is persisted together with the review, or not at all
            target = self.recommend(rec["ptp_id"], as_of=as_of, hardship_confirmed=True, confirmed_by=reviewer_id,
                                    parent_decision_id=decision_id, save=False)
            recommended = target["recommended_action"]
        else:
            target = self.recommend(rec["ptp_id"], as_of=as_of, hardship_confirmed=rec["hardship_status"] == "confirmed",
                                    confirmed_by=rec.get("hardship_confirmed_by"), save=False)
            recommended = rec["recommended_action"]
        allowed_now = set(target["allowed_actions"])
        allowed = allowed_now if confirm_hardship else allowed_now & set(rec["allowed_actions"])

        if decision == "accept":
            final = recommended
            if final not in allowed_now:
                raise PermissionError(f"the recommendation '{final}' is no longer permitted: the policy gates have changed since it was made; "
                                      "ask for a new recommendation")
        elif decision == "escalate":
            final = "specialist_review"
        else:
            final = final_action
            if final not in ALL_ACTIONS:
                raise ValueError(f"final_action must be one of {ALL_ACTIONS}")
            if final not in allowed:
                blockers = [g["gate"] for g in target["gates"] if g["allowed"] is not None and final not in g["allowed"]]
                raise PermissionError(f"override refused: '{final}' is blocked by policy gate(s): {', '.join(blockers) or 'a gate at record time'}")
            if role == "agent" and actions.INTENSITY[final] > actions.INTENSITY[recommended]:
                raise PermissionError("an agent cannot raise contact intensity above the recommendation; ask a reviewer or team lead")

        with self.store.transaction():
            child_id = None
            if confirm_hardship:
                child_id = self.store.save(target)
            rid = self.store.add_review(decision_id, reviewer_id, role, decision, final, reason, confirm_hardship, child_id,
                                        identity_verified=verified, recommended_action=rec["recommended_action"],
                                        allowed_now=sorted(allowed_now))
        return {"review_id": rid, "decision_id": decision_id, "final_action": final, "child_decision_id": child_id,
                "recommended_action": rec["recommended_action"], "overridden": final != rec["recommended_action"],
                "identity_verified": verified}

    # ------------------------------------------------------------------ internals
    def _context_rows(self, ptp_id: str, as_of: date) -> dict:
        cur = self.con.execute("""
            select p.ptp_id, p.case_id, p.crm_customer_id, p.product, p.dpd_at_creation, p.ptp_due_date, p.ptp_created_ts,
                   p.reminder_scheduled_flag, p.ptp_amount, p.overdue_at_creation, p.contact_id,
                   k.insolvency_hold_flag, k.deceased_hold_flag, k.cease_contact_flag, k.third_party_rep_flag, k.dispute_flag,
                   k.dispute_status, k.hardship_flag, k.hardship_source, k.hardship_program_accepted, k.hardship_plan_end,
                   k.vulnerable_customer_flag, k.contact_rule_set, k.contact_cap_7d,
                   k.case_status, k.agency_placement_flag, k.legal_referral_flag, k.legal_status, k.chargeoff_flag,
                   u.crm_customer_id as u_id, u.consent_call, u.consent_sms, u.consent_email, u.preferred_language,
                   u.sunday_contact_permitted,
                   u.bankruptcy_flag, u.consumer_proposal_flag, u.deceased_flag, u.cease_communication_flag,
                   u.vulnerability_flag, u.is_deleted, u.duplicate_of_crm_id, u.credit_counselling_agency_flag,
                   u.legal_representative_flag, u.power_of_attorney_flag
            from cur_promises p join cur_cases k on k.case_id = p.case_id
            left join raw_customers u on u.crm_customer_id = p.crm_customer_id where p.ptp_id = ?""", [ptp_id])
        cols = [d[0] for d in cur.description]
        r = dict(zip(cols, cur.fetchone()))
        r["contacts_7d"] = self.con.execute(
            "select count(*) from cur_contacts where case_id = ? and direction = 'outbound' and not is_system_event "
            "and channel_eff in ('call','sms','email') and contact_date_local > ? - 7 and contact_date_local <= ?",
            [r["case_id"], as_of, as_of]).fetchone()[0]
        r["paid_since_creation"] = self.con.execute(
            "select count(*) from cur_contacts where case_id = ? and system_event_type = 'payment_received' "
            "and contact_date_local >= cast(? as date) and contact_date_local <= ?",
            [r["case_id"], r["ptp_created_ts"], as_of]).fetchone()[0] > 0
        r["note"] = self.con.execute(
            "select note_id, note_ts, note_text from cur_notes where contact_id = ? limit 1", [r["contact_id"]]).fetchone() if r["contact_id"] else None
        return r

    def _programmes(self, product: str | None, dpd: int | None, as_of: date, level: str) -> list[dict]:
        rows = self.con.execute(
            "select program_id, program_name, approver_level, duration_months, payment_reduction_pct, max_dpd, product_types, "
            "min_hardship_level, requires_income_evidence from maple.main.raw_hardship_programs "
            "where effective_from <= ? and effective_to >= ? and program_id <> 'HP-SETTLE'", [as_of, as_of]).fetchall()
        out = []
        for pid, name, appr, dur, red, maxdpd, ptypes, minlvl, evidence in rows:
            types = json.loads(ptypes)
            if product not in (None, "multi") and product not in types:
                continue
            if dpd is not None and maxdpd is not None and dpd > maxdpd:
                continue
            if LEVEL[minlvl] > LEVEL[level]:
                continue
            out.append({"program_id": pid, "name": name, "approver": appr, "duration_months": dur,
                        "payment_reduction_pct": red, "requires_income_evidence": evidence})
        out.sort(key=lambda p: (p["approver"] != "agent", p["program_id"]))
        return out

    def identity_for(self, crm_customer_id: str | None) -> dict | None:
        """Golden C360 identity summary for a CRM id (resolves merged duplicates), or None if C360 is unavailable."""
        if self._c360 is None or not crm_customer_id:
            return None
        with self._c360_lock:
            r = self._c360.execute(
                "select c.customer_key, c.match_confidence, c.trust_score, c.identity_basis, c.shared_mobile_blocked, c.conflict_flags, "
                "c.n_crm_records, c.n_linked_accounts from id_crm_clusters k join c360 c using (customer_key) "
                "where k.crm_customer_id = ?", [crm_customer_id]).fetchone()
        if r is None:
            return None
        return {"customer_key": r[0], "match_confidence": float(r[1]), "trust_score": float(r[2]), "identity_basis": r[3],
                "shared_mobile_blocked": r[4], "conflict_flags": list(r[5] or []), "n_crm_records": r[6], "n_linked_accounts": r[7]}

    def _build_context(self, row: dict, raw: dict, as_of: date, risk: float, hardship_confirmed: bool, ident: dict | None = None) -> Context:
        miss = sum(1 for n in F.MODEL_FEATURE_NAMES if row.get(n) is None) / len(F.MODEL_FEATURE_NAMES)
        rule = self.rule_sets.get(raw["contact_rule_set"] or "", {})
        found = raw["u_id"] is not None
        sunday = rule.get("sunday", "13:00-17:00")
        if raw["sunday_contact_permitted"] is False:               # the customer's own Sunday setting can only tighten the rule
            sunday = "none"
        plan_end = raw["hardship_plan_end"]
        ctx = Context(
            ptp_id=raw["ptp_id"], as_of=as_of, due_date=raw["ptp_due_date"], risk=risk,
            band=actions.band_for(risk, self.thresholds), product=raw["product"], dpd=raw["dpd_at_creation"],
            reminder_scheduled=bool(raw["reminder_scheduled_flag"]), contacts_7d=raw["contacts_7d"],
            cap_7d=int(raw["contact_cap_7d"] or rule.get("cap_7d", 3)), paid_since_creation=raw["paid_since_creation"],
            case_status=raw["case_status"], agency_placed=bool(raw["agency_placement_flag"]),
            legal_handling=bool(raw["legal_referral_flag"]) or raw["legal_status"] is not None,
            charged_off=bool(raw["chargeoff_flag"]),
            insolvency_hold=bool(raw["insolvency_hold_flag"] or raw["bankruptcy_flag"] or raw["consumer_proposal_flag"]),
            deceased=bool(raw["deceased_hold_flag"] or raw["deceased_flag"]),
            cease_contact=bool(raw["cease_contact_flag"] or raw["cease_communication_flag"]),
            representative=bool(raw["third_party_rep_flag"] or raw["credit_counselling_agency_flag"]
                                or raw["legal_representative_flag"] or raw["power_of_attorney_flag"]),
            dispute_open=bool(raw["dispute_flag"]) and raw["dispute_status"] == "open",
            hardship_flag=bool(raw["hardship_flag"]), hardship_source=raw["hardship_source"],
            hardship_plan_active=bool(raw["hardship_program_accepted"]) and (plan_end is None or plan_end >= as_of),
            vulnerable=bool(raw["vulnerable_customer_flag"] or raw["vulnerability_flag"]),
            consent_call=bool(raw["consent_call"]) if found else False, consent_sms=bool(raw["consent_sms"]) if found else False,
            consent_email=bool(raw["consent_email"]) if found else False,
            sunday_rule=sunday, preferred_language=raw["preferred_language"],
            customer_found=found, duplicate_record=bool(raw["duplicate_of_crm_id"]) or bool(raw["is_deleted"]),
            identity_trust=ident["trust_score"] if ident else None,
            identity_low_trust=bool(ident and ident["trust_score"] is not None and ident["trust_score"] < config.IDENTITY_TRUST_REVIEW),
            identity_conflicts=ident["conflict_flags"] if ident else [],
            hardship_text=bool(row.get("g_hardship_text")), hardship_confirmed=hardship_confirmed,
            missing_feature_share=miss, features_audit=gates.model_audit())
        hs = gates.hardship_status(ctx)
        if hs in ("recorded", "confirmed"):
            # a recorded hardship offers agent-approvable programmes; a reviewer-confirmed one also unlocks 'clear'-level programmes
            ctx.eligible_programmes = self._programmes(raw["product"], raw["dpd_at_creation"], as_of,
                                                       "clear" if hs == "confirmed" else "possible")
        return ctx

    def _explain(self, row, raw, ctx: Context, choice, reasons, results, slot) -> dict:
        hs = gates.hardship_status(ctx)

        def what_if() -> list[str]:
            items = []
            if hs == "possible":
                items.append("A reviewer confirms hardship -> the recommendation becomes an eligible hardship option, never a reminder.")
            if choice.action in CONTACT_ACTIONS:
                items.append("A payment arrives before the due date -> no further contact.")
                items.append("Not paid by the end of the due date plus the 2-business-day grace -> the promise counts as broken and a "
                             "human follows up; another automated reminder is not sent.")
            if choice.action == "no_action":
                items.append("No payment by the due date plus the 2-business-day grace -> the promise counts as broken and a human follows up.")
            if any(r.gate == "contact_budget" and r.status == "restrict" for r in results):
                items.append("The 7-day contact budget frees up -> a reminder becomes possible again.")
            items.append(f"Break risk moves across a band threshold ({', '.join(f'{t:.2f}' for t in self.thresholds)}) -> the ranked action changes.")
            return items[:4]

        note = raw.get("note")
        evidence = {"source": "agent note written at the promise's own contact", "note_id": note[0], "note_ts": str(note[1]),
                    "snippet": mask(note[2])[:240], "provenance": "agent-noted",
                    "relevance": "Shown for context. It is not selected to support the reasons above, which come from the model's features."
                    } if note and note[2] else None
        when = f" on {slot['date']} between {slot['from_local']} and {slot['to_local']} local time" if slot else ""
        summary = f"Break risk {ctx.risk:.0%} ({ctx.band}). Recommended: {ACTION_LABEL[choice.action].lower()}{when}. {choice.rationale}"
        relevant = {r.citation for r in results if r.citation and (
            r.status != "pass" or (choice.action in CONTACT_ACTIONS and r.gate in ("contact_budget", "channel_consent")))}
        near = min(abs(ctx.risk - t) for t in self.thresholds)
        return {
            "summary": summary,
            "top_reasons": [{"text": r.text, "effect": f"{r.effect} break risk", "strength": round(abs(r.contribution), 3),
                             "feature": r.feature} for r in reasons],
            "evidence": evidence, "what_would_change_this": what_if(),
            "policy_citations": sorted(relevant),
            "band_stability": "stable" if near > 0.05 else "borderline",
        }
