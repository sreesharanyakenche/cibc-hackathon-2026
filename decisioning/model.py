"""Load a trained PTP-break model, score feature rows, and turn per-prediction contributions into
plain-English reasons.

Scoring is the same code path for batch and live: callers pass the DataFrame produced by
`features.compute_features`. Calibration is isotonic (stored as JSON), applied with np.interp.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import lightgbm as lgb
import numpy as np
import polars as pl

from . import config
from .features import FEATURES

DESC = {f.name: f.description for f in FEATURES}

# value -> phrase. Each returns text WITHOUT the direction; the direction comes from the contribution sign.
PHRASES = {
    "ptp_to_overdue_ratio": lambda v: f"Promised {v:.0%} of the overdue amount",
    "ptp_amount": lambda v: f"Promised amount is ${v:,.0f}",
    "overdue_at_creation": lambda v: f"${v:,.0f} was overdue when the promise was made",
    "entry_overdue": lambda v: f"${v:,.0f} was overdue when the case opened",
    "dpd_at_creation": lambda v: f"{v:.0f} days past due when the promise was made",
    "entry_dpd": lambda v: f"Case opened at {v:.0f} days past due",
    "days_to_due": lambda v: f"Due {v:.0f} days after the promise was made",
    "prior_broken_ptp_count": lambda v: f"{v:.0f} earlier broken promise(s) on this case",
    "ptp_sequence_in_case": lambda v: f"Promise number {v:.0f} on this case",
    "cust_prior_broken_rate": lambda v: f"{v:.0%} of this customer's earlier promises were broken",
    "cust_prior_resolved": lambda v: f"{v:.0f} earlier resolved promise(s) by this customer",
    "cust_days_since_last_broken": lambda v: f"Last broken promise was {v:.0f} days ago",
    "case_age_days": lambda v: f"Case is {v:.0f} days old",
    "acct_utilisation": lambda v: f"Accounts are at {v:.0%} of limit/original amount",
    "acct_max_dpd": lambda v: f"Worst account is {v:.0f} days past due",
    "acct_delinquent_n": lambda v: f"{v:.0f} account(s) past due",
    "acct_paid_vs_min": lambda v: f"Paid {v:.0%} of the minimum due last quarter",
    "sal_avg_3m": lambda v: f"Average salary credit ${v:,.0f}/month over the prior 3 months",
    "sal_gap_months_3m": lambda v: f"{v:.0f} salary gap month(s) in the prior 3",
    "t_note_words": lambda v: f"Agent note is {v:.0f} words long",
    "t_payday": lambda v: "Note ties the promise to a pay date" if v else "Note does not tie the promise to a pay date",
    "t_link_sent": lambda v: "A payment link was sent" if v else "No payment link recorded",
    "t_maybe": lambda v: "Customer used hedged language ('will try')" if v else "Customer committed without hedging",
    "t_forgot": lambda v: "Customer said they had forgotten" if v else "No forgetting mentioned",
    "t_dispute": lambda v: "A dispute was mentioned" if v else "No dispute mentioned",
    "is_self_serve": lambda v: "Promise was made self-serve (no agent)" if v else "Promise was made with an agent",
    "reminder_scheduled": lambda v: "A reminder is scheduled" if v else "No reminder is scheduled",
    "channel_code": lambda v: "Promise channel: " + {0: "call", 1: "IVR", 2: "web", 3: "app", 4: "SMS reply"}.get(int(v), "unknown"),
    "product_code": lambda v: "Product: " + {0: "card", 1: "personal loan", 2: "auto loan", 3: "line of credit", 4: "overdraft",
                                             5: "mortgage", 6: "multiple"}.get(int(v), "unknown"),
    "payment_method_code": lambda v: "Payment method: " + {0: "online banking", 1: "debit card", 2: "e-transfer", 3: "PAD",
                                                           4: "branch"}.get(int(v), "unknown"),
    "due_dom": lambda v: f"Due on day {v:.0f} of the month",
    "due_dow": lambda v: "Due on a " + ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"][int(v) % 7],
    "created_hour": lambda v: f"Promise made around {v:.0f}:00",
}


@dataclass
class Reason:
    feature: str
    text: str
    effect: str          # raises | lowers
    contribution: float  # log-odds contribution (SHAP-style, from LightGBM pred_contrib)
    value: float | None


class PtpModel:
    def __init__(self, version: str = config.MODEL_VERSION) -> None:
        stem = config.MODEL_DIR / f"{config.MODEL_NAME}_{version}"
        self.version = version
        self.sha256 = hashlib.sha256(Path(str(stem) + ".txt").read_bytes()).hexdigest()   # recorded in every Decision Record
        self.booster = lgb.Booster(model_file=str(stem) + ".txt")
        self.meta = json.loads((stem.with_suffix(".json")).read_text(encoding="utf-8"))
        self.names: list[str] = self.meta["features"]
        cal = self.meta["calibration"]
        self._cx, self._cy = np.array(cal["x"]), np.array(cal["y"])

    def _matrix(self, df: pl.DataFrame) -> np.ndarray:
        return df.select(self.names).cast(pl.Float64).to_numpy().astype(float)

    def score(self, df: pl.DataFrame) -> np.ndarray:
        raw = self.booster.predict(self._matrix(df))
        return np.interp(raw, self._cx, self._cy)

    def reasons(self, df: pl.DataFrame, k: int = 3) -> list[list[Reason]]:
        """Top-k contributions per row (largest absolute log-odds contribution)."""
        x = self._matrix(df)
        contrib = self.booster.predict(x, pred_contrib=True)[:, :-1]
        out = []
        for i in range(len(x)):
            order = np.argsort(-np.abs(contrib[i]))[:k]
            rs = []
            for j in order:
                name, v, c = self.names[j], x[i, j], float(contrib[i, j])
                val = None if np.isnan(v) else float(v)
                if val is None:
                    text = f"{DESC.get(name, name)} is not available"
                else:
                    text = PHRASES.get(name, lambda z, n=name: f"{DESC.get(n, n)}: {z:g}")(val)
                rs.append(Reason(name, text, "raises" if c > 0 else "lowers", round(c, 4), val))
            out.append(rs)
        return out
