# TFS Web App – User Manual

How to start, use and stop the **Temperature Forecasting System (TFS)** website on a Windows PC.
The website shows daily **maximum temperature** forecasts for Bangladesh (BMD) stations, how well the
models perform, and checks new data files. It runs on your own computer; nothing is sent to the internet.

---

## 1. Quick start (the PC where the project already works)

1. Open the folder `Final Work File\app`.
2. **Double-click `run_app.bat`.**
   A black window opens and, after a few seconds, your browser opens **http://127.0.0.1:5000**.
3. Use the website (section 3).
4. To stop: **close the black window** (or click in it and press `Ctrl + C`).

> Keep the black window open while you use the website – it *is* the website.
> If the browser did not open by itself, open it and type `http://127.0.0.1:5000`.

**Alternative (PowerShell / VS Code terminal):**

```powershell
cd "C:\Users\USER\Desktop\Shihab\Niaz Bhai\Weather_ML\Final Work File\app"
powershell -ExecutionPolicy Bypass -File .\run_app.ps1
```

Then open http://127.0.0.1:5000 yourself.

---

## 2. What must be in place

The app has no database. It reads everything from the pipeline folder next to it:

```
Final Work File\
├── Work with claude\    the TFS pipeline: Python environment (.venv), data, results, trained models
└── app\                 this website (run_app.bat, app.py, ...)
```

| Needed | Where | Made by |
|---|---|---|
| Python environment with all libraries | `Work with claude\.venv` | `setup_env.ps1` (section 6) |
| Data and quality results | `Work with claude\data`, `results\01_*`, `02_*` | pipeline Days 2–3 |
| Model results and trained models | `results\09_*`, `models\current\`, `model_metadata\` | pipeline Days 7–9 |
| Audit report | `results\audit_report.json` | pipeline Day 10 |

On this PC all of these already exist (full run finished on 28 Sep 2026). If the models are missing, the
website still opens but says *"Final models are not trained yet"* and the forecast buttons are disabled.

---

## 3. Using the website

The menu at the top has four pages. On a phone-sized screen the menu is behind the ☰ button.

### 3.1 Dashboard (`/`)

An overview of the whole system:

- **Key figures** – number of stations, average test error (RMSE, °C), data period, deployed models
  and the audit result (PASS, no leakage).
- **Model leaderboard** – for each horizon (1, 2, 3, 5, 7, 10, 13, 15 days): the best simple baseline,
  the machine-learning model, both errors on the test period (July 2022 – June 2025) and the improvement.
  Lower RMSE is better.
- **Charts** – error by horizon (baseline vs. model) and data coverage per station
  (original BMD observations / filled values / missing).
- **Data quality** – missing days, values not in the original BMD record, validation status.
- **Data analysis figures** – the pipeline's charts (seasonal cycle, warming trend, data availability, …).
  Click a figure to open it full size.
- **Download results** – the 10 main reports (with a description of each). Click
  *"Show all … result files"* for everything else.

### 3.2 Make Forecast (`/forecast`)

1. **Select a station** from the list.
2. Either
   - move the **Forecast Horizon** slider (1–15 days; models exist for 1, 2, 3, 5, 7, 10, 13 and 15 days)
     and click **Generate Forecast**, or
   - click **Show all horizons (15-day outlook)** to get every horizon at once.
3. Read the result:
   - **Predicted maximum temperature** (°C) with an **80 % range** – the true value falls inside this
     range about 8 times out of 10.
   - **Normal for this date** – the long-term average maximum for that day of the year.
   - **Model used** and its test error compared with the best baseline.
   - **Warnings** (yellow) – read them; see section 4.
   - A chart of the last weeks of data with the forecast.
4. **Download as JSON / CSV** saves the forecast (or the whole outlook) to your Downloads folder.

**All stations at once:** choose **All stations** (second entry in the station list), then
- **Generate Forecast** – every station at the horizon on the slider, or
- **Show all horizons (15-day outlook)** – every station at every horizon (one column per horizon;
  hover a value for its date and 80 % range).

This takes a few seconds (one forecast per station). Warnings that apply to every station are shown once
above the table; station-specific ones (low confidence, all-station normal) are in the *Notes* column.
**Download all stations as CSV** saves one row per station and horizon with the forecast, 80 % range,
normal, model, test RMSE, baseline, confidence and notes – e.g. `forecast_all_stations_outlook_from_2026-05-31.csv`.

**Direct links** (bookmark or share on the same PC):
`http://127.0.0.1:5000/forecast?station=DHAKA&horizon=3`,
`http://127.0.0.1:5000/forecast?station=DHAKA&outlook=1`, or for every station
`http://127.0.0.1:5000/forecast?station=ALL&horizon=3` / `http://127.0.0.1:5000/forecast?station=ALL&outlook=1`.

