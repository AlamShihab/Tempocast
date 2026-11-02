"""Versioned updates of the pipeline's raw data file (``data/raw/<raw_filename>``).

An upload never replaces the history. Only rows whose (station, date) pair is not already
present are appended, so re-uploading a file that overlaps the existing record adds nothing.
Before every update the current file is copied to ``data/raw/versions/`` and the change is
recorded in ``versions/manifest.json``, so an update that turns out to be wrong (or one whose
pipeline run fails validation) can be rolled back to any earlier version.

The column names, the date format and the station-case rule all come from the pipeline
configuration, and dates are parsed with the pipeline's own ``parse_dates`` helper, so a file
the upload page accepted is merged exactly as the pipeline would read it.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

VERSIONS_DIRNAME = "versions"
MANIFEST_NAME = "manifest.json"


# ----------------------------------------------------------------------------- paths

def raw_file(s) -> Path:
    return Path(s.raw_data_file)


def versions_dir(s) -> Path:
    d = raw_file(s).parent / VERSIONS_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def manifest_path(s) -> Path:
    return versions_dir(s) / MANIFEST_NAME


# ----------------------------------------------------------------------------- manifest

def load_manifest(s) -> Dict[str, Any]:
    p = manifest_path(s)
    if not p.is_file():
        return {"versions": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        data.setdefault("versions", [])
        return data
    except (OSError, ValueError):
        return {"versions": []}


def save_manifest(s, data: Dict[str, Any]) -> None:
    manifest_path(s).write_text(json.dumps(data, indent=2), encoding="utf-8")


def versions(s) -> List[Dict[str, Any]]:
    """Recorded versions, newest first."""
    return sorted(load_manifest(s).get("versions", []), key=lambda v: v.get("created", ""), reverse=True)


# ----------------------------------------------------------------------------- reading

def _columns(s, df: pd.DataFrame) -> Dict[str, str]:
    """Map station / date / value to the actual column names of ``df`` (case-insensitive)."""
    want = {"station": s.col_station, "date": s.col_date, "value": s.col_temp}
    lower = {str(c).strip().lower(): c for c in df.columns}
    out = {}
    for role, name in want.items():
        col = lower.get(str(name).strip().lower())
        if col is None:
            raise KeyError(f"The file has no '{name}' column (found: {', '.join(map(str, df.columns))})")
        out[role] = col
    return out


def _normalise(s, df: pd.DataFrame) -> pd.DataFrame:
    """Station / date / value as the pipeline reads them, plus a merge key. Unparseable dates drop out."""
    from src.validation import parse_dates

    cols = _columns(s, df)
    out = pd.DataFrame(index=df.index)
    station = df[cols["station"]].astype("string").str.strip()
    if s.config.get("data", {}).get("station_uppercase", True):
        station = station.str.upper()
    out["station"] = station
    out["date"] = parse_dates(df[cols["date"]].astype("string"),
                              s.date_format, s.config.get("date_fallback_formats", []))
    out["value"] = pd.to_numeric(df[cols["value"]], errors="coerce")
    out = out[out["station"].notna() & out["date"].notna()]
    out["key"] = out["station"].astype(str) + "|" + out["date"].dt.strftime("%Y-%m-%d")
    return out


def read_raw(s) -> pd.DataFrame:
    """The current raw file as text columns, or an empty frame when it does not exist yet."""
    p = raw_file(s)
    if not p.is_file():
        return pd.DataFrame(columns=[s.col_station, s.col_date, s.col_temp], dtype="string")
    return pd.read_csv(p, dtype=str, keep_default_na=False, na_values=[""], encoding="utf-8-sig")


# ----------------------------------------------------------------------------- planning a merge

def plan(s, upload: pd.DataFrame) -> Dict[str, Any]:
    """What an upload would add, without writing anything.

    Returns the counts the upload page shows plus ``_rows``: the new rows in the pipeline's own
    column order and date format, ready to append.
    """
    current = read_raw(s)
    cur_norm = _normalise(s, current) if len(current) else pd.DataFrame(columns=["station", "date", "value", "key"])
    up_norm = _normalise(s, upload)

    unreadable = int(len(upload) - len(up_norm))
    # A file that repeats a day for the same station keeps its first row (the pipeline flags duplicates).
    up_unique = up_norm.drop_duplicates(subset="key", keep="first")
    within_file = int(len(up_norm) - len(up_unique))

    known = set(cur_norm["key"]) if len(cur_norm) else set()
    is_new = ~up_unique["key"].isin(known)
    new_rows = up_unique[is_new]

    cur_last = cur_norm["date"].max() if len(cur_norm) else None
    after_end = int((new_rows["date"] > cur_last).sum()) if cur_last is not None and len(new_rows) else len(new_rows)

    # The pipeline requires that no target falls on or after the end of the test period: the test
    # window ends on the last original observation (feature check ``no_filled_targets``). Observed
    # values beyond it therefore need the test period moved forward, which changes the split and
    # makes the saved screening and tuning results invalid - so it forces a full re-analysis.
    test_end = pd.to_datetime(s.config.get("split", {}).get("test_end_date"), errors="coerce")
    new_last = new_rows["date"].max() if len(new_rows) else None
    past_test_end = bool(new_last is not None and pd.notna(test_end) and new_last > test_end)
    merged_last = max([d for d in (cur_last, new_last) if d is not None], default=None)

    rows = pd.DataFrame({
        s.col_station: new_rows["station"].astype(str),
        s.col_date: new_rows["date"].dt.strftime(s.date_format),
        s.col_temp: new_rows["value"],
    }) if len(new_rows) else pd.DataFrame(columns=[s.col_station, s.col_date, s.col_temp])

    return {
        "upload_rows": int(len(upload)),
        "unreadable_rows": unreadable,
        "duplicate_rows_in_file": within_file,
        "already_present": int((~is_new).sum()),
        "new_rows": int(len(new_rows)),
        "new_rows_after_current_end": after_end,
        "new_stations": sorted(set(new_rows["station"]) - set(cur_norm["station"])) if len(new_rows) else [],
        "stations_touched": sorted(set(new_rows["station"])) if len(new_rows) else [],
        "new_date_range": ([new_rows["date"].min().strftime("%Y-%m-%d"), new_rows["date"].max().strftime("%Y-%m-%d")]
                           if len(new_rows) else None),
        "current_rows": int(len(current)),
        "current_date_range": ([cur_norm["date"].min().strftime("%Y-%m-%d"), cur_last.strftime("%Y-%m-%d")]
                               if len(cur_norm) else None),
        "test_end_date": test_end.strftime("%Y-%m-%d") if pd.notna(test_end) else None,
        "extends_past_test_end": past_test_end,
        "rows_past_test_end": int((new_rows["date"] > test_end).sum()) if past_test_end else 0,
        "suggested_test_end": merged_last.strftime("%Y-%m-%d") if past_test_end and merged_last is not None else None,
        "_rows": rows,
    }


# ----------------------------------------------------------------------------- committing

def _next_index(s, manifest: Optional[Dict[str, Any]] = None) -> int:
    """One past the highest version number in the manifest (ids stay unique after manual edits)."""
    data = manifest if manifest is not None else load_manifest(s)
    highest = 0
    for v in data.get("versions", []):
        try:
            highest = max(highest, int(str(v.get("id", "")).split("_", 1)[0].lstrip("v")))
        except (ValueError, AttributeError):
            continue
    return highest + 1


def _make_id(index: int) -> str:
    # Microseconds as well as the counter: two versions written in the same second stay distinct.
    return f"v{index:04d}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"


def snapshot(s, note: str, index: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Copy the current raw file into ``versions/`` and return its manifest entry."""
    src = raw_file(s)
    if not src.is_file():
        return None
    vid = _make_id(_next_index(s) if index is None else index)
    dest = versions_dir(s) / f"{vid}{src.suffix}"
    shutil.copy2(src, dest)
    cur = read_raw(s)
    return {"id": vid, "file": dest.name, "created": datetime.now().isoformat(timespec="seconds"),
            "rows": int(len(cur)), "note": note, "bytes": dest.stat().st_size}


