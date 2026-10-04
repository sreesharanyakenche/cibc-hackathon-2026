#!/usr/bin/env python3
"""
Reconcile the identity-matching metrics.  Read-only: does not run or change the matcher.

Adapted from the reference implementation (CIBC_Hackathon_CodeBlooded-main/scripts/validate_identity_metrics.py).
Inputs  : warehouse/c360.duckdb (id_identity_links, id_crm_clusters), warehouse/maple.duckdb (raw_contact_history, raw_promises_to_pay)
Outputs : outputs/IDENTITY_METRICS.md, outputs/identity_metrics.json

Definitions (stated once, used everywhere)
  Account            one row of card_accounts / loan_accounts / deposit_accounts (1,844,500 in total).
  Candidate (tier X) an account whose best identity candidate has tier X (A..E); '-' = no candidate (unmatched).
  Auto-linked        link_status in (linked, linked_with_alternatives): tier A-C and a unique best customer.
  Withheld           link_status in (ambiguous, review): a candidate exists but was deliberately not attributed.
  Ground truth       The collections system's own account -> CRM-customer record: an account that appears in contact_history or
                     promises_to_pay with exactly ONE distinct, non-orphan crm_customer_id.  It is independent of the matcher
                     (no identity signal from names/DOB/phone/postal was used to create it), but it is NOT an audited gold label.
  Evaluable          accounts that have such a ground truth (a subset: accounts that were ever contacted / promised).
  Correct            predicted customer_key == customer_key of the ground-truth CRM id (customer_key = anchor of the CRM duplicate cluster).
  Precision          correct auto-links / auto-linked accounts, both restricted to evaluable accounts.
  Recall             correct auto-links / evaluable accounts (overall; per-tier recall is reported as 'share of evaluable accounts
                     correctly linked at this tier' because ground truth has no tier).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from data_pipeline.config import MAIN_DB, ROOT, WAREHOUSE

REPORTS = ROOT / "outputs"

con = duckdb.connect()
con.execute(f"ATTACH '{MAIN_DB.as_posix()}' AS src (READ_ONLY)")
con.execute(f"ATTACH '{(WAREHOUSE / 'c360.duckdb').as_posix()}' AS out (READ_ONLY)")
con.execute("CREATE VIEW links AS SELECT * FROM out.id_identity_links")
con.execute("CREATE VIEW clusters AS SELECT * FROM out.id_crm_clusters")
# a collections row whose CRM id is not a CRM record at all is an orphan reference: not usable as ground truth
con.execute("""
CREATE TABLE truth_pairs AS
SELECT h.account_id, h.crm_customer_id FROM src.raw_contact_history h JOIN clusters c ON c.crm_customer_id = h.crm_customer_id WHERE h.account_id IS NOT NULL
UNION ALL
SELECT p.account_id, p.crm_customer_id FROM src.raw_promises_to_pay p JOIN clusters c ON c.crm_customer_id = p.crm_customer_id WHERE p.account_id IS NOT NULL""")
con.execute("""
CREATE TABLE truth AS
SELECT account_id, min(crm_customer_id) AS truth_crm FROM truth_pairs GROUP BY 1 HAVING count(DISTINCT crm_customer_id) = 1""")
n_conflicting_truth = con.execute("SELECT count(*) FROM (SELECT account_id FROM truth_pairs GROUP BY 1 HAVING count(DISTINCT crm_customer_id) > 1)").fetchone()[0]
con.execute("""
CREATE TABLE ev AS
SELECT l.source_system, l.account_id, coalesce(l.tier, '-') AS tier, l.link_status,
       (l.link_status IN ('linked','linked_with_alternatives')) AS auto,
       (l.link_status IN ('ambiguous','review')) AS withheld,
       t.truth_crm IS NOT NULL AS evaluable,
       (l.customer_key IS NOT NULL AND l.customer_key = tc.customer_key) AS correct,
       tc.cluster_size AS truth_cluster_size
