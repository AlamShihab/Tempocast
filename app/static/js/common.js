/* Shared helpers for all pages (no external libraries besides the local Plotly copy). */
(function () {
    'use strict';

    const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

    const tfs = {
        /** POST (or GET when body is undefined) JSON to the API; always resolves to a parsed object. */
        async api(url, body, method) {
            const opts = { method: method || (body === undefined ? 'GET' : 'POST'), headers: {} };
            if (body instanceof FormData) {
                opts.body = body;
            } else if (body !== undefined) {
                opts.headers['Content-Type'] = 'application/json';
                opts.body = JSON.stringify(body);
            }
            let res;
            try {
                res = await fetch(url, opts);
            } catch (e) {
                return { status: 'error', message: 'The server did not respond. Is the app still running?' };
            }
            let data;
            try {
                data = await res.json();
            } catch (e) {
                data = { status: 'error', message: `Unexpected response (${res.status}).` };
            }
            if (!res.ok && data.status !== 'error') data = { status: 'error', message: `Request failed (${res.status}).` };
            return data;
        },

        fmt(v, digits = 2, suffix = '') {
            if (v === null || v === undefined || Number.isNaN(v)) return '--';
            return Number(v).toFixed(digits) + suffix;
        },

        pct(v, digits = 1) {
            return tfs.fmt(v, digits, '%');
        },

        esc(s) {
            return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
        },

        $(id) { return document.getElementById(id); },

        setText(id, text) {
            const el = document.getElementById(id);
            if (el) el.textContent = text;
        },

        show(id, visible = true) {
            const el = document.getElementById(id);
            if (el) el.style.display = visible ? '' : 'none';
        },

        /** Page-level notice under the navigation bar (list of messages; empty hides it). */
        notice(messages) {
            const el = document.getElementById('global-notice');
            if (!el) return;
            const list = (messages || []).filter(Boolean);
            if (!list.length) { el.classList.add('hidden'); return; }
            el.innerHTML = list.length === 1 ? tfs.esc(list[0])
                : '<ul>' + list.map((m) => `<li>${tfs.esc(m)}</li>`).join('') + '</ul>';
            el.classList.remove('hidden');
        },

        /** Colors from the CSS tokens, so charts follow light/dark mode. */
        theme() {
            return {
                s1: css('--series-1'), s2: css('--series-2'), s3: css('--series-3'), neutral: css('--neutral-series'),
                text: css('--text-primary'), text2: css('--text-secondary'), grid: css('--grid'),
                bg: css('--bg-secondary'), error: css('--error'), success: css('--success'),
            };
        },

        /** Base Plotly layout: recessive axes, legend on top, unified hover. */
        layout(extra = {}) {
            const t = tfs.theme();
            const axis = { gridcolor: t.grid, zerolinecolor: t.grid, linecolor: t.grid, tickcolor: t.grid,
                           color: t.text2, automargin: true, title: { font: { size: 13, color: t.text2 } } };
            const base = {
                paper_bgcolor: t.bg, plot_bgcolor: t.bg, font: { color: t.text, size: 13 },
                margin: { l: 56, r: 20, t: 20, b: 50 }, hovermode: 'x unified',
                legend: { orientation: 'h', y: 1.12, x: 0, font: { size: 12, color: t.text2 } },
                xaxis: { ...axis }, yaxis: { ...axis },
                hoverlabel: { bgcolor: t.bg, bordercolor: t.grid, font: { color: t.text } },
            };
            for (const [k, v] of Object.entries(extra)) {
                base[k] = (typeof v === 'object' && !Array.isArray(v) && base[k]) ? { ...base[k], ...v } : v;
            }
            return base;
        },

        plot(id, traces, layout) {
            const el = document.getElementById(id);
            if (!el) return;
            if (!window.Plotly) { el.innerHTML = '<div class="chart-empty">Chart library not loaded.</div>'; return; }
            Plotly.react(el, traces, layout, { responsive: true, displaylogo: false,
                                               modeBarButtonsToRemove: ['lasso2d', 'select2d'] });
        },

        emptyChart(id, message) {
            const el = document.getElementById(id);
            if (!el) return;
            if (window.Plotly) Plotly.purge(el);
            el.innerHTML = `<div class="chart-empty">${tfs.esc(message)}</div>`;
        },

        download(content, filename, type) {
            const blob = new Blob([content], { type });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = filename;
            document.body.appendChild(a);
            a.click();
            a.remove();
            setTimeout(() => URL.revokeObjectURL(url), 1000);
        },

        /** '#rrggbb' -> 'rgba(r, g, b, a)' (for translucent bands in charts). */
        alpha(hex, a) {
            const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(String(hex).trim());
            if (!m) return hex;
            return `rgba(${parseInt(m[1], 16)}, ${parseInt(m[2], 16)}, ${parseInt(m[3], 16)}, ${a})`;
        },

        /** CSV text from a header and rows (values quoted when needed; null -> empty). */
        toCsv(header, rows) {
            const q = (v) => {
                const s = v === null || v === undefined ? '' : String(v);
                return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
            };
            return [header, ...rows].map((r) => r.map(q).join(',')).join('\n') + '\n';
        },

        fillSelect(select, values, placeholder, labelFn) {
            select.innerHTML = '';
            if (placeholder) select.appendChild(new Option(placeholder, ''));
            values.forEach((v) => select.appendChild(new Option(labelFn ? labelFn(v) : v, v)));
        },
    };

    window.tfs = tfs;

    // mobile navigation toggle
    document.addEventListener('DOMContentLoaded', () => {
        const btn = document.getElementById('nav-toggle');
        const menu = document.getElementById('nav-menu');
        if (btn && menu) {
            btn.addEventListener('click', () => {
                const open = menu.classList.toggle('open');
                btn.setAttribute('aria-expanded', String(open));
            });
        }
    });

    // redraw charts in the new colors when the OS theme changes
    if (window.matchMedia) {
        window.matchMedia('(prefers-color-scheme: dark)').addEventListener?.('change', () => {
            document.dispatchEvent(new Event('tfs:theme'));
        });
    }
})();
