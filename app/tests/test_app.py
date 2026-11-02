"""Tests for the TFS Flask app against a small synthetic project built with the real pipeline.

    cd app
    ..\\"Work with claude"\\.venv\\Scripts\\python.exe -m pytest

The fixture runs the actual pipeline stages (validate -> prepare -> split -> features ->
baselines -> screening -> tuning -> final evaluation -> audit) on 2 synthetic stations,
so every route is exercised with real output files.
"""
from __future__ import annotations

import importlib
import io
import json
import os
import shutil
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

APP_DIR = Path(__file__).resolve().parents[1]
CODE_DIR = Path(os.environ.get("TFS_CODE_DIR", APP_DIR.parent / "Work with claude")).resolve()
sys.path.insert(0, str(APP_DIR))       # the app's app.py / config.py must win over the pipeline's app.py
sys.path.append(str(CODE_DIR))


def _synthetic_raw(path: Path) -> None:
    rng = np.random.default_rng(0)
    parts = []
    for i, st in enumerate(["Rangpur", "Dhaka"]):
        dates = pd.date_range("2016-01-01", "2021-12-31", freq="D")
        anom = np.zeros(len(dates))
        for t in range(1, len(dates)):
            anom[t] = 0.7 * anom[t - 1] + rng.normal(0, 1)
        v = 30 + i + 4 * np.sin(2 * np.pi * np.arange(len(dates)) / 365.25) + anom
        parts.append(pd.DataFrame({"station": st, "Date": dates.strftime("%d/%m/%Y"), "value": v.round(1)}))
    pd.concat(parts).to_csv(path, index=False)


def _build_project(root: Path) -> None:
    from src import screening as SC
    from src import tuning as TU
    from src.audit import run_audit
    from src.baselines import run_stage as run_baselines
    from src.config import load_settings
    from src.datasets import DatasetBuilder, load_feature_table
    from src.feature_engineering import load_contract, run_features
    from src.paths import ProjectPaths
    from src.preparation import load_standardized, prepare, write_outputs as write_prep
    from src.reporting import build_master
    from src.splitting import build_split, load_split, split_config_hash, write_outputs as write_split
    from src.training import run_final
    from src.utils import read_json, write_json
    from src.validation import validate_file, write_outputs as write_val

    shutil.copytree(CODE_DIR / "config", root / "config")
    cfg = read_json(root / "config" / "config.json")
    cfg["source"].pop("reference_original", None)
    cfg["forecast_horizons"] = [1, 15]
    cfg["split"].update({"test_start_date": "2021-01-01", "test_end_date": "2021-12-31",
                         "min_test_targets_per_station": 100})
    cfg["cv"].update({"folds": 2, "validation_days": 150})
    cfg["screening"].update({"train_start_date": None, "n_jobs": 1})
    cfg["preparation"].update({"max_days_since_last_value_for_forecast": 60})
    write_json(root / "config" / "config.json", cfg)
    f = read_json(root / "config" / "features.json")
    f["min_clim_observed_days"] = 300
    write_json(root / "config" / "features.json", f)
    m = read_json(root / "config" / "models.json")
    for name in m["models"]:
        m["models"][name]["enabled"] = name in ("Ridge", "LightGBM")
    write_json(root / "config" / "models.json", m)
    t = read_json(root / "config" / "tuning.json")
    t["optimization"].update({"startup_trials": 1, "pruner_startup_trials": 2})
    t["search_spaces"]["LightGBM"] = {"n_estimators": ["int", 20, 40], "num_leaves": ["int", 4, 16]}
    t["budget"] = {}
    write_json(root / "config" / "tuning.json", t)

    p = ProjectPaths(root=root)
    p.ensure()
    s = load_settings(p)
    _synthetic_raw(s.raw_data_file)
    write_val(validate_file(s), s)
    prep = prepare(s)
    write_prep(prep, s)
    write_split(build_split(prep.daily, s, prep.eligibility), s)
    run_features(s)
    run_baselines(s)
    spec, folds = load_split(s)
    fp = SC.run_fingerprint(s, load_contract(s)["contract_hash"], split_config_hash(s))
    b = DatasetBuilder(load_feature_table(s), load_standardized(s), spec, folds, s)
    SC.run_screening(s, b, fp)
    SC.build_leaderboards(s, fp, len(folds))
    for c in TU.candidates(s):
        TU.run_study(s, b, c["horizon"], c["model"], c["profile"], fp, n_trials=2)
    TU.build_summary(s, fp)
    run_final(s)
    run_audit(s)
    build_master(s)


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    root = tmp_path_factory.mktemp("tfs_project")
    _build_project(root)
    return root


