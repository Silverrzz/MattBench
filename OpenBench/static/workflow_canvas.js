(() => {
    const form = document.getElementById('workflow-editor');
    if (!form) return;
    const data = JSON.parse(document.getElementById('workflow-data').textContent);
    const byId = id => document.getElementById('workflow-' + id);
    const canvas = byId('canvas'), world = byId('world'), nodes = byId('nodes'), wires = byId('wires');
    const inspector = byId('inspector'), body = byId('inspector-body'), palette = byId('palette');
    const feedback = byId('feedback'), name = byId('name');
    const token = form.querySelector('[name=csrfmiddlewaretoken]').value;
    const types = {
        load_data: {name: 'Load data', icon: 'folder-open', color: '#8cbab5', detail: 'Repository, dataset or workload'},
        load_parameters: {name: 'Load SPSA parameters', icon: 'sliders', color: '#c4ab84', detail: 'Tune results or parameter values'},
        load_network: {name: 'Load network', icon: 'microchip', color: '#d6b57e', detail: 'A registered network'},
        load_checkpoint: {name: 'Load checkpoint', icon: 'floppy-disk', color: '#d6b57e', detail: 'Optimizer state and network'},
        load_artifact: {name: 'Load artifact', icon: 'file', color: '#a7aec6', detail: 'A workload output file'},
        datagen: {name: 'Generate data', icon: 'database', color: '#8cbab5', detail: 'Self-play games'},
        merge: {name: 'Merge datasets', icon: 'code-merge', color: '#8cbab5', detail: 'Combine any number of sources'},
        select: {name: 'Select files', icon: 'filter', color: '#8cbab5', detail: 'Route matching and remaining files'},
        split: {name: 'Split dataset', icon: 'code-branch', color: '#8cbab5', detail: 'Reproducible game-level partitions'},
        convert: {name: 'Convert dataset', icon: 'arrows-rotate', color: '#8cbab5', detail: 'Prepare the profile’s data format'},
        tune: {name: 'SPSA tune', icon: 'sliders', color: '#c4ab84', detail: 'Optimize engine parameters'},
        task: {name: 'Custom task', icon: 'terminal', color: '#a7aec6', detail: 'Commands with typed inputs and outputs'},
        upload: {name: 'Upload dataset', icon: 'cloud-arrow-up', color: '#b7a5d7', detail: 'Hugging Face'},
        train: {name: 'Train network', icon: 'bolt', color: '#d6b57e', detail: 'Run a training schedule'},
        test: {name: 'Engine test', icon: 'chess-knight', color: '#9db4df', detail: 'SPRT or fixed games'},
        decision: {name: 'Condition', icon: 'code-branch', color: '#d9a1b0', detail: 'Branch on a metric'},
        approval: {name: 'Approval', icon: 'hand', color: '#adb2be', detail: 'Wait for your decision'},
    };
    const outcomeNames = {completed: 'Done', pass: 'Pass', fail: 'Fail', inconclusive: 'Inconclusive', error: 'Error', cancelled: 'Cancelled'};
    let definition = structuredClone(data.definition), identifier = data.id, version = data.version;
    let selection = null, gesture = null, armed = null, insertion = null, saving = false, space = false;
    let past = [], future = [], saved = '', wheelTimer;
    const nodeElements = new Map();
    const make = (tag, text, className) => { const item = document.createElement(tag); if (text !== undefined) item.textContent = text; if (className) item.className = className; return item; };
    const icon = value => { const item = make('i', undefined, 'fa-solid fa-' + value); item.setAttribute('aria-hidden', 'true'); return item; };
    const snapshot = () => JSON.stringify({definition, name: name.value});
    const remember = () => { const value = snapshot(); if (past[past.length - 1] !== value) past.push(value); if (past.length > 60) past.shift(); future = []; };
    const changed = () => {
        byId('undo').disabled = !past.length; byId('redo').disabled = !future.length;
        if (!saving) { feedback.textContent = snapshot() === saved ? (identifier ? 'Saved' : '') : 'Unsaved'; feedback.dataset.error = 'false'; }
    };
    function normalize() {
        definition.layout ||= {nodes: {}, viewport: {x: 0, y: 0, zoom: 1}};
        definition.layout.nodes ||= {}; definition.layout.viewport ||= {x: 0, y: 0, zoom: 1};
        const levels = new Map(), columns = new Map();
        function level(id, seen = new Set()) {
            if (levels.has(id)) return levels.get(id); if (seen.has(id)) return 0;
            const parents = definition.edges.filter(edge => edge.to === id);
            const result = parents.length ? 1 + Math.max(...parents.map(edge => level(edge.from, new Set([...seen, id])))) : 0;
            levels.set(id, result); return result;
        }
        for (const step of definition.steps) {
            step.config ||= {}; step.inputs ||= {}; step.worker ||= {};
            const row = level(step.id), column = columns.get(row) || 0; columns.set(row, column + 1);
            definition.layout.nodes[step.id] ||= {x: 160 + column * 310, y: 130 + row * 220};
        }
        for (const step of definition.steps) for (const [input, value] of Object.entries(step.inputs)) for (let reference of Array.isArray(value) ? value : [value]) {
            if (typeof reference === 'string') { const source = definition.steps.find(row => row.id === reference); if (!source) continue; const kind = data.ports[step.kind]?.[0]?.[input]?.replace('?', '').replace('[]', ''); const output = Object.entries(data.ports[source.kind][1]).find(([key, type]) => type === kind)?.[0]; reference = {node: reference, port: output}; }
            if (reference.port && !definition.edges.some(edge => edge.from === reference.node && edge.to === step.id && edge.output === reference.port && edge.input === input)) {
                const route = definition.edges.find(edge => edge.from === reference.node && edge.to === step.id && !edge.output); definition.edges.push({from: reference.node, to: step.id, output: reference.port, input, on: route?.on || ['completed', 'pass']});
            }
        }
    }
    const hadLayout = Boolean(definition.layout);
    normalize(); name.value = data.name; saved = snapshot();
    const camera = () => definition.layout.viewport;
    function point(clientX, clientY) {
        const rect = canvas.getBoundingClientRect(), view = camera();
        return {x: (clientX - rect.left - view.x) / view.zoom, y: (clientY - rect.top - view.y) / view.zoom};
    }
    function updateCamera() {
        const view = camera(); world.style.transform = `translate(${view.x}px, ${view.y}px) scale(${view.zoom})`;
        canvas.style.backgroundSize = `${24 * view.zoom}px ${24 * view.zoom}px`; canvas.style.backgroundPosition = `${view.x}px ${view.y}px`;
        byId('zoom').textContent = Math.round(view.zoom * 100) + '%';
    }
    function zoom(value, clientX, clientY) {
        const rect = canvas.getBoundingClientRect(); clientX ??= rect.left + rect.width / 2; clientY ??= rect.top + rect.height / 2;
        const before = point(clientX, clientY), view = camera(); view.zoom = Math.max(.15, Math.min(2, value));
        view.x = clientX - rect.left - before.x * view.zoom; view.y = clientY - rect.top - before.y * view.zoom; updateCamera(); changed();
    }
    function fit() {
        if (!definition.steps.length) { Object.assign(camera(), {x: 0, y: 0, zoom: 1}); updateCamera(); return; }
        const bounds = [...nodeElements].map(([id, element]) => ({...definition.layout.nodes[id], width: element.offsetWidth, height: element.offsetHeight})), rect = canvas.getBoundingClientRect();
        const left = Math.min(...bounds.map(p => p.x)), top = Math.min(...bounds.map(p => p.y));
        const width = Math.max(...bounds.map(p => p.x + p.width)) - left, height = Math.max(...bounds.map(p => p.y + p.height)) - top;
        const available = rect.width - (inspector.hidden ? 0 : 320), scale = Math.min(1, Math.max(.15, Math.min((available - 130) / width, (rect.height - 180) / height)));
        Object.assign(camera(), {zoom: scale, x: (available - width * scale) / 2 - left * scale, y: (rect.height - height * scale) / 2 - top * scale}); updateCamera(); changed();
    }
    const find = id => definition.steps.find(step => step.id === id);
    const assetNames = {base_network: 'Baseline net', base_parameters: 'Baseline params', data: 'Data', dataset: 'Data', network: 'Network', checkpoint: 'Checkpoint', parameters: 'Parameters', metrics: 'Metrics', source: 'Metrics', artifact: 'Artifact', training: 'Training', validation: 'Validation', matched: 'Matched', remaining: 'Remaining'};
    const assetColors = {dataset: '#8cbab5', parameters: '#c4ab84', network: '#d6b57e', checkpoint: '#b9a1d2', metrics: '#9db4df', artifact: '#adb2be'};
    function ports(step, direction) { return step.kind === 'task' ? step.config[direction === 0 ? 'inputs' : 'outputs'] || data.ports.task[direction] : data.ports[step.kind][direction]; }
    const baseType = kind => kind.replace('?', '').replace('[]', '');
    function outcomes(step) { return [...new Set([...(['test', 'decision', 'approval'].includes(step.kind) ? ['pass', 'fail'] : ['completed']), ...definition.edges.filter(edge => edge.from === step.id && !edge.output).flatMap(edge => edge.on), ...(step.config.outcome_ports || [])])]; }
    function summary(step) {
        if (step.kind === 'datagen') return `${Number(step.config.parameters?.datagen_max_games || step.config.games || 10000000).toLocaleString()} games`;
        if (step.kind === 'upload') return step.config.repo || 'Choose a destination';
        if (step.kind === 'train') return 'files' in step.config ? 'Source files · ' + Object.keys(step.config.files || {}).length : step.config.spec ? step.config.spec.layers.join(' → ') + ' · ' + step.config.spec.superbatches + ' SB' : data.schedules.find(row => row.id === step.config.schedule)?.name || 'Configure training';
        if (step.kind === 'test') return step.config.parameters ? [step.config.parameters.test_mode === 'GAMES' ? Number(step.config.parameters.test_max_games).toLocaleString() + ' games' : 'SPRT', step.config.parameters.dev_time_control].filter(Boolean).join(' · ') : ({nodes: `${Number(step.config.nodes || 25000).toLocaleString()} nodes`, stc: 'Short time control', ltc: 'Long time control'})[step.config.control] || 'Configure test';
        if (step.kind === 'decision') return `${step.config.field || 'loss'} ${({lt: '<', lte: '≤', gt: '>', gte: '≥', eq: '='})[step.config.operator] || '<'} ${step.config.value ?? 0}`;
        if (step.kind === 'load_data') return ({huggingface: step.config.repo || 'Choose data', registry: data.datasets.find(row => row.id === step.config.dataset)?.name, datagen: step.config.workload ? 'Workload #' + step.config.workload : ''})[step.config.provider || 'huggingface'] || 'Choose data';
        if (step.kind === 'load_parameters') return step.config.provider === 'spsa' ? 'Tune #' + (step.config.workload || '…') : 'Parameter values';
        if (step.kind === 'load_network') return data.networks.find(row => row.sha256 === step.config.network)?.name || 'Choose network';
        if (step.kind === 'split') return Math.round((step.config.validation_fraction ?? .1) * 100) + '% validation';
        if (step.kind === 'merge') return definition.edges.filter(edge => edge.to === step.id && edge.output).length + ' sources';
        if (step.kind === 'task') return (step.config.commands?.length || 0) + ' commands';
        if (step.kind === 'select') return (step.config.patterns || ['*']).join(', ');
        return types[step.kind].detail;
    }
    function portPoint(id, key, input = false) {
        const node = nodeElements.get(id), port = [...node.querySelectorAll(input ? '[data-input]' : '[data-output]')].find(item => (input ? item.dataset.input : item.dataset.output) === key);
        if (!port) return definition.layout.nodes[id];
        const rect = port.getBoundingClientRect(), origin = world.getBoundingClientRect(), scale = camera().zoom;
        return {x: (rect.left + rect.width / 2 - origin.left) / scale, y: (rect.top + rect.height / 2 - origin.top) / scale};
    }
    const curve = (a, b, horizontal) => { const bend = Math.max(65, Math.abs((horizontal ? b.x - a.x : b.y - a.y)) * .45); return horizontal ? `M${a.x},${a.y} C${a.x + bend},${a.y} ${b.x - bend},${b.y} ${b.x},${b.y}` : `M${a.x},${a.y} C${a.x},${a.y + bend} ${b.x},${b.y - bend} ${b.x},${b.y}`; };
    function svg(tag, attrs) { const item = document.createElementNS('http://www.w3.org/2000/svg', tag); for (const [key, value] of Object.entries(attrs)) item.setAttribute(key, value); return item; }
    function drawWires(pointer) {
        wires.replaceChildren();
        const route = MattBenchRouting.create(definition.steps.flatMap(step => {
            const id = step.id, element = nodeElements.get(id), position = definition.layout.nodes[id];
            return element && position ? [{id, left: position.x, top: position.y, right: position.x + element.offsetWidth, bottom: position.y + element.offsetHeight}] : [];
        }));
        for (const edge of definition.edges) for (const output of edge.output ? [edge.output] : edge.on.map(value => '@' + value)) {
            if (!nodeElements.has(edge.from) || !nodeElements.has(edge.to)) continue;
            const edgeOutput = edge.output || '@' + edge.on[0];
            const selected = selection?.from === edge.from && selection?.to === edge.to && selection?.output === edgeOutput && selection?.input === (edge.input || '@control');
            const group = svg('g', {class: 'wf-wire-group' + (selected ? ' is-selected' : '')}), path = route(portPoint(edge.from, output), portPoint(edge.to, edge.input || '@control', true), Boolean(edge.output), edge.from, edge.to).path;
            if (edge.output) group.style.setProperty('--wire-color', assetColors[ports(find(edge.from), 1)[edge.output]] || '#adb2be');
            group.append(svg('path', {d: path, class: 'wf-wire'}), svg('path', {d: path, class: 'wf-wire-hit'}));
            group.addEventListener('pointerdown', event => { if (event.button !== 0 || space) return; event.stopPropagation(); select({from: edge.from, to: edge.to, output: edgeOutput, input: edge.input || '@control'}); }); wires.append(group);
        }
        if (armed && pointer) wires.append(svg('path', {d: curve(portPoint(armed.id, armed.output), pointer, !armed.output.startsWith('@')), class: 'wf-wire wf-wire-preview'}));
    }
    function renderNodes() {
        nodes.replaceChildren(); nodeElements.clear(); byId('empty').hidden = definition.steps.length > 0; byId('start').disabled = !definition.steps.length || saving;
        for (const step of definition.steps) {
            const type = types[step.kind], item = make('article', undefined, 'wf-node'); item.dataset.node = step.id; item.tabIndex = 0;
            item.setAttribute('aria-label', `${step.name}, ${type.name}`); item.style.setProperty('--node-color', type.color);
            const position = definition.layout.nodes[step.id]; item.style.transform = `translate(${position.x}px, ${position.y}px)`; item.classList.toggle('is-selected', selection === step.id);
            const input = make('button', undefined, 'wf-port wf-port-in'); input.type = 'button'; input.dataset.input = '@control'; input.setAttribute('aria-label', `Trigger ${step.name}`); input.title = 'Control input';
            input.addEventListener('click', () => { if (armed) connect(armed.id, step.id, armed.output, '@control'); });
            const header = make('div', undefined, 'wf-node-header'), badge = make('span', undefined, 'wf-node-icon'); badge.append(icon(type.icon));
            const heading = make('div', undefined, 'wf-node-heading'); heading.append(make('span', type.name, 'wf-node-kind'), make('span', step.name, 'wf-node-name')); header.append(badge, heading);
            const sockets = make('div', undefined, 'wf-node-sockets');
            for (const direction of [0, 1]) {
                const column = make('div', undefined, direction ? 'wf-socket-outputs' : 'wf-socket-inputs');
                for (const [key, kind] of Object.entries(ports(step, direction))) {
                    const slot = make('div', (assetNames[key] || key) + (kind.includes('[]') ? ' +' : ''), 'wf-socket'); slot.style.setProperty('--asset-color', assetColors[baseType(kind)]);
                    const port = make('button', undefined, 'wf-port'); port.type = 'button'; port.dataset[direction ? 'output' : 'input'] = key;
                    port.setAttribute('aria-label', `${step.name}: ${direction ? 'output' : 'input'} ${key}`); port.title = baseType(kind) + (kind.endsWith('?') ? ' · optional' : '');
                    port.addEventListener('click', event => { if (direction && event.detail === 0) { armed = {id: step.id, output: key}; canvas.classList.add('is-connecting'); } else if (!direction && armed) connect(armed.id, step.id, armed.output, key); }); slot.append(port); column.append(slot);
                }
                sockets.append(column);
            }
            const outputs = make('div', undefined, 'wf-node-outputs');
            for (const outcome of outcomes(step)) {
                const slot = make('div', outcomeNames[outcome], 'wf-output'); slot.dataset.outcome = outcome;
                const port = make('button', undefined, 'wf-port wf-port-out'); port.type = 'button'; port.dataset.output = '@' + outcome; port.setAttribute('aria-label', `${step.name}: ${outcomeNames[outcome]}`); port.title = `Route on ${outcomeNames[outcome].toLowerCase()}`;
                port.addEventListener('click', event => { if (event.detail === 0) { armed = {id: step.id, output: '@' + outcome}; canvas.classList.add('is-connecting'); } }); slot.append(port); outputs.append(slot);
            }
            item.append(input, header, make('div', summary(step), 'wf-node-summary'), sockets, outputs);
            item.addEventListener('keydown', event => {
                if (event.target !== item) return;
                if (event.key === 'Enter') { select(step.id); return; }
                if (event.key.startsWith('Arrow')) { event.preventDefault(); remember(); const shift = event.shiftKey ? 50 : 10; position.x += ({ArrowLeft: -shift, ArrowRight: shift})[event.key] || 0; position.y += ({ArrowUp: -shift, ArrowDown: shift})[event.key] || 0; item.style.transform = `translate(${position.x}px, ${position.y}px)`; drawWires(); changed(); }
            });
            nodes.append(item); nodeElements.set(step.id, item);
        }
        updateCamera(); drawWires(); changed();
    }
    function field(root, label, value, update, options, type = 'text') {
        const wrapper = make('label', label, 'wf-field' + (type === 'checkbox' ? ' wf-field-check' : '')), input = make(options ? 'select' : type === 'textarea' ? 'textarea' : 'input');
        input.setAttribute('aria-label', label);
        if (options) input.append(...options.map(row => new Option(row.name, String(row.id)))); else if (type !== 'textarea') input.type = type;
        if (type === 'checkbox') input.checked = Boolean(value); else input.value = value ?? ''; if (type === 'number') input.step = 'any';
        input.addEventListener('change', () => { remember(); update(type === 'checkbox' ? input.checked : type === 'number' ? Number(input.value) : input.value); renderNodes(); }); wrapper.append(input); root.append(wrapper); return input;
    }
    function section(label) { const details = make('details'); details.append(make('summary', label)); body.append(details); return details; }
    const choices = (items, label) => [{id: '', name: label}, ...items];
    function select(value) { selection = value; inspector.hidden = value === null; for (const [id, item] of nodeElements) item.classList.toggle('is-selected', id === value); if (value !== null) renderInspector(); drawWires(); }
    function jsonField(root, label, value, update, validate = () => {}) {
        const wrapper = make('label', label, 'wf-field'), input = make('textarea'); input.setAttribute('aria-label', label); input.value = JSON.stringify(value, null, 2); input.spellcheck = false; input.rows = Math.min(14, input.value.split('\n').length + 1);
        input.addEventListener('input', () => input.setCustomValidity(''));
        input.addEventListener('change', () => { try { const parsed = JSON.parse(input.value); validate(parsed); remember(); update(parsed); input.setCustomValidity(''); renderNodes(); } catch (error) { input.setCustomValidity(error.message); input.reportValidity(); } }); wrapper.append(input); root.append(wrapper); return input;
    }
    function parameterText(value) {
        if (typeof value === 'string') {
            try { return parameterText(JSON.parse(value)); } catch { return value; }
        }
        return Object.entries(value || {}).map(([name, number]) => `${name}, ${number}`).join('\n');
    }
    const configDialog = byId('config-dialog'), configFrame = byId('config-frame');
    let nodeEditor = null;
    byId('config-close').addEventListener('click', () => configDialog.close());
    configDialog.addEventListener('close', () => { nodeEditor = null; configFrame.src = 'about:blank'; });
    window.addEventListener('message', event => {
        if (event.origin !== location.origin || event.source !== configFrame.contentWindow || !nodeEditor) return;
        if (event.data?.type === 'mattbench-node-ready') configFrame.contentWindow.postMessage({type: 'mattbench-node-init', payload: nodeEditor.payload}, location.origin);
        if (event.data?.type === 'mattbench-node-config') {
            remember(); nodeEditor.apply(event.data.value); configDialog.close(); renderNodes(); renderInspector();
        }
    });
    function configure(label, url, payload, apply) {
        const button = make('button', label, 'wf-button wf-configure'); button.type = 'button';
        button.addEventListener('click', () => {
            nodeEditor = {payload, apply}; byId('config-title').textContent = label;
            configFrame.title = label; configFrame.src = url + (url.includes('?') ? '&' : '?') + 'workflow=1'; configDialog.showModal();
        }); body.append(button);
    }
    function scheduleStages(spec) {
        if (!spec) return [];
        const boundaries = [...new Set([...spec.lr_stages, ...spec.wdl_stages].map(stage => stage.start).concat(spec.superbatches + 1))].sort((a, b) => a - b);
        return boundaries.slice(0, -1).map((start, index) => ({start, end: boundaries[index + 1] - 1}));
    }
    function renderInspector() {
        const expanded = [...body.querySelectorAll('details[open]')].map(item => item.querySelector('summary').textContent);
        body.replaceChildren();
        if (selection === 'settings') {
            byId('inspector-title').textContent = 'Workflow defaults';
            field(body, 'Engine', definition.engine, value => { definition.engine = value; }, choices(data.engines, 'Choose when needed'));
            field(body, 'Training profile', definition.profile, value => { definition.profile = value; for (const step of definition.steps) if (step.kind === 'train' && !step.config.profile && !('files' in step.config) && !step.config.schedule && step.config.schedule_mode !== 'saved') step.config.spec = structuredClone(data.schedule_defaults[value]); }, data.profiles); return;
        }
        if (typeof selection === 'object' && selection) {
            byId('inspector-title').textContent = 'Connection';
            const selected = selection, edge = definition.edges.find(item => item.from === selected.from && item.to === selected.to && (item.output || '@' + item.on[0]) === selected.output && (item.input || '@control') === selected.input);
            body.append(make('p', `${find(selected.from).name} → ${find(selected.to).name}`, 'wf-inline-note'));
            if (edge) {
                if (edge.output) body.append(make('p', `${assetNames[edge.output] || edge.output} → ${assetNames[edge.input] || edge.input}`, 'wf-inline-note'));
                const multiple = edge.on.length > 1 && edge.on.length < Object.keys(outcomeNames).length;
                field(body, 'When', multiple ? 'selected' : edge.on.length > 1 ? 'any' : edge.on[0], value => { if (value === 'selected') return; edge.on = value === 'any' ? Object.keys(outcomeNames) : [value]; if (!edge.output) selection.output = '@' + edge.on[0]; renderNodes(); }, [...(multiple ? [{id: 'selected', name: edge.on.map(id => outcomeNames[id]).join(' or ')}] : []), {id: 'any', name: 'Any outcome'}, ...Object.keys(outcomeNames).map(id => ({id, name: outcomeNames[id]}))]);
            }
            const remove = make('button', 'Remove connection', 'wf-danger'); remove.type = 'button'; remove.addEventListener('click', deleteSelection); body.append(remove); return;
        }
        const step = find(selection); if (!step) { select(null); return; }
        byId('inspector-title').textContent = types[step.kind].name;
        field(body, 'Name', step.name, value => { step.name = value; });
        const config = step.config, engine = config.engine || definition.engine, profileId = config.profile || definition.profile;
        const profile = data.profiles.find(row => row.id === profileId), engineName = data.engines.find(row => String(row.id) === engine)?.name;
        if (step.kind === 'train') field(body, 'Engine', config.engine || '', value => { config.engine = value; renderInspector(); }, choices(data.engines, engineName ? 'Default · ' + engineName : 'Choose engine'));
        if (['datagen', 'test', 'tune'].includes(step.kind)) {
            const parameters = config.parameters || {};
            const values = {...(engineName ? {dev_engine: engineName} : {}), ...parameters};
            if (config.games && !('datagen_max_games' in values)) values.datagen_max_games = String(config.games);
            if (config.control && config.control !== 'template' && !('dev_time_control' in values)) values.dev_time_control = values.base_time_control = config.control === 'nodes' ? 'N=' + (config.nodes || 25000) : config.time_control || (config.control === 'ltc' ? '60.0+0.6' : '10.0+0.1');
            configure('Configure ' + ({test: 'test', tune: 'tune', datagen: 'datagen'}[step.kind]), '/' + step.kind + '/new/', {values, preset: config.preset, edited: config.edited || []}, result => {
                config.parameters = result.values; config.preset = result.preset; config.edited = result.edited;
                config.engine = data.engines.find(row => row.name === result.values.dev_engine)?.id || '';
                config.control = 'template'; delete config.games;
            });
            body.append(make('p', config.parameters ? [parameters.dev_engine, parameters.dev_branch, step.kind === 'test' ? parameters.test_mode : '', parameters.dev_time_control].filter(Boolean).join(' · ') : 'Choose an engine preset or configure this workload.', 'wf-inline-note'));
        }
        if (step.kind === 'upload') {
            field(body, 'Dataset repository', config.repo, value => { config.repo = value; }).placeholder = 'username/dataset';
            field(body, 'Private dataset', config.private ?? true, value => { config.private = value; }, null, 'checkbox');
        }
        if (step.kind === 'load_data') {
            field(body, 'Source', config.provider || 'huggingface', value => { config.provider = value; renderInspector(); }, [{id: 'huggingface', name: 'Hugging Face'}, {id: 'registry', name: 'Dataset library'}, {id: 'datagen', name: 'Datagen workload'}]);
            if (!config.provider || config.provider === 'huggingface') {
                field(body, 'Repository', config.repo, value => { config.repo = value; }).placeholder = 'username/dataset';
                field(body, 'Revision', config.ref || 'main', value => { config.ref = value; });
                field(body, 'File patterns', (config.patterns || ['*']).join('\n'), value => { config.patterns = value.split('\n').filter(Boolean); }, null, 'textarea');
            } else if (config.provider === 'registry') field(body, 'Dataset', config.dataset, value => { config.dataset = value; }, choices(data.datasets, 'Choose dataset'));
            else field(body, 'Datagen workload', config.workload, value => { config.workload = Number(value); }, choices(data.datagens, 'Choose workload'));
        }
        if (step.kind === 'load_parameters') {
            field(body, 'Source', config.provider || 'inline', value => { config.provider = value; renderInspector(); }, [{id: 'inline', name: 'Paste SPSA outputs'}, {id: 'spsa', name: 'SPSA workload'}]);
            if (config.provider === 'spsa') field(body, 'Tune', config.workload, value => { config.workload = Number(value); }, choices(data.tunes, 'Choose tune'));
            else {
                const input = field(body, 'Parameters', parameterText(config.values), value => { config.values = value; }, null, 'textarea');
                input.placeholder = 'Parameter, 123\nOtherParameter, 0.5'; input.rows = 8; input.spellcheck = false;
            }
        }
        if (step.kind === 'load_network') field(body, 'Network', config.network, value => { config.network = value; }, choices(data.networks.map(row => ({id: row.sha256, name: row.engine + ' / ' + row.name})), 'Choose network'));
        if (step.kind === 'load_checkpoint') field(body, 'Checkpoint', config.checkpoint, value => { config.checkpoint = Number(value); }, choices(data.checkpoints, 'Choose checkpoint'));
        if (step.kind === 'load_artifact') {
            field(body, 'Source', config.provider || 'training', value => { config.provider = value; renderInspector(); }, [{id: 'training', name: 'Training workload'}, {id: 'workflow', name: 'Workflow workload'}, {id: 'huggingface', name: 'Hugging Face file'}]);
            if (config.provider === 'huggingface') {
                field(body, 'Repository', config.repo, value => { config.repo = value; });
                field(body, 'Repository type', config.repo_type || 'dataset', value => { config.repo_type = value; }, [{id: 'dataset', name: 'Dataset'}, {id: 'model', name: 'Model'}]);
                field(body, 'Revision', config.ref || 'main', value => { config.ref = value; });
                field(body, 'File path', config.path, value => { config.path = value; });
            } else field(body, 'Artifact', config.artifact, value => { config.artifact = Number(value); }, choices(config.provider === 'workflow' ? data.workflow_artifacts : data.artifacts, 'Choose artifact'));
        }
        if (step.kind === 'select') field(body, 'Matching file patterns', (config.patterns || ['*']).join('\n'), value => { config.patterns = value.split('\n').filter(Boolean); }, null, 'textarea');
        if (['train', 'convert', 'split'].includes(step.kind)) field(body, 'Training profile', config.profile || '', value => { config.profile = value; if (step.kind === 'train') { if (config.schedule_mode === 'saved') delete config.schedule; else if (!('files' in config)) config.spec = structuredClone(data.schedule_defaults[value || definition.profile]); } renderInspector(); }, choices(data.profiles, profile ? 'Default · ' + profile.name : 'Workflow default'));
        if (step.kind === 'train' && !profile) { body.append(make('p', 'Choose an available training profile.', 'wf-inline-note')); return; }
        if (step.kind === 'split') {
            field(body, 'Validation fraction', config.validation_fraction ?? .1, value => { config.validation_fraction = value; }, null, 'number');
            field(body, 'Seed', config.seed ?? 42, value => { config.seed = value; }, null, 'number');
        }
        if (step.kind === 'convert') field(body, 'Shuffle games', config.shuffle ?? false, value => { config.shuffle = value; }, null, 'checkbox');
        if (step.kind === 'train') {
            const selectedSchedule = data.schedules.find(row => row.id === config.schedule);
            const sourceMode = config.schedule_mode || ('files' in config ? 'source' : config.schedule && !config.spec ? 'saved' : 'builder');
            field(body, 'Schedule', sourceMode, value => {
                delete config.files; delete config.schedule; delete config.spec;
                if (value === 'builder') config.spec = structuredClone(data.schedule_defaults[profileId]);
                else if (value === 'source') config.files = {[profile.runtime_settings.example_source]: ''};
                else config.schedule = '';
                config.schedule_mode = value;
                renderInspector();
            }, [{id: 'builder', name: 'Visual builder'}, {id: 'source', name: 'Source files'}, {id: 'saved', name: 'Saved schedule'}]);
            if (sourceMode !== 'builder') {
                if (sourceMode === 'source') {
                    const entry = profile.runtime_settings.example_source;
                    field(body, entry, config.files[entry] || '', value => { config.files[entry] = value; }, null, 'textarea');
                    const files = section('Source bundle');
                    jsonField(files, 'Files', config.files, value => { if (!value || Array.isArray(value) || typeof value !== 'object') throw Error('Use an object mapping file paths to source text.'); config.files = value; });
                    jsonField(files, 'Runtime settings', config.settings || {}, value => { if (!value || Array.isArray(value) || typeof value !== 'object') throw Error('Use a settings object.'); config.settings = value; });
                } else field(body, 'Saved schedule', config.schedule || '', value => { config.schedule = value; renderInspector(); }, choices(data.schedules.filter(row => row.profile === profileId), 'Choose schedule'));
                if (selectedSchedule) { const link = make('a', 'Edit schedule source', 'wf-inline-note'); link.href = '/training/schedules/' + selectedSchedule.id + '/'; body.append(link); }
            } else {
                const spec = config.spec ||= structuredClone(data.schedule_defaults[profileId]);
                configure('Edit training schedule', '/training/schedules/builder/?profile=' + encodeURIComponent(profileId), {spec}, result => { config.spec = result.spec; config.profile = result.spec.training_profile; });
                const schedule = section('Schedule');
                field(schedule, 'Copy saved schedule', '', value => { const row = data.schedules.find(row => row.id === value); if (row?.spec) { config.spec = structuredClone(row.spec); renderInspector(); } }, choices(data.schedules.filter(row => row.profile === profileId && row.spec), 'Choose schedule'));
            }
            const stages = sourceMode === 'builder' ? scheduleStages(config.spec) : sourceMode === 'saved' ? selectedSchedule?.stages || [] : [];
            const values = {name: 'wf-' + step.id, engine, training_profile: profileId, schedule: sourceMode === 'saved' ? config.schedule : '@workflow', dataset: '@workflow', stage_datasets: JSON.stringify((stages.length ? stages : [{}]).map((_, stage) => ({stage, datasets: ['@workflow']}))), checkpoint: step.inputs.checkpoint ? '@workflow' : '', ...config.training};
            values.engine = engine;
            values.schedule = sourceMode === 'saved' ? config.schedule : '@workflow';
            values.training_profile = profileId;
            const scheduleOption = sourceMode === 'saved' ? null : {id: '@workflow', name: 'Node schedule', scope: 'Workflow', engine, profile: profileId, stages, workloads: sourceMode === 'builder'};
            configure('Configure training run', '/training/new/', {values, schedule: scheduleOption, checkpoint_input: true}, result => {
                config.training = result.values; config.engine = result.values.engine;
                if (result.values.schedule !== '@workflow') { config.schedule = result.values.schedule; config.schedule_mode = 'saved'; delete config.spec; delete config.files; config.profile = data.schedules.find(row => row.id === config.schedule)?.profile || profileId; }
            });
        }
        if (step.kind === 'decision') {
            field(body, 'Metric', config.field || 'loss', value => { config.field = value; });
            field(body, 'Condition', config.operator || 'lt', value => { config.operator = value; }, [{id: 'lt', name: 'Less than'}, {id: 'lte', name: 'At most'}, {id: 'gt', name: 'Greater than'}, {id: 'gte', name: 'At least'}, {id: 'eq', name: 'Equal'}]);
            field(body, 'Value', config.value ?? 0, value => { config.value = value; }, null, 'number');
        }
        if (step.kind === 'task') {
            jsonField(body, 'Commands', config.commands || [], value => { config.commands = value; });
            const recipe = section('Recipe');
            field(recipe, 'Compute', config.accelerator || 'cpu', value => { config.accelerator = value; }, [{id: 'cpu', name: 'CPU'}, {id: 'cuda', name: 'CUDA GPU'}, {id: 'rocm', name: 'ROCm GPU'}]);
            field(recipe, 'Engine for network outputs', config.engine || '', value => { config.engine = value; }, choices(data.engines, 'Choose if needed'));
            field(recipe, 'Repository', config.repository?.url, value => { (config.repository ||= {}).url = value; });
            field(recipe, 'Pinned commit', config.repository?.commit, value => { (config.repository ||= {}).commit = value; });
            field(recipe, 'Execution image', config.execution_image, value => { config.execution_image = value; });
            const io = section('Ports and outputs');
            jsonField(io, 'Input ports', config.inputs || {data: 'artifact[]?'}, value => { changePorts(step, 0, value); }, value => validatePorts(0, value));
            jsonField(io, 'Output ports', config.outputs || {artifact: 'artifact'}, value => { changePorts(step, 1, value); }, value => validatePorts(1, value));
            jsonField(io, 'Output patterns', config.files || {artifact: 'outputs/*'}, value => { config.files = value; });
        }
        if (['datagen', 'train', 'test', 'tune', 'convert', 'split', 'task', 'upload'].includes(step.kind)) {
            const worker = section('Worker settings');
            field(worker, 'Worker', step.worker.require || '', value => { step.worker.require = value; }, choices(data.workers, 'Any eligible worker'));
            field(worker, 'CPU allocation', step.worker.cpu || 'inherit', value => { step.worker.cpu = value; renderInspector(); }, [{id: 'inherit', name: 'Worker default'}, {id: 'physical', name: 'Physical cores'}, {id: 'logical', name: 'All logical CPUs'}, {id: 'limit', name: 'Fixed thread count'}]);
            if (step.worker.cpu === 'limit') field(worker, 'Threads', step.worker.threads || 1, value => { step.worker.threads = value; }, null, 'number');
            field(worker, 'Prefer worker', step.worker.prefer || '', value => { step.worker.prefer = value; renderInspector(); }, choices(data.workers, 'No preference'));
            if (step.worker.prefer) field(worker, 'Fallback after (seconds)', step.worker.fallback_seconds ?? 300, value => { step.worker.fallback_seconds = value; }, null, 'number');
            field(worker, 'Tags', (step.worker.tags || []).join(', '), value => { step.worker.tags = value.split(',').map(item => item.trim()).filter(Boolean); });
            if (['datagen', 'test', 'tune'].includes(step.kind)) field(worker, 'Worker limit (0 = any)', step.worker.max_workers || 0, value => { step.worker.max_workers = value; }, null, 'number');
        }
        const advanced = section('Advanced');
        field(advanced, 'Incoming routes', step.join || 'all', value => { step.join = value; }, [{id: 'all', name: 'Require all'}, {id: 'any', name: 'Allow any'}]);
        field(advanced, 'Retry errors', step.retries || 0, value => { step.retries = value; }, null, 'number');
        field(advanced, 'Deadline (hours)', step.timeout_hours || 168, value => { step.timeout_hours = value; }, null, 'number');
        if (step.kind === 'test') field(advanced, 'Game cap (0 = none)', step.config.max_games || 0, value => { step.config.max_games = value; }, null, 'number');
        field(advanced, 'Show outcome port', '', value => { if (value) (step.config.outcome_ports ||= []).push(value); }, choices(Object.keys(outcomeNames).filter(id => !outcomes(step).includes(id) && (['test', 'decision', 'approval'].includes(step.kind) ? id !== 'completed' : !['pass', 'fail'].includes(id))).map(id => ({id, name: outcomeNames[id]})), 'Choose outcome'));
        const footer = make('div', undefined, 'wf-inspector-footer'), remove = make('button', 'Delete step', 'wf-danger'); remove.type = 'button'; remove.addEventListener('click', deleteSelection); footer.append(remove); body.append(footer);
        for (const details of body.querySelectorAll('details')) details.open = expanded.includes(details.querySelector('summary').textContent);
    }
    function ancestors(id, seen = new Set()) { for (const edge of definition.edges.filter(edge => edge.to === id)) if (!seen.has(edge.from)) { seen.add(edge.from); ancestors(edge.from, seen); } return seen; }
    function validatePorts(direction, value) {
        const types = Object.keys(assetColors);
        if (!value || Array.isArray(value) || typeof value !== 'object' || Object.keys(value).length > 32 || Object.entries(value).some(([key, kind]) => !/^[a-z][a-z0-9_]{0,63}$/.test(key) || typeof kind !== 'string' || !(direction ? types.includes(kind) : types.some(type => [type, type + '?', type + '[]', type + '[]?'].includes(kind))))) throw Error('Use up to 32 named ports with valid asset types.');
    }
    function changePorts(step, direction, value) {
        step.config[direction ? 'outputs' : 'inputs'] = value;
        const removed = definition.edges.length;
        definition.edges = definition.edges.filter(edge => {
            if (!edge.output || edge.from !== step.id && edge.to !== step.id) return true;
            const source = ports(find(edge.from), 1)[edge.output], target = ports(find(edge.to), 0)[edge.input];
            return source && target && (baseType(target) === source || baseType(target) === 'artifact');
        });
        if (direction) {
            step.config.files = Object.fromEntries(Object.keys(value).map(key => [key, step.config.files?.[key] || 'outputs/' + key + '/*']));
        }
        resetInteraction(); inferInputs();
        if (removed !== definition.edges.length) feedback.textContent = 'Removed incompatible connections for the updated ports.';
    }
    function inferInputs() {
        for (const step of definition.steps) {
            step.inputs = {};
            for (const edge of definition.edges.filter(edge => edge.to === step.id && edge.output)) {
                const kind = ports(step, 0)[edge.input]; if (!kind) continue;
                const reference = {node: edge.from, port: edge.output};
                if (kind.includes('[]')) (step.inputs[edge.input] ||= []).push(reference); else step.inputs[edge.input] = reference;
            }
        }
    }
    function connect(from, to, output, input, record = true) {
        const source = find(from), target = find(to), control = typeof output === 'string' && output.startsWith('@');
        if (!source || !target || (control ? !outcomes(source).includes(output.slice(1)) : !ports(source, 1)[output])) { resetInteraction(); drawWires(); return; }
        if (from === to || ancestors(from).has(to)) { feedback.textContent = 'Connections cannot form a loop.'; feedback.dataset.error = 'true'; return; }
        if (!input) {
            const compatible = Object.entries(ports(target, 0)).filter(([key, kind]) => baseType(kind) === ports(source, 1)[output] || baseType(kind) === 'artifact');
            input = control ? '@control' : compatible.length === 1 ? compatible[0][0] : null;
        }
        if (!input || control && input !== '@control' || !control && (!ports(target, 0)[input] || baseType(ports(target, 0)[input]) !== 'artifact' && ports(source, 1)[output] !== baseType(ports(target, 0)[input]))) { feedback.textContent = 'Connect to an input with a matching asset type.'; feedback.dataset.error = 'true'; return; }
        const existing = definition.edges.find(edge => edge.from === from && edge.to === to && (control ? !edge.output : edge.output === output && edge.input === input));
        if (existing && (!control || existing.on.includes(output.slice(1)))) { resetInteraction(); renderNodes(); select(to); return; }
        if (!control && !ports(target, 0)[input].includes('[]') && definition.edges.some(edge => edge.to === to && edge.input === input)) { feedback.textContent = 'This input already has a source. Use Merge datasets to combine data.'; return; }
        if (record) remember();
        const on = control ? [output.slice(1)] : ['test', 'decision', 'approval'].includes(source.kind) ? ['pass'] : ['completed'];
        if (existing) existing.on.push(...on); else definition.edges.push({from, to, on, ...(!control ? {output, input} : {})});
        inferInputs(); resetInteraction(); renderNodes(); select(to);
    }
    function removeEdge(target) { definition.edges = definition.edges.filter(edge => !(edge.from === target.from && edge.to === target.to && (edge.output || '@' + edge.on[0]) === target.output && (edge.input || '@control') === target.input)); inferInputs(); }
    function resetInteraction() {
        const pointerId = gesture?.pointerId;
        armed = null; gesture = null;
        canvas.classList.remove('is-dragging', 'is-connecting');
        for (const item of nodeElements.values()) item.classList.remove('is-connect-target');
        if (pointerId !== undefined && canvas.hasPointerCapture(pointerId)) canvas.releasePointerCapture(pointerId);
        changed();
    }
    function deleteSelection() {
        if (!selection || selection === 'settings') return; remember();
        if (typeof selection === 'string') { definition.steps = definition.steps.filter(step => step.id !== selection); definition.edges = definition.edges.filter(edge => edge.from !== selection && edge.to !== selection); delete definition.layout.nodes[selection]; inferInputs(); } else removeEdge(selection);
        resetInteraction(); renderNodes(); select(null);
    }
    function openPalette(position = null) { insertion = position; palette.hidden = false; byId('add').setAttribute('aria-expanded', 'true'); byId('search').value = ''; renderPalette(''); byId('search').focus(); }
    function closePalette() { palette.hidden = true; byId('add').setAttribute('aria-expanded', 'false'); }
    function renderPalette(query) {
        const root = byId('step-types'); root.replaceChildren();
        for (const [kind, type] of Object.entries(types)) if ((type.name + ' ' + type.detail).toLowerCase().includes(query.toLowerCase())) {
            const button = make('button', undefined, 'wf-palette-item'); button.type = 'button'; button.style.setProperty('--node-color', type.color);
            const badge = make('span', undefined, 'wf-palette-icon'); badge.append(icon(type.icon)); const label = make('span', type.name); label.append(make('small', type.detail)); button.append(badge, label); button.addEventListener('click', () => addStep(kind)); root.append(button);
        }
    }
    function addStep(kind) {
        if (definition.steps.length >= 64) { feedback.textContent = 'A workflow supports up to 64 steps.'; return; } remember();
        const id = 'step-' + crypto.randomUUID().slice(0, 8), rect = canvas.getBoundingClientRect(), previous = typeof selection === 'string' ? definition.layout.nodes[selection] : null;
        const position = insertion ? {x: insertion.x - 118, y: insertion.y - 60} : previous ? {x: previous.x + 330, y: previous.y} : point(rect.left + rect.width * .4 - 118, rect.top + rect.height * .4 - 60);
        while (Object.values(definition.layout.nodes).some(p => Math.abs(p.x - position.x) < 280 && Math.abs(p.y - position.y) < 220)) { position.x += 330; }
        const config = structuredClone(({datagen: {games: 10000000, parameters: {}}, test: {control: 'stc', parameters: {}}, tune: {parameters: {}}, train: {spec: data.schedule_defaults[definition.profile]}, upload: {private: true}, decision: {field: 'loss', operator: 'lt', value: 0}, task: {commands: [], inputs: {data: 'artifact[]?'}, outputs: {artifact: 'artifact'}, files: {artifact: 'outputs/*'}}})[kind] || {});
        definition.steps.push({id, name: types[kind].name, kind, config, inputs: {}, worker: {}, join: 'all', retries: 0, timeout_hours: 168}); definition.layout.nodes[id] = position;
        closePalette(); renderNodes(); select(id);
        const right = position.x * camera().zoom + camera().x + 280; if (right > rect.width - 340) camera().x -= right - rect.width + 340;
        updateCamera(); drawWires(); nodeElements.get(id).focus({preventScroll: true});
    }
    function restore(direction) {
        const source = direction === 'undo' ? past : future, destination = direction === 'undo' ? future : past;
        if (!source.length) return;
        const current = snapshot(); let value;
        do { value = source.pop(); } while (value === current && source.length);
        if (value === current) { changed(); return; }
        destination.push(current); value = JSON.parse(value); definition = value.definition; name.value = value.name; normalize(); resetInteraction(); renderNodes(); select(null);
    }
    canvas.addEventListener('pointerdown', event => {
        if (gesture) return;
        if (event.button !== 0 && event.button !== 1) return;
        const node = event.target.closest('[data-node]'), output = event.target.closest('[data-output]'), input = event.target.closest('[data-input]');
        if (input && armed && event.button === 0 && !space) { event.preventDefault(); connect(armed.id, node.dataset.node, armed.output, input.dataset.input); return; }
        if (output && event.button === 0 && !space) { event.preventDefault(); armed = {id: node.dataset.node, output: output.dataset.output}; gesture = {kind: 'wire', pointerId: event.pointerId, startX: event.clientX, startY: event.clientY}; canvas.classList.add('is-connecting'); canvas.setPointerCapture(event.pointerId); return; }
        if (event.button === 1 || space || !node) {
            if (event.target.closest('.wf-wire-hit') && event.button === 0 && !space) return;
            if (armed) { armed = null; drawWires(); canvas.classList.remove('is-connecting'); }
            if (!node) select(null);
            gesture = {kind: 'pan', x: camera().x, y: camera().y, startX: event.clientX, startY: event.clientY};
        } else { select(node.dataset.node); const position = definition.layout.nodes[node.dataset.node]; gesture = {kind: 'node', id: node.dataset.node, x: position.x, y: position.y, startX: event.clientX, startY: event.clientY, recorded: false}; }
        gesture.pointerId = event.pointerId; closePalette(); canvas.setPointerCapture(event.pointerId); event.preventDefault();
    });
    canvas.addEventListener('pointermove', event => {
        if (!gesture) { if (armed) drawWires(point(event.clientX, event.clientY)); return; }
        if (gesture.pointerId !== event.pointerId) return;
        const dx = event.clientX - gesture.startX, dy = event.clientY - gesture.startY;
        if (gesture.kind === 'wire') {
            drawWires(point(event.clientX, event.clientY)); const target = document.elementFromPoint(event.clientX, event.clientY)?.closest('[data-node]');
            for (const [id, item] of nodeElements) item.classList.toggle('is-connect-target', target === item && id !== armed?.id);
        } else if (gesture.kind === 'pan') { camera().x = gesture.x + dx; camera().y = gesture.y + dy; updateCamera(); canvas.classList.add('is-dragging'); }
        else if (Math.hypot(dx, dy) > 3 || gesture.recorded) {
            if (!gesture.recorded) { remember(); gesture.recorded = true; }
            const position = definition.layout.nodes[gesture.id]; position.x = gesture.x + dx / camera().zoom; position.y = gesture.y + dy / camera().zoom;
            nodeElements.get(gesture.id).style.transform = `translate(${position.x}px, ${position.y}px)`; drawWires();
        }
    });
    function endGesture(event) {
        if (gesture && gesture.pointerId !== event.pointerId) return;
        if (gesture?.kind === 'wire' && armed) {
            const target = document.elementFromPoint(event.clientX, event.clientY)?.closest('[data-node]');
            if (target && target.dataset.node !== armed.id) connect(armed.id, target.dataset.node, armed.output, document.elementFromPoint(event.clientX, event.clientY)?.closest('[data-input]')?.dataset.input);
            else if (Math.hypot(event.clientX - gesture.startX, event.clientY - gesture.startY) > 5) { armed = null; drawWires(); }
        }
        gesture = null; canvas.classList.remove('is-dragging'); canvas.classList.toggle('is-connecting', Boolean(armed));
        for (const item of nodeElements.values()) item.classList.remove('is-connect-target');
        if (canvas.hasPointerCapture(event.pointerId)) canvas.releasePointerCapture(event.pointerId); changed();
    }
    canvas.addEventListener('pointerup', endGesture);
    canvas.addEventListener('pointercancel', () => { resetInteraction(); drawWires(); });
    canvas.addEventListener('lostpointercapture', event => { if (gesture?.pointerId === event.pointerId) { resetInteraction(); drawWires(); } });
    canvas.addEventListener('wheel', event => {
        event.preventDefault();
        if (event.ctrlKey || !event.deltaX && Math.abs(event.deltaY) >= 35) zoom(camera().zoom * Math.exp(-event.deltaY * .0025), event.clientX, event.clientY);
        else { camera().x -= event.deltaX; camera().y -= event.deltaY; updateCamera(); }
        clearTimeout(wheelTimer); wheelTimer = setTimeout(changed, 150);
    }, {passive: false});
    canvas.addEventListener('dblclick', event => { if (!event.target.closest('[data-node], .wf-wire-hit')) openPalette(point(event.clientX, event.clientY)); });
    canvas.addEventListener('contextmenu', event => { if (!event.target.closest('[data-node]')) { event.preventDefault(); openPalette(point(event.clientX, event.clientY)); } });
    byId('add').addEventListener('click', () => palette.hidden ? openPalette() : closePalette()); byId('first').addEventListener('click', () => openPalette());
    byId('first').addEventListener('pointerdown', event => event.stopPropagation());
    form.querySelector('[data-close-palette]').addEventListener('click', closePalette); byId('search').addEventListener('input', event => renderPalette(event.target.value));
    byId('settings').addEventListener('click', () => select(selection === 'settings' ? null : 'settings')); byId('inspector-close').addEventListener('click', () => { select(null); canvas.focus(); });
    byId('undo').addEventListener('click', () => restore('undo')); byId('redo').addEventListener('click', () => restore('redo'));
    byId('zoom-in').addEventListener('click', () => zoom(camera().zoom * 1.2)); byId('zoom-out').addEventListener('click', () => zoom(camera().zoom / 1.2)); byId('zoom').addEventListener('click', () => zoom(1)); byId('fit').addEventListener('click', fit);
    name.addEventListener('beforeinput', remember); name.addEventListener('input', changed);
    document.addEventListener('keydown', event => {
        if (configDialog.open) return;
        const editing = event.target.matches('input, select, textarea, [contenteditable=true]');
        if (event.key === 'Escape') { resetInteraction(); closePalette(); select(null); return; }
        if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') { event.preventDefault(); save(false); return; }
        if (editing) return;
        if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'z') { event.preventDefault(); restore(event.shiftKey ? 'redo' : 'undo'); }
        else if (event.key.toLowerCase() === 'a') { event.preventDefault(); openPalette(); }
        else if (event.key.toLowerCase() === 'f') { event.preventDefault(); fit(); }
        else if (event.key === 'Delete' || event.key === 'Backspace') { event.preventDefault(); deleteSelection(); }
        else if (event.code === 'Space' && !event.target.closest('button, a, summary')) { event.preventDefault(); space = true; }
    });
    document.addEventListener('keyup', event => { if (event.code === 'Space') space = false; }); window.addEventListener('blur', () => { space = false; resetInteraction(); drawWires(); });
    async function request(url, value) {
        const response = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRFToken': token}, body: JSON.stringify(value)}), result = await response.json().catch(() => ({}));
        if (!response.ok || result.error || response.redirected) throw new Error(result.error || 'Could not save. Check your connection and sign-in.'); return result;
    }
    const requestId = crypto.randomUUID();
    async function save(run) {
        if (saving) return;
        if (!form.reportValidity()) return;
        name.value = name.value.trim() || 'Untitled workflow'; const submitted = snapshot(); saving = true; feedback.textContent = 'Saving…';
        byId('save').disabled = true; byId('start').disabled = true;
        try {
            const result = await request(identifier ? `/workflows/${identifier}/` : '/workflows/new/', {name: name.value, version, definition: structuredClone(definition)});
            identifier = result.id; version = result.version; saved = submitted; history.replaceState(null, '', result.url); feedback.textContent = snapshot() === saved ? 'Saved' : 'Unsaved'; feedback.dataset.error = 'false';
            if (run && snapshot() !== saved) throw Error('The workflow changed while saving. Save it again before running.');
            if (run) { const result = await request(`/workflows/${identifier}/run/`, {version, request_id: requestId}); location.assign(result.url); }
        } catch (error) { feedback.textContent = error.message; feedback.dataset.error = 'true'; }
        finally { saving = false; byId('save').disabled = false; byId('start').disabled = !definition.steps.length; }
    }
    form.addEventListener('submit', event => { event.preventDefault(); save(false); }); byId('start').addEventListener('click', () => save(true));
    window.addEventListener('beforeunload', event => { if (snapshot() !== saved || saving) { event.preventDefault(); event.returnValue = ''; } });
    new ResizeObserver(() => { updateCamera(); drawWires(); }).observe(canvas);
    renderNodes(); if (definition.steps.length && !hadLayout) { fit(); saved = snapshot(); }
})();
