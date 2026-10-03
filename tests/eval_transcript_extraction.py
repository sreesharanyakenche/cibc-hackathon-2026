"""Evaluate the rule-based transcript extractor against the 500 gold-labelled transcripts."""
import csv, json, sys, collections
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from data_pipeline.config import LABELS, RELEASE
from insight import executor
from insight.transcripts import RuleSummarizer, load_transcript

con = executor.connect()
files = dict(con.execute("select transcript_id, file_path from cur_transcripts").fetchall())
rows = list(csv.DictReader(open(LABELS / "call_transcripts_labels_public_500.csv", encoding="utf-8")))
S = RuleSummarizer()
n = collections.Counter(); ok = collections.Counter(); confusion = collections.defaultdict(collections.Counter)
bad = []
for r in rows:
    ex = S.summarize(load_transcript(files[r["transcript_id"]]))
    gold_hard = r["label_hardship"] in ("clear", "possible")
    n["hardship"] += 1; ok["hardship"] += (ex.hardship == gold_hard)
    if gold_hard:
        n["reason"] += 1; ok["reason"] += (ex.reason_code == r["label_hardship_reason"])
        confusion[r["label_hardship_reason"]][ex.reason_code] += 1
    n["next_step"] += 1; ok["next_step"] += (ex.next_step == r["label_next_step"])
    confusion["_next:" + r["label_next_step"]][ex.next_step] += 1
    if r["label_ptp_mentioned"] == "true":
        n["ptp"] += 1
        good = ex.ptp_amount is not None and abs(ex.ptp_amount - float(r["label_ptp_amount"])) < 0.02 and \
            r["label_ptp_date"][5:7] == f"{ex.ptp_month:02d}" and int(r["label_ptp_date"][8:]) == ex.ptp_day
        ok["ptp"] += good
        if not good and len(bad) < 5: bad.append((r["transcript_id"], r["label_ptp_amount"], r["label_ptp_date"], ex.ptp_amount, ex.ptp_month, ex.ptp_day))
    else:
        n["ptp_absent"] += 1; ok["ptp_absent"] += (ex.ptp_amount is None)
    n["cease"] += 1; ok["cease"] += (ex.cease == (r["label_cease_request"] == "true"))
    n["dispute"] += 1; ok["dispute"] += (ex.dispute == (r["label_dispute"] == "true"))
for k in n: print(f"{k:12s} {ok[k]}/{n[k]} = {100*ok[k]/n[k]:.1f}%")
for g, c in confusion.items(): print(g, dict(c))
print("ptp misses", bad)
