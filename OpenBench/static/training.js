(() => {
    const form = document.getElementById('training-new-form');
    if (form) {
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
    const checkpointDetails = document.getElementById('checkpoint-details');
    const checkpointList = document.getElementById('checkpoint-list');
    const checkpointCount = document.getElementById('checkpoint-count');
    const checkpointFeedback = document.getElementById('checkpoint-feedback');
    const loadCheckpoints = document.getElementById('load-checkpoints');
    const finishCheckpoints = document.getElementById('finish-checkpoints');
    const checkpointRows = new Map();
    let checkpointCursor = null;
    let checkpointLoading = false;
    let loadedCount = -1;
    let loadedLatest = null;
    let latestCheckpoint = detail.dataset.checkpointLatest || '';
    const checkpointsChanged = () => loadedCount !== Number(checkpointCount.textContent) || loadedLatest !== latestCheckpoint;
    const terminal = () => ['COMPLETED', 'FAILED', 'CANCELLED'].includes(detail.dataset.runState);
    function checkpointActions() {
        if (finishCheckpoints) finishCheckpoints.disabled = !terminal() || !checkpointRows.size;
        for (const row of checkpointRows.values()) {
            row.querySelector('button')?.toggleAttribute('disabled', !terminal());
        }
    }
    async function fetchCheckpoints(older = false) {
        if (checkpointLoading) return;
        checkpointLoading = true;
        loadCheckpoints.disabled = true;
        checkpointFeedback.textContent = 'Loading checkpoints…';
        try {
            const suffix = older && checkpointCursor ? `?before=${checkpointCursor}` : '';
            const response = await fetch(`/training/${detail.dataset.runId}/checkpoints/${suffix}`, {headers: {Accept: 'application/json'}});
            if (!response.ok || response.redirected) throw new Error('Unable to load checkpoints. Check your connection and try again.');
            const data = await response.json();
            const selected = new Set([...checkpointRows.entries()].filter(([, row]) => row.querySelector('input').checked).map(([id]) => id));
            if (!older) {
                checkpointRows.clear();
                checkpointList.replaceChildren();
            }
            for (const item of data.checkpoints) {
                if (checkpointRows.has(item.id)) continue;
                const row = document.createElement('div');
                row.className = 'checkpoint-row';
                row.dataset.superbatch = item.superbatch;
                const label = document.createElement('label');
                const checkbox = document.createElement('input');
                checkbox.type = 'checkbox';
                checkbox.name = 'keep';
                checkbox.value = item.id;
                checkbox.checked = item.imported || selected.has(item.id);
                checkbox.disabled = item.imported || !finishCheckpoints;
                label.append(checkbox, document.createTextNode(`SB ${item.superbatch}${item.imported ? ' · imported' : ''}`));
                const size = document.createElement('span');
                size.textContent = `${(item.size / 1024 ** 2).toLocaleString(undefined, {maximumFractionDigits: 1})} MB`;
                size.title = `SHA-256 ${item.sha256}`;
                row.append(label, size);
                for (const [id, title] of [[item.archive, 'Checkpoint'], [item.network, 'Network']]) {
                    const link = document.createElement('a');
                    link.href = `/training/${detail.dataset.runId}/artifacts/${id}/`;
                    link.textContent = title;
                    link.setAttribute('aria-label', `Download ${title.toLowerCase()} for superbatch ${item.superbatch}`);
                    row.append(link);
                }
                if (finishCheckpoints && detail.dataset.resumable === 'true') {
                    const resume = document.createElement('button');
                    resume.className = 'button';
                    resume.type = 'submit';
                    resume.name = 'action';
                    resume.value = 'resume';
                    resume.textContent = 'Resume';
                    resume.setAttribute('aria-label', `Resume from superbatch ${item.superbatch}`);
                    resume.addEventListener('click', () => { document.getElementById('resume-checkpoint-id').value = item.id; });
                    row.append(resume);
                }
                checkpointRows.set(item.id, row);
            }
            const sorted = [...checkpointRows.values()].sort((a, b) => Number(b.dataset.superbatch) - Number(a.dataset.superbatch));
            for (const row of sorted) checkpointList.append(row);
            checkpointCursor = data.next;
            loadedCount = data.total;
            if (!older) loadedLatest = String(data.latest || '');
            checkpointCount.textContent = data.total;
            loadCheckpoints.hidden = checkpointRows.size >= data.total;
            loadCheckpoints.textContent = 'Load older checkpoints';
            checkpointFeedback.textContent = data.total ? `${checkpointRows.size} of ${data.total} checkpoints` : 'No checkpoints yet';
            checkpointActions();
        } catch (error) {
            checkpointFeedback.textContent = error.message;
            loadCheckpoints.hidden = false;
            loadCheckpoints.textContent = 'Retry loading checkpoints';
        } finally {
            checkpointLoading = false;
            loadCheckpoints.disabled = false;
        }
    }
    checkpointDetails.addEventListener('toggle', () => {
        if (checkpointDetails.open && checkpointsChanged()) fetchCheckpoints();
    });
    loadCheckpoints.addEventListener('click', () => fetchCheckpoints(loadedCount >= 0));
    document.getElementById('checkpoint-form').addEventListener('submit', event => {
        if (event.submitter?.value === 'finish-checkpoints' && !window.confirm('Import selected networks and delete all remaining checkpoint archives and unimported checkpoint networks?')) event.preventDefault();
    });
    const labels = {
        loss: ['Loss', ''], superbatch: ['Superbatch', ''], positions_per_second: ['Positions / second', ''], elapsed_seconds: ['Elapsed', ''],
    };
    const resourceLabels = {
        gpu_percent: ['GPU use', '%'], gpu_used_mb: ['GPU memory', ' MB'], cpu_percent: ['CPU use', '%'],
        ram_used_gb: ['RAM', ' GB'], disk_free_gb: ['Storage free', ' GB'], learning_rate: ['Learning rate', ''],
        downloaded_bytes: ['Downloaded', ''], converted_files: ['Converted files', ''],
        checkpoints_saved: ['Checkpoints stored', ''], last_checkpoint_superbatch: ['Latest checkpoint SB', ''], dataset_games: ['Dataset games', ''],
    };
    function update(data) {
        if (data.state) detail.dataset.runState = data.state;
        if (data.checkpoint_count !== undefined) checkpointCount.textContent = data.checkpoint_count;
        if (data.checkpoint_latest !== undefined) latestCheckpoint = String(data.checkpoint_latest || '');
        checkpointActions();
        if (checkpointDetails.open && checkpointsChanged()) fetchCheckpoints();
        const states = {VALIDATING: 'Checking inputs', PREPARING: 'Checking inputs', QUEUED: 'Waiting for worker', DOWNLOADING: 'Downloading', CONVERTING: 'Converting', COMPILING: 'Compiling', TRAINING: 'Training', SAVING: 'Saving outputs', COMPLETED: 'Completed', FAILED: 'Failed', CANCELLED: 'Cancelled'};
        const lines = [`${detail.dataset.runName} · ${detail.dataset.engine}`, states[detail.dataset.runState] || detail.dataset.runState, `Progress: ${Number(data.metrics.progress || 0).toFixed(1)}%`];
        for (const [key, [label, unit]] of Object.entries({...labels, ...resourceLabels})) {
            const value = data.metrics[key];
            if (value === undefined) continue;
            let text = typeof value === 'number' ? value.toLocaleString(undefined, {maximumSignificantDigits: 6}) + unit : String(value);
            if (key === 'elapsed_seconds') text = `${Math.floor(value / 3600)}h ${Math.floor(value / 60) % 60}m`;
            if (key === 'downloaded_bytes') text = `${(value / 1024 ** 3).toFixed(2)} GB`;
            if (key in labels || ['learning_rate', 'downloaded_bytes', 'converted_files'].includes(key)) {
                lines.push(`${label}: ${text}`);
                continue;
            }
            const container = document.getElementById('training-resources');
            let item = container.querySelector(`[data-metric="${key}"]`);
            if (!item) {
                item = document.createElement('div');
                item.dataset.metric = key;
                const title = document.createElement('dt');
                title.textContent = label;
                item.append(title, document.createElement('dd'));
                container.append(item);
            }
            item.lastElementChild.textContent = text;
        }
        metrics.textContent = lines.join('\n');
        const points = (data.history || []).filter(point => Number.isFinite(point.loss));
        if (points.length > 1) {
            document.getElementById('training-chart').hidden = false;
            const low = Math.min(...points.map(point => point.loss));
            const high = Math.max(...points.map(point => point.loss));
            const range = high - low || 1;
            document.getElementById('training-loss-line').setAttribute('d', points.map((point, index) => `${index ? 'L' : 'M'}${(index * 796 / (points.length - 1) + 2).toFixed(1)},${(190 - 180 * (point.loss - low) / range).toFixed(1)}`).join(' '));
            document.getElementById('training-loss-range').textContent = `${low.toPrecision(4)} – ${high.toPrecision(4)}`;
        }
        const log = document.getElementById('training-log');
        if (data.log !== undefined && log.textContent !== data.log) {
            const scroll = log.scrollTop;
            log.textContent = data.log;
            log.scrollTop = document.getElementById('training-follow-log').checked ? log.scrollHeight : scroll;
        }
    }
    update({metrics: JSON.parse(document.getElementById('training-initial-metrics').textContent), history: JSON.parse(document.getElementById('training-initial-history').textContent)});
    window.addEventListener('live-training', event => update(event.detail));
})();
