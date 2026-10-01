(() => {

    const THEME_KEY = 'openbench-theme';
    const root = document.documentElement;
    const prefers_light = window.matchMedia('(prefers-color-scheme: light)');

    function effective_theme() {
        return root.dataset.theme || (prefers_light.matches ? 'light' : 'dark');
    }

    function store_theme(theme) {
        try {
            localStorage.setItem(THEME_KEY, theme);
        } catch (err) {}
    }

    function label_theme_toggle(button) {
        const next = effective_theme() === 'dark' ? 'light' : 'dark';
        button.setAttribute('aria-label', `Switch to ${next} theme`);
        button.title = `Switch to ${next} theme`;
    }

    function init_theme_toggle() {
        const button = document.getElementById('theme-toggle');
        if (!button) return;

        label_theme_toggle(button);
        prefers_light.addEventListener('change', () => label_theme_toggle(button));

        button.addEventListener('click', () => {
            const next = effective_theme() === 'dark' ? 'light' : 'dark';
            root.dataset.theme = next;
            store_theme(next);
            label_theme_toggle(button);
        });
    }

    function strip_slash(path) {
        return path.length > 1 ? path.replace(/\/+$/, '') : path;
    }

    function mark_current_nav() {
        const here = strip_slash(window.location.pathname);

        document.querySelectorAll('#sidebar a[href], [data-current-nav] a[href]').forEach(link => {
            const url = new URL(link.href, window.location.href);
            if (url.origin !== window.location.origin) return;

            const path = strip_slash(url.pathname);
            if (path !== '/' && (here === path || here.startsWith(path + '/')))
                link.setAttribute('aria-current', 'page');
        });
    }

    function init_sidebar_drawer() {
        const toggle = document.getElementById('sidebar-toggle');
        const sidebar = document.getElementById('sidebar');
        if (!toggle || !sidebar) return;

        const page = document.getElementById('content-parent');
        const drawer_layout = window.matchMedia('(max-width: 767px)');
        const is_open = () => document.body.classList.contains('sidebar-open');

        function sync() {
            const open = is_open() && drawer_layout.matches;
            toggle.setAttribute('aria-expanded', open);
            if (page) page.inert = open;
        }

        function open_drawer() {
            sync();
            if (!is_open()) return;
            const first_link = sidebar.querySelector('a[href], button, input[type="submit"]');
            if (first_link) first_link.focus();
        }

        function close_drawer() {
            document.body.classList.remove('sidebar-open');
            sync();
            toggle.focus();
        }

        sync();
        toggle.addEventListener('click', () => {
            document.body.classList.toggle('sidebar-open');
            open_drawer();
        });
        drawer_layout.addEventListener('change', () => {
            document.body.classList.remove('sidebar-open');
            sync();
        });

        document.addEventListener('click', event => {
            if (is_open() && !sidebar.contains(event.target) && !toggle.contains(event.target))
                close_drawer();
        });

        document.addEventListener('keydown', event => {
            if (event.key === 'Escape' && is_open())
                close_drawer();
        });
    }

    function format_stamps(selector, options) {
        document.querySelectorAll(selector).forEach(element => {
            const date = new Date(1000 * element.textContent);
            element.textContent = date.toLocaleString(undefined, options);
        });
    }

    function summarise_engine_options() {
        document.querySelectorAll('.engine-options').forEach(cell => {

            const options = cell.textContent.trim().split(/\s+/);
            if (options.length <= 2) return;

            const summary = ['Threads=', 'Hash=']
                .map(prefix => options.find(option => option.startsWith(prefix)))
                .filter(option => option !== undefined);

            // Options are user input, so they are only ever added as text
            const popup = document.createElement('div');
            popup.classList.add('engine-options-popup');
            popup.setAttribute('aria-hidden', 'true');
            options.forEach((option, index) => {
                if (index) popup.appendChild(document.createElement('br'));
                popup.appendChild(document.createTextNode(option));
            });

            const full = document.createElement('span');
            full.classList.add('visually-hidden');
            full.textContent = options.join(' ');

            const shown = document.createElement('span');
            shown.setAttribute('aria-hidden', 'true');
            shown.textContent = [...summary, '...'].join(' ');

            cell.tabIndex = 0;
            cell.replaceChildren(full, shown, popup);
        });
    }

    function scroll_region_label(wrap) {
        if (wrap.dataset.regionLabel) return wrap.dataset.regionLabel;
        const caption = wrap.querySelector('caption');
        if (caption) return caption.textContent.trim();
        const titled = wrap.closest('[aria-labelledby]');
        const title = titled && document.getElementById(titled.getAttribute('aria-labelledby'));
        return title ? `${title.textContent.trim()} table` : 'Scrollable table';
    }

    function sync_scroll_region(wrap) {
        const scrolls = wrap.scrollWidth > wrap.clientWidth + 1;
        if (scrolls && !wrap.hasAttribute('tabindex')) {
            wrap.tabIndex = 0;
            wrap.setAttribute('role', 'region');
            wrap.setAttribute('aria-label', scroll_region_label(wrap));
            wrap.dataset.scrollRegion = '';
        }
        else if (!scrolls && 'scrollRegion' in wrap.dataset) {
            wrap.removeAttribute('tabindex');
            wrap.removeAttribute('role');
            wrap.removeAttribute('aria-label');
            delete wrap.dataset.scrollRegion;
        }
    }

    function init_scroll_regions() {
        const observer = new ResizeObserver(entries => entries.forEach(entry => sync_scroll_region(entry.target)));
        const watch = scope => scope.querySelectorAll('.table-wrap').forEach(wrap => {
            if (wrap.dataset.scrollWatched) return;
            wrap.dataset.scrollWatched = 'true';
            observer.observe(wrap);
            new MutationObserver(() => sync_scroll_region(wrap)).observe(wrap, { childList : true, subtree : true });
        });

        watch(document);
        new MutationObserver(records => records.forEach(record => record.addedNodes.forEach(node => {
            if (node.nodeType !== Node.ELEMENT_NODE) return;
            if (node.matches('.table-wrap')) watch(node.parentNode);
            else watch(node);
        }))).observe(document.body, { childList : true, subtree : true });
    }

    function apply_css_fractions() {
        const properties = { fraction : '--fraction', share : '--share' };
        Object.entries(properties).forEach(([key, property]) => {
            document.querySelectorAll(`[data-${key}]`).forEach(element => {
                element.style.setProperty(property, element.dataset[key]);
            });
        });
    }

    function guard_duplicate_submissions() {
        document.addEventListener('submit', event => {
            const form = event.target;
            if (form.dataset.submitting) {
                event.preventDefault();
                return;
            }
            form.dataset.submitting = true;
        }, true);

        window.addEventListener('pageshow', event => {
            if (!event.persisted) return;
            document
                .querySelectorAll('form[data-submitting]')
                .forEach(form => delete form.dataset.submitting);
        });
    }

    function resolve_action_template(template) {
        return template.replace(/\{([\w-]+)\}/g,
            (_, id) => encodeURIComponent(document.getElementById(id).value));
    }

    function init_delegated_actions() {
        document.addEventListener('click', event => {
            const confirming = event.target.closest('[data-confirm]');
            if (confirming && !window.confirm(confirming.dataset.confirm)) {
                event.preventDefault();
                return;
            }

            if (event.defaultPrevented) return;

            const alerting = event.target.closest('[data-alert]');
            if (alerting)
                window.alert(alerting.dataset.alert);

            const submitter = event.target.closest('[data-submit-form]');
            if (submitter) {
                event.preventDefault();
                document.getElementById(submitter.dataset.submitForm).submit();
            }
        });

        document.addEventListener('submit', event => {
            const form = event.target.closest('form[data-action-template]');
            if (!form) return;
            const duplicate = event.defaultPrevented;
            event.preventDefault();
            if (duplicate) return;
            form.action = resolve_action_template(form.dataset.actionTemplate);
            form.submit();
        });
    }

    const ROW_CONTROLS = 'a, button, input, select, textarea, label, summary, [contenteditable], [data-row-ignore]';

    function navigable_row(event) {
        if (!(event.target instanceof Element)) return null;
        const row = event.target.closest('[data-row-href]');
        if (!row) return null;
        const control = event.target.closest(ROW_CONTROLS);
        return control && row.contains(control) ? null : row;
    }

    function row_destination(row) {
        const url = new URL(row.dataset.rowHref, window.location.href);
        return url.origin === window.location.origin ? url.href : null;
    }

    function selecting_text_in(row) {
        const selection = window.getSelection();
        return selection !== null
            && !selection.isCollapsed
            && selection.toString().trim() !== ''
            && selection.containsNode(row, true);
    }

    function open_in_new_tab(url) {
        window.open(url, '_blank', 'noopener');
    }

    function init_row_navigation() {
        document.addEventListener('click', event => {
            if (event.defaultPrevented || event.button !== 0 || event.shiftKey || event.altKey) return;
            const row = navigable_row(event);
            if (!row || selecting_text_in(row)) return;
            const url = row_destination(row);
            if (!url) return;
            if (event.metaKey || event.ctrlKey) open_in_new_tab(url);
            else window.location.assign(url);
        });

        // A middle button press on anything but a link starts autoscroll where the platform has it
        document.addEventListener('mousedown', event => {
            if (event.button === 1 && navigable_row(event)) event.preventDefault();
        });

        document.addEventListener('auxclick', event => {
            if (event.button !== 1) return;
            const row = navigable_row(event);
            const url = row && row_destination(row);
            if (!url) return;
            event.preventDefault();
            open_in_new_tab(url);
        });
    }

    function hide_empty_groups(table, filtering) {
        let subgroup_shown = false;
        let group_shown = false;
        [...table.rows].reverse().forEach(row => {
            if ('rowHref' in row.dataset) {
                if (!row.hidden) subgroup_shown = group_shown = true;
            }
            else if (row.classList.contains('table-small-header')) {
                row.hidden = filtering && !subgroup_shown;
                subgroup_shown = false;
            }
            else if (row.classList.contains('table-header')) {
                row.hidden = filtering && !group_shown;
                subgroup_shown = group_shown = false;
            }
        });
    }

    function init_row_filter(input) {
        const table = document.getElementById(input.dataset.rowFilter);
        const count = document.getElementById(input.dataset.rowFilterCount);
        if (!table) return;

        function apply() {
            const terms = input.value.toLowerCase().split(/\s+/).filter(Boolean);
            const rows = [...table.querySelectorAll('tr[data-row-href]')];
            let shown = 0;
            rows.forEach(row => {
                const text = row.textContent.toLowerCase();
                row.hidden = !terms.every(term => text.includes(term));
                if (!row.hidden) shown += 1;
            });
            hide_empty_groups(table, terms.length > 0);
            table.classList.toggle('row-filtered', terms.length > 0);
            if (count)
                count.textContent = terms.length ? `${shown} of ${rows.length} shown` : `${rows.length} on this page`;
        }

        input.addEventListener('input', apply);
        apply();
    }

    guard_duplicate_submissions();
    init_delegated_actions();
    init_row_navigation();

    document.addEventListener('DOMContentLoaded', () => {
        init_theme_toggle();
        mark_current_nav();
        init_sidebar_drawer();
        format_stamps('.timestamp', {
            year : 'numeric', month : '2-digit', day : '2-digit',
            hour : '2-digit', minute : '2-digit', second : '2-digit',
            hour12 : false,
        });
        format_stamps('.datestamp', { month : 'short', day : '2-digit' });
        summarise_engine_options();
        apply_css_fractions();
        init_scroll_regions();
        document.querySelectorAll('input[data-row-filter]').forEach(init_row_filter);
    });

})();
