(() => {
    'use strict';

    const REFRESH_MS = 60_000;
    const LIVE_REFRESH_GAP_MS = 10_000;
    const FETCH_TIMEOUT_MS = 20_000;
    const DASH = '—';
    const MINUS = '−';
    const ELO_RANGE_WARMUP = 0.1;
    const MAX_TICKS = 6;
    const MAX_BACKOFF_MS = 8 * REFRESH_MS;
    const MAX_CLIENT_ERRORS = 3;
    const POLLED_STATUSES = new Set(['pending', 'active']);
    const WORKLOAD_EVENT = 'openbench:workload-change';
    const LISTING_EVENT = 'openbench:listing-change';
    const ETA_REASONS = {
        too_few_games: 'needs 200 games first',
        outside_bounds: 'LLR is outside the bounds',
        empty_outcome: 'needs wins, draws and losses',
        no_variance: 'results too uniform to project',
        no_target: 'no target to reach',
        no_rate: 'no recent throughput',
    };
    const ESTIMATE_NOTE = 'Assumes the test keeps producing results like it has so far; an order of magnitude, not a promise.';
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
            title: estimate ? ESTIMATE_NOTE : undefined,
            value,
            meta: eta_meta(insights),
        });
    }

    function llr_meter(progress) {
        const span = progress.llr_upper - progress.llr_lower;
        if (!is_number(progress.llr) || !(span > 0)) return null;
        const label = `LLR ${format_fixed(progress.llr)} between ${format_fixed(progress.llr_lower)} and ${format_fixed(progress.llr_upper)}`;
        return meter((progress.llr - progress.llr_lower) / span, 'position', label);
    }

    function games_meter(progress) {
        if (!is_number(progress.fraction)) return null;
        const label = `${format_count(progress.games)} of ${format_count(progress.target_games)} games`;
        return meter(progress.fraction, 'fill', label);
    }

    function captioned(caption, meter_node) {
        return meter_node ? [element('span', 'summary-meter-caption', caption), meter_node] : null;
    }

    function summary_meter(insights) {
        if (insights.workload.status !== 'active') return [];
        return captioned('LLR', llr_meter(insights.progress)) ?? captioned('Games', games_meter(insights.progress)) ?? [];
    }

    function time_left_part(eta) {
        if (is_number(eta.remaining_seconds)) {
            const estimate = eta.kind === 'sprt_estimate';
            const part = element('span', 'row-timing-left', `${estimate ? '≈ ' : ''}${format_duration(eta.remaining_seconds)} left`);
            if (estimate) part.title = ESTIMATE_NOTE;
            return part;
        }
        if (eta.reason && eta.reason !== 'no_target')
            return element('span', 'row-timing-unavailable', ETA_REASONS[eta.reason] ?? 'not available');
        return null;
    }

    function rate_part(timing) {
        const rate = timing && (timing.recent ?? timing.overall);
        if (!rate || !(rate.games_per_hour > 0)) return null;
        const part = element('span', null, `${format_rate(rate.games_per_hour)} games/h`);
        part.title = `Games per hour, ${timing.recent ? `last ${format_duration(rate.window_seconds)}` : 'overall'}`;
        return part;
    }

    function summary_timing(insights) {
        const parts = [time_left_part(insights.eta), rate_part(insights.timing)].filter(Boolean);
        if (!parts.length) return null;
        const line = element('div', 'row-timing');
        line.append(...parts.flatMap((part, index) => (index ? [' · ', part] : [part])));
        return line;
    }

    function render_summary(view, insights) {
        const active = insights.workload.status === 'active';
        view.meter.replaceChildren(...summary_meter(insights));
        if (!active) {
            if (view.timing.querySelector('.row-timing-left, .row-timing-unavailable')) view.timing.replaceChildren();
            return;
        }
        const timing = summary_timing(insights);
        view.timing.replaceChildren(...(timing ? [timing] : []));
    }

    function render_verdict(view, insights) {
        if (!view.verdict) return;
        const results = insights.progress.games > 0 ? insights.results : null;
        view.verdict.hidden = !results;
        if (results) {
            const { verdict } = results;
            view.verdict.className = `results-verdict results-verdict-${verdict.tone}`;
            view.text.textContent = verdict.text;
        }

        const open = view.forecast.querySelector('details')?.open ?? false;
        const note = results && results.outlook ? outlook_note(results.outlook) : null;
        if (note) note.open = open;
        view.forecast.replaceChildren(...(note ? [note] : []));
    }

    function strength_tiles(strength) {
        return [
            stat_tile({
                label: 'Normalized Elo', value: format_interval(strength.normalized_elo), meta: format_bounds(strength.normalized_elo),
                title: 'Elo scaled by the per-pair spread; the scale the pentanomial SPRT uses.',
            }),
            stat_tile({
                label: 'LOS', value: format_percent(strength.los), meta: 'chance dev is stronger',
                title: 'Likelihood of superiority',
            }),
            stat_tile({ label: 'Draw ratio', value: format_percent(strength.draw_ratio), meta: 'of all games' }),
        ];
    }

    function render_tiles(container, insights) {
        const { timing } = insights;
        container.replaceChildren(...(timing ? [elapsed_tile(timing), rate_tile(timing), eta_tile(insights)] : []));
    }

    function show_section(id, shown) {
        document.querySelectorAll(`[data-section="${id}"], [data-section-link="${id}"]`).forEach(node => {
            node.hidden = !shown;
        });
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
        const link = element('a', null, row.machine_label || 'Unnamed machine');
        link.href = `/machines/${encodeURIComponent(row.machine_id)}/`;
        if (row.machine_name && row.machine_name !== row.machine_label) link.title = row.machine_name;
        const sessions = row.registrations.length;
        const pool = row.machine_name && row.pool !== row.machine_name ? row.pool : null;
        const detail = [row.owner, pool, sessions > 1 ? `${sessions} sessions` : null].filter(Boolean).join(' · ');
        cell.append(link, element('span', 'contribution-sub', detail));
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

    const PENTA_OUTCOMES = [
        { label: 'LL', detail: 'lost both', tone: 'loss' },
        { label: 'LD', detail: 'loss and draw', tone: 'loss-soft' },
        { label: 'DD / WL', detail: 'level pair', tone: 'level' },
        { label: 'WD', detail: 'win and draw', tone: 'win-soft' },
        { label: 'WW', detail: 'won both', tone: 'win' },
    ];
    const TRI_OUTCOMES = [
        { label: 'Losses', detail: '', tone: 'loss' },
        { label: 'Draws', detail: '', tone: 'level' },
        { label: 'Wins', detail: '', tone: 'win' },
    ];
    const OUTLOOK_CAVEAT = 'A forecast, not a promise. It averages the SPRT’s chance to pass over every strength the games so far allow, '
        + 'starting from the assumption that a patch is worth about nothing (0 ± {sd} normalized Elo, the weight of {games} games showing no difference). '
        + 'Early on that assumption decides the number; the games take over as they accumulate. '
        + 'Tests usually run a little longer than forecast, because results arrive in batches.';

    function format_ratio_percent(fraction, digits = 1) {
        return is_number(fraction) ? `${format_signed(100 * fraction, digits)}%` : DASH;
    }

    function format_probability(probability) {
        if (!is_number(probability)) return DASH;
        if (probability > 0.99) return '> 99%';
        if (probability < 0.01) return '< 1%';
        return format_percent(probability, 0);
    }

    function format_p(value) {
        if (!is_number(value)) return DASH;
        return value < 0.001 ? '< 0.001' : format_fixed(value, 3);
    }

    function badge(text, variant) {
        return element('span', `badge badge-${variant}`, text);
    }

    function outlook_note(outlook) {
        const details = element('details', 'insights-details');
        const text = OUTLOOK_CAVEAT
            .replace('{sd}', format_fixed(outlook.prior.sd_elo, 1))
            .replace('{games}', format_count(outlook.prior.equivalent_games));
        details.append(element('summary', null, 'About the forecast'), element('p', null, text));
        return details;
    }

    function variance_tile(variance) {
        return stat_tile({
            label: 'Pair variance', value: format_ratio_percent(variance.ratio - 1, 0),
            meta: `vs independent games · one pair is worth ${format_fixed(variance.pair_efficiency)} independent pairs`,
            title: 'Observed per-pair score variance against the variance of two independent games. Below zero, playing each opening with both colours is cancelling opening bias.',
        });
    }

    function decisive_tile(outcomes) {
        const swept = is_number(outcomes.swept_pair_rate) ? ` · LL+WW pairs ${format_percent(outcomes.swept_pair_rate)}` : '';
        return stat_tile({
            label: 'Decisive games', value: format_percent(outcomes.decisive_game_rate),
            meta: `1 in ${format_fixed(outcomes.games_per_decisive, 1)} games${swept}`,
        });
    }

    function spread_text(spread) {
        if (!spread) return 'one host, noise unknown';
        return `${spread.beyond_noise ? 'beyond' : 'within'} noise across ${format_count(spread.hosts)} hosts`;
    }

    function spread_title(spread) {
        return spread
            ? `Mean over hosts, 95% interval ${format_ratio_percent(spread.lower)} to ${format_ratio_percent(spread.upper)}.`
            : 'Needs two hosts to judge the noise.';
    }

    function speed_tile(speed) {
        const { difference, speed: pooled, spread } = speed.overall;
        return stat_tile({
            label: 'Dev search speed', value: format_ratio_percent(difference),
            meta: `${format_compact(pooled.dev_nps)} vs ${format_compact(pooled.base_nps)} nps · ${spread_text(spread)}`,
            title: `Nodes per second of dev relative to base, from the games workers have reported in full. ${spread_title(spread)}`,
        });
    }

    function result_tiles(results, strength) {
        const tiles = strength_tiles(strength);
        if (results.outcomes.pair_variance) tiles.push(variance_tile(results.outcomes.pair_variance));
        if (is_number(results.outcomes.games_per_decisive)) tiles.push(decisive_tile(results.outcomes));
        if (results.speed) tiles.push(speed_tile(results.speed));
        return tiles;
    }

    function table_shell(caption, headers, numeric_from) {
        const wrap = element('div', 'table-wrap contribution-table');
        const table = element('table', 'stripes');
        table.append(element('caption', 'contribution-caption', caption));
        const head = element('tr', 'table-header');
        headers.forEach((text, index) => head.append(element('th', index >= numeric_from ? 'numeric' : null, text)));
        const thead = element('thead');
        thead.append(head);
        const body = element('tbody');
        table.append(thead, body);
        wrap.append(table);
        return { wrap, body };
    }

    function outcome_cell(outcome) {
        const cell = element('td', 'contribution-name results-outcome-name');
        cell.append(element('span', null, outcome.label));
        if (outcome.detail) cell.append(element('span', 'contribution-sub', outcome.detail));
        return cell;
    }

    function outcome_share_cell(fraction, widest, tone) {
        const cell = element('td', 'share-cell');
        const bar = element('span', `share-bar results-outcome-bar results-outcome-bar-${tone}`);
        bar.setAttribute('aria-hidden', 'true');
        bar.style.setProperty('--share', widest > 0 ? (fraction / widest).toFixed(4) : '0');
        cell.append(bar, element('span', 'share-value', format_percent(fraction)));
        return cell;
    }

    function outcome_table(results, insights) {
        const penta = insights.workload.use_penta && results.outcomes.pentanomial_fractions;
        const fractions = penta ? results.outcomes.pentanomial_fractions : results.outcomes.trinomial_fractions;
        if (!fractions) return null;

        const counts = penta ? insights.progress.pentanomial : insights.progress.trinomial;
        const { wrap, body } = table_shell(
            penta ? 'Pair outcomes' : 'Game outcomes', ['Outcome', 'Share', penta ? 'Pairs' : 'Games'], 2);
        const widest = Math.max(...fractions);
        (penta ? PENTA_OUTCOMES : TRI_OUTCOMES).forEach((outcome, index) => {
            const tr = element('tr');
            tr.append(outcome_cell(outcome), outcome_share_cell(fractions[index], widest, outcome.tone),
                numeric_cell(format_count(counts[index])));
            body.append(tr);
        });
        return wrap;
    }

    function host_label(host) {
        const cell = element('td', 'contribution-name');
        const link = element('a', null, host.machine_label || `${host.owner}’s machine`);
        if (host.machine_name) link.title = host.machine_name;
        link.href = `/machines/${encodeURIComponent(host.machine_id)}/`;
        const rows = host.machines > 1 ? ` · ${host.machines} registrations` : '';
        cell.append(link, element('span', 'contribution-sub', `${host.owner} · ${host.cpu_name}${rows}`));
        return cell;
    }

    function cpu_group_label(cpu) {
        const cell = element('td', 'contribution-name');
        const hosts = `${format_count(cpu.hosts)} ${cpu.hosts === 1 ? 'host' : 'hosts'}`;
        cell.append(element('span', null, cpu.cpu_name), element('span', 'contribution-sub', hosts));
        return cell;
    }

    function flagged_cell(text, flagged, flag_text) {
        const cell = numeric_cell(text);
        if (flagged) cell.append(' ', badge(flag_text, 'warn'));
        return cell;
    }

    function deviation_cell(deviation) {
        if (!deviation) return numeric_cell(DASH);
        const cell = flagged_cell(`${format_signed(deviation.z_score)} σ`, deviation.flagged, 'deviates');
        cell.append(element('span', 'contribution-sub', `adjusted p ${format_p(deviation.adjusted_p_value)}`));
        return cell;
    }

    function fault_cell(count, rate_value, flagged) {
        const text = count ? `${format_count(count)} (${format_percent(rate_value, 2)})` : '0';
        return flagged_cell(text, flagged, 'high');
    }

    function speed_cell(stats, reading) {
        if (!reading) return numeric_cell(format_ratio_percent(stats.speed.difference));
        const cell = numeric_cell(format_ratio_percent(reading.difference));
        const noise = reading.spread ? `${reading.spread.beyond_noise ? 'beyond' : 'within'} noise` : 'one host';
        cell.append(element('span', 'contribution-sub', noise));
        return cell;
    }

    function consistency_cells(stats, reading) {
        return [
            numeric_cell(format_interval(stats.elo)),
            deviation_cell(stats.deviation), fault_cell(stats.crashes, stats.crash_rate, stats.crash_flagged),
            fault_cell(stats.timelosses, stats.timeloss_rate, stats.timeloss_flagged), speed_cell(stats, reading),
        ];
    }

    const CONSISTENCY_HEADERS = ['Elo', 'Against the rest', 'Crashes', 'Time losses', 'Dev speed'];

    function cpu_table(results, contributions) {
        const readings = new Map((results.speed ? results.speed.cpus : []).map(cpu => [cpu.cpu_name, cpu.reading]));
        const shares = new Map(contributions.map(cpu => [cpu.cpu_name, cpu.stats]));
        const { wrap, body } = table_shell('By CPU', ['CPU', 'Share', 'Games', 'Pairs / h', ...CONSISTENCY_HEADERS], 2);
        results.consistency.cpus.forEach(cpu => {
            const share = shares.get(cpu.cpu_name);
            const tr = element('tr');
            tr.append(cpu_group_label(cpu), share_cell(share ? share.share : null),
                numeric_cell(format_count(cpu.stats.games)), numeric_cell(share ? format_rate(share.pairs_per_hour) : DASH),
                ...consistency_cells(cpu.stats, readings.get(cpu.cpu_name)));
            body.append(tr);
        });
        return wrap;
    }

    function flagged_host_table(hosts) {
        const { wrap, body } = table_shell('Hosts that stand out', ['Host', 'Games', ...CONSISTENCY_HEADERS], 1);
        hosts.forEach(host => {
            const tr = element('tr');
            tr.append(host_label(host), numeric_cell(format_count(host.stats.games)), ...consistency_cells(host.stats));
            body.append(tr);
        });
        return wrap;
    }

    function heterogeneity_note(consistency) {
        const test = consistency.heterogeneity;
        if (!test) return `CPUs are compared once two have ${format_count(consistency.min_samples)} results each`;
        const p = `χ² p ${format_p(test.p_value)}`;
        return test.flagged ? `CPUs disagree more than chance explains (${p})` : `CPUs agree within chance (${p})`;
    }

    function host_note(hosts) {
        const total = `${format_count(hosts.total)} ${hosts.total === 1 ? 'host' : 'hosts'}`;
        return hosts.flagged.length ? `${format_count(hosts.flagged.length)} of ${total} flagged` : `${total}, none stands out`;
    }

    function games_section() {
        return document.querySelector('[data-games-insights]');
    }

    function games_shown() {
        const section = games_section();
        return Boolean(section) && !section.hidden;
    }

    function games_pointer() {
        const note = element('p', 'insights-note');
        const link = element('a', null, 'Games');
        link.href = '#games';
        note.append('Pair outcomes, with the level bucket split into two draws and a win with a loss, are under ', link, '.');
        return note;
    }

    function render_results(container, insights) {
        const { results, strength } = insights;
        if (!results || !strength || !insights.progress.games) return container.replaceChildren();

        const tiles = element('div', 'stat-tiles');
        tiles.append(...result_tiles(results, strength));
        if (games_shown()) return container.replaceChildren(tiles, games_pointer());

        const grid = element('div', 'results-grid');
        const outcomes = outcome_table(results, insights);
        if (outcomes) grid.append(outcomes);
        container.replaceChildren(tiles, grid);
    }

    function workers_note(results) {
        if (!results || !results.consistency.cpus.length) return '';
        return `${heterogeneity_note(results.consistency)} · ${host_note(results.consistency.hosts)}`;
    }

    function render_workers(container, note, insights) {
        const { cpus, machines } = insights.contributions;
        const { results } = insights;
        const with_elo = insights.strength !== null;
        note.textContent = workers_note(results);
        if (!machines.length) return container.replaceChildren();

        const tables = [results && results.consistency.cpus.length
            ? cpu_table(results, cpus)
            : contribution_table('By CPU', 'CPU', cpus, cpu_label, with_elo)];
        if (results && results.consistency.hosts.flagged.length) tables.push(flagged_host_table(results.consistency.hosts.flagged));
        tables.push(contribution_table('By machine', 'Machine', machines, machine_label, with_elo));
        container.replaceChildren(...tables, element('p', 'insights-note', 'Pairs per hour average over the whole elapsed time.'));
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
        const subtitle = element('span', 'chart-subtitle', spec.subtitle);
        subtitle.id = `insights-${spec.key}-description`;
        header.append(element('span', 'card-title', spec.title), subtitle);

        const box = element('div', 'chart-box');
        const canvas = element('canvas');
        canvas.setAttribute('role', 'img');
        canvas.setAttribute('aria-describedby', subtitle.id);
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
            this.announcer = section.querySelector('[data-insights-announcer]');
            this.announced_status = null;
            this.error = section.querySelector('[data-insights-error]');
            this.evidence = section.querySelector('[data-insights-evidence]');
            this.summary = {
                verdict: section.querySelector('[data-insights-verdict]'),
                text: section.querySelector('[data-verdict-text]'),
                forecast: section.querySelector('[data-insights-forecast]'),
                meter: section.querySelector('[data-summary-meter]'),
                timing: section.querySelector('[data-summary-timing]'),
            };
            this.workers_note = section.querySelector('[data-insights-workers-note]');
            this.tiles = section.querySelector('[data-insights-tiles]');
            this.charts = section.querySelector('[data-insights-charts]');
            this.results = section.querySelector('[data-insights-results]');
            this.contributions = section.querySelector('[data-insights-contributions]');
            this.panels = null;
            this.latest = null;
            this.timer = null;
            this.stale = false;
            this.failures = 0;
            this.client_failures = 0;
            this.refreshing = false;
            this.refreshed_at = 0;
            this.trailing = null;
        }

        start() {
            this.refresh();
            document.addEventListener('visibilitychange', () => this.on_visibility());
            document.addEventListener(WORKLOAD_EVENT, () => this.on_live_change());
            watch_theme(() => this.render_charts(true));
            this.watch_games();
        }

        watch_games() {
            const section = games_section();
            if (!section) return;
            new MutationObserver(() => {
                show_section('games', games_shown());
                if (this.latest && this.results) render_results(this.results, this.latest);
            }).observe(section, { attributes: true, attributeFilter: ['hidden'] });
        }

        on_live_change() {
            if (this.trailing) return;
            const wait = this.refreshed_at + LIVE_REFRESH_GAP_MS - Date.now();
            if (!this.refreshing && wait <= 0) return void this.refresh();
            this.trailing = setTimeout(() => {
                this.trailing = null;
                if (this.refreshing) this.on_live_change();
                else this.refresh();
            }, Math.max(wait, 1_000));
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
            this.refreshing = true;
            this.evidence.setAttribute('aria-busy', 'true');
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
                this.refreshing = false;
                this.refreshed_at = Date.now();
                this.evidence.removeAttribute('aria-busy');
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
            const played = this.latest.progress.games > 0;
            show_section('results', played && Boolean(this.latest.results));
            show_section('progress', played && Boolean(this.latest.timing));
            show_section('workers', played);
            render_summary(this.summary, this.latest);
            render_verdict(this.summary, this.latest);
            render_tiles(this.tiles, this.latest);
            if (this.results) render_results(this.results, this.latest);
            this.render_history();
            render_workers(this.contributions, this.workers_note, this.latest);

            const refreshing = this.polled ? ' · refreshes every minute' : '';
            this.status.textContent = `Updated ${time_format.format(new Date())}${refreshing}`;
            this.announce_status_change();
        }

        announce_status_change() {
            const status = this.latest.workload.status;
            if (!this.announcer || status === this.announced_status) return;
            this.announcer.textContent = this.announced_status === null
                ? 'Insights loaded'
                : `Workload is now ${status}`;
            this.announced_status = status;
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
            outcomes: [
                `${format_count(finished.passed)} passed`,
                `${format_count(finished.failed)} failed`,
                ...(finished.completed ? [`${format_count(finished.completed)} completed`] : []),
                `${format_count(finished.stopped)} stopped`,
            ].join(' · '),
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

    function keep_server_stats(section) {
        let refreshed_at = Date.now();
        init_server_stats(section);
        document.addEventListener(LISTING_EVENT, () => {
            if (Date.now() - refreshed_at < REFRESH_MS) return;
            refreshed_at = Date.now();
            init_server_stats(section);
        });
    }

    document.addEventListener('DOMContentLoaded', () => {
        const workload = document.querySelector('[data-workload-insights]');
        if (workload) new WorkloadInsights(workload).start();

        const server = document.querySelector('[data-server-insights]');
        if (server) keep_server_stats(server);
    });

})();
