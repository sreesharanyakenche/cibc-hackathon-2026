"""LLM plug-in point for the Insight layer.

Design rule: the LLM may DRAFT (SQL, wording, extraction). Deterministic code DECIDES
(guard, refusal rules, evidence gate, citations). Nothing here is required for the
template path, so the system runs end to end with `NullLLM`.

To enable: set ANTHROPIC_API_KEY (and optionally INSIGHT_LLM_MODEL) and construct
`Engine(..., llm=get_llm())`. Nothing else changes.
"""
from __future__ import annotations

import os
from typing import Protocol


class LLMUnavailable(RuntimeError):
    pass


class LLM(Protocol):
    def complete(self, system: str, user: str, *, max_tokens: int = 1500) -> str: ...


class NullLLM:
    """Used when no API key is configured: every call raises, planners treat that as 'no plan'."""

    available = False

    def complete(self, system: str, user: str, *, max_tokens: int = 1500) -> str:
        raise LLMUnavailable("no LLM configured")


class AnthropicLLM:
    available = True

    def __init__(self, model: str | None = None, api_key: str | None = None) -> None:
        import anthropic  # lazy: only needed when an LLM is configured

        self._client = anthropic.Anthropic(api_key=api_key or os.environ["ANTHROPIC_API_KEY"])
        self.model = model or os.environ.get("INSIGHT_LLM_MODEL", "claude-sonnet-5-5")

    def complete(self, system: str, user: str, *, max_tokens: int = 1500) -> str:
        msg = self._client.messages.create(
            model=self.model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}], temperature=0)
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")


def get_llm() -> LLM:
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return AnthropicLLM()
        except Exception:  # missing package etc.: degrade, do not crash the demo
            return NullLLM()
    return NullLLM()