@pytest.fixture(scope="module")
def client(project):
    os.environ["TFS_PROJECT_DIR"] = str(project)
    os.environ["TFS_CODE_DIR"] = str(CODE_DIR)
    import config
    importlib.reload(config)
    import app as webapp
    webapp = importlib.reload(webapp)
    webapp.app.config["TESTING"] = True
    webapp._pipeline.clear()
    webapp._file_cache.clear()
    with webapp.app.test_client() as c:
        c.webapp = webapp
        yield c


# ----------------------------------------------------------------------------- pages
@pytest.mark.parametrize("url", ["/", "/forecast", "/model-performance", "/data-upload"])
def test_pages_render(client, url):
    r = client.get(url)
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "TFS Dashboard" in html and "plotly.min.js" in html


def test_not_found_html_and_json(client):
    assert client.get("/nope").status_code == 404
    r = client.get("/api/nope")
    assert r.status_code == 404 and r.get_json()["status"] == "error"


def test_static_assets(client):
    for p in ["/static/css/style.css", "/static/js/common.js", "/static/vendor/plotly.min.js"]:
        assert client.get(p).status_code == 200


# ----------------------------------------------------------------------------- dashboard
def test_status_and_dashboard(client):
    st = client.get("/api/status").get_json()
    assert st["config_ok"] and st["pipeline_ready"] and st["horizons"] == [1, 15]
    d = client.post("/api/dashboard-data", json={}).get_json()
    assert d["status"] == "ok" and d["total_stations"] == 2
    assert d["stations"] == ["DHAKA", "RANGPUR"] and d["horizons"] == [1, 15]
    assert [r["horizon"] for r in d["leaderboard"]] == [1, 15]
    for r in d["leaderboard"]:
        assert r["baseline_rmse"] > 0 and r["ml_rmse"] > 0 and r["status"] == "final"
        assert r["production_kind"] in ("model", "baseline")
    assert d["avg_rmse"] > 0 and len(d["coverage"]) == 2
    assert d["audit"]["pipeline_status"] == "PASS" and d["audit"]["data_leakage_detected"] is False
    assert d["data_quality"]["validation_status"] in ("PASS", "PASS_WITH_ISSUES")


# ----------------------------------------------------------------------------- forecast
def test_forecast_success_matches_engine(client):
    r = client.post("/api/forecast", json={"station": "dhaka", "horizon": 15})
    assert r.status_code == 200, r.get_json()
    d = r.get_json()
    assert d["status"] == "success" and d["station"] == "DHAKA" and d["horizon"] == 15
    assert d["origin_date"] == "2021-12-31" and d["forecast_date"] == "2022-01-15"
    lo, hi = d["confidence_range"]
    assert lo < d["predicted_max_temp"] < hi
    assert d["predicted_min_temp"] is None and d["model_name"]
    assert d["baseline_rmse"] > 0 and len(d["history"]) == 60
    ref = client.webapp.get_engine().forecast("DHAKA", [15])["forecasts"][0]["forecast_c"]
    assert d["predicted_max_temp"] == ref


def test_outlook_all_horizons(client):
    d = client.post("/api/outlook", json={"station": "Dhaka"}).get_json()
    assert d["status"] == "success" and d["station"] == "DHAKA"
    assert [f["horizon"] for f in d["forecasts"]] == [1, 15]
    single = client.post("/api/forecast", json={"station": "DHAKA", "horizon": 15}).get_json()
    last = d["forecasts"][-1]
    assert last["predicted_max_temp"] == single["predicted_max_temp"]
    assert last["forecast_date"] == single["forecast_date"] == "2022-01-15"
    assert last["confidence_range"] == single["confidence_range"] and last["baseline_rmse"] > 0
    assert d["origin_date"] == "2021-12-31" and len(d["history"]) == 60
    # the synthetic data ends in 2021, so the forecast is flagged as issued from old data
    assert d["data_age_days"] > 7 and "already passed" in d["warnings"][0]
    assert single["data_age_days"] == d["data_age_days"] and "already passed" in single["warnings"][0]


def test_outlook_errors(client):
    assert client.post("/api/outlook", json={}).status_code == 400
    r = client.post("/api/outlook", json={"station": "ATLANTIS"})
    assert r.status_code == 422 and r.get_json()["code"] == "UNKNOWN_STATION"


def test_forecast_all_stations_one_horizon(client):
    d = client.post("/api/forecast-all", json={"horizon": 15}).get_json()
    assert d["status"] == "success" and d["horizons"] == [15] and d["skipped"] == []
    assert [r["station"] for r in d["stations"]] == client.webapp.get_eligible_stations("forecast")
    for r in d["stations"]:
        single = client.post("/api/forecast", json={"station": r["station"], "horizon": 15}).get_json()
        (f,) = r["forecasts"]
        assert f["predicted_max_temp"] == single["predicted_max_temp"]
        assert f["confidence_range"] == single["confidence_range"] and f["forecast_date"] == single["forecast_date"]
        assert f["baseline_rmse"] == single["baseline_rmse"] > 0
        assert r["origin_date"] == single["origin_date"] and r["warnings"] == single["warnings"]
        assert "history" not in r


