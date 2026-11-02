/* Dashboard: KPIs, leaderboard, RMSE-by-horizon and coverage charts, data quality, downloads. */
(function () {
    'use strict';
    let data = null;

    document.addEventListener('DOMContentLoaded', () => {
        loadDashboardData();
        loadFigures();
        loadDownloads();
        document.addEventListener('tfs:theme', () => data && drawCharts(data));
    });

    async function loadDashboardData() {
        data = await tfs.api('/api/dashboard-data', {});
        if (data.status !== 'ok') {
            tfs.notice([data.message || 'Error loading dashboard data. Check that the pipeline results exist.']);
            tfs.$('leaderboard-body').innerHTML = '<tr><td colspan="7">No data.</td></tr>';
            return;
        }
        tfs.notice(data.messages);

        tfs.setText('total-stations', data.total_stations ?? '--');
        tfs.setText('forecast-stations', data.forecastable_stations ?? '--');
        tfs.setText('avg-rmse', data.avg_rmse === null ? '--' : tfs.fmt(data.avg_rmse, 2));
        tfs.setText('data-period', data.data_period || '--');
        tfs.setText('num-models', data.available_models.length ? data.leaderboard.filter((r) => r.production_model).length + ' / ' + data.horizons.length : '--');
        const a = data.audit || {};
        tfs.setText('audit-label', a.pipeline_status
            ? `Audit: ${a.pipeline_status} · leakage: ${a.data_leakage_detected ? 'detected' : 'none'}` : 'Audit: not run yet');
        if (data.test_period) tfs.setText('test-period', `(${data.test_period[0]} to ${data.test_period[1]})`);

        populateLeaderboard(data.leaderboard);
        drawCharts(data);

        const q = data.data_quality || {};
        tfs.setText('missing-percent', q.missing_percent == null ? '--' : tfs.pct(q.missing_percent, 2));
        tfs.setText('filled-percent', q.not_in_original_percent == null ? '--' : tfs.pct(q.not_in_original_percent, 2));
        tfs.setText('complete-stations', q.stations_with_complete_data == null ? '--'
            : `${q.stations_with_complete_data} of ${data.total_stations}`);
        tfs.setText('validation-status', q.validation_status || '--');
    }

    function populateLeaderboard(rows) {
        const tbody = tfs.$('leaderboard-body');
        if (!rows.length) {
            tbody.innerHTML = '<tr><td colspan="7">No results yet - run the pipeline.</td></tr>';
            return;
        }
        tbody.innerHTML = rows.map((r) => {
            let status;
            if (r.status === 'pending') status = '<span class="badge pending">pending</span>';
            else if (r.production_kind === 'model') status = `<span class="badge success">✓ ${tfs.esc(r.production_model)}</span>`;
            else status = `<span class="badge warning">baseline: ${tfs.esc(r.production_model)}</span>`;
            const imp = r.improvement_percent == null ? '--'
                : `<span class="${r.improvement_percent > 0 ? 'badge success' : 'badge error'}">${r.improvement_percent > 0 ? '+' : ''}${tfs.fmt(r.improvement_percent, 1)}%</span>`;
            return `<tr>
                <td>${r.horizon} day${r.horizon > 1 ? 's' : ''}</td>
                <td>${tfs.esc(r.baseline_name || '--')}</td>
                <td class="num">${tfs.fmt(r.baseline_rmse, 3)}</td>
                <td>${tfs.esc(r.ml_model || '--')}</td>
                <td class="num">${tfs.fmt(r.ml_rmse, 3)}</td>
                <td class="num">${imp}</td>
                <td>${status}</td>
            </tr>`;
        }).join('');
    }

    function drawCharts(d) {
        createRmseChart(d.leaderboard);
        createCoverageChart(d.coverage);
    }

    function createRmseChart(rows) {
        if (!rows.length) return tfs.emptyChart('rmse-chart', 'No results yet.');
        const t = tfs.theme();
        const x = rows.map((r) => r.horizon);
        const traces = [{
            x, y: rows.map((r) => r.baseline_rmse), name: 'Best baseline', type: 'scatter', mode: 'lines+markers',
            line: { color: t.s2, width: 2, dash: 'dot' }, marker: { size: 8, color: t.s2 },
            customdata: rows.map((r) => r.baseline_name || ''),
            hovertemplate: '%{y:.3f} °C · %{customdata}<extra>Baseline</extra>',
        }];
        if (rows.some((r) => r.ml_rmse != null)) {
            traces.push({
                x, y: rows.map((r) => r.ml_rmse), name: 'ML model', type: 'scatter', mode: 'lines+markers',
                line: { color: t.s1, width: 2 }, marker: { size: 8, color: t.s1 },
                customdata: rows.map((r) => r.ml_model || ''),
                hovertemplate: '%{y:.3f} °C · %{customdata}<extra>ML model</extra>',
            });
        }
        tfs.plot('rmse-chart', traces, tfs.layout({
            xaxis: { title: { text: 'Forecast horizon (days)' }, tickvals: x },
            yaxis: { title: { text: 'Test RMSE (°C)' }, rangemode: 'tozero' },
        }));
    }

    function createCoverageChart(cov) {
        if (!cov || !cov.length) return tfs.emptyChart('coverage-chart', 'Station coverage not available yet.');
        const t = tfs.theme();
        const rows = [...cov].sort((a, b) => b.observed_pct - a.observed_pct);
        const x = rows.map((r) => r.station);
        const bar = (key, name, color) => ({
            x, y: rows.map((r) => r[key]), name, type: 'bar', marker: { color, line: { color: t.bg, width: 1 } },
            hovertemplate: `%{y:.1f}%<extra>${name}</extra>`,
        });
        tfs.plot('coverage-chart', [
            bar('observed_pct', 'Observed (BMD)', t.s1),
            bar('filled_pct', 'Filled (input only)', t.s2),
            bar('missing_pct', 'Missing', t.neutral),
        ], tfs.layout({
            barmode: 'stack', bargap: 0.25, legend: { traceorder: 'normal' },
            margin: { l: 56, r: 20, t: 20, b: 120 },
            xaxis: { tickangle: -60, tickfont: { size: 10 } },
            yaxis: { title: { text: '% of station days' }, range: [0, 100] },
        }));
    }

    let files = [];
    let showAll = false;

    async function loadDownloads() {
        const res = await tfs.api('/api/downloads');
        files = res.status === 'ok' ? res.files : [];
        // before the key reports exist, list everything
        showAll = !files.some((f) => f.key);
        tfs.$('downloads-toggle').addEventListener('click', () => { showAll = !showAll; renderDownloads(); });
        renderDownloads();
    }

    function renderDownloads() {
        const tbody = tfs.$('downloads-body');
        const toggle = tfs.$('downloads-toggle');
        if (!files.length) {
            tbody.innerHTML = '<tr><td colspan="5">No result files yet.</td></tr>';
            tfs.show('downloads-toggle', false);
            return;
        }
        const rows = showAll ? files : files.filter((f) => f.key);
        tbody.innerHTML = rows.map((f) => `<tr>
            <td>${tfs.esc(f.name)}</td><td class="muted-cell">${tfs.esc(f.description || '')}</td>
            <td class="num">${tfs.fmt(f.size_kb, 1)}</td><td>${tfs.esc(f.modified)}</td>
            <td><a class="btn btn-secondary btn-sm" href="/api/download/${encodeURIComponent(f.name)}">Download</a></td>
        </tr>`).join('');
        const others = files.length - files.filter((f) => f.key).length;
        tfs.show('downloads-toggle', others > 0 && files.some((f) => f.key));
        toggle.textContent = showAll ? 'Show only the key reports' : `Show all ${files.length} result files (${others} more)`;
    }

    async function loadFigures() {
        const res = await tfs.api('/api/figures');
        if (res.status !== 'ok' || !res.figures.length) return;
        tfs.$('figure-grid').innerHTML = res.figures.map((f) => `
            <figure class="figure-card">
                <a href="${tfs.esc(f.url)}" target="_blank" rel="noopener">
                    <img src="${tfs.esc(f.url)}" alt="${tfs.esc(f.title)}" loading="lazy">
                </a>
                <figcaption>${tfs.esc(f.title)}</figcaption>
            </figure>`).join('');
        tfs.show('figures-section', true);
    }
})();
