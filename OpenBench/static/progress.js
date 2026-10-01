(() => {
    'use strict';

    const DASH = '—';
    const MINUS = '−';
    const MAX_TICKS = 6;
    const BAR_THICKNESS = 24;
    const SHORT_SHA = 8;
    const CLASS_LABELS = { stc: 'STC', ltc: 'LTC', vltc: 'VLTC', smp: 'SMP' };

    const count_format = new Intl.NumberFormat();
    const compact_format = new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 });
    const day_format = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' });
    const long_day_format = new Intl.DateTimeFormat(undefined, {
        weekday: 'short', month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC',
    });

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

    function format_signed(value, digits = 1) {
        if (!is_number(value)) return DASH;
        const text = Math.abs(value).toFixed(digits);
        if (Number(text) === 0) return text;
        return (value > 0 ? '+' : MINUS) + text;
    }

    function format_percent(fraction) {
        return is_number(fraction) ? `${Math.round(100 * fraction)}%` : DASH;
    }

    const half_width = interval => (interval.upper - interval.lower) / 2;

    const plural = (value, noun) => `${value} ${noun}${value === 1 ? '' : 's'}`;

    function format_interval(interval) {
        if (!interval || ![interval.lower, interval.value, interval.upper].every(is_number)) return DASH;
        return `${format_signed(interval.value, 2)} ± ${format_fixed(half_width(interval))}`;
    }

    const day_ms = iso => Date.parse(`${iso}T00:00:00Z`);

    function css_palette() {
        const style = getComputedStyle(document.documentElement);
        const read = name => style.getPropertyValue(name).trim();
        return {
            series: read('--series-1'),
            classes: { stc: read('--series-1'), ltc: read('--series-2'), vltc: read('--series-3'), smp: read('--series-4') },
            grid: read('--chart-grid'),
            axis: read('--chart-axis'),
            text: read('--text'),
            muted: read('--text-muted'),
            surface: read('--surface'),
            tooltip: read('--surface-3'),
            border: read('--border'),
            pass: read('--pass'),
            fail: read('--fail'),
            stopped: read('--neutral-edge'),
            font: read('--font-sans'),
        };
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
                ctx.beginPath();
                ctx.moveTo(chartArea.left, Math.round(y) + 0.5);
                ctx.lineTo(chartArea.right, Math.round(y) + 0.5);
                ctx.stroke();
            });
            ctx.restore();
        },
    };

    function axis(palette, extra) {
        const font = { family: palette.font };
        return {
            grid: { color: palette.grid, drawTicks: false },
            border: { color: palette.axis },
            ...extra,
            ticks: { color: palette.muted, font, padding: 6, includeBounds: false, ...extra.ticks },
        };
    }

    function tooltip_options(palette, callbacks, filter) {
        return {
            filter,
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
            callbacks,
        };
    }

    function base_options(palette, spec) {
        const reduced_motion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
        return {
            responsive: true,
            maintainAspectRatio: false,
            animation: reduced_motion || spec.quiet ? false : { duration: 300 },
            interaction: spec.interaction,
            layout: { padding: { top: 4, right: 8 } },
            plugins: {
                legend: spec.legend ?? { display: false },
                tooltip: tooltip_options(palette, spec.tooltip, spec.tooltip_filter),
                reference_lines: { lines: spec.lines || [] },
            },
            scales: { x: spec.x, y: spec.y },
        };
    }

    function with_alpha(color, alpha) {
        const hex = color.replace('#', '');
        const full = hex.length === 3 ? [...hex].map(c => c + c).join('') : hex;
        if (!/^[0-9a-f]{6}$/i.test(full)) return color;
        const [r, g, b] = [0, 2, 4].map(i => parseInt(full.slice(i, i + 2), 16));
        return `rgba(${r}, ${g}, ${b}, ${alpha})`;
    }

    function chain_points(series, origin) {
        return [
            { x: origin, y: 0, lower: 0, upper: 0 },
            ...series.points.map(point => ({
                x: point.index,
                y: point.cumulative ? point.cumulative.value : null,
                lower: point.cumulative ? point.cumulative.lower : null,
                upper: point.cumulative ? point.cumulative.upper : null,
                point,
            })),
        ];
    }

    function chain_datasets(series, origin, color, palette) {
        const points = chain_points(series, origin);
        const hidden = { borderWidth: 0, pointRadius: 0, pointHoverRadius: 0, pointHitRadius: 0, spanGaps: false, band: true };
        const label = CLASS_LABELS[series.time_class] ?? series.time_class;
        return [
            { ...hidden, label: `${label} lower`, data: points.map(point => ({ x: point.x, y: point.lower })), fill: false },
            {
                ...hidden,
                label: `${label} upper`,
                data: points.map(point => ({ x: point.x, y: point.upper })),
                fill: '-1',
                backgroundColor: with_alpha(color, 0.14),
            },
            {
                label,
                series,
                data: points,
                spanGaps: false,
                clip: false,
                borderColor: color,
                backgroundColor: color,
                borderWidth: 2,
                borderJoinStyle: 'round',
                borderCapStyle: 'round',
                pointRadius: context => (context.raw && context.raw.point ? 4 : 0),
                pointHoverRadius: context => (context.raw && context.raw.point ? 6 : 0),
                pointHitRadius: 12,
                pointBorderWidth: 2,
                pointBorderColor: palette.surface,
                pointHoverBorderColor: palette.surface,
                tension: 0,
            },
        ];
    }

    function projected_datasets(series, origin, color, palette) {
        const points = chain_points(series, origin);
        const label = CLASS_LABELS[series.time_class] ?? series.time_class;
        return series.points.flatMap((point, position) => {
            if (!point.projected) return [];
            const before = points.slice(0, position + 1).findLast(found => is_number(found.y));
            return [{
                label: `${label} running`,
                provisional: true,
                data: [{ x: before.x, y: before.y }, { x: point.index, y: point.projected.value, point }],
                clip: false,
                borderColor: color,
                borderWidth: 2,
                borderDash: [4, 4],
                backgroundColor: palette.surface,
                pointRadius: context => (context.raw && context.raw.point ? 4 : 0),
                pointHoverRadius: context => (context.raw && context.raw.point ? 6 : 0),
                pointHitRadius: 12,
                pointBorderWidth: 2,
                pointBorderColor: color,
                tension: 0,
            }];
        });
    }

    function step_title(row) {
        const step = row.step;
        const commits = `${step.base.sha.slice(0, SHORT_SHA)} → ${step.dev.sha.slice(0, SHORT_SHA)}`;
        return step.subject ? [`Step ${row.index}: ${commits}`, step.subject] : `Step ${row.index}: ${commits}`;
    }

    function chain_summary(series) {
        const label = CLASS_LABELS[series.time_class] ?? series.time_class;
        const running = series.provisional ? `, ${series.provisional} still running` : '';
        if (!series.total) return `${label}: no step measured${running}`;
        return `${label} ${format_interval(series.total)} over ${series.measured} of ${plural(series.steps, 'step')}${running}`;
    }

    function trunk_chart(report, palette, quiet) {
        const lineage = report.lineage;
        if (!lineage && report.lineage_engines.length > 1) return { empty: 'A lineage follows one engine; choose an engine to see its trunk.' };
        if (!lineage) return { empty: 'No test of one commit against another yet.' };
        if (!lineage.steps.length) return { empty: 'No step joined the trunk in this window.' };
        if (!lineage.series.some(series => series.measured || series.provisional)) return { empty: 'No trunk step in this window has an Elo estimate.' };

        const rows = new Map(lineage.steps.map(row => [row.index, row]));
        const origin = lineage.steps[0].index - 1;
        const last = lineage.steps.at(-1).index;
        const datasets = lineage.series.flatMap(series => [
            ...chain_datasets(series, origin, palette.classes[series.time_class], palette),
            ...projected_datasets(series, origin, palette.classes[series.time_class], palette),
        ]);
        const bounds = datasets.flatMap(dataset => dataset.data.map(point => point.y)).filter(is_number);
        const low = Math.min(0, ...bounds);
        const high = Math.max(0, ...bounds);
        const pad = 0.08 * (high - low || 1);

        return {
            label: `Chained Elo along ${plural(lineage.steps.length, 'trunk step')}. ${lineage.series.map(chain_summary).join('. ')}.`,
            config: {
                type: 'line',
                data: { datasets },
                options: base_options(palette, {
                    quiet,
                    interaction: { mode: 'nearest', axis: 'x', intersect: false },
                    legend: {
                        display: true,
                        position: 'top',
                        align: 'start',
                        labels: {
                            color: palette.muted,
                            font: { family: palette.font },
                            boxWidth: 10,
                            boxHeight: 10,
                            padding: 12,
                            filter: (item, data) => !data.datasets[item.datasetIndex].band && !data.datasets[item.datasetIndex].provisional,
                        },
                        onClick: () => {},
                    },
                    x: axis(palette, {
                        type: 'linear',
                        min: origin,
                        max: last,
                        ticks: { precision: 0, maxTicksLimit: 12, callback: value => (Number.isInteger(value) ? `s${value}` : '') },
                    }),
                    y: axis(palette, {
                        min: low - pad,
                        max: high + pad,
                        ticks: { callback: value => format_signed(value, Math.abs(high - low) < 10 ? 1 : 0), maxTicksLimit: MAX_TICKS },
                    }),
                    lines: [{ value: 0, color: palette.axis }],
                    tooltip_filter: item => Boolean(!item.dataset.band && item.raw && item.raw.point),
                    tooltip: {
                        title: items => {
                            const row = items[0] && rows.get(items[0].raw.x);
                            return row ? step_title(row) : '';
                        },
                        label: item => {
                            const point = item.raw.point;
                            if (point.projected) return `${item.dataset.label}: step ${format_interval(point.elo)} so far, not in the total`;
                            return `${item.dataset.label}: chained ${format_interval(point.cumulative)} (step ${format_interval(point.elo)})`;
                        },
                        footer: items => {
                            const row = items[0] && rows.get(items[0].raw.x);
                            return row ? long_day_format.format(Date.parse(row.step.measured_at)) : '';
                        },
                    },
                }),
            },
        };
    }

    function outcome_chart(report, palette, quiet) {
        const weeks = report.weekly_outcomes;
        const total = weeks.reduce((sum, week) => sum + week.passed + week.failed + week.stopped, 0);
        if (!total) return { empty: 'No SPRT test finished in this window.' };

        const series = [
            { key: 'passed', label: 'Passed', color: palette.pass },
            { key: 'failed', label: 'Failed', color: palette.fail },
            { key: 'stopped', label: 'Stopped', color: palette.stopped },
        ];
        const decided = weeks.reduce((sum, week) => sum + week.passed + week.failed, 0);
        const passed = weeks.reduce((sum, week) => sum + week.passed, 0);

        return {
            label: `${total} SPRT tests finished over ${weeks.length} weeks; ${passed} of ${decided} decided tests passed.`,
            config: {
                type: 'bar',
                data: {
                    labels: weeks.map(week => week.week_start),
                    datasets: series.map(item => ({
                        label: item.label,
                        data: weeks.map(week => week[item.key]),
                        backgroundColor: item.color,
                        borderColor: palette.surface,
                        borderWidth: { top: 2, right: 0, bottom: 0, left: 0 },
                        borderSkipped: 'start',
                        maxBarThickness: BAR_THICKNESS,
                        categoryPercentage: 0.8,
                        barPercentage: 0.9,
                    })),
                },
                options: base_options(palette, {
                    quiet,
                    interaction: { mode: 'index', intersect: false },
                    legend: {
                        display: true,
                        position: 'top',
                        align: 'start',
                        labels: { color: palette.muted, font: { family: palette.font }, boxWidth: 10, boxHeight: 10, padding: 12 },
                    },
                    x: axis(palette, {
                        stacked: true,
                        grid: { display: false },
                        ticks: { callback: index => day_format.format(day_ms(weeks[index].week_start)), autoSkip: true, maxTicksLimit: 8, maxRotation: 0 },
                    }),
                    y: axis(palette, {
                        stacked: true,
                        beginAtZero: true,
                        ticks: { precision: 0, maxTicksLimit: MAX_TICKS },
                    }),
                    tooltip: {
                        title: items => (items[0] ? `Week of ${long_day_format.format(day_ms(weeks[items[0].dataIndex].week_start))}` : ''),
                        label: item => `${item.dataset.label}: ${format_count(item.raw)}`,
                        footer: items => {
                            const week = items[0] && weeks[items[0].dataIndex];
                            if (!week) return '';
                            const decided_week = week.passed + week.failed;
                            return decided_week ? `Pass rate ${format_percent(week.passed / decided_week)}` : 'No decided tests';
                        },
                    },
                }),
            },
        };
    }

    function games_chart(report, palette, quiet) {
        const days = report.daily_games;
        const total = days.reduce((sum, day) => sum + day.games, 0);
        if (!total) return { empty: 'No games were recorded in this window.' };

        return {
            label: `${format_count(total)} games over ${days.length} days, from ${day_format.format(day_ms(days[0].day))} to ${day_format.format(day_ms(days.at(-1).day))}.`,
            config: {
                type: 'bar',
                data: {
                    labels: days.map(day => day.day),
                    datasets: [{
                        label: 'Games',
                        data: days.map(day => day.games),
                        backgroundColor: palette.series,
                        borderRadius: { topLeft: 4, topRight: 4 },
                        borderSkipped: 'start',
                        maxBarThickness: BAR_THICKNESS,
                        categoryPercentage: 1,
                        barPercentage: days.length > 120 ? 1 : 0.8,
                    }],
                },
                options: base_options(palette, {
                    quiet,
                    interaction: { mode: 'index', intersect: false },
                    x: axis(palette, {
                        grid: { display: false },
                        ticks: { callback: index => day_format.format(day_ms(days[index].day)), autoSkip: true, maxTicksLimit: 8, maxRotation: 0 },
                    }),
                    y: axis(palette, {
                        beginAtZero: true,
                        ticks: { callback: format_compact, maxTicksLimit: MAX_TICKS },
                    }),
                    tooltip: {
                        title: items => (items[0] ? long_day_format.format(day_ms(days[items[0].dataIndex].day)) : ''),
                        label: item => `${format_count(item.raw)} games`,
                    },
                }),
            },
        };
    }

    const BUILDERS = { trunk: trunk_chart, outcomes: outcome_chart, games: games_chart };

    class ChartPanel {
        constructor(box) {
            this.box = box;
            this.canvas = box.querySelector('canvas');
            this.empty = box.parentElement.querySelector('.chart-empty');
            this.build = BUILDERS[box.dataset.progressChart];
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

        render(report, palette, quiet) {
            if (typeof window.Chart === 'undefined') return this.show_empty('The charting library failed to load.');

            const built = this.build(report, palette, quiet);
            if (!built.config) return this.show_empty(built.empty);

            this.box.hidden = false;
            this.empty.hidden = true;
            this.canvas.setAttribute('aria-label', `${this.title} chart. ${built.label}`);
            if (this.chart) this.chart.destroy();
            this.chart = new window.Chart(this.canvas, { ...built.config, plugins: [reference_lines_plugin] });
        }
    }

    function read_report() {
        const island = document.getElementById('progress-data');
        return island ? JSON.parse(island.textContent) : null;
    }

    function wire_engine_form(root) {
        const form = root.querySelector('[data-progress-engine-form]');
        if (!form) return;
        const select = form.querySelector('[data-progress-engine]');
        const submit = form.querySelector('[data-progress-engine-submit]');
        submit.hidden = true;
        select.addEventListener('change', () => form.requestSubmit());
    }

    function watch_theme(on_change) {
        new MutationObserver(on_change).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
        window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', on_change);
    }

    function init(root) {
        wire_engine_form(root);

        const report = read_report();
        if (!report) return;

        const panels = [...root.querySelectorAll('[data-progress-chart]')].map(box => new ChartPanel(box));
        const render = quiet => {
            const palette = css_palette();
            panels.forEach(panel => panel.render(report, palette, quiet));
        };
        render(false);
        watch_theme(() => render(true));
    }

    document.addEventListener('DOMContentLoaded', () => {
        const root = document.querySelector('[data-progress]');
        if (root) init(root);
    });

})();