def test_forecast_all_stations_every_horizon(client):
    d = client.post("/api/forecast-all", json={}).get_json()
    assert d["status"] == "success" and d["horizons"] == [1, 15] and len(d["stations"]) == 2
    for r in d["stations"]:
        outlook = client.post("/api/outlook", json={"station": r["station"]}).get_json()
        assert r["forecasts"] == outlook["forecasts"] and r["data_age_days"] == outlook["data_age_days"]


def test_forecast_all_stations_errors(client):
    r = client.post("/api/forecast-all", json={"horizon": 4})
    assert r.status_code == 400 and r.get_json()["code"] == "BAD_HORIZON"
    assert client.post("/api/forecast-all", json={"horizon": "soon"}).status_code == 400


def test_forecast_all_skips_stations_that_cannot_be_forecast(client, monkeypatch):
    eng = client.webapp.get_engine()
    real = eng.forecast

    def flaky(station, hs):
        if station == "RANGPUR":
            return {"station": station, "status": "error",
                    "error": {"code": "INSUFFICIENT_RECENT_DATA", "message": "too few recent values"}}
        return real(station, hs)

    monkeypatch.setattr(eng, "forecast", flaky)
    d = client.post("/api/forecast-all", json={"horizon": 1}).get_json()
    assert d["status"] == "success" and [r["station"] for r in d["stations"]] == ["DHAKA"]
    assert d["skipped"] == [{"station": "RANGPUR", "code": "INSUFFICIENT_RECENT_DATA",
                             "message": "too few recent values"}]


def test_forecast_input_errors(client):
    r = client.post("/api/forecast", json={"station": "DHAKA", "horizon": 4})
    assert r.status_code == 400 and r.get_json()["code"] == "BAD_HORIZON"
    r = client.post("/api/forecast", json={"station": "ATLANTIS", "horizon": 1})
    assert r.status_code == 422 and r.get_json()["code"] == "UNKNOWN_STATION"
    r = client.post("/api/forecast", json={"horizon": 1})
    assert r.status_code == 400
    r = client.post("/api/forecast", json={"station": "DHAKA", "horizon": "abc"})
    assert r.status_code == 400


# ----------------------------------------------------------------------------- model metrics
def test_model_metrics(client):
    d = client.post("/api/model-metrics", json={"station": "Dhaka", "horizon": 15}).get_json()
    assert d["status"] == "ok" and d["selected_horizon"] == 15
    assert [m["horizon"] for m in d["metrics_by_horizon"]] == [1, 15]
    for m in d["metrics_by_horizon"]:
        assert m["rmse"] > 0 and m["n"] > 100 and m["skill_vs_climatology"] is not None
    assert len(d["residuals"]) == d["metrics_by_horizon"][1]["n"]
    assert 0 < len(d["feature_importance"]) <= 10
    assert all(f["label"] for f in d["feature_importance"])
    assert d["model_info"]["num_features"] > 10 and d["model_info"]["training_period"]
    assert client.get("/api/stations?kind=evaluated").get_json()["stations"] == ["DHAKA", "RANGPUR"]


def test_feature_labels(client):
    label = client.webapp.feature_label
    assert label("clim_target") == "Normal for the target date"
    assert label("lag_1") == "Max temp 1 day earlier" and label("lag_14") == "Max temp 14 days earlier"
    assert label("roll_mean_30") == "Mean of the last 30 days"
    assert label("anom_mean_7") == "Mean anomaly of the last 7 days"
    assert label("something_new") == "something new"


def test_model_metrics_unknown_station(client):
    r = client.post("/api/model-metrics", json={"station": "ATLANTIS"})
    assert r.status_code == 404


# ----------------------------------------------------------------------------- upload
def _csv_bytes(df: pd.DataFrame) -> io.BytesIO:
    return io.BytesIO(df.to_csv(index=False).encode("utf-8"))


def _good_df(n=40):
    dates = pd.date_range("2024-01-01", periods=n, freq="D")
    return pd.DataFrame({"station": "Dhaka", "Date": dates.strftime("%d/%m/%Y"),
                         "value": (30 + np.sin(np.arange(n) / 4)).round(1)})


