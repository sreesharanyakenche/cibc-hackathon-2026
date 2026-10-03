"""Shared paths. Everything else imports from here."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / "data" / "maple_data" / "maple_collections_release"
DATA = RELEASE / "data"
SCHEMA_JSON = RELEASE / "schema" / "schema.json"
DOCS_DIR = RELEASE / "files" / "docs"
TRANSCRIPTS_DIR = RELEASE / "files" / "transcripts"
LABELS = RELEASE / "labels"
WAREHOUSE = ROOT / "warehouse"
MAIN_DB = WAREHOUSE / "maple.duckdb"

PARQUET_TABLES = [
    "loan_instalments", "transactions", "card_statements",
    "account_monthly_snapshot", "bureau_history", "salary_credit_history",
]
