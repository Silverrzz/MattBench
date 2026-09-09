(() => {
    if (!document.querySelector('[data-live]')) return;

    const status = document.createElement('span');
    status.setAttribute('role', 'status');
    status.className = 'live-status';
    document.getElementById('content-header').appendChild(status);

    const subscriptions = new Set();
    const regions = new Map();
    let socket;
    let retry;
    let watchdog;
    let delay = 1000;
    let stopped = false;

    document.addEventListener('input', event => {
        if (event.target.matches('#actions input, #actions textarea')) {
            event.target.dataset.liveDirty = 'true';
        }
    });

    window.subscribe_live = name => {
        subscriptions.add(name);
        if (socket && socket.readyState === WebSocket.OPEN) {
            socket.send(JSON.stringify({subscribe: name}));
        }
    };

    function connect() {
        if (stopped) return;
        clearTimeout(retry);
        status.textContent = 'Connecting…';
        const url = new URL('/ws/live/', window.location.href);
        url.protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        url.searchParams.set('page', window.location.pathname + window.location.search);
        socket = new WebSocket(url);
        watchdog = setTimeout(() => socket.close(), 15000);

        socket.onopen = () => {
            subscriptions.forEach(name => socket.send(JSON.stringify({subscribe: name})));
        };

        socket.onmessage = event => {
            const data = JSON.parse(event.data);
            clearTimeout(watchdog);
            if (data.unavailable) {
                status.textContent = 'Live updates unavailable';
                stopped = true;
                return;
            }
            watchdog = setTimeout(() => socket.close(), 15000);
            status.textContent = 'Live';
            delay = 1000;
            Object.entries(data.regions || {}).forEach(([id, html]) => {
                const target = document.getElementById(id);
                if (!target || (regions.get(id) === html && id !== 'live-networks')) return;
                target.innerHTML = html;
                regions.set(id, html);
                format_live_content(target);
            });
            if (data.summary) populate_summary(data.summary);
            if (data.results) populate_results(data.results);
            if (data.digest !== undefined) populate_spsa_digest(data.digest);
            Object.entries(data.fields || {}).forEach(([id, value]) => {
                const input = document.getElementById(id);
                if (input && !input.dataset.liveDirty && input !== document.activeElement) {
                    input.value = value;
                }
            });
            if (data.networks) {
                Networks = data.networks;
                sort_networks(network_sort_fields);
            }
        };

        socket.onclose = event => {
            clearTimeout(watchdog);
            if (stopped) return;
            if ([4400, 4403, 4404].includes(event.code)) {
                status.textContent = 'Live updates unavailable';
                stopped = true;
                return;
            }
            status.textContent = 'Reconnecting…';
            retry = setTimeout(connect, delay + Math.random() * 500);
            delay = Math.min(delay * 2, 30000);
        };
        socket.onerror = () => socket.close();
    }

    window.addEventListener('pagehide', () => {
        stopped = true;
        clearTimeout(retry);
        clearTimeout(watchdog);
        if (socket) socket.close();
    });
    window.addEventListener('pageshow', event => {
        if (event.persisted) {
            stopped = false;
            connect();
        }
    });
    connect();
})();