### 3.3 Model Performance (`/model-performance`)

1. **Select a station** (only stations with test-period results are listed).
2. Optionally choose a **horizon** for the feature-importance and error charts.

You see, per horizon: RMSE, MAE, R², bias (positive = forecasts too warm), **skill vs. climatology**
(positive = better than just using the seasonal normal) and the number of test days; a chart of error
by horizon (this station vs. all stations vs. climatology); the **inputs that matter most** to the model;
the distribution of forecast errors; and model details (type, training period, number of inputs).

### 3.4 Upload Data (`/data-upload`)

This page does two things, in two steps. **Uploading only checks the file.** Adding it to the system
is a second step that you confirm.

#### Step 1 – check the file

1. Drag a **.csv** or **.xlsx** file onto the box (or click the box to choose a file). Maximum 20 MB.
2. For a CSV file, check the preview (first 10 rows; Excel files are checked on the server without a
   preview) and click **Validate Data**.
3. Result: **green** = valid (warnings, if any, are listed); **red** = errors that must be fixed.

The file needs three columns (capital letters do not matter):

| Column | Meaning | Example |
|---|---|---|
| `station` | station name | `DHAKA` |
| `Date` | date, `DD/MM/YYYY` (also `YYYY-MM-DD` or `DD-MM-YYYY`) | `31/05/2026` |
| `value` | daily maximum temperature in °C | `34.2` |

