"""Temperature Forecasting System (TFS) - Flask web application.

    python app.py            ->  http://127.0.0.1:5000

Reads the pipeline outputs from ``config.PROJECT_DIR`` (no database) and reuses the
pipeline's own code for forecasting (``src.forecasting.ForecastEngine``) and data
validation (``src.validation.validate_dataframe``), so the website applies exactly
the same features, models and rules as the training pipeline.

Pages:  /  /forecast  /model-performance  /data-upload
API:    /api/dashboard-data  /api/stations  /api/forecast  /api/outlook  /api/forecast-all  /api/model-metrics
        /api/upload  /api/downloads  /api/download/<filename>  /api/figures  /api/status
        /api/data-status  /api/apply-data  /api/pipeline-run  /api/pipeline-run/cancel
        /api/data-versions/restore

Uploading a file only validates it. Adding it to the pipeline's data and re-running the
analysis is a second, explicit step (``/api/apply-data``): new rows are merged into
``data/raw`` with a version snapshot (``data_versions``) and the pipeline then runs in the
background (``pipeline_runner``).
"""
from __future__ import annotations

import io
import json
import logging
import math
import os
import re
import socket
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
from flask import Flask, jsonify, render_template, request, send_from_directory
from werkzeug.exceptions import HTTPException
from werkzeug.utils import secure_filename

import config

if str(config.CODE_DIR) not in sys.path:
    sys.path.append(str(config.CODE_DIR))   # after the app folder: the pipeline also has an app.py

import data_versions                      # noqa: E402 - needs CODE_DIR on sys.path (uses src.validation)
from pipeline_runner import mode_options, runner  # noqa: E402

app = Flask(__name__)
app.config.update(UPLOAD_FOLDER=str(config.UPLOAD_FOLDER), MAX_CONTENT_LENGTH=config.MAX_FILE_SIZE,
                  SECRET_KEY=config.SECRET_KEY, TESTING=config.TESTING, JSON_SORT_KEYS=False)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
logger = logging.getLogger("tfs.webapp")


# =============================================================================
# Pipeline access (cached, reloaded when the underlying files change)
# =============================================================================

_lock = threading.Lock()
_file_cache: Dict[str, tuple] = {}
_pipeline: Dict[str, Any] = {}


def _mtime(path: Path) -> Optional[float]:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def get_settings():
    """Pipeline Settings for PROJECT_DIR (config/*.json). Raises TFSError/ConfigError if invalid."""
    cfg = config.PROJECT_DIR / "config" / "config.json"
    key = ("settings", _mtime(cfg))
    with _lock:
        if _pipeline.get("settings_key") != key:
            from src.config import load_settings
            from src.paths import ProjectPaths
            _pipeline["settings"] = load_settings(ProjectPaths(root=config.PROJECT_DIR))
            _pipeline["settings_key"] = key
        return _pipeline["settings"]


def get_engine():
    """Forecast engine (data + climatology + models); rebuilt when the model registry changes."""
    s = get_settings()
    key = (_mtime(s.paths.best_model_registry), _mtime(s.paths.processed / "02_daily_standardized.parquet"))
    with _lock:
        if _pipeline.get("engine_key") != key:
            from src.forecasting import ForecastEngine
            logger.info("Loading forecast engine (data, climatology, models)")
            _pipeline["engine"] = ForecastEngine(s)
            _pipeline["engine_key"] = key
        return _pipeline["engine"]


def load_json_results(filename: str) -> Optional[dict]:
    """Load a JSON file from the pipeline results folder (cached by modification time); None if missing."""
    path = config.RESULTS_DIR / filename
    mt = _mtime(path)
    if mt is None:
        return None
    cached = _file_cache.get(str(path))
    if cached and cached[0] == mt:
        return cached[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.error("Could not read %s: %s", path, exc)
        return None
    _file_cache[str(path)] = (mt, data)
    return data


def load_csv(filename: str) -> Optional[pd.DataFrame]:
    path = config.RESULTS_DIR / filename
    mt = _mtime(path)
    if mt is None:
        return None
    cached = _file_cache.get(str(path))
    if cached and cached[0] == mt:
        return cached[1]
    try:
        df = pd.read_csv(path)
    except (OSError, ValueError) as exc:
        logger.error("Could not read %s: %s", path, exc)
        return None
    _file_cache[str(path)] = (mt, df)
    return df


def clean(obj: Any) -> Any:
    """Make numpy/pandas values JSON-safe (NaN/inf -> None)."""
    if isinstance(obj, dict):
        return {str(k): clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating, float)):
        v = float(obj)
        return None if math.isnan(v) or math.isinf(v) else v
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, (pd.Timestamp, datetime)):
        return obj.strftime("%Y-%m-%d")
    return obj


def api_error(message: str, status: int = 400, code: str = "BAD_REQUEST", **extra):
    return jsonify(clean({"status": "error", "code": code, "message": message, **extra})), status


def horizons() -> List[int]:
    try:
        return list(get_settings().horizons)
    except Exception:  # noqa: BLE001 - config problems are reported by /api/status
        return []


