"""Deterministic refusal rules. Run BEFORE any retrieval or SQL planning.

Refusals are a tested behaviour (benchmark: refused must be true for protected-ground,
proxy, bulk-PII and out-of-scope questions, and false for everything else), so every
rule is explicit, named and explainable. An LLM may add a second opinion later, but it
can only make the system refuse MORE, never override these rules.

Sources: POL-COLL-004 §4.5/§4.6 (protected grounds, vulnerability is a safeguard),
README privacy rules, build-phase rules ("no protected attributes in decisions").
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Refusal:
    code: str
    reason: str


PROTECTED = re.compile(
    r"\b(gender|sex|marital|married|single status|family status|citizenship|immigration|immigrant|newcomer|"
    r"race|racial|ethnic\w*|religio\w*|national origin|household|accent|disabilit\w*|accessibilit\w*|"
    r"sexual orientation|indigenous)\b", re.I)
POLICY_WORDS = re.compile(r"\b(policy|policies|allowed to|permitted|rule|rules|procedure|does the bank|what does)\b", re.I)
DATA_INTENT = re.compile(
    r"\b(list|show|rank|sort|order|prioriti[sz]e|call first|target|segment|break ?down|breakdown|by|which customers|"
    r"score|split|compare|group|who|how many|rate|average|share|first)\b", re.I)

PROXY_AREA = re.compile(r"\b(fsa|fsas|postal (area|code)s?|forward sortation|neighbou?rhood|zip ?codes?)\b", re.I)
DECISION_VERB = re.compile(
    r"\b(stop offering|should we (stop|exclude|offer|target|avoid|deny)|exclude|deny|decline|avoid|target|"
    r"prioriti[sz]e|default more|riskier|call first|only offer|restrict)\b", re.I)

BULK_VERB = re.compile(r"\b(export|dump|download|extract|list|give me|send me|show me|pull)\b", re.I)
PII_FIELD = re.compile(r"\b(names?|phone( numbers?)?|e-?mails?|addresses|address|date of birth|dob|sin|national id|"
                       r"postal codes?)\b", re.I)
BULK_SCOPE = re.compile(r"\b(every|all|each|entire|whole)\b.*\bcustomers?\b|\bcustomers? in\b", re.I)
INDIVIDUAL_PII = re.compile(
    r"\b(what is|what's|give me|tell me|show me)\b.*\b(phone|e-?mail|address|date of birth|dob|sin)\b.*"
    r"\b(of|for)\b\s+(?!the (customer|account) type)", re.I)

OFF_TOPIC = re.compile(
    r"\b(weather|forecast|temperature|stock price|share price|bitcoin|crypto|sports?|score of the game|recipe|"
    r"movie|joke|poem|horoscope|lottery|president|prime minister|capital of)\b", re.I)


def check(question: str) -> Refusal | None:
    q = question.strip()
    if OFF_TOPIC.search(q):
        return Refusal("out_of_scope", "This is outside the collections data and policies I am allowed to answer from.")

    if PROTECTED.search(q) and not POLICY_WORDS.search(q) and DATA_INTENT.search(q):
        term = PROTECTED.search(q).group(1).lower()
        return Refusal(
            "protected_attribute",
            f"'{term}' is a protected ground. Protected attributes must not be used in decisions, rankings or lists "
            f"(POL-COLL-004 §4.6); they are held only to test fairness.")

    if PROXY_AREA.search(q) and DECISION_VERB.search(q):
        return Refusal(
            "proxy_variable",
            "Postal areas (FSAs) are a proxy for income and protected grounds, so they cannot drive decisions about "
            "who is offered hardship help (POL-COLL-004 §4.5-§4.6).")

    if (BULK_VERB.search(q) and PII_FIELD.search(q) and BULK_SCOPE.search(q)) or INDIVIDUAL_PII.search(q):
        return Refusal(
            "pii_export",
            "Bulk or individual personal data (names, phone numbers, emails, addresses, dates of birth) cannot be "
            "exported or read out through this assistant; PII is masked by default and unmasking needs a privileged, logged role.")
    return None