FROM links l LEFT JOIN truth t USING (account_id) LEFT JOIN clusters tc ON tc.crm_customer_id = t.truth_crm""")

q = lambda sql: con.execute(sql).fetchall()
total = q("SELECT count(*) FROM ev")[0][0]
evaluable = q("SELECT count(*) FROM ev WHERE evaluable")[0][0]

rows = []
for tier, in q("SELECT DISTINCT tier FROM ev ORDER BY 1"):
    r = q(f"""SELECT count(*), count(*) FILTER (WHERE auto), count(*) FILTER (WHERE withheld),
                     count(*) FILTER (WHERE evaluable), count(*) FILTER (WHERE evaluable AND auto),
                     count(*) FILTER (WHERE evaluable AND auto AND correct), count(*) FILTER (WHERE evaluable AND withheld),
                     count(*) FILTER (WHERE evaluable AND withheld AND correct)
              FROM ev WHERE tier = '{tier}'""")[0]
    cand, auto, wh, ev_n, ev_auto, ev_ok, ev_wh, ev_wh_ok = r
    rows.append(dict(tier=tier, candidates=cand, auto_linked=auto, withheld=wh, evaluable=ev_n, evaluable_auto_linked=ev_auto,
                     correct=ev_ok, precision_pct=round(100 * ev_ok / ev_auto, 2) if ev_auto else None,
                     share_of_evaluable_correctly_linked_pct=round(100 * ev_ok / evaluable, 2),
                     evaluable_withheld=ev_wh, withheld_would_have_been_correct=ev_wh_ok,
                     withheld_candidate_accuracy_pct=round(100 * ev_wh_ok / ev_wh, 2) if ev_wh else None))

tot = q("""SELECT count(*) FILTER (WHERE auto), count(*) FILTER (WHERE withheld), count(*) FILTER (WHERE link_status='unmatched'),
                  count(*) FILTER (WHERE evaluable), count(*) FILTER (WHERE evaluable AND auto),
                  count(*) FILTER (WHERE evaluable AND auto AND correct), count(*) FILTER (WHERE evaluable AND withheld),
                  count(*) FILTER (WHERE evaluable AND link_status='unmatched') FROM ev""")[0]
overall = dict(accounts=total, auto_linked=tot[0], withheld=tot[1], unmatched=tot[2],
               auto_link_rate_all_accounts_pct=round(100 * tot[0] / total, 2),
               evaluable_accounts=tot[3], evaluable_auto_linked=tot[4], evaluable_correct=tot[5], evaluable_withheld=tot[6], evaluable_unmatched=tot[7],
               auto_link_rate_evaluable_pct=round(100 * tot[4] / tot[3], 2), precision_evaluable_pct=round(100 * tot[5] / tot[4], 2),
               recall_evaluable_pct=round(100 * tot[5] / tot[3], 2), accounts_with_conflicting_truth_excluded=n_conflicting_truth)

# strict view: truth CRM record is a singleton cluster, i.e. the result cannot depend on any CRM merge decision
strict = q("""SELECT count(*), count(*) FILTER (WHERE auto), count(*) FILTER (WHERE auto AND correct) FROM ev WHERE evaluable AND truth_cluster_size = 1""")[0]
merged = q("""SELECT count(*), count(*) FILTER (WHERE auto), count(*) FILTER (WHERE auto AND correct) FROM ev WHERE evaluable AND truth_cluster_size > 1""")[0]
views = {
    "truth_crm_record_is_singleton (independent of CRM merges)": dict(evaluable=strict[0], auto_linked=strict[1], correct=strict[2], precision_pct=round(100 * strict[2] / strict[1], 2)),
    "truth_crm_record_is_in_a_merged_cluster": dict(evaluable=merged[0], auto_linked=merged[1], correct=merged[2], precision_pct=round(100 * merged[2] / merged[1], 2)),
}
by_sys = {}
for s, in q("SELECT DISTINCT source_system FROM ev ORDER BY 1"):
    r = q(f"SELECT count(*), count(*) FILTER (WHERE evaluable), count(*) FILTER (WHERE evaluable AND auto), count(*) FILTER (WHERE evaluable AND auto AND correct), "
          f"count(*) FILTER (WHERE auto) FROM ev WHERE source_system='{s}'")[0]
    by_sys[s] = dict(accounts=r[0], auto_linked_all=r[4], evaluable=r[1], evaluable_auto_linked=r[2], correct=r[3], precision_pct=round(100 * r[3] / r[2], 2) if r[2] else None)

# representativeness of the evaluation population
rep = []
for tier, in q("SELECT DISTINCT tier FROM ev ORDER BY 1"):
    a, b = q(f"SELECT count(*), count(*) FILTER (WHERE evaluable) FROM ev WHERE tier='{tier}'")[0]
    rep.append(dict(tier=tier, share_of_all_accounts_pct=round(100 * a / total, 2), share_of_evaluable_accounts_pct=round(100 * b / evaluable, 2)))

out = dict(generated_utc=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"), by_tier=rows, overall=overall, strict_views=views,
           by_source_system=by_sys, representativeness=rep)
(REPORTS / "identity_metrics.json").write_text(json.dumps(out, indent=1))

n = lambda x: f"{int(x):,}"
L = []
w = L.append
w("# Identity matching: metric reconciliation\n")
w(f"*Generated {out['generated_utc']} UTC by `data_pipeline/identity_validate.py`. The matcher was not run or modified.*\n")
w("## What is measured\n")
w("- **Population:** " + n(total) + " source accounts (cards, loans, deposits). The matcher assigns each account to a `customer_key` or withholds it.")
w("- **Ground truth:** the collections system's own account -> CRM-customer record (accounts that appear in `contact_history` or `promises_to_pay` with exactly one non-orphan CRM id). "
  f"It was created without any name/DOB/phone/postal matching, so it is independent of the matcher, but it is **not an audited gold label**. {n(n_conflicting_truth)} accounts with conflicting CRM ids were excluded.")
w(f"- **Evaluation population:** the {n(evaluable)} accounts that have a ground truth ({100*evaluable/total:.1f}% of all accounts) — accounts that were contacted or promised on, i.e. delinquent/collections-active accounts. **This is not a random sample of all accounts.**")
w("- **Correct:** predicted `customer_key` equals the `customer_key` of the ground-truth CRM id (the anchor of its duplicate cluster).")
w("- **Precision** = correct auto-links ÷ auto-linked accounts (both within the evaluation population). **Recall** = correct auto-links ÷ all evaluable accounts.\n")
w("## Headline numbers and their denominators\n")
o = overall
w("| Metric | Value | Numerator / denominator |")
w("|---|---:|---|")
w(f"| Auto-link rate, all accounts | {o['auto_link_rate_all_accounts_pct']}% | {n(o['auto_linked'])} / {n(o['accounts'])} |")
w(f"| Auto-link rate, evaluable accounts | {o['auto_link_rate_evaluable_pct']}% | {n(o['evaluable_auto_linked'])} / {n(o['evaluable_accounts'])} |")
w(f"| **Precision of auto-links (evaluable)** | **{o['precision_evaluable_pct']}%** | {n(o['evaluable_correct'])} / {n(o['evaluable_auto_linked'])} |")
w(f"| Recall (evaluable) | {o['recall_evaluable_pct']}% | {n(o['evaluable_correct'])} / {n(o['evaluable_accounts'])} |")
w(f"| Withheld (candidate exists, not attributed) | {n(o['withheld'])} accounts | {n(o['evaluable_withheld'])} of them evaluable |")
w(f"| Unmatched (no candidate; mostly no DOB to block on) | {n(o['unmatched'])} accounts | {n(o['evaluable_unmatched'])} of them evaluable |\n")
w("## By tier\n")
w("| Tier | Candidates | Auto-linked | Withheld | Evaluable & auto-linked | Correct | Precision | Share of evaluable correctly linked here (recall contribution) | Evaluable & withheld | …would have been correct | Ground-truth source |")
w("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|")
for r in rows:
    prec = f"{r['precision_pct']}%" if r["precision_pct"] is not None else "n/a (none auto-linked)"
    wc = f"{n(r['withheld_would_have_been_correct'])} ({r['withheld_candidate_accuracy_pct']}%)" if r["evaluable_withheld"] else "–"
    w(f"| {r['tier']} | {n(r['candidates'])} | {n(r['auto_linked'])} | {n(r['withheld'])} | {n(r['evaluable_auto_linked'])} | {n(r['correct'])} | {prec} | {r['share_of_evaluable_correctly_linked_pct']}% | {n(r['evaluable_withheld'])} | {wc} | collections account->CRM pairs |")
w("\nTier `-` = no candidate. A, B and C are auto-link tiers; D and E are always withheld (`review`); an A-C account is also withheld (`ambiguous`) when two different customers tie for the best tier.\n")
w("### Why several numbers read 99.84%\n")
w("Headline figures can coincide when rounded; they are different fractions: "
  f"all auto-links ({n(o['evaluable_correct'])}/{n(o['evaluable_auto_linked'])}), tier A, and tier E "
  f"({n(next(r for r in rows if r['tier']=='E')['withheld_would_have_been_correct'])}/{n(next(r for r in rows if r['tier']=='E')['evaluable_withheld'])}). "
  "**Tier E is not auto-linked**: its figure is the accuracy of the withheld candidate (what would have been correct if released), not a precision of linked accounts, and it rests on a small sample.\n")
w("## Robustness checks\n")
w("| Evaluation slice | Evaluable | Auto-linked | Correct | Precision |")
w("|---|---:|---:|---:|---:|")
for k, v in views.items():
    w(f"| {k} | {n(v['evaluable'])} | {n(v['auto_linked'])} | {n(v['correct'])} | {v['precision_pct']}% |")
for s, v in by_sys.items():
    w(f"| source system {s} | {n(v['evaluable'])} | {n(v['evaluable_auto_linked'])} | {n(v['correct'])} | {v['precision_pct']}% |")
w("\n## Is the evaluation population representative?\n")
w("| Tier | Share of all accounts | Share of evaluable accounts |")
w("|---|---:|---:|")
for r in rep:
    w(f"| {r['tier']} | {r['share_of_all_accounts_pct']}% | {r['share_of_evaluable_accounts_pct']}% |")
w("\nIf these two columns differ materially, precision measured on evaluable accounts may not transfer to the remaining accounts. "
  "Accounts outside the evaluation population (never contacted) have **no ground truth**, so their precision is unmeasured.\n")
w("## How to cite these numbers\n")
w(f"Use: *\"On the {n(evaluable)} accounts that have a collections-recorded customer link, {o['precision_evaluable_pct']}% of auto-linked accounts "
  f"({n(o['evaluable_correct'])}/{n(o['evaluable_auto_linked'])}) were linked to the correct customer; {o['recall_evaluable_pct']}% of those accounts were correctly linked.\"* "
  "Do not quote the precision without the evaluation population.\n")
(REPORTS / "IDENTITY_METRICS.md").write_text("\n".join(L) + "\n")
print(json.dumps(overall, indent=1))
for r in rows:
    print(r["tier"], r["candidates"], r["auto_linked"], r["withheld"], r["evaluable_auto_linked"], r["correct"], r["precision_pct"])
print(views)
print(rep)