def pipeline_ready() -> bool:
    return (config.MODELS_DIR / "current" / "best_models.json").exists()


# =============================================================================
# Helper functions (dashboard, leaderboard, stations, validation)
# =============================================================================

def get_eligible_stations(kind: str = "forecast") -> List[str]:
    """Station names for dropdowns.

    kind = "forecast"   stations the system can forecast (Day 3 eligibility)
           "evaluated"  stations with test-period metrics (Day 9)
           "all"        every station in the data
    """
    if kind == "evaluated":
        sm = load_csv(config.STATION_METRICS_CSV)
        return sorted(sm["station"].astype(str).unique()) if sm is not None else []
    el = load_csv(config.ELIGIBILITY_CSV)
    if el is None:
        return []
    if kind == "forecast":
        el = el[el["eligible_for_forecast"].astype(bool)]
    return sorted(el["station"].astype(str).unique())


def format_leaderboard() -> List[Dict[str, Any]]:
    """One row per horizon: best baseline vs ML model on the untouched test period."""
    fe = load_json_results(config.FINAL_EVALUATION_JSON)
    rows = []
    if fe:
        for h in horizons():
            info = fe.get("horizons", {}).get(f"h{h}")
            if not info:
                continue
            d, prod = info["decision"], info["production"]
            cands = info.get("candidates", [])
            ml = next((c for c in cands if c["model"] == d.get("cv_choice")), None)
            ml_rmse = ml["test"]["all"]["RMSE"] if ml else None
            base = d.get("best_baseline_test_RMSE")
            rows.append({
                "horizon": h, "baseline_name": d.get("best_baseline"), "baseline_rmse": base,
                "ml_model": d.get("cv_choice"), "ml_rmse": ml_rmse,
                "improvement_percent": 100 * (base - ml_rmse) / base if base and ml_rmse else None,
                "production_kind": prod.get("kind"), "production_model": prod.get("name"),
                "production_rmse": (prod.get("test") or {}).get("all", {}).get("RMSE"),
                "beats_baseline": bool(ml_rmse is not None and base is not None and ml_rmse < base),
                "status": "final"})
        return rows
    base = load_json_results(config.BASELINE_RESULTS_JSON)   # before Day 9: baselines only
    if base:
        for h in horizons():
            hb = base.get("horizons", {}).get(f"h{h}", {})
            name = hb.get("best_baseline_by_cv")
            if name:
                rows.append({"horizon": h, "baseline_name": name,
                             "baseline_rmse": hb["test"][name]["all"]["RMSE"], "ml_model": None, "ml_rmse": None,
                             "improvement_percent": None, "production_kind": None, "production_model": None,
                             "production_rmse": None, "beats_baseline": None, "status": "pending"})
    return rows


def calculate_summary_stats() -> Dict[str, Any]:
    """Everything the dashboard shows, from whichever pipeline outputs exist."""
    v = load_json_results(config.VALIDATION_JSON) or {}
    audit = load_json_results(config.AUDIT_JSON) or {}
    el = load_csv(config.ELIGIBILITY_CSV)
    lb = format_leaderboard()
    cal = v.get("calendar", {})
    prod_rmse = [r["production_rmse"] for r in lb if r.get("production_rmse") is not None]
    coverage = []
    if el is not None:
        for r in el.itertuples():
            days = max(int(r.calendar_days), 1)
            coverage.append({"station": r.station, "observed_pct": round(100 * r.observed_days / days, 2),
                             "filled_pct": round(100 * r.filled_days / days, 2),
                             "missing_pct": round(100 * (r.missing_days + r.masked_days) / days, 2),
                             "forecast_confidence": r.forecast_confidence})
    messages = []
    if not v:
        messages.append("Validation results not found - run the pipeline (Day 2).")
    if not pipeline_ready():
        messages.append("Final models are not trained yet - forecasts become available after Day 9. "
                        "Baseline results are shown where available.")
    return {
        "pipeline_ready": pipeline_ready(),
        "stations": get_eligible_stations("forecast"),
        "total_stations": v.get("stations", {}).get("count") or (len(el) if el is not None else 0),
        "forecastable_stations": len(get_eligible_stations("forecast")),
        "avg_rmse": float(np.mean(prod_rmse)) if prod_rmse else None,
        "data_period": f"{cal.get('overall_first_date', '?')} to {cal.get('overall_last_date', '?')}" if cal else None,
        "test_period": (load_json_results(config.FINAL_EVALUATION_JSON) or {}).get("test_period"),
        "horizons": horizons(),
        "leaderboard": lb,
        "available_models": sorted({r["production_model"] for r in lb if r.get("production_model")}),
        "coverage": coverage,
        "data_quality": {
            "missing_percent": cal.get("missing_pct"),
            "not_in_original_percent": v.get("provenance", {}).get("not_in_original_pct"),
            "stations_with_complete_data": int((el["missing_days"] == 0).sum()) if el is not None else None,
            "validation_status": v.get("status"),
        },
        "audit": {"pipeline_status": audit.get("pipeline_status"),
                  "data_leakage_detected": audit.get("data_leakage_detected")},
        "messages": messages,
    }