def test_upload_valid_csv(client):
    r = client.post("/api/upload", data={"file": (_csv_bytes(_good_df()), "good.csv")},
                    content_type="multipart/form-data")
    d = r.get_json()
    assert d["status"] == "success", d
    assert d["rows"] == 40 and d["stations"] == ["DHAKA"] and d["date_range"] == "2024-01-01 to 2024-02-09"
    saved = client.webapp.config.UPLOAD_FOLDER / d["saved_as"]
    assert saved.exists()
    saved.unlink()


def test_upload_valid_xlsx(client):
    df = _good_df()
    df["Date"] = pd.to_datetime(df["Date"], format="%d/%m/%Y")
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    buf.seek(0)
    d = client.post("/api/upload", data={"file": (buf, "good.xlsx")}, content_type="multipart/form-data").get_json()
    assert d["status"] == "success", d
    (client.webapp.config.UPLOAD_FOLDER / d["saved_as"]).unlink()


def test_upload_rejections(client):
    bad = _good_df().drop(columns=["value"])
    d = client.post("/api/upload", data={"file": (_csv_bytes(bad), "bad.csv")},
                    content_type="multipart/form-data").get_json()
    assert d["status"] == "error" and any("value" in e for e in d["errors"])
    r = client.post("/api/upload", data={"file": (io.BytesIO(b"x"), "notes.txt")}, content_type="multipart/form-data")
    assert r.status_code == 400 and r.get_json()["code"] == "BAD_FILE_TYPE"
    r = client.post("/api/upload", data={}, content_type="multipart/form-data")
    assert r.status_code == 400
    old = client.webapp.app.config["MAX_CONTENT_LENGTH"]
    client.webapp.app.config["MAX_CONTENT_LENGTH"] = 100
    try:
        r = client.post("/api/upload", data={"file": (_csv_bytes(_good_df()), "big.csv")},
                        content_type="multipart/form-data")
        assert r.status_code == 413 and r.get_json()["code"] == "FILE_TOO_LARGE"
    finally:
        client.webapp.app.config["MAX_CONTENT_LENGTH"] = old


# ----------------------------------------------------------------------------- downloads
def test_downloads(client):
    files = client.get("/api/downloads").get_json()["files"]
    names = [f["name"] for f in files]
    assert names[0] == "final_temperature_ml_results.json" and files[0]["key"] and files[0]["description"]
    keys = [f["key"] for f in files]
    assert keys == sorted(keys, reverse=True)                 # key reports first, then the rest
    assert any(not k for k in keys)
    r = client.get("/api/download/final_temperature_ml_results.json")
    assert r.status_code == 200 and b"results_by_horizon" in r.data
    assert "attachment" in r.headers.get("Content-Disposition", "")
    assert client.get("/api/download/..%2Fconfig%2Fconfig.json").status_code in (400, 404)
    assert client.get("/api/download/../config/config.json").status_code in (400, 404)
    assert client.get("/api/download/missing.json").status_code == 404
    assert client.get("/api/download/model.joblib").status_code == 400


def test_figures(client):
    figs = client.get("/api/figures").get_json()["figures"]
    assert figs and all(f["title"] for f in figs)
    assert "09_permutation_importance.png" in [f["name"] for f in figs]
    r = client.get(figs[0]["url"])
    assert r.status_code == 200 and r.mimetype == "image/png"
    assert client.get("/figures/..%2Fconfig.json").status_code in (400, 404)


# ----------------------------------------------------------------------------- before training
def test_before_models_exist(client, project, tmp_path):
    """A project with only data results (no models yet) must degrade gracefully."""
    w = client.webapp
    partial = tmp_path / "partial"
    shutil.copytree(project / "config", partial / "config")
    shutil.copytree(project / "data", partial / "data")
    (partial / "results").mkdir()
    for name in ["01_validation_report.json", "02_station_eligibility.csv", "06_baseline_results.json"]:
        shutil.copy2(project / "results" / name, partial / "results" / name)
    saved = {k: getattr(w.config, k) for k in ["PROJECT_DIR", "RESULTS_DIR", "MODELS_DIR", "MODEL_METADATA_DIR",
                                               "PREDICTIONS_DIR", "FIGURES_DIR"]}
    try:
        w.config.PROJECT_DIR = partial
        w.config.RESULTS_DIR = partial / "results"
        w.config.MODELS_DIR = partial / "models"
        w.config.MODEL_METADATA_DIR = partial / "model_metadata"
        w.config.PREDICTIONS_DIR = partial / "predictions"
        w.config.FIGURES_DIR = partial / "results" / "figures"
        w._pipeline.clear()
        w._file_cache.clear()
        d = client.post("/api/dashboard-data", json={}).get_json()
        assert d["status"] == "ok" and d["pipeline_ready"] is False and d["messages"]
        assert all(r["status"] == "pending" and r["baseline_rmse"] > 0 for r in d["leaderboard"])
        r = client.post("/api/forecast", json={"station": "DHAKA", "horizon": 1})
        assert r.status_code == 503 and r.get_json()["code"] == "MODEL_NOT_FOUND"
        assert client.post("/api/outlook", json={"station": "DHAKA"}).status_code == 503
        assert client.post("/api/forecast-all", json={}).status_code == 503
        assert client.post("/api/model-metrics", json={"station": "DHAKA"}).status_code == 503
        assert client.get("/api/figures").get_json()["figures"] == []
    finally:
        for k, v in saved.items():
            setattr(w.config, k, v)
        w._pipeline.clear()
        w._file_cache.clear()


