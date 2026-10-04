"""Deterministic policy gates. Pure functions of a `Context`: no model, no LLM, no database.

Gates run in a fixed order and can only RESTRICT the set of allowed actions or demand human review; none can
add pressure. Hardship is route-only: it can move a case to softer or human-led treatment, never to more contact.

Sources (all in the provided documents) unless marked ENGINEERING RULE:
  POL-COLL-001 §5.1 hours 07:00-21:00 local, Sunday 13:00-17:00 where permitted, no contact on statutory holidays
  POL-COLL-001 §5.2 max 3 outbound call attempts / rolling 7 days; payment-asking automated reminders count
  POL-COLL-001 §5.3 servicing consent (consent_call / consent_sms / consent_email) governs collection contact
  POL-COLL-001 §5.4 cease request and representatives stop direct contact
  POL-COLL-001 §5.5 French-preferring customers are served in French (informational here: recorded on the contact slot)
  POL-COLL-001 §7.3 bankruptcy / consumer proposal: all collection contact stops (insolvency_hold)
  POL-COLL-004 §4.2 hardship: acknowledge, record, offer an eligible option before asking for payment
  POL-COLL-004 §4.4 an accepted plan moves the case to the hardship_specialist queue
  POL-COLL-004 §4.5 vulnerability is a safeguard, never a risk factor; certified agent; may pause contact
  POL-COLL-004 §4.6 no protected grounds in decisions
  PRC-COLL-012 §6.1 a dispute pauses payment requests for the disputed amount
  ENGINEERING RULE (no document states it): the bank's own collectors do not contact customers whose case is placed with an
  agency, in legal handling, charged off, settled or otherwise no longer an active in-house case.

ASSUMPTIONS (not in the documents, set here and flagged): statutory holidays other than 30 Sep (and the 30 Sep date itself is
documented only as an example); the recommended contact times; the risk-band thresholds in config.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time, timedelta

from . import features as F

ALL_ACTIONS = ["sms_payment_link", "email_reminder", "call_daytime", "call_evening", "offer_hardship_plan",
               "specialist_review", "no_action"]
CONTACT_ACTIONS = {"sms_payment_link", "email_reminder", "call_daytime", "call_evening"}
PAYMENT_REQUEST_ACTIONS = CONTACT_ACTIONS            # all of these ask for payment
SAFE_ACTIONS = {"no_action", "specialist_review"}
# hardship flags recorded by a person (the agent after the customer stated it, or the customer on a form) vs inferred by a model
HUMAN_HARDSHIP_SOURCES = {"agent_coded", "customer_form"}

# Statutory holidays: only 30 Sep is named in POL-COLL-001 §5.1. The rest are an ASSUMPTION (common federal dates, 2026).
# NOTE: the data's own stat_holiday_flag disagrees with part of this list (e.g. 11 Nov is not a holiday there); see docs.
HOLIDAYS = {date(2026, 9, 30): "National Day for Truth and Reconciliation (POL-COLL-001 §5.1)",
            date(2026, 10, 12): "Thanksgiving (assumption)", date(2026, 12, 25): "Christmas Day (assumption)",
            date(2026, 11, 11): "Remembrance Day (assumption, provincial)", date(2026, 9, 7): "Labour Day (assumption)"}

CASE_STATUS_TEXT = {"placed_agency": "placed with a collection agency", "legal": "in legal handling",
                    "charged_off": "charged off", "settled": "settled", "closed_paid": "closed (paid)",
                    "cured": "already cured"}


@dataclass
class Context:
    ptp_id: str
    as_of: date
    due_date: date
    risk: float
    band: str
    # promise / case
    product: str | None = None
    dpd: int | None = None
    reminder_scheduled: bool = False
    contacts_7d: int = 0                      # outbound calls + payment-asking reminders on the case, rolling 7 days
    cap_7d: int = 3
    paid_since_creation: bool = False
    case_status: str | None = "open"
    agency_placed: bool = False
    legal_handling: bool = False              # legal_referral_flag or a legal_status
    charged_off: bool = False
    # case flags
    insolvency_hold: bool = False
    deceased: bool = False
    cease_contact: bool = False
    representative: bool = False
    dispute_open: bool = False
    hardship_flag: bool = False
    hardship_source: str | None = None
    hardship_plan_active: bool = False        # an accepted plan whose end date has not passed
    vulnerable: bool = False
    # customer
    consent_call: bool = True
    consent_sms: bool = True
    consent_email: bool = True
    sunday_rule: str = "13:00-17:00"          # 'none' or window (case's contact_rule_set, and the customer's own Sunday flag)
    preferred_language: str | None = None
    customer_found: bool = True
    duplicate_record: bool = False
    # signals
    hardship_text: bool = False               # unconfirmed note-text signal
    hardship_confirmed: bool = False          # only a human reviewer in this system sets this
    missing_feature_share: float = 0.0
    eligible_programmes: list[dict] = field(default_factory=list)
    features_audit: list[str] = field(default_factory=list)


@dataclass
class GateResult:
    gate: str
    status: str                     # pass | restrict | stop | review | info
    detail: str
    allowed: set[str] | None = None  # None = no restriction from this gate
    citation: str | None = None

    def as_dict(self) -> dict:
        return {"gate": self.gate, "status": self.status, "detail": self.detail, "citation": self.citation,
                "allowed": sorted(self.allowed) if self.allowed is not None else None}


def hardship_status(ctx: Context) -> str:
    """none | possible | recorded | confirmed.

    confirmed  a reviewer in this system confirmed it (requires a reviewer id)
    recorded   the case's hardship flag was set by a person (agent_coded / customer_form): policy §4.2 says the agent records it
    possible   an unconfirmed signal only: a model-sourced flag, or hardship wording in the note with no recorded flag
    """
    if ctx.hardship_confirmed:
        return "confirmed"
    if ctx.hardship_flag and ctx.hardship_source in HUMAN_HARDSHIP_SOURCES:
        return "recorded"
    if ctx.hardship_flag or ctx.hardship_text:
        return "possible"
    return "none"


def stage_reasons(ctx: Context) -> list[str]:
    reasons = []
    if ctx.case_status and ctx.case_status not in ("open", "insolvency_hold"):
        reasons.append(CASE_STATUS_TEXT.get(ctx.case_status, f"in status '{ctx.case_status}'"))
    if ctx.agency_placed and "placed with a collection agency" not in reasons:
        reasons.append("placed with a collection agency")
    if ctx.legal_handling and "in legal handling" not in reasons:
        reasons.append("in legal handling")
    if ctx.charged_off and "charged off" not in reasons:
        reasons.append("charged off")
    return reasons


def run_gates(ctx: Context) -> list[GateResult]:
    out: list[GateResult] = []

    # G1 protected / proxy fields
    problems = ctx.features_audit
    out.append(GateResult("protected_attribute_block", "stop" if problems else "pass",
                          "; ".join(problems) if problems else
                          "No protected attribute or proxy-risk field is used by the model or the gates' risk logic.",
                          set(SAFE_ACTIONS) if problems else None, "POL-COLL-004 §4.6"))

    # G2 hard stops
    stops = [n for n, v in (("insolvency / consumer proposal hold", ctx.insolvency_hold),
                            ("deceased", ctx.deceased), ("written cease-communication request", ctx.cease_contact)) if v]
    if stops:
        out.append(GateResult("hard_stop", "stop", "All collection contact stops: " + ", ".join(stops) + ".",
                              set(SAFE_ACTIONS), "POL-COLL-001 §5.4, §7.3"))
    else:
        out.append(GateResult("hard_stop", "pass", "No insolvency, deceased or cease-contact flag.", None, "POL-COLL-001 §5.4, §7.3"))
    if ctx.representative:
        out.append(GateResult("representative", "stop",
                              "A representative (credit counsellor / lawyer / trustee / POA) acts for the customer: no direct contact, "
                              "debt details are not disclosed to third parties.", set(SAFE_ACTIONS), "POL-COLL-001 §5.4"))

    # G2b case stage (agency / legal / charged off / settled / not an active in-house case)
    stage = stage_reasons(ctx)
    if stage:
        out.append(GateResult("case_stage", "stop",
                              "The case is " + " and ".join(stage) + ": no direct contact from the bank's own collectors; the owning party "
                              "(agency, legal or recoveries) or a human handles it. Engineering rule: the provided policy documents "
                              "do not state this.", set(SAFE_ACTIONS), None))
    else:
        out.append(GateResult("case_stage", "pass", "Active in-house case: not placed, not in legal handling, not charged off or settled.",
                              None, None))

    if ctx.dispute_open:
        out.append(GateResult("open_dispute", "restrict",
                              "An open dispute pauses payment requests for the disputed amount until it is resolved.",
                              {"specialist_review", "no_action"}, "PRC-COLL-012 §6.1"))

    # G3 identity / data quality
    dq = []
    if not ctx.customer_found:
        dq.append("customer record not found")
    if ctx.duplicate_record:
        dq.append("customer record is flagged as a duplicate or deleted (identity uncertain)")
    if ctx.missing_feature_share > 0.5:
        dq.append(f"{ctx.missing_feature_share:.0%} of model features are missing")
    out.append(GateResult("identity_and_data_quality", "review" if dq else "pass",
                          "; ".join(dq) if dq else "Customer record found; no duplicate flag; enough features present.",
                          None, None))

    # G4 hardship (route-only)
    hs = hardship_status(ctx)
    if hs == "none":
        out.append(GateResult("hardship_route_only", "pass", "No hardship signal.", None, "POL-COLL-004 §4.2"))
    elif hs == "possible":
        out.append(GateResult(
            "hardship_route_only", "restrict",
            "Possible hardship from an unconfirmed signal (model-sourced flag or note wording): a human must confirm it before any "
            "payment request. Treatment can only become softer or human-led.",
            {"specialist_review", "no_action"}, "POL-COLL-004 §4.2"))
    elif ctx.hardship_plan_active:
        out.append(GateResult(
            "hardship_route_only", "restrict",
            f"Hardship is {hs} and an accepted hardship plan is in force: the hardship specialist queue owns contact; "
            "no new payment request.", {"specialist_review", "no_action"}, "POL-COLL-004 §4.2, §4.4"))
    else:
        allowed = {"offer_hardship_plan", "specialist_review", "no_action"} if ctx.eligible_programmes else set(SAFE_ACTIONS)
        out.append(GateResult(
            "hardship_route_only", "restrict",
            f"Hardship is {hs}: offer an eligible option before asking for payment; payment requests are not recommended until then."
            + ("" if ctx.eligible_programmes else " No programme is automatically eligible, so a specialist decides."),
            allowed, "POL-COLL-004 §4.2"))

    # G5 vulnerability safeguard
    if ctx.vulnerable:
        out.append(GateResult("vulnerability_safeguard", "restrict",
                              "Vulnerable customer: route to a vulnerable-customer-certified agent; vulnerability is a safeguard and is not "
                              "used to score risk.", {"specialist_review", "no_action"}, "POL-COLL-004 §4.5"))

    # G6 consent per channel
    consent = {"sms_payment_link": ctx.consent_sms, "email_reminder": ctx.consent_email,
               "call_daytime": ctx.consent_call, "call_evening": ctx.consent_call}
    blocked = sorted(a for a, ok in consent.items() if not ok)
    out.append(GateResult("channel_consent", "restrict" if blocked else "pass",
                          ("No servicing consent for: " + ", ".join(blocked)) if blocked else "Servicing consent on file for call, SMS and email.",
                          set(ALL_ACTIONS) - set(blocked) if blocked else None, "POL-COLL-001 §5.3"))

    # G7 contact budget
    left = ctx.cap_7d - ctx.contacts_7d
    if left <= 0:
        out.append(GateResult("contact_budget", "restrict",
                              f"{ctx.contacts_7d} of {ctx.cap_7d} permitted outbound attempts already used in the last 7 days "
                              f"(payment-asking reminders count).", set(ALL_ACTIONS) - CONTACT_ACTIONS - {"offer_hardship_plan"},
                              "POL-COLL-001 §5.2"))
    else:
        out.append(GateResult("contact_budget", "pass", f"{left} of {ctx.cap_7d} attempts left this rolling week.", None, "POL-COLL-001 §5.2"))

    # G8 promise already being honoured
    if ctx.paid_since_creation:
        out.append(GateResult("payment_received", "restrict", "A payment has been received on the case since the promise was made.",
                              {"no_action", "specialist_review"}, "PRC-COLL-010 §3.3"))
    return out


def allowed_actions(results: list[GateResult]) -> set[str]:
    allowed = set(ALL_ACTIONS)
    for r in results:
        if r.allowed is not None:
            allowed &= r.allowed
    if not allowed:
        allowed = {"no_action"}
    return allowed


# ------------------------------------------------------------------------------------------------ timing

DESIRED_WINDOWS = {"sms_payment_link": (9, 10), "email_reminder": (9, 10), "call_daytime": (11, 13), "call_evening": (17, 19)}
PERMITTED_HOURS = (7, 21)         # POL-COLL-001 §5.1, customer local time


def fit_window(desired: tuple[int, int], allowed: tuple[int, int]) -> tuple[int, int]:
    """Place a desired [start, end) hour window inside an allowed window, keeping its length where possible.

    Overlap: the intersection. No overlap: the nearest slot of the same length inside the allowed window.
    The result is always non-empty and always inside `allowed`.
    """
    s, e = desired
    a, b = allowed
    length = max(e - s, 1)
    if e <= a:                                   # desired is entirely before the allowed window
        start = a
    elif s >= b:                                 # entirely after it
        start = max(a, b - length)
    else:                                        # overlap
        start, end = max(s, a), min(e, b)
        return (start, end) if end > start else (a, min(b, a + length))
    return (start, min(b, start + length))


def contact_slot(ctx: Context, action: str) -> dict | None:
    """Earliest permitted local contact time for a contact action (None for non-contact actions).

    Day: the later of the as-of date and the day before the due date, moved forward past holidays and a non-permitted Sunday.
    Time: SMS/email 09:00-10:00; call_daytime 11:00-13:00; call_evening 17:00-19:00 (the right-party-contact rate peaks at
    17-19h in the September data, BQ-011 gold). Always inside 07:00-21:00, and on a permitted Sunday inside the Sunday window.
    """
    if action not in CONTACT_ACTIONS:
        return None
    day = max(ctx.as_of, ctx.due_date - timedelta(days=1))
    notes = []
    for _ in range(14):
        if day in HOLIDAYS:
            notes.append(f"{day.isoformat()} skipped: {HOLIDAYS[day]}")
            day += timedelta(days=1)
        elif day.weekday() == 6 and ctx.sunday_rule in ("none", ""):
            notes.append(f"{day.isoformat()} skipped: no Sunday collection contact for this customer / province")
            day += timedelta(days=1)
        else:
            break
    allowed = PERMITTED_HOURS
    if day.weekday() == 6:
        a, b = (int(x.split(":")[0]) for x in ctx.sunday_rule.split("-"))   # '13:00-17:00'
        allowed = (max(a, PERMITTED_HOURS[0]), min(b, PERMITTED_HOURS[1]))
        notes.append("Sunday: limited to " + ctx.sunday_rule + " local time")
    desired = DESIRED_WINDOWS[action]
    s, e = fit_window(desired, allowed)
    if (s, e) != desired:
        notes.append(f"preferred window {desired[0]:02d}:00-{desired[1]:02d}:00 moved to fit the permitted hours")
    return {"date": day.isoformat(), "from_local": time(s).strftime("%H:%M"), "to_local": time(e).strftime("%H:%M"),
            "language": "fr" if (ctx.preferred_language or "").lower().startswith("fr") else "en", "notes": notes}


def model_audit() -> list[str]:
    return F.audit_registry()
