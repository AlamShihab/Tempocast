"""Configuration for the TFS Flask web application.

The app reads the pipeline's outputs (JSON / CSV / .joblib) from the TFS project
folder and reuses the project's own code (``src``) for forecasting and validation,
so the website computes features exactly as training did.

Override any path with environment variables (or a ``.env`` file next to this one):

    TFS_PROJECT_DIR   folder with results/, models/, data/, config/ (pipeline outputs)
    TFS_CODE_DIR      folder containing the pipeline package ``src`` (default: TFS_PROJECT_DIR if it has
                      ``src``, else the sibling "Work with claude" folder)
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path

try:  # optional .env support
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:  # pragma: no cover
    pass

# ----------------------------------------------------------------------------- paths
BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = Path(os.environ.get("TFS_PROJECT_DIR", BASE_DIR.parent / "Work with claude")).resolve()
_DEFAULT_CODE = PROJECT_DIR if (PROJECT_DIR / "src" / "__init__.py").is_file() else BASE_DIR.parent / "Work with claude"
CODE_DIR = Path(os.environ.get("TFS_CODE_DIR", _DEFAULT_CODE)).resolve()
RESULTS_DIR = PROJECT_DIR / "results"                 # pipeline results (JSON / CSV / MD)
FIGURES_DIR = RESULTS_DIR / "figures"
MODELS_DIR = PROJECT_DIR / "models"                   # models/current/best_{h}d.joblib
MODEL_METADATA_DIR = PROJECT_DIR / "model_metadata"   # best_{h}d.json
PREDICTIONS_DIR = PROJECT_DIR / "predictions"

# ----------------------------------------------------------------------------- uploads
UPLOAD_FOLDER = BASE_DIR / "static" / "uploads"
UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
MAX_FILE_SIZE = int(os.environ.get("TFS_MAX_UPLOAD_MB", 20)) * 1024 * 1024   # same limit as the pipeline
ALLOWED_EXTENSIONS = {"csv", "xlsx"}
PREVIEW_ROWS = 10

# ----------------------------------------------------------------------------- flask
DEBUG = os.environ.get("TFS_DEBUG", "0") == "1"
TESTING = False
SECRET_KEY = os.environ.get("TFS_SECRET_KEY") or secrets.token_hex(16)
HOST = os.environ.get("TFS_HOST", "127.0.0.1")
PORT = int(os.environ.get("TFS_PORT", 5000))

# ----------------------------------------------------------------------------- pipeline files
RESULTS_JSON = "final_temperature_ml_results.json"     # master summary (Day 10)
FINAL_EVALUATION_JSON = "09_final_evaluation.json"      # decisions + candidates per horizon (Day 9)
LEADERBOARD_CSV = "09_leaderboard_test.csv"             # candidates + baselines on the test period
STATION_METRICS_CSV = "09_station_metrics.csv"          # production model, per station x horizon
BASELINE_RESULTS_JSON = "06_baseline_results.json"
BASELINE_STATION_CSV = "06_baseline_station_metrics.csv"
ELIGIBILITY_CSV = "02_station_eligibility.csv"
VALIDATION_JSON = "01_validation_report.json"
AUDIT_JSON = "audit_report.json"
SCREENING_JSON = "07_screening_summary.json"
IMPORTANCE_CSV = "09_importance_h{h}.csv"               # permutation importance per horizon
PREDICTIONS_FILE = "09_test_predictions.parquet"        # production-model test predictions

# Files that /api/download/<name> may serve (top level of RESULTS_DIR only)
DOWNLOAD_SUFFIXES = {".json", ".csv", ".md"}

# Shown first on the dashboard (the other result files are behind "show all")
KEY_DOWNLOADS = {
    "final_temperature_ml_results.json": "Master summary of the whole pipeline",
    "09_final_evaluation.json": "Final test-period evaluation and deployment decision per horizon",
    "09_leaderboard_test.csv": "Every tuned model and baseline on the test period",
    "09_station_metrics.csv": "Test metrics of the deployed models per station and horizon",
    "audit_report.json": "Leakage and consistency audit (13 checks)",
    "08_tuning_summary.json": "Hyperparameter tuning results per candidate",
    "07_screening_summary.json": "Model screening: top models per horizon",
    "06_baseline_table.csv": "Baseline results (persistence, climatology, recent mean)",
    "02_station_eligibility.csv": "Station coverage and training / forecast eligibility",
    "01_data_quality_summary.md": "Data quality summary of the source data",
}

# Titles for the pipeline's PNG figures (results/figures); others get a title from the file name
FIGURE_TITLES = {
    "eda_seasonal_cycle.png": "Seasonal cycle of daily maximum temperature",
    "eda_trend.png": "Long-term warming: annual mean anomaly over all stations",
    "eda_availability_heatmap.png": "Data availability by station and year",
    "eda_status_by_year.png": "Observed, filled and missing days by year",
    "eda_distribution.png": "Distribution of daily maximum temperature",
    "eda_autocorrelation.png": "Autocorrelation of temperature and its anomalies",
    "eda_gap_lengths.png": "Lengths of data gaps",
    "06_baselines_rmse_by_horizon.png": "Baseline error by forecast horizon",
    "09_permutation_importance.png": "Feature importance of the deployed models",
}

# A forecast issued from data older than this many days is flagged as stale on the forecast page
STALE_DATA_DAYS = 7

# Column names, date format, horizons and value ranges come from the pipeline's
# config/config.json (single source of truth) - they are NOT repeated here.
