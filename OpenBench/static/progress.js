(() => {
    'use strict';

    const DAY_MS = 86_400_000;
    const DASH = '—';
    const MINUS = '−';
    const MAX_TICKS = 6;
    const DAY_STEPS = [1, 2, 7, 14, 28, 56, 91, 182, 364, 728];
    const BAR_THICKNESS = 24;

    const count_format = new Intl.NumberFormat();
    const compact_format = new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 });
    const day_format = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' });
    const long_day_format = new Intl.DateTimeFormat(undefined, {
        weekday: 'short', month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC',
    });
    const month_format = new Intl.DateTimeFormat(undefined, { month: 'short', year: 'numeric', timeZone: 'UTC' });

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

    function format_interval(interval) {
        if (!interval || ![interval.lower, interval.value, interval.upper].every(is_number)) return DASH;
        const half = Math.max(interval.upper - interval.value, interval.value - interval.lower);
        return `${format_signed(interval.value, 2)} ± ${format_fixed(half)}`;
    }

    const day_ms = iso => Date.parse(`${iso}T00:00:00Z`);

    function css_palette() {
        const style = getComputedStyle(document.documentElement);
        const read = name => style.getPropertyValue(name).trim();
        return {
            series: read('--series-1'),
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

    function day_ticks(min, max) {
        const span_days = (max - min) / DAY_MS;
        const step = DAY_STEPS.find(days => span_days / days <= MAX_TICKS) ?? DAY_STEPS.at(-1);
        return scale => {
            const first = Math.ceil(scale.min / DAY_MS) * DAY_MS;
            const ticks = [];
            for (let value = first; value <= scale.max; value += step * DAY_MS) ticks.push({ value });
            scale.ticks = ticks;
        };
    }

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

    function elo_chart(report, palette, quiet) {
        const greens = report.greens;
        if (!greens.length) return { empty: 'No greens finished in this window.' };

        const start = day_ms(report.start);
        const end = Math.max(Date.parse(report.generated_at), Date.parse(greens.at(-1).finished_at));
        const points = [
            { x: start, y: 0 },
            ...greens.map(green => ({ x: Date.parse(green.finished_at), y: green.cumulative_elo, green })),
            { x: end, y: greens.at(-1).cumulative_elo },
        ];
        const values = points.map(point => point.y);
        const low = Math.min(0, ...values);
        const high = Math.max(0, ...values);
        const pad = 0.08 * (high - low || 1);
        const last = greens.at(-1);

        return {
            label: `Cumulative Elo estimate ${format_signed(last.cumulative_elo)} from ${greens.length} greens since ${day_format.format(start)}.`,
            config: {
                type: 'line',
                data: {
                    datasets: [{
                        label: 'Cumulative Elo',
                        data: points,
                        stepped: true,
                        borderColor: palette.series,
                        backgroundColor: palette.series,
                        borderWidth: 2,
                        borderJoinStyle: 'round',
                        borderCapStyle: 'round',
                        pointRadius: context => (context.raw && context.raw.green ? 4 : 0),
                        pointHoverRadius: context => (context.raw && context.raw.green ? 6 : 0),
                        pointHitRadius: context => (context.raw && context.raw.green ? 12 : 0),
                        pointBorderWidth: 2,
                        pointBorderColor: palette.surface,
                        pointHoverBorderColor: palette.surface,
                    }],
                },
                options: base_options(palette, {
                    quiet,
                    interaction: { mode: 'nearest', axis: 'x', intersect: false },
                    x: axis(palette, {
                        type: 'linear',
                        min: start,
                        max: end,
                        afterBuildTicks: day_ticks(start, end),
                        ticks: { callback: value => ((end - start) > 400 * DAY_MS ? month_format : day_format).format(value) },
                    }),
                    y: axis(palette, {
                        min: low - pad,
                        max: high + pad,
                        ticks: { callback: value => format_signed(value, Math.abs(high - low) < 10 ? 1 : 0), maxTicksLimit: MAX_TICKS },
                    }),
                    lines: [{ value: 0, color: palette.axis }],
                    tooltip_filter: item => Boolean(item.raw && item.raw.green),
                    tooltip: {
                        title: items => (items[0] && items[0].raw.green ? items[0].raw.green.name : ''),
                        label: item => (item.raw.green ? `Elo ${format_interval(item.raw.green.elo)}` : ''),
                        footer: items => {
                            const green = items[0] && items[0].raw.green;
                            if (!green) return '';
                            return [
                                `Running sum ${format_signed(green.cumulative_elo)}`,
                                `${format_count(green.games)} games · ${long_day_format.format(Date.parse(green.finished_at))}`,
                            ];
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

    const BUILDERS = { elo: elo_chart, outcomes: outcome_chart, games: games_chart };

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

    function fill_share_bars(root) {
        root.querySelectorAll('.share-bar[data-share]').forEach(bar => {
            const share = Number(bar.dataset.share);
            bar.style.setProperty('--share', is_number(share) ? String(Math.min(1, Math.max(0, share))) : '0');
        });
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
        fill_share_bars(root);
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
