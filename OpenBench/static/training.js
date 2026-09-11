(() => {
    const form = document.getElementById('training-new-form');
    if (form) {
        const environmentInput = document.getElementById('train-environment');
        const environmentRows = document.getElementById('training-environment-rows');
        const addEnvironment = document.getElementById('training-add-environment');
        const initialEnvironment = environmentInput.value.split('\n').map(value => value.replace(/\r$/, ''));
        environmentInput.hidden = true;
        addEnvironment.hidden = false;
        function persistEnvironment() {
            environmentInput.value = Array.from(environmentRows.querySelectorAll('input'), input => input.value).join('\n');
        }
        function addEnvironmentRow(value = '', focus = false) {
            const row = document.createElement('div');
            row.className = 'training-environment-row';
            const input = document.createElement('input');
            input.type = 'text';
            input.value = value;
            input.placeholder = 'KEY=value';
            input.autocomplete = 'off';
            input.spellcheck = false;
            input.setAttribute('aria-label', 'Environment variable, KEY=value');
            input.setAttribute('aria-describedby', 'training-environment-note');
            input.addEventListener('input', persistEnvironment);
            const remove = document.createElement('button');
            remove.type = 'button';
            remove.className = 'button';
            remove.textContent = '\u00d7';
            remove.setAttribute('aria-label', 'Remove environment variable');
            remove.addEventListener('click', () => {
                row.remove();
                persistEnvironment();
                addEnvironment.focus();
            });
            row.append(input, remove);
            environmentRows.append(row);
            if (focus) input.focus();
        }
        initialEnvironment.forEach(value => addEnvironmentRow(value));
        addEnvironment.addEventListener('click', () => addEnvironmentRow('', true));
        form.addEventListener('submit', persistEnvironment);
        const options = JSON.parse(document.getElementById('training-schedule-options').textContent);
        const datasets = JSON.parse(document.getElementById('training-dataset-options').textContent);
        const engine = document.getElementById('train-engine');
        const schedule = document.getElementById('train-schedule');
        const scheduleLink = document.getElementById('training-schedule-link');
        const datasetInput = document.getElementById('train-dataset');
        const overridesInput = document.getElementById('train-stage_datasets');
        const container = document.getElementById('training-stage-datasets');
        const initialDataset = datasetInput.value;
        const drafts = new Map();
        let overrides;
        try { overrides = JSON.parse(overridesInput.value || '[]'); } catch { overrides = []; }
        if (!Array.isArray(overrides)) overrides = [];
        overrides = overrides.filter(value => value && Number.isInteger(value.stage));
        let previousSchedule = schedule.value;
        const stages = () => {
            const selected = options.find(option => option.id === schedule.value);
            return selected ? selected.stages?.length ? selected.stages : [{start: null, end: null}] : [];
        };
        let pickerNumber = 0;
        let closePicker = null;
        document.addEventListener('pointerdown', event => {
            if (!event.target.closest('.dataset-picker')) closePicker?.();
        });
        function picker(root, selected, label, onChange) {
            root.classList.add('dataset-picker');
            const input = document.createElement('select');
            input.className = 'sr-only';
            input.tabIndex = -1;
            input.setAttribute('aria-hidden', 'true');
            input.required = true;
            input.append(new Option('Choose a dataset', ''), ...datasets.map(dataset => new Option(dataset.name, dataset.id)));
            input.value = selected;
            const trigger = document.createElement('button');
            trigger.type = 'button';
            trigger.className = 'dataset-picker-trigger';
            trigger.setAttribute('aria-label', label);
            trigger.setAttribute('aria-haspopup', 'dialog');
            trigger.setAttribute('aria-expanded', 'false');
            const panel = document.createElement('div');
            panel.className = 'dataset-picker-panel';
            panel.setAttribute('role', 'dialog');
            panel.setAttribute('aria-label', label);
            panel.hidden = true;
            const search = document.createElement('input');
            search.type = 'search';
            search.className = 'dataset-picker-search';
            search.placeholder = 'Search datasets…';
            search.autocomplete = 'off';
            search.setAttribute('aria-label', 'Search by dataset, repository, revision or owner');
            search.setAttribute('role', 'combobox');
            search.setAttribute('aria-autocomplete', 'list');
            search.setAttribute('aria-expanded', 'false');
            const list = document.createElement('div');
            list.id = 'dataset-choices-' + (++pickerNumber);
            list.className = 'dataset-picker-list';
            list.setAttribute('role', 'listbox');
            list.setAttribute('aria-label', label);
            panel.id = list.id + '-panel';
            trigger.setAttribute('aria-controls', panel.id);
            search.setAttribute('aria-controls', list.id);
            let active = -1;
            let visible = [];
            const card = dataset => {
                const content = document.createElement('span');
                content.className = 'dataset-picker-card';
                const name = document.createElement('strong');
                name.textContent = dataset.name;
                const repo = document.createElement('span');
                repo.className = 'dataset-picker-repo';
                repo.textContent = dataset.repo + (dataset.is_owner ? ' · You' : dataset.owner ? ' · ' + dataset.owner : '');
                const revision = document.createElement('span');
                revision.className = 'dataset-picker-revision';
                revision.textContent = dataset.revision || 'Default revision';
                content.append(name, repo, revision);
                return content;
            };
            const items = datasets.map((dataset, index) => {
                const item = document.createElement('div');
                item.id = list.id + '-' + index;
                item.className = 'dataset-picker-option';
                item.setAttribute('role', 'option');
                item.append(card(dataset));
                item.addEventListener('pointerdown', event => event.preventDefault());
                item.addEventListener('click', () => choose(index));
                list.append(item);
                return item;
            });
            const empty = document.createElement('div');
            empty.className = 'dataset-picker-empty';
            empty.setAttribute('role', 'status');
            panel.append(search, list, empty);
            function filter() {
                const terms = search.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
                visible = [];
                datasets.forEach((dataset, index) => {
                    const text = [dataset.name, dataset.repo, dataset.revision, dataset.owner, dataset.is_owner ? 'you' : ''].join(' ').toLowerCase();
                    items[index].hidden = !terms.every(term => text.includes(term));
                    if (!items[index].hidden) visible.push(index);
                });
                empty.hidden = visible.length > 0;
                empty.textContent = datasets.length ? 'No datasets match your search' : 'No registered datasets available';
                activate(visible[0] ?? -1);
            }
            function activate(index) {
                active = index;
                items.forEach((item, i) => item.classList.toggle('is-active', i === active));
                if (items[active]) {
                    search.setAttribute('aria-activedescendant', items[active].id);
                    items[active].scrollIntoView({block: 'nearest'});
                } else search.removeAttribute('aria-activedescendant');
            }
            function close() {
                panel.hidden = true;
                trigger.setAttribute('aria-expanded', 'false');
                search.setAttribute('aria-expanded', 'false');
                search.removeAttribute('aria-activedescendant');
                if (closePicker === close) closePicker = null;
            }
            function open(query = '') {
                closePicker?.();
                closePicker = close;
                panel.hidden = false;
                trigger.setAttribute('aria-expanded', 'true');
                search.setAttribute('aria-expanded', 'true');
                search.value = query;
                search.focus();
                filter();
                const selectedIndex = datasets.findIndex(dataset => dataset.id === input.value);
                if (!query && visible.includes(selectedIndex)) activate(selectedIndex);
            }
            function sync() {
                const match = datasets.find(dataset => dataset.id === input.value);
                trigger.replaceChildren(match ? card(match) : document.createTextNode('Choose a dataset'));
                trigger.classList.toggle('is-placeholder', !match);
                trigger.removeAttribute('aria-invalid');
                items.forEach((item, index) => item.setAttribute('aria-selected', String(datasets[index].id === input.value)));
                onChange(match?.id || '');
            }
            function choose(index) {
                if (!datasets[index]) return;
                input.value = datasets[index].id;
                sync();
                close();
                trigger.focus();
            }
            trigger.addEventListener('click', () => panel.hidden ? open() : close());
            trigger.addEventListener('keydown', event => {
                if (['ArrowDown', 'ArrowUp'].includes(event.key)) {
                    event.preventDefault();
                    open();
                } else if (event.key.length === 1 && event.key !== ' ' && !event.ctrlKey && !event.metaKey && !event.altKey) {
                    event.preventDefault();
                    open(event.key);
                }
            });
            search.addEventListener('input', filter);
            search.addEventListener('keydown', event => {
                if (event.isComposing) return;
                if (event.key === 'Escape') { event.preventDefault(); close(); trigger.focus(); return; }
                if (event.key === 'Enter') { event.preventDefault(); choose(active); return; }
                if (['ArrowDown', 'ArrowUp'].includes(event.key)) {
                    event.preventDefault();
                    const position = visible.indexOf(active) + (event.key === 'ArrowDown' ? 1 : -1);
                    activate(visible[Math.max(0, Math.min(visible.length - 1, position))] ?? -1);
                }
            });
            root.addEventListener('focusout', event => { if (!root.contains(event.relatedTarget)) close(); });
            input.addEventListener('invalid', event => {
                event.preventDefault();
                trigger.setAttribute('aria-invalid', 'true');
                if (!form.querySelector('.dataset-picker-trigger[aria-invalid="true"]:focus')) trigger.focus();
            });
            input.addEventListener('change', sync);
            root.append(input, trigger, panel);
            sync();
        }
        function persist() {
            overridesInput.value = JSON.stringify(overrides);
            datasetInput.value = overrides.find(value => value.stage === 0)?.dataset || '';
        }
        function renderOverrides() {
            closePicker?.();
            container.replaceChildren();
            const ranges = stages();
            overrides = ranges.map((_, index) => overrides.find(value => value.stage === index) || {stage: index, dataset: initialDataset});
            if (!ranges.length) {
                const empty = document.createElement('p');
                empty.className = 'field-note';
                empty.textContent = 'Select a schedule.';
                container.append(empty);
            }
            ranges.forEach((range, index) => {
                const override = overrides.find(value => value.stage === index);
                const row = document.createElement('div');
                row.className = 'training-stage-row';
                const stage = document.createElement('div');
                stage.className = 'training-stage-label';
                const title = document.createElement('strong');
                title.textContent = 'Stage ' + (index + 1);
                const bounds = document.createElement('span');
                bounds.textContent = range.start === null ? 'Entire training run' : 'SB ' + range.start + '–' + range.end;
                stage.append(title, bounds);
                const root = document.createElement('div');
                picker(root, override.dataset, 'Dataset for stage ' + (index + 1), value => { override.dataset = value; persist(); });
                row.append(stage, root);
                container.append(row);
            });
            persist();
        }
        function scheduleChanged() {
            drafts.set(previousSchedule, overrides);
            overrides = drafts.get(schedule.value) || [];
            previousSchedule = schedule.value;
            overrides = overrides.filter(override => override.stage >= 0 && override.stage < stages().length);
            scheduleLink.hidden = !schedule.value;
            if (schedule.value) scheduleLink.href = '/training/schedules/' + schedule.value + '/';
            renderOverrides();
        }
        function updateSchedules() {
            const previous = schedule.value;
            const available = options.filter(option => !option.engine || option.engine === engine.value);
            schedule.replaceChildren(new Option('Choose a schedule', ''), ...available.map(option => new Option(option.name + ' (' + option.scope + ')', option.id)));
            schedule.disabled = false;
            if (available.some(option => option.id === previous)) schedule.value = previous;
            scheduleChanged();
        }
        schedule.addEventListener('change', scheduleChanged);
        engine.addEventListener('change', updateSchedules);
        if (!engine.value && schedule.value) engine.value = options.find(option => option.id === schedule.value)?.engine || '';
        updateSchedules();
        const retention = document.getElementById('train-checkpoint_retention');
        const keepLast = document.getElementById('train-checkpoint_keep_last');
        function updateRetention() {
            const keep = retention.value === 'latest';
            keepLast.disabled = !keep;
            keepLast.required = keep;
            document.getElementById('training-checkpoint-count').hidden = !keep;
            updateSettingsSummary();
        }
        const worker = document.getElementById('train-worker');
        function updateSettingsSummary() {
            document.getElementById('training-settings-summary').textContent = (worker.value ? worker.selectedOptions[0].textContent : 'Automatic worker') + ' · ' + (retention.value === 'all' ? 'Keep all checkpoints' : 'Keep latest ' + (keepLast.value || '…'));
        }
        worker.addEventListener('change', updateSettingsSummary);
        keepLast.addEventListener('input', updateSettingsSummary);
        form.addEventListener('invalid', event => {
            const details = event.target.closest('details');
            if (details) details.open = true;
        }, true);
        retention.addEventListener('change', updateRetention);
        updateRetention();
        form.addEventListener('submit', () => persist());
    }
    const metrics = document.getElementById('long-statblock');
    if (!metrics) return;
    const detail = document.querySelector('.training-detail');
    let lossPoints = [];
    const lossChart = document.getElementById('training-chart');
    const lossSvg = lossChart.querySelector('svg');
    const lossPlot = lossChart.querySelector('.history-chart-plot');
    const lossTooltip = document.getElementById('training-loss-tooltip');
    const lossReadout = document.getElementById('training-loss-readout');
    let lossCoordinates = [];
    let selectedLoss = -1;
    let lossCrosshair;
    let lossMarker;
    const clampLoss = (value, low, high) => Math.max(low, Math.min(high, value));
    function hideLoss() {
        selectedLoss = -1;
        lossTooltip.hidden = true;
        lossCrosshair?.setAttribute('visibility', 'hidden');
        lossMarker?.setAttribute('visibility', 'hidden');
    }
    function showLoss(index, announce = false) {
        if (!lossCoordinates.length) return;
        selectedLoss = clampLoss(index, 0, lossCoordinates.length - 1);
        const point = lossCoordinates[selectedLoss];
        lossCrosshair.setAttribute('x1', point.x);
        lossCrosshair.setAttribute('x2', point.x);
        lossCrosshair.setAttribute('visibility', 'visible');
        lossMarker.setAttribute('cx', point.x);
        lossMarker.setAttribute('cy', point.y);
        lossMarker.setAttribute('visibility', 'visible');
        const text = `Superbatch ${point.step.toLocaleString()}\nLoss ${point.loss}`;
        lossTooltip.textContent = text;
        lossTooltip.hidden = false;
        const matrix = lossSvg.getScreenCTM();
        const bounds = lossPlot.getBoundingClientRect();
        const position = new DOMPoint(point.x, point.y).matrixTransform(matrix);
        const x = position.x - bounds.left;
        const y = position.y - bounds.top;
        const left = x + 12 + lossTooltip.offsetWidth > bounds.width ? x - lossTooltip.offsetWidth - 12 : x + 12;
        lossTooltip.style.left = `${clampLoss(left, 0, bounds.width - lossTooltip.offsetWidth)}px`;
        lossTooltip.style.top = `${clampLoss(y - lossTooltip.offsetHeight - 12, 0, bounds.height - lossTooltip.offsetHeight)}px`;
        if (announce) lossReadout.textContent = text.replace('\n', ', ');
    }
    function drawLoss() {
        const selectedStep = lossCoordinates[selectedLoss]?.step;
        lossChart.hidden = lossPoints.length === 0;
        if (!lossPoints.length) {
            lossCoordinates = [];
            hideLoss();
            return;
        }
        const width = Math.max(180, lossSvg.clientWidth);
        const height = lossSvg.clientHeight;
        const left = 56;
        const right = width - 12;
        const top = 10;
        const bottom = height - 22;
        const low = Math.min(...lossPoints.map(point => point.loss));
        const high = Math.max(...lossPoints.map(point => point.loss));
        const padding = (high - low || Math.abs(low) || 1) * 0.15;
        const minimum = low - padding;
        const maximum = high + padding;
        const first = lossPoints[0].step;
        const last = lossPoints[lossPoints.length - 1].step;
        const x = step => first === last ? (left + right) / 2 : left + (right - left) * (step - first) / (last - first);
        const y = loss => top + (bottom - top) * (maximum - loss) / (maximum - minimum);
        lossCoordinates = lossPoints.map(point => ({...point, x: x(point.step), y: y(point.loss)}));
        const content = document.createDocumentFragment();
        const element = (tag, attributes, text) => {
            const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
            for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
            if (text !== undefined) node.textContent = text;
            return node;
        };
        lossSvg.setAttribute('viewBox', `0 0 ${width} ${height}`);
        for (const value of [minimum, (minimum + maximum) / 2, maximum]) {
            content.append(element('line', {x1: left, x2: right, y1: y(value), y2: y(value), class: 'history-chart-grid'}));
            content.append(element('text', {x: left - 9, y: y(value), 'text-anchor': 'end', 'dominant-baseline': 'middle'}, value.toPrecision(3)));
        }
        const ticks = Math.min(last - first, width < 340 ? 2 : 4);
        for (let i = 0; i <= ticks; i++) {
            const step = ticks ? Math.round(first + (last - first) * i / ticks) : first;
            content.append(element('line', {x1: x(step), x2: x(step), y1: top, y2: bottom, class: 'history-chart-grid'}));
            content.append(element('text', {x: x(step), y: height - 6, 'text-anchor': !ticks ? 'middle' : i === 0 ? 'start' : i === ticks ? 'end' : 'middle'}, step.toLocaleString(undefined, {notation: step >= 10000 ? 'compact' : 'standard'})));
        }
        const points = lossPoints.map(point => `${x(point.step)},${y(point.loss)}`).join(' ');
        if (lossPoints.length > 1) {
            content.append(element('polygon', {points: `${x(first)},${bottom} ${points} ${x(last)},${bottom}`, fill: 'var(--brand)', class: 'history-chart-area'}));
            content.append(element('polyline', {points, stroke: 'var(--brand)', class: 'history-chart-path'}));
        }
        const end = lossPoints[lossPoints.length - 1];
        content.append(element('circle', {cx: x(end.step), cy: y(end.loss), r: 4, fill: 'var(--brand)', class: 'history-chart-endpoint'}));
        lossCrosshair = element('line', {y1: top, y2: bottom, class: 'history-chart-crosshair', visibility: 'hidden'});
        lossMarker = element('circle', {r: 4, class: 'history-chart-hover-point', visibility: 'hidden'});
        content.append(lossCrosshair, lossMarker);
        lossSvg.replaceChildren(content);
        const previous = lossCoordinates.findIndex(point => point.step === selectedStep);
        if (previous >= 0) showLoss(previous);
        else hideLoss();
        document.getElementById('training-loss-range').textContent = `${low.toPrecision(4)} – ${high.toPrecision(4)}`;
    }
    function inspectLoss(event) {
        if (!lossCoordinates.length) return;
        const matrix = lossSvg.getScreenCTM();
        if (!matrix) return;
        const position = new DOMPoint(event.clientX, event.clientY).matrixTransform(matrix.inverse()).x;
        let low = 0;
        let high = lossCoordinates.length - 1;
        while (low < high) {
            const middle = (low + high) >> 1;
            if (lossCoordinates[middle].x < position) low = middle + 1;
            else high = middle;
        }
        const index = low > 0 && position - lossCoordinates[low - 1].x < lossCoordinates[low].x - position ? low - 1 : low;
        showLoss(index);
    }
    lossPlot.addEventListener('pointermove', inspectLoss);
    lossPlot.addEventListener('pointerdown', inspectLoss);
    lossPlot.addEventListener('pointerleave', event => {
        if (event.pointerType === 'mouse') hideLoss();
    });
    lossPlot.addEventListener('pointercancel', hideLoss);
    lossPlot.addEventListener('blur', hideLoss);
    lossPlot.addEventListener('focus', () => {
        if (selectedLoss < 0) showLoss(lossCoordinates.length - 1, true);
    });
    lossPlot.addEventListener('keydown', event => {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End', 'Escape'].includes(event.key)) return;
        event.preventDefault();
        if (event.key === 'Escape') return hideLoss();
        const index = selectedLoss < 0 ? lossCoordinates.length - 1 : selectedLoss;
        if (event.key === 'Home') showLoss(0, true);
        else if (event.key === 'End') showLoss(lossCoordinates.length - 1, true);
        else showLoss(index + (event.key === 'ArrowLeft' ? -1 : 1), true);
    });
    if (typeof ResizeObserver === 'function') new ResizeObserver(drawLoss).observe(lossPlot);
    else window.addEventListener('resize', drawLoss);
    const labels = {
        downloaded_bytes: ['Downloaded', ''], converted_files: ['Converted files', ''],
        loss: ['Loss', ''], superbatch: ['Superbatch', ''], positions_per_second: ['Positions / second', ''], elapsed_seconds: ['Elapsed', ''], remaining_seconds: ['Estimated remaining', ''],
    };
    function update(data) {
        if (data.state) detail.dataset.runState = data.state;
        const lines = [`${detail.dataset.runState === 'TRAINING' ? 'Training progress' : 'Progress'}: ${Number(data.metrics.progress || 0).toFixed(1)}%`];
        for (const [key, [label, unit]] of Object.entries(labels)) {
            const value = data.metrics[key];
            if (value === undefined) continue;
            if (key === 'downloaded_bytes' && detail.dataset.runState !== 'DOWNLOADING') continue;
            let text = typeof value === 'number' ? value.toLocaleString(undefined, {maximumSignificantDigits: 6}) + unit : String(value);
            if (key === 'elapsed_seconds' || key === 'remaining_seconds') text = `${Math.floor(value / 3600)}h ${Math.floor(value / 60) % 60}m ${Math.floor(value % 60)}s`;
            if (key === 'superbatch') {
                text = `${value}${data.metrics.end_superbatch ? ` / ${data.metrics.end_superbatch}` : ''}`;
                if (data.metrics.superbatch_progress !== undefined) text += ` (${Number(data.metrics.superbatch_progress).toFixed(1)}%)`;
            }
            if (key === 'downloaded_bytes') text = `${(value / 1024 ** 3).toFixed(2)} GB`;
            lines.push(`${label}: ${text}`);
        }
        metrics.textContent = lines.join('\n');
        if (data.updated) detail.dataset.updated = data.updated;
        const reportTime = new Date(detail.dataset.updated);
        if (!Number.isNaN(reportTime.getTime())) document.getElementById('training-last-report').textContent = `Last worker report: ${reportTime.toLocaleTimeString()}`;
        lossPoints = (data.history || []).filter(point => Number.isFinite(point.loss) && Number.isFinite(point.step)).sort((a, b) => a.step - b.step);
        drawLoss();

    }
    update({metrics: JSON.parse(document.getElementById('training-initial-metrics').textContent), history: JSON.parse(document.getElementById('training-initial-history').textContent)});
    window.addEventListener('live-training', event => update(event.detail));
})();
