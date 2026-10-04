"""Disparity test on the held-out window (run: python -m decisioning.fairness).

Protected attributes and proxy-risk fields are NEVER model inputs. They are used here, for evaluation only, to
check whether the model's behaviour differs across groups (the README: "included only to test fairness").

Per group: n, mean predicted risk, observed break rate, calibration gap (predicted - observed), and the share of
promises landing in the high / very_high bands (the bands that trigger calls). A group is flagged when
  |its calibration gap - the overall gap| > GAP_THRESHOLD   or   |its high-band share - overall| > BAND_THRESHOLD
and it has at least MIN_N promises. Thresholds are ASSUMPTIONS (the design says 'policy threshold, TBC').
"""
from __future__ import annotations

from datetime import datetime

import polars as pl

from . import config, features as F
from .actions import band_for
from .records import RecordStore

GAP_THRESHOLD = 0.05
BAND_THRESHOLD = 0.05
MIN_N = 500

PROXY = ["province_code", "preferred_language", "urban_rural"]
PROTECTED = ["gender_code", "age_band", "marital_status", "citizenship_status", "newcomer_program_flag", "vulnerability_flag"]


def run() -> pl.DataFrame:
    con = F.connect()
    cols = ", ".join(f"u.{c}" for c in PROXY + PROTECTED)
    df = con.execute(f"select t.ptp_id, t.risk, t.y, {cols} from ptp_test_scores t "
                     f"join raw_customers u on u.crm_customer_id = t.crm_customer_id").pl()
    df = df.with_columns(pl.col("risk").map_elements(lambda r: band_for(r, config.BANDS), return_dtype=pl.String).alias("band"))
    df = df.with_columns((pl.col("band").is_in(["high", "very_high"])).cast(pl.Float64).alias("high_band"))
    overall_gap = float(df["risk"].mean() - df["y"].mean())
    overall_high = float(df["high_band"].mean())
    rows = []
    for kind, fields in (("proxy", PROXY), ("protected", PROTECTED)):
        for f in fields:
            g = df.with_columns(pl.col(f).cast(pl.String).fill_null("unknown")).group_by(f).agg(
                pl.len().alias("n"), pl.col("risk").mean().alias("mean_risk"), pl.col("y").mean().alias("observed"),
                pl.col("high_band").mean().alias("high_band_share")).filter(pl.col("n") >= MIN_N)
            for r in g.iter_rows(named=True):
                gap = r["mean_risk"] - r["observed"]
                flag = abs(gap - overall_gap) > GAP_THRESHOLD or abs(r["high_band_share"] - overall_high) > BAND_THRESHOLD
                rows.append({"kind": kind, "field": f, "group": r[f], "n": r["n"], "mean_risk": round(r["mean_risk"], 4),
                             "observed": round(r["observed"], 4), "calibration_gap": round(gap, 4),
                             "high_band_share": round(r["high_band_share"], 4), "flagged": flag})
    out = pl.DataFrame(rows)
    store = RecordStore()
    store.con.execute("create table if not exists fairness_tests (run_at timestamp, kind varchar, field varchar, grp varchar, "
                      "n bigint, mean_risk double, observed double, calibration_gap double, high_band_share double, flagged boolean, "
                      "model_version varchar)")
    now = datetime.now()
    for r in rows:
        store.con.execute("insert into fairness_tests values (?,?,?,?,?,?,?,?,?,?,?)",
                          [now, r["kind"], r["field"], str(r["group"]), r["n"], r["mean_risk"], r["observed"],
                           r["calibration_gap"], r["high_band_share"], r["flagged"], config.MODEL_VERSION])
    lines = [f"# Fairness / proxy disparity check (held-out window, model {config.MODEL_NAME} {config.MODEL_VERSION})", "",
             f"Overall: n={len(df):,}, calibration gap {overall_gap:+.3f}, share in high/very_high bands {overall_high:.1%}. "
             f"Flag rule: |group gap - overall gap| > {GAP_THRESHOLD} or |band share - overall| > {BAND_THRESHOLD}, n >= {MIN_N} "
             "(thresholds are assumptions). Protected attributes are used for this audit only, never as model inputs.", ""]
    for kind in ("proxy", "protected"):
        lines += [f"## {kind} fields", "", "| field | group | n | mean risk | observed | gap | high-band share | flagged |", "|---|---|---:|---:|---:|---:|---:|---|"]
        for r in rows:
            if r["kind"] == kind:
                lines.append(f"| {r['field']} | {r['group']} | {r['n']:,} | {r['mean_risk']:.3f} | {r['observed']:.3f} | "
                             f"{r['calibration_gap']:+.3f} | {r['high_band_share']:.1%} | {'**YES**' if r['flagged'] else 'no'} |")
        lines.append("")
    flagged = [r for r in rows if r["flagged"]]
    lines += ["## Result", "", f"{len(flagged)} of {len(rows)} groups flagged." +
              (" Flagged groups: " + "; ".join(f"{r['field']}={r['group']}" for r in flagged) + "." if flagged else ""),
              "Fields marked as proxies stay excluded from the model regardless of the result."]
    (config.ROOT / "docs" / "fairness_check.md").write_text("\n".join(lines), encoding="utf-8")
    return out


if __name__ == "__main__":
    res = run()
    print(res.filter(pl.col("flagged")))
    print(f"{res['flagged'].sum()} of {len(res)} groups flagged; report in docs/fairness_check.md")
