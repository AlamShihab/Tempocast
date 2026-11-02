# CSV write failure audit — 7 October 2026

The failure was a Windows file-sharing lock on
`Work with claude/data/processed/02_daily_standardized_sample.csv`.
The pipeline treated an unavailable preview export as a fatal preparation error.

## Evidence

- Failed web run: `WEB_20261007_123942`; failed preparation subprocess:
  `PIPE_20261007T063951Z_314f88`, at 12:39:52 Bangladesh time.
- The traceback ends at `src/preparation.py` calling pandas `to_csv()` on the
  existing preview. Validation had already succeeded.
- A non-destructive Windows open probe with read/write access reported:
  “The process cannot access the file ... because it is being used by another process.”
- The file had the `Archive` attribute, not `ReadOnly`. Its ACL granted the user
  FullControl. This distinguishes the observed failure from a missing write permission.
- The process holding the file was not identified. Excel is a possible source,
  but the audit does not establish which application owned the lock.

## Why it stopped the run

Preparation wrote the main parquet first, then attempted to overwrite the fixed
preview filename directly. There was no retry or alternate export path. That
exception prevented the eligibility report, preparation report, and EDA from
being regenerated and stopped every later stage.

The web runner then restored the raw file and test-period configuration, but it
did not restore generated outputs. Its claim that previous results were
“untouched” was incorrect. At inspection, the validation report described
699,527 uploaded rows while the restored raw data contained 686,378 rows; the
parquet had been rewritten at the failure time, while the preparation report
still described the earlier run. The failed full run had also already moved
saved screening and tuning results into `experiments/archive_20261007_123942`.

## Fix

- Preparation writes the parquet, preview CSV, and eligibility CSV to temporary
  files and then replaces their destinations. Interrupted writes preserve the
  prior destination file. Transient replacement locks receive two short retries.
- A preview that remains locked gets a fresh unique CSV filename. Preparation
  continues, records the actual filename in its report and stage outputs, and
  logs an explicit warning that the original preview was not updated.
- Required outputs still fail if they cannot be written; their messages identify
  the file and explain how to resolve the lock or permissions issue. Disk-full
  and other unrelated errors are not silently ignored.
- The web runner retains stage warnings in its completed-run message, describes
  partial output updates honestly on failure, and defers archiving previous fits
  until screening starts.

These changes protect individual preparation files. They do not make all stages
one transaction; a failure in another required output can still leave a mixture
of outputs until the pipeline is rerun successfully.

## Verification and current state

- Pipeline test suite: **110 passed**.
- Flask app test suite: **40 passed**.
- Regressions cover a real Windows sharing lock, simulated persistent and
  transient locks, preservation of a previous file on a failed write, alternate
  preview reporting, required-output errors, warning visibility, and archive timing.
- Suites emitted existing model convergence / synthetic tuning-bound warnings;
  there were no test failures.
- A separate full run, `WEB_20261007_124654`, started during this repair. By the
  final inspection it had completed setup, validation, preparation, split,
  features, and baselines and was performing model screening. It was left running.
- That run used the updated preparation code and successfully refreshed the
  canonical preview at 12:47:03. The lock had been released by then. Validation
  and preparation now both match the current raw-file SHA-256:
  `b858382cf1b6299e68c9a5fd2204747207ce888daa85987175f829871dfa2f5d`.
  All preparation/validation reconciliation counts match.
- The proposed separate data-recovery run was stopped by its active-run guard
  before it made any changes; no duplicate pipeline was launched.

The original failed upload's **13,149 new rows were rolled back**. The ongoing
run is a rerun on the existing raw data, not a reapplication of that upload.
After the ongoing run finishes, reapply the saved upload if those rows are wanted.
Restart the Flask app after the run finishes to load the runner's updated
messages and archive timing. Existing in-memory runs keep their original runner code.

The original logs and archived training results were retained. Existing edits
to `CLAUDE.md` and `config/feature_contract.json` were not changed by this repair.
