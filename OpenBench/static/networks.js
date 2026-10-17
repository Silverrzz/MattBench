var Networks = JSON.parse(document.getElementById('json-networks').textContent);
var network_sort_fields = ['default', 'engine', 'name'];

function sort_networks(fields) {
    network_sort_fields = fields;
    const body = document.querySelector('#network-table tbody');
    if (!body) return;
    const rows = Array.from(body.rows);
    const sorted = rows.slice().sort((a, b) => {
        for (const field of fields) {
            const left = a.dataset[field] || '';
            const right = b.dataset[field] || '';
            if (left !== right) return left > right ? -1 : 1;
        }
        return 0;
    });
    if (sorted.every((row, index) => row === rows[index])) return;
    const fragment = document.createDocumentFragment();
    sorted.forEach(row => fragment.appendChild(row));
    body.appendChild(fragment);
}

window.addEventListener('live-content', event => {
    if (event.detail.id === 'live-networks') sort_networks(network_sort_fields);
});
