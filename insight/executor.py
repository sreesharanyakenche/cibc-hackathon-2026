from __future__ import annotations

import duckdb

from . import config, guard


def connect() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(config.MAIN_DB), read_only=True)


def run(con: duckdb.DuckDBPyConnection, sql: str, params: list | None = None):
    """Validate then execute; returns (columns, rows). Row-capped."""
    guard.validate(sql)
    capped = f"select * from ({sql}) as _q limit {config.MAX_ROWS}"
    cur = con.execute(capped, params or [])
    cols = [d[0] for d in cur.description]
    return cols, cur.fetchall()


def to_csv(cols: list[str], rows: list[tuple]) -> str:
    def cell(v):
        if v is None:
            return ""
        if isinstance(v, float):
            s = f"{v:.4f}".rstrip("0").rstrip(".")
            return s if s not in ("", "-0") else "0"
        s = str(v)
        return f'"{s}"' if ("," in s or '"' in s or "\n" in s) else s
    lines = [",".join(cols)] + [",".join(cell(v) for v in r) for r in rows]
    return "\n".join(lines)
