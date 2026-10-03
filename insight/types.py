from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable


@dataclass
class Period:
    start: date          # inclusive
    end: date            # inclusive
    label: str = ""


@dataclass
class Plan:
    """A validated-or-validatable SQL plan produced by a planner (template or LLM)."""
    name: str
    sql: str
    understood: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)   # extra documents to cite (mixed route)
    summarize: Callable[[list[str], list[tuple]], str] | None = None  # optional prose after the table


@dataclass
class Answer:
    question: str
    answer: str
    sql_or_sources: str
    refused: bool
    route: str
    refusal_reason: str | None = None
    plan: str | None = None
    understood: dict[str, Any] = field(default_factory=dict)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
