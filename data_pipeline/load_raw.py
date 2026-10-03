"""Step 1: load the 22 CSV tables into DuckDB as typed raw_* tables, and expose
the 6 Parquet tables as raw_* views.

Identifier columns are kept as VARCHAR (leading zeros matter for the 10-digit CIF).
'NA' outcome codes are kept (only empty strings become NULL).
Types come from schema.json physical_type; unparseable values become NULL
(date-like text columns are VARCHAR in the schema, so nothing is lost there).
"""
import json
import sys
import time

import duckdb

from config import DATA, MAIN_DB, PARQUET_TABLES, SCHEMA_JSON, WAREHOUSE

ID_HINTS = ("_id", "_ref", "_key", "_hash", "_uuid")


def sql_type(col: dict) -> str:
    t = col.get("physical_type") or "VARCHAR"
    name = col["name"]
    if any(name.endswith(h) for h in ID_HINTS) or name.startswith("src_customer"):
        return "VARCHAR"
    return t


def main() -> None:
    WAREHOUSE.mkdir(exist_ok=True)
    schema = json.loads(SCHEMA_JSON.read_text(encoding="utf-8"))
    con = duckdb.connect(str(MAIN_DB))
    con.execute("PRAGMA threads=8")
    for table, meta in schema.items():
        if table in PARQUET_TABLES:
            continue
        path = (DATA / f"{table}.csv").as_posix()
        cols = meta["columns"]
        sel = ", ".join(
            f'TRY_CAST("{c["name"]}" AS {sql_type(c)}) AS "{c["name"]}"'
            if sql_type(c) != "VARCHAR" else f'"{c["name"]}"'
            for c in cols
        )
        t0 = time.time()
        con.execute(f"DROP TABLE IF EXISTS raw_{table}")
        con.execute(
            f"CREATE TABLE raw_{table} AS SELECT {sel} FROM "
            f"read_csv('{path}', header=true, all_varchar=true, nullstr='', "
            f"sample_size=-1, quote='\"', escape='\"', max_line_size=10000000)"
        )
        n = con.execute(f"SELECT count(*) FROM raw_{table}").fetchone()[0]
        print(f"raw_{table}: {n:,} rows ({time.time()-t0:.0f}s)", flush=True)
    for table in PARQUET_TABLES:
        glob = (DATA / table / "*" / "*.parquet").as_posix()
        con.execute(
            f"CREATE OR REPLACE VIEW raw_{table} AS "
            f"SELECT * FROM read_parquet('{glob}', hive_partitioning=true, union_by_name=true)"
        )
    con.close()


if __name__ == "__main__":
    sys.exit(main())
