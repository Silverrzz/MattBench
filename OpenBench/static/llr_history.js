(() => {
    const widget = document.querySelector('.llr-history');
    if (!widget) return;

    const plot = widget.querySelector('.llr-plot');
    const svg = widget.querySelector('.llr-graph');
    const tooltip = widget.querySelector('.llr-tooltip');
    const feedback = widget.querySelector('[data-llr-feedback]');
    const readout = widget.querySelector('#llr-readout');
    const number = new Intl.NumberFormat();
    const compact = new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 });
    const signed = value => `${value > 0 ? '+' : ''}${value.toFixed(2)}`;
    const clamp = (value, low, high) => Math.max(low, Math.min(high, value));
    let data;
    let coordinates = [];
    let selected = -1;
    let crosshair;
    let marker;
    let timer;
    let controller;
    let frame;

    function element(tag, attributes = {}, text) {
        const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
        Object.entries(attributes).forEach(([name, value]) => node.setAttribute(name, value));
        if (text !== undefined) node.textContent = text;
        return node;
    }

    function validate(next) {
        if (!next || !Array.isArray(next.points) || !next.points.length ||
            ![next.lower, next.upper, next.llr, next.games].every(Number.isFinite) ||
            next.lower >= next.upper || next.games < 0 || typeof next.finished !== 'boolean') return false;
        return next.points.every((point, index) => Number.isInteger(point.games) && point.games >= 0 &&
            point.games <= next.games && Number.isFinite(point.llr) &&
            (index === 0 || point.games > next.points[index - 1].games));
    }

    function hide() {
        selected = -1;
        tooltip.hidden = true;
        crosshair?.setAttribute('visibility', 'hidden');
        marker?.setAttribute('visibility', 'hidden');
    }

    function show(index, announce = false) {
        if (!coordinates.length) return;
        selected = clamp(index, 0, coordinates.length - 1);
        const point = coordinates[selected];
        crosshair.setAttribute('x1', point.x);
        crosshair.setAttribute('x2', point.x);
        crosshair.setAttribute('visibility', 'visible');
        marker.setAttribute('cx', point.x);
        marker.setAttribute('cy', point.y);
        marker.setAttribute('visibility', 'visible');
        tooltip.textContent = `${number.format(point.games)} games\nLLR ${signed(point.llr)}`;
        tooltip.hidden = false;
        const width = plot.clientWidth;
        const left = point.x + 12 + tooltip.offsetWidth > width ? point.x - tooltip.offsetWidth - 12 : point.x + 12;
        const top = point.y - tooltip.offsetHeight - 12;
        tooltip.style.left = `${clamp(left, 0, width - tooltip.offsetWidth)}px`;
        tooltip.style.top = `${clamp(top, 0, plot.clientHeight - tooltip.offsetHeight)}px`;
        if (announce) readout.textContent = `${number.format(point.games)} games, LLR ${signed(point.llr)}.`;
    }

    function draw() {
        if (!data) return;
        const selectedGames = coordinates[selected]?.games;
        const width = Math.max(180, plot.clientWidth);
        const height = svg.clientHeight;
        const left = 40;
        const right = width - 12;
        const top = 10;
        const bottom = height - 22;
        const extreme = data.points.reduce((maximum, point) => Math.max(maximum, Math.abs(point.llr)), Math.max(1, Math.abs(data.lower), Math.abs(data.upper)));
        const limit = Math.ceil(extreme * 1.15 * 10) / 10;
        const x = games => left + (right - left) * games / Math.max(1, data.games);
        const y = llr => top + (bottom - top) * (limit - llr) / (2 * limit);
        coordinates = data.points.map(point => ({ ...point, x: x(point.games), y: y(point.llr) }));
        svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
        svg.setAttribute('aria-label', `LLR ${signed(data.llr)} after ${number.format(data.games)} games. Pass at ${signed(data.upper)}, fail at ${signed(data.lower)}.`);
        const content = document.createDocumentFragment();
        const defs = element('defs');
        const gradient = element('linearGradient', { id: 'llr-line-gradient', x1: 0, x2: 0, y1: y(Math.min(limit, Math.max(1, data.upper))), y2: y(Math.max(-limit, Math.min(-1, data.lower))), gradientUnits: 'userSpaceOnUse' });
        const neutralOffset = Math.max(1, data.upper) / (Math.max(1, data.upper) - Math.min(-1, data.lower));
        gradient.append(element('stop', { offset: 0, 'stop-color': 'var(--llr-positive)' }), element('stop', { offset: neutralOffset, 'stop-color': 'var(--llr-neutral)' }), element('stop', { offset: 1, 'stop-color': 'var(--llr-negative)' }));
        defs.append(gradient);
        content.append(defs);
        for (let i = -1; i <= 1; i++) {
            const value = limit * i;
            content.append(element('line', { x1: left, x2: right, y1: y(value), y2: y(value), class: i === 0 ? 'llr-zero' : 'llr-grid' }));
            const label = Math.abs(value) >= 100 ? compact.format(value) : value.toFixed(1);
            content.append(element('text', { x: left - 9, y: y(value), 'text-anchor': 'end', 'dominant-baseline': 'middle' }, label));
        }
        const ticks = Math.min(data.games, width < 340 ? 2 : 4);
        for (let i = 0; i <= ticks; i++) {
            const games = ticks ? Math.round(data.games * i / ticks) : 0;
            const position = x(games);
            content.append(element('line', { x1: position, x2: position, y1: top, y2: bottom, class: 'llr-grid' }));
            content.append(element('text', { x: position, y: height - 6, 'text-anchor': i === 0 ? 'start' : i === ticks ? 'end' : 'middle' }, games >= 10000 ? compact.format(games) : number.format(games)));
        }
        for (const [bound, name] of [[data.upper, 'upper'], [data.lower, 'lower']]) {
            content.append(element('line', { x1: left, x2: right, y1: y(bound), y2: y(bound), class: `llr-bound llr-bound-${name}` }));
        }
        const points = coordinates.map(point => `${point.x.toFixed(2)},${point.y.toFixed(2)}`).join(' ');
        if (coordinates.length > 1) {
            content.append(element('polygon', { points: `${coordinates[0].x},${y(0)} ${points} ${coordinates[coordinates.length - 1].x},${y(0)}`, fill: 'url(#llr-line-gradient)', class: 'llr-area' }));
            content.append(element('polyline', { points, stroke: 'url(#llr-line-gradient)', class: 'llr-path' }));
        }
        const end = coordinates[coordinates.length - 1];
        content.append(element('circle', { cx: end.x, cy: end.y, r: 4, fill: 'url(#llr-line-gradient)', class: 'llr-endpoint' }));
        crosshair = element('line', { y1: top, y2: bottom, class: 'llr-crosshair', visibility: 'hidden' });
        marker = element('circle', { r: 4, class: 'llr-hover-point', visibility: 'hidden' });
        content.append(crosshair, marker);
        svg.replaceChildren(content);
        const previous = coordinates.findIndex(point => point.games === selectedGames);
        if (previous >= 0) show(previous);
        else hide();
    }

    function inspect(event) {
        if (!coordinates.length) return;
        const position = event.clientX - svg.getBoundingClientRect().left;
        let low = 0;
        let high = coordinates.length - 1;
        while (low < high) {
            const middle = (low + high) >> 1;
            if (coordinates[middle].x < position) low = middle + 1;
            else high = middle;
        }
        const index = low > 0 && position - coordinates[low - 1].x < coordinates[low].x - position ? low - 1 : low;
        show(index);
    }

    function schedule() {
        clearTimeout(timer);
        if (!data?.finished && !document.hidden) timer = setTimeout(refresh, 30000);
    }

    async function refresh() {
        if (controller || document.hidden || data?.finished) return;
        controller = new AbortController();
        const timeout = setTimeout(() => controller?.abort(), 15000);
        try {
            const response = await fetch(widget.dataset.url, { signal: controller.signal, cache: 'no-store', headers: { Accept: 'application/json' } });
            if (!response.ok) throw new Error('History request failed');
            const next = await response.json();
            if (!validate(next)) throw new Error('Invalid history');
            data = next;
            feedback.hidden = true;
            feedback.textContent = '';
            draw();
        } catch {
            feedback.textContent = data ? 'Update unavailable' : 'Chart unavailable';
            feedback.hidden = false;
        } finally {
            clearTimeout(timeout);
            controller = null;
            schedule();
        }
    }

    plot.addEventListener('pointermove', inspect);
    plot.addEventListener('pointerdown', inspect);
    plot.addEventListener('pointerleave', event => {
        if (event.pointerType === 'mouse') hide();
    });
    plot.addEventListener('pointercancel', hide);
    plot.addEventListener('blur', hide);
    plot.addEventListener('focus', () => {
        if (selected < 0) show(coordinates.length - 1, true);
    });
    plot.addEventListener('keydown', event => {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End', 'Escape'].includes(event.key)) return;
        event.preventDefault();
        if (event.key === 'Escape') return hide();
        const index = selected < 0 ? coordinates.length - 1 : selected;
        if (event.key === 'Home') show(0, true);
        else if (event.key === 'End') show(coordinates.length - 1, true);
        else show(index + (event.key === 'ArrowLeft' ? -1 : 1), true);
    });
    document.addEventListener('visibilitychange', () => {
        if (document.hidden) clearTimeout(timer);
        else refresh();
    });
    window.addEventListener('pagehide', () => {
        clearTimeout(timer);
        controller?.abort();
    });
    window.addEventListener('pageshow', schedule);
    const resize = () => {
        cancelAnimationFrame(frame);
        frame = requestAnimationFrame(draw);
    };
    if (typeof ResizeObserver === 'function') new ResizeObserver(resize).observe(plot);
    else window.addEventListener('resize', resize);
    try {
        const initial = JSON.parse(document.getElementById('llr-history-data').textContent);
        if (!validate(initial)) throw new Error('Invalid history');
        data = initial;
        draw();
        schedule();
    } catch {
        refresh();
    }
})();
