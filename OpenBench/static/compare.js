(() => {
    'use strict';

    const DASH = '—';
    const MINUS = '−';
    const MAX_TICKS = 6;
    const ELO_RANGE_WARMUP = 0.1;
    const BAND_ALPHA = 0.14;
    const HIT_RADIUS = 12;
    const SERIES_TOKENS = { a: '--series-1', b: '--series-2' };
    const SERIES_DASH = { a: [], b: [6, 4] };
    const DURATION_STEPS = [60, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 345600, 604800, 1209600];

    const count_format = new Intl.NumberFormat();
    const compact_format = new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 });

    const is_number = value => typeof value === 'number' && Number.isFinite(value);

    function format_count(value) {
        return is_number(value) ? count_format.format(Math.round(value)) : DASH;
    }

    function format_compact(value) {
        return is_number(value) ? compact_format.format(value) : DASH;
    }

    function format_fixed(value, digits = 2) {
        return is_number(value) ? value.toFixed(digits).replace('-', MINUS) : DASH;
    }

    function format_signed(value, digits = 2) {
        if (!is_number(value)) return DASH;
        const text = Math.abs(value).toFixed(digits);
        if (Number(text) === 0) return text;
        return (value > 0 ? '+' : MINUS) + text;
    }

    function format_duration(seconds) {
        if (!is_number(seconds)) return DASH;
        const total = Math.max(0, Math.round(seconds));
        const days = Math.floor(total / 86400);
        const hours = Math.floor((total % 86400) / 3600);
        const minutes = Math.floor((total % 3600) / 60);

        if (days) return hours ? `${days}d ${hours}h` : `${days}d`;
        if (hours) return minutes ? `${hours}h ${minutes}m` : `${hours}h`;
        if (minutes) return `${minutes}m`;
        return `${total}s`;
    }

    function css_palette() {
        const style = getComputedStyle(document.documentElement);
        const read = name => style.getPropertyValue(name).trim();
        return {
            series: { a: read(SERIES_TOKENS.a), b: read(SERIES_TOKENS.b) },
            difference: read('--series-3'),
            grid: read('--chart-grid'),
            axis: read('--chart-axis'),
            text: read('--text'),
            muted: read('--text-muted'),
            surface: read('--surface'),
            tooltip: read('--surface-3'),
            border: read('--border'),
            pass: read('--pass'),
            fail: read('--fail'),
            font: read('--font-sans'),
        };
    }

    function with_alpha(color, alpha) {
        const hex = color.replace('#', '');
        const full = hex.length === 3 ? [...hex].map(c => c + c).join('') : hex;
        if (!/^[0-9a-f]{6}$/i.test(full)) return color;
        const [r, g, b] = [0, 2, 4].map(i => parseInt(full.slice(i, i + 2), 16));
        return `rgba(${r}, ${g}, ${b}, ${alpha})`;
    }

    const reference_lines_plugin = {
        id: 'reference_lines',
        afterDatasetsDraw(chart, _args, options) {
            const { ctx, chartArea, scales } = chart;
            ctx.save();
            (options.lines || []).forEach(line => {
                const y = scales.y.getPixelForValue(line.value);
                if (y < chartArea.top || y > chartArea.bottom) return;
                ctx.strokeStyle = line.color;
                ctx.lineWidth = 1;
                ctx.setLineDash(line.dash || []);
                ctx.beginPath();
                ctx.moveTo(chartArea.left, Math.round(y) + 0.5);
                ctx.lineTo(chartArea.right, Math.round(y) + 0.5);
                ctx.stroke();
                if (!line.label) return;
                ctx.setLineDash([]);
                ctx.fillStyle = options.text_color;
                ctx.font = `500 11px ${options.font}`;
                ctx.textAlign = 'left';
                ctx.textBaseline = line.below ? 'top' : 'bottom';
                ctx.fillText(line.label, chartArea.left + 6, y + (line.below ? 3 : -3));
            });
            ctx.restore();
        },
    };

    const crosshair_plugin = {
        id: 'crosshair',
        afterDatasetsDraw(chart, _args, options) {
            const active = chart.tooltip && chart.tooltip.getActiveElements();
            if (!active || !active.length) return;
            const { ctx, chartArea } = chart;
            ctx.save();
            ctx.strokeStyle = options.color;
            ctx.lineWidth = 1;
            active.forEach(item => {
                const x = Math.round(item.element.x) + 0.5;
                ctx.beginPath();
                ctx.moveTo(x, chartArea.top);
                ctx.lineTo(x, chartArea.bottom);
                ctx.stroke();
            });
            ctx.restore();
        },
    };

    // The two workloads have points at different x values, so the built-in modes would only ever
    // pick one series: this picks the nearest point of every series whose span covers the cursor
    function nearest_per_series(chart, event) {
        const position = window.Chart.helpers.getRelativePosition(event, chart);
        if (position.x < chart.chartArea.left || position.x > chart.chartArea.right) return [];
        return chart.data.datasets.flatMap((dataset, datasetIndex) => {
            if (dataset.tooltip === false || !chart.isDatasetVisible(datasetIndex)) return [];
            const elements = chart.getDatasetMeta(datasetIndex).data;
            if (!elements.length) return [];
            if (position.x < elements[0].x - HIT_RADIUS || position.x > elements.at(-1).x + HIT_RADIUS) return [];
            let best = 0;
            elements.forEach((element, index) => {
                if (Math.abs(element.x - position.x) < Math.abs(elements[best].x - position.x)) best = index;
            });
            return [{ element: elements[best], datasetIndex, index: best }];
        });
    }

    function duration_step(span_seconds) {
        return DURATION_STEPS.find(step => span_seconds / step <= MAX_TICKS) ?? DURATION_STEPS.at(-1);
    }

    function ticks_at_multiples(step) {
        return scale => {
            const values = [];
            for (let value = Math.ceil(scale.min / step) * step; value <= scale.max; value += step) values.push(value);
            scale.ticks = values.map(value => ({ value }));
        };
    }

    function axis_options(palette, format_tick, title, step) {
        const font = { family: palette.font };
        return {
            type: 'linear',
            grid: { color: palette.grid, drawTicks: false },
            border: { color: palette.axis },
            ticks: {
                color: palette.muted, font, padding: 6, maxTicksLimit: MAX_TICKS + 1, includeBounds: false,
                callback: format_tick,
            },
            afterBuildTicks: step ? ticks_at_multiples(step) : undefined,
            title: title ? { display: true, text: title, color: palette.muted, font, padding: { top: 4 } } : { display: false },
        };
    }

    function legend_options(palette, show) {
        return {
            display: show,
            position: 'top',
            align: 'start',
            labels: {
                color: palette.muted,
                font: { family: palette.font },
                boxWidth: 16,
                boxHeight: 2,
                padding: 12,
                filter: item => !item.text.startsWith('band:'),
            },
        };
    }

    function base_options(palette, spec) {
        const reduced_motion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
        return {
            responsive: true,
            maintainAspectRatio: false,
            animation: reduced_motion || spec.quiet ? false : { duration: 300 },
            interaction: { mode: 'nearest_per_series', intersect: false },
            layout: { padding: { top: 4, right: 8 } },
            plugins: {
                legend: legend_options(palette, spec.legend !== false),
                tooltip: {
                    filter: item => item.dataset.tooltip !== false,
                    backgroundColor: palette.tooltip,
                    borderColor: palette.border,
                    borderWidth: 1,
                    titleColor: palette.text,
                    bodyColor: palette.text,
                    footerColor: palette.muted,
                    titleFont: { family: palette.font, weight: '600' },
                    bodyFont: { family: palette.font },
                    footerFont: { family: palette.font, weight: '400' },
                    padding: 8,
                    cornerRadius: 6,
                    boxWidth: 8,
                    boxHeight: 8,
                    usePointStyle: false,
                    callbacks: spec.tooltip,
                },
                reference_lines: { lines: spec.lines || [], text_color: palette.muted, font: palette.font },
                crosshair: { color: palette.axis },
            },
            scales: {
                x: { ...axis_options(palette, spec.x_tick, spec.x_title, spec.x_step), min: spec.x_min, max: spec.x_max },
                y: { ...axis_options(palette, spec.y_tick), min: spec.y_min, max: spec.y_max },
            },
        };
    }

    function line_dataset(color, points, label, dash = []) {
        return {
            label,
            data: points,
            borderColor: color,
            backgroundColor: color,
            borderWidth: 2,
            borderDash: dash,
            borderJoinStyle: 'round',
            borderCapStyle: 'round',
            pointRadius: 0,
            pointHitRadius: HIT_RADIUS,
            pointHoverRadius: 5,
            pointHoverBorderWidth: 2,
            tension: 0,
        };
    }

    function band_datasets(color, lower, upper, key) {
        const hidden = { borderWidth: 0, pointRadius: 0, pointHoverRadius: 0, pointHitRadius: 0, tooltip: false };
        return [
            { ...hidden, label: `band:${key}:lower`, data: lower, fill: false },
            { ...hidden, label: `band:${key}:upper`, data: upper, fill: '-1', backgroundColor: with_alpha(color, BAND_ALPHA) },
        ];
    }

    function history_points(workload, y_key) {
        const history = workload.history;
        return history.games.flatMap((games, index) => {
            const y = history[y_key][index];
            if (!is_number(y)) return [];
            return [{
                x: games, y, games, elapsed: history.elapsed[index],
                lower: history.elo_lower[index], upper: history.elo_upper[index],
            }];
        });
    }

    function elo_points(workload) {
        return history_points(workload, 'elo').filter(point => is_number(point.lower) && is_number(point.upper));
    }

    function fitted_range(series_list) {
        const settled = series_list.flatMap(points => {
            if (!points.length) return [];
            const last_games = points.at(-1).x;
            return points.filter(point => point.x >= ELO_RANGE_WARMUP * last_games);
        });
        const low = Math.min(0, ...settled.map(point => point.lower));
        const high = Math.max(0, ...settled.map(point => point.upper));
        const pad = 0.08 * (high - low || 1);
        return { min: low - pad, max: high + pad };
    }

    function last_value_text(workload, points, describe) {
        const last = points.at(-1);
        return last ? `${workload.label}: ${describe(last)}` : `${workload.label}: no points`;
    }

    function elo_chart(data, palette, quiet) {
        const series = data.workloads.map(workload => ({ workload, points: elo_points(workload) }));
        if (series.every(entry => entry.points.length < 2)) return { empty: 'Not enough points to draw this chart yet.' };

        const range = fitted_range(series.map(entry => entry.points));
        const x_max = Math.max(...series.map(entry => (entry.points.length ? entry.points.at(-1).x : 0)));
        const describe = point => `Elo ${format_signed(point.y)} (95% ${format_fixed(point.lower)} to ${format_fixed(point.upper)}) after ${format_count(point.x)} games`;

        return {
            label: series.map(entry => last_value_text(entry.workload, entry.points, describe)).join('; ') + '.',
            config: {
                type: 'line',
                data: {
                    datasets: series.flatMap(({ workload, points }) => {
                        const color = palette.series[workload.key];
                        return [
                            ...band_datasets(color, points.map(p => ({ x: p.x, y: p.lower })), points.map(p => ({ x: p.x, y: p.upper })), workload.key),
                            line_dataset(color, points, workload.label, SERIES_DASH[workload.key]),
                        ];
                    }),
                },
                options: base_options(palette, {
                    quiet,
                    x_tick: format_compact,
                    x_title: 'games',
                    y_tick: value => format_signed(value, Math.abs(range.max - range.min) < 10 ? 1 : 0),
                    y_min: range.min,
                    y_max: range.max,
                    x_min: 0,
                    x_max,
                    lines: [{ value: 0, color: palette.axis }],
                    tooltip: {
                        title: () => '',
                        label: item => `${item.dataset.label}: ${describe(item.raw)}`,
                    },
                }),
            },
        };
    }

    function difference_chart(data, palette, quiet) {
        const columns = data.difference;
        const points = columns.games.map((games, index) => ({
            x: games, y: columns.value[index], lower: columns.lower[index], upper: columns.upper[index],
        }));
        if (points.length < 2) return { empty: 'The two histories do not overlap in games yet.' };

        const range = fitted_range([points]);
        const last = points.at(-1);
        const describe = point => `A − B ≈ ${format_signed(point.y)} (approx. 95% ${format_fixed(point.lower)} to ${format_fixed(point.upper)}) at ${format_count(point.x)} games`;

        return {
            label: `${describe(last)}.`,
            config: {
                type: 'line',
                data: {
                    datasets: [
                        ...band_datasets(palette.difference, points.map(p => ({ x: p.x, y: p.lower })), points.map(p => ({ x: p.x, y: p.upper })), 'difference'),
                        line_dataset(palette.difference, points, 'A − B'),
                    ],
                },
                options: base_options(palette, {
                    quiet,
                    legend: false,
                    x_tick: format_compact,
                    x_title: 'games',
                    y_tick: value => format_signed(value, Math.abs(range.max - range.min) < 10 ? 1 : 0),
                    y_min: range.min,
                    y_max: range.max,
                    x_min: points[0].x,
                    x_max: last.x,
                    lines: [{ value: 0, color: palette.axis }],
                    tooltip: {
                        title: () => '',
                        label: item => describe(item.raw),
                    },
                }),
            },
        };
    }

    function bound_lines(data, palette) {
        const lines = [];
        const seen = new Set();
        data.workloads.forEach(workload => {
            [
                { value: workload.llr_upper, color: palette.pass, kind: 'pass', below: true },
                { value: workload.llr_lower, color: palette.fail, kind: 'fail', below: false },
            ].forEach(bound => {
                if (!is_number(bound.value) || seen.has(`${bound.kind}:${bound.value}`)) return;
                seen.add(`${bound.kind}:${bound.value}`);
                lines.push({ ...bound, label: `${bound.kind} ${format_fixed(bound.value)}` });
            });
        });
        return [...lines, { value: 0, color: palette.axis, dash: [2, 3] }];
    }

    function llr_chart(data, palette, quiet) {
        const series = data.workloads.map(workload => ({ workload, points: history_points(workload, 'llr') }));
        if (series.every(entry => entry.points.length < 2)) return { empty: 'Not enough points to draw this chart yet.' };

        const lines = bound_lines(data, palette);
        const values = [...series.flatMap(entry => entry.points.map(point => point.y)), ...lines.map(line => line.value)];
        const low = Math.min(...values);
        const high = Math.max(...values);
        const pad = 0.08 * (high - low || 1);
        const describe = point => `LLR ${format_fixed(point.y)} after ${format_count(point.x)} games`;

        return {
            label: series.map(entry => last_value_text(entry.workload, entry.points, describe)).join('; ') + '.',
            config: {
                type: 'line',
                data: {
                    datasets: series.map(({ workload, points }) => line_dataset(palette.series[workload.key], points, workload.label, SERIES_DASH[workload.key])),
                },
                options: base_options(palette, {
                    quiet,
                    x_tick: format_compact,
                    x_title: 'games',
                    y_tick: value => format_fixed(value, 1),
                    y_min: low - pad,
                    y_max: high + pad,
                    x_min: 0,
                    x_max: Math.max(...series.map(entry => (entry.points.length ? entry.points.at(-1).x : 0))),
                    lines,
                    tooltip: {
                        title: () => '',
                        label: item => `${item.dataset.label}: ${describe(item.raw)}`,
                    },
                }),
            },
        };
    }

    function games_chart(data, palette, quiet) {
        const series = data.workloads.map(workload => ({
            workload,
            points: workload.history.games.map((games, index) => ({ x: workload.history.elapsed[index], y: games })),
        }));
        if (series.every(entry => entry.points.length < 2)) return { empty: 'Not enough points to draw this chart yet.' };

        const x_max = Math.max(...series.map(entry => (entry.points.length ? entry.points.at(-1).x : 0)));
        const y_max = Math.max(...series.flatMap(entry => entry.points.map(point => point.y)));
        const describe = point => `${format_count(point.y)} games after ${format_duration(point.x)}`;

        return {
            label: series.map(entry => last_value_text(entry.workload, entry.points, describe)).join('; ') + '.',
            config: {
                type: 'line',
                data: {
                    datasets: series.map(({ workload, points }) => line_dataset(palette.series[workload.key], points, workload.label, SERIES_DASH[workload.key])),
                },
                options: base_options(palette, {
                    quiet,
                    x_tick: value => format_duration(value),
                    x_title: 'elapsed',
                    x_step: x_max > 0 ? duration_step(x_max) : undefined,
                    y_tick: format_compact,
                    x_min: 0,
                    x_max: x_max > 0 ? x_max : undefined,
                    y_min: 0,
                    y_max: y_max > 0 ? y_max * 1.06 : undefined,
                    tooltip: {
                        title: () => '',
                        label: item => `${item.dataset.label}: ${describe(item.raw)}`,
                    },
                }),
            },
        };
    }

    const BUILDERS = { elo: elo_chart, difference: difference_chart, llr: llr_chart, games: games_chart };

    class ChartPanel {
        constructor(box) {
            this.box = box;
            this.canvas = box.querySelector('canvas');
            this.empty = box.parentElement.querySelector('.chart-empty');
            this.build = BUILDERS[box.dataset.compareChart];
            this.title = box.parentElement.querySelector('.card-title').textContent;
            this.chart = null;
        }

        show_empty(message) {
            if (this.chart) this.chart.destroy();
            this.chart = null;
            this.box.hidden = true;
            this.empty.hidden = false;
            this.empty.textContent = message;
        }

        render(data, palette, quiet) {
            if (typeof window.Chart === 'undefined') return this.show_empty('The charting library failed to load.');

            const built = this.build(data, palette, quiet);
            if (!built.config) return this.show_empty(built.empty);

            this.box.hidden = false;
            this.empty.hidden = true;
            this.canvas.setAttribute('aria-label', `${this.title} chart. ${built.label}`);
            if (this.chart) this.chart.destroy();
            this.chart = new window.Chart(this.canvas, { ...built.config, plugins: [reference_lines_plugin, crosshair_plugin] });
        }
    }

    function read_data() {
        const island = document.getElementById('compare-data');
        return island ? JSON.parse(island.textContent) : null;
    }

    function watch_theme(on_change) {
        new MutationObserver(on_change).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
        window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', on_change);
    }

    function init(root) {
        const data = read_data();
        if (!data) return;
        if (typeof window.Chart !== 'undefined') window.Chart.Interaction.modes.nearest_per_series = nearest_per_series;

        const panels = [...root.querySelectorAll('[data-compare-chart]')].map(box => new ChartPanel(box));
        const render = quiet => {
            const palette = css_palette();
            panels.forEach(panel => panel.render(data, palette, quiet));
        };
        render(false);
        watch_theme(() => render(true));
    }

    document.addEventListener('DOMContentLoaded', () => {
        const root = document.querySelector('[data-compare]');
        if (root) init(root);
    });

})();
