"""Action ranking and human-review triggers (deterministic; the model only supplies the risk number).

Ranking logic (risk bands are set in config and are an ASSUMPTION: the documents say the budget and thresholds
are 'set by policy'; none are given):
  low        no_action when a reminder is already scheduled (restraint is a first-class outcome), else sms_payment_link
  medium     sms_payment_link, falling back to email_reminder, then call_daytime
  high       call_evening, falling back to sms_payment_link, then call_daytime
  very_high  same as high
Hardship, vulnerability, case stage, hard stops, an open dispute, an exhausted contact budget and missing consent are applied
by the gates BEFORE this step through the allowed-action set; ranking never widens it. Nothing here claims an action is
'effective': the data has no uplift evidence, so the ranking only picks the lightest option the risk band calls for.

Review triggers (v2). A trigger is "review" (a person must look at THIS recommendation before it is used) or "routing"
(the case is already handed to a person by the action itself). review_required is true only for fired "review" triggers.
v1 fired on about 87% of open promises because it counted routine hardship / vulnerability routing, any score within 0.05 of a
band edge (35% of scores sit within 0.02 of one, because the model's output is compressed into 0.25-0.78), and every high-risk case
that policy blocked. v2 keeps the 8 Round-1 triggers but defines them narrowly. "Low model confidence" now means thin evidence
(30%+ of features missing) or a score in the calibration tail (above TAIL_RISK, where the isotonic calibrator rests on very few
validation promises); closeness to a band edge is shown as `band_stability` in the explanation, not used to demand a review.
"""
from __future__ import annotations

from dataclasses import dataclass

from .gates import Context, GateResult, allowed_actions, hardship_status

TRIGGER_VERSION = "v2"
TAIL_RISK = 0.90          # ASSUMPTION: above this the isotonic calibrator's last steps (0.872, 0.999) rest on very few validation promises
MIN_MISSING = 0.30        # ASSUMPTION: share of model features missing that counts as thin evidence (about 3% of open promises)

PREFERENCE = {
    "low": ["no_action", "sms_payment_link"],
    "medium": ["sms_payment_link", "email_reminder", "call_daytime"],
    "high": ["call_evening", "sms_payment_link", "call_daytime"],
    "very_high": ["call_evening", "sms_payment_link", "call_daytime"],
}

# contact intensity, used to stop a lower role from escalating beyond the recommendation
INTENSITY = {"no_action": 0, "specialist_review": 0, "offer_hardship_plan": 0, "email_reminder": 1,
             "sms_payment_link": 1, "call_daytime": 2, "call_evening": 3}


def band_for(risk: float, thresholds: tuple[float, float, float]) -> str:
    lo, hi, vhi = thresholds
    return "low" if risk < lo else "medium" if risk < hi else "high" if risk < vhi else "very_high"


@dataclass
class Choice:
    action: str
    rationale: str
    ranked: list[tuple[str, str]]       # (action, why it was/was not chosen)


