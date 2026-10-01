(() => {
    'use strict';

    const REFRESH_MS = 60_000;
    const CATCH_UP_MS = 4_000;
    const MAX_CATCH_UPS = 80;
    const FETCH_TIMEOUT_MS = 20_000;
    const DASH = '—';
    const MINUS = '−';
    const OUTCOME_KEYS = [
        { key: 'wins', label: 'Wins', variant: 'win' },
        { key: 'draws', label: 'Draws', variant: 'draw' },
        { key: 'losses', label: 'Losses', variant: 'loss' },
    ];
    const PAIR_ROWS = [
        { key: 'ww', label: 'Won both', bucket: 'WW' },
        { key: 'wd', label: 'Won one, drew one', bucket: 'DW' },
        { key: 'wl', label: 'Won one, lost one', bucket: 'DD' },
        { key: 'dd', label: 'Drew both', bucket: 'DD' },
        { key: 'dl', label: 'Drew one, lost one', bucket: 'LD' },
        { key: 'll', label: 'Lost both', bucket: 'LL' },
    ];
    const SIDE_LABELS = { dev: 'Dev', base: 'Base' };

    const count_format = new Intl.NumberFormat();
    const compact_format = new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 });

    const is_number = value => typeof value === 'number' && Number.isFinite(value);
    const format_count = value => (is_number(value) ? count_format.format(Math.round(value)) : DASH);
    const format_compact = value => (is_number(value) ? compact_format.format(value) : DASH);
    const format_fixed = (value, digits = 1) => (is_number(value) ? value.toFixed(digits).replace('-', MINUS) : DASH);
    const format_percent = (fraction, digits = 1) => (is_number(fraction) ? `${(100 * fraction).toFixed(digits)}%` : DASH);

    function format_pawns(centipawns) {
        if (!is_number(centipawns)) return DASH;
        const text = Math.abs(centipawns / 100).toFixed(2);
        if (Number(text) === 0) return text;
        return (centipawns > 0 ? '+' : MINUS) + text;
    }

    function plural(count, noun, many = `${noun}s`) {
        return `${format_count(count)} ${count === 1 ? noun : many}`;
    }

    function element(tag, class_name, text) {
        const node = document.createElement(tag);
        if (class_name) node.className = class_name;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    function stat_tile({ label, value, meta, title }) {
        const tile = element('div', 'stat-tile');
        const label_node = element('span', 'stat-label', label);
        if (title) label_node.title = title;
        tile.append(label_node, element('span', 'stat-value', value));
        if (meta) tile.append(element('span', 'stat-meta', meta));
        return tile;
    }

    function share_cell(share) {
        const cell = element('td', 'share-cell');
        const bar = element('span', 'share-bar');
        bar.setAttribute('aria-hidden', 'true');
        bar.style.setProperty('--share', is_number(share) ? share.toFixed(4) : '0');
        cell.append(bar, element('span', 'share-value', format_percent(share)));
        return cell;
    }

    function data_table(caption, columns, rows) {
        const wrap = element('div', 'table-wrap games-table');
        const table = element('table', 'stripes');
        table.append(element('caption', 'contribution-caption', caption));

        const head = element('tr', 'table-header');
        columns.forEach(column => {
            const th = element('th', column.numeric ? 'numeric' : null, column.label);
            th.scope = 'col';
            if (column.title) th.title = column.title;
            head.append(th);
        });
        const thead = element('thead');
        thead.append(head);

        const body = element('tbody');
        rows.forEach(row => {
            const tr = element('tr');
            columns.forEach(column => {
                const value = column.value(row);
                tr.append(value instanceof Node ? value : element('td', column.numeric ? 'numeric' : column.class_name, value));
            });
            body.append(tr);
        });

        table.append(thead, body);
        wrap.append(table);
        return wrap;
    }

    function outcome_bar(record) {
        const total = record.wins + record.draws + record.losses;
        const bar = element('span', 'outcome-bar');
        bar.setAttribute('aria-hidden', 'true');
        OUTCOME_KEYS.forEach(({ key, variant }) => {
            if (!record[key]) return;
            const segment = element('span', `outcome-segment outcome-${variant}`);
            segment.style.setProperty('--share', (record[key] / total).toFixed(4));
            bar.append(segment);
        });
        const cell = element('td', 'outcome-cell');
        cell.append(bar);
        return cell;
    }

    function colour_card(colour) {
        const rows = [
            { label: 'Dev as White', record: colour.dev_as_white },
            { label: 'Dev as Black', record: colour.dev_as_black },
            { label: 'White, either engine', record: colour.white },
        ];
        const table = data_table('Results by colour', [
            { label: 'Games as', value: row => row.label },
            { label: 'Wins', numeric: true, value: row => format_count(row.record.wins) },
            { label: 'Draws', numeric: true, value: row => format_count(row.record.draws) },
            { label: 'Losses', numeric: true, value: row => format_count(row.record.losses) },
            { label: 'Score', numeric: true, value: row => format_percent(row.record.score) },
            { label: 'Split', value: row => outcome_bar(row.record) },
        ], rows);
        const block = element('div', 'games-block');
        block.append(table, swatch_legend(OUTCOME_KEYS.map(({ label, variant }) => [label, `outcome-${variant}`])));
        return block;
    }

    function pairs_card(pairs) {
        const table = data_table('Pair outcomes for dev', [
            { label: 'Pair', value: row => row.label },
            { label: 'Bucket', title: 'The pentanomial bucket this outcome is counted in', value: row => row.bucket },
            { label: 'Pairs', numeric: true, value: row => format_count(pairs[row.key]) },
            { label: 'Share', value: row => share_cell(pairs.total ? pairs[row.key] / pairs.total : null) },
        ], PAIR_ROWS);
        const sweeps = `Same colour won both games: White ${format_count(pairs.white_sweeps)}, Black ${format_count(pairs.black_sweeps)}.`;
        const block = element('div', 'games-block');
        block.append(table, element('p', 'insights-note', sweeps));
        return block;
    }

    function termination_card(terminations, games) {
        const table = data_table('How games ended', [
            { label: 'Ending', value: row => row.label },
            { label: 'Games', numeric: true, value: row => format_count(row.games) },
            { label: 'Share', value: row => share_cell(row.share) },
        ], terminations.rows);
        const block = element('div', 'games-block');
        block.append(table);
        if (terminations.inferred_games)
            block.append(element('p', 'insights-note',
                `${format_count(terminations.inferred_games)} of ${format_count(games)} endings are inferred from the last move and the reported scores; uploaded PGNs do not carry the reason.`));
        return block;
    }

    function css_palette() {
        const style = getComputedStyle(document.documentElement);
        const read = name => style.getPropertyValue(name).trim();
        return {
            decisive: read('--series-1'),
            drawn: read('--series-2'),
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

    function length_config(lengths, palette) {
        const font = { family: palette.font };
        const bins = lengths.histogram;
        const dataset = (label, key, color) => ({
            label,
            data: bins.map(bin => bin[key]),
            backgroundColor: color,
            borderColor: palette.surface,
            borderWidth: { top: 1, right: 1, bottom: 0, left: 1 },
            borderRadius: 2,
            maxBarThickness: 24,
        });
        return {
            type: 'bar',
            data: {
                labels: bins.map(bin => bin.first_ply),
                datasets: [dataset('Decisive', 'decisive', palette.decisive), dataset('Drawn', 'drawn', palette.drawn)],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: false,
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
                        titleFont: { family: palette.font, weight: '600' },
                        bodyFont: { family: palette.font },
                        padding: 8,
                        cornerRadius: 6,
                        boxWidth: 8,
                        boxHeight: 8,
                        callbacks: {
                            title: items => {
                                const bin = items[0] && bins[items[0].dataIndex];
                                return bin ? `Plies ${bin.first_ply} to ${bin.last_ply}` : '';
                            },
                            label: item => `${item.dataset.label}: ${plural(item.raw, 'game')}`,
                        },
                    },
                },
                scales: {
                    x: {
                        stacked: true,
                        grid: { display: false },
                        border: { color: palette.axis },
                        ticks: { color: palette.muted, font, maxRotation: 0, autoSkip: true, maxTicksLimit: 8 },
                        title: { display: true, text: 'plies, from', color: palette.muted, font, padding: { top: 4 } },
                    },
                    y: {
                        stacked: true,
                        beginAtZero: true,
                        grid: { color: palette.grid, drawTicks: false },
                        border: { color: palette.axis },
                        ticks: { color: palette.muted, font, padding: 6, maxTicksLimit: 6, precision: 0, callback: format_compact },
                    },
                },
            },
        };
    }

    function length_label(lengths) {
        const { all, decisive, drawn } = lengths;
        return `Game length histogram. Median ${format_fixed(all.median, 0)} plies over ${plural(all.games, 'game')}; ` +
            `decisive games median ${format_fixed(decisive.median, 0)}, drawn games median ${format_fixed(drawn.median, 0)}.`;
    }

    function swatch_legend(entries) {
        const legend = element('p', 'chart-legend');
        entries.forEach(([label, variant]) => {
            const item = element('span', 'chart-legend-item');
            item.append(element('span', `chart-legend-swatch ${variant}`), element('span', null, label));
            legend.append(item);
        });
        return legend;
    }

    function length_table(lengths) {
        const rows = [
            { label: 'All games', summary: lengths.all },
            { label: 'Decisive', summary: lengths.decisive },
            { label: 'Drawn', summary: lengths.drawn },
        ];
        const table = data_table('Game length in plies', [
            { label: 'Games', value: row => row.label },
            { label: 'Count', numeric: true, value: row => format_count(row.summary.games) },
            { label: 'Lower quartile', numeric: true, value: row => format_fixed(row.summary.q1, 0) },
            { label: 'Median', numeric: true, value: row => format_fixed(row.summary.median, 0) },
            { label: 'Upper quartile', numeric: true, value: row => format_fixed(row.summary.q3, 0) },
            { label: 'Mean', numeric: true, value: row => format_fixed(row.summary.mean) },
            { label: 'Longest', numeric: true, value: row => format_count(row.summary.longest) },
        ], rows);
        table.id = 'games-length-table';
        return table;
    }

    class LengthChart {
        constructor() {
            this.card = element('figure', 'card chart-card games-length');
            const caption = element('figcaption', 'chart-caption');
            const subtitle = element('span', 'chart-subtitle', '');
            subtitle.id = 'games-length-description';
            this.subtitle = subtitle;
            caption.append(element('span', 'card-title', 'Game length'), subtitle);
            this.box = element('div', 'chart-box');
            this.canvas = element('canvas');
            this.canvas.setAttribute('role', 'img');
            this.canvas.setAttribute('aria-describedby', subtitle.id);
            this.canvas.setAttribute('aria-details', 'games-length-table');
            this.box.append(this.canvas);
            this.card.append(caption, swatch_legend([['Decisive', 'series-decisive'], ['Drawn', 'series-drawn']]), this.box);
            this.chart = null;
        }

        render(lengths) {
            this.subtitle.textContent = `Games per ${lengths.bin_plies}-ply bin, decisive and drawn stacked`;
            this.canvas.setAttribute('aria-label', length_label(lengths));
            if (typeof window.Chart === 'undefined') {
                this.box.hidden = true;
                return;
            }
            const config = length_config(lengths, css_palette());
            if (this.chart) {
                this.chart.data = config.data;
                this.chart.options = config.options;
                this.chart.update('none');
            } else {
                this.chart = new window.Chart(this.canvas, config);
            }
        }
    }

    function opening_cell(row) {
        return element('td', 'mono games-opening', row.opening);
    }

    function openings_block(openings) {
        const block = element('div', 'games-block');
        const pair_columns = ['ww', 'wd', 'wl', 'dd', 'dl', 'll'].map(key => ({
            label: key.toUpperCase(), numeric: true, value: row => format_count(row[key]),
        }));

        if (openings.lopsided.length)
            block.append(data_table('Openings with the most one-sided results for dev', [
                { label: 'Opening', value: opening_cell },
                { label: 'Pairs', numeric: true, value: row => format_count(row.pairs) },
                { label: 'Dev score', numeric: true, value: row => format_percent(row.dev_score) },
                ...pair_columns,
            ], openings.lopsided));

        if (openings.colour_bound.length)
            block.append(data_table('Openings where one colour won both games', [
                { label: 'Opening', value: opening_cell },
                { label: 'Pairs', numeric: true, value: row => format_count(row.pairs) },
                { label: 'White won both', numeric: true, value: row => format_count(row.white_sweeps) },
                { label: 'Black won both', numeric: true, value: row => format_count(row.black_sweeps) },
            ], openings.colour_bound));

        if (openings.drawn.length)
            block.append(data_table('Openings that drew every game', [
                { label: 'Opening', value: opening_cell },
                { label: 'Pairs', numeric: true, value: row => format_count(row.pairs) },
            ], openings.drawn));

        block.append(element('p', 'insights-note',
            `${plural(openings.tracked, 'opening')} played, ${format_count(openings.repeated)} of them in two or more pairs; ${format_count(openings.always_drawn)} of those drew every game. The tables rank openings played in two or more pairs and list at most ten each.`));
        return block;
    }

    function advantage_table(evals) {
        const rows = evals.advantage.filter(row => row.reached > 0);
        if (!rows.length) return null;
        return data_table('Advantages by own evaluation that were not converted', [
            { label: 'Engine', value: row => SIDE_LABELS[row.side] ?? row.side },
            { label: 'Reached', value: row => `≥ ${format_pawns(row.threshold_cp)}`, class_name: 'mono' },
            { label: 'Games', numeric: true, value: row => format_count(row.reached) },
            { label: 'Won', numeric: true, value: row => format_count(row.won) },
            { label: 'Drawn', numeric: true, value: row => format_count(row.drawn) },
            { label: 'Lost', numeric: true, value: row => format_count(row.lost) },
            { label: 'Not won', value: row => share_cell(row.not_won_share) },
        ], rows);
    }

    function phase_table(evals) {
        const rows = evals.phases.filter(row => row.dev.moves || row.base.moves);
        if (!rows.length) return null;
        const columns = [
            { label: 'Phase', value: row => row.label },
            { label: 'Dev depth', numeric: true, value: row => format_fixed(row.dev.mean_depth) },
            { label: 'Base depth', numeric: true, value: row => format_fixed(row.base.mean_depth) },
        ];
        if (evals.has_timing)
            columns.push(
                { label: 'Dev NPS', numeric: true, value: row => format_compact(row.dev.nps) },
                { label: 'Base NPS', numeric: true, value: row => format_compact(row.base.nps) },
                { label: 'Dev ms / move', numeric: true, value: row => format_fixed(row.dev.mean_time_ms, 0) },
                { label: 'Base ms / move', numeric: true, value: row => format_fixed(row.base.mean_time_ms, 0) },
            );
        return data_table('Search by game phase', columns, rows);
    }

    function evals_block(evals) {
        const block = element('div', 'games-block');
        block.append(...[advantage_table(evals), phase_table(evals)].filter(Boolean));
        return block;
    }

    function summary_tiles(report) {
        const { colour, pairs, lengths, evals } = report;
        const tiles = [
            stat_tile({
                label: 'Games analysed', value: format_count(report.games),
                meta: `${plural(pairs.total, 'pair')} · ${plural(report.limits.members, 'batch', 'batches')}`,
            }),
            stat_tile({
                label: 'White score', value: format_percent(colour.white.score),
                meta: `dev ${format_percent(colour.dev_as_white.score)} as White · ${format_percent(colour.dev_as_black.score)} as Black`,
                title: 'Score of the White side over every game, whichever engine played it',
            }),
            stat_tile({
                label: 'Split pairs', value: format_percent(pairs.middle_wl_share),
                meta: `${format_count(pairs.wl)} won and lost · ${format_count(pairs.dd)} drew both`,
                title: 'Share of the middle pentanomial bucket that is a win and a loss rather than two draws',
            }),
            stat_tile({
                label: 'Median length', value: `${format_fixed(lengths.all.median, 0)} plies`,
                meta: `decisive ${format_fixed(lengths.decisive.median, 0)} · drawn ${format_fixed(lengths.drawn.median, 0)}`,
            }),
        ];
        if (evals)
            tiles.push(stat_tile({
                label: 'Book balance', value: format_pawns(evals.book.mean_white_cp),
                meta: `mean size ${format_pawns(evals.book.mean_abs_cp).replace('+', '')} · ${plural(evals.book.games, 'game')}`,
                title: 'Mean first reported evaluation of each game, from White\'s side',
            }));
        return tiles;
    }

    function limits_note(limits) {
        const notes = [];
        if (!limits.complete)
            notes.push(`still reading the archive (${format_percent(limits.archive_bytes ? limits.analysed_bytes / limits.archive_bytes : null, 0)})`);
        if (limits.damaged_members) notes.push(`${plural(limits.damaged_members, 'damaged batch', 'damaged batches')} cut short`);
        if (limits.malformed_games) notes.push(`${plural(limits.malformed_games, 'malformed game')} skipped`);
        if (limits.unfinished_games) notes.push(`${plural(limits.unfinished_games, 'unfinished game')} left out`);
        if (limits.unpaired_games) notes.push(`${plural(limits.unpaired_games, 'game')} without a partner`);
        if (limits.untracked_opening_pairs) notes.push(`${plural(limits.untracked_opening_pairs, 'pair')} beyond the opening limit`);
        return notes.length ? `Partial: ${notes.join('; ')}.` : '';
    }

    class GameInsights {
        constructor(container) {
            this.container = container;
            this.url = `/api/workload/${encodeURIComponent(container.dataset.workloadId)}/games/`;
            this.chart = null;
            this.latest = null;
            this.catch_ups = 0;
            this.timer = null;
            this.stale = false;
        }

        start() {
            this.refresh();
            document.addEventListener('visibilitychange', () => {
                if (document.hidden || !this.stale) return;
                this.stale = false;
                this.refresh();
            });
            const repaint = () => this.latest && this.chart && this.chart.render(this.latest.lengths);
            new MutationObserver(repaint).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
            window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', repaint);
        }

        async fetch() {
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
            try {
                const response = await fetch(this.url, {
                    credentials: 'same-origin', headers: { Accept: 'application/json' }, signal: controller.signal,
                });
                const data = response.ok ? await response.json() : null;
                return data && data.games ? data.games : null;
            } catch (err) {
                return null;
            } finally {
                clearTimeout(timer);
            }
        }

        async refresh() {
            const games = await this.fetch();
            if (games && games.status === 'ready' && games.report.games > 0) this.render(games.report);
            this.schedule(games);
        }

        schedule(games) {
            clearTimeout(this.timer);
            const catching_up = Boolean(games && games.report && !games.report.limits.complete) && this.catch_ups < MAX_CATCH_UPS;
            if (catching_up) this.catch_ups += 1;
            if (!catching_up && !(games && games.active)) return;
            this.timer = setTimeout(() => {
                if (document.hidden) this.stale = true;
                else this.refresh();
            }, catching_up ? CATCH_UP_MS : REFRESH_MS);
        }

        render(report) {
            this.latest = report;
            if (!this.chart) this.chart = new LengthChart();

            const group = element('div', 'insights-group');
            const tiles = element('div', 'stat-tiles');
            tiles.append(...summary_tiles(report));
            group.append(element('h3', 'insights-group-title', 'Games'), tiles);

            const note = limits_note(report.limits);
            if (note) group.append(element('p', 'insights-note', note));

            const grid = element('div', 'games-grid');
            grid.append(colour_card(report.colour), pairs_card(report.pairs),
                termination_card(report.terminations, report.games));

            const lengths = element('div', 'games-grid');
            lengths.append(this.chart.card, length_table(report.lengths));

            this.container.replaceChildren(group, grid, lengths, openings_block(report.openings));
            if (report.evals) this.container.append(evals_block(report.evals));
            this.container.hidden = false;
            this.chart.render(report.lengths);
        }
    }

    document.addEventListener('DOMContentLoaded', () => {
        const container = document.querySelector('[data-games-insights]');
        if (container) new GameInsights(container).start();
    });

})();
