const assert = require('node:assert/strict');
const V = require('../OpenBench/static/builder_values.js');
const stages = V.ranges([{length: 100}, {length: 700}], 'lengths');
assert.deepEqual(stages, [{start: 1, end: 100}, {start: 101, end: 800}]);
assert.deepEqual(V.ranges(stages, 'boundaries'), stages);
assert.equal(V.allocation(stages, 900).message, 'Allocated 800 / 900 SB · 100 SB shortfall');
assert.equal(V.allocation(stages, 700).message, 'Allocated 800 / 700 SB · 100 SB excess');
for (const length of [0, -1, 0.1, NaN, Infinity]) assert.throws(() => V.ranges([{length}], 'lengths'));
assert.deepEqual(V.ranges([{length: 1}, {length: 1}], 'lengths'), [{start: 1, end: 1}, {start: 2, end: 2}]);
assert.deepEqual(V.removeStage(stages, 0), [{start: 1, end: 800}]);
assert.deepEqual(V.removeStage(stages, 1), [{start: 1, end: 800}]);
assert.equal(V.splitStage(stages).at(-1).end, 800);
assert.throws(() => V.splitStage([{start: 1, end: 1}]));
const sequence = {start: 1, end: 800, kind: 'sequence', segments: [
    {start: 1, end: 50, kind: 'cosine', initial: 0.01, final: 0.001},
    {start: 51, end: 800, kind: 'linear', initial: 0.001, final: 0.0001},
]};
const divided = V.splitStage([sequence]);
assert.deepEqual(divided.map(s => [s.start, s.end]), [[1, 400], [401, 800]]);
assert.deepEqual(divided[0].segments.map(s => [s.start, s.end]), [[1, 50], [51, 400]]);
assert.deepEqual(divided[1].segments.map(s => [s.start, s.end]), [[1, 400]]);
assert.equal(divided[1].segments[0].kind, 'linear');
assert.deepEqual(V.removeStage(divided, 1), [sequence]);
assert.deepEqual(V.removeStage(divided, 0)[0].segments.map(s => [s.start, s.end]), [[1, 800]]);
const firstRemoved = V.removeStage([{start: 1, end: 100}, {...sequence, start: 101, end: 900}], 0)[0];
assert.deepEqual(firstRemoved.segments.map(s => [s.start, s.end]), [[1, 150], [151, 900]]);
assert.equal(sequence.segments[1].end, 800); // Operations never mutate the original draft.
const layout = V.importLayout('const X: [usize; 32] = [' + Array.from({length: 32}, (_, i) => Math.floor(i / 4)).join(', /* x */') + ',];', 'a8');
assert.deepEqual(layout.layout.slice(0, 8), Array(8).fill(7));
assert.deepEqual(layout.layout.slice(-8), Array(8).fill(0));
assert.equal(layout.count, 8);
assert.throws(() => V.importLayout(String(Array(32).fill(2))));
assert.throws(() => V.parseArray('[1, 2, oops]'));
assert.throws(() => V.parseArray('[1e999]'));
assert.deepEqual(V.parseArray('[1, // x\n 0,]'), [1, 0]);
const dist = [0, 0, ...Array(31).fill(1 / 31)];
assert.deepEqual(V.distribution(dist, 'target'), dist);
assert.throws(() => V.distribution(Array(33).fill(1), 'target'));
console.log('Builder imports and stage operations passed.');
