(() => {
    'use strict';

    const POLL_MS = 15_000;
    const HIDDEN_POLL_MS = 60_000;
    const MAX_BACKOFF_MS = 240_000;
    const FETCH_TIMEOUT_MS = 10_000;
    const HIGHLIGHT_MS = 2_500;
    const TICK_MS = 1_000;
    const JUST_NOW_SECONDS = 5;
    const MAX_CLIENT_ERRORS = 3;
    const MAX_WATCHED = 50;
    const WATCH_KEY = 'openbench-live-watch';
    const WORKLOAD_EVENT = 'openbench:workload-change';
    const LISTING_EVENT = 'openbench:listing-change';
    const RUNNING = new Set(['pending', 'active']);
    const TITLE_MARKS = {
        passed: '✓ Passed',
        failed: '✗ Failed',
        completed: '✓ Completed',
        stopped: '■ Stopped',
        deleted: '■ Deleted',
    };

    class FetchError extends Error {
        constructor(message, client) {
            super(message);
            this.name = 'FetchError';
            this.client = client;
        }
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
            if (!response.ok || !data || data.error)
                throw new FetchError(`HTTP ${response.status}`, response.status >= 400 && response.status < 500);
            return data;
        } finally {
            clearTimeout(timer);
        }
    }

    function element(tag, class_name, text) {
        const node = document.createElement(tag);
        if (class_name) node.className = class_name;
        if (text !== undefined) node.textContent = text;
        return node;
    }

    function hidden_text(text) {
        return element('span', 'visually-hidden', text);
    }

    function unspoken_text(text) {
        const node = element('span', '', text);
        node.setAttribute('aria-hidden', 'true');
        return node;
    }

    function with_breaks(lines) {
        return lines.flatMap((line, index) => index ? [document.createElement('br'), line] : [line]);
    }

    function plural(count, noun) {
        return `${count} ${noun}${count === 1 ? '' : 's'}`;
    }

    function format_age(seconds) {
        if (seconds < JUST_NOW_SECONDS) return 'just now';
        if (seconds < 60) return `${Math.floor(seconds)} s ago`;
        return `${Math.floor(seconds / 60)} min ago`;
    }

    function flash(node) {
        node.classList.add('live-changed');
        setTimeout(() => node.classList.remove('live-changed'), HIGHLIGHT_MS);
    }

    function same_origin_href(href) {
        const url = new URL(href, window.location.href);
        return url.origin === window.location.origin ? url.href : null;
    }

    class LiveStatus {
        constructor(root) {
            this.root = root;
            this.text = root.querySelector('[data-live-text]');
            this.reload = root.querySelector('[data-live-reload]');
            this.announcer = root.querySelector('[data-live-announcer]');
            this.state = 'live';
            this.checked_at = Date.now();
            this.notice = null;
            this.detail = null;
            this.retry_at = null;
        }

        start() {
            this.root.hidden = false;
            this.reload.addEventListener('click', () => window.location.reload());
            setInterval(() => { if (!document.hidden) this.render(); }, TICK_MS);
            this.render();
        }

        checked() {
            const recovered = this.state === 'retrying';
            this.state = 'live';
            this.checked_at = Date.now();
            this.detail = null;
            this.retry_at = null;
            if (recovered) this.announce('Live updates resumed');
            this.render();
        }

        retrying(delay_ms) {
            if (this.state !== 'retrying') this.announce('Live updates interrupted, retrying');
            this.state = 'retrying';
            this.retry_at = Date.now() + delay_ms;
            this.render();
        }

        stop(detail) {
            this.state = 'stopped';
            this.retry_at = null;
            this.detail = detail;
            this.render();
        }

        prompt(notice, action) {
            if (notice === this.notice) return;
            this.notice = notice;
            this.reload.textContent = action;
            this.reload.hidden = false;
            this.announce(`${notice}. ${action} to see it.`);
            this.render();
        }

        announce(message) {
            this.announcer.textContent = message;
        }

        progress() {
            if (this.retry_at === null) return `Live · updated ${format_age((Date.now() - this.checked_at) / 1000)}`;
            const wait = Math.max(0, Math.ceil((this.retry_at - Date.now()) / 1000));
            return `Live updates interrupted · retrying in ${wait} s`;
        }

        render() {
            const progress = this.detail ?? this.progress();
            this.root.dataset.liveState = this.state;
            this.text.textContent = this.notice ? `${this.notice} · ${progress}` : progress;
        }
    }

    class Poller {
        constructor({ url, params, status, apply, background }) {
            this.url = url;
            this.params = params;
            this.status = status;
            this.apply = apply;
            this.background = background;
            this.token = null;
            this.timer = null;
            this.waiting = false;
            this.stopped = false;
            this.failures = 0;
            this.client_failures = 0;
        }

        start() {
            this.status.start();
            document.addEventListener('visibilitychange', () => this.on_visibility());
            this.schedule();
        }

        get delay() {
            if (this.failures) return Math.min(MAX_BACKOFF_MS, POLL_MS * 2 ** this.failures);
            return document.hidden ? HIDDEN_POLL_MS : POLL_MS;
        }

        get address() {
            const url = new URL(this.url, window.location.origin);
            Object.entries(this.params).forEach(([name, value]) => url.searchParams.set(name, value));
            if (this.token) url.searchParams.set('token', this.token);
            return url;
        }

        schedule() {
            clearTimeout(this.timer);
            if (!this.stopped) this.timer = setTimeout(() => this.due(), this.delay);
        }

        due() {
            if (document.hidden && !this.background()) this.waiting = true;
            else this.poll();
        }

        on_visibility() {
            if (document.hidden || !this.waiting || this.stopped) return;
            this.waiting = false;
            this.poll();
        }

        stop(detail) {
            this.stopped = true;
            clearTimeout(this.timer);
            this.status.stop(detail);
        }

        async poll() {
            try {
                const data = await fetch_json(this.address);
                this.failures = this.client_failures = 0;
                this.token = data.token;
                this.status.checked();
                if (data.changed) this.apply(data, this);
            } catch (err) {
                this.failures += 1;
                this.client_failures = err instanceof FetchError && err.client ? this.client_failures + 1 : 0;
                if (this.client_failures >= MAX_CLIENT_ERRORS) this.stop('Live updates stopped · reload the page');
                else this.status.retrying(this.delay);
            } finally {
                this.schedule();
            }
        }
    }

    function stat_block(result) {
        const block = element('div', `statblock statblock-${result.colour}`);
        block.append(hidden_text(`${result.outcome}. `), ...with_breaks(result.statblock));
        return block;
    }

    function progress_bar(progress) {
        if (!progress) return null;
        const bar = element('div', `row-progress row-progress-${progress.kind}`);
        bar.setAttribute('role', 'meter');
        bar.setAttribute('aria-valuemin', '0');
        bar.setAttribute('aria-valuemax', '1');
        bar.setAttribute('aria-valuenow', progress.fraction.toFixed(3));
        bar.setAttribute('aria-valuetext', progress.label);
        bar.title = progress.label;
        bar.dataset.fraction = progress.fraction.toFixed(4);
        bar.style.setProperty('--fraction', bar.dataset.fraction);
        return bar;
    }

    function time_left(timing) {
        const left = element('span', `row-timing-${timing.kind}`);
        if (timing.note) left.title = timing.note;
        if (timing.kind === 'unavailable') left.append(hidden_text('Time left unavailable: '));
        if (timing.estimate) left.append(unspoken_text('≈ '), hidden_text('estimated '));
        left.append(timing.text);
        return left;
    }

    function games_rate(timing) {
        const rate = element('span', '', timing.rate);
        rate.title = `Games per hour, ${timing.rate_window}`;
        rate.append(hidden_text(` (${timing.rate_window})`));
        return rate;
    }

    function timing_line(timing) {
        if (!timing) return null;
        const line = element('div', 'row-timing');
        if (timing.text) line.append(time_left(timing));
        if (timing.text && timing.rate) line.append(unspoken_text(' · '));
        if (timing.rate) line.append(games_rate(timing));
        return line;
    }

    function reason_line(reason) {
        if (!reason) return null;
        const line = element('div', `row-meta row-reason row-reason-${reason.severity}`);
        line.title = reason.headline;
        line.append(hidden_text('Status: '), reason.brief);
        return line;
    }

    function set_text(node, text) {
        if (node.textContent !== text) node.textContent = text;
    }

    function render_moment(row_node, moment) {
        const stamp = row_node.querySelector('.row-name time');
        if (!stamp || !moment) return;
        if (stamp.dateTime !== moment.at) stamp.dateTime = stamp.title = moment.at;
        set_text(stamp, `${moment.verb} ${moment.ago}`);
    }

    function replace_changed(parent, parts) {
        const shown = [...parent.children];
        if (shown.length !== parts.length) {
            parent.replaceChildren(...parts);
            return;
        }
        shown.forEach((node, index) => {
            if (!node.isEqualNode(parts[index])) node.replaceWith(parts[index]);
        });
    }

    function stale_line() {
        const line = element('div', 'row-meta row-reason');
        line.append(hidden_text('Status: '), 'changed since this page loaded');
        return line;
    }

    class LiveListing {
        constructor(table) {
            this.table = table;
            this.machine_status = table.querySelector('[data-live-machine-status]');
            this.rows = new Map([...table.querySelectorAll('tr[data-live-row]')].map(node => [node.dataset.liveRow, node]));
        }

        apply(payload, poller) {
            if (this.machine_status) set_text(this.machine_status, payload.machine_status);

            const listed = new Map(payload.rows.map(row => [String(row.id), row]));
            const current = ([id, node]) => listed.get(id)?.result.status === node.dataset.liveStatus;
            const kept = [...this.rows].filter(current);
            const gone = [...this.rows].filter(entry => !current(entry));
            const played = kept.filter(([id, node]) => this.render(node, listed.get(id))).length;
            gone.forEach(([, node]) => this.mark_stale(node));

            const arrived = [...listed.keys()].filter(id => !this.rows.has(id)).length;
            this.report(gone.length, arrived, poller.status);

            if (played || gone.length || arrived) document.dispatchEvent(new CustomEvent(LISTING_EVENT));
            if (!payload.rows.length) poller.stop('Nothing is running · live updates stopped');
        }

        render(node, row) {
            const parts = [stat_block(row.result), progress_bar(row.progress), timing_line(row.timing), reason_line(row.reason)];
            replace_changed(node.querySelector('td.statblock-cell'), parts.filter(Boolean));
            render_moment(node, row.moment);

            const played = String(row.result.games) !== node.dataset.liveGames;
            node.dataset.liveGames = row.result.games;
            if (played) flash(node);
            return played;
        }

        mark_stale(node) {
            if (node.classList.contains('live-stale')) return;
            node.classList.add('live-stale');
            const cell = node.querySelector('td.statblock-cell');
            const block = cell.querySelector('.statblock');
            set_text(block.querySelector('.visually-hidden'), 'Out of date. ');
            cell.replaceChildren(block, stale_line());
        }

        report(left, arrived, status) {
            const notices = [
                left ? `${plural(left, 'workload')} finished or changed state` : null,
                arrived ? `${plural(arrived, 'new workload')}` : null,
            ].filter(Boolean);
            if (notices.length) status.prompt(notices.join(', '), 'Refresh');
        }
    }

    class FinishWatch {
        constructor(workload_id, button, note) {
            this.workload_id = workload_id;
            this.button = button;
            this.note = note;
            this.supported = 'Notification' in window;
        }

        start() {
            if (!this.supported || !this.button) return;
            this.button.hidden = false;
            this.button.addEventListener('click', () => this.toggle());
            this.sync();
        }

        get watched() {
            try {
                const stored = JSON.parse(localStorage.getItem(WATCH_KEY) ?? '[]');
                return Array.isArray(stored) ? stored.map(String) : [];
            } catch (err) {
                return [];
            }
        }

        store(ids) {
            try {
                localStorage.setItem(WATCH_KEY, JSON.stringify(ids.slice(-MAX_WATCHED)));
            } catch (err) {}
        }

        get watching() {
            return this.supported && Notification.permission === 'granted' && this.watched.includes(this.workload_id);
        }

        forget() {
            this.store(this.watched.filter(id => id !== this.workload_id));
        }

        async toggle() {
            if (this.watching) {
                this.forget();
                this.note.textContent = '';
            } else if (await Notification.requestPermission() === 'granted') {
                this.store([...this.watched.filter(id => id !== this.workload_id), this.workload_id]);
                this.note.textContent = 'A notification will appear when this workload finishes, while this tab stays open.';
            } else {
                this.note.textContent = 'Notifications are blocked for this site. Allow them in the browser to use this.';
            }
            this.sync();
        }

        sync() {
            this.button.setAttribute('aria-pressed', String(this.watching));
        }

        fire(title, body) {
            if (!this.watching) return;
            this.forget();
            this.sync();
            const notification = new Notification(title, { body, tag: `openbench-workload-${this.workload_id}` });
            notification.addEventListener('click', () => {
                window.focus();
                notification.close();
            });
        }
    }

    function stated(evidence) {
        return evidence.filter(item => typeof item.text === 'string' && item.text.trim() !== '');
    }

    function sentence_case(text) {
        return text.charAt(0).toUpperCase() + text.slice(1);
    }

    function evidence_item(item) {
        const entry = element('li', '', item.text);
        const href = item.link && same_origin_href(item.link.href);
        if (!href) return entry;
        const link = element('a', '', item.link.label);
        link.href = href;
        entry.append(' ', link);
        return entry;
    }

    function render_diagnosis(section, diagnosis) {
        if (!section) return;
        section.hidden = diagnosis === null || !diagnosis.urgent;
        if (diagnosis === null) return;

        section.className = `diagnosis diagnosis-${diagnosis.severity}`;
        section.dataset.diagnosisState = diagnosis.state;
        set_text(section.querySelector('.diagnosis-brief'), sentence_case(diagnosis.brief));
        set_text(section.querySelector('.diagnosis-headline'), diagnosis.headline);
        section.querySelector('.diagnosis-evidence').replaceChildren(...stated(diagnosis.evidence).map(evidence_item));
    }

    class LiveWorkload {
        constructor(container, watch) {
            this.container = container;
            this.watch = watch;
            this.workload_id = container.dataset.workloadId;
            this.title = document.title;
            this.statblock = container.querySelector('#long-statblock');
            this.outcome = container.querySelector('[data-live-outcome]');
            this.badge = container.querySelector('[data-live-badge]');
            this.states = JSON.parse(document.getElementById('workload-states').textContent);
            this.diagnosis = container.querySelector('.diagnosis');
        }

        apply(payload, poller) {
            const { result, diagnosis } = payload.workload;
            const played = String(result.games) !== this.container.dataset.liveGames;
            const moved = result.status !== this.container.dataset.liveStatus;
            const settled = !RUNNING.has(result.status);

            this.render(result);
            render_diagnosis(this.diagnosis, diagnosis);
            this.container.dataset.liveGames = result.games;
            this.container.dataset.liveStatus = result.status;

            if (played) flash(this.statblock);
            if (played || moved)
                document.dispatchEvent(new CustomEvent(WORKLOAD_EVENT, { detail: { status: result.status, settled } }));

            if (settled) this.settle(result, poller);
            else if (moved) poller.status.prompt(`This workload is now ${result.status}`, 'Reload');
        }

        render(result) {
            this.statblock.className = `long-statblock long-statblock-${result.colour}`;
            this.statblock.replaceChildren(...with_breaks(result.statblock));
            this.outcome.textContent = `Result: ${result.outcome}. `;
            const state = this.states[result.status];
            this.badge.className = `badge badge-${state.variant}`;
            set_text(this.badge, state.label ?? result.outcome);
        }

        settle(result, poller) {
            const mark = TITLE_MARKS[result.status] ?? '■ Finished';
            document.title = `${mark} · ${this.title}`;
            this.watch.fire(`#${this.workload_id} ${result.outcome.toLowerCase()}`, this.title);
            poller.status.prompt(`This workload ${result.status === 'deleted' ? 'was deleted' : `finished: ${result.outcome.toLowerCase()}`}`, 'Reload');
            poller.stop('Live updates stopped');
        }
    }

    function start_listing(table) {
        const listing = new LiveListing(table);
        const author = table.dataset.liveAuthor;
        new Poller({
            url: table.dataset.liveListing,
            params: author ? { author } : {},
            status: new LiveStatus(document.querySelector('[data-live-indicator]')),
            apply: (payload, poller) => listing.apply(payload, poller),
            background: () => false,
        }).start();
    }

    function start_workload(container) {
        const watch = new FinishWatch(
            container.dataset.workloadId,
            container.querySelector('[data-live-notify]'),
            container.querySelector('[data-live-notify-note]'),
        );
        const workload = new LiveWorkload(container, watch);
        watch.start();
        new Poller({
            url: container.dataset.liveWorkload,
            params: {},
            status: new LiveStatus(container.querySelector('[data-live-indicator]')),
            apply: (payload, poller) => workload.apply(payload, poller),
            background: () => watch.watching,
        }).start();
    }

    document.addEventListener('DOMContentLoaded', () => {
        const table = document.querySelector('table[data-live-listing]');
        if (table) start_listing(table);

        const container = document.querySelector('.workload-container[data-live-workload]');
        if (container) start_workload(container);
    });

})();
