"""Run the benchmark questions through the engine, write the submission CSV, and
(optionally) score the dev split against the organisers' gold answers.

    python -m insight.benchmark --out outputs/benchmark_answers.csv --score

The submission CSV has exactly the template columns: question_id, answer, sql_or_sources, refused.
The same code path runs for every question (no hand edits), including the extra questions
released at 19:00 IST on 4 Oct: pass them with --questions <csv in the same format>.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from pathlib import Path

from data_pipeline.config import DATA, LABELS, ROOT
from .engine import Engine


def run(questions_csv: Path, out_csv: Path, engine: Engine | None = None) -> list[dict]:
    engine = engine or Engine()
    results = []
    for q in csv.DictReader(open(questions_csv, encoding="utf-8")):
        a = engine.ask(q["question_text"])
        results.append({"question_id": q["question_id"], "answer": a.answer,
                        "sql_or_sources": a.sql_or_sources, "refused": str(a.refused).lower(),
                        "_route": a.route, "_plan": a.plan or ""})
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(out_csv, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["question_id", "answer", "sql_or_sources", "refused"], extrasaction="ignore")
        w.writeheader()
        w.writerows(results)
    return results


# ----------------------------------------------------------------------------- scoring (dev only)

def _tolerance(text: str) -> float | None:
    t = (text or "").strip().lower()
    if not t:
        return 0.06
    if t == "exact":
        return 0.0
    m = re.search(r"±\s*([\d.]+)", t)
    return float(m.group(1)) if m else None


def _num(x: str):
    try:
        return float(x.replace(",", ""))
    except ValueError:
        return None


def _table(text: str) -> list[list[str]]:
    block = text.split("\n\n")[0]
    return [[c.strip() for c in r] for r in csv.reader(io.StringIO(block))]


def score_dev(results: list[dict]) -> list[tuple[str, str, str]]:
    qmeta = {r["question_id"]: r for r in csv.DictReader(open(DATA / "benchmark_questions.csv", encoding="utf-8"))}
    got = {r["question_id"]: r for r in results}
    report = []
    for g in csv.DictReader(open(LABELS / "benchmark_dev_answers.csv", encoding="utf-8")):
        qid = g["question_id"]
        r = got.get(qid)
        if r is None:
            report.append((qid, "MISSING", "")); continue
        gold = g["gold_answer"]
        if gold.lower().startswith("refuse"):
            ok = r["refused"] == "true"
            report.append((qid, "PASS" if ok else "FAIL", "expected refusal" if not ok else "refused")); continue
        if r["refused"] == "true":
            report.append((qid, "FAIL", "refused a valid question")); continue
        if g["gold_sql_reference"]:
            tol = _tolerance(qmeta[qid]["tolerance"])
            a, b = _table(r["answer"]), _table(gold)
            if len(a) != len(b):
                report.append((qid, "FAIL", f"rows {len(a)-1} vs gold {len(b)-1}")); continue
            bad = 0
            for ra, rb in zip(a[1:], b[1:]):
                for x, y in zip(ra, rb):
                    nx, ny = _num(x), _num(y)
                    if nx is not None and ny is not None:
                        bad += abs(nx - ny) > (tol if tol is not None else 0.06)
                    else:
                        bad += x != y
            report.append((qid, "PASS" if not bad else "FAIL", "" if not bad else f"{bad} cells outside tolerance {tol}"))
        else:
            sources = json.loads(g["gold_sources"]) if g["gold_sources"] else []
            cited = all(s.split()[0] in r["sql_or_sources"] or s in r["sql_or_sources"] for s in sources) if sources else True
            nums = re.findall(r"\d+(?:\.\d+)?", re.sub(r"[§$]", " ", gold))
            facts = all(n in r["answer"] for n in nums)
            report.append((qid, "PASS" if cited and facts else "FAIL", f"cited={cited} key_numbers_present={facts}"))
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", default=str(DATA / "benchmark_questions.csv"))
    ap.add_argument("--out", default=str(ROOT / "outputs" / "benchmark_answers.csv"))
    ap.add_argument("--score", action="store_true", help="score the dev split against labels/benchmark_dev_answers.csv")
    args = ap.parse_args(argv)
    results = run(Path(args.questions), Path(args.out))
    print(f"wrote {len(results)} answers to {args.out}")
    if args.score:
        rep = score_dev(results)
        for qid, status, why in rep:
            print(f"  {qid} {status} {why}")
        print(f"dev: {sum(s == 'PASS' for _, s, _ in rep)}/{len(rep)} pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
