const TIME_CONTROL_PLACEHOLDERS = {
    ''            : 'Anything',
    'FIXED-DEPTH' : 'All, or e.g D=10',
    'FIXED-NODES' : 'All, or e.g N=40000',
    'FIXED-TIME'  : 'All, or e.g MT=1000',
    'FISCHER'     : 'All, or e.g 15.0+0.15',
    'CYCLIC'      : 'All, or e.g 40/10.0+0.0',
};

const STATUS_DEFAULTS = {
    greens  : true, yellows : true, reds    : true,
    blues   : true, stopped : true, deleted : false,
};

function changed_timecontrol() {
    const tc_type = document.getElementById('tc-type').value;
    document.getElementById('tc-value-input').placeholder = TIME_CONTROL_PLACEHOLDERS[tc_type];
}

function prune_search(form) {

    // Keep the shareable URL short by only submitting non-default fields
    Array.from(form.elements).forEach(element => {
        const simple = element.tagName === 'SELECT'
                    || (element.tagName === 'INPUT' && element.type === 'text');
        if (simple && element.value === '') element.disabled = true;
    });

    Object.entries(STATUS_DEFAULTS).forEach(([status, shown]) => {
        const box = document.getElementById('show-' + status);
        if (box.checked === shown) return;

        const hidden = document.createElement('input');
        hidden.type  = 'hidden';
        hidden.name  = (box.checked ? 'show-' : 'hide-') + status;
        hidden.value = '1';
        form.appendChild(hidden);
    });
}

document.addEventListener('DOMContentLoaded', () => {

    changed_timecontrol();
    document.getElementById('tc-type').addEventListener('change', changed_timecontrol);

    const form = document.querySelector('form.search-form');
    form.addEventListener('submit', () => prune_search(form));
});
