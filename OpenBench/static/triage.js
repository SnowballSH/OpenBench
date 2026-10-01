(function () {

    'use strict';

    function copy_with_textarea(text) {
        const focused = document.activeElement;
        const area = document.createElement('textarea');
        area.value = text;
        area.setAttribute('readonly', '');
        area.classList.add('visually-hidden');
        document.body.append(area);
        area.select();
        try {
            return document.execCommand('copy');
        }
        catch (err) {
            return false;
        }
        finally {
            area.remove();
            if (focused instanceof HTMLElement) focused.focus();
        }
    }

    async function copy_text(text) {
        if (navigator.clipboard && window.isSecureContext) {
            try {
                await navigator.clipboard.writeText(text);
                return true;
            }
            catch (err) {}
        }
        return copy_with_textarea(text);
    }

    function init_copy(view) {
        const button = view.querySelector('[data-log-copy]');
        const status = view.querySelector('[data-log-status]');
        if (!button) return;

        const label = button.textContent;

        button.addEventListener('click', async () => {
            const lines = [...view.querySelectorAll('.log-line')].map(line => line.textContent);
            const copied = await copy_text(lines.join('\n'));
            const message = copied ? `Copied ${lines.length} lines` : 'Unable to copy the log';
            button.textContent = copied ? 'Copied' : 'Copy failed';
            if (status) status.textContent = message;
            window.setTimeout(() => { button.textContent = label; }, 2000);
        });
    }

    function reveal(hash) {
        if (!/^#L[0-9]+$/.test(hash)) return;
        const line = document.getElementById(hash.slice(1));
        if (!line) return;
        const fold = line.closest('details');
        if (fold) fold.open = true;
        document.querySelectorAll('.log-line-target').forEach(old => old.classList.remove('log-line-target'));
        line.classList.add('log-line-target');
        line.scrollIntoView({ block : 'center' });
    }

    document.addEventListener('DOMContentLoaded', () => {
        const view = document.querySelector('[data-log-view]');
        if (!view) return;
        init_copy(view);
        reveal(window.location.hash);
        window.addEventListener('hashchange', () => reveal(window.location.hash));
        document.querySelectorAll('a[data-log-jump]').forEach(link => {
            link.addEventListener('click', () => reveal(link.hash));
        });
    });

})();
