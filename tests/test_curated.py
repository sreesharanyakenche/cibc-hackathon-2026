import sys
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "data_pipeline"))
from config import MAIN_DB


@pytest.fixture(scope="module")
def con():
    c = duckdb.connect(str(MAIN_DB), read_only=True)
    yield c
    c.close()


def one(con, sql):
    return con.execute(sql).fetchone()[0]


def test_promise_resolved_count(con):
    assert one(con, "select count(*) from cur_promises where is_resolved") == 204_369


def test_september_promise_kept_rate_matches_gold(con):  # BQ-034 gold 35.1 (August)
    v = one(con, """select round(100.0*avg(case when is_kept then 1 else 0 end),1)
                    from cur_promises where is_resolved
                    and ptp_due_date between date '2026-08-01' and date '2026-08-31'""")
    assert v == 35.1


def test_open_cases_use_outcome_null(con):  # BQ-035 gold sums to 96,000
    assert one(con, "select count(*) from cur_cases where is_open") == 96_000


def test_bucket_calc_has_no_nulls_and_flags_mismatch(con):
    assert one(con, "select count(*) from cur_cases where current_bucket_calc is null") == 0
    assert one(con, "select count(*) from cur_cases where is_open and bucket_mismatch_flag") == 3_807


def test_channel_eff_never_null_for_contacts(con):  # REL-0926
    assert one(con, "select count(*) from cur_contacts where channel_eff is null") == 0


def test_september_dialer_rows_not_dropped(con):  # BQ-011 trap: filtering on channel alone loses rows
    with_eff = one(con, """select count(*) from cur_contacts where channel_eff='call' and direction='outbound'
                           and contact_ts_local >= timestamp '2026-09-26'""")
    raw_only = one(con, """select count(*) from cur_contacts where channel='call' and direction='outbound'
                           and contact_ts_local >= timestamp '2026-09-26'""")
    assert with_eff > raw_only


def test_snapshot_dates_all_parse(con):
    assert one(con, "select count(*) from cur_snapshot where month_end_parsed is null") == 0
