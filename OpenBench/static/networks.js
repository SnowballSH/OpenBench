(() => {

    const INITIAL_ORDER = ['default', 'engine', 'name'];

    function is_greater_than(a, b, attrs) {
        for (const attr of attrs) {
            if (a[attr] === b[attr])
                continue;
            return a[attr] > b[attr];
        }
        return false;
    }

    function compare_descending(a, b) {
        if (is_greater_than(a, b, INITIAL_ORDER)) return -1;
        if (is_greater_than(b, a, INITIAL_ORDER)) return 1;
        return 0;
    }

    document.addEventListener('DOMContentLoaded', () => {
        const networks = JSON.parse(document.getElementById('json-networks').textContent);
        const body = document.getElementById('network-table').tBodies[0];
        const rows = Array.from(body.rows).map((row, index) => ({ row, network : networks[index] }));
        rows.sort((a, b) => compare_descending(a.network, b.network));
        body.append(...rows.map(entry => entry.row));
    });

})();
