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

        document.querySelectorAll('#sidebar a[href]').forEach(link => {
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
            options.forEach((option, index) => {
                if (index) popup.appendChild(document.createElement('br'));
                popup.appendChild(document.createTextNode(option));
            });

            cell.textContent = [...summary, '...'].join(' ');
            cell.appendChild(popup);
        });
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
            if (confirming && !window.confirm(confirming.dataset.confirm))
                event.preventDefault();

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

    guard_duplicate_submissions();
    init_delegated_actions();

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
    });

})();
