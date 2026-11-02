"""Runs the TFS pipeline (``train_pipeline.py``) in the background for the web app.

One run at a time. Each stage is a separate ``python train_pipeline.py --only <stage>`` process,
so a stage that crashes cannot take the web server down with it and the app always knows which
stage is active. Progress is kept in memory and mirrored to ``logs/webapp_runs/<run_id>.json``,
so reloading the page (or restarting the app) still shows the last run.

Two modes:

``refresh``  the data stages, then the final evaluation and audit. Screening and tuning are
             skipped: their saved results stay valid because the pipeline's reuse fingerprint
             covers the configuration (feature contract, split, seed, model and feature
             settings) and not the data, and the test window is pinned to fixed dates. The
             deployed models are refit on the new data, but model families and hyperparameters
             remain those chosen by the last tuning run.
``full``     everything, including screening and tuning. The saved screening and tuning
             results are moved aside just before screening (never deleted) so they are recomputed.
             This is the slow path - the last full tuning run took hours.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import config

# Stage keys as registered in train_pipeline.py (Day 1-10).
DATA_STAGES = ["setup", "validate", "prepare", "split", "features", "baselines"]
HEAVY_STAGES = ["screening", "tuning"]
FINAL_STAGES = ["evaluate", "audit"]

MODES: Dict[str, Dict[str, Any]] = {
    "data": {
        "label": "Update the data only",
        "stages": ["setup", "validate", "prepare"],
        "description": ("Validates and rebuilds the daily data, and nothing else. The existing models "
                        "immediately forecast from the new last day; the accuracy figures keep coming "
                        "from the last full evaluation. Takes seconds, changes no model, and is the "
                        "only option that works when the new data runs past the test period."),
    },
    "refresh": {
        "label": "Rebuild and refit",
        "stages": DATA_STAGES + FINAL_STAGES,
        "description": ("Also rebuilds the features and baselines and refits the deployed models on the "
                        "new data, reusing the tuned model families and hyperparameters. Minutes. Only "
                        "possible while the data does not extend past the end of the test period."),
    },
    "full": {
        "label": "Full re-analysis",
        "stages": DATA_STAGES + HEAVY_STAGES + FINAL_STAGES,
        "description": ("Everything, including model screening and hyperparameter tuning from scratch, "
                        "and a new test period when the data has grown. The most faithful option and by "
                        "far the slowest - the last tuning run took hours over 48 studies."),
    },
}

STAGE_TITLES = {
    "setup": "Project setup & configuration",
    "validate": "Data validation & QC",
    "prepare": "Preparation & EDA",
    "split": "Temporal split & leakage control",
    "features": "Feature engineering",
    "baselines": "Baselines & preprocessing",
    "screening": "Model screening",
    "tuning": "Hyperparameter tuning",
    "evaluate": "Final evaluation & model selection",
    "audit": "Reporting & audit",
}

LOG_TAIL_LINES = 40          # lines of stage output kept for the progress panel
_MAX_LOG_CHARS = 100_000     # per stage, so a chatty stage cannot grow the state without bound


class PipelineRunner:
    """Background pipeline runs with progress the web app can poll."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: Optional[Dict[str, Any]] = None
        self._thread: Optional[threading.Thread] = None
        self._process: Optional[subprocess.Popen] = None
        self._cancel = threading.Event()

    # ------------------------------------------------------------------ state

    @property
    def busy(self) -> bool:
        with self._lock:
            return bool(self._state and self._state["status"] == "running")

    def state(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            if self._state is not None:
                return json.loads(json.dumps(self._state))
        return self._last_persisted()

    def _runs_dir(self) -> Path:
        d = config.PROJECT_DIR / "logs" / "webapp_runs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _last_persisted(self) -> Optional[Dict[str, Any]]:
        try:
            files = sorted(self._runs_dir().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return None
        for f in files[:1]:
            try:
                return json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
        return None

    def _persist(self) -> None:
        if not self._state:
            return
        try:
            path = self._runs_dir() / f"{self._state['run_id']}.json"
            path.write_text(json.dumps(self._state, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _set(self, **fields: Any) -> None:
        with self._lock:
            if self._state is not None:
                self._state.update(fields)
        self._persist()

    # ------------------------------------------------------------------ starting

    def start(self, mode: str, *, data_version: Optional[Dict[str, Any]] = None,
              rollback_to: Optional[str] = None, restore_config: Optional[str] = None,
              note: str = "") -> Dict[str, Any]:
        """Begin a run. Raises RuntimeError when one is already going."""
        if mode not in MODES:
            raise ValueError(f"Unknown run mode '{mode}'. Use one of: {', '.join(MODES)}")
        with self._lock:
            if self._state and self._state["status"] == "running":
                raise RuntimeError("A pipeline run is already in progress.")
            stages = MODES[mode]["stages"]
            run_id = f"WEB_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            self._state = {
                "run_id": run_id,
                "mode": mode,
                "mode_label": MODES[mode]["label"],
                "status": "running",
                "started": datetime.now().isoformat(timespec="seconds"),
                "finished": None,
                "note": note,
                "data_version": data_version,
                "rollback_to": rollback_to,
                "restore_config": restore_config,
                "rolled_back": None,
                "current_stage": None,
                "stages": [{"key": k, "title": STAGE_TITLES.get(k, k), "status": "pending",
                            "seconds": None, "log": [], "warnings": []} for k in stages],
                "message": "Starting…",
            }
            self._cancel.clear()
            self._thread = threading.Thread(target=self._run, name=f"tfs-pipeline-{run_id}", daemon=True)
        self._persist()
        self._thread.start()
        return self.state()

    def cancel(self) -> bool:
        """Ask the active run to stop; the running stage is terminated."""
        with self._lock:
            if not (self._state and self._state["status"] == "running"):
                return False
            self._cancel.set()
            proc = self._process
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        return True

    # ------------------------------------------------------------------ running

    def _python(self) -> str:
        """The pipeline's interpreter - the app already runs in it, so reuse it."""
        return sys.executable

    def _stage_index(self, key: str) -> int:
        return next(i for i, st in enumerate(self._state["stages"]) if st["key"] == key)

    def _update_stage(self, key: str, **fields: Any) -> None:
        with self._lock:
            if self._state is None:
                return
            self._state["stages"][self._stage_index(key)].update(fields)
        self._persist()

    def _archive_heavy_results(self) -> Dict[str, Any]:
        """Move saved screening and tuning results aside so a full run really recomputes them.

        Entries are moved one by one rather than as whole folders: on Windows a single file still
        held open (an Optuna ``.db``, a virus scanner) would otherwise abort the entire archive.
        A file that cannot be moved is copied instead, and if even that fails it is left in place
        and reported - the run still goes ahead.
        """
        exp = config.PROJECT_DIR / "experiments"
        if not exp.is_dir():
            return {"archived_to": None, "moved": 0, "kept": []}
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = exp / f"archive_{stamp}"
        moved, kept = 0, []
        for name in ("screening", "tuning"):
            src = exp / name
            if not (src.is_dir() and any(src.iterdir())):
                continue
            target = dest / name
            target.mkdir(parents=True, exist_ok=True)
            for item in list(src.iterdir()):
                try:
                    shutil.move(str(item), str(target / item.name))
                    moved += 1
                    continue
                except (OSError, shutil.Error):
                    pass
                try:    # locked: copy it aside, then try to remove the original
                    if item.is_dir():
                        shutil.copytree(item, target / item.name, dirs_exist_ok=True)
                        shutil.rmtree(item, ignore_errors=True)
                    else:
                        shutil.copy2(item, target / item.name)
                        item.unlink()
                    moved += 1
                except OSError:
                    kept.append(f"{name}/{item.name}")
        if kept:
            logger_msg = ", ".join(kept[:5]) + (f" and {len(kept) - 5} more" if len(kept) > 5 else "")
            self._set(archive_warning=(f"{len(kept)} saved result file(s) could not be moved aside "
                                       f"({logger_msg}); those fits may be reused."))
        return {"archived_to": f"archive_{stamp}" if moved else None, "moved": moved, "kept": kept}

    def _run(self) -> None:
        state = self.state() or {}
        mode = state.get("mode", "refresh")
        try:
            for stage in list(self._state["stages"]):
                key = stage["key"]
                if self._cancel.is_set():
                    self._update_stage(key, status="skipped")
                    continue
                # Preserve reusable fits when validation/preparation fails before
                # screening even starts (for example, an output locked in Excel).
                if mode == "full" and key == "screening":
                    info = self._archive_heavy_results()
                    self._set(archived_to=info["archived_to"])
                self._set(current_stage=key, message=f"{STAGE_TITLES.get(key, key)}…")
                self._update_stage(key, status="running", started=datetime.now().isoformat(timespec="seconds"))
                began = datetime.now()
                code, tail = self._run_stage(key)
                seconds = round((datetime.now() - began).total_seconds(), 1)

                if self._cancel.is_set():
                    self._update_stage(key, status="cancelled", seconds=seconds, log=tail)
                    self._fail("The run was cancelled.", status="cancelled")
                    return
                if code != 0:
                    self._update_stage(key, status="failed", seconds=seconds, log=tail)
                    self._fail(self._failure_message(key, tail))
                    return
                self._update_stage(key, status="completed", seconds=seconds, log=tail)

            message = "The pipeline finished. The website now shows the new results."
            warnings = [w for st in self._state["stages"] for w in st.get("warnings", [])]
            if warnings:
                message += " Warnings: " + " ".join(dict.fromkeys(warnings))
            self._finish("completed", message)
        except Exception as exc:                                    # noqa: BLE001 - report, never crash the server
            # Anything unexpected (a locked file while archiving, a disk error) must still undo the
            # data and configuration changes this run made, exactly like a failed stage does.
            self._fail(f"The run stopped unexpectedly: {type(exc).__name__}: {exc}")

    def _run_stage(self, key: str) -> tuple:
        """Run one stage as a subprocess, keeping the tail of its output.

        ``train_pipeline.py`` lives in the code folder but must act on the project folder, which
        can be a different one (``TFS_PROJECT_DIR``). The pipeline resolves its root from
        ``TFS_ROOT``, so that is set explicitly rather than relying on the working directory.
        """
        script = config.CODE_DIR / "train_pipeline.py"
        if not script.is_file():
            return 1, [f"The pipeline script was not found: {script}"]
        cmd = [self._python(), str(script), "--only", key]
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8",
               "TFS_ROOT": str(config.PROJECT_DIR)}
        lines: List[str] = []
        warnings: List[str] = []
        size = 0
        try:
            proc = subprocess.Popen(cmd, cwd=str(config.CODE_DIR), env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                    errors="replace", bufsize=1)
        except OSError as exc:
            return 1, [f"Could not start the pipeline: {exc}"]

        with self._lock:
            self._process = proc
        try:
            for line in proc.stdout:                                # type: ignore[union-attr]
                line = line.rstrip()
                if not line:
                    continue
                warning = re.search(r"\|\s*WARNING\s*\|[^|]*\|\s*(.*)", line)
                if warning and warning.group(1) not in warnings:
                    warnings.append(warning.group(1))
                    self._update_stage(key, warnings=warnings[:20])
                size += len(line)
                if size <= _MAX_LOG_CHARS:
                    lines.append(line)
                    if len(lines) % 5 == 0:
                        self._update_stage(key, log=lines[-LOG_TAIL_LINES:])
            proc.wait()
        finally:
            with self._lock:
                self._process = None
        return proc.returncode, lines[-LOG_TAIL_LINES:]

    @staticmethod
    def _failure_message(key: str, tail: List[str]) -> str:
        message = f"Stage '{STAGE_TITLES.get(key, key)}' failed."
        for line in reversed(tail):
            detail = re.search(r"failed \[[A-Z_]+\]: (.*)", line)
            if detail:
                message += " " + detail.group(1)
                break
            if "PermissionError:" in line:
                message += (" An output file could not be written. Close any pipeline files open "
                            "in Excel or another program and check output-folder permissions.")
                break
        return message + (" Earlier stages may have updated some outputs. After fixing the error, "
                          "run the pipeline again to rebuild consistent results.")

    def _fail(self, message: str, status: str = "failed") -> None:
        """End a run that did not succeed, undoing whatever it changed before it started."""
        state = self._state or {}
        rollback, cfg_backup = state.get("rollback_to"), state.get("restore_config")
        if rollback or cfg_backup:
            try:
                import data_versions
                from src.config import load_settings
                from src.paths import ProjectPaths
                s = load_settings(ProjectPaths(root=config.PROJECT_DIR))
                if cfg_backup and data_versions.restore_config(s, cfg_backup):
                    message += " The test period was set back to what it was."
                if rollback:
                    data_versions.restore(s, rollback)
                    self._set(rolled_back=rollback)
                    message += (f" The uploaded rows were removed again: the data file was restored to "
                                f"version {rollback}.")
            except Exception as exc:                                # noqa: BLE001
                message += (f" The data file could NOT be rolled back automatically "
                            f"({type(exc).__name__}: {exc}) - restore version {rollback} by hand "
                            f"on the upload page.")
        self._finish(status, message)

    def _finish(self, status: str, message: str) -> None:
        self._set(status=status, message=message, current_stage=None,
                  finished=datetime.now().isoformat(timespec="seconds"))


runner = PipelineRunner()


def mode_options() -> List[Dict[str, Any]]:
    """The run modes, for the upload page."""
    return [{"key": k, "label": v["label"], "description": v["description"],
             "stages": [{"key": s, "title": STAGE_TITLES.get(s, s)} for s in v["stages"]]}
            for k, v in MODES.items()]