def commit(s, merge_plan: Dict[str, Any], source_name: str) -> Dict[str, Any]:
    """Append the planned new rows to the raw file, keeping a snapshot of the previous version.

    The merged file is written to a temporary name and moved into place, so an interrupted write
    cannot leave the pipeline with a half-written data file.
    """
    rows: pd.DataFrame = merge_plan["_rows"]
    if not len(rows):
        raise ValueError("The upload contains no rows that are missing from the current data file.")

    index = _next_index(s)
    previous = snapshot(s, f"before adding {len(rows):,} rows from {source_name}", index=index)
    target = raw_file(s)
    target.parent.mkdir(parents=True, exist_ok=True)

    current = read_raw(s)
    merged = pd.concat([current, rows.astype("string")], ignore_index=True) if len(current) else rows.astype("string")

    tmp = target.with_suffix(target.suffix + ".tmp")
    merged.to_csv(tmp, index=False, encoding="utf-8")
    tmp.replace(target)

    entry = {
        "id": _make_id(index + 1),
        "file": None,                       # the live file, not a snapshot
        "created": datetime.now().isoformat(timespec="seconds"),
        "source_file": source_name,
        "rows": int(len(merged)),
        "rows_added": int(len(rows)),
        "rows_already_present": merge_plan.get("already_present", 0),
        "date_range": merge_plan.get("new_date_range"),
        "stations_touched": len(merge_plan.get("stations_touched", [])),
        "new_stations": merge_plan.get("new_stations", []),
        "restores_from": previous["id"] if previous else None,
        "note": f"added {len(rows):,} new rows from {source_name}",
        "current": True,
    }
    data = load_manifest(s)
    for v in data["versions"]:
        v["current"] = False
    if previous:
        data["versions"].append(previous)
    data["versions"].append(entry)
    save_manifest(s, data)
    return entry


