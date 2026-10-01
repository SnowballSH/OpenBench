(() => {

    const SUGGEST_DELAY_MS = 150;
    const LIST_ID = 'quick-jump-list';

    function is_editable(target) {
        return target instanceof Element
            && target.closest('input, textarea, select, [contenteditable]:not([contenteditable="false"])') !== null;
    }

    function init_shortcut(input) {
        document.addEventListener('keydown', event => {
            if (event.key !== '/' || event.defaultPrevented) return;
            if (event.ctrlKey || event.metaKey || event.altKey) return;
            if (is_editable(event.target)) return;
            event.preventDefault();
            input.focus();
            input.select();
        });
    }

    function same_origin(url) {
        return new URL(url, window.location.href).origin === window.location.origin;
    }

    function build_option(suggestion, index) {
        const option = document.createElement('li');
        option.id = `${LIST_ID}-option-${index}`;
        option.setAttribute('role', 'option');
        option.setAttribute('aria-selected', 'false');
        option.dataset.url = suggestion.url;

        const label = document.createElement('span');
        label.className = 'quick-jump-label';
        label.textContent = suggestion.label;

        const detail = document.createElement('span');
        detail.className = 'quick-jump-detail';
        detail.textContent = suggestion.detail;

        option.append(label, detail);
        return option;
    }

    function init_suggestions(form, input) {
        const endpoint = form.dataset.suggestUrl;
        if (!endpoint || !window.fetch || !window.AbortController) return;

        const list = document.createElement('ul');
        list.id = LIST_ID;
        list.className = 'quick-jump-list';
        list.setAttribute('role', 'listbox');
        list.setAttribute('aria-label', 'Jump suggestions');
        list.hidden = true;
        input.parentNode.appendChild(list);

        input.setAttribute('role', 'combobox');
        input.setAttribute('aria-autocomplete', 'list');
        input.setAttribute('aria-controls', LIST_ID);
        input.setAttribute('aria-expanded', 'false');

        let active = -1;
        let timer = null;
        let pending = null;
        let listed_for = null;

        const current_text = () => input.value.trim();
        const options = () => [...list.children];
        const has_current_options = () => list.children.length > 0 && listed_for === current_text();
        const is_open = () => !list.hidden;

        function set_active(index) {
            active = index;
            options().forEach((option, position) => {
                option.setAttribute('aria-selected', position === index ? 'true' : 'false');
            });
            if (index < 0) {
                input.removeAttribute('aria-activedescendant');
                return;
            }
            const option = list.children[index];
            input.setAttribute('aria-activedescendant', option.id);
            option.scrollIntoView({ block : 'nearest' });
        }

        function set_open(open) {
            list.hidden = !open;
            input.setAttribute('aria-expanded', open ? 'true' : 'false');
            if (!open) set_active(-1);
        }

        function cancel_pending() {
            window.clearTimeout(timer);
            if (pending) pending.abort();
            pending = null;
        }

        function close() {
            cancel_pending();
            set_open(false);
        }

        function clear() {
            list.replaceChildren();
            listed_for = null;
            set_open(false);
        }

        function show(text, suggestions) {
            if (text !== current_text()) return;
            listed_for = text;
            list.replaceChildren(...suggestions.filter(suggestion => same_origin(suggestion.url)).map(build_option));
            set_active(-1);
            set_open(list.children.length > 0 && document.activeElement === input);
        }

        function request(text) {
            const controller = new AbortController();
            pending = controller;
            fetch(`${endpoint}?${new URLSearchParams({ q : text })}`, {
                signal : controller.signal,
                credentials : 'same-origin',
                headers : { Accept : 'application/json' },
            })
                .then(response => response.ok ? response.json() : Promise.reject(new Error(response.status)))
                .then(data => {
                    if (pending === controller) show(text, data.suggestions || []);
                })
                .catch(() => {
                    if (pending === controller) clear();
                });
        }

        function schedule() {
            cancel_pending();
            const text = current_text();
            if (!text) {
                clear();
                return;
            }
            set_active(-1);
            timer = window.setTimeout(() => request(text), SUGGEST_DELAY_MS);
        }

        function move(step) {
            const count = list.children.length;
            if (!is_open()) set_open(true);
            const next = active < 0 ? (step > 0 ? 0 : count - 1) : active + step;
            set_active(next < 0 || next >= count ? -1 : next);
        }

        function follow(option) {
            close();
            window.location.assign(option.dataset.url);
        }

        input.addEventListener('input', schedule);
        input.addEventListener('blur', close);
        form.addEventListener('submit', close);

        input.addEventListener('keydown', event => {
            if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
                if (!has_current_options()) return;
                event.preventDefault();
                move(event.key === 'ArrowDown' ? 1 : -1);
            }
            else if (event.key === 'Enter' && is_open() && active >= 0) {
                event.preventDefault();
                follow(list.children[active]);
            }
            else if (event.key === 'Escape' && is_open()) {
                // A search input would otherwise clear its text on Escape
                event.preventDefault();
                event.stopImmediatePropagation();
                close();
            }
        });

        // Keeps focus in the input, so a press on an option does not close the list before its click
        list.addEventListener('mousedown', event => event.preventDefault());
        list.addEventListener('click', event => {
            const option = event.target.closest('[role="option"]');
            if (option) follow(option);
        });
    }

    document.addEventListener('DOMContentLoaded', () => {
        const form = document.getElementById('quick-jump');
        const input = document.getElementById('quick-jump-input');
        if (!form || !input) return;

        init_shortcut(input);
        init_suggestions(form, input);
        input.addEventListener('keydown', event => {
            if (event.key === 'Escape') input.blur();
        });
    });

})();
