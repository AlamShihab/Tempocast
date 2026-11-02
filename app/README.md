# TFS Web Application (Flask)

Local website for the **Temperature Forecasting System**: daily **maximum** temperature
forecasts for BMD stations at 1, 2, 3, 5, 7, 10, 13 and 15 days, with model performance,
data validation and result downloads.

The app has no database. It reads the pipeline's output files from the project folder
(`..\Work with claude`) and reuses the pipeline's own code, so forecasts are computed
with exactly the same features, models and validation rules as in training.

```
Final Work File\
├── Work with claude\      TFS pipeline (code, config, data, results, models)
└── app\                   this web app
```

## Setup

The app runs in the pipeline's virtual environment (it imports `src\` and loads the trained models).

```powershell
cd "C:\Users\USER\Desktop\Shihab\Niaz Bhai\Weather_ML\Final Work File\Work with claude"
uv pip install --python .venv\Scripts\python.exe -r ..\app\requirements.txt
# or: .venv\Scripts\python.exe -m pip install -r ..\app\requirements.txt
```

## Run

**Double-click `run_app.bat`** – it starts the app and opens **http://127.0.0.1:5000** in the browser;
close its window (or `Ctrl+C`) to stop. Step-by-step instructions for users: **`USER_MANUAL.md`**.

From a terminal:

```powershell
cd "C:\Users\USER\Desktop\Shihab\Niaz Bhai\Weather_ML\Final Work File\app"
powershell -ExecutionPolicy Bypass -File .\run_app.ps1
# or: ..\"Work with claude"\.venv\Scripts\python.exe app.py
```

Only one copy can run per port: a second start stops with "Port 5000 is already in use" (Windows would
otherwise let two servers share the port).

The pages work at every stage: before the models exist the dashboard shows data quality and
baseline results and explains what is missing; forecasts become available after the final
evaluation (Day 9, `models\current\best_models.json`). Output files are re-read automatically
when the pipeline rewrites them.

### Configuration (`config.py`, environment variables or a `.env` file)

