"""Layer 3/4 settings. Paths and policy parameters live here, nowhere else."""
from datetime import date

from data_pipeline.config import MAIN_DB, ROOT, WAREHOUSE  # noqa: F401

FEATURE_DB = WAREHOUSE / "features.duckdb"      # derived tables (owned by this layer)
DECISIONS_DB = WAREHOUSE / "decisions.duckdb"   # Decision Records + human reviews (append-only)
MODEL_DIR = ROOT / "models"
MODEL_NAME = "ptp_break"
MODEL_VERSION = "v1"
FEATURE_VERSION = "features_v1"

AS_OF = date(2026, 9, 28)                        # snapshot date

# ---- time-based split on promise due date (resolved promises only) ------------------------------
# Break rate moves from ~33% (to Apr 2026) to ~59% (Aug-Sep 2026). Calibration therefore uses a
# validation window close to the test window, and the test window is the most recent resolved period.
TRAIN_END = date(2026, 6, 30)        # due <= this: training
VALID_END = date(2026, 8, 15)        # (TRAIN_END, VALID_END]: early stopping + calibration
# due > VALID_END: held-out test

# ---- action policy (ASSUMPTION: the documents say thresholds/budgets are 'set by policy', none are given) -------
BANDS = (0.50, 0.65, 0.75)      # calibrated break probability: low < 0.50 <= medium < 0.65 <= high < 0.75 <= very_high
