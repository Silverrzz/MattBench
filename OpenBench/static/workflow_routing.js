(() => {
    const distance = (a, b) => Math.abs(a.x - b.x) + Math.abs(a.y - b.y);
    const same = (a, b) => Math.abs(a.x - b.x) < .01 && Math.abs(a.y - b.y) < .01;
    function simplify(points) {
        const result = [];
        for (const point of points) {
            if (result.length && same(result[result.length - 1], point)) continue;
            while (result.length > 1) {
                const a = result[result.length - 2], b = result[result.length - 1];
                if ((a.x === b.x && b.x === point.x && (b.y - a.y) * (point.y - b.y) >= 0) || (a.y === b.y && b.y === point.y && (b.x - a.x) * (point.x - b.x) >= 0)) result.pop();
                else break;
            }
            result.push(point);
        }
        return result;
    }
    function roundedPath(points, radius = 10) {
        points = simplify(points);
        if (!points.length) return '';
        let path = `M${points[0].x},${points[0].y}`;
        for (let i = 1; i < points.length - 1; i++) {
            const a = points[i - 1], b = points[i], c = points[i + 1], before = distance(a, b), after = distance(b, c);
            const bend = Math.min(radius, before / 2, after / 2);
            const entry = {x: b.x + (a.x - b.x) * bend / before, y: b.y + (a.y - b.y) * bend / before};
            const exit = {x: b.x + (c.x - b.x) * bend / after, y: b.y + (c.y - b.y) * bend / after};
            path += ` L${entry.x},${entry.y} Q${b.x},${b.y} ${exit.x},${exit.y}`;
        }
        const last = points[points.length - 1];
        return path + ` L${last.x},${last.y}`;
    }
    function clear(a, b, obstacles) {
        return !obstacles.some(rect => a.x === b.x
            ? a.x > rect.left + .01 && a.x < rect.right - .01 && Math.max(a.y, b.y) > rect.top + .01 && Math.min(a.y, b.y) < rect.bottom - .01
            : a.y > rect.top + .01 && a.y < rect.bottom - .01 && Math.max(a.x, b.x) > rect.left + .01 && Math.min(a.x, b.x) < rect.right - .01);
    }
    function score(points) {
        points = simplify(points);
        return points.reduce((total, point, i) => total + (i ? distance(points[i - 1], point) : 0), 0) + Math.max(0, points.length - 2) * 24;
    }
    function search(a, b, obstacles, horizontal) {
        const xs = [...new Set([a.x, b.x, ...obstacles.flatMap(rect => [rect.left, rect.right])])].sort((x, y) => x - y);
        const ys = [...new Set([a.y, b.y, ...obstacles.flatMap(rect => [rect.top, rect.bottom])])].sort((x, y) => x - y);
        const width = xs.length, size = width * ys.length, start = ys.indexOf(a.y) * width + xs.indexOf(a.x), finish = ys.indexOf(b.y) * width + xs.indexOf(b.x);
        const costs = new Float64Array(size * 2).fill(Infinity), previous = new Int32Array(size * 2).fill(-1), heap = [], visible = new Map();
        function push(state, cost, estimate) {
            const item = {state, cost, estimate}; let index = heap.length; heap.push(item);
            while (index) { const parent = (index - 1) >> 1; if (heap[parent].estimate <= estimate) break; heap[index] = heap[parent]; index = parent; }
            heap[index] = item;
        }
        function pop() {
            const item = heap[0], tail = heap.pop();
            if (heap.length) {
                let index = 0;
                while (index * 2 + 1 < heap.length) {
                    let child = index * 2 + 1;
                    if (child + 1 < heap.length && heap[child + 1].estimate < heap[child].estimate) child++;
                    if (heap[child].estimate >= tail.estimate) break;
                    heap[index] = heap[child]; index = child;
                }
                heap[index] = tail;
            }
            return item;
        }
        const initial = start * 2 + (horizontal ? 0 : 1);
        costs[initial] = 0; push(initial, 0, distance(a, b));
        while (heap.length) {
            const current = pop(), cell = current.state >> 1;
            if (current.cost !== costs[current.state]) continue;
            if (cell === finish) {
                const result = [];
                for (let state = current.state; state !== -1; state = previous[state]) { const at = state >> 1; result.push({x: xs[at % width], y: ys[Math.floor(at / width)]}); }
                return result.reverse();
            }
            const x = cell % width, y = Math.floor(cell / width), point = {x: xs[x], y: ys[y]};
            for (const [dx, dy] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
                const nx = x + dx, ny = y + dy;
                if (nx < 0 || nx >= width || ny < 0 || ny >= ys.length) continue;
                const next = ny * width + nx, target = {x: xs[nx], y: ys[ny]}, key = Math.min(cell, next) * size + Math.max(cell, next);
                if ((cell === start || next === finish) && (horizontal ? dx < 0 : dy < 0)) continue;
                if (!visible.has(key)) visible.set(key, clear(point, target, obstacles));
                if (!visible.get(key)) continue;
                const direction = dx ? 0 : 1, state = next * 2 + direction, cost = current.cost + distance(point, target) + (direction !== (current.state & 1) ? 24 : 0);
                if (cost >= costs[state]) continue;
                costs[state] = cost; previous[state] = current.state; push(state, cost, cost + distance(target, b));
            }
        }
        return null;
    }
    function create(rectangles) {
        const layers = new Map();
        function route(a, b, horizontal, source, target) {
            if ((horizontal ? a.y === b.y && a.x <= b.x : a.x === b.x && a.y <= b.y) && clear(a, b, rectangles)) return {points: [a, b], radius: 10};
            for (const padding of [18, 6, 1]) {
                if (!layers.has(padding)) layers.set(padding, rectangles.map(rect => ({id: rect.id, left: rect.left - padding, right: rect.right + padding, top: rect.top - padding, bottom: rect.bottom + padding})));
                const obstacles = layers.get(padding), offset = padding + 10;
                const start = {x: a.x + (horizontal ? offset : 0), y: a.y + (horizontal ? 0 : offset)};
                const finish = {x: b.x - (horizontal ? offset : 0), y: b.y - (horizontal ? 0 : offset)};
                if (!clear(a, start, obstacles.filter(rect => rect.id !== source)) || !clear(finish, b, obstacles.filter(rect => rect.id !== target))) continue;
                const candidates = [[start, {x: finish.x, y: start.y}, finish], [start, {x: start.x, y: finish.y}, finish]];
                const xs = new Set([(start.x + finish.x) / 2, ...obstacles.flatMap(rect => [rect.left, rect.right])]);
                const ys = new Set([(start.y + finish.y) / 2, ...obstacles.flatMap(rect => [rect.top, rect.bottom])]);
                for (const x of xs) candidates.push([start, {x, y: start.y}, {x, y: finish.y}, finish]);
                for (const y of ys) candidates.push([start, {x: start.x, y}, {x: finish.x, y}, finish]);
                let best = null, bestScore = Infinity;
                for (const candidate of candidates) {
                    const middle = simplify(candidate), points = simplify([a, ...middle, b]), cost = score(points);
                    if (middle.length > 1 && (horizontal ? middle[1].x < start.x || middle[middle.length - 2].x > finish.x : middle[1].y < start.y || middle[middle.length - 2].y > finish.y)) continue;
                    if (cost >= bestScore || !candidate.every((point, i) => !i || clear(candidate[i - 1], point, obstacles))) continue;
                    best = points; bestScore = cost;
                }
                const middle = best ? null : search(start, finish, obstacles, horizontal);
                if (best || middle) return {points: best || simplify([a, ...middle, b]), radius: Math.min(10, padding)};
            }
            const lane = Math.max(...rectangles.map(rect => horizontal ? rect.bottom : rect.right), horizontal ? a.y : a.x, horizontal ? b.y : b.x) + 28;
            return {points: horizontal
                ? [a, {x: a.x + 28, y: a.y}, {x: a.x + 28, y: lane}, {x: b.x - 28, y: lane}, {x: b.x - 28, y: b.y}, b]
                : [a, {x: a.x, y: a.y + 28}, {x: lane, y: a.y + 28}, {x: lane, y: b.y - 28}, {x: b.x, y: b.y - 28}, b], radius: 1};
        }
        return (a, b, horizontal, source, target) => { const result = route(a, b, horizontal, source, target); return {...result, path: roundedPath(result.points, result.radius)}; };
    }
    globalThis.MattBenchRouting = {create, roundedPath};
})();
