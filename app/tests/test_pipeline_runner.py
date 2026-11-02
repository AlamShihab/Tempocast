"""Focused runner regressions; no model training required."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pipeline_runner as pr


def make_runner(tmp_path, monkeypatch, mode="data"):
    monkeypatch.setattr(pr.config, "PROJECT_DIR", tmp_path)
    run = pr.PipelineRunner()
    run._state = {"run_id": "test", "status": "running", "mode": mode,
                  "stages": [{"key": k, "status": "pending", "log": [], "warnings": []}
                             for k in pr.MODES[mode]["stages"]]}
    return run


def test_error_message_explains_lock_and_partial_outputs():
    message = pr.PipelineRunner._failure_message("prepare", [
        "INFO | prepare finished",
        "ERROR | Stage 'prepare' failed [OUTPUT_WRITE_BLOCKED]: Cannot update 'required.csv'. Close the file.",
        "INFO | Pipeline run failed",
    ])
    assert "required.csv" in message and "Close the file" in message
    assert "Earlier stages may have updated some outputs" in message
    assert "untouched" not in message


def test_failure_before_screening_keeps_saved_fits(tmp_path, monkeypatch):
    run = make_runner(tmp_path, monkeypatch, mode="full")
    archived = []
    monkeypatch.setattr(run, "_archive_heavy_results", lambda: archived.append(True))
    monkeypatch.setattr(run, "_run_stage", lambda key: (1 if key == "prepare" else 0, []))
    run._run()
    assert run.state()["status"] == "failed"
    assert not archived


def test_full_run_archives_immediately_before_screening(tmp_path, monkeypatch):
    run = make_runner(tmp_path, monkeypatch, mode="full")
    events = []

    def archive():
        events.append("archive")
        return {"archived_to": "archive_test"}

    monkeypatch.setattr(run, "_archive_heavy_results", archive)
    monkeypatch.setattr(run, "_run_stage", lambda key: (events.append(key) or 0, []))
    run._run()
    assert events == pr.DATA_STAGES + ["archive"] + pr.HEAVY_STAGES + pr.FINAL_STAGES
    assert run.state()["status"] == "completed"


def test_warning_remains_visible_after_later_stages(tmp_path, monkeypatch):
    run = make_runner(tmp_path, monkeypatch)
    warning = "Locked preview saved as preview_current.csv."

    class Process:
        returncode = 0

        def __init__(self):
            self.stdout = iter([f"2026-10-07 | WARNING | tfs.preparation | {warning}",
                                *[f"INFO line {i}" for i in range(60)]])

        def wait(self):
            return 0

    monkeypatch.setattr(pr.subprocess, "Popen", lambda *a, **kw: Process())
    run._run()
    state = run.state()
    assert state["status"] == "completed"
    assert warning in state["message"]
    assert state["stages"][0]["warnings"] == [warning]
    assert all(warning not in line for line in state["stages"][-1]["log"])