# ----------------------------------------------------------------------------- data updates & runs
def _new_rows_df(n=5, station="Dhaka", start="2022-01-01"):
    """Rows after the synthetic project's last day (2021-12-31), so they really are new."""
    dates = pd.date_range(start, periods=n, freq="D")
    return pd.DataFrame({"station": station, "Date": dates.strftime("%d/%m/%Y"),
                         "value": (31 + np.arange(n) / 10).round(1)})


def test_data_status(client):
    d = client.get("/api/data-status").get_json()
    assert d["status"] == "success" and d["busy"] is False
    info = d["data"]
    assert info["exists"] and info["rows"] > 1000
    assert info["stations"] == 2 and info["date_range"] == ["2016-01-01", "2021-12-31"]
    assert [m["key"] for m in d["modes"]] == ["data", "refresh", "full"]
    by_key = {m["key"]: [s["key"] for s in m["stages"]] for m in d["modes"]}
    assert by_key["data"] == ["setup", "validate", "prepare"]
    assert "screening" not in by_key["refresh"] and "tuning" not in by_key["refresh"]
    assert by_key["refresh"][-2:] == ["evaluate", "audit"]
    assert len(by_key["full"]) == 10


def test_upload_reports_what_is_new(client):
    """The upload response says how many rows are missing from the pipeline's data file."""
    d = client.post("/api/upload", data={"file": (_csv_bytes(_new_rows_df(5)), "new.csv")},
                    content_type="multipart/form-data").get_json()
    assert d["status"] == "success", d
    m = d["merge"]
    assert m["new_rows"] == 5 and m["already_present"] == 0
    assert m["new_rows_after_current_end"] == 5
    assert m["new_date_range"] == ["2022-01-01", "2022-01-05"]
    assert m["new_stations"] == [] and m["stations_touched"] == ["DHAKA"]
    (client.webapp.config.UPLOAD_FOLDER / d["saved_as"]).unlink()

    # Rows the data file already holds are recognised and add nothing.
    existing = pd.read_csv(client.webapp.get_settings().raw_data_file, dtype=str).head(20)
    d2 = client.post("/api/upload", data={"file": (_csv_bytes(existing), "old.csv")},
                     content_type="multipart/form-data").get_json()
    assert d2["status"] == "success" and d2["merge"]["new_rows"] == 0
    assert d2["merge"]["already_present"] == 20
    (client.webapp.config.UPLOAD_FOLDER / d2["saved_as"]).unlink()


def test_apply_data_rejects_bad_requests(client):
    up = client.post("/api/upload", data={"file": (_csv_bytes(_new_rows_df(3)), "n.csv")},
                     content_type="multipart/form-data").get_json()
    saved = up["saved_as"]
    try:
        r = client.post("/api/apply-data", json={"file": saved, "mode": "turbo"})
        assert r.status_code == 400 and r.get_json()["code"] == "BAD_MODE"
        for name in ["../../../../etc/passwd", "does_not_exist.csv", ""]:
            r = client.post("/api/apply-data", json={"file": name, "mode": "refresh"})
            assert r.status_code == 404 and r.get_json()["code"] == "UPLOAD_NOT_FOUND", name
        # A file whose rows are all present already is refused before anything is written.
        existing = pd.read_csv(client.webapp.get_settings().raw_data_file, dtype=str).head(10)
        old = client.post("/api/upload", data={"file": (_csv_bytes(existing), "old.csv")},
                          content_type="multipart/form-data").get_json()
        r = client.post("/api/apply-data", json={"file": old["saved_as"], "mode": "refresh"})
        assert r.status_code == 400 and r.get_json()["code"] == "NOTHING_NEW"
        assert r.get_json()["merge"]["new_rows"] == 0
        (client.webapp.config.UPLOAD_FOLDER / old["saved_as"]).unlink()
    finally:
        (client.webapp.config.UPLOAD_FOLDER / saved).unlink(missing_ok=True)


