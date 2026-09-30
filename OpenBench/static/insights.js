(() => {
    'use strict';

    const REFRESH_MS = 60_000;
    const FETCH_TIMEOUT_MS = 20_000;
    const DASH = '—';
    const MINUS = '−';
    const ELO_RANGE_WARMUP = 0.1;
    const MAX_TICKS = 6;
    const MAX_BACKOFF_MS = 8 * REFRESH_MS;
    const MAX_CLIENT_ERRORS = 3;
    const POLLED_STATUSES = new Set(['pending', 'active']);
    const ETA_REASONS = {
        too_few_games: 'needs 200 games first',
        outside_bounds: 'LLR is outside the bounds',
        empty_outcome: 'needs wins, draws and losses',
        no_variance: 'results too uniform to project',
        no_target: 'no target to reach',
        no_rate: 'no recent throughput',
    };
    const DURATION_STEPS = [60, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 345600, 604800, 1209600];

    const count_format = new Intl.NumberFormat();
    const compact_format = new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 });
    const time_format = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
    const date_format = new Intl.DateTimeFormat(undefined, {
        month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false,
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

    function format_signed(value, digits = 2) {
        if (!is_number(value)) return DASH;
        const text = Math.abs(value).toFixed(digits);
        if (Number(text) === 0) return text;
        return (value > 0 ? '+' : MINUS) + text;
    }

    function format_percent(fraction, digits = 1) {
        return is_number(fraction) ? `${(100 * fraction).toFixed(digits)}%` : DASH;
    }

    function format_rate(per_hour) {
        if (!is_number(per_hour)) return DASH;
        return per_hour >= 100 ? count_format.format(Math.round(per_hour)) : per_hour.toFixed(1);
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

    function format_datetime(iso) {
        return iso ? date_format.format(new Date(iso)) : DASH;
    }

    function format_interval(interval) {
        if (!interval || ![interval.lower, interval.value, interval.upper].every(is_number)) return DASH;
        const half = Math.max(interval.upper - interval.value, interval.value - interval.lower);
        return `${format_signed(interval.value)} ± ${format_fixed(half)}`;
    }

    function format_bounds(interval) {
        return interval ? `95% ${format_fixed(interval.lower)} to ${format_fixed(interval.upper)}` : '';
    }

    function element(tag, class_name, text) {
        const node = document.createElement(tag);
        if (class_name) node.className = class_name;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    function meter(fraction, variant, label) {
        const clamped = Math.min(1, Math.max(0, fraction));
        const bar = element('span', `insight-meter insight-meter-${variant}`);
        bar.setAttribute('role', 'meter');
        bar.setAttribute('aria-valuemin', '0');
        bar.setAttribute('aria-valuemax', '1');
        bar.setAttribute('aria-valuenow', clamped.toFixed(3));
        bar.setAttribute('aria-valuetext', label);
        bar.title = label;
        bar.style.setProperty('--fraction', clamped.toFixed(4));
        return bar;
    }

    function stat_tile({ label, value, meta, title, gauge }) {
        const tile = element('div', 'stat-tile');
        const label_node = element('span', 'stat-label', label);
        if (title) label_node.title = title;
        tile.append(label_node, element('span', 'stat-value', value));
        if (meta) tile.append(element('span', 'stat-meta', meta));
        if (gauge) tile.append(gauge);
        return tile;
    }

    function tile_group(heading, tiles) {
        const group = element('div', 'insights-group');
        const grid = element('div', 'stat-tiles');
        grid.append(...tiles);
        group.append(element('h4', 'insights-group-title', heading), grid);
        return group;
    }

    async function fetch_json(url) {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
        try {
            const response = await fetch(url, {
                credentials: 'same-origin',
                headers: { Accept: 'application/json' },
                signal: controller.signal,
            });
            const data = await response.json().catch(() => null);
            if (!response.ok || !data || data.error) {
                const client = (response.status >= 400 && response.status < 500) || Boolean(data && data.error);
                throw new FetchError(data && data.error ? data.error : `HTTP ${response.status}`, client);
            }
            return data;
        } finally {
            clearTimeout(timer);
        }
    }

    class FetchError extends Error {
        constructor(message, client) {
            super(message);
            this.name = 'FetchError';
            this.client = client;
        }
    }

    function describe_error(err) {
        return err && err.name === 'AbortError' ? 'the request timed out' : (err && err.message) || 'unknown error';
    }

    function elapsed_tile(timing) {
        const meta = timing.ended_at
            ? `${format_datetime(timing.started_at)} to ${format_datetime(timing.ended_at)}`
            : `since ${format_datetime(timing.started_at)}`;
        return stat_tile({ label: 'Elapsed', value: format_duration(timing.elapsed_seconds), meta });
    }

    function games_tile(progress) {
        if (!is_number(progress.target_games) || progress.target_games <= 0)
            return stat_tile({ label: 'Games', value: format_count(progress.games), meta: `${format_count(progress.pairs)} pairs` });

        const fraction = progress.fraction ?? 0;
        const label = `${format_count(progress.games)} of ${format_count(progress.target_games)} games`;
        return stat_tile({
            label: 'Games',
            value: format_count(progress.games),
            meta: `of ${format_count(progress.target_games)} · ${format_percent(fraction, 0)}`,
            gauge: meter(fraction, 'fill', label),
        });
    }

    function rate_tile(timing) {
        const recent = timing.recent;
        const overall = timing.overall;
        const headline = recent ?? overall;
        const window_text = recent ? `last ${format_duration(recent.window_seconds)}` : 'overall';
        const meta = recent && overall
            ? `${window_text} · overall ${format_rate(overall.games_per_hour)}`
            : window_text;
        return stat_tile({
            label: 'Games per hour',
            value: headline ? format_rate(headline.games_per_hour) : DASH,
            meta: headline ? meta : 'needs 5 minutes of play',
        });
    }

    function eta_meta(insights) {
        const { eta, timing, workload } = insights;
        if (eta.kind === 'finished') return timing.ended_at ? `finished ${format_datetime(timing.ended_at)}` : 'finished';
        if (workload.status === 'pending') return 'not approved yet';
        if (eta.reason) return ETA_REASONS[eta.reason] ?? 'not available';
        if (!is_number(eta.remaining_seconds)) return 'not available';

        const prefix = eta.kind === 'sprt_estimate' ? '≈ ' : '';
        return `${prefix}${format_datetime(eta.completes_at)} · ${format_compact(eta.remaining_games)} left`;
    }

    function eta_tile(insights) {
        const { eta } = insights;
        const estimate = eta.kind === 'sprt_estimate';
        let value = DASH;
        if (eta.kind === 'finished') value = 'Done';
        else if (is_number(eta.remaining_seconds)) value = `${estimate ? '≈ ' : ''}${format_duration(eta.remaining_seconds)}`;

        return stat_tile({
            label: estimate || (insights.workload.mode === 'SPRT' && eta.kind !== 'finished') ? 'Time left (estimate)' : 'Time left',
            title: estimate ? 'Assumes the test keeps producing results like it has so far; an order of magnitude, not a promise.' : undefined,
            value,
            meta: eta_meta(insights),
        });
    }

    function llr_tile(progress) {
        const span = progress.llr_upper - progress.llr_lower;
        const fraction = span > 0 ? (progress.llr - progress.llr_lower) / span : 0.5;
        const label = `LLR ${format_fixed(progress.llr)} between ${format_fixed(progress.llr_lower)} and ${format_fixed(progress.llr_upper)}`;
        return stat_tile({
            label: 'LLR',
            value: format_fixed(progress.llr),
            meta: `bounds ${format_fixed(progress.llr_lower)} to ${format_fixed(progress.llr_upper)}`,
            gauge: meter(fraction, 'position', label),
        });
    }

    function strength_tiles(strength, progress) {
        const [losses, draws, wins] = progress.trinomial;
        return [
            stat_tile({ label: 'Elo', value: format_interval(strength.elo), meta: format_bounds(strength.elo) }),
            stat_tile({
                label: 'Normalized Elo', value: format_interval(strength.normalized_elo), meta: format_bounds(strength.normalized_elo),
                title: 'Elo scaled by the per-pair spread; the scale the pentanomial SPRT uses.',
            }),
            stat_tile({
                label: 'LOS', value: format_percent(strength.los), meta: 'chance dev is stronger',
                title: 'Likelihood of superiority',
            }),
            stat_tile({
                label: 'Draw ratio', value: format_percent(strength.draw_ratio),
                meta: `W ${format_compact(wins)} · D ${format_compact(draws)} · L ${format_compact(losses)}`,
            }),
        ];
    }

    function render_tiles(container, insights) {
        const { progress, timing, strength, workload } = insights;
        const groups = [tile_group('Progress', [elapsed_tile(timing), games_tile(progress), rate_tile(timing), eta_tile(insights)])];

        if (strength && progress.games > 0) {
            const tiles = strength_tiles(strength, progress);
            if (workload.mode === 'SPRT' && is_number(progress.llr)) tiles.unshift(llr_tile(progress));
            groups.push(tile_group('Strength', tiles));
        }

        container.replaceChildren(...groups);
    }

    function share_cell(share) {
        const cell = element('td', 'share-cell');
        const bar = element('span', 'share-bar');
        bar.setAttribute('aria-hidden', 'true');
        bar.style.setProperty('--share', is_number(share) ? share.toFixed(4) : '0');
        cell.append(bar, element('span', 'share-value', format_percent(share)));
        return cell;
    }

    function numeric_cell(text) {
        return element('td', 'numeric', text);
    }

    function machine_label(row) {
        const cell = element('td', 'contribution-name');
        const name = row.machine_name || `Machine ${row.machine_id}`;
        const link = element('a', null, name);
        link.href = `/machines/${encodeURIComponent(row.machine_id)}/`;
        cell.append(link, element('span', 'contribution-sub', row.owner || ''));
        return cell;
    }

    function cpu_label(row) {
        const cell = element('td', 'contribution-name');
        const machines = `${row.machines} ${row.machines === 1 ? 'machine' : 'machines'}`;
        cell.append(element('span', null, row.cpu_name), element('span', 'contribution-sub', machines));
        return cell;
    }

    function contribution_table(caption, first_header, rows, label_cell, with_elo) {
        const wrap = element('div', 'table-wrap contribution-table');
        const table = element('table', 'stripes');
        table.append(element('caption', 'contribution-caption', caption));

        const head = element('tr', 'table-header');
        const headers = [first_header, 'Share', 'Games', 'Pairs / h', ...(with_elo ? ['Elo'] : [])];
        headers.forEach((text, index) => head.append(element('th', index >= 2 ? 'numeric' : null, text)));
        const thead = element('thead');
        thead.append(head);
        table.append(thead);

        const body = element('tbody');
        rows.forEach(row => {
            const tr = element('tr');
            tr.append(label_cell(row), share_cell(row.stats.share), numeric_cell(format_count(row.stats.games)),
                numeric_cell(format_rate(row.stats.pairs_per_hour)));
            if (with_elo) tr.append(numeric_cell(format_interval(row.stats.elo)));
            body.append(tr);
        });
        table.append(body);
        wrap.append(table);
        return wrap;
    }

    function render_contributions(container, insights) {
        const { cpus, machines } = insights.contributions;
        const with_elo = insights.strength !== null;
        const card = element('div', 'insights-contributions');
        const header = element('div', 'card-header');
        header.append(element('h4', 'card-title', 'Contributions'),
            element('span', 'muted insights-note', 'Pairs per hour average over the whole elapsed time'));
        card.append(header);

        if (!machines.length) {
            card.append(element('p', 'insights-empty', 'No results have been reported yet.'));
        } else {
            const grid = element('div', 'contribution-grid');
            grid.append(
                contribution_table('By CPU', 'CPU', cpus, cpu_label, with_elo),
                contribution_table('By machine', 'Machine', machines, machine_label, with_elo),
            );
            card.append(grid);
        }
        container.replaceChildren(card);
    }

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
            const lines = options.lines || [];
            const { ctx, chartArea, scales } = chart;
            ctx.save();
            lines.forEach(line => {
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
            const x = Math.round(active[0].element.x) + 0.5;
            ctx.save();
            ctx.strokeStyle = options.color;
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(x, chartArea.top);
            ctx.lineTo(x, chartArea.bottom);
            ctx.stroke();
            ctx.restore();
        },
    };

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

    function base_options(palette, spec) {
        const reduced_motion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
        return {
            responsive: true,
            maintainAspectRatio: false,
            animation: reduced_motion || spec.quiet ? false : { duration: 300 },
            interaction: { mode: 'nearest', axis: 'x', intersect: false },
            layout: { padding: { top: 4, right: 8 } },
            plugins: {
                legend: { display: false },
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
                    displayColors: false,
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

    function line_dataset(palette, points, label) {
        return {
            label,
            data: points,
            borderColor: palette.series,
            backgroundColor: palette.series,
            borderWidth: 2,
            borderJoinStyle: 'round',
            borderCapStyle: 'round',
            pointRadius: 0,
            pointHitRadius: 12,
            pointHoverRadius: 5,
            pointHoverBorderWidth: 2,
            pointHoverBorderColor: palette.surface,
            tension: 0,
        };
    }

    function band_datasets(palette, lower, upper) {
        const hidden = { borderWidth: 0, pointRadius: 0, pointHoverRadius: 0, pointHitRadius: 0, tooltip: false };
        return [
            { ...hidden, label: 'lower', data: lower, fill: false },
            { ...hidden, label: 'upper', data: upper, fill: '-1', backgroundColor: with_alpha(palette.series, 0.14) },
        ];
    }

    function tooltip_title(items) {
        const raw = items[0] && items[0].raw;
        return raw ? `${format_count(raw.games)} games · ${format_datetime(raw.timestamp)}` : '';
    }

    function llr_chart(insights, palette) {
        const { progress } = insights;
        const points = insights.history.points
            .filter(point => is_number(point.llr))
            .map(point => ({ x: point.games, y: point.llr, games: point.games, timestamp: point.timestamp }));
        const pad = 0.08 * (progress.llr_upper - progress.llr_lower);
        const last = points.at(-1);

        return {
            points: points.length,
            label: last ? `LLR ${format_fixed(last.y)} after ${format_count(last.x)} games; bounds ${format_fixed(progress.llr_lower)} and ${format_fixed(progress.llr_upper)}.` : '',
            config: {
                type: 'line',
                data: { datasets: [line_dataset(palette, points, 'LLR')] },
                options: base_options(palette, {
                    x_tick: format_compact,
                    x_title: 'games',
                    y_tick: value => format_fixed(value, 1),
                    y_min: Math.min(progress.llr_lower - pad, ...points.map(p => p.y)),
                    y_max: Math.max(progress.llr_upper + pad, ...points.map(p => p.y)),
                    x_min: 0,
                    x_max: last ? last.x : undefined,
                    lines: [
                        { value: progress.llr_upper, color: palette.pass, label: `pass ${format_fixed(progress.llr_upper)}`, below: true },
                        { value: progress.llr_lower, color: palette.fail, label: `fail ${format_fixed(progress.llr_lower)}` },
                        { value: 0, color: palette.axis, dash: [2, 3] },
                    ],
                    tooltip: {
                        title: tooltip_title,
                        label: item => `LLR ${format_fixed(item.raw.y)}`,
                    },
                }),
            },
        };
    }

    function elo_range(points) {
        const last_games = points.at(-1).x;
        const settled = points.filter(point => point.x >= ELO_RANGE_WARMUP * last_games);
        const lows = settled.map(point => point.lower);
        const highs = settled.map(point => point.upper);
        const low = Math.min(0, ...lows);
        const high = Math.max(0, ...highs);
        const pad = 0.08 * (high - low || 1);
        return { min: low - pad, max: high + pad };
    }

    function elo_chart(insights, palette) {
        const points = insights.history.points
            .filter(point => is_number(point.elo) && is_number(point.elo_lower) && is_number(point.elo_upper))
            .map(point => ({
                x: point.games, y: point.elo, lower: point.elo_lower, upper: point.elo_upper,
                games: point.games, timestamp: point.timestamp,
            }));
        if (points.length < 2) return { points: points.length, label: '', config: null };

        const range = elo_range(points);
        const last = points.at(-1);
        const lower = points.map(point => ({ x: point.x, y: point.lower }));
        const upper = points.map(point => ({ x: point.x, y: point.upper }));

        return {
            points: points.length,
            label: `Elo ${format_signed(last.y)}, 95% interval ${format_fixed(last.lower)} to ${format_fixed(last.upper)}, after ${format_count(last.x)} games.`,
            config: {
                type: 'line',
                data: { datasets: [...band_datasets(palette, lower, upper), line_dataset(palette, points, 'Elo')] },
                options: base_options(palette, {
                    x_tick: format_compact,
                    x_title: 'games',
                    y_tick: value => format_signed(value, Math.abs(range.max - range.min) < 10 ? 1 : 0),
                    y_min: range.min,
                    y_max: range.max,
                    x_min: 0,
                    x_max: last.x,
                    lines: [{ value: 0, color: palette.axis }],
                    tooltip: {
                        title: tooltip_title,
                        label: item => `Elo ${format_signed(item.raw.y)}`,
                        footer: items => items[0] ? `95% ${format_fixed(items[0].raw.lower)} to ${format_fixed(items[0].raw.upper)}` : '',
                    },
                }),
            },
        };
    }

    function throughput_chart(insights, palette) {
        const history = insights.history.points;
        const origin = history.length ? Date.parse(history[0].timestamp) : 0;
        const points = history.map(point => ({
            x: (Date.parse(point.timestamp) - origin) / 1000,
            y: point.games, games: point.games, timestamp: point.timestamp,
        }));
        const target = insights.progress.target_games;
        const last = points.at(-1);
        const top = Math.max(is_number(target) ? target : 0, ...points.map(point => point.y));

        return {
            points: points.length,
            label: last ? `${format_count(last.y)} games played over ${format_duration(last.x)}.` : '',
            config: {
                type: 'line',
                data: { datasets: [line_dataset(palette, points, 'Games')] },
                options: base_options(palette, {
                    x_tick: value => format_duration(value),
                    x_title: 'elapsed',
                    y_tick: format_compact,
                    x_min: 0,
                    x_max: last ? last.x : undefined,
                    x_step: last ? duration_step(last.x) : undefined,
                    y_min: 0,
                    y_max: top > 0 ? top * 1.06 : undefined,
                    lines: is_number(target) && target > 0
                        ? [{ value: target, color: palette.axis, dash: [4, 3], label: `target ${format_count(target)}`, below: true }]
                        : [],
                    tooltip: {
                        title: items => items[0] ? format_datetime(items[0].raw.timestamp) : '',
                        label: item => `${format_count(item.raw.y)} games`,
                        footer: items => items[0] ? `${format_duration(items[0].raw.x)} elapsed` : '',
                    },
                }),
            },
        };
    }

    function chart_specs(insights) {
        const specs = [];
        if (insights.workload.mode === 'SPRT')
            specs.push({ key: 'llr', title: 'LLR', subtitle: 'Log-likelihood ratio against the SPRT bounds', build: llr_chart });
        if (insights.strength)
            specs.push({ key: 'elo', title: 'Elo', subtitle: 'Estimate with its 95% interval', build: elo_chart });
        specs.push({ key: 'throughput', title: 'Games played', subtitle: 'Cumulative games; the slope is the throughput', build: throughput_chart });
        return specs;
    }

    function chart_card(spec) {
        const card = element('figure', 'card chart-card');
        const header = element('figcaption', 'chart-caption');
        header.append(element('span', 'card-title', spec.title), element('span', 'chart-subtitle', spec.subtitle));

        const box = element('div', 'chart-box');
        const canvas = element('canvas');
        canvas.setAttribute('role', 'img');
        box.append(canvas);

        const empty = element('p', 'chart-empty');
        empty.hidden = true;
        card.append(header, box, empty);
        return { card, box, canvas, empty };
    }

    class ChartPanel {
        constructor(spec) {
            this.spec = spec;
            this.view = chart_card(spec);
            this.chart = null;
        }

        destroy() {
            if (this.chart) this.chart.destroy();
            this.chart = null;
        }

        show_empty(message) {
            this.destroy();
            this.view.box.hidden = true;
            this.view.empty.hidden = false;
            this.view.empty.textContent = message;
        }

        render(insights, palette, quiet) {
            if (typeof window.Chart === 'undefined') return this.show_empty('The charting library failed to load.');

            const built = this.spec.build(insights, palette);
            if (!built.config || built.points < 2)
                return this.show_empty('Not enough points to draw this chart yet.');

            built.config.options.animation = quiet ? false : built.config.options.animation;
            this.view.box.hidden = false;
            this.view.empty.hidden = true;
            this.view.canvas.setAttribute('aria-label', `${this.spec.title} chart. ${built.label}`);

            if (this.chart) {
                this.chart.data = built.config.data;
                this.chart.options = built.config.options;
                this.chart.update('none');
            } else {
                this.chart = new window.Chart(this.view.canvas, { ...built.config, plugins: [reference_lines_plugin, crosshair_plugin] });
            }
        }
    }

    class WorkloadInsights {
        constructor(section) {
            this.section = section;
            this.url = `/api/workload/${encodeURIComponent(section.dataset.workloadId)}/insights/`;
            this.status = section.querySelector('[data-insights-status]');
            this.error = section.querySelector('[data-insights-error]');
            this.tiles = section.querySelector('[data-insights-tiles]');
            this.charts = section.querySelector('[data-insights-charts]');
            this.contributions = section.querySelector('[data-insights-contributions]');
            this.panels = null;
            this.latest = null;
            this.timer = null;
            this.stale = false;
            this.failures = 0;
            this.client_failures = 0;
        }

        start() {
            this.refresh();
            document.addEventListener('visibilitychange', () => this.on_visibility());
            watch_theme(() => this.render_charts(true));
        }

        get polled() {
            return !this.latest || POLLED_STATUSES.has(this.latest.workload.status);
        }

        get retrying() {
            return this.polled && this.client_failures < MAX_CLIENT_ERRORS;
        }

        get delay() {
            return Math.min(MAX_BACKOFF_MS, REFRESH_MS * 2 ** Math.max(0, this.failures - 1));
        }

        async refresh() {
            this.section.setAttribute('aria-busy', 'true');
            try {
                const data = await fetch_json(this.url);
                this.latest = data.insights;
                this.failures = this.client_failures = 0;
                this.render();
                this.show_error(null);
            } catch (err) {
                this.failures += 1;
                this.client_failures = err instanceof FetchError && err.client ? this.client_failures + 1 : 0;
                this.show_error(err);
            } finally {
                this.section.removeAttribute('aria-busy');
                this.schedule();
            }
        }

        schedule() {
            clearTimeout(this.timer);
            if (!this.retrying) return;
            this.timer = setTimeout(() => {
                if (document.hidden) this.stale = true;
                else this.refresh();
            }, this.delay);
        }

        on_visibility() {
            if (document.hidden || !this.stale) return;
            this.stale = false;
            this.refresh();
        }

        render() {
            render_tiles(this.tiles, this.latest);
            this.render_history();
            render_contributions(this.contributions, this.latest);

            const refreshing = this.polled ? ' · refreshes every minute' : '';
            this.status.textContent = `Updated ${time_format.format(new Date())}${refreshing}`;
        }

        render_history() {
            if (this.latest.history.points.length < 2) {
                this.panels?.forEach(panel => panel.destroy());
                this.panels = null;
                const message = this.latest.history.synthetic
                    ? 'This workload predates history recording, so there are no charts.'
                    : 'Charts appear once the workload has reported at least twice.';
                this.charts.replaceChildren(element('p', 'insights-empty', message));
                return;
            }

            const first = this.panels === null;
            if (first) {
                this.panels = chart_specs(this.latest).map(spec => new ChartPanel(spec));
                this.charts.replaceChildren(...this.panels.map(panel => panel.view.card));
            }
            this.render_charts(!first);
        }

        render_charts(quiet) {
            if (!this.latest || !this.panels) return;
            const palette = css_palette();
            this.panels.forEach(panel => panel.render(this.latest, palette, quiet));
        }

        show_error(err) {
            if (!err) {
                this.error.hidden = true;
                this.error.textContent = '';
                return;
            }
            const retry = this.retrying ? ` Retrying in ${format_duration(this.delay / 1000)}.` : '';
            this.error.textContent = `Insights could not be loaded: ${describe_error(err)}.${retry}`;
            this.error.hidden = false;
            if (!this.latest) this.status.textContent = '';
        }
    }

    function watch_theme(on_change) {
        new MutationObserver(on_change).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
        window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', on_change);
    }

    function server_fields(server) {
        const { fleet, workloads, finished_last_7d: finished } = server;
        const decided = finished.sprt_passed + finished.sprt_failed;
        return {
            machines: format_count(fleet.machines),
            fleet: `${format_count(fleet.threads)} threads · ${format_fixed(fleet.mnps, 1)} MNPS`,
            active: format_count(workloads.active),
            pending: `${format_count(workloads.pending)} pending`,
            games: format_count(server.games_last_24h),
            finished: format_count(finished.total),
            outcomes: `${format_count(finished.passed)} passed · ${format_count(finished.failed)} failed · ${format_count(finished.stopped)} stopped`,
            pass_rate: format_percent(finished.sprt_pass_rate, 0),
            decided: decided ? `${format_count(finished.sprt_passed)} of ${format_count(decided)} decided SPRTs` : 'no decided SPRTs',
        };
    }

    async function init_server_stats(section) {
        try {
            const data = await fetch_json('/api/insights/server/');
            const fields = server_fields(data.server);
            section.querySelectorAll('[data-field]').forEach(node => {
                node.textContent = fields[node.dataset.field] ?? DASH;
            });
            section.removeAttribute('aria-busy');
        } catch (err) {
            section.hidden = true;
        }
    }

    document.addEventListener('DOMContentLoaded', () => {
        const workload = document.querySelector('[data-workload-insights]');
        if (workload) new WorkloadInsights(workload).start();

        const server = document.querySelector('[data-server-insights]');
        if (server) init_server_stats(server);
    });

})();
