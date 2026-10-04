"""Policy / procedure retrieval ("RAG" without the generation risk).

* Corpus: the 19 documents in reference_documents (markdown), chunked by section.
* Version-aware: superseded documents are excluded unless the question asks for an older version.
* Authority-aware: policy > procedure > programme terms > regulatory > scripts > FAQ.
  (Benchmark traps: the FAQ and the old script v1.0 contradict the current policy.)
* Evidence gate: an answer needs enough of the question's meaningful (idf-weighted) terms
  to be present in the retrieved chunk. Otherwise we refuse ("no evidence, no answer").
* Extractive: the answer is built from the document's own sentences and always cites
  document + section. An LLM can later rewrite the extract (see llm.py), never replace the citation.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from rank_bm25 import BM25Okapi

from data_pipeline.config import DOCS_DIR, RELEASE

AUTHORITY = {
    "policy": 1.0, "procedure": 0.95, "hardship_programme_terms": 0.9, "regulatory_summary": 0.7,
    "incident_report": 0.6, "release_note": 0.6, "data_contract": 0.55, "call_script": 0.5, "faq": 0.35,
}
STOP = set("""a an the of to in on for and or is are was were be been it its this that these those with at by from as
can do does did we you i our us my your their they he she will would should could may might must have has had not no
what how when where which who whom why if then than so such there here any some about into over under per also
customer customers agent agents""".split())
SYN = {"sunday": ["sunday", "dimanche"], "quebec": ["québec", "quebec"], "québec": ["quebec"], "calling": ["appel"],
       "call": ["appel"], "calls": ["appel"], "holiday": ["holidays", "férié"], "sue": ["legal", "court", "lawsuit"], "suing": ["legal", "court"], "lawsuit": ["legal", "court"],
       "dncl": ["do", "not", "call"], "casl": ["casl", "consent"], "grace": ["grace"],
       "proposal": ["consumer", "proposal", "insolvency"], "bankrupt": ["bankruptcy", "insolvency"],
       "bankruptcy": ["bankruptcy", "insolvency"], "hours": ["hours", "permitted"],
       "attempts": ["attempts", "calls"], "reduced": ["reduced", "payment"], "cut": ["reduced", "hardship"],
       "fraudulent": ["fraud"], "fraudulently": ["fraud"], "unauthorised": ["fraud"], "unauthorized": ["fraud"], "scam": ["fraud"],
       "layoff": ["hardship", "job"], "laid": ["hardship", "job"]}


FR_EN = {"dimanche": "sunday", "appel": "call", "appels": "call", "québec": "quebec", "recouvrement": "collection",
         "aucun": "no", "fériés": "holiday", "férié": "holiday", "jours": "day", "heures": "hours", "permises": "permitted",
         "difficultés": "hardship", "faillite": "bankruptcy", "proposition": "proposal", "cesser": "cease",
         "paiement": "payment", "réduit": "reduced", "mois": "month", "français": "french", "langue": "language",
         "tentatives": "attempts", "fréquence": "frequency", "insolvabilité": "insolvency", "clients": "customer",
         "vulnérabilité": "vulnerability", "service": "service", "offert": "offered"}


def _stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def tokenize(text: str) -> list[str]:
    toks = [t for t in re.findall(r"[a-zà-ÿ0-9]+", text.lower())]
    return [_stem(t) for t in toks if t not in STOP and len(t) > 1]


def expand_query(q: str) -> list[str]:
    out = tokenize(q)
    for w in re.findall(r"[a-z]+", q.lower()):
        for extra in SYN.get(w, []):
            out.append(_stem(extra))
    return out


@dataclass
class Chunk:
    doc_id: str          # citation id, e.g. POL-COLL-001
    section: str         # e.g. §5.2, Q2
    title: str
    text: str
    version: str
    doc_type: str
    language: str
    superseded: bool
    heading: str = ""
    tokens: list[str] = field(default_factory=list)

    @property
    def cite(self) -> str:
        return f"{self.doc_id} {self.section}".strip()


@dataclass
class Hit:
    chunk: Chunk
    score: float        # BM25 x document authority x language weight (used for ranking)
    coverage: float     # idf-weighted share of the question's terms found in the chunk
    raw: float = 0.0    # BM25 only (used to decide which secondary documents are worth citing)


def _clean(md: str) -> str:
    md = re.sub(r"^>.*$", "", md, flags=re.M)             # synthetic-document banner
    return md.replace("**", "").replace("`", "")


def _chunks_for(doc: dict, md: str) -> list[Chunk]:
    base = re.sub(r"-(V\d+(\.\d+)?|FR)$", "", doc["doc_id"], flags=re.I)
    superseded = bool(doc["superseded_by"]) or "SUPERSEDED" in md.upper().split("\n#")[0][:400]
    meta = dict(doc_id=base, version=doc["version"], doc_type=doc["doc_type"], language=doc["language"], superseded=superseded)
    text = _clean(md)
    title_m = re.search(r"^#\s+(.+)$", text, flags=re.M)
    title = title_m.group(1).strip() if title_m else doc["title"]
    out: list[Chunk] = []
    parts = re.split(r"(?m)^##\s+", text)
    if len(parts) > 1:                                      # sectioned (policies, procedures, regulatory)
        for p in parts[1:]:
            head, _, body = p.partition("\n")
            sec = re.match(r"(§\s?[\d.]+)", head)
            out.append(Chunk(section=sec.group(1).replace(" ", "") if sec else head.strip(), title=title,
                             text=(head + "\n" + body).strip(), heading=head.strip(), **meta))
    elif re.search(r"(?m)^Q\d+\.", text):                   # FAQ
        for m in re.finditer(r"(?ms)^(Q\d+)\.\s*(.*?)(?=^Q\d+\.|\Z)", text):
            out.append(Chunk(section=m.group(1), title=title, text=m.group(2).strip(), heading=m.group(2).split("\n")[0], **meta))
    elif "|---" in text:                                    # programme-terms table: one chunk per row + whole doc
        rows = [r for r in text.splitlines() if r.startswith("|") and "---" not in r][1:]
        for r in rows:
            cells = [c.strip() for c in r.strip("|").split("|")]
            out.append(Chunk(section=cells[0], title=title, text=" - ".join(cells), heading=cells[0], **meta))
    else:                                                   # short free-form document
        body = re.sub(r"^#\s+.+$", "", text, count=1, flags=re.M).strip()
        out.append(Chunk(section="§1", title=title, text=body, heading=title, **meta))
    for c in out:
        c.tokens = tokenize(c.title + " " + c.text)
        if c.language == "FR":                      # let English questions find the French annexes
            c.tokens += [_stem(FR_EN[t]) for t in re.findall(r"[a-zà-ÿ]+", c.text.lower()) if t in FR_EN]
    return out


class PolicyIndex:
    def __init__(self, con) -> None:
        docs = con.execute(
            "select doc_id, doc_type, title, language, version, superseded_by, file_path from raw_reference_documents").fetchall()
        keys = ("doc_id", "doc_type", "title", "language", "version", "superseded_by", "file_path")
        self.chunks: list[Chunk] = []
        for row in docs:
            d = dict(zip(keys, row))
            path = RELEASE / "files" / d["file_path"]
            if path.exists():
                self.chunks += _chunks_for(d, path.read_text(encoding="utf-8"))
        for no, code, name, crit, fail, when in con.execute(
                "select item_no, item_code, item_name, pass_criteria, fail_examples, applies_when from raw_qa_checklist").fetchall():
            ch = Chunk(doc_id="qa_checklist", section=f"item {no}", title="QA checklist",
                       text=f"{name}: {crit}. Fails when: {fail}. Applies when: {when}.", version="QA-2026.2",
                       doc_type="procedure", language="EN", superseded=False, heading=name)
            ch.tokens = tokenize(f"{code} {name} {crit} {when}")
            self.chunks.append(ch)
        self._bm25_all = BM25Okapi([c.tokens or ["_"] for c in self.chunks])
        n = len(self.chunks)
        df: dict[str, int] = {}
        for c in self.chunks:
            for t in set(c.tokens):
                df[t] = df.get(t, 0) + 1
        self.idf = {t: math.log((n + 1) / (v + 0.5)) for t, v in df.items()}
        self.unseen_idf = math.log(n + 1)

    def get(self, doc_id: str, section: str | None = None) -> list[Chunk]:
        out = [c for c in self.chunks if c.doc_id == doc_id and not c.superseded]
        if section:
            out = [c for c in out if c.section == section]
        return out

    def search(self, question: str, k: int = 3, allow_superseded: bool | None = None) -> list[Hit]:
        q_tokens = expand_query(question)
        if not q_tokens:
            return []
        if allow_superseded is None:
            allow_superseded = bool(re.search(r"\b(old|older|previous|superseded|v?4\.1|v1\.0|before march)\b", question, re.I))
        scores = self._bm25_all.get_scores(q_tokens)
        base_terms = set(tokenize(question))
        denom = sum(self.idf.get(t, self.unseen_idf) for t in base_terms) or 1.0
        hits = []
        for c, s in zip(self.chunks, scores):
            if c.superseded and not allow_superseded:
                continue
            lang_w = 1.0 if c.language == "EN" else (0.95 if re.search(r"qu[eé]bec|montr[eé]al|french|fran[cç]ais", question, re.I) else 0.6)
            matched = sum(self.idf.get(t, 0.0) for t in base_terms if t in set(c.tokens))
            coverage = matched / denom
            hits.append(Hit(c, float(s) * AUTHORITY.get(c.doc_type, 0.5) * lang_w, coverage, float(s)))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:k]

    @staticmethod
    def _units(chunk: Chunk) -> list[str]:
        """Paragraph / bullet units with wrapped lines re-joined (markdown wraps mid-sentence)."""
        units: list[str] = []
        for ln in chunk.text.splitlines():
            s = ln.strip()
            if not s or s.startswith("#") or s == chunk.heading:
                continue
            bullet = re.match(r"^(?:[-*]|\d+\.)\s+(.*)$", s)
            if bullet:
                units.append(bullet.group(1))
            elif units and not units[-1].endswith((".", ":")) or (units and ln.startswith("  ")):
                units[-1] += " " + s
            else:
                units.append(s)
        return units

    def answer(self, question: str, min_coverage: float = 0.1, min_score: float = 6.0):
        """Return (text, hits, citations, notes) or None when evidence is insufficient."""
        hits = self.search(question, k=4)
        if not hits or hits[0].score < min_score or hits[0].coverage < min_coverage:
            return None
        top = hits[0]
        units = self._units(top.chunk) or [top.chunk.text]
        qset = set(expand_query(question))
        scored = [(sum(1 for t in tokenize(u) if t in qset), i, u) for i, u in enumerate(units)]
        best = sorted(scored, key=lambda x: (-x[0], x[1]))[:2]
        chosen = [u for sc, i, u in sorted(best, key=lambda x: x[1]) if sc > 0] or units[:2]
        body = " ".join(chosen)
        cites, notes = [top.chunk.cite], []
        unit_re = re.compile(r"(\d+)[ -]?(months?|business days?|days?|weeks?)", re.I)
        body_units = {(n, u.lower().rstrip("s")) for n, u in unit_re.findall(body)}
        for h in hits[1:]:
            c = h.chunk
            if c.cite in cites or h.raw < 0.5 * top.raw or h.coverage < 0.25:
                continue
            if c.doc_type == "faq":
                faq_units = {(n, u.lower().rstrip("s")) for n, u in unit_re.findall(c.text)}
                same_unit = {u for _, u in body_units}
                if any(u in same_unit and (n, u) not in body_units for n, u in faq_units):
                    notes.append(f"{c.cite} (FAQ v{c.version}) gives a different figure and is not relied on; "
                                 f"{top.chunk.doc_id} v{top.chunk.version} takes precedence.")
                    continue
            cites.append(c.cite)
            if len(cites) == 3:
                break
        return f"{body} [{', '.join(cites)}]", hits, cites, notes
