/* Data upload page: drag & drop, preview, validation with the pipeline's rules, then the
   optional second step - merging the new rows into the pipeline's data file and running the
   pipeline in the background (progress is polled from /api/pipeline-run). */
(function () {
    'use strict';
    let file = null;         // the File chosen in the browser
    let saved = null;        // its name in static/uploads once validated (what /api/apply-data takes)
    let merge = null;        // what the upload would add, from /api/upload
    let modes = [];          // run modes offered by the server
    let poll = null;         // interval id while a run is active

    document.addEventListener('DOMContentLoaded', () => {
        setupDragDrop();
        tfs.$('file-input').addEventListener('change', (e) => e.target.files[0] && selectFile(e.target.files[0]));
        tfs.$('validate-btn').addEventListener('click', validateFile);
        tfs.$('upload-more-btn').addEventListener('click', resetForm);
        tfs.$('upload-again-btn').addEventListener('click', resetForm);
        tfs.$('apply-btn').addEventListener('click', applyData);
        tfs.$('rerun-btn').addEventListener('click', rerunOnly);
        tfs.$('run-cancel-btn').addEventListener('click', cancelRun);
        tfs.$('run-refresh-btn').addEventListener('click', refreshRun);
        loadDataStatus();
    });

    function setupDragDrop() {
        const area = tfs.$('drag-drop-area');
        area.addEventListener('click', () => tfs.$('file-input').click());
        area.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); tfs.$('file-input').click(); }
        });
        area.addEventListener('dragover', (e) => { e.preventDefault(); area.classList.add('dragover'); });
        area.addEventListener('dragleave', () => area.classList.remove('dragover'));
        area.addEventListener('drop', (e) => {
            e.preventDefault();
            area.classList.remove('dragover');
            const f = e.dataTransfer.files[0];
            if (f && /\.(csv|xlsx)$/i.test(f.name)) selectFile(f);
            else alertBox(['Please drop a .csv or .xlsx file.']);
        });
    }

    function selectFile(f) {
        file = f;
        tfs.setText('file-name', f.name);
        tfs.setText('file-size', f.size > 1048576 ? `${(f.size / 1048576).toFixed(2)} MB` : `${(f.size / 1024).toFixed(2)} KB`);
        tfs.show('file-info', true);
        tfs.$('validate-btn').disabled = false;
        tfs.show('validation-section', false);
        previewFile(f);
    }

    /** Minimal CSV line splitter that respects quoted fields. */
    function splitCsv(line) {
        const out = [];
        let cur = '', q = false;
        for (let i = 0; i < line.length; i++) {
            const c = line[i];
            if (c === '"') { if (q && line[i + 1] === '"') { cur += '"'; i++; } else q = !q; }
            else if (c === ',' && !q) { out.push(cur); cur = ''; }
            else cur += c;
        }
        out.push(cur);
        return out;
    }

    function previewFile(f) {
        tfs.$('preview-head').innerHTML = '';
        tfs.$('preview-body').innerHTML = '';
        if (!/\.csv$/i.test(f.name)) {
            tfs.show('preview-section', true);
            tfs.setText('preview-info', 'Preview is shown for CSV files; Excel files are checked on the server.');
            return;
        }
        const reader = new FileReader();
        reader.onload = (e) => {
            const text = String(e.target.result).replace(/^﻿/, '');
            const lines = text.split(/\r?\n/).filter((l) => l.trim() !== '');
            if (!lines.length) return;
            const header = splitCsv(lines[0]);
            tfs.$('preview-head').innerHTML = '<tr>' + header.map((h) => `<th>${tfs.esc(h)}</th>`).join('') + '</tr>';
            tfs.$('preview-body').innerHTML = lines.slice(1, 11).map((l) =>
                '<tr>' + splitCsv(l).map((c) => `<td>${tfs.esc(c)}</td>`).join('') + '</tr>').join('');
            tfs.show('preview-section', true);
            tfs.setText('preview-info', `Showing the first ${Math.min(10, lines.length - 1)} of ${(lines.length - 1).toLocaleString()} data rows`);
        };
        reader.readAsText(f.slice(0, 5 * 1024 * 1024));   // enough for the preview and the row count of typical files
    }

    async function validateFile() {
        if (!file) return alertBox(['Please select a file.']);
        const fd = new FormData();
        fd.append('file', file);
        tfs.show('loading-spinner', true);
        tfs.$('validate-btn').disabled = true;
        const data = await tfs.api('/api/upload', fd);
        tfs.show('loading-spinner', false);
        tfs.$('validate-btn').disabled = false;
        if (data.status === 'success') displaySuccessResults(data);
        else displayErrorResults(data.errors && data.errors.length ? data.errors : [data.message || 'Upload failed.']);
    }

    function displaySuccessResults(d) {
        tfs.show('error-card', false);
        tfs.show('success-card', true);
        tfs.show('validation-section', true);
        saved = d.saved_as || null;
        merge = d.merge || null;
        showMergeStep();
        const withWarnings = d.warnings && d.warnings.length;
        tfs.setText('success-title', withWarnings ? '✅ Validation passed with warnings' : '✅ Validation Successful!');
        tfs.setText('success-message', `${d.message}. ${d.note || ''}`);
        tfs.setText('val-rows', Number(d.rows).toLocaleString());
        tfs.setText('val-date-range', d.date_range);
        tfs.setText('val-stations', `${d.stations.length}: ${d.stations.slice(0, 12).join(', ')}${d.stations.length > 12 ? ', …' : ''}`);
        const totalMissing = Object.values(d.missing_values).reduce((a, b) => a + b, 0);
        tfs.setText('val-missing', totalMissing.toLocaleString());
        tfs.setText('val-missing-days', d.missing_days == null ? '--' : Number(d.missing_days).toLocaleString());
        tfs.setText('val-saved', d.saved_as || '--');
        tfs.show('warning-block', !!withWarnings);
        tfs.$('warning-list').innerHTML = (d.warnings || []).map((w) => `<li>${tfs.esc(w)}</li>`).join('');
        tfs.$('missing-body').innerHTML = Object.entries(d.missing_values)
            .map(([col, n]) => `<tr><td>${tfs.esc(col)}</td><td class="num">${Number(n).toLocaleString()}</td></tr>`).join('');
        tfs.$('validation-section').scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    function displayErrorResults(errors) {
        tfs.show('success-card', false);
        tfs.show('error-card', true);
        tfs.show('validation-section', true);
        tfs.show('merge-card', false);
        tfs.show('nothing-new-card', false);
        tfs.$('error-list').innerHTML = errors.map((e) => `<li>${tfs.esc(e)}</li>`).join('');
        tfs.$('validation-section').scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    function alertBox(errors) {
        displayErrorResults(errors);
    }

    function resetForm() {
        file = null;
        saved = null;
        merge = null;
        tfs.$('file-input').value = '';
        ['file-info', 'preview-section', 'validation-section', 'merge-card', 'nothing-new-card']
            .forEach((id) => tfs.show(id, false));
        tfs.$('validate-btn').disabled = true;
        window.scrollTo({ top: 0, behavior: 'smooth' });
    }

    // ------------------------------------------------------------------ step 2: add to the pipeline

    const num = (v) => (v === null || v === undefined ? '--' : Number(v).toLocaleString());
    const range = (r) => (Array.isArray(r) && r.length === 2 ? `${r[0]} → ${r[1]}` : '--');

    /** Show the "add to pipeline" panel, or explain that the file adds nothing. */
    function showMergeStep() {
        if (!merge) {                       // the server could not compare (e.g. no data file yet)
            tfs.show('merge-card', false);
            tfs.show('nothing-new-card', false);
            return;
        }
        if (!merge.new_rows) {
            tfs.show('merge-card', false);
            tfs.show('nothing-new-card', true);
            tfs.setText('nothing-new-text',
                `All ${num(merge.upload_rows)} rows are already in the pipeline's data file `
                + `(${num(merge.current_rows)} rows, ${range(merge.current_date_range)}). `
                + 'Nothing would change, so there is nothing to run.');
            return;
        }
        tfs.show('nothing-new-card', false);
        tfs.show('merge-card', true);
        tfs.setText('merge-summary',
            `${num(merge.new_rows)} of the ${num(merge.upload_rows)} rows in this file are not yet in the `
            + 'pipeline\'s data. Only those will be added — nothing is overwritten.');
        tfs.setText('merge-upload-rows', num(merge.upload_rows));
        tfs.setText('merge-new-rows', num(merge.new_rows)
            + (merge.new_rows_after_current_end ? ` (${num(merge.new_rows_after_current_end)} after the current last day)` : ''));
        tfs.setText('merge-present', num(merge.already_present)
            + (merge.duplicate_rows_in_file ? ` · ${num(merge.duplicate_rows_in_file)} repeated inside the file` : ''));
        tfs.setText('merge-range', range(merge.new_date_range));
        tfs.setText('merge-stations', num((merge.stations_touched || []).length));
        tfs.setText('merge-current', `${num(merge.current_rows)} rows, ${range(merge.current_date_range)}`);

        showTestPeriodWarning();

        const fresh = merge.new_stations || [];
        tfs.show('merge-new-stations', fresh.length > 0);
        if (fresh.length) {
            tfs.$('merge-new-stations').innerHTML =
                `<p>⚠️ ${fresh.length} station(s) are not in the current data and have no history yet: `
                + `<strong>${tfs.esc(fresh.slice(0, 15).join(', '))}</strong>`
                + `${fresh.length > 15 ? ', …' : ''}. They need at least a year of data before they `
                + 'become forecastable.</p>';
        }
        renderModes();
    }

    /** New observations dated after the test period can only be used if that period moves, which
        changes the split and so forces a full re-analysis. Say so before anything is written. */
    /** Rows dated after the test period can be used straight away for forecasting, but rebuilding the
        features (or re-scoring the models) first needs that period moved — which forces a full run. */
    function showTestPeriodWarning() {
        const needed = !!(merge && merge.extends_past_test_end);
        tfs.show('test-period-block', needed);
        const box = tfs.$('extend-test-period');
        if (box) box.checked = false;
        if (!needed) return;
        tfs.setText('test-period-text',
            `${num(merge.rows_past_test_end)} of the new rows are dated after ${merge.test_end_date}, `
            + 'the end of the test period the models are scored on. Forecasting from them is fine — '
            + '"Update the data only" does that in seconds. But rebuilding the features or re-scoring '
            + 'the models needs the test period to end on the last observation.');
        tfs.setText('extend-test-label',
            `Move the end of the test period to ${merge.suggested_test_end} and run the full `
            + 're-analysis instead. The models are then scored on a different (longer) test period, and '
            + 'because the split changes, screening and tuning must be redone — expect hours, not minutes.');
        if (box) box.onchange = renderModes;
    }

    function renderModes() {
        const box = tfs.$('mode-choices');
        if (!box || !modes.length) return;
        const past = !!(merge && merge.extends_past_test_end);
        const extend = past && tfs.$('extend-test-period') && tfs.$('extend-test-period').checked;
        const allowed = (key) => (!past ? true : extend ? key === 'full' : key === 'data');
        const preferred = past ? (extend ? 'full' : 'data') : 'data';
        box.innerHTML = modes.map((m) => {
            const off = !allowed(m.key);
            const why = off
                ? (extend ? ' <span class="muted">(a moved test period needs the full run)</span>'
                          : ' <span class="muted">(not possible past the test period)</span>')
                : '';
            return `
            <label class="form-row" style="align-items: flex-start; gap: .5rem; display: flex; margin-bottom: .6rem;${off ? ' opacity: .55;' : ''}">
                <input type="radio" name="run-mode" value="${tfs.esc(m.key)}"${m.key === preferred ? ' checked' : ''}${off ? ' disabled' : ''}>
                <span><strong>${tfs.esc(m.label)}</strong>${why}<br>
                <span class="muted">${tfs.esc(m.description)}</span><br>
                <span class="muted">Stages: ${m.stages.map((s) => tfs.esc(s.title)).join(' → ')}</span></span>
            </label>`;
        }).join('');
    }

    function chosenMode() {
        const el = document.querySelector('input[name="run-mode"]:checked');
        return el ? el.value : 'refresh';
    }

    async function applyData() {
        if (!saved) return alertBox(['Validate a file first.']);
        const mode = chosenMode();
        const extend = !!(merge.extends_past_test_end && tfs.$('extend-test-period').checked);
        const label = (modes.find((m) => m.key === mode) || {}).label || mode;
        let extra = mode === 'full'
            ? '\n\nA full re-analysis re-runs model screening and tuning and can take hours.'
            : '';
        if (extend) extra += `\n\nThe test period will end at ${merge.suggested_test_end} instead of `
            + `${merge.test_end_date}, so the reported accuracy will be measured on a different period.`;
        if (!window.confirm(`Add ${Number(merge.new_rows).toLocaleString()} new rows to the pipeline's `
            + `data file and start "${label}"?${extra}`)) return;

        tfs.$('apply-btn').disabled = true;
        tfs.setText('apply-btn', 'Starting…');
        const d = await tfs.api('/api/apply-data', { file: saved, mode, extend_test_period: extend });
        tfs.$('apply-btn').disabled = false;
        tfs.setText('apply-btn', 'Add rows & run pipeline');
        if (d.status !== 'success') {
            tfs.notice([d.message || 'The data could not be added.'].concat(d.errors || []));
            if (d.merge) { merge = d.merge; showMergeStep(); }
            return;
        }
        tfs.notice([]);
        tfs.show('merge-card', false);
        renderRun(d.run);
        startPolling();
        loadDataStatus();
        tfs.$('run-section').scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    async function rerunOnly() {
        const mode = window.confirm('Re-run including model screening and tuning (this can take hours)?\n\n'
            + 'OK = full re-analysis, Cancel = rebuild and refit only.') ? 'full' : 'refresh';
        tfs.$('rerun-btn').disabled = true;
        const d = await tfs.api('/api/pipeline-run/start', { mode });
        tfs.$('rerun-btn').disabled = false;
        if (d.status !== 'success') return tfs.notice([d.message || 'The run could not be started.']);
        tfs.notice([]);
        renderRun(d.run);
        startPolling();
        tfs.$('run-section').scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    async function cancelRun() {
        if (!window.confirm('Stop the run? The stage in progress is terminated; earlier results stay.')) return;
        const d = await tfs.api('/api/pipeline-run/cancel', {});
        if (d.status !== 'success') return tfs.notice([d.message || 'Nothing to cancel.']);
        renderRun(d.run);
    }

    async function refreshRun() {
        const d = await tfs.api('/api/pipeline-run');
        renderRun(d.run);
    }

    function startPolling() {
        if (poll) clearInterval(poll);
        poll = setInterval(async () => {
            const d = await tfs.api('/api/pipeline-run');
            renderRun(d.run);
            if (!d.busy) {
                clearInterval(poll);
                poll = null;
                loadDataStatus();
            }
        }, 3000);
    }

    const STAGE_MARK = {
        completed: '✅', running: '⏳', failed: '❌', pending: '·', skipped: '—', cancelled: '⛔',
    };

    function renderRun(run) {
        if (!run) { tfs.show('run-section', false); return; }
        tfs.show('run-section', true);
        const done = (run.stages || []).filter((s) => s.status === 'completed').length;
        tfs.setText('run-title',
            `${run.mode_label || run.mode} · ${run.status} · ${done}/${(run.stages || []).length} stages`
            + (run.started ? ` · started ${run.started.replace('T', ' ')}` : ''));
        tfs.setText('run-message', run.message || '');
        tfs.$('run-stage-body').innerHTML = (run.stages || []).map((s) => `
            <tr><td>${STAGE_MARK[s.status] || ''} ${tfs.esc(s.title)}</td>
                <td>${tfs.esc(s.status)}</td>
                <td class="num">${s.seconds == null ? '' : Number(s.seconds).toLocaleString()}</td></tr>`).join('');
        const active = (run.stages || []).filter((s) => (s.log || []).length).slice(-1)[0];
        tfs.setText('run-log', active && active.log.length ? active.log.join('\n') : 'No output yet.');
        tfs.show('run-cancel-btn', run.status === 'running');
        if (run.status === 'completed') {
            tfs.notice(['The pipeline finished. The dashboard, forecasts and model pages now use the new data.']);
        }
    }

    // ------------------------------------------------------------------ data file & versions

    async function loadDataStatus() {
        const d = await tfs.api('/api/data-status');
        if (d.status !== 'success') {
            tfs.setText('data-file', d.message || 'unavailable');
            return;
        }
        modes = d.modes || [];
        renderModes();
        const info = d.data || {};
        tfs.setText('data-file', info.file || 'not created yet');
        tfs.setText('data-rows', num(info.rows));
        tfs.setText('data-stations', num(info.stations));
        tfs.setText('data-range', range(info.date_range));
        tfs.setText('data-test-period', info.test_start_date
            ? `${info.test_start_date} → ${info.test_end_date}` : '--');
        renderVersions(info.versions || []);
        if (d.run) renderRun(d.run);
        if (d.busy && !poll) startPolling();
        tfs.$('rerun-btn').disabled = !!d.busy;
    }

    function renderVersions(list) {
        const body = tfs.$('versions-body');
        if (!list.length) {
            body.innerHTML = '<tr><td colspan="4" class="muted">No updates yet — the data file is the original one.</td></tr>';
            return;
        }
        body.innerHTML = list.map((v) => {
            const when = String(v.created || '').replace('T', ' ');
            const restore = v.file
                ? `<button class="btn btn-outline btn-sm" data-restore="${tfs.esc(v.id)}">Restore</button>`
                : (v.current ? '<span class="badge">in use</span>' : '');
            return `<tr><td>${tfs.esc(when)}</td>
                        <td>${tfs.esc(v.note || v.id)}</td>
                        <td class="num">${num(v.rows)}</td>
                        <td>${restore}</td></tr>`;
        }).join('');
        body.querySelectorAll('button[data-restore]').forEach((b) =>
            b.addEventListener('click', () => restoreVersion(b.getAttribute('data-restore'))));
    }

    async function restoreVersion(id) {
        if (!window.confirm(`Restore the data file to version ${id}?\n\n`
            + 'The current file is saved as a version first. Press OK, then choose whether to re-run.')) return;
        const rerun = window.confirm('Re-run the pipeline on the restored data now?\n\n'
            + 'OK = restore and run a fast refresh, Cancel = restore only.');
        const d = await tfs.api('/api/data-versions/restore', { id, rerun, mode: 'refresh' });
        if (d.status !== 'success') return tfs.notice([d.message || 'The version could not be restored.']);
        tfs.notice([d.message]);
        if (d.run) { renderRun(d.run); startPolling(); }
        loadDataStatus();
    }
})();