_FEATURE_LABELS = {
    "station_code": "Station", "year": "Year", "month": "Month", "day": "Day of month",
    "day_of_year": "Day of year", "iso_week": "Week of year", "quarter": "Quarter",
    "month_sin": "Month (seasonal sine)", "month_cos": "Month (seasonal cosine)",
    "doy_sin": "Day of year (seasonal sine)", "doy_cos": "Day of year (seasonal cosine)",
    "lag_0": "Max temp on the last data day", "lag_1": "Max temp 1 day earlier",
    "delta_1": "Change from the day before",
    "n_valid_7": "Valid values in the last 7 days", "lag_0_is_filled": "Last value is filled (not BMD)",
    "clim_doy": "Normal for the last data day", "clim_month_mean": "Monthly normal",
    "clim_month_std": "Monthly normal variability", "clim_season_mean": "Seasonal normal",
    "anom_0": "Anomaly on the last data day", "clim_fallback": "Uses the all-station normal",
    "clim_target": "Normal for the target date",
}
_FEATURE_PATTERNS = [
    (r"lag_(\d+)", "Max temp {} days earlier"), (r"roll_mean_(\d+)", "Mean of the last {} days"),
    (r"roll_std_(\d+)", "Variability of the last {} days"), (r"roll_min_(\d+)", "Lowest of the last {} days"),
    (r"roll_max_(\d+)", "Highest of the last {} days"), (r"anom_mean_(\d+)", "Mean anomaly of the last {} days"),
    (r"filled_share_(\d+)", "Share filled, last {} days"), (r"missing_share_(\d+)", "Share missing, last {} days"),
]


def feature_label(name: str) -> str:
    """Plain-language name of a model feature (the origin is the last day with data)."""
    if name in _FEATURE_LABELS:
        return _FEATURE_LABELS[name]
    for pattern, text in _FEATURE_PATTERNS:
        m = re.fullmatch(pattern, name)
        if m:
            return text.format(m.group(1))
    return name.replace("_", " ")


def data_age(origin: str) -> tuple:
    """(days between the last data day and today, warning text or None)."""
    age = (datetime.now().date() - datetime.strptime(origin, "%Y-%m-%d").date()).days
    if age <= config.STALE_DATA_DAYS:
        return age, None
    return age, (f"The latest data is from {origin} ({age} days ago), so these forecasts are for dates that have "
                 "already passed. Forecasting from today needs newer station data in the pipeline.")


def validate_csv(df: pd.DataFrame) -> Dict[str, Any]:
    """Validate an uploaded table with the pipeline's Day 2 rules (FR-DATA-001..015).

    Returns {is_valid, errors, warnings, report}. Required columns, date format and the
    plausible temperature range come from the pipeline configuration.
    """
    from src.validation import validate_dataframe

    s = get_settings()
    result = validate_dataframe(df, s)
    rep = result.report
    errors = list(rep.get("blocking_errors", []))
    warnings = []
    for check, info in rep.get("issue_counts", {}).items():
        if not info.get("count"):
            continue
        line = f"{check.replace('_', ' ')}: {info['count']:,} ({info.get('requirement', '')})"
        if info.get("severity") == "error":
            errors.append(line)
        elif info.get("severity") == "warning":
            warnings.append(line)
    return {"is_valid": result.status != "FAIL", "status": result.status, "errors": errors,
            "warnings": warnings, "report": rep}


def _read_upload(storage) -> pd.DataFrame:
    name = storage.filename.lower()
    raw = storage.read()
    if name.endswith(".csv"):
        return pd.read_csv(io.BytesIO(raw), dtype=str, keep_default_na=False, na_values=[""], encoding="utf-8-sig")
    df = pd.read_excel(io.BytesIO(raw), dtype=object, engine="openpyxl")
    for c in df.columns:  # Excel dates arrive as datetimes -> ISO text the validator accepts
        if pd.api.types.is_datetime64_any_dtype(df[c]) or df[c].map(lambda x: isinstance(x, datetime)).any():
            df[c] = pd.to_datetime(df[c], errors="coerce").dt.strftime("%Y-%m-%d")
    return df.astype("string")


# =============================================================================
# Pages
# =============================================================================

@app.route("/")
def dashboard():
    return render_template("dashboard.html", active="dashboard")


@app.route("/forecast")
def forecast_page():
    return render_template("forecast.html", active="forecast")


@app.route("/model-performance")
def model_performance_page():
    return render_template("model_performance.html", active="performance")


