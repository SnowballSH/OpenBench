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

        const sync = () => toggle.setAttribute('aria-expanded', document.body.classList.contains('sidebar-open'));
        const close = () => { document.body.classList.remove('sidebar-open'); sync(); };

        sync();
        toggle.addEventListener('click', sync);

        document.addEventListener('click', event => {
            if (document.body.classList.contains('sidebar-open')
                && !sidebar.contains(event.target) && !toggle.contains(event.target))
                close();
        });

        document.addEventListener('keydown', event => {
            if (event.key === 'Escape' && document.body.classList.contains('sidebar-open')) {
                close();
                toggle.focus();
            }
        });
    }

    document.addEventListener('DOMContentLoaded', () => {
        init_theme_toggle();
        mark_current_nav();
        init_sidebar_drawer();
    });

})();
