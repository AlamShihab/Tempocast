/* Forecast page: station + horizon -> prediction, or all horizons (outlook); history chart; downloads.
   "All stations" runs the same forecast for every station (table + CSV). */
(function () {
    'use strict';
    let horizons = [1, 2, 3, 5, 7, 10, 13, 15];
    let stationCount = 0;
    let last = null;          // last single-horizon forecast
    let lastOutlook = null;   // last all-horizons outlook
    let lastAll = null;       // last all-stations forecast (one or every horizon)
    const OUTLOOK_LABEL = 'Show all horizons (15-day outlook)';
    const ALL = '__all__';    // value of the "All stations" option
    const SECTIONS = ['results-section', 'outlook-section', 'all-section', 'error-section'];

    document.addEventListener('DOMContentLoaded', () => {
        loadStations();
        tfs.$('horizon-slider').addEventListener('input', updateHorizonLabel);
        tfs.$('generate-btn').addEventListener('click', generateForecast);
        tfs.$('outlook-btn').addEventListener('click', showOutlook);
        tfs.$('download-json-btn').addEventListener('click', () => downloadResults('json'));
        tfs.$('download-csv-btn').addEventListener('click', () => downloadResults('csv'));
        tfs.$('outlook-json-btn').addEventListener('click', () => downloadOutlook('json'));
        tfs.$('outlook-csv-btn').addEventListener('click', () => downloadOutlook('csv'));
        tfs.$('all-csv-btn').addEventListener('click', downloadAll);
        document.addEventListener('tfs:theme', () => {
            if (last) drawChart(last);
            if (lastOutlook) drawOutlookChart(lastOutlook);
        });
    });

    async function loadStations() {
        const data = await tfs.api('/api/dashboard-data', {});
        const select = tfs.$('station-select');
        if (data.status !== 'ok') {
            tfs.fillSelect(select, [], '-- stations unavailable --');
            tfs.notice([data.message]);
            return;
        }
        tfs.notice(data.messages);
        if (data.horizons && data.horizons.length) horizons = data.horizons;
        setupSlider();
        tfs.fillSelect(select, data.stations, '-- Select a station --');
        stationCount = data.stations.length;
        if (stationCount) select.add(new Option(`All stations (${stationCount})`, ALL), 1);
        if (data.stations.includes('DHAKA')) select.value = 'DHAKA';
        tfs.$('generate-btn').disabled = !data.pipeline_ready;
        tfs.$('outlook-btn').disabled = !data.pipeline_ready;

        // deep links: /forecast?station=DHAKA&horizon=3 (one horizon) or /forecast?station=DHAKA&outlook=1;
        // station=ALL for every station
        const q = new URLSearchParams(window.location.search);
        const st = (q.get('station') || '').toUpperCase();
        const h = Number(q.get('horizon'));
        if (st && data.stations.includes(st)) select.value = st;
        else if (st === 'ALL' && stationCount) select.value = ALL;
        if (horizons.includes(h)) { tfs.$('horizon-slider').value = horizons.indexOf(h); updateHorizonLabel(); }
        if (st && data.pipeline_ready) {
            if (q.get('outlook')) showOutlook();
            else generateForecast();
        }
    }

    function setupSlider() {
        const slider = tfs.$('horizon-slider');
        slider.min = 0;
        slider.max = horizons.length - 1;
        const i3 = horizons.indexOf(3);
        slider.value = i3 >= 0 ? i3 : 0;
        tfs.$('slider-ticks').innerHTML = horizons.map((h) => `<span>${h}</span>`).join('');
        tfs.setText('horizon-list', horizons.join(', '));
        updateHorizonLabel();
    }

    function currentHorizon() {
        return horizons[Number(tfs.$('horizon-slider').value)] ?? horizons[0];
    }

    function updateHorizonLabel() {
        const h = currentHorizon();
        tfs.setText('horizon-value', `${h} day${h === 1 ? '' : 's'}`);
    }

    const days = (h) => `${h} day${h === 1 ? '' : 's'}`;
    const modelLabel = (d) => (d.model_kind === 'baseline' ? `${d.model_name} (baseline)` : d.model_name);
    const rangeText = (r) => (r ? `${tfs.fmt(r[0], 1)} – ${tfs.fmt(r[1], 1)}` : '--');

    async function generateForecast() {
        const station = tfs.$('station-select').value;
        if (!station) return showError('Please select a station.');
        if (station === ALL) return forecastAll(tfs.$('generate-btn'), currentHorizon());
        const btn = tfs.$('generate-btn');
        btn.disabled = true;
        btn.textContent = 'Generating...';
        const data = await tfs.api('/api/forecast', { station, horizon: currentHorizon() });
        btn.disabled = false;
        btn.textContent = 'Generate Forecast';
        if (data.status === 'success') displayResults(data);
        else showError(data.message || 'Error generating forecast. Please try again.');
    }

    async function showOutlook() {
        const station = tfs.$('station-select').value;
        if (!station) return showError('Please select a station.');
        if (station === ALL) return forecastAll(tfs.$('outlook-btn'), null);
        const btn = tfs.$('outlook-btn');
        btn.disabled = true;
        btn.textContent = 'Loading outlook...';
        const data = await tfs.api('/api/outlook', { station });
        btn.disabled = false;
        btn.textContent = OUTLOOK_LABEL;
        if (data.status === 'success') displayOutlook(data);
        else showError(data.message || 'Error generating the outlook. Please try again.');
    }

    /* Every station at one horizon, or at every horizon when horizon is null (takes a few seconds). */
    async function forecastAll(btn, horizon) {
        const buttons = [tfs.$('generate-btn'), tfs.$('outlook-btn')];
        const label = btn.textContent;
        buttons.forEach((b) => { b.disabled = true; });
        btn.textContent = `Forecasting ${stationCount} stations...`;
        const data = await tfs.api('/api/forecast-all', horizon == null ? {} : { horizon });
        buttons.forEach((b) => { b.disabled = false; });
        btn.textContent = label;
        if (data.status === 'success') displayAll(data);
        else showError(data.message || 'Error generating the forecasts. Please try again.');
    }

    function showOnly(id) {
        SECTIONS.forEach((s) => tfs.show(s, s === id));
    }

    const FALLBACK_NOTE = 'This station uses the all-station climatology (little early history).';

    function notesFor(d) {
        const notes = [...(d.warnings || [])];
        if (d.input_quality && d.input_quality.climatology_fallback) notes.push(FALLBACK_NOTE);
        return notes;
    }

    function warningItems(notes) {
        return notes.map((x) => `<div class="warning-item">⚠️ ${tfs.esc(x)}</div>`).join('');
    }

    function displayResults(d) {
        last = d;
        showOnly('results-section');
        tfs.setText('result-station', d.station);
        tfs.setText('result-date', d.forecast_date);
        tfs.setText('result-horizon', days(d.horizon));
        tfs.setText('result-model', modelLabel(d));
        tfs.setText('prediction-temp', tfs.fmt(d.predicted_max_temp, 1));
        tfs.setText('confidence-range', rangeText(d.confidence_range));
        tfs.setText('climatology', tfs.fmt(d.climatology_temp, 1));
        tfs.setText('origin-date', d.origin_date);
        tfs.setText('forecast-confidence', d.forecast_confidence || '--');
        tfs.setText('result-rmse', tfs.fmt(d.model_rmse, 2, ' °C'));
        tfs.setText('result-baseline', d.baseline_rmse == null ? '--' : `${tfs.fmt(d.baseline_rmse, 2, ' °C')} (${d.baseline_name})`);
        tfs.setText('result-improvement', d.improvement_percent == null ? '--' : tfs.fmt(d.improvement_percent, 1, '%'));
        tfs.$('warnings').innerHTML = warningItems(notesFor(d));
        drawChart(d);
        tfs.$('results-section').scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    function displayOutlook(d) {
        lastOutlook = d;
        showOnly('outlook-section');
        tfs.setText('outlook-station', d.station);
        tfs.setText('outlook-origin', d.origin_date);
        tfs.$('outlook-warnings').innerHTML = warningItems(notesFor(d));
        tfs.$('outlook-body').innerHTML = d.forecasts.map((f) => {
            const imp = f.improvement_percent;
            const impCell = imp == null ? '--'
                : `<span class="badge ${imp > 0 ? 'success' : 'error'}">${imp > 0 ? '+' : ''}${tfs.fmt(imp, 1)}%</span>`;
            return `<tr>
                <td>${days(f.horizon)}</td>
                <td>${tfs.esc(f.forecast_date)}</td>
                <td class="num"><strong>${tfs.fmt(f.predicted_max_temp, 1)}</strong></td>
                <td class="num">${rangeText(f.confidence_range)}</td>
                <td class="num">${tfs.fmt(f.climatology_temp, 1)}</td>
                <td>${tfs.esc(modelLabel(f))}</td>
                <td class="num">${tfs.fmt(f.model_rmse, 2)}</td>
                <td class="num">${impCell}</td>
            </tr>`;
        }).join('');
        drawOutlookChart(d);
        tfs.$('outlook-section').scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    /* All stations: notes that apply to every station are shown once above the table, the rest per row.
       One horizon -> detail columns; every horizon -> one forecast column per horizon. */
    function displayAll(d) {
        lastAll = d;
        showOnly('all-section');
        const rows = d.stations;
        const one = d.horizons.length === 1;
        const notes = rows.map(notesFor);
        const shared = notes.length ? notes[0].filter((n) => notes.every((list) => list.includes(n))) : [];
        const own = notes.map((list) => list.filter((n) => !shared.includes(n)));
        const anyOwn = own.some((list) => list.length);
        const origins = [...new Set(rows.map((r) => r.origin_date))].sort();
        const issued = origins.length === 1 ? `data up to ${origins[0]}`
            : `each station's latest data (${origins[0]} to ${origins[origins.length - 1]})`;

        const skipped = (d.skipped || []).map((s) => `Not forecast: ${s.station} – ${s.message}`);
        tfs.$('all-warnings').innerHTML = warningItems([...skipped, ...shared]);

        const first = rows.length ? rows[0].forecasts[0] : null;
        if (one) {
            tfs.setText('all-title', `All Stations · ${days(d.horizons[0])} ahead`);
            tfs.setText('all-note', `${rows.length} stations, issued from ${issued}.`
                + (first ? ` Model: ${modelLabel(first)} (test RMSE ${tfs.fmt(first.model_rmse, 2)} °C).` : '')
                + ' The 80% range is ± 1.28 × the test RMSE; the normal is the usual maximum for the date.');
        } else {
            tfs.setText('all-title', 'All Stations · 15-Day Outlook');
            tfs.setText('all-note', `Forecast maximum temperature (°C) for ${rows.length} stations at every horizon, `
                + `issued from ${issued}. Hover a value for its date and 80% range; the CSV also has the normal and the model.`);
        }

        // "Data up to" only when the stations' data ends differ (otherwise it is in the note above)
        const perStationOrigin = origins.length > 1;
        const th = (label, num) => `<th${num ? ' class="num"' : ''}>${tfs.esc(label)}</th>`;
        tfs.$('all-head').innerHTML = '<tr>' + [
            th('Station'),
            perStationOrigin ? th('Data up to') : '',
            ...(one ? [th('Forecast date'), th('Forecast (°C)', true), th('80% range (°C)', true), th('Normal (°C)', true)]
                    : d.horizons.map((h) => th(days(h), true))),
            th('Confidence'),
            anyOwn ? th('Notes') : '',
        ].join('') + '</tr>';

        tfs.$('all-body').innerHTML = rows.map((r, i) => {
            const f0 = r.forecasts[0];
            const cells = one ? [
                `<td>${tfs.esc(f0.forecast_date)}</td>`,
                `<td class="num"><strong>${tfs.fmt(f0.predicted_max_temp, 1)}</strong></td>`,
                `<td class="num">${rangeText(f0.confidence_range)}</td>`,
                `<td class="num">${tfs.fmt(f0.climatology_temp, 1)}</td>`,
            ] : r.forecasts.map((f) => `<td class="num" title="${tfs.esc(`${f.forecast_date} · 80%: ${rangeText(f.confidence_range)} °C`)}">`
                                         + `${tfs.fmt(f.predicted_max_temp, 1)}</td>`);
            return `<tr>
                <td><strong>${tfs.esc(r.station)}</strong></td>
                ${perStationOrigin ? `<td>${tfs.esc(r.origin_date)}</td>` : ''}
                ${cells.join('')}
                <td>${tfs.esc(r.forecast_confidence || '--')}</td>
                ${anyOwn ? `<td class="muted-cell notes-cell">${own[i].map(tfs.esc).join('<br>')}</td>` : ''}
            </tr>`;
        }).join('');
        tfs.$('all-section').scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    /* Recent history: grey line, observed values as filled dots, filled values as hollow dots. */
    function historyTraces(hist, t) {
        const obs = hist.filter((p) => p.observed);
        const fil = hist.filter((p) => p.filled);
        const traces = [
            { x: hist.map((p) => p.date), y: hist.map((p) => p.value), type: 'scatter', mode: 'lines',
              line: { color: t.neutral, width: 1.5 }, name: 'History', hoverinfo: 'skip', showlegend: false },
        ];
        if (obs.length) {
            traces.push({ x: obs.map((p) => p.date), y: obs.map((p) => p.value), type: 'scatter', mode: 'markers',
                          marker: { color: t.s1, size: 7, line: { color: t.bg, width: 1 } }, name: 'Observed (BMD)',
                          hovertemplate: '%{y:.1f} °C<extra>Observed</extra>' });
        }
        if (fil.length) {
            traces.push({ x: fil.map((p) => p.date), y: fil.map((p) => p.value), type: 'scatter', mode: 'markers',
                          marker: { color: t.bg, size: 7, line: { color: t.neutral, width: 2 } }, name: 'Filled value',
                          hovertemplate: '%{y:.1f} °C<extra>Filled</extra>' });
        }
        return traces;
    }

    function chartLayout(origin, t) {
        return tfs.layout({
            yaxis: { title: { text: 'Max temperature (°C)' } },
            xaxis: { type: 'date' },
            shapes: [{ type: 'line', xref: 'x', yref: 'paper', x0: origin, x1: origin, y0: 0, y1: 1,
                       line: { color: t.text2, width: 1, dash: 'dot' } }],
            annotations: [{ x: origin, y: 1, xref: 'x', yref: 'paper', text: 'data end', showarrow: false,
                            yanchor: 'bottom', font: { size: 11, color: t.text2 } }],
        });
    }

    function drawChart(d) {
        const t = tfs.theme();
        const traces = historyTraces(d.history || [], t);
        const r = d.confidence_range;
        traces.push({
            x: [d.forecast_date], y: [d.predicted_max_temp], type: 'scatter', mode: 'markers', name: 'Forecast',
            marker: { color: t.s2, size: 13, symbol: 'diamond', line: { color: t.bg, width: 2 } },
            error_y: r ? { type: 'data', symmetric: false, array: [r[1] - d.predicted_max_temp],
                           arrayminus: [d.predicted_max_temp - r[0]], color: t.s2, thickness: 2, width: 8 } : undefined,
            hovertemplate: '%{y:.1f} °C<extra>Forecast (80% range)</extra>',
        });
        if (d.climatology_temp != null) {
            traces.push({ x: [d.forecast_date], y: [d.climatology_temp], type: 'scatter', mode: 'markers',
                          name: 'Normal for date', marker: { color: t.s3, size: 10, symbol: 'line-ew-open', line: { width: 3, color: t.s3 } },
                          hovertemplate: '%{y:.1f} °C<extra>Normal</extra>' });
        }
        tfs.plot('forecast-chart', traces, chartLayout(d.origin_date, t));
    }

    function drawOutlookChart(d) {
        const t = tfs.theme();
        const f = d.forecasts;
        const x = f.map((r) => r.forecast_date);
        const lo = f.map((r) => (r.confidence_range ? r.confidence_range[0] : null));
        const hi = f.map((r) => (r.confidence_range ? r.confidence_range[1] : null));
        const traces = historyTraces((d.history || []).slice(-30), t);
        traces.push(
            { x, y: lo, type: 'scatter', mode: 'lines', line: { width: 0, color: t.s2 }, hoverinfo: 'skip', showlegend: false },
            { x, y: hi, type: 'scatter', mode: 'lines', line: { width: 0, color: t.s2 }, fill: 'tonexty',
              fillcolor: tfs.alpha(t.s2, 0.18), name: '80% range', hoverinfo: 'skip' },
            { x, y: f.map((r) => r.climatology_temp), type: 'scatter', mode: 'lines', name: 'Normal for date',
              line: { color: t.s3, width: 2, dash: 'dash' }, hovertemplate: '%{y:.1f} °C<extra>Normal</extra>' },
            { x, y: f.map((r) => r.predicted_max_temp), type: 'scatter', mode: 'lines+markers', name: 'Forecast',
              line: { color: t.s2, width: 2 }, marker: { color: t.s2, size: 10, symbol: 'diamond', line: { color: t.bg, width: 2 } },
              customdata: f.map((r) => [days(r.horizon), rangeText(r.confidence_range), r.model_name]),
              hovertemplate: '%{y:.1f} °C · 80%: %{customdata[1]} · %{customdata[0]} ahead · %{customdata[2]}<extra>Forecast</extra>' },
        );
        tfs.plot('outlook-chart', traces, chartLayout(d.origin_date, t));
    }

    function showError(message) {
        showOnly('error-section');
        tfs.setText('error-message', message);
    }

    const fileBase = (station) => `forecast_${station.replace(/[^A-Za-z0-9]+/g, '_')}`;

    function downloadResults(format) {
        if (!last) return;
        const d = last;
        const name = `${fileBase(d.station)}_${d.horizon}d`;
        if (format === 'json') {
            const out = {
                station: d.station, horizon_days: d.horizon, origin_date: d.origin_date, forecast_date: d.forecast_date,
                predicted_max_temp_c: d.predicted_max_temp, range_80_c: d.confidence_range, climatology_c: d.climatology_temp,
                model: d.model_name, model_kind: d.model_kind, model_test_rmse_c: d.model_rmse,
                baseline: d.baseline_name, baseline_test_rmse_c: d.baseline_rmse, forecast_confidence: d.forecast_confidence,
                warnings: d.warnings,
            };
            tfs.download(JSON.stringify(out, null, 2), `${name}.json`, 'application/json');
        } else {
            tfs.download(tfs.toCsv(CSV_HEADER, [csvRow(d.station, d.origin_date, d)]), `${name}.csv`, 'text/csv');
        }
    }

    function downloadOutlook(format) {
        if (!lastOutlook) return;
        const d = lastOutlook;
        const name = `${fileBase(d.station)}_outlook`;
        if (format === 'json') {
            const out = {
                station: d.station, origin_date: d.origin_date, data_age_days: d.data_age_days,
                forecast_confidence: d.forecast_confidence, warnings: d.warnings,
                forecasts: d.forecasts.map((f) => ({
                    horizon_days: f.horizon, forecast_date: f.forecast_date, predicted_max_temp_c: f.predicted_max_temp,
                    range_80_c: f.confidence_range, climatology_c: f.climatology_temp, model: f.model_name,
                    model_kind: f.model_kind, model_test_rmse_c: f.model_rmse, baseline: f.baseline_name,
                    baseline_test_rmse_c: f.baseline_rmse,
                })),
            };
            tfs.download(JSON.stringify(out, null, 2), `${name}.json`, 'application/json');
        } else {
            tfs.download(tfs.toCsv(CSV_HEADER, d.forecasts.map((f) => csvRow(d.station, d.origin_date, f))),
                         `${name}.csv`, 'text/csv');
        }
    }

    /* One row per station and horizon; every row carries its own notes. Skipped stations get an empty row. */
    function downloadAll() {
        if (!lastAll) return;
        const d = lastAll;
        const rows = [];
        d.stations.forEach((s) => {
            const notes = notesFor(s).join(' | ');
            s.forecasts.forEach((f) => rows.push([...csvRow(s.station, s.origin_date, f), s.forecast_confidence, notes]));
        });
        (d.skipped || []).forEach((s) => rows.push([s.station, ...Array(CSV_HEADER.length).fill(''), `Not forecast: ${s.message}`]));
        rows.sort((a, b) => String(a[0]).localeCompare(String(b[0])));   // stable: horizons stay in order
        const origins = new Set(d.stations.map((s) => s.origin_date));
        const name = `forecast_all_stations_${d.horizons.length === 1 ? `${d.horizons[0]}d` : 'outlook'}`
                     + (origins.size === 1 ? `_from_${[...origins][0]}` : '');
        tfs.download(tfs.toCsv([...CSV_HEADER, 'forecast_confidence', 'notes'], rows), `${name}.csv`, 'text/csv');
    }

    const CSV_HEADER = ['station', 'origin_date', 'horizon_days', 'forecast_date', 'predicted_max_temp_c', 'lower_80_c',
                        'upper_80_c', 'climatology_c', 'model', 'model_test_rmse_c', 'baseline', 'baseline_test_rmse_c'];

    function csvRow(station, origin, f) {
        const r = f.confidence_range;
        return [station, origin, f.horizon, f.forecast_date, f.predicted_max_temp, r ? r[0] : '', r ? r[1] : '',
                f.climatology_temp, f.model_name, f.model_rmse, f.baseline_name, f.baseline_rmse];
    }
})();
