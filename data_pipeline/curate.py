"""Step 2: curated (silver) views over raw_* tables.

Each trap documented in the organiser materials is fixed once here, so every
consumer (NL-to-SQL, features, UI) inherits the fix. Original columns are never
overwritten: corrected values are added as new *_calc / *_eff columns.

Traps handled (source in brackets):
  * contact_history.channel renamed channel_v2 on 2026-09-26 [REL-0926, BQ-011]
  * account_monthly_snapshot.month_end_date in 4 formats (ISO, DD/MM/YYYY, MM/DD/YYYY, YYYYMMDD;
    unambiguous because month-end days are >= 28); dpd_bucket inconsistent [BQ-017]
  * collections_cases.current_bucket disagrees with current_dpd for ~4% of open cases [BQ-035]
  * promise rates exclude open and cancelled promises [metric_definitions, PRC-COLL-010 s3.3]
  * 'open case' = outcome IS NULL (BQ-004, BQ-035 gold)
"""
import sys

import duckdb

from data_pipeline.config import MAIN_DB

SNAPSHOT_DATE = "2026-09-28"  # README: snapshot date

BUCKET_SQL = """case when {c} is null then null
  when {c} = 0 then 'current' when {c} <= 30 then '1-30' when {c} <= 60 then '31-60'
  when {c} <= 90 then '61-90' when {c} <= 120 then '91-120' when {c} <= 150 then '121-150'
  when {c} <= 180 then '151-180' else '180+' end"""

VIEWS = {
    "meta_snapshot": f"select DATE '{SNAPSHOT_DATE}' as snapshot_date",

    "cur_cases": f"""
        select c.*,
               {BUCKET_SQL.format(c='current_dpd')} as current_bucket_calc,
               (c.outcome is null) as is_open,
               (c.current_bucket is distinct from {BUCKET_SQL.format(c='current_dpd')}) as bucket_mismatch_flag
        from raw_collections_cases c""",

    "cur_promises": f"""
        select p.*,
               coalesce(p.ptp_status in ('kept','partially_kept','broken'), false) as is_resolved,
               coalesce(p.ptp_status = 'broken', false) as is_broken,
               coalesce(p.ptp_status = 'kept', false) as is_kept,
               coalesce(
                 p.ptp_status = 'open'
                 and p.ptp_due_date + INTERVAL 2 DAY < (select snapshot_date from meta_snapshot),
                 false
) as open_past_grace_flag
        from raw_promises_to_pay p""",

    "cur_contacts": """
        select h.*,
               coalesce(h.channel_v2, h.channel) as channel_eff,
               coalesce(
                h.channel = 'system' or h.channel_v2 = 'system' or h.direction = 'internal',
                false
) as is_system_event,
cast(h.contact_ts_local as date) as contact_date_local
        from raw_contact_history h""",

    "cur_snapshot": f"""
        select s.*,
               coalesce(try_strptime(s.month_end_date, '%Y-%m-%d'),
                        try_strptime(s.month_end_date, '%d/%m/%Y'),
                        try_strptime(s.month_end_date, '%m/%d/%Y'),
                        try_strptime(s.month_end_date, '%Y%m%d'))::DATE as month_end_parsed,
               coalesce(try_strptime(s.month_end_date, '%Y-%m-%d'),
                        try_strptime(s.month_end_date, '%d/%m/%Y'),
                        try_strptime(s.month_end_date, '%Y%m%d'))::DATE as month_end_parsed_ref,
               {BUCKET_SQL.format(c='s.dpd')} as dpd_bucket_calc
        from raw_account_monthly_snapshot s""",

    "cur_notes": "select * from raw_agent_notes",
    "cur_transcripts": "select * from raw_call_transcripts",
    "cur_agents": "select * from raw_agents",
}


def main() -> None:
    MAIN_DB.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(MAIN_DB))
    for name, sql in VIEWS.items():
        con.execute(f"create or replace view {name} as {sql}")
        print("created", name)
    con.close()


if __name__ == "__main__":
    sys.exit(main())
