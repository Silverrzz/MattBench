/* Pure builder operations, also exercised by the Node tests. */
(() => {
    const parseArray = text => {
        if (typeof text !== 'string' || text.length > 65536) throw Error('Paste a numeric array.');
        text = text.replace(/\/\*[\s\S]*?\*\/|\/\/[^\n]*/g, '').trim();
        if (text.includes('=')) {
            const index = text.indexOf('=');
            if (!/^\s*(?:(?:pub\s+)?const|static|let)\s+[A-Za-z_]\w*\s*(?::[^=]+)?\s*$/.test(text.slice(0, index))) throw Error('Invalid array declaration.');
            text = text.slice(index + 1).trim();
        }
        text = text.replace(/;$/, '').trim();
        if (text.startsWith('[') && text.endsWith(']')) text = text.slice(1, -1).trim();
        const tokens = text.replace(/,$/, '').trim().split(/[\s,]+/);
        if (!tokens.length || tokens.some(token => !/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(token))) throw Error('The entire array must contain only numbers, separators and comments.');
        const values = tokens.map(Number);
        if (values.some(value => !Number.isFinite(value))) throw Error('Array values must be finite.');
        return values;
    };
    const importLayout = (text, order = 'a1', mirrored = true) => {
        const values = parseArray(text);
        if (!['a1', 'a8'].includes(order) || ![32, 64].includes(values.length)) throw Error('Paste 32 or 64 bucket IDs and choose an orientation.');
        if (values.some(value => !Number.isInteger(value) || value < 0 || value > 63)) throw Error('Bucket IDs must be integers from 0 to 63.');
        const width = values.length / 8;
        let rows = Array.from({length: 8}, (_, rank) => values.slice(rank * width, (rank + 1) * width));
        if (width === 4) { rows = rows.map(row => [...row, ...[...row].reverse()]); mirrored = true; }
        if (order === 'a8') rows.reverse();
        if (mirrored && rows.some(row => row.some((value, i) => value !== row[7 - i]))) throw Error('The layout is asymmetric. Choose unmirrored or correct it.');
        const count = Math.max(...values) + 1;
        if (new Set(values).size !== count) throw Error('Bucket IDs must be contiguous, starting at zero.');
        return {layout: rows.flat(), count, mirrored};
    };
    const distribution = (values, mode) => {
        if (values.length !== 33 || values.some(p => !Number.isFinite(p) || p < 0 || p > 1)) throw Error('Enter 33 finite values between 0 and 1, indexed 0–32.');
        if (mode === 'target' && Math.abs(values.reduce((a, b) => a + b, 0) - 1) > 0.00001) throw Error('Target proportions must sum to approximately one (within 0.00001).');
        return values;
    };
    const ranges = (stages, mode) => {
        let start = 1;
        return stages.map(stage => {
            const end = mode === 'lengths' ? start + stage.length - 1 : stage.end;
            const length = end - start + 1;
            if (!Number.isInteger(length) || length <= 0 || !Number.isInteger(end)) throw Error('Each stage needs a positive integer length and an inclusive end at or after its start.');
            const result = {...stage, start, end};
            delete result.length;
            start = end + 1;
            return result;
        });
    };
    const allocation = (stages, total) => {
        const allocated = stages.at(-1)?.end || 0;
        const difference = allocated - total;
        return {allocated, valid: difference === 0, message: 'Allocated ' + allocated + ' / ' + total + ' SB' + (difference ? ' · ' + Math.abs(difference) + ' SB ' + (difference > 0 ? 'excess' : 'shortfall') : '')};
    };
    const splitStage = stages => {
        const result = structuredClone(stages);
        const last = result.at(-1);
        if (!last || last.end <= last.start) throw Error('The last stage needs at least two superbatches to split.');
        const end = last.end;
        last.end = Math.floor((last.start + end) / 2);
        if (last.kind === 'sequence') {
            const cut = last.end - last.start + 1;
            const slice = (start, end) => last.segments.filter(s => s.start <= end && s.end >= start)
                .map(s => ({...s, start: Math.max(s.start, start) - start + 1, end: Math.min(s.end, end) - start + 1}));
            const first = slice(1, cut), second = slice(cut + 1, end - last.start + 1);
            last.segments = first;
            result.push({...last, start: last.end + 1, end, segments: second});
        } else result.push({...last, start: last.end + 1, end, kind: 'constant', initial: last.final, final: last.final});
        return result;
    };
    const removeStage = (stages, index) => {
        if (stages.length < 2) throw Error('Keep at least one stage.');
        const result = structuredClone(stages);
        const duration = result[index].end - result[index].start + 1;
        const neighbor = result[index ? index - 1 : 1];
        if (neighbor.kind === 'sequence') {
            const segments = neighbor.segments.map(s => ({...s, length: s.end - s.start + 1}));
            segments[index ? segments.length - 1 : 0].length += duration;
            neighbor.segments = ranges(segments, 'lengths');
        }
        if (index) result[index - 1].end = result[index].end;
        else result[1].start = 1;
        result.splice(index, 1);
        return result;
    };
    const api = {parseArray, importLayout, distribution, ranges, allocation, splitStage, removeStage};
    if (typeof module !== 'undefined') module.exports = api;
    else window.BuilderValues = api;
})();