def test_merge_is_versioned_and_reversible(client, project):
    """Merging adds only the new rows, snapshots the old file and can be rolled back."""
    import data_versions as dv

    s = client.webapp.get_settings()
    raw = Path(s.raw_data_file)
    before_rows = len(dv.read_raw(s))
    before_bytes = raw.read_bytes()

    plan = dv.plan(s, _new_rows_df(4, start="2022-03-01"))
    assert plan["new_rows"] == 4
    entry = dv.commit(s, plan, "march.csv")
    try:
        assert len(dv.read_raw(s)) == before_rows + 4
        assert entry["rows_added"] == 4 and entry["restores_from"]

        # Applying the same rows again is a no-op.
        assert dv.plan(s, _new_rows_df(4, start="2022-03-01"))["new_rows"] == 0

        # The snapshot is on disk and the ids are unique.
        vs = dv.versions(s)
        assert len({v["id"] for v in vs}) == len(vs)
        snap = next(v for v in vs if v.get("file"))
        assert (dv.versions_dir(s) / snap["file"]).is_file()

        listed = client.get("/api/data-status").get_json()["data"]
        assert listed["rows"] == before_rows + 4 and len(listed["versions"]) >= 2

        # Restoring through the API brings the original file back byte for byte.
        r = client.post("/api/data-versions/restore", json={"id": snap["id"]})
        assert r.status_code == 200, r.get_json()
        assert raw.read_bytes() == before_bytes
        assert len(dv.read_raw(s)) == before_rows

        r = client.post("/api/data-versions/restore", json={"id": "v9999_nope"})
        assert r.status_code == 404 and r.get_json()["code"] == "UNKNOWN_VERSION"
    finally:
        raw.write_bytes(before_bytes)
        shutil.rmtree(dv.versions_dir(s), ignore_errors=True)


def test_pipeline_run_idle_state(client):
    d = client.get("/api/pipeline-run").get_json()
    assert d["status"] == "success" and d["busy"] is False
    assert client.post("/api/pipeline-run/cancel", json={}).get_json()["code"] == "NO_RUN"
    r = client.post("/api/pipeline-run/start", json={"mode": "nope"})
    assert r.status_code == 400 and r.get_json()["code"] == "BAD_MODE"


def test_pipeline_run_acts_on_the_project_folder(client, project):
    """A run executes train_pipeline.py against TFS_PROJECT_DIR, not the code folder."""
    import time

    import pipeline_runner as pr

    # The pipeline writes a summary of every run into <project>/logs/pipeline_runs, so a new file
    # there proves the subprocess resolved its root to the project folder and not to the code folder.
    runs = project / "logs" / "pipeline_runs"
    before = {p.name for p in runs.glob("*.json")} if runs.is_dir() else set()
    code_runs = CODE_DIR / "logs" / "pipeline_runs"
    code_before = {p.name for p in code_runs.glob("*.json")} if code_runs.is_dir() else set()

    # 'setup' only: it just ensures the project's standard folders exist.
    pr.MODES["_setup_only"] = {"label": "Setup only", "stages": ["setup"], "description": "test"}
    try:
        client.webapp.runner.start("_setup_only")
        state = None
        for _ in range(120):
            state = client.webapp.runner.state()
            if state and state["status"] != "running":
                break
            time.sleep(1)
        assert state and state["status"] == "completed", state
        stage = state["stages"][0]
        assert stage["status"] == "completed" and stage["seconds"] is not None

        # The pipeline's own run summary landed in the project folder, not in the code folder.
        added = {p.name for p in runs.glob("*.json")} - before
        assert len(added) == 1, f"expected one new run summary in {runs}, got {added}"
        summary = json.loads((runs / added.pop()).read_text(encoding="utf-8"))
        assert summary["status"] == "completed"
        assert [st["stage"] for st in summary["stages"]] == ["setup"]
        code_after = {p.name for p in code_runs.glob("*.json")} if code_runs.is_dir() else set()
        assert code_after == code_before, "the run wrote into the code folder"

        # The run is readable through the API and recorded on disk.
        d = client.get("/api/pipeline-run").get_json()
        assert d["busy"] is False and d["run"]["run_id"] == state["run_id"]
        assert (project / "logs" / "webapp_runs" / f"{state['run_id']}.json").is_file()
    finally:
        pr.MODES.pop("_setup_only", None)
        shutil.rmtree(project / "logs" / "webapp_runs", ignore_errors=True)