| Variable | Default | Meaning |
|---|---|---|
| `TFS_PROJECT_DIR` | `..\Work with claude` | folder with `results\`, `models\`, `data\`, `config\` |
| `TFS_CODE_DIR` | project folder (if it contains `src\__init__.py`) | folder with the pipeline package `src` |
| `TFS_PORT` / `TFS_HOST` | `5000` / `127.0.0.1` | where the server listens |
| `TFS_MAX_UPLOAD_MB` | `20` | upload size limit |
| `TFS_DEBUG` | `0` | `1` = Flask debug mode (development only) |

Column names, date format, horizons and plausible value ranges are **not** repeated here: they come
from the pipeline's `config\config.json` (single source of truth).

## Pages

| Page | URL | What it shows |
|---|---|---|
| Dashboard | `/` | stations, average test RMSE, data period, deployed models and audit status; leaderboard per horizon (best baseline vs ML model, improvement, what is deployed); RMSE-by-horizon chart; data coverage per station (observed / filled / missing); data-quality figures; gallery of the pipeline's figures (`results\figures`); result files (key reports first, "show all" for the rest) |
| Make Forecast | `/forecast` | choose station + horizon → predicted max temperature, 80 % range, normal for the date, model, test RMSE vs baseline, warnings, chart of the last 60 days + forecast, JSON/CSV download. **15-day outlook**: every horizon at once (table + chart with the 80 % band, JSON/CSV). **All stations**: every forecastable station at one horizon or at every horizon (table + CSV, one row per station and horizon). A warning appears when the latest data is more than 7 days old (forecasts are then for past dates). Deep links: `/forecast?station=DHAKA&horizon=3`, `/forecast?station=DHAKA&outlook=1`, `/forecast?station=ALL&horizon=3`, `/forecast?station=ALL&outlook=1` |
| Model Performance | `/model-performance` | per station: RMSE, MAE, R², bias, skill vs climatology and test days per horizon; RMSE chart (station vs all stations vs climatology); feature importance with plain-language feature names; residual histogram; model information |
| Upload Data | `/data-upload` | drag & drop CSV/XLSX → preview → validation with the pipeline's Day 2 rules (errors block, warnings are listed). Then, as a separate confirmed step, **add the new rows to the pipeline's data and re-run it**: the preview says how many rows are actually new, the run mode is chosen, progress is shown stage by stage. Also shows the current data file, its test period and the version history (with restore) |

## API

All responses are JSON. Errors: `{"status": "error", "code": "...", "message": "..."}` with a matching HTTP status.

| Method | Route | Body / params | Returns |
|---|---|---|---|
| GET/POST | `/api/dashboard-data` | – | KPIs, leaderboard, coverage, data quality, audit, messages |
| GET | `/api/stations?kind=forecast\|evaluated\|all` | – | station list |
| POST | `/api/forecast` | `{"station": "DHAKA", "horizon": 3}` | `predicted_max_temp`, `confidence_range` (80 %), `forecast_date`, `origin_date`, `data_age_days`, model + baseline RMSE, warnings, 60-day history |
| POST | `/api/outlook` | `{"station": "DHAKA"}` | `forecasts`: one row per horizon (same fields as `/api/forecast`), plus origin, data age, warnings and history once |
| POST | `/api/forecast-all` | `{"horizon": 3}`, or `{}` for every horizon | `stations`: one entry per forecastable station (origin, data age, confidence, warnings, `input_quality`, `forecasts` as in `/api/outlook`, no history); `skipped`: stations that could not be forecast, with code and message. Takes a few seconds (~7 s for 55 stations) |
| POST | `/api/model-metrics` | `{"station": "DHAKA", "horizon": 1}` | metrics by horizon, feature importance (for `horizon`, with a plain-language `label`), residuals, model info |
| POST | `/api/upload` | multipart field `file` (.csv/.xlsx) | validation result, rows, stations, date range, empty cells per column, warnings, and `merge`: how many rows are missing from the pipeline's data and whether they run past the test period |
| GET | `/api/data-status` | – | the pipeline's data file (rows, stations, period, test period), its version history and the available run modes |
| POST | `/api/apply-data` | `{"file": "<saved_as>", "mode": "data"\|"refresh"\|"full", "extend_test_period": false}` | merges the new rows (version snapshot first) and starts the run |
| POST | `/api/pipeline-run/start` | `{"mode": "data"}` | re-runs on the data already in place |
| GET | `/api/pipeline-run` | – | progress of the current or last run (per-stage status, seconds, output tail) |
| POST | `/api/pipeline-run/cancel` | – | terminates the running stage |
| POST | `/api/data-versions/restore` | `{"id": "v0003_…", "rerun": false, "mode": "data"}` | puts an earlier data file back |
| GET | `/api/downloads` | – | result files that can be downloaded (`key` + `description` for the main reports, which come first) |
| GET | `/api/download/<filename>` | – | the file (only `.json/.csv/.md` directly in `results\`) |
| GET | `/api/figures` | – | the pipeline's PNG figures with titles; each is served at `/figures/<name>` |
| GET | `/api/status` | – | configuration check and which pipeline files exist |

Error codes include `BAD_HORIZON`, `UNKNOWN_STATION`, `STATION_NOT_FORECASTABLE`,
`INSUFFICIENT_RECENT_DATA`, `MODEL_NOT_FOUND` (503 before Day 9), `BAD_FILE_TYPE`, `FILE_TOO_LARGE`,
`NOTHING_NEW`, `TEST_PERIOD_TOO_SHORT`, `FULL_RUN_REQUIRED`, `RUN_IN_PROGRESS`, `UPLOAD_NOT_FOUND`.

## Updating the data from the website

`data_versions.py` merges uploads into `data\raw\temperature.csv`; `pipeline_runner.py` runs
`train_pipeline.py` stage by stage in the background (one run at a time) and reports progress.

**Only rows that are not already there are added.** Rows are matched on station + date, so
re-uploading an overlapping file adds nothing. The previous file is copied to
`data\raw\versions\` before every change and listed in `versions\manifest.json`, so any update can
be rolled back from the page — and a run that fails rolls the data (and the test period) back by itself.

### Run modes

| Mode | Stages | What it does |
|---|---|---|
| `data` | setup → validate → prepare | Rebuilds the daily data only. The existing models forecast from the new last day at once; the published accuracy still comes from the last full evaluation. Seconds. |
| `refresh` | the data stages + evaluate → audit | Also rebuilds features and baselines and refits the deployed models, reusing the tuned families and hyperparameters. Minutes. |
| `full` | all ten | Also redoes screening and tuning from scratch (saved results are moved to `experiments\archive_<stamp>\`, never deleted). Hours. |

### Why new data usually means `data` mode

The feature stage requires that **no target falls on or after the end of the test period**
(`no_filled_targets` in `src\feature_engineering.py`): the test window has to end on the last
original observation. The project's test period ends 2025-06-30 while the data already runs to
2026-05-31, because everything after that date is gap-filled rather than observed.

So any genuinely observed new data lies past the test period, and then:

- **`data`** works unchanged — features are not rebuilt, so the rule is not involved. The models
  forecast from the new days immediately. This is the default.
- **`refresh`** is refused (`TEST_PERIOD_TOO_SHORT`) because rebuilding the features would fail.
- **`full`** is possible with `extend_test_period: true`. The app then moves
  `split.test_end_date` to the new last day (keeping a copy of `config.json` in
  `data\raw\versions\`) and runs everything. This changes the period the models are scored on, and
  because the split changes, the saved screening and tuning results no longer apply — hence the
  full run.

## Where the numbers come from

| Shown | Pipeline file |
|---|---|
| Leaderboard, test period, deployed model | `results\09_final_evaluation.json` (before Day 9: `06_baseline_results.json`) |
| Station metrics | `results\09_station_metrics.csv`; climatology skill from `06_baseline_station_metrics.csv` |
| Residuals | `predictions\09_test_predictions.parquet` |
| Feature importance | `results\09_importance_h{h}.csv` (permutation importance, test sample) |
| Model information | `model_metadata\best_{h}d.json` |
| Coverage, forecastable stations | `results\02_station_eligibility.csv` |
| Data quality | `results\01_validation_report.json` |
| Audit status | `results\audit_report.json` |
| Forecasts, outlook | `src.forecasting.ForecastEngine` + `models\current\best_{h}d.joblib` |
| Figures | `results\figures\*.png` |

## Differences from the original build prompt (and why)

- **Maximum temperature only.** The data contains daily maximum temperature per station; there is
  no TMin, rainfall or pressure. `predicted_min_temp` is returned as `null`, and upload validation uses
  the real columns (`station`, `Date`, `value`) instead of `TMax/TMin/Rainfall/Pressure`.
- **Horizons are the eight trained ones** (1, 2, 3, 5, 7, 10, 13, 15). The slider steps through
  these instead of offering 1–15, which would ask for models that do not exist.
- **No example or random data.** The coverage chart uses the real per-station shares.
- **Works offline.** Plotly is served from `static\vendor\` (copied from the Python package) and the
  JavaScript uses the browser's `fetch` instead of jQuery from a CDN.
- **Current library versions.** Flask 3 / pandas 2 / numpy 2 (the pinned versions in the prompt do
  not install on Python 3.12 and would break the pipeline code).
- **Honest model comparison.** The ML model per horizon is chosen by cross-validation; the
  leaderboard shows when a baseline is deployed because no model beat it.
- **Extras:** 80 % range, "normal for date", 15-day outlook for all horizons, stale-data warning,
  data-quality warnings, per-station skill vs climatology, plain-language feature names, pipeline figure
  gallery, deep links, dark mode, mobile menu, result-file downloads, structured error codes.

## Tests

```powershell
cd app
..\"Work with claude"\.venv\Scripts\python.exe -m pytest
```

`tests\test_app.py` builds a small synthetic project with the real pipeline (2 stations, horizons
1 and 15) and checks every page and API route, including errors, uploads (CSV + Excel), size limit,
path-traversal protection and the "models not trained yet" state.

## Security notes

Local use only (binds to 127.0.0.1). Uploads: extension and size checked, file names sanitised,
content validated before saving. Downloads: only whitelisted file types from the results folder,
no path traversal. `TFS_DEBUG=1` must not be used on a shared network.
