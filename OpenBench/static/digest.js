(() => {
    'use strict';

    const VISIT_KEY = 'openbench-digest-visit';
    const VISIT_GAP_MS = 30 * 60 * 1000;
    const MAX_TICKS = 6;
    const BAR_THICKNESS = 24;
    const HOUR_MS = 3600 * 1000;
    const CLASS_LABELS = { stc: 'STC', ltc: 'LTC', vltc: 'VLTC', smp: 'SMP', other: 'Other' };

    const count_format = new Intl.NumberFormat();
    const compact_format = new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 });
    const hour_format = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'UTC' });
    const day_format = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' });
    const moment_format = new Intl.DateTimeFormat(undefined, {
        weekday: 'short', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'UTC',
    });
    const visit_format = new Intl.DateTimeFormat(undefined, {
        month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false,
    });

    const format_count = value => count_format.format(Math.round(value));
    const format_compact = value => compact_format.format(value);

    function read_visits() {
        try {
            const stored = JSON.parse(window.localStorage.getItem(VISIT_KEY));
            const valid = value => (typeof value === 'string' && Number.isFinite(Date.parse(value)) ? value : null);
            return stored && typeof stored === 'object' ? { last: valid(stored.last), previous: valid(stored.previous) } : {};
        } catch (error) {
            return {};
        }
    }

    function write_visits(visits) {
        try {
            window.localStorage.setItem(VISIT_KEY, JSON.stringify(visits));
        } catch (error) {
            /* Storage is blocked: the link simply stays hidden */
        }
    }

    function next_visits(visits, now) {
        const resumed = visits.last && Date.parse(now) - Date.parse(visits.last) <= VISIT_GAP_MS;
        return { last: now, previous: resumed ? visits.previous || null : visits.last || null };
    }

    function wire_last_visit(root) {
        const now = root.dataset.digestGenerated;
        if (!now || !Number.isFinite(Date.parse(now))) return;

        const visits = next_visits(read_visits(), now);
        write_visits(visits);

        const link = root.querySelector('[data-digest-last-visit]');
        if (!link || !visits.previous) return;

        const since = new Date(visits.previous);
        link.href = `/digest/?from=${encodeURIComponent(since.toISOString())}`;
        if (new URL(link.href).search === window.location.search) return;
        link.title = `Since ${visit_format.format(since)}`;
        link.hidden = false;
    }

    function css_palette() {
        const tokens = getComputedStyle(document.documentElement);
        const read = name => tokens.getPropertyValue(name).trim();
        return {
            classes: {
                stc: read('--series-1'),
                ltc: read('--series-2'),
                vltc: read('--series-3'),
                smp: read('--series-4'),
                other: read('--neutral-edge'),
            },
            grid: read('--chart-grid'),
            axis: read('--chart-axis'),
            text: read('--text'),
            muted: read('--text-muted'),
            surface: read('--surface'),
            tooltip: read('--surface-3'),
            border: read('--border'),
            font: read('--font-sans'),
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

    function tick_label(buckets, index, spans_days) {
        const start = new Date(buckets[index].start);
        return spans_days ? day_format.format(start) : hour_format.format(start);
    }

    function bucket_title(bucket, hours) {
        const start = Date.parse(bucket.start);
        return `${moment_format.format(start)} to ${hour_format.format(start + hours * HOUR_MS)} UTC`;
    }

    function games_chart(fleet, palette, quiet) {
        const { buckets, classes, bucket_hours: hours } = fleet;
        if (!fleet.games || !buckets.length) return { empty: 'No games were recorded in this window.' };

        const reduced_motion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
        const spans_days = buckets.length * hours > 48;
        const peak = fleet.peak_games_per_hour;

        return {
            label: `${format_count(fleet.games)} games over ${buckets.length} periods of ${hours} h, peaking at ${format_count(peak)} games per hour.`,
            config: {
                type: 'bar',
                data: {
                    labels: buckets.map(bucket => bucket.start),
                    datasets: classes.map((name, position) => ({
                        label: CLASS_LABELS[name] ?? name,
                        data: buckets.map(bucket => bucket.games[position] / hours),
                        backgroundColor: palette.classes[name] ?? palette.classes.other,
                        borderColor: palette.surface,
                        borderWidth: { top: 2, right: 0, bottom: 0, left: 0 },
                        borderSkipped: 'start',
                        maxBarThickness: BAR_THICKNESS,
                        categoryPercentage: 1,
                        barPercentage: 0.8,
                    })),
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    animation: reduced_motion || quiet ? false : { duration: 300 },
                    interaction: { mode: 'index', intersect: false },
                    layout: { padding: { top: 4, right: 8 } },
                    plugins: {
                        legend: { display: false },
                        tooltip: {
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
                            callbacks: {
                                title: items => (items[0] ? bucket_title(buckets[items[0].dataIndex], hours) : ''),
                                label: item => `${item.dataset.label}: ${format_count(item.raw)} games per hour`,
                                footer: items => {
                                    const bucket = items[0] && buckets[items[0].dataIndex];
                                    if (!bucket) return '';
                                    const total = bucket.games.reduce((sum, games) => sum + games, 0);
                                    return `${format_count(total)} games in this period`;
                                },
                            },
                        },
                    },
                    scales: {
                        x: axis(palette, {
                            stacked: true,
                            grid: { display: false },
                            ticks: {
                                callback: index => tick_label(buckets, index, spans_days),
                                autoSkip: true,
                                maxTicksLimit: 8,
                                maxRotation: 0,
                            },
                        }),
                        y: axis(palette, {
                            stacked: true,
                            beginAtZero: true,
                            ticks: { callback: format_compact, maxTicksLimit: MAX_TICKS },
                        }),
                    },
                },
            },
        };
    }

    class ChartPanel {
        constructor(box) {
            this.box = box;
            this.canvas = box.querySelector('canvas');
            this.empty = box.parentElement.querySelector('.chart-empty');
            this.chart = null;
        }

        show_empty(message) {
            if (this.chart) this.chart.destroy();
            this.chart = null;
            this.box.hidden = true;
            this.empty.hidden = false;
            this.empty.textContent = message;
        }

        render(fleet, palette, quiet) {
            if (typeof window.Chart === 'undefined') return this.show_empty('The charting library failed to load.');

            const built = games_chart(fleet, palette, quiet);
            if (!built.config) return this.show_empty(built.empty);

            this.box.hidden = false;
            this.empty.hidden = true;
            this.canvas.setAttribute('aria-label', `Games per hour chart. ${built.label}`);
            if (this.chart) this.chart.destroy();
            this.chart = new window.Chart(this.canvas, built.config);
        }
    }

    function read_fleet() {
        const island = document.getElementById('digest-data');
        return island ? JSON.parse(island.textContent) : null;
    }

    function watch_theme(on_change) {
        new MutationObserver(on_change).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
        window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', on_change);
    }

    function init(root) {
        wire_last_visit(root);

        const fleet = read_fleet();
        const box = root.querySelector('[data-digest-chart]');
        if (!fleet || !box) return;

        const panel = new ChartPanel(box);
        panel.render(fleet, css_palette(), false);
        watch_theme(() => panel.render(fleet, css_palette(), true));
    }

    document.addEventListener('DOMContentLoaded', () => {
        const root = document.querySelector('[data-digest]');
        if (root) init(root);
    });

})();
