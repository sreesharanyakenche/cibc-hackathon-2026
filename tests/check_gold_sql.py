"""Run the organisers' reference SQL (benchmark_dev_answers.csv) against our raw_* tables
and compare with the gold answer text. Validates the loader before we build on it."""
import csv, io, sys
from pathlib import Path
import duckdb
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "data_pipeline"))
from config import LABELS, MAIN_DB

con = duckdb.connect()
con.execute(f"attach '{MAIN_DB.as_posix()}' as src (read_only)")
tables = [r[0] for r in con.execute("select table_name from information_schema.tables where table_catalog='src' and table_name like 'raw_%'").fetchall()]
con.execute("create schema gold")
for t in tables:
    con.execute(f"create view gold.{t[4:]} as select * from src.main.{t}")
con.execute("set schema='gold'")

def _num(x):
    try:
        return float(x)
    except ValueError:
        return None

def _eq(a, b):
    fa, fb = _num(a), _num(b)
    return abs(fa - fb) < 0.06 if fa is not None and fb is not None else a == b

def norm(rows):
    return [[str(round(float(x),1)) if isinstance(x,(int,float)) and not isinstance(x,bool) and not float(x).is_integer() else str(int(x)) if isinstance(x,(int,float)) and not isinstance(x,bool) else str(x) for x in r] for r in rows]

ok = bad = 0
for r in csv.DictReader(open(LABELS / "benchmark_dev_answers.csv", encoding="utf-8")):
    if not r["gold_sql_reference"]:
        continue
    cur = con.execute(r["gold_sql_reference"])
    got = norm(cur.fetchall())
    gold = list(csv.reader(io.StringIO(r["gold_answer"])))[1:]
    gold = [[c.strip() for c in row] for row in gold]
    match = all(_eq(a, b) for ga, gb in zip(got, gold) for a, b in zip(ga, gb)) and len(got) == len(gold)
    print(r["question_id"], "OK" if match else "MISMATCH", len(got), len(gold))
    if not match:
        print("  got ", got[:4]); print("  gold", gold[:4])
    ok += match; bad += not match
print(f"{ok} ok, {bad} mismatched")
