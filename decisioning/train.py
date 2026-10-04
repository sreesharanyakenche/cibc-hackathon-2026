"""Train, calibrate and evaluate the PTP-break model.

    python -m decisioning.train

Label:   broken = 1; kept and partially_kept = 0; open and cancelled promises are dropped
         (certified metric definition: resolved = kept, partially_kept, broken).
Split:   by due date. train <= 2026-06-30 < valid <= 2026-08-15 < test. Break rate drifts from ~33% to ~59%
         during 2026, so calibration is fitted on the validation window that sits next to the test window.
Model:   LightGBM, monotone constraints on prior-broken counts, isotonic calibration (stored as JSON, no pickle).
Baseline: the incumbent `ptp_break v2.1` scores in model_scores, compared on the same test promises.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

import lightgbm as lgb
import numpy as np
import polars as pl
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import average_precision_score, brier_score_loss, log_loss, roc_auc_score

from . import config, features as F

PARAMS = dict(
    objective="binary", metric="auc", learning_rate=0.05, num_leaves=31, min_data_in_leaf=200,
    feature_fraction=0.8, bagging_fraction=0.8, bagging_freq=1, lambda_l2=5.0, verbose=-1, seed=7,
    num_threads=8,
)
MONOTONE = {"prior_broken_ptp_count": 1, "cust_prior_broken_rate": 1}   # more broken promises never lowers risk

# Excluded from the production model: open promises almost never have ratio 1.0 while recently kept ones do, so the
# amount fields look generated after the outcome. Trained here only as a labelled REFERENCE to quantify the effect.
AMOUNT_FEATURES = ["ptp_amount", "ptp_to_overdue_ratio", "overdue_at_creation", "entry_overdue"]
_MODEL = set(F.MODEL_FEATURE_NAMES)
GROUPS = {
    "customer_only": [n for n in ["cust_prior_resolved", "cust_prior_broken_rate", "cust_days_since_last_broken", "acct_max_dpd",
                                  "acct_delinquent_n", "acct_utilisation", "acct_paid_vs_min", "acct_n_accounts", "sal_avg_3m",
                                  "sal_gap_months_3m", "sal_months_seen_3m", "cust_tenure_months", "case_age_days", "entry_dpd"]
                      if n in _MODEL],
    "promise_only": [n for n in ["days_to_due", "dpd_at_creation", "prior_broken_ptp_count", "ptp_sequence_in_case",
                                 "is_instalment", "channel_code", "payment_method_code", "product_code", "reminder_scheduled",
                                 "has_income_date", "is_self_serve", "due_dow", "due_dom", "created_hour"] if n in _MODEL],
}
TEXT = [f.name for f in F.MODEL_FEATURES if f.name.startswith("t_")]
CONTACT = [f.name for f in F.MODEL_FEATURES if f.name.startswith("case_") and f.name != "case_age_days"]


def load_dataset(con) -> pl.DataFrame:
    df = F.compute_features(con, "p.is_resolved")
    lab = con.execute("select ptp_id, cast(is_broken as int) as y from cur_promises where is_resolved").pl()
    df = df.join(lab, on="ptp_id")
    num = [pl.col(f.name).cast(pl.Float64) for f in F.FEATURES]
    return df.with_columns(num)


def split(df: pl.DataFrame):
    d = df["ptp_due_date"]
    tr = df.filter(d <= config.TRAIN_END)
    va = df.filter((d > config.TRAIN_END) & (d <= config.VALID_END))
    te = df.filter(d > config.VALID_END)
    return tr, va, te


def xy(df: pl.DataFrame, names: list[str]):
    return df.select(names).to_numpy().astype(float), df["y"].to_numpy()


def fit(tr, va, names: list[str], rounds: int = 1500):
    cat = [names.index(c) for c in F.CATEGORICAL if c in names]
    mono = [MONOTONE.get(n, 0) for n in names]
    xtr, ytr = xy(tr, names)
    xva, yva = xy(va, names)
    dtr = lgb.Dataset(xtr, ytr, feature_name=names, categorical_feature=cat, free_raw_data=False)
    dva = lgb.Dataset(xva, yva, reference=dtr)
    params = dict(PARAMS, monotone_constraints=mono)
    return lgb.train(params, dtr, rounds, valid_sets=[dva],
                     callbacks=[lgb.early_stopping(50, verbose=False)])


def calibration_table(y, p, bins: int = 10):
    order = np.argsort(p)
    out = []
    for chunk in np.array_split(order, bins):
        out.append({"mean_pred": round(float(p[chunk].mean()), 4), "observed": round(float(y[chunk].mean()), 4), "n": int(len(chunk))})
    ece = sum(abs(r["mean_pred"] - r["observed"]) * r["n"] for r in out) / len(y)
    return out, round(float(ece), 4)


def main() -> None:
    t0 = time.time()
    con = F.connect()
    F.build_derived_tables(con)
    dups = F.duplicate_keys(con)
    assert not any(dups.values()), f"derived tables have duplicate keys (non-deterministic as-of joins): {dups}"
    df = load_dataset(con)
    assert df["ptp_id"].n_unique() == len(df), "feature rows are not unique per promise"
    tr, va, te = split(df)
    print(f"rows: train {len(tr):,} valid {len(va):,} test {len(te):,}  ({time.time() - t0:.0f}s)")
    print("broken rate: train %.3f valid %.3f test %.3f" % (tr["y"].mean(), va["y"].mean(), te["y"].mean()))

    # ---- ablation (tests H1: does scoring the promise beat scoring the customer?) -----------------
    abl = {}
    sets = {"customer_only": GROUPS["customer_only"], "promise_only": GROUPS["promise_only"],
            "promise+contact": GROUPS["promise_only"] + CONTACT,
            "promise+customer": GROUPS["promise_only"] + GROUPS["customer_only"] + CONTACT,
            "all_without_text": [n for n in F.MODEL_FEATURE_NAMES if n not in TEXT],
            "all": F.MODEL_FEATURE_NAMES,
            "REFERENCE_with_amount_features": F.MODEL_FEATURE_NAMES + AMOUNT_FEATURES}
    boosters = {}
    for name, cols in sets.items():
        b = fit(tr, va, cols)
        boosters[name] = b
        xte, yte = xy(te, cols)
        abl[name] = round(float(roc_auc_score(yte, b.predict(xte, num_iteration=b.best_iteration))), 4)
        print(f"  ablation {name:18s} test AUC {abl[name]}  (iters {b.best_iteration})")

    inc = con.execute("select ptp_id, score from maple.main.raw_model_scores where model_name = 'ptp_break'").pl()
    variants = {"": ("all", F.MODEL_FEATURE_NAMES),
                "_reference_amounts": ("REFERENCE_with_amount_features", sets["REFERENCE_with_amount_features"])}
    for suffix, (key, names) in variants.items():
        package(con, tr, va, te, boosters[key], names, suffix, abl, inc)
    print("done in %.0fs" % (time.time() - t0))


def package(con, tr, va, te, booster, names, suffix, abl, inc) -> dict:
    """Calibrate, evaluate, and save one model variant (model text + JSON metadata)."""
    xva, yva = xy(va, names)
    xte, yte = xy(te, names)
    raw_va = booster.predict(xva, num_iteration=booster.best_iteration)
    raw_te = booster.predict(xte, num_iteration=booster.best_iteration)
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.001, y_max=0.999).fit(raw_va, yva)
    cal_te = iso.predict(raw_te)
    table_cal, ece_cal = calibration_table(yte, cal_te)
    _, ece_raw = calibration_table(yte, raw_te)
    metrics = {
        "test_auc": round(float(roc_auc_score(yte, raw_te)), 4),
        "test_pr_auc": round(float(average_precision_score(yte, cal_te)), 4),
        "test_logloss_calibrated": round(float(log_loss(yte, np.clip(cal_te, 1e-6, 1 - 1e-6))), 4),
        "test_brier_calibrated": round(float(brier_score_loss(yte, cal_te)), 4),
        "ece_raw": ece_raw, "ece_calibrated": ece_cal,
        "test_base_rate": round(float(yte.mean()), 4), "valid_base_rate": round(float(yva.mean()), 4),
        "train_base_rate": round(float(tr["y"].mean()), 4),
    }
    jt = te.select(["ptp_id", "y"]).join(inc, on="ptp_id")
    if len(jt) > 1000:
        ours = dict(zip(te["ptp_id"].to_list(), cal_te))
        metrics["incumbent_v2_1_test_auc"] = round(float(roc_auc_score(jt["y"].to_numpy(), jt["score"].to_numpy())), 4)
        metrics["ours_on_incumbent_subset_auc"] = round(float(roc_auc_score(
            jt["y"].to_numpy(), np.array([ours[i] for i in jt["ptp_id"].to_list()]))), 4)
        metrics["incumbent_n"] = len(jt)
    imp = sorted(zip(names, booster.feature_importance("gain")), key=lambda x: -x[1])
    total = sum(g for _, g in imp) or 1
    importance = [{"feature": n, "gain_share": round(float(g / total), 4)} for n, g in imp]
    version = config.MODEL_VERSION + suffix
    config.MODEL_DIR.mkdir(exist_ok=True)
    stem = config.MODEL_DIR / f"{config.MODEL_NAME}_{version}"
    booster.save_model(str(stem) + ".txt", num_iteration=booster.best_iteration)
    meta = {
        "model_name": config.MODEL_NAME, "model_version": version, "feature_version": config.FEATURE_VERSION,
        "trained_at": datetime.now().isoformat(timespec="seconds"), "features": names, "categorical": F.CATEGORICAL,
        "monotone": MONOTONE, "best_iteration": booster.best_iteration,
        "split": {"train_end": str(config.TRAIN_END), "valid_end": str(config.VALID_END),
                  "n_train": len(tr), "n_valid": len(va), "n_test": len(te)},
        "calibration": {"method": "isotonic", "x": [float(v) for v in iso.X_thresholds_],
                        "y": [float(v) for v in iso.y_thresholds_]},
        "metrics": metrics, "ablation_test_auc": abl, "calibration_table_test": table_cal, "importance": importance,
        "uses_protected_attributes": False,
        "reference_only": bool(suffix), "reference_note": (
            "Trained WITH the amount features (excluded from production: they look generated after the outcome). "
            "Never used for decisions; kept to quantify the effect." if suffix else None),
    }
    Path(str(stem) + ".json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    if not suffix:    # held-out predictions of the primary model, for fairness / action-mix analysis
        out = te.select(["ptp_id", "case_id", "crm_customer_id", "ptp_due_date", "y"]).with_columns(
            pl.Series("risk", cal_te), pl.Series("risk_raw", raw_te))
        con.register("te_scores", out.to_arrow())
        con.execute("create or replace table ptp_test_scores as select * from te_scores")
    print(f"[{version}]", json.dumps(metrics))
    print("   top features:", [(d["feature"], d["gain_share"]) for d in importance[:6]])
    return metrics


if __name__ == "__main__":
    main()
