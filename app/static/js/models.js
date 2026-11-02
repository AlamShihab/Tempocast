/* Model performance page: per-station metrics by horizon, importance, residuals, model info. */
(function () {
    'use strict';
    let last = null;

    document.addEventListener('DOMContentLoaded', () => {
        loadStations();
        tfs.$('station-select').addEventListener('change', reload);
        tfs.$('horizon-select').addEventListener('change', reload);
        document.addEventListener('tfs:theme', () => last && createPerformanceCharts(last));
    });

    async function loadStations() {
        const [st, dash] = await Promise.all([tfs.api('/api/stations?kind=evaluated'), tfs.api('/api/dashboard-data', {})]);
        const select = tfs.$('station-select');
        if (dash.status === 'ok') {
            tfs.fillSelect(tfs.$('horizon-select'), dash.horizons, null, (h) => `${h} day${h > 1 ? 's' : ''}`);
        }
        if (st.status !== 'ok' || !st.stations.length) {
            tfs.fillSelect(select, [], '-- no evaluated stations yet --');
            tfs.notice(['Station metrics appear after the final evaluation (Day 9) has run.']);
            return;
        }
        tfs.fillSelect(select, st.stations, '-- Select a station --');
        if (st.stations.includes('DHAKA')) {
            select.value = 'DHAKA';
            reload();
        }
    }

    function reload() {
        const station = tfs.$('station-select').value;
        if (station) loadModelMetrics(station, Number(tfs.$('horizon-select').value) || undefined);
    }

    async function loadModelMetrics(station, horizon) {
        tfs.show('loading', true);
        tfs.show('error-message', false);
        const data = await tfs.api('/api/model-metrics', { station, horizon });
        tfs.show('loading', false);
        if (data.status !== 'ok') {
            ['metrics-section', 'charts-section', 'model-info-section'].forEach((id) => tfs.show(id, false));
            tfs.show('error-message', true);
            tfs.$('error-message').querySelector('p').textContent = data.message || 'Error loading model metrics.';
            return;
        }
        last = data;
        ['metrics-section', 'charts-section', 'model-info-section'].forEach((id) => tfs.show(id, true));
        tfs.show('late-note', !!data.late_start_station);
        populateMetricsTable(data.metrics_by_horizon);
        createPerformanceCharts(data);
        populateModelInfo(data.model_info, data.selected_horizon);
    }

    function populateMetricsTable(metrics) {
        tfs.$('metrics-body').innerHTML = metrics.map((r) => {
            const skill = r.skill_vs_climatology;
            const cls = skill == null ? '' : Math.abs(skill) < 0.0005 ? 'pending' : skill > 0 ? 'success' : 'error';
            const skillCell = skill == null ? '--'
                : `<span class="badge ${cls}">${skill >= 0.0005 ? '+' : ''}${tfs.fmt(100 * skill, 1)}%</span>`;
            return `<tr>
                <td>${r.horizon} day${r.horizon > 1 ? 's' : ''}</td>
                <td>${tfs.esc(r.production_model || '--')}</td>
                <td class="num">${tfs.fmt(r.rmse, 3)}</td>
                <td class="num">${tfs.fmt(r.mae, 3)}</td>
                <td class="num">${tfs.fmt(r.r_squared, 3)}</td>
                <td class="num">${r.bias == null ? '--' : (r.bias > 0 ? '+' : '') + tfs.fmt(r.bias, 2)}</td>
                <td class="num">${skillCell}</td>
                <td class="num">${r.n ?? '--'}</td>
            </tr>`;
        }).join('');
    }

    function createPerformanceCharts(data) {
        const t = tfs.theme();
        const m = data.metrics_by_horizon;
        const x = m.map((r) => r.horizon);
        const traces = [
            { x, y: m.map((r) => r.rmse), name: data.station, type: 'scatter', mode: 'lines+markers',
              line: { color: t.s1, width: 2 }, marker: { size: 8, color: t.s1 },
              hovertemplate: '%{y:.3f} °C<extra>' + tfs.esc(data.station) + '</extra>' },
        ];
        if (m.some((r) => r.all_stations_rmse != null)) {
            traces.push({ x, y: m.map((r) => r.all_stations_rmse), name: 'All stations', type: 'scatter', mode: 'lines+markers',
                          line: { color: t.neutral, width: 2, dash: 'dot' }, marker: { size: 7, color: t.neutral },
                          hovertemplate: '%{y:.3f} °C<extra>All stations</extra>' });
        }
        if (m.some((r) => r.climatology_rmse != null)) {
            traces.push({ x, y: m.map((r) => r.climatology_rmse), name: 'Climatology at station', type: 'scatter',
                          mode: 'lines+markers', line: { color: t.s3, width: 2, dash: 'dash' }, marker: { size: 7, color: t.s3 },
                          hovertemplate: '%{y:.3f} °C<extra>Climatology</extra>' });
        }
        tfs.plot('rmse-chart', traces, tfs.layout({
            xaxis: { title: { text: 'Forecast horizon (days)' }, tickvals: x },
            yaxis: { title: { text: 'Test RMSE (°C)' }, rangemode: 'tozero' },
        }));

        const fi = data.feature_importance || [];
        tfs.setText('importance-note', `${data.feature_importance_note || ''} Horizon: ${data.selected_horizon} day${data.selected_horizon === 1 ? '' : 's'}.`);
        if (fi.length) {
            const rows = [...fi].reverse();
            tfs.plot('feature-chart', [{
                x: rows.map((r) => r.importance), y: rows.map((r) => r.label || r.feature), type: 'bar', orientation: 'h',
                marker: { color: t.s1 }, customdata: rows.map((r) => r.feature),
                hovertemplate: '+%{x:.3f} °C RMSE<extra>%{y} (%{customdata})</extra>',
            }], tfs.layout({
                hovermode: 'closest', margin: { l: 20, r: 20, t: 10, b: 50 },
                xaxis: { title: { text: 'RMSE increase when shuffled (°C)' } }, showlegend: false,
            }));
        } else {
            tfs.emptyChart('feature-chart', 'Feature importance not available for this horizon.');
        }

        const res = data.residuals || [];
        if (res.length) {
            const mean = res.reduce((a, b) => a + b, 0) / res.length;
            tfs.plot('residuals-chart', [{
                x: res, type: 'histogram', xbins: { size: 0.5 }, marker: { color: t.s1, line: { color: t.bg, width: 1 } },
                hovertemplate: '%{x} °C: %{y} days<extra></extra>', name: 'Errors',
            }], tfs.layout({
                hovermode: 'closest', bargap: 0.05, showlegend: false,
                xaxis: { title: { text: `Forecast − observed (°C) · h = ${data.selected_horizon} d · mean ${tfs.fmt(mean, 2)} °C` }, zeroline: true },
                yaxis: { title: { text: 'Days' } },
                shapes: [{ type: 'line', x0: 0, x1: 0, yref: 'paper', y0: 0, y1: 1, line: { color: t.text2, width: 1 } }],
            }));
        } else {
            tfs.emptyChart('residuals-chart', 'Test predictions not available for this station and horizon.');
        }
    }

    function populateModelInfo(info, horizon) {
        const kind = info.kind === 'baseline' ? ' (baseline rule)' : '';
        tfs.setText('model-type', info.model_type ? `${info.model_type}${kind} · ${horizon}-day horizon` : '--');
        tfs.setText('model-profile', info.profile || '--');
        tfs.setText('training-period', info.training_period || '--');
        tfs.setText('test-period', info.test_period || '--');
        tfs.setText('num-features', info.num_features ?? '--');
        tfs.setText('num-samples', info.training_samples ? Number(info.training_samples).toLocaleString() : '--');
    }
})();
