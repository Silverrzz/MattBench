const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// Exercise the real initial render and live-training event handler without a browser dependency.
const elements = new Map();
for (const id of ['long-statblock', 'training-chart', 'training-loss-tooltip', 'training-loss-readout',
    'training-last-report', 'training-initial-metrics', 'training-initial-history']) {
    elements.set(id, {textContent: '', addEventListener() {}, querySelector() { return this; }});
}
const detail = {dataset: {runState: 'DOWNLOADING'}};
const initial = {progress: 5.20833, phase_progress: 28.2037, downloaded_bytes: 15 * 1024 ** 3};
elements.get('training-initial-metrics').textContent = JSON.stringify(initial);
elements.get('training-initial-history').textContent = '[]';
const handlers = new Map();
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../OpenBench/static/training.js'), 'utf8'), {
    document: {getElementById: id => elements.get(id), querySelector: () => detail},
    window: {addEventListener: (event, callback) => handlers.set(event, callback)},
});
const text = () => elements.get('long-statblock').textContent;
const update = (state, metrics) => handlers.get('live-training')({detail: {state, metrics}});
assert.equal(text(), 'Progress: 28.2% (Downloading)\nOverall training: 5.2%\nDownloaded: 15.00 GB');
update('DOWNLOADING', {...initial, phase_progress: 40});
assert.match(text(), /^Progress: 40.0% \(Downloading\)\nOverall training: 5.2%/);
for (const [state, label] of Object.entries({DOWNLOADING: 'Downloading', CONVERTING: 'Converting', COMPILING: 'Compiling', SAVING: 'Uploading'})) {
    update(state, {progress: 50, phase_progress: 0});
    assert.equal(text(), `Progress: 0.0% (${label})\nOverall training: 50.0%`);
    for (const metrics of [{progress: 12.5}, {progress: 12.5, phase_progress: null}, {}]) {
        update(state, metrics);
        assert.equal(text(), `Progress: ${(metrics.progress || 0).toFixed(1)}% (${label})`);
    }
}
for (const state of ['TRAINING', 'QUEUED', 'FAILED', 'CANCELLED', 'COMPLETED']) {
    update(state, {progress: 50, phase_progress: 99});
    assert.equal(text(), `Progress: ${state === 'COMPLETED' ? '100.0' : '50.0'}%`);
}
update('TRAINING', {progress: 50, phase_progress: 99, stage: 2, stage_count: 3});
assert.equal(text(), 'Progress: 50.0% (Stage 2 of 3)');
console.log('Training progress initial render and live-update regressions passed.');