@app.route("/data-upload")
def data_upload_page():
    s = None
    try:
        s = get_settings()
    except Exception:  # noqa: BLE001
        pass
    schema = ({"station": s.col_station, "date": s.col_date, "value": s.col_temp, "date_format": s.date_format,
               "max_mb": config.MAX_FILE_SIZE // (1024 * 1024)} if s else None)
    return render_template("data_upload.html", active="upload", schema=schema)


# =============================================================================
# API
# =============================================================================

@app.route("/api/status")
def api_status():
    try:
        s = get_settings()
        cfg_ok, cfg_msg = True, None
    except Exception as exc:  # noqa: BLE001
        s, cfg_ok, cfg_msg = None, False, str(getattr(exc, "message", exc))
    return jsonify(clean({"status": "ok", "project_dir": str(config.PROJECT_DIR), "config_ok": cfg_ok,
                          "config_error": cfg_msg, "pipeline_ready": pipeline_ready(),
                          "horizons": list(s.horizons) if s else [],
                          "files": {f: (config.RESULTS_DIR / f).exists() for f in
                                    [config.VALIDATION_JSON, config.BASELINE_RESULTS_JSON, config.FINAL_EVALUATION_JSON,
                                     config.RESULTS_JSON, config.AUDIT_JSON]}}))


@app.route("/api/dashboard-data", methods=["GET", "POST"])
def api_dashboard_data():
    try:
        return jsonify(clean({"status": "ok", **calculate_summary_stats()}))
    except Exception as exc:  # noqa: BLE001
        logger.exception("dashboard-data failed")
        return api_error(f"Could not build dashboard data: {getattr(exc, 'message', exc)}", 500, "SERVER_ERROR")


@app.route("/api/stations")
def api_stations():
    kind = request.args.get("kind", "forecast")
    if kind not in {"forecast", "evaluated", "all"}:
        return api_error("kind must be forecast, evaluated or all")
    return jsonify({"status": "ok", "kind": kind, "stations": get_eligible_stations(kind)})


@app.route("/api/forecast", methods=["POST"])
def api_forecast():
    body = request.get_json(silent=True) or {}
    station = str(body.get("station", "")).strip()
    try:
        horizon = int(body.get("horizon"))
    except (TypeError, ValueError):
        return api_error("horizon must be an integer number of days")
    if not station:
        return api_error("Please select a station")
    hs = horizons()
    if horizon not in hs:
        return api_error(f"Horizon {horizon} days is not available. Available horizons: {hs}", code="BAD_HORIZON")
    eng, fc = _run_forecast(station, [horizon])
    if eng is None:
        return fc
    return jsonify(clean({"status": "success", "station": fc["station"], "horizon": horizon,
                          **_forecast_row(fc["forecasts"][0]),
                          "predicted_min_temp": None,
                          "note": "The system forecasts daily maximum temperature only.",
                          **_forecast_context(eng, fc)}))


@app.route("/api/outlook", methods=["POST"])
def api_outlook():
    """All horizons for one station in one call (the full 1-15 day outlook)."""
    body = request.get_json(silent=True) or {}
    station = str(body.get("station", "")).strip()
    if not station:
        return api_error("Please select a station")
    eng, fc = _run_forecast(station, horizons())
    if eng is None:
        return fc
    return jsonify(clean({"status": "success", "station": fc["station"],
                          "forecasts": [_forecast_row(f) for f in fc["forecasts"]],
                          **_forecast_context(eng, fc)}))


@app.route("/api/forecast-all", methods=["POST"])
def api_forecast_all():
    """Every forecastable station at once: one horizon (``{"horizon": 3}``) or, without it, every horizon.

    Stations the engine cannot forecast (e.g. too little recent data) are listed in ``skipped``.
    """
    body = request.get_json(silent=True) or {}
    hs = horizons()
    if body.get("horizon") in (None, ""):
        req = hs
    else:
        try:
            h = int(body.get("horizon"))
        except (TypeError, ValueError):
            return api_error("horizon must be an integer number of days")
        if h not in hs:
            return api_error(f"Horizon {h} days is not available. Available horizons: {hs}", code="BAD_HORIZON")
        req = [h]
    eng, err = _engine_or_error()
    if eng is None:
        return err
    lb = {r["horizon"]: r for r in format_leaderboard()}
    rows, skipped = [], []
    for station in get_eligible_stations("forecast"):
        fc = eng.forecast(station, req)
        if fc.get("status") != "ok":
            e = fc.get("error", {})
            skipped.append({"station": station, "code": e.get("code"), "message": e.get("message")})
            continue
        age, stale = data_age(fc["origin_date"])
        rows.append({"station": fc["station"], "origin_date": fc["origin_date"], "data_age_days": age,
                     "forecast_confidence": fc.get("forecast_confidence"),
                     "warnings": ([stale] if stale else []) + fc.get("warnings", []),
                     "input_quality": fc.get("input_quality"),
                     "forecasts": [_forecast_row(f, lb) for f in fc["forecasts"]]})
    return jsonify(clean({"status": "success", "horizons": req, "stations": rows, "skipped": skipped}))


def _engine_or_error():
    """(engine, None) or (None, error response) when there are no models or they cannot be loaded."""
    if not pipeline_ready():
        return None, api_error("No trained models yet - run the pipeline up to Day 9 first.", 503, "MODEL_NOT_FOUND")
    try:
        return get_engine(), None
    except Exception as exc:  # noqa: BLE001
        logger.exception("engine load failed")
        return None, api_error(f"Could not load models: {getattr(exc, 'message', exc)}", 500, "ENGINE_ERROR")


def _run_forecast(station: str, hs: List[int]):
    """(engine, forecast) from the pipeline's ForecastEngine, or (None, error response)."""
    eng, err = _engine_or_error()
    if eng is None:
        return None, err
    fc = eng.forecast(station, hs)
    if fc.get("status") != "ok":
        err = fc.get("error", {})
        return None, api_error(err.get("message", "Forecast failed"), 422, err.get("code", "FORECAST_ERROR"))
    return eng, fc


def _forecast_row(f: Dict[str, Any], leaderboard: Optional[Dict[int, dict]] = None) -> Dict[str, Any]:
    """One horizon of an engine forecast, with the test-period comparison against the best baseline."""
    if leaderboard is None:
        leaderboard = {r["horizon"]: r for r in format_leaderboard()}
    lb = leaderboard.get(f["horizon_days"], {})
    base, model_rmse = lb.get("baseline_rmse"), f.get("test_rmse_c")
    return {"horizon": f["horizon_days"], "forecast_date": f["target_date"], "predicted_max_temp": f["forecast_c"],
            "confidence_range": f.get("interval_80_c"), "confidence_level": 0.8,
            "climatology_temp": f.get("climatology_c"),
            "model_name": f["model"], "model_kind": f["model_kind"], "model_rmse": model_rmse,
            "baseline_name": lb.get("baseline_name"), "baseline_rmse": base,
            "improvement_percent": 100 * (base - model_rmse) / base if base and model_rmse else None}


def _forecast_context(eng, fc: Dict[str, Any]) -> Dict[str, Any]:
    """Origin, data age, confidence, warnings and recent history shared by forecast and outlook."""
    age, stale = data_age(fc["origin_date"])
    hist = eng.history(fc["station"])
    return {"origin_date": fc["origin_date"], "data_age_days": age,
            "forecast_confidence": fc.get("forecast_confidence"),
            "warnings": ([stale] if stale else []) + fc.get("warnings", []),
            "input_quality": fc.get("input_quality"),
            "history": [{"date": d, "value": v, "observed": bool(o), "filled": bool(fl)} for d, v, o, fl in
                        zip(hist["date"], hist["value_clean"], hist["is_observed"], hist["is_filled"])]}


@app.route("/api/model-metrics", methods=["POST"])
def api_model_metrics():
    body = request.get_json(silent=True) or {}
    station = str(body.get("station", "")).strip().upper()
    sm = load_csv(config.STATION_METRICS_CSV)
    if sm is None:
        return api_error("Station metrics not available yet - run the final evaluation (Day 9).", 503, "NOT_READY")
    sm = sm.assign(station=sm["station"].astype(str))
    rows = sm[sm["station"] == station].sort_values("horizon")
    if rows.empty:
        return api_error(f"No test-period metrics for station '{station}'", 404, "UNKNOWN_STATION")
    hs = horizons()
    try:
        sel_h = int(body.get("horizon") or hs[0])
    except (TypeError, ValueError):
        return api_error("horizon must be an integer")
    if sel_h not in hs:
        return api_error(f"Horizon {sel_h} not available: {hs}", code="BAD_HORIZON")

    clim = {}
    bs = load_csv(config.BASELINE_STATION_CSV)
    if bs is not None:
        c = bs[(bs["station"].astype(str) == station) & (bs["baseline"] == "DOYClimatology")]
        clim = dict(zip(c["horizon"].astype(int), c["RMSE"]))
    overall = {r["horizon"]: r["production_rmse"] for r in format_leaderboard()}
    metrics = [{"horizon": int(r.horizon), "rmse": r.RMSE, "mae": r.MAE, "r_squared": r.R2, "bias": r.Bias,
                "n": int(r.n), "production_model": r.production,
                "climatology_rmse": clim.get(int(r.horizon)),
                "skill_vs_climatology": (1 - r.RMSE / clim[int(r.horizon)]) if clim.get(int(r.horizon)) else None,
                "all_stations_rmse": overall.get(int(r.horizon))} for r in rows.itertuples()]

    imp = load_csv(config.IMPORTANCE_CSV.format(h=sel_h))
    importance = ([{"feature": r.feature, "label": feature_label(r.feature), "importance": r.rmse_increase}
                   for r in imp.head(10).itertuples()] if imp is not None else [])
    residuals: List[float] = []
    pred_path = config.PREDICTIONS_DIR / config.PREDICTIONS_FILE
    if pred_path.exists():
        p = pd.read_parquet(pred_path, columns=["station", "horizon", "y_true", "y_pred"])
        p = p[(p["station"].astype(str) == station) & (p["horizon"] == sel_h)]
        residuals = (p["y_pred"] - p["y_true"]).round(3).tolist()

    meta_path = config.MODEL_METADATA_DIR / f"best_{sel_h}d.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    fe = load_json_results(config.FINAL_EVALUATION_JSON) or {}
    cand = next((c for c in fe.get("horizons", {}).get(f"h{sel_h}", {}).get("candidates", [])
                 if c["model"] == meta.get("model")), {})
    dev = meta.get("periods", {}).get("development_origins", [None, None])
    late = bool(rows["late_start"].iloc[0]) if "late_start" in rows else False
    return jsonify(clean({
        "status": "ok", "station": station, "selected_horizon": sel_h, "horizons": hs,
        "late_start_station": late, "metrics_by_horizon": metrics,
        "feature_importance": importance,
        "feature_importance_note": "Permutation importance of the production model (all stations, test sample): "
                                   "increase in RMSE (°C) when the feature is shuffled.",
        "residuals": residuals,
        "model_info": {"model_type": meta.get("model"), "kind": meta.get("kind"), "profile": meta.get("profile"),
                       "training_period": f"{dev[0]} to {dev[1]}" if dev[0] else None,
                       "test_period": " to ".join(meta.get("periods", {}).get("test", [])) or None,
                       "num_features": len(meta.get("feature_names", [])) or None,
                       "training_samples": cand.get("n_train"), "params": meta.get("params")},
    }))


@app.route("/api/upload", methods=["POST"])
def api_upload():
    f = request.files.get("file")
    if f is None or not f.filename:
        return api_error("No file received (form field 'file').")
    ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
    if ext not in config.ALLOWED_EXTENSIONS:
        return api_error("Only .csv and .xlsx files are accepted.", code="BAD_FILE_TYPE",
                         errors=[f"Unsupported file type '.{ext}'"])
    try:
        get_settings()
        df = _read_upload(f)
    except Exception as exc:  # noqa: BLE001
        return api_error("The file could not be read.", code="UNREADABLE_FILE",
                         errors=[f"{type(exc).__name__}: {getattr(exc, 'message', exc)}"])
    v = validate_csv(df)
    rep = v["report"]
    if not v["is_valid"]:
        return jsonify(clean({"status": "error", "message": "Validation failed", "errors": v["errors"],
                              "warnings": v["warnings"], "rows": len(df)}))
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    saved = f"{stamp}_{secure_filename(f.filename) or 'upload.' + ext}"
    f.stream.seek(0)
    f.save(config.UPLOAD_FOLDER / saved)
    cal = rep.get("calendar", {})
    stations = sorted(df[rep["schema"]["mapped"]["station"]].dropna().astype(str).str.strip().str.upper().unique())
    return jsonify(clean({
        "status": "success", "validation_status": v["status"],
        "message": "Data validated successfully" + (" (with warnings)" if v["warnings"] else ""),
        "rows": len(df), "columns": [str(c) for c in df.columns], "stations": stations,
        "date_range": f"{cal.get('overall_first_date')} to {cal.get('overall_last_date')}",
        "missing_values": {str(c): int(df[c].isna().sum() + (df[c].astype(str).str.strip() == "").sum())
                           for c in df.columns},
        "missing_days": cal.get("missing_days_total"), "warnings": v["warnings"], "errors": v["errors"],
        "saved_as": saved,
        "merge": merge_preview(df),
        "note": "Validation only so far: use \"Add to pipeline\" below to merge the new rows and re-run.",
    }))


def merge_preview(df: pd.DataFrame) -> Optional[Dict[str, Any]]:
    """How many rows of an uploaded file are missing from the pipeline's data file (no writes)."""
    try:
        p = data_versions.plan(get_settings(), df)
    except Exception as exc:  # noqa: BLE001 - the upload itself stays valid if this cannot be worked out
        logger.warning("Could not compare the upload with the data file: %s", exc)
        return None
    return {k: v for k, v in p.items() if not k.startswith("_")}


def _saved_upload(name: str) -> Path:
    """Resolve a file inside static/uploads by name, refusing anything outside it."""
    safe = secure_filename(name or "")
    path = (config.UPLOAD_FOLDER / safe).resolve()
    if not safe or config.UPLOAD_FOLDER.resolve() not in path.parents or not path.is_file():
        raise FileNotFoundError("That uploaded file is no longer available. Please upload it again.")
    return path


def _read_saved(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[""], encoding="utf-8-sig")
    df = pd.read_excel(path, dtype=object, engine="openpyxl")
    for c in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[c]) or df[c].map(lambda x: isinstance(x, datetime)).any():
            df[c] = pd.to_datetime(df[c], errors="coerce").dt.strftime("%Y-%m-%d")
    return df.astype("string")


# =============================================================================
# Data updates and pipeline runs
# =============================================================================

@app.route("/api/data-status")
def api_data_status():
    """The pipeline's data file, its version history and the available run modes."""
    try:
        summary = data_versions.summary(get_settings())
    except Exception as exc:  # noqa: BLE001
        return api_error(f"The data file could not be read: {exc}", code="DATA_UNAVAILABLE", status=503)
    return jsonify(clean({"status": "success", "data": summary, "modes": mode_options(),
                          "run": runner.state(), "busy": runner.busy}))


@app.route("/api/apply-data", methods=["POST"])
def api_apply_data():
    """Merge a validated upload into the pipeline's data file and start a run.

    Body: ``{"file": "<saved_as from /api/upload>", "mode": "refresh"|"full"}``.
    Only rows whose station and date are not already in the data file are added; the previous
    file is snapshotted first and restored automatically if the run fails validation.
    """
    body = request.get_json(silent=True) or {}
    mode = str(body.get("mode") or "refresh")
    if mode not in {m["key"] for m in mode_options()}:
        return api_error(f"Unknown run mode '{mode}'.", code="BAD_MODE")
    if runner.busy:
        return api_error("A pipeline run is already in progress.", code="RUN_IN_PROGRESS", status=409,
                         run=runner.state())
    try:
        path = _saved_upload(str(body.get("file") or ""))
    except FileNotFoundError as exc:
        return api_error(str(exc), code="UPLOAD_NOT_FOUND", status=404)

    try:
        s = get_settings()
        df = _read_saved(path)
        v = validate_csv(df)
        if not v["is_valid"]:
            return api_error("That file does not pass validation, so it was not added.",
                             code="VALIDATION_FAILED", errors=v["errors"])
        plan = data_versions.plan(s, df)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Could not prepare the data update")
        return api_error(f"The data update could not be prepared: {type(exc).__name__}: {exc}",
                         code="MERGE_FAILED", status=500)

    public = {k: v for k, v in plan.items() if not k.startswith("_")}
    if not plan["new_rows"]:
        return api_error("Every row in that file is already in the pipeline's data, so there is "
                         "nothing to add.", code="NOTHING_NEW", merge=public)

    # Rebuilding the features requires that no target falls on or after the end of the test period,
    # so observations beyond it can only be used either without rebuilding features ("data" mode) or
    # by moving the test period, which changes the split and invalidates screening and tuning.
    extend = bool(plan["extends_past_test_end"] and body.get("extend_test_period"))
    if plan["extends_past_test_end"] and mode != "data":
        if not extend:
            return api_error(
                f"{plan['rows_past_test_end']:,} of the new rows are dated after the end of the test "
                f"period ({plan['test_end_date']}). Rebuilding the features needs that period to end on "
                f"the last observation. Either update the data only - the current models then forecast "
                f"from the new days straight away - or move the test period to "
                f"{plan['suggested_test_end']}, which changes how the models are scored and requires a "
                f"full re-analysis (\"extend_test_period\": true with \"mode\": \"full\").",
                code="TEST_PERIOD_TOO_SHORT", merge=public)
        if mode != "full":
            return api_error(
                "Moving the test period changes the data split, so the saved screening and tuning "
                "results no longer apply. Choose the full re-analysis for this update.",
                code="FULL_RUN_REQUIRED", merge=public)

    cfg_change = None
    try:
        if extend and mode == "full":
            cfg_change = data_versions.set_test_end(s, plan["suggested_test_end"])
        entry = data_versions.commit(s, plan, path.name)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Could not write the data file")
        if cfg_change and cfg_change.get("config_backup"):
            data_versions.restore_config(s, cfg_change["config_backup"])
        return api_error(f"The data file could not be updated: {type(exc).__name__}: {exc}",
                         code="WRITE_FAILED", status=500)

    _pipeline.clear()          # the test period may have changed: reload the pipeline settings
    _file_cache.clear()
    try:
        run = runner.start(mode, data_version=entry, rollback_to=entry.get("restores_from"),
                           restore_config=(cfg_change or {}).get("config_backup"),
                           note=f"{entry['rows_added']} new rows from {path.name}")
    except RuntimeError as exc:
        return api_error(str(exc), code="RUN_IN_PROGRESS", status=409)
    logger.info("Data updated (+%s rows) and %s run started", entry["rows_added"], mode)
    return jsonify(clean({"status": "success", "version": entry, "merge": public,
                          "test_period_change": cfg_change, "run": run}))


@app.route("/api/pipeline-run")
def api_pipeline_run():
    """Progress of the current (or last) run, for polling."""
    return jsonify(clean({"status": "success", "run": runner.state(), "busy": runner.busy}))


@app.route("/api/pipeline-run/cancel", methods=["POST"])
def api_pipeline_cancel():
    if not runner.cancel():
        return api_error("There is no run to cancel.", code="NO_RUN")
    return jsonify(clean({"status": "success", "message": "Stopping the run…", "run": runner.state()}))


@app.route("/api/pipeline-run/start", methods=["POST"])
def api_pipeline_start():
    """Re-run the pipeline on the data that is already in place (no upload involved)."""
    body = request.get_json(silent=True) or {}
    mode = str(body.get("mode") or "refresh")
    if mode not in {m["key"] for m in mode_options()}:
        return api_error(f"Unknown run mode '{mode}'.", code="BAD_MODE")
    try:
        run = runner.start(mode, note="re-run on the current data")
    except RuntimeError as exc:
        return api_error(str(exc), code="RUN_IN_PROGRESS", status=409, run=runner.state())
    return jsonify(clean({"status": "success", "run": run}))


@app.route("/api/data-versions/restore", methods=["POST"])
def api_restore_version():
    """Put an earlier data version back, optionally re-running the pipeline on it."""
    body = request.get_json(silent=True) or {}
    version_id = str(body.get("id") or "")
    if runner.busy:
        return api_error("A pipeline run is in progress; wait for it to finish first.",
                         code="RUN_IN_PROGRESS", status=409)
    try:
        s = get_settings()
        entry = data_versions.restore(s, version_id)
    except (ValueError, FileNotFoundError) as exc:
        return api_error(str(exc), code="UNKNOWN_VERSION", status=404)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Could not restore the data version")
        return api_error(f"The version could not be restored: {type(exc).__name__}: {exc}",
                         code="RESTORE_FAILED", status=500)
    run = None
    if body.get("rerun"):
        mode = str(body.get("mode") or "refresh")
        try:
            run = runner.start(mode, note=f"re-run after restoring {version_id}")
        except (RuntimeError, ValueError) as exc:
            return api_error(str(exc), code="RUN_NOT_STARTED", status=409, version=entry)
    return jsonify(clean({"status": "success", "version": entry, "run": run,
                          "message": f"Data version {version_id} restored."
                                     + ("" if run else " The results still come from the previous run -"
                                                       " re-run the pipeline to match the data.")}))


@app.route("/api/downloads")
def api_downloads():
    """Result files; the key reports (config.KEY_DOWNLOADS) come first with a description."""
    files = []
    if config.RESULTS_DIR.exists():
        for p in sorted(config.RESULTS_DIR.iterdir()):
            if p.is_file() and p.suffix.lower() in config.DOWNLOAD_SUFFIXES:
                files.append({"name": p.name, "size_kb": round(p.stat().st_size / 1024, 1),
                              "modified": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                              "key": p.name in config.KEY_DOWNLOADS,
                              "description": config.KEY_DOWNLOADS.get(p.name)})
    order = list(config.KEY_DOWNLOADS)
    files.sort(key=lambda f: (order.index(f["name"]) if f["key"] else len(order), f["name"]))
    return jsonify({"status": "ok", "files": files})


@app.route("/api/figures")
def api_figures():
    """PNG figures written by the pipeline (results/figures), known ones first."""
    figs = []
    if config.FIGURES_DIR.exists():
        order = list(config.FIGURE_TITLES)
        for p in sorted(config.FIGURES_DIR.glob("*.png"),
                        key=lambda p: (order.index(p.name) if p.name in order else len(order), p.name)):
            title = config.FIGURE_TITLES.get(p.name) or p.stem.replace("_", " ").capitalize()
            figs.append({"name": p.name, "title": title, "url": f"/figures/{p.name}"})
    return jsonify({"status": "ok", "figures": figs})


@app.route("/api/download/<path:filename>")
def api_download(filename: str):
    safe = secure_filename(filename)
    if not safe or safe != filename or Path(filename).suffix.lower() not in config.DOWNLOAD_SUFFIXES:
        return api_error("Invalid file name.", 400, "BAD_FILENAME")
    path = config.RESULTS_DIR / safe
    if not path.is_file() or path.resolve().parent != config.RESULTS_DIR.resolve():
        return api_error(f"File '{safe}' not found in results.", 404, "NOT_FOUND")
    return send_from_directory(config.RESULTS_DIR, safe, as_attachment=True)


@app.route("/figures/<path:filename>")
def pipeline_figure(filename: str):
    safe = secure_filename(filename)
    if not safe or safe != filename or not safe.lower().endswith(".png"):
        return api_error("Invalid figure name.", 400, "BAD_FILENAME")
    return send_from_directory(config.FIGURES_DIR, safe)


# =============================================================================
# Errors
# =============================================================================

@app.errorhandler(413)
def too_large(_e):
    return api_error(f"File too large (max {config.MAX_FILE_SIZE // (1024 * 1024)} MB).", 413, "FILE_TOO_LARGE",
                     errors=["File too large"])


@app.errorhandler(HTTPException)
def http_error(e: HTTPException):
    if request.path.startswith("/api/"):
        return api_error(e.description or e.name, e.code or 500, e.name.upper().replace(" ", "_"))
    return render_template("error.html", code=e.code, title=e.name, message=e.description), e.code


@app.errorhandler(Exception)
def server_error(e: Exception):
    logger.exception("Unhandled error on %s", request.path)
    if request.path.startswith("/api/"):
        return api_error("Internal server error - see the server log.", 500, "SERVER_ERROR")
    return render_template("error.html", code=500, title="Server error",
                           message="Something went wrong. See the server log for details."), 500


@app.context_processor
def inject_globals():
    return {"project_dir": str(config.PROJECT_DIR), "year": datetime.now().year}


def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1" if host == "0.0.0.0" else host, port)) == 0


if __name__ == "__main__":
    # Windows lets a second server bind the same port, and the two then answer at random.
    # (Not in the debug reloader's child process: its parent already holds the port.)
    if not os.environ.get("WERKZEUG_RUN_MAIN") and port_in_use(config.HOST, config.PORT):
        logger.error("Port %d is already in use - the app is probably already running in another window "
                     "(open http://%s:%d). Close that window first, or set TFS_PORT to another port.",
                     config.PORT, config.HOST, config.PORT)
        sys.exit(1)
    logger.info("TFS web app - project: %s", config.PROJECT_DIR)
    app.run(host=config.HOST, port=config.PORT, debug=config.DEBUG)