def test_pipeline_run_reports_failure_without_touching_results(client, project):
    import time

    import pipeline_runner as pr

    evaluation = (project / "results" / "09_final_evaluation.json").read_bytes()
    pr.MODES["_broken"] = {"label": "Broken", "stages": ["nosuchstage"], "description": "test"}
    try:
        client.webapp.runner.start("_broken")
        state = None
        for _ in range(60):
            state = client.webapp.runner.state()
            if state and state["status"] != "running":
                break
            time.sleep(1)
        assert state and state["status"] == "failed", state
        assert state["stages"][0]["status"] == "failed"
        assert state["rolled_back"] is None
        assert "Earlier stages may have updated some outputs" in state["message"]
        assert (project / "results" / "09_final_evaluation.json").read_bytes() == evaluation
    finally:
        pr.MODES.pop("_broken", None)
        shutil.rmtree(project / "logs" / "webapp_runs", ignore_errors=True)


def test_upload_flags_rows_past_the_test_period(client):
    """Observations after the test period need that period moved; the preview must say so."""
    d = client.post("/api/upload", data={"file": (_csv_bytes(_new_rows_df(5)), "after.csv")},
                    content_type="multipart/form-data").get_json()
    m = d["merge"]
    assert m["extends_past_test_end"] is True
    assert m["test_end_date"] == "2021-12-31"
    assert m["rows_past_test_end"] == 5
    assert m["suggested_test_end"] == "2022-01-05"
    (client.webapp.config.UPLOAD_FOLDER / d["saved_as"]).unlink()

    # Rows inside the existing range do not touch the test period.
    inside = pd.DataFrame({"station": "Dhaka", "Date": ["05/06/2018", "06/06/2018"], "value": [31.0, 31.2]})
    d2 = client.post("/api/upload", data={"file": (_csv_bytes(inside), "inside.csv")},
                     content_type="multipart/form-data").get_json()
    assert d2["merge"]["extends_past_test_end"] is False
    assert d2["merge"]["rows_past_test_end"] == 0 and d2["merge"]["suggested_test_end"] is None
    (client.webapp.config.UPLOAD_FOLDER / d2["saved_as"]).unlink()


def test_apply_data_past_test_period_needs_confirmation(client):
    """The guards refuse before anything is written, and demand the full run."""
    s = client.webapp.get_settings()
    raw = Path(s.raw_data_file)
    before = raw.read_bytes()
    cfg_before = Path(s.paths.config_file).read_bytes()
    up = client.post("/api/upload", data={"file": (_csv_bytes(_new_rows_df(5)), "after.csv")},
                     content_type="multipart/form-data").get_json()
    try:
        r = client.post("/api/apply-data", json={"file": up["saved_as"], "mode": "full"})
        d = r.get_json()
        assert r.status_code == 400 and d["code"] == "TEST_PERIOD_TOO_SHORT"
        assert "2021-12-31" in d["message"] and "2022-01-05" in d["message"]

        r = client.post("/api/apply-data", json={"file": up["saved_as"], "mode": "refresh",
                                                 "extend_test_period": True})
        assert r.status_code == 400 and r.get_json()["code"] == "FULL_RUN_REQUIRED"

        # Neither the data file nor the configuration was touched by the refused calls.
        assert raw.read_bytes() == before
        assert Path(s.paths.config_file).read_bytes() == cfg_before
        assert client.get("/api/pipeline-run").get_json()["busy"] is False
    finally:
        (client.webapp.config.UPLOAD_FOLDER / up["saved_as"]).unlink(missing_ok=True)


def test_set_test_end_is_recorded_and_reversible(client):
    import data_versions as dv

    s = client.webapp.get_settings()
    cfg_file = Path(s.paths.config_file)
    before = cfg_file.read_bytes()
    try:
        out = dv.set_test_end(s, "2022-01-30")
        assert out["changed"] is True and out["previous_test_end_date"] == "2021-12-31"
        written = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert written["split"]["test_end_date"] == "2022-01-30"
        assert "web app" in written["split"]["_decision"]

        # Setting the same value again is a no-op.
        assert dv.set_test_end(s, "2022-01-30")["changed"] is False

        # The change is listed, and the backup restores the original file exactly.
        assert any(v.get("kind") == "config" for v in dv.versions(s))
        assert dv.restore_config(s, out["config_backup"]) is True
        assert cfg_file.read_bytes() == before
        assert dv.restore_config(s, "config_does_not_exist.json") is False
    finally:
        cfg_file.write_bytes(before)
        shutil.rmtree(dv.versions_dir(s), ignore_errors=True)
        client.webapp._pipeline.clear()
        client.webapp._file_cache.clear()