Valid files are kept in `app\static\uploads\` (file name starts with the date and time).

#### Step 2 – add it to the system and re-run

Under the green result, **Add this data to the pipeline** appears. It first tells you how much of your
file is actually new:

- **New rows to be added** – rows whose station and date are not in the system yet. Only these are
  added; nothing is ever overwritten. If you upload the same file twice, the second time adds nothing.
- **Already in the data file** – rows that are there already.
- **Stations affected**, and a warning if the file brings stations with no history yet.

Then choose **how much should be re-computed**:

| Choice | Time | What changes |
|---|---|---|
| **Update the data only** (default) | seconds | The forecasts start from the new last day straight away. The models themselves are not retrained, and the accuracy figures on the other pages stay as they are. |
| **Rebuild and refit** | minutes | Also rebuilds the features and retrains the deployed models on the new data, keeping the model types and settings found by the last tuning run. |
| **Full re-analysis** | **hours** | Also redoes model screening and hyperparameter tuning from scratch. |

Click **Add rows & run pipeline** and confirm. A **Pipeline run** panel shows each stage as it
finishes, with the pipeline's own output. You can leave the page and come back – the progress is
still there. When it finishes, the dashboard, forecasts and model pages use the new data.

> **In practice you will almost always use "Update the data only."** With real new BMD readings the
> other two choices are refused, and the page explains why: the system scores its models on a fixed
> test period that ends 2025-06-30, and it can only rebuild the features while that period ends on the
> last reading. Updating the data alone does not touch the features, so it just works. If you do want
> the models rebuilt on a longer record, tick the box that moves the test period forward – but then the
> accuracy numbers are measured over a different period and the full re-analysis is required.

#### Safety and undo

Every change to the data file is saved as a **version** first. Under **Pipeline data file** you see
the current file, the test period, and the version history with a **Restore** button on each earlier
version. If a run fails, the system puts the previous data file (and test period) back on its own and
the website keeps showing the last good results.

---

## 4. Understanding the warnings

| Warning | Meaning |
|---|---|
| *The latest data is from 2026-05-31 (… days ago), so these forecasts are for dates that have already passed.* | The newest data in the pipeline ends on 31 May 2026, so "tomorrow" is 1 June 2026. To forecast from today, newer data is needed (section 5). |
| *Recent inputs are mostly filled values (not original BMD observations).* | The last weeks of data at this station are gap-filled values, not original BMD readings; the forecast is less certain. |
| *Low confidence: station has too few original observations…* | Stations with very little original data (e.g. DIMLA, JHENAIDAH, MOULVI BAZAR, PIROJPUR, BRAHMANBARIA). Their accuracy could not be checked. |
| *Skill at 10–15 days is close to climatology…* | Beyond about a week, forecasts are only slightly better than the seasonal normal. |
| *No forecast for GAZIPUR / HABIGANJ / SYDPUR / SAINT MARTIN …* | These stations stopped reporting too long ago or have too little data; the reason is shown. |

---

## 5. Keeping it up to date

- **After re-running the pipeline** (new models, new results): just **refresh the browser page**. The app
  notices changed result and model files by itself; no restart needed.
- **Forecasting from today** needs station data up to today. The easiest way is now the website
  itself: go to **Upload Data**, upload the new readings as a CSV, and choose **Update the data only**
  (section 3.4). It takes a few seconds, and the forecast page then issues forecasts from the new last
  day. The trained models stay as they are.

- The same thing from a terminal, if you prefer, in the `Work with claude` folder:

  ```powershell
  .venv\Scripts\python.exe train_pipeline.py --only validate
  .venv\Scripts\python.exe train_pipeline.py --only prepare
  ```

  after adding the new rows to `data\raw\temperature.csv` yourself. Note that this file must hold the
  **whole** history (1999 to today), not only the new days – the website's upload does that merging for
  you and keeps a version of the previous file.

---

## 6. First-time setup on another PC

1. Copy the **whole** `Final Work File` folder (both `Work with claude` and `app`) – the data, results and
   trained models are not in git, so a git clone is not enough.
2. Install **Python 3.12** (python.org) or **uv** (https://docs.astral.sh/uv/).
3. Open PowerShell in `Final Work File\Work with claude` and create the Python environment
   (the copied `.venv` does not work on another PC, `-Recreate` rebuilds it):

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\setup_env.ps1 -Recreate -SkipGit
   .venv\Scripts\python.exe -m pip install -r ..\app\requirements.txt
   ```

4. Start the app with `app\run_app.bat` (section 1).

---

## 7. Troubleshooting

| Problem | What to do |
|---|---|
| Browser says *"This site can't be reached"* | The black window is closed or still starting. Start `run_app.bat` again and wait ~5 seconds, then refresh. |
| Black window says *"Port 5000 is already in use"* and stops | The app is already running in another window – the browser tab that opened shows that copy, so just use it (or close the other window and start again). To run on another port, open `run_app.bat` in Notepad and change `5000` in the line `set "TFS_PORT=5000"`. |
| *"The pipeline's Python environment was not found"* | `Work with claude\.venv` is missing – do section 6, step 3. |
| Double-clicking `run_app.ps1` opens Notepad | Normal on Windows – use `run_app.bat` instead. |
| Forecast buttons are grey / *"No trained models yet"* | The models are missing: run the pipeline up to Day 9 (`python train_pipeline.py --from tuning` in `Work with claude`, about 8 hours). |
| A station is missing from the forecast list | It cannot be forecast (section 4, last row). |
| A page shows old numbers | Refresh the page (`F5`). |
| Anything else | The black window shows the error messages; the pipeline log is `Work with claude\logs\tfs.log`. |

---

## 8. Settings (optional)

Settings can be changed with environment variables or a file named `.env` in the `app` folder
(one `NAME=value` per line):

| Setting | Default | Meaning |
|---|---|---|
| `TFS_PORT` | `5000` | port of the website (with `run_app.bat`, change it inside the .bat file) |
| `TFS_HOST` | `127.0.0.1` | `127.0.0.1` = only this PC can open the site. `0.0.0.0` lets other PCs on the same network open it via this PC's IP address – only do this on a trusted network. |
| `TFS_PROJECT_DIR` | `..\Work with claude` | where the pipeline folder is |
| `TFS_MAX_UPLOAD_MB` | `20` | largest file the Upload page accepts |

Technical details (API, file sources, tests): see `README.md` in this folder.
