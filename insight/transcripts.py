"""Call-transcript lookup and rule-based structured summary.

Route: "Summarise the most recent recorded call on case X".
  1. latest transcript for the case (SQL, shown to the user)
  2. read the transcript JSON turns
  3. extract: hardship reason, promise (amount/date), cease request, dispute, next step
  4. render a one-line summary in the same shape as the gold summaries and cite the transcript id

Extraction is deterministic and evaluated against the 500 gold-labelled transcripts
(tests/eval_transcript_extraction.py). `Summarizer` is the plug-in point for an LLM
version; it must keep the same output fields so the evidence check still applies.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Protocol

from data_pipeline.config import RELEASE

CASE_RE = re.compile(r"\bCS-\d{4}-\d+\b")

MONTHS = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7, "august": 8,
          "september": 9, "october": 10, "november": 11, "december": 12}
MON_ABBR = {1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun", 7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct",
            11: "Nov", 12: "Dec"}

# order matters: first match wins for the headline reason
REASONS = [
    ("job_loss", "job loss", r"laid off|lost my job|let go|position (was )?eliminated|plant (closed|shut)|terminated|"
                             r"unemployed|out of work|downsiz|redundan"),
    ("reduced_income", "reduced hours / income", r"hours\b.{0,30}\b(cut|reduced)|reduced hours|fewer hours|"
                                                  r"days a week|pay (got |was |has been )?cut|less pay|reduced (income|pay)|"
                                                  r"only getting|only bringing home|income (dropped|is down)|cut back|overtime|"
                                                  r"making less|bringing home"),
    ("illness", "illness", r"\bsick\b|illness|\bill\b|surgery|hospital|cancer|medical|anxiety|depress|diagnos|injur|"
                           r"recovering|treatment"),
    ("disability", "disability", r"disabilit"),
    ("separation", "separation", r"separat|divorc|split up|moved out"),
    ("disaster_evacuation", "disaster / evacuation", r"evacuat|wildfire|flood|storm|hurricane|fire damage"),
    ("ei_waiting", "waiting for Employment Insurance", r"employment insurance|\bei\b|waiting (for|on) (my )?(ei|claim)"),
    ("rate_reset", "payment increase after rate reset", r"renewal|rate (went up|increase|reset|jump)|payment (went|jumped) up|"
                                                         r"interest rate"),
    ("bereavement", "bereavement", r"passed away|funeral|death in|bereave|lost my (husband|wife|mother|father)"),
]
ANY_REASON = re.compile("|".join(rx for _, _, rx in REASONS))
CEASE = re.compile(r"stop (calling|contacting|texting)|don'?t (call|contact) me|do not (call|contact)|no more calls|"
                   r"cease|leave me alone|arrêtez de (m'appeler|appeler)", re.I)
DISPUTE = re.compile(r"dispute|not (my|a valid) charge|already paid|late fee|charged in error|didn'?t make that|"
                     r"payment (was )?not applied|billing error|fraud", re.I)
INSOLVENCY = re.compile(r"consumer proposal|bankrupt|insolvency|trustee", re.I)
THIRD_PARTY = re.compile(r"credit counsel|my lawyer|my attorney|my trustee|on (his|her) behalf", re.I)

COMPLAINT = re.compile(r"complain|harass|calling me every day|how many times you call|ombudsman|speak (to|with) (a |your )?"
                       r"(supervisor|manager)|too many calls", re.I)
FR_MONTHS = {"janv": 1, "févr": 2, "fevr": 2, "mars": 3, "avr": 4, "mai": 5, "juin": 6, "juil": 7, "août": 8, "aout": 8,
             "sept": 9, "oct": 10, "nov": 11, "déc": 12, "dec": 12}
CUST_A = re.compile(r"\$?([\d,]+(?:\.\d+)?)\$?\s+([A-Za-zéûô]+)\.?\s+(\d{1,2})\b")
CUST_B = re.compile(r"\$?([\d,]+(?:\.\d+)?)\$?\s+(?:on\s+)?the\s+(\d{1,2})(?:st|nd|rd|th)?\b", re.I)
CUST_C = re.compile(r"payer\s+\$?([\d,]+(?:\.\d+)?)\$?\s+le\s+(\d{1,2})\b", re.I)
FR_PAY = re.compile(r"paiement de\s+([\d,.]+)\s*\$\s+le\s+(\d{1,2})\s+([A-Za-zéûô]+)", re.I)
PAY_RE = re.compile(r"payment of \$?([\d,]+(?:\.\d+)?)\s+on\s+([A-Za-z]+)\s+(\d{1,2})", re.I)
PAY_RE_CUST = re.compile(r"(?:pay|payment of)\s+\$?([\d,]+(?:\.\d+)?)\s+(?:on\s+)?(?:the\s+)?([A-Za-z]+)?\s*(\d{1,2})?", re.I)
ORD_RE = re.compile(r"\$?([\d,]+(?:\.\d+)?)\s+(?:on )?the (\d{1,2})(?:st|nd|rd|th)", re.I)


@dataclass
class Extraction:
    transcript_id: str
    reason_code: str | None = None
    reason_text: str | None = None
    ptp_amount: float | None = None
    ptp_month: int | None = None
    ptp_day: int | None = None
    cease: bool = False
    dispute: bool = False
    insolvency: bool = False
    third_party: bool = False
    complaint: bool = False
    hardship: bool = False
    supervisor: bool = False
    payment_link: bool = False
    next_step: str = "call_back"
    evidence: list[dict] = field(default_factory=list)

    def summary(self) -> str:
        parts = []
        if self.reason_text:
            parts.append(f"Customer reports {self.reason_text}")
        elif self.dispute:
            parts.append("Customer disputes a charge")
        if self.ptp_amount is not None and self.ptp_month:
            parts.append(f"promised ${round(self.ptp_amount):,} on {self.ptp_day} {MON_ABBR[self.ptp_month]}")
        elif not parts:
            parts.append("No commitment made")
        if self.cease:
            parts.append("asked to stop contact")
        parts.append("next step: " + self.next_step.replace("_", " "))
        text = "; ".join(parts)
        return text[0].upper() + text[1:] + "."


class Summarizer(Protocol):
    def summarize(self, transcript: dict) -> Extraction: ...


class RuleSummarizer:
    def summarize(self, transcript: dict) -> Extraction:
        turns = transcript["turns"]
        ex = Extraction(transcript_id=str(transcript["transcript_id"]))
        cust = [t for t in turns if t["speaker"] == "customer"]
        agent = [t for t in turns if t["speaker"] == "agent"]
        ctext = " ".join(t["text"] for t in cust).lower()
        for code, label, rx in REASONS:
            if re.search(rx, ctext):
                ex.reason_code, ex.reason_text = code, label
                break
        ex.hardship = ex.reason_code is not None
        ex.cease = bool(CEASE.search(ctext))
        ex.dispute = bool(DISPUTE.search(ctext))
        ex.insolvency = bool(INSOLVENCY.search(ctext))
        ex.complaint = bool(COMPLAINT.search(ctext))
        ex.third_party = bool(THIRD_PARTY.search(ctext))
        atext = " ".join(t["text"] for t in agent).lower()
        ex.supervisor = bool(re.search(r"supervisor|team lead|manager", atext))
        ex.payment_link = "payment link" in atext

        call_day = int(str(transcript.get("call_start_ts", "2026-01-01"))[8:10])
        call_month = int(str(transcript.get("call_start_ts", "2026-01-01"))[5:7])

        def infer_month(day: int) -> int:
            return call_month if day >= call_day else call_month % 12 + 1

        for t in agent:                                       # agent read-back is the cleanest source
            m = PAY_RE.search(t["text"])
            if m and m.group(2).lower() in MONTHS:
                ex.ptp_amount = float(m.group(1).replace(",", ""))
                ex.ptp_month, ex.ptp_day = MONTHS[m.group(2).lower()], int(m.group(3))
            else:
                f = FR_PAY.search(t["text"])
                if f and f.group(3).lower().strip(".")[:4] in FR_MONTHS or (f and f.group(3).lower().strip(".")[:3] in FR_MONTHS):
                    key = f.group(3).lower().strip(".")
                    ex.ptp_amount = float(f.group(1).replace(",", ""))
                    ex.ptp_month = FR_MONTHS.get(key[:4]) or FR_MONTHS.get(key[:3])
                    ex.ptp_day = int(f.group(2))
            if ex.ptp_amount is not None:
                ex.evidence.append({"speaker": "agent", "start_sec": t["start_sec"], "text": t["text"]})
                break
        if ex.ptp_amount is None:                              # customer-stated promise
            for t in cust:
                txt = t["text"]
                a = CUST_A.search(txt)
                if a and (a.group(2).lower() in MONTHS or a.group(2).lower()[:3] in {k[:3] for k in MONTHS}):
                    mon = MONTHS.get(a.group(2).lower()) or next(v for k, v in MONTHS.items() if k[:3] == a.group(2).lower()[:3])
                    ex.ptp_amount, ex.ptp_month, ex.ptp_day = float(a.group(1).replace(",", "")), mon, int(a.group(3))
                else:
                    b = CUST_B.search(txt) or CUST_C.search(txt)
                    if b:
                        ex.ptp_amount, ex.ptp_day = float(b.group(1).replace(",", "")), int(b.group(2))
                        ex.ptp_month = infer_month(ex.ptp_day)
                if ex.ptp_amount is not None:
                    ex.evidence.append({"speaker": "customer", "start_sec": t["start_sec"], "text": t["text"]})
                    break
        for t in cust:
            low = t["text"].lower()
            if ANY_REASON.search(low) or CEASE.search(low) or DISPUTE.search(low):
                ex.evidence.append({"speaker": "customer", "start_sec": t["start_sec"], "text": t["text"]})
        ex.next_step = self._next_step(ex)
        ex.evidence.sort(key=lambda e: e["start_sec"])
        return ex

    @staticmethod
    def _next_step(ex: Extraction) -> str:
        if ex.cease or ex.insolvency:
            return "stop_contact"
        if ex.complaint or ex.supervisor or (ex.third_party and not ex.hardship):
            return "escalate_supervisor"
        if ex.dispute and not ex.hardship:
            return "dispute_investigation"
        if ex.hardship:
            return "hardship_plan_review"
        if ex.payment_link or ex.ptp_amount is not None:
            return "send_payment_link"
        return "call_back"


def load_transcript(file_path: str) -> dict:
    return json.loads((RELEASE / "files" / file_path).read_text(encoding="utf-8"))


def latest_for_case_sql() -> str:
    return ("select transcript_id, case_id, call_start_ts, file_path from cur_transcripts "
            "where case_id = ? order by call_start_ts desc limit 1")