def restore(s, version_id: str) -> Dict[str, Any]:
    """Put a snapshot back as the live raw file (the file being replaced is snapshotted first)."""
    data = load_manifest(s)
    match = next((v for v in data["versions"] if v.get("id") == version_id and v.get("file")), None)
    if match is None:
        raise ValueError(f"No stored data version '{version_id}'.")
    src = versions_dir(s) / match["file"]
    if not src.is_file():
        raise FileNotFoundError(f"The stored file for version '{version_id}' is missing.")

    index = _next_index(s)
    previous = snapshot(s, f"before restoring {version_id}", index=index)
    target = raw_file(s)
    tmp = target.with_suffix(target.suffix + ".tmp")
    shutil.copy2(src, tmp)
    tmp.replace(target)

    entry = {"id": _make_id(index + 1), "file": None, "created": datetime.now().isoformat(timespec="seconds"),
             "rows": int(match.get("rows") or 0), "restored_from": version_id,
             "note": f"restored data version {version_id}", "current": True}
    for v in data["versions"]:
        v["current"] = False
    if previous:
        data["versions"].append(previous)
    data["versions"].append(entry)
    save_manifest(s, data)
    return entry


def set_test_end(s, new_end: str) -> Dict[str, Any]:
    """Move ``split.test_end_date`` forward, keeping a copy of the previous ``config.json``.

    Needed when new observations run past the current test period: the pipeline's feature check
    requires the test window to end on the last observation. Changing the split also changes the
    reuse fingerprint, so screening and tuning must be redone - the caller runs the full pipeline.
    """
    cfg_file = Path(s.paths.config_file)
    before = json.loads(cfg_file.read_text(encoding="utf-8"))
    old_end = before.get("split", {}).get("test_end_date")
    if old_end == new_end:
        return {"changed": False, "test_end_date": new_end}

    # Copy the bytes rather than re-serialising, so restoring gives back the original file exactly.
    backup = versions_dir(s) / f"config_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
    shutil.copy2(cfg_file, backup)

    after = json.loads(json.dumps(before))
    after.setdefault("split", {})["test_end_date"] = new_end
    after["split"]["_decision"] = (
        f"{datetime.now().strftime('%Y-%m-%d')} (web app): test period extended to {new_end} "
        f"after new observations were added (was {old_end})")
    # Same shape as the pipeline's own writer (UTF-8, indent 2, trailing newline).
    tmp = cfg_file.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(after, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(cfg_file)

    data = load_manifest(s)
    data["versions"].append({
        "id": _make_id(_next_index(s)), "file": None, "config_backup": backup.name,
        "created": datetime.now().isoformat(timespec="seconds"), "kind": "config",
        "note": f"test period end moved from {old_end} to {new_end}",
    })
    save_manifest(s, data)
    return {"changed": True, "test_end_date": new_end, "previous_test_end_date": old_end,
            "config_backup": backup.name}


def summary(s) -> Dict[str, Any]:
    """Current data file plus its version history, for the upload page."""
    p = raw_file(s)
    cur = read_raw(s)
    norm = _normalise(s, cur) if len(cur) else None
    return {
        "file": s.paths.relative(p) if p.is_file() else None,
        "exists": p.is_file(),
        "rows": int(len(cur)),
        "stations": int(norm["station"].nunique()) if norm is not None and len(norm) else 0,
        "date_range": ([norm["date"].min().strftime("%Y-%m-%d"), norm["date"].max().strftime("%Y-%m-%d")]
                       if norm is not None and len(norm) else None),
        "test_end_date": s.config.get("split", {}).get("test_end_date"),
        "test_start_date": s.config.get("split", {}).get("test_start_date"),
        "versions": versions(s),
    }


def restore_config(s, backup_name: str) -> bool:
    """Put a saved ``config.json`` back (used when a run that moved the test period fails)."""
    src = versions_dir(s) / backup_name
    if not src.is_file():
        return False
    cfg_file = Path(s.paths.config_file)
    tmp = cfg_file.with_suffix(".json.tmp")
    shutil.copy2(src, tmp)
    tmp.replace(cfg_file)
    data = load_manifest(s)
    data["versions"].append({
        "id": _make_id(_next_index(s)), "file": None, "kind": "config",
        "created": datetime.now().isoformat(timespec="seconds"),
        "note": f"configuration restored from {backup_name} after a failed run",
    })
    save_manifest(s, data)
    return True
