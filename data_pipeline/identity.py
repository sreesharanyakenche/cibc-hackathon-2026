#!/usr/bin/env python3
"""
Evidence-preserving identity resolution.   warehouse/maple.duckdb (raw_*, read-only) -> warehouse/c360.duckdb (id_* tables)

Adapted from the reference implementation in CIBC_Hackathon_CodeBlooded-main (scripts/identity_matching.py); the matching
rules, tiers and evidence model are unchanged. Differences here: reads our raw_* tables, pads deposit CIFs inline, and
persists results as DuckDB tables instead of parquet.

Two stages, both deterministic and fully explained row by row:

 STAGE 1  CRM person clusters (duplicate CRM records of one person -> one `customer_key`).
          * `customer_key` = the anchor CRM id of the cluster, so it keeps the meaning the existing L1 agent gives it
            (the CRM id used in notes/PTPs/contacts).  A cluster's other CRM ids are listed, never discarded.
          * Auto-merge only on strong evidence (below).  Anything weaker is written to the pair table as `review` and NOT merged.
 STAGE 2  Source-system customers (Cards / Lending / Core-Deposit, + bureau name/DOB for Core) -> customer_key.
          There is no shared id, so a source customer is matched on date of birth (blocking) + last name + first name +
          postal + phone, and every link carries per-field evidence.

Phone numbers are *never* decisive on their own: a phone shared by >= 2 different people ("shared mobile") has its
evidence neutralised and is flagged.  (Contrast: the existing L1 rule counts distinct CRM ids by last-4 digits, which is
true for every one of the 10,000 possible values in this dataset.)

Tiers -> match_confidence (ordinal confidence, NOT a calibrated probability; precision per tier is measured against
account->customer pairs that the collections system itself recorded, see identity_report.json):
   A 0.97  DOB + last name + first name exact + >=1 corroborator (postal exact or non-shared phone exact/partial)
   B 0.90  DOB + last name + first name exact, no corroborator available (none contradicting)
   C 0.88  DOB + last name + first initial / middle name / preferred name + >=1 corroborator
   D 0.65  DOB + last name + first initial only, no corroborator                       -> review, not auto-linked
   E 0.60  DOB + last name, first name contradicts, >=2 corroborators                  -> review, not auto-linked
 Status: linked (tier A-C, unique best customer_key) | ambiguous (A-C but >1 customer_key at the same top tier) |
         review (D/E) | unmatched (no candidate; includes records without a DOB, which cannot be blocked).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from data_pipeline.config import MAIN_DB, ROOT, WAREHOUSE

C360_DB = WAREHOUSE / "c360.duckdb"
REPORTS = ROOT / "outputs"

CONF = {"A": 0.97, "B": 0.90, "C": 0.88, "D": 0.65, "E": 0.60}
MAX_CLUSTER = 4  # clusters larger than this are not merged (flagged)


def log(m: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", file=sys.stderr, flush=True)


def P(t: str) -> str:
    return f"src.raw_{t}"


class UnionFind:
    def __init__(self):
        self.p: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--memory-limit", default="8GB")
    ap.add_argument("--threads", type=int, default=6)
    ap.add_argument("--temp-dir", type=Path, default=WAREHOUSE / "tmp_identity")
    a = ap.parse_args()
    a.temp_dir.mkdir(parents=True, exist_ok=True)
    REPORTS.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(f"ATTACH '{MAIN_DB.as_posix()}' AS src (READ_ONLY)")
    con.execute(f"SET memory_limit='{a.memory_limit}'")
    con.execute(f"SET threads={a.threads}")
    con.execute(f"SET temp_directory='{a.temp_dir}'")
    con.execute("SET preserve_insertion_order=false")
    t0 = time.time()
    report: dict = {"generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"), "conf_by_tier": CONF}

    # SQL helpers -------------------------------------------------------------
    con.execute("CREATE MACRO letters(x) AS regexp_replace(lower(strip_accents(x)), '[^a-z]', '', 'g')")
    con.execute("CREATE MACRO tokens(x) AS list_filter(string_split(trim(regexp_replace(regexp_replace(lower(strip_accents(x)), '[^a-z ]', ' ', 'g'), ' +', ' ', 'g')), ' '), t -> t <> '')")
    con.execute("CREATE MACRO digits10(x) AS right(regexp_replace(x, '[^0-9]', '', 'g'), 10)")

    # =========================================================== STAGE 1: CRM
    log("stage 1: CRM normalisation")
    con.execute(f"""
        CREATE TABLE crm AS
        SELECT crm_customer_id AS id, date_of_birth AS dob, first_name, last_name,
               letters(first_name) AS fn_key, letters(last_name) AS ln_key,
               letters(coalesce(middle_name, '')) AS mn_key, letters(coalesce(preferred_name, '')) AS pn_key,
               upper(replace(postal_code, ' ', '')) AS postal,
               CASE WHEN length(digits10(primary_phone_e164)) = 10 THEN digits10(primary_phone_e164) END AS phone,
               CASE WHEN length(digits10(secondary_phone_raw)) = 10 THEN digits10(secondary_phone_raw) END AS phone2,
               national_id_hash AS nh, duplicate_of_crm_id AS dup_of, customer_status, record_source
        FROM {P('customers')}""")
    n_crm = con.execute("SELECT count(*) FROM crm").fetchone()[0]

    log("stage 1: candidate duplicate pairs")
    con.execute("""
        CREATE TABLE pair_raw AS
        SELECT a.id AS id_a, b.id AS id_b, 'national_id_hash' AS block FROM crm a JOIN crm b ON a.nh = b.nh AND a.id < b.id
        UNION
        SELECT least(a.id, b.dup_of), greatest(a.id, b.dup_of), 'declared_pointer' FROM crm b JOIN crm a ON a.id = b.dup_of
        UNION
        SELECT a.id, b.id, 'dob_name_postal' FROM crm a JOIN crm b
          ON a.dob = b.dob AND a.ln_key = b.ln_key AND a.postal = b.postal AND a.id < b.id
         WHERE a.dob IS NOT NULL AND a.postal IS NOT NULL AND a.ln_key <> ''""")
    con.execute("""
        CREATE TABLE crm_pairs AS
        SELECT id_a, id_b, string_agg(DISTINCT block, '+' ORDER BY block) AS blocks FROM pair_raw GROUP BY 1, 2""")
    # phone sharing between *different* people is judged after clustering; for pairs we use phone as corroboration only
    con.execute("""
        CREATE TABLE crm_pair_ev AS
        SELECT p.id_a, p.id_b, p.blocks,
               (a.nh IS NOT NULL AND a.nh = b.nh) AS same_nh,
               (a.nh IS NOT NULL AND b.nh IS NOT NULL AND a.nh <> b.nh) AS diff_nh,
               (a.dob IS NOT NULL AND a.dob = b.dob) AS same_dob,
               (a.dob IS NOT NULL AND b.dob IS NOT NULL AND a.dob <> b.dob) AS diff_dob,
               (a.ln_key = b.ln_key) AS same_ln,
               (a.fn_key = b.fn_key) AS same_fn,
               (a.fn_key <> b.fn_key AND left(a.fn_key,1) = left(b.fn_key,1)) AS same_fn_initial,
               (a.postal IS NOT NULL AND a.postal = b.postal) AS same_postal,
               (a.phone IS NOT NULL AND a.phone = b.phone) AS same_phone,
               (a.dup_of = b.id OR b.dup_of = a.id) AS declared
        FROM crm_pairs p JOIN crm a ON a.id = p.id_a JOIN crm b ON b.id = p.id_b""")
    con.execute("""
        CREATE TABLE crm_pair_dec AS
        SELECT *,
          CASE
            WHEN same_nh AND diff_dob THEN 'conflict'
            WHEN diff_nh THEN 'conflict'
            WHEN same_nh AND same_dob THEN 'merge'
            WHEN declared AND NOT diff_dob AND (same_dob::int + same_ln::int + same_fn::int + same_postal::int + same_phone::int) >= 3 THEN 'merge'
            WHEN same_dob AND same_ln AND same_fn AND same_postal AND same_phone THEN 'merge'
            ELSE 'review'
          END AS decision,
          CASE
            WHEN same_nh AND diff_dob THEN 'national_id_hash_equal_but_dob_differs'
            WHEN diff_nh THEN 'national_id_hash_differs'
            WHEN same_nh AND same_dob THEN 'national_id_hash+dob'
            WHEN declared AND NOT diff_dob AND (same_dob::int + same_ln::int + same_fn::int + same_postal::int + same_phone::int) >= 3 THEN 'declared_pointer+3_corroborating_fields'
            WHEN same_dob AND same_ln AND same_fn AND same_postal AND same_phone THEN 'dob+name+postal+phone'
            ELSE 'insufficient_evidence'
          END AS method,
          CASE
            WHEN same_nh AND same_dob THEN 0.99
            WHEN declared AND NOT diff_dob AND (same_dob::int + same_ln::int + same_fn::int + same_postal::int + same_phone::int) >= 3 THEN 0.95
            WHEN same_dob AND same_ln AND same_fn AND same_postal AND same_phone THEN 0.93
            ELSE 0.5 END AS confidence
        FROM crm_pair_ev""")
    report["crm_pairs_by_decision"] = dict(con.execute("SELECT decision || ' / ' || method, count(*) FROM crm_pair_dec GROUP BY 1 ORDER BY 2 DESC").fetchall())
    report["crm_pairs_by_block"] = dict(con.execute("SELECT blocks, count(*) FROM crm_pair_dec GROUP BY 1 ORDER BY 2 DESC").fetchall())
    log(f"  pairs: {report['crm_pairs_by_decision']}")

    # clusters via union-find over 'merge' pairs
    uf = UnionFind()
    for ia, ib in con.execute("SELECT id_a, id_b FROM crm_pair_dec WHERE decision = 'merge'").fetchall():
        uf.union(ia, ib)
    members: dict[str, list[str]] = defaultdict(list)
    for x in list(uf.p):
        members[uf.find(x)].append(x)
    big = {r for r, m in members.items() if len(m) > MAX_CLUSTER}
    report["crm_clusters_over_limit_not_merged"] = len(big)
    status = dict(con.execute("SELECT id, customer_status FROM crm").fetchall())
    declared_dup = {r[0] for r in con.execute("SELECT id FROM crm WHERE dup_of IS NOT NULL").fetchall()}
    rows = []
    for root, m in members.items():
        if root in big:
            continue
        pref = sorted(m, key=lambda i: (i in declared_dup, i))  # anchor: not-a-declared-duplicate first, then smallest id
        anchor = pref[0]
        for i in m:
            rows.append((i, anchor, len(m), i == anchor))
    con.execute("CREATE TABLE cluster_map (crm_customer_id VARCHAR, customer_key VARCHAR, cluster_size INTEGER, is_anchor BOOLEAN)")
    con.executemany("INSERT INTO cluster_map VALUES (?, ?, ?, ?)", rows)
    con.execute("""
        CREATE TABLE crm_clusters AS
        SELECT c.id AS crm_customer_id,
               coalesce(m.customer_key, c.id) AS customer_key,
               coalesce(m.cluster_size, 1) AS cluster_size,
               coalesce(m.is_anchor, true) AS is_anchor,
               CASE WHEN m.customer_key IS NULL THEN 'singleton' ELSE 'merged_on_evidence' END AS cluster_method
        FROM crm c LEFT JOIN cluster_map m ON m.crm_customer_id = c.id""")
    s = con.execute("""SELECT count(DISTINCT customer_key), count(*) FILTER (WHERE cluster_size > 1), max(cluster_size) FROM crm_clusters""").fetchone()
    report["crm_records"] = n_crm
    report["customer_keys"] = s[0]
    report["crm_records_in_merged_clusters"] = s[1]
    report["max_cluster_size"] = s[2]
    log(f"  {n_crm:,} CRM records -> {s[0]:,} customer_keys ({s[1]:,} records in merged clusters)")

    # shared mobile (after clustering): a phone used by >= 2 different customer_keys
    con.execute("""
        CREATE TABLE phone_use AS
        SELECT phone, count(DISTINCT cc.customer_key) AS n_people FROM
          (SELECT id, phone FROM crm WHERE phone IS NOT NULL UNION ALL SELECT id, phone2 FROM crm WHERE phone2 IS NOT NULL) x
          JOIN crm_clusters cc ON cc.crm_customer_id = x.id GROUP BY 1""")
    report["shared_mobile_phones_2plus_people"] = con.execute("SELECT count(*) FROM phone_use WHERE n_people >= 2").fetchone()[0]
    report["shared_mobile_phones_3plus_people"] = con.execute("SELECT count(*) FROM phone_use WHERE n_people >= 3").fetchone()[0]
    con.execute("""
        CREATE TABLE crm_person AS
        SELECT c.*, cc.customer_key,
               coalesce(pu.n_people, 1) AS phone_people, coalesce(pu2.n_people, 1) AS phone2_people
        FROM crm c JOIN crm_clusters cc ON cc.crm_customer_id = c.id
        LEFT JOIN phone_use pu ON pu.phone = c.phone LEFT JOIN phone_use pu2 ON pu2.phone = c.phone2""")

    # =========================================================== STAGE 2: source systems
    log("stage 2: source identity tuples")
    con.execute(f"""
        CREATE TABLE src_acct AS
        SELECT 'CARDS' AS system, card_account_id AS account_id, src_customer_ref AS ref,
               cardholder_name_raw AS name_raw, try_strptime(cardholder_dob_raw, '%d-%m-%Y')::DATE AS dob,
               cardholder_phone_raw AS phone_raw, cardholder_postal_raw AS postal_raw
        FROM {P('card_accounts')}
        UNION ALL
        SELECT 'LENDING', loan_id, src_customer_ref, borrower_name_raw, try_strptime(borrower_dob_raw, '%Y/%m/%d')::DATE,
               borrower_phone_raw, borrower_postal_raw FROM {P('loan_accounts')}
        UNION ALL
        SELECT 'CORE', deposit_account_id,
               CASE WHEN regexp_matches(src_customer_ref, '^[0-9]+$') AND length(src_customer_ref) < 10 THEN lpad(src_customer_ref, 10, '0') ELSE src_customer_ref END, holder_name_raw, try_strptime(holder_dob_raw, '%Y%m%d')::DATE,
               holder_phone_raw, holder_postal_raw FROM {P('deposit_accounts')}""")
    report["source_accounts"] = dict(con.execute("SELECT system, count(*) FROM src_acct GROUP BY 1").fetchall())
    report["source_accounts_without_dob"] = dict(con.execute("SELECT system, count(*) FROM src_acct WHERE dob IS NULL GROUP BY 1").fetchall())
    # distinct identity tuples (an account holder presents the same identity on several accounts)
    con.execute(f"""
        CREATE TABLE src AS
        SELECT row_number() OVER () AS tid, system, ref, name_raw, dob, phone_raw, postal_raw, 'account' AS origin FROM
          (SELECT DISTINCT system, ref, name_raw, dob, phone_raw, postal_raw FROM src_acct)
        UNION ALL   -- the bureau file repeats the core-banking CIF with a FULL name and DOB
        SELECT 1000000000 + row_number() OVER (), 'CORE', bank_subject_ref, subject_name_reported, subject_dob_reported, NULL, NULL, 'bureau'
        FROM (SELECT DISTINCT bank_subject_ref, subject_name_reported, subject_dob_reported FROM {P('external')}
              WHERE subject_name_reported IS NOT NULL AND subject_dob_reported IS NOT NULL)""")
    con.execute("""
        CREATE TABLE src_n AS
        SELECT *, tokens(name_raw) AS toks, letters(name_raw) AS name_key,
               upper(replace(postal_raw, ' ', '')) AS postal,
               CASE WHEN regexp_matches(phone_raw, '[xX]') THEN NULL WHEN length(digits10(phone_raw)) = 10 THEN digits10(phone_raw) END AS phone,
               CASE WHEN regexp_matches(phone_raw, '^[0-9]{6}[xX]+[0-9]{2}$') THEN left(phone_raw, 6) END AS mask_p6,
               CASE WHEN regexp_matches(phone_raw, '^[0-9]{6}[xX]+[0-9]{2}$') THEN right(phone_raw, 2) END AS mask_s2
        FROM src""")
    report["source_identity_tuples"] = con.execute("SELECT count(*) FROM src_n").fetchone()[0]

    log("stage 2: candidate generation (block on DOB + last name)")
    con.execute("""
        CREATE TABLE cand AS
        SELECT s.tid, c.customer_key, c.id AS crm_customer_id,
          CASE WHEN list_contains(s.toks, c.ln_key) THEN 'token' ELSE 'substring' END AS ln_type,
          CASE WHEN list_contains(s.toks, c.fn_key) THEN 'exact'
               WHEN c.mn_key <> '' AND list_contains(s.toks, c.mn_key) THEN 'middle'
               WHEN c.pn_key <> '' AND list_contains(s.toks, c.pn_key) THEN 'preferred'
               WHEN list_contains(s.toks, left(c.fn_key, 1)) THEN 'initial'
               ELSE 'mismatch' END AS fn_type,
          CASE WHEN s.postal IS NULL OR c.postal IS NULL THEN 'missing'
               WHEN s.postal = c.postal THEN 'exact'
               WHEN left(s.postal, 3) = left(c.postal, 3) THEN 'fsa'
               ELSE 'mismatch' END AS postal_type,
          CASE WHEN s.phone IS NULL AND s.mask_p6 IS NULL THEN 'missing'
               WHEN (s.phone IS NOT NULL AND s.phone IN (c.phone, c.phone2)) THEN 'exact'
               WHEN s.mask_p6 IS NOT NULL AND ((left(c.phone, 6) = s.mask_p6 AND right(c.phone, 2) = s.mask_s2)
                    OR (left(c.phone2, 6) = s.mask_p6 AND right(c.phone2, 2) = s.mask_s2)) THEN 'partial'
               ELSE 'mismatch' END AS phone_type,
          (greatest(c.phone_people, c.phone2_people) >= 2) AS shared_phone
        FROM src_n s JOIN crm_person c
          ON s.dob = c.dob
         AND length(c.ln_key) >= 2
         AND (list_contains(s.toks, c.ln_key) OR contains(s.name_key, c.ln_key))
        WHERE s.dob IS NOT NULL""")
    log(f"  candidates: {con.execute('SELECT count(*) FROM cand').fetchone()[0]:,}")
    con.execute("""
        CREATE TABLE cand_t AS
        SELECT *,
          ((postal_type = 'exact')::int + (phone_type IN ('exact','partial') AND NOT shared_phone)::int) AS corrob,
          CASE
            WHEN fn_type = 'exact' AND ((postal_type = 'exact') OR (phone_type IN ('exact','partial') AND NOT shared_phone)) THEN 'A'
            WHEN fn_type = 'exact' AND postal_type <> 'mismatch' AND (phone_type NOT IN ('mismatch') OR shared_phone) THEN 'B'
            WHEN fn_type IN ('initial','middle','preferred') AND ((postal_type = 'exact') OR (phone_type IN ('exact','partial') AND NOT shared_phone)) THEN 'C'
            WHEN fn_type IN ('initial','middle','preferred') THEN 'D'
            WHEN fn_type = 'mismatch' AND ((postal_type = 'exact')::int + (phone_type IN ('exact','partial') AND NOT shared_phone)::int) >= 2 THEN 'E'
          END AS tier
        FROM cand""")
    # best evidence per (tuple, customer_key): collapse duplicate CRM records of one person
    con.execute("""
        CREATE TABLE cand_k AS
        SELECT tid, customer_key, min(tier) AS tier,
               arg_min(crm_customer_id, tier) AS via_crm_customer_id,
               arg_min(ln_type, tier) AS ln_type, arg_min(fn_type, tier) AS fn_type,
               arg_min(postal_type, tier) AS postal_type, arg_min(phone_type, tier) AS phone_type,
               bool_or(shared_phone) AS shared_phone
        FROM cand_t WHERE tier IS NOT NULL GROUP BY 1, 2""")
    con.execute("""
        CREATE TABLE tuple_link AS
        WITH ranked AS (
          SELECT *, rank() OVER (PARTITION BY tid ORDER BY tier) AS rk,
                 count(*) OVER (PARTITION BY tid, tier) AS n_same_tier
          FROM cand_k)
        SELECT r.tid, r.customer_key, r.tier, r.via_crm_customer_id, r.ln_type, r.fn_type, r.postal_type, r.phone_type, r.shared_phone,
               r.n_same_tier,
               CASE WHEN r.tier IN ('A','B','C') AND r.n_same_tier = 1 THEN 'linked'
                    WHEN r.tier IN ('A','B','C') THEN 'ambiguous'
                    ELSE 'review' END AS status
        FROM ranked r WHERE rk = 1""")

    # roll up tuples -> source customers (system, ref)
    log("stage 2: roll-up to source customers")
    con.execute("""
        CREATE TABLE ent_votes AS
        SELECT s.system, s.ref, l.customer_key, l.status, l.tier, s.origin, l.ln_type, l.fn_type, l.postal_type, l.phone_type,
               l.shared_phone, l.via_crm_customer_id, l.n_same_tier
        FROM tuple_link l JOIN src_n s USING (tid)""")
    con.execute("""
        CREATE TABLE ent AS
        WITH per_key AS (
          SELECT system, ref, customer_key, min(tier) AS best_tier, count(*) AS n_tuples,
                 bool_or(status = 'linked') AS any_linked,
                 bool_or(origin = 'bureau') AS bureau_supported,
                 bool_or(shared_phone) AS shared_phone,
                 any_value(via_crm_customer_id) AS via_crm_customer_id,
                 string_agg(DISTINCT 'ln:' || ln_type || ' fn:' || fn_type || ' postal:' || postal_type || ' phone:' || phone_type, ' | ') AS evidence
          FROM ent_votes GROUP BY 1, 2, 3),
        ranked AS (SELECT *, rank() OVER (PARTITION BY system, ref ORDER BY best_tier, n_tuples DESC) rk,
                          count(*) OVER (PARTITION BY system, ref) AS n_candidate_people FROM per_key),
        top AS (SELECT * FROM ranked WHERE rk = 1)
        -- Exactly ONE row per source customer. When several customers tie for the best rank the row is kept (n_top > 1,
        -- status 'ambiguous') and the lexicographically first tied customer_key is shown; all tied keys are listed.
        SELECT system, ref,
               min(customer_key) AS customer_key, any_value(best_tier) AS best_tier, max(n_tuples) AS n_tuples,
               bool_or(bureau_supported) AS bureau_supported, bool_or(shared_phone) AS shared_phone,
               arg_min(via_crm_customer_id, customer_key) AS via_crm_customer_id,
               arg_min(evidence, customer_key) AS evidence, any_value(n_candidate_people) AS n_candidate_people,
               count(*) AS n_top, list(customer_key ORDER BY customer_key) AS tied_customer_keys
        FROM top GROUP BY system, ref""")
    con.execute("""
        CREATE TABLE ent_all AS
        SELECT DISTINCT system, ref FROM src_n""")

    # account-level links ------------------------------------------------------
    con.execute("""
        CREATE TABLE identity_links AS
        SELECT a.system AS source_system, a.account_id, a.ref AS src_customer_ref,
               e.customer_key,
               CASE
                 WHEN e.ref IS NULL THEN 'unmatched'
                 WHEN e.best_tier IN ('A','B','C') AND e.n_top = 1 AND e.n_candidate_people = 1 THEN 'linked'
                 WHEN e.best_tier IN ('A','B','C') AND e.n_top = 1 THEN 'linked_with_alternatives'
                 WHEN e.best_tier IN ('A','B','C') THEN 'ambiguous'
                 ELSE 'review' END AS link_status,
               e.best_tier AS tier,
               CASE e.best_tier WHEN 'A' THEN 0.97 WHEN 'B' THEN 0.90 WHEN 'C' THEN 0.88 WHEN 'D' THEN 0.65 WHEN 'E' THEN 0.60 END AS match_confidence,
               CASE e.best_tier WHEN 'A' THEN 'dob+lastname+firstname+corroborated'
                                WHEN 'B' THEN 'dob+lastname+firstname'
                                WHEN 'C' THEN 'dob+lastname+first_initial_or_alias+corroborated'
                                WHEN 'D' THEN 'dob+lastname+first_initial'
                                WHEN 'E' THEN 'dob+lastname+2_corroborators_name_conflict' END AS match_method,
               e.evidence AS match_evidence, e.n_tuples AS supporting_identity_records, e.bureau_supported,
               e.n_candidate_people AS candidate_people, coalesce(e.shared_phone, false) AS shared_mobile,
               CASE WHEN e.n_top > 1 THEN e.tied_customer_keys END AS tied_customer_keys,
               list_filter([
                 CASE WHEN e.n_candidate_people > 1 THEN 'multiple_candidate_people' END,
                 CASE WHEN coalesce(e.shared_phone, false) THEN 'shared_mobile' END,
                 CASE WHEN e.evidence LIKE '%postal:mismatch%' THEN 'postal_mismatch' END,
                 CASE WHEN e.evidence LIKE '%phone:mismatch%' THEN 'phone_mismatch' END,
                 CASE WHEN e.evidence LIKE '%fn:mismatch%' THEN 'first_name_conflict' END,
                 CASE WHEN a.dob IS NULL THEN 'source_dob_missing' END
               ], x -> x IS NOT NULL) AS conflict_flags
        FROM src_acct a LEFT JOIN ent e ON e.system = a.system AND e.ref = a.ref""")
    n_links, n_acct = con.execute("SELECT (SELECT count(*) FROM identity_links), (SELECT count(*) FROM src_acct)").fetchone()
    n_distinct = con.execute("SELECT count(*) FROM (SELECT DISTINCT source_system, account_id FROM identity_links)").fetchone()[0]
    assert n_links == n_acct == n_distinct, f"identity_links must have exactly one row per account ({n_links} rows, {n_distinct} distinct, {n_acct} accounts)"
    report["accounts_total"] = n_acct
    report["links_by_status"] = dict(con.execute("SELECT link_status, count(*) FROM identity_links GROUP BY 1 ORDER BY 2 DESC").fetchall())
    report["links_by_system_status"] = {f"{s}/{st}": n for s, st, n in con.execute(
        "SELECT source_system, link_status, count(*) FROM identity_links GROUP BY 1, 2 ORDER BY 1, 3 DESC").fetchall()}
    report["links_by_tier"] = dict(con.execute("SELECT coalesce(tier,'-'), count(*) FROM identity_links GROUP BY 1 ORDER BY 1").fetchall())

    # Accuracy is measured separately, with explicit denominators, by scripts/validate_identity_metrics.py -> reports/IDENTITY_METRICS.md

    # =========================================================== write outputs
    log("writing outputs")
    con.execute(f"ATTACH '{C360_DB.as_posix()}' AS out")
    for name, sql in {
        "id_crm_pairs": "SELECT * FROM crm_pair_dec",
        "id_crm_clusters": "SELECT * FROM crm_clusters",
        "id_identity_links": "SELECT * FROM identity_links",
        "id_phone_sharing": "SELECT phone, n_people FROM phone_use WHERE n_people >= 2",
    }.items():
        con.execute(f"CREATE OR REPLACE TABLE out.{name} AS {sql}")
    report["seconds"] = round(time.time() - t0)
    (REPORTS / "identity_report.json").write_text(json.dumps(report, indent=1, default=str))
    print(json.dumps({k: report[k] for k in ("crm_records", "customer_keys", "crm_records_in_merged_clusters", "links_by_status")}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