def test_failed_run_rolls_back_the_data_and_the_test_period(client, project):
    """A run that fails must leave the data file and the configuration as they were."""
    import time

    import data_versions as dv
    import pipeline_runner as pr

    s = client.webapp.get_settings()
    raw = Path(s.raw_data_file)
    cfg_file = Path(s.paths.config_file)
    raw_before, cfg_before = raw.read_bytes(), cfg_file.read_bytes()

    plan = dv.plan(s, _new_rows_df(6))
    assert plan["new_rows"] == 6 and plan["extends_past_test_end"]
    cfg_change = dv.set_test_end(s, plan["suggested_test_end"])
    entry = dv.commit(s, plan, "rollback.csv")
    assert raw.read_bytes() != raw_before and cfg_file.read_bytes() != cfg_before

    pr.MODES["_broken"] = {"label": "Broken", "stages": ["nosuchstage"], "description": "test"}
    try:
        client.webapp.runner.start("_broken", data_version=entry,
                                   rollback_to=entry["restores_from"],
                                   restore_config=cfg_change["config_backup"])
        state = None
        for _ in range(60):
            state = client.webapp.runner.state()
            if state and state["status"] != "running":
                break
            time.sleep(1)
        assert state and state["status"] == "failed", state
        assert state["rolled_back"] == entry["restores_from"]
        assert "test period was set back" in state["message"]
        assert "uploaded rows were removed again" in state["message"]

        # Both files are back to exactly what they were.
        assert raw.read_bytes() == raw_before
        assert cfg_file.read_bytes() == cfg_before
    finally:
        pr.MODES.pop("_broken", None)
        raw.write_bytes(raw_before)
        cfg_file.write_bytes(cfg_before)
        shutil.rmtree(dv.versions_dir(s), ignore_errors=True)
        shutil.rmtree(project / "logs" / "webapp_runs", ignore_errors=True)
        client.webapp._pipeline.clear()
        client.webapp._file_cache.clear()


def test_data_only_update_is_allowed_past_the_test_period(client, project):
    """The data-only mode accepts observations beyond the test period and moves the forecast origin.

    It rebuilds the daily data and nothing else, so the deployed models and their published scores
    stay exactly as they were - that is what makes it usable when the test period cannot move.
    """
    import time

    import data_versions as dv

    s = client.webapp.get_settings()
    raw = Path(s.raw_data_file)
    raw_before = raw.read_bytes()
    cfg_before = Path(s.paths.config_file).read_bytes()
    models_before = (project / "models" / "current" / "best_models.json").read_bytes()
    evaluation_before = (project / "results" / "09_final_evaluation.json").read_bytes()
    origin_before = client.post("/api/forecast", json={"station": "DHAKA", "horizon": 1}
                                ).get_json()["origin_date"]
    assert origin_before == "2021-12-31"

    up = client.post("/api/upload", data={"file": (_csv_bytes(_new_rows_df(8)), "jan.csv")},
                     content_type="multipart/form-data").get_json()
    assert up["merge"]["extends_past_test_end"] is True
    try:
        r = client.post("/api/apply-data", json={"file": up["saved_as"], "mode": "data"})
        d = r.get_json()
        assert r.status_code == 200, d                       # accepted without moving the test period
        assert d["test_period_change"] is None
        assert d["version"]["rows_added"] == 8

        for _ in range(120):
            if not client.get("/api/pipeline-run").get_json()["busy"]:
                break
            time.sleep(1)
        run = client.get("/api/pipeline-run").get_json()["run"]
        assert run["status"] == "completed", run
        assert [st["key"] for st in run["stages"]] == ["setup", "validate", "prepare"]

        # Forecasts now start from the new last day; models and scores are untouched.
        client.webapp._pipeline.clear()
        client.webapp._file_cache.clear()
        after = client.post("/api/forecast", json={"station": "DHAKA", "horizon": 1}).get_json()
        assert after["origin_date"] == "2022-01-08", after["origin_date"]
        assert after["predicted_max_temp"] is not None
        assert (project / "models" / "current" / "best_models.json").read_bytes() == models_before
        assert (project / "results" / "09_final_evaluation.json").read_bytes() == evaluation_before
        assert Path(s.paths.config_file).read_bytes() == cfg_before
    finally:
        (client.webapp.config.UPLOAD_FOLDER / up["saved_as"]).unlink(missing_ok=True)
        raw.write_bytes(raw_before)
        shutil.rmtree(dv.versions_dir(s), ignore_errors=True)
        shutil.rmtree(project / "logs" / "webapp_runs", ignore_errors=True)
        client.webapp._pipeline.clear()
        client.webapp._file_cache.clear()
        # Put the prepared data back so later tests see the original period again.
        client.webapp.runner.start("data", note="test cleanup")
        for _ in range(120):
            if client.webapp.runner.state()["status"] != "running":
                break
            time.sleep(1)
        client.webapp._pipeline.clear()
        client.webapp._file_cache.clear()
        shutil.rmtree(project / "logs" / "webapp_runs", ignore_errors=True)