def choose(ctx: Context, results: list[GateResult]) -> Choice:
    allowed = allowed_actions(results)
    hs = hardship_status(ctx)
    ranked: list[tuple[str, str]] = []
    stop = next((r for r in results if r.status == "stop"), None)

    if stop:
        return Choice("no_action", f"A hard stop applies ({stop.gate}): no collection contact is recommended; a human owns the case. "
                                   f"{stop.detail}",
                      [("no_action", f"hard stop: {stop.gate}"), ("specialist_review", "a human follows up")])
    if ctx.paid_since_creation:
        return Choice("no_action", "A payment has already been received since the promise was made.", [("no_action", "payment received")])
    if hs in ("recorded", "confirmed"):
        who = "a reviewer confirmed" if hs == "confirmed" else "a person recorded"
        if ctx.hardship_plan_active:
            return Choice("no_action", f"Hardship ({who}) and an accepted plan is in force: the hardship specialist queue owns contact "
                                       "(POL-COLL-004 §4.4); no new payment request.",
                          [("no_action", "plan in force"), ("specialist_review", "specialist reviews the plan monthly")])
        if "offer_hardship_plan" in allowed:
            prog = ", ".join(p["program_id"] for p in ctx.eligible_programmes[:3])
            return Choice("offer_hardship_plan", f"Hardship ({who}): offer an eligible option before asking for payment ({prog}).",
                          [("offer_hardship_plan", f"hardship {hs}"), ("specialist_review", "specialist reviews the plan monthly")])
        return Choice("specialist_review", f"Hardship ({who}) but no programme is automatically eligible or another gate blocks the offer: "
                                           "a specialist decides.", [("specialist_review", "no automatic option")])
    if hs == "possible":
        return Choice("specialist_review", "Possible hardship from an unconfirmed signal: a human confirms it before any payment request.",
                      [("specialist_review", "possible hardship"), ("no_action", "no reminder pressure meanwhile")])
    if ctx.vulnerable:
        return Choice("specialist_review", "Vulnerable customer: a certified agent handles contact; no automated pressure.",
                      [("specialist_review", "safeguard")])

    for a in PREFERENCE[ctx.band]:
        if a == "no_action" and ctx.band == "low":
            if ctx.reminder_scheduled:
                ranked.append((a, "low break risk and a reminder is already scheduled: no extra contact needed"))
                return Choice(a, "Break risk is low and the standard reminder is already scheduled: no additional contact.", ranked)
            ranked.append((a, "low risk but no reminder scheduled (procedure requires one)"))
            continue
        if a in allowed:
            ranked.append((a, "preferred for this risk band and allowed by every gate"))
            return Choice(a, f"Risk band {ctx.band}: {a.replace('_', ' ')} is the lightest option for this band that policy allows.", ranked)
        ranked.append((a, "blocked by a gate (consent, budget or dispute)"))
    return Choice("no_action", "Every contact option the risk band calls for is blocked by policy (consent, budget or dispute): no action.",
                  ranked + [("no_action", "default when nothing is allowed")])


def review_triggers(ctx: Context, results: list[GateResult], choice: Choice, thresholds: tuple[float, float, float],
                    min_missing: float = MIN_MISSING, tail_risk: float = TAIL_RISK) -> list[dict]:
    """The Round-1 triggers, evaluated and recorded one by one. 'kind' is review or routing (see module docstring)."""
    hs = hardship_status(ctx)
    any_stop = any(r.status == "stop" for r in results)
    # high risk, nothing hard-stops the case, yet every contact the band calls for is blocked by consent / budget / dispute
    blocked_high = (ctx.band in ("high", "very_high") and choice.action == "no_action" and not any_stop
                    and not ctx.paid_since_creation and hs == "none" and not ctx.vulnerable)
    lead = ctx.eligible_programmes[0] if ctx.eligible_programmes else None
    needs_approval = choice.action == "offer_hardship_plan" and lead is not None and lead.get("approver") != "agent"
    audit_failed = any(r.gate == "protected_attribute_block" and r.status != "pass" for r in results)
    identity_issue = bool(ctx.duplicate_record or not ctx.customer_found or ctx.identity_low_trust)

    def t(name, fired, why, kind="review"):
        return {"trigger": name, "fired": bool(fired), "why": why if fired else "ok", "kind": kind}

    return [
        t("uncertain_identity", identity_issue, "customer record is missing, flagged duplicate/deleted, or the C360 identity trust score is low"),
        t("possible_hardship", hs == "possible", "unconfirmed hardship signal (model-sourced flag or note wording)"),
        t("low_model_confidence", ctx.missing_feature_share >= min_missing or ctx.risk > tail_risk,
          f"{ctx.missing_feature_share:.0%} of model features are missing" if ctx.missing_feature_share >= min_missing
          else f"risk {ctx.risk:.2f} is in the calibration tail (above {tail_risk:.2f})"),
        t("policy_conflict", blocked_high, "high break risk but every contact the band calls for is blocked by consent, budget or dispute"),
        t("high_impact_action", needs_approval, "the lead hardship option needs approval above agent level"),
        {"trigger": "conflicting_agent_outputs", "fired": False, "why": "single recommender; not evaluated in this build", "kind": "review"},
        t("insufficient_data_quality", ctx.missing_feature_share > 0.5, f"{ctx.missing_feature_share:.0%} of model features are missing"),
        t("fairness_or_proxy_concern", audit_failed, "feature audit failed"),
        t("vulnerable_customer", ctx.vulnerable, "routed to a vulnerable-customer-certified agent", kind="routing"),
    ]


def review_is_required(triggers: list[dict]) -> bool:
    return any(t["fired"] and t.get("kind", "review") == "review" for t in triggers)
