(() => {

    const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });

    function cell_value(row, column, numeric) {
        const cell = row.cells[column];
        const raw = cell ? (cell.dataset.sortValue ?? cell.textContent.trim()) : '';
        if (!numeric) return raw;
        const value = Number(raw);
        return Number.isFinite(value) ? value : -Infinity;
    }

    function compare(numeric) {
        return numeric ? (a, b) => a - b : (a, b) => collator.compare(a, b);
    }

    function sort_by(table, header, button) {
        const numeric = button.dataset.sort === 'number';
        const current = header.getAttribute('aria-sort');
        const direction = current === 'descending' || (!current && !numeric) ? 'ascending' : 'descending';
        const sign = direction === 'ascending' ? 1 : -1;
        const column = header.cellIndex;
        const order = compare(numeric);

        table.querySelectorAll('thead th[aria-sort]').forEach(other => other.removeAttribute('aria-sort'));
        header.setAttribute('aria-sort', direction);

        const body = table.tBodies[0];
        const rows = Array.from(body.rows).map((row, index) => ({ row, index, value: cell_value(row, column, numeric) }));
        rows.sort((a, b) => sign * order(a.value, b.value) || a.index - b.index);
        body.append(...rows.map(entry => entry.row));
    }

    function init_sortable(table) {
        table.querySelectorAll('thead th').forEach(header => {
            const button = header.querySelector('.sort-button');
            if (button) button.addEventListener('click', () => sort_by(table, header, button));
        });
    }

    document.addEventListener('DOMContentLoaded', () => {
        document.querySelectorAll('table[data-sortable]').forEach(init_sortable);
    });

})();
