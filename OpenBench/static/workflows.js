(() => {
    const editor = document.getElementById('workflow-editor');
    const runForm = document.getElementById('workflow-run');
    if (!editor && !runForm) return;
    const form = editor || runForm;
    const feedback = document.getElementById('workflow-feedback');
    const container = document.getElementById('workflow-steps');
    const token = form.querySelector('[name=csrfmiddlewaretoken]').value;
    const element = (tag, text, className) => {
        const node = document.createElement(tag);
        if (text !== undefined) node.textContent = text;
        if (className) node.className = className;
        return node;
    };
    async function request(url, data) {
        const response = await fetch(url, {method: data ? 'POST' : 'GET', headers: data ? {'Content-Type': 'application/json', 'X-CSRFToken': token} : {}, body: data ? JSON.stringify(data) : undefined});
        if (!response.headers.get('Content-Type')?.includes('application/json')) throw new Error('The session is unavailable. Reload and sign in again.');
        const result = await response.json();
        if (!response.ok || result.error) throw new Error(result.error || 'Request failed.');
        return result;
    }
    if (editor) {
        const data = JSON.parse(document.getElementById('workflow-data').textContent);
        let definition = structuredClone(data.definition);
        let identifier = data.id;
        let version = data.version;
        const requestId = crypto.randomUUID();
        const kinds = {datagen: 'Generate data', upload: 'Upload dataset', train: 'Train net', test: 'SPRT', decision: 'Metric decision', approval: 'Manual decision'};
        const outcomeNames = {completed: 'Completed', pass: 'Pass / approve', fail: 'Fail / decline', inconclusive: 'Inconclusive', error: 'Error after retries', cancelled: 'Cancelled'};
        const name = document.getElementById('workflow-name');
        const profile = document.getElementById('workflow-profile');
        const engine = document.getElementById('workflow-engine');
        name.value = data.name;
        profile.append(...data.profiles.map(row => new Option(row.name, row.id)));
        profile.value = definition.profile;
        engine.append(new Option('Choose engine', ''), ...data.engines.map(row => new Option(row.name, row.id)));
        engine.value = definition.engine;
        function input(root, label, value, update, options = null, type = 'text') {
            const wrapper = element('label', label, type === 'checkbox' ? 'choice' : 'field');
            const field = element(options ? 'select' : 'input');
            if (options) field.append(...options.map(row => new Option(row.name, String(row.id))));
            else field.type = type;
            if (type === 'checkbox') field.checked = Boolean(value);
            else field.value = value ?? '';
            if (type === 'number') { field.min = '0'; field.step = '1'; }
            field.addEventListener('change', () => update(type === 'checkbox' ? field.checked : type === 'number' ? Number(field.value) : field.value));
            wrapper.append(field);
            root.append(wrapper);
            return field;
        }
        function render() {
            const expanded = new Map([...container.children].map(card => [card.dataset.step, [...card.querySelectorAll('details')].map(details => details.open)]));
            container.replaceChildren();
            const activeProfile = data.profiles.find(row => row.id === definition.profile);
            const engineName = data.engines.find(row => String(row.id) === definition.engine)?.name;
            definition.steps.forEach((step, index) => {
                step.worker = {cpu: 'inherit', threads: 1, require: '', prefer: '', fallback_seconds: 300, tags: [], max_workers: 0, ...step.worker};
                const card = element('article', undefined, 'workflow-card');
                card.dataset.step = step.id;
                card.append(element('h2', `${index + 1}. ${step.name}`));
                const fields = element('div', undefined, 'field-grid');
                input(fields, 'Step name', step.name, value => { step.name = value; render(); });
                input(fields, 'Step type', step.kind, value => { step.kind = value; step.config = {}; step.inputs = {}; render(); }, Object.entries(kinds).map(([id, name]) => ({id, name})));
                if (['datagen', 'test'].includes(step.kind)) {
                    const options = data.templates.filter(row => row.kind === step.kind && (!engineName || row.engine === engineName) && (!activeProfile || row.variant === activeProfile.variant));
                    input(fields, 'Workload template', step.config.template, value => { step.config.template = Number(value); }, [{id: '', name: 'Choose an existing workload'}, ...options]);
                }
                if (step.kind === 'datagen') input(fields, 'Target games', step.config.games ?? 10000000, value => { step.config.games = value; }, null, 'number');
                if (step.kind === 'upload') {
                    input(fields, 'Hugging Face dataset repository', step.config.repo, value => { step.config.repo = value; });
                    input(fields, 'Private repository', step.config.private ?? true, value => { step.config.private = value; }, null, 'checkbox');
                }
                if (step.kind === 'train') {
                    const options = data.schedules.filter(row => row.profile === definition.profile && (!row.engine || row.engine === definition.engine));
                    input(fields, 'Training schedule', step.config.schedule, value => { step.config.schedule = value; }, [{id: '', name: 'Choose a schedule'}, ...options]);
                    input(fields, 'Priority', step.config.priority ?? 0, value => { step.config.priority = value; }, null, 'number').removeAttribute('min');
                }
                if (step.kind === 'test') {
                    const control = step.config.control || 'template';
                    input(fields, 'Test control', control, value => { step.config.control = value; render(); }, [{id: 'template', name: 'Use template'}, {id: 'nodes', name: 'Fixed nodes'}, {id: 'stc', name: 'STC'}, {id: 'ltc', name: 'LTC'}]);
                    if (control === 'nodes') {
                        step.config.nodes ??= 25000;
                        input(fields, 'Nodes per move', step.config.nodes, value => { step.config.nodes = value; }, null, 'number');
                    }
                    if (['stc', 'ltc'].includes(control)) input(fields, 'Time control', step.config.time_control || (control === 'stc' ? '10.0+0.1' : '60.0+0.6'), value => { step.config.time_control = value; });
                    input(fields, 'Game cap (0: no cap)', step.config.max_games ?? 0, value => { step.config.max_games = value; }, null, 'number');
                }
                if (step.kind === 'decision') {
                    input(fields, 'Metric', step.config.field || 'loss', value => { step.config.field = value; }, ['games', 'llr', 'loss', 'positions_per_second', 'superbatch'].map(id => ({id, name: id.replaceAll('_', ' ')})));
                    step.config.field ||= 'loss';
                    step.config.operator ||= 'lt';
                    step.config.value ??= 0;
                    input(fields, 'Comparison', step.config.operator, value => { step.config.operator = value; }, [{id: 'lt', name: 'Less than'}, {id: 'lte', name: 'At most'}, {id: 'gt', name: 'Greater than'}, {id: 'gte', name: 'At least'}, {id: 'eq', name: 'Equal'}]);
                    const metricValue = input(fields, 'Value', step.config.value, value => { step.config.value = value; }, null, 'number');
                    metricValue.step = 'any';
                    metricValue.removeAttribute('min');
                }
                const source = {upload: ['dataset', ['datagen']], train: ['dataset', ['upload']], test: ['network', ['train', 'test']], decision: ['source', Object.keys(kinds)]}[step.kind];
                if (source) input(fields, 'Use output from', step.inputs[source[0]], value => { step.inputs[source[0]] = value; }, [{id: '', name: 'Choose upstream step'}, ...definition.steps.filter(row => row.id !== step.id && source[1].includes(row.kind)).map(row => ({id: row.id, name: row.name}))]);
                card.append(fields);
                const worker = element('details');
                worker.open = expanded.get(step.id)?.[0] || false;
                worker.append(element('summary', 'Worker settings, joins and error handling'));
                const workerFields = element('div', undefined, 'field-grid');
                input(workerFields, 'CPU budget', step.worker.cpu, value => { step.worker.cpu = value; render(); }, [{id: 'inherit', name: 'Worker defaults'}, {id: 'physical', name: 'One thread per physical core'}, {id: 'logical', name: 'All available logical CPUs'}, {id: 'limit', name: 'Fixed CPU budget'}]);
                if (step.worker.cpu === 'limit') input(workerFields, 'CPU threads', step.worker.threads, value => { step.worker.threads = value; }, null, 'number');
                input(workerFields, 'Require worker', step.worker.require, value => { step.worker.require = value; }, [{id: '', name: 'Any eligible owned worker'}, ...data.workers]);
                input(workerFields, 'Prefer worker', step.worker.prefer, value => { step.worker.prefer = value; }, [{id: '', name: 'No preference'}, ...data.workers]);
                input(workerFields, 'Preferred-worker fallback after seconds', step.worker.fallback_seconds, value => { step.worker.fallback_seconds = value; }, null, 'number');
                input(workerFields, 'Required worker tags (comma-separated)', step.worker.tags.join(', '), value => { step.worker.tags = value.split(',').map(tag => tag.trim()).filter(Boolean); });
                if (step.kind === 'datagen' || step.kind === 'test') input(workerFields, 'Maximum workers (0: unlimited)', step.worker.max_workers, value => { step.worker.max_workers = value; }, null, 'number');
                input(workerFields, 'Start when incoming routes match', step.join || 'all', value => { step.join = value; }, [{id: 'all', name: 'All incoming routes'}, {id: 'any', name: 'Any incoming route'}]);
                input(workerFields, 'Automatic error retries', step.retries ?? 0, value => { step.retries = value; }, null, 'number');
                input(workerFields, 'Deadline in hours', step.timeout_hours ?? 168, value => { step.timeout_hours = value; }, null, 'number');
                worker.append(workerFields);
                card.append(worker);
                const routes = element('details');
                routes.open = expanded.get(step.id)?.[1] || false;
                routes.append(element('summary', 'Branches after this step'));
                const routeFields = element('div', undefined, 'field-grid');
                const outcomes = ['test', 'decision', 'approval'].includes(step.kind) ? ['pass', 'fail', 'inconclusive', 'error', 'cancelled'] : ['completed', 'inconclusive', 'error', 'cancelled'];
                for (const outcome of outcomes) {
                    const label = element('label', outcomeNames[outcome], 'field');
                    const select = element('select');
                    select.multiple = true;
                    select.setAttribute('aria-label', `${step.name}: ${outcomeNames[outcome]} destinations`);
                    for (const target of definition.steps.filter(row => row.id !== step.id)) {
                        const selected = definition.edges.some(edge => edge.from === step.id && edge.to === target.id && edge.on.includes(outcome));
                        select.append(new Option(target.name, target.id, selected, selected));
                    }
                    select.addEventListener('change', () => {
                        definition.edges = definition.edges.map(edge => edge.from === step.id ? {...edge, on: edge.on.filter(item => item !== outcome)} : edge).filter(edge => edge.on.length);
                        for (const option of select.selectedOptions) {
                            const edge = definition.edges.find(edge => edge.from === step.id && edge.to === option.value);
                            if (edge) edge.on.push(outcome);
                            else definition.edges.push({from: step.id, to: option.value, on: [outcome]});
                        }
                    });
                    label.append(select);
                    routeFields.append(label);
                }
                routes.append(element('p', 'Select one or more destinations. Empty routes end that branch. Connections cannot form a cycle.'), routeFields);
                card.append(routes);
                const remove = element('button', 'Remove step', 'button');
                remove.type = 'button';
                remove.addEventListener('click', () => {
                    definition.steps = definition.steps.filter(row => row.id !== step.id);
                    definition.edges = definition.edges.filter(edge => edge.from !== step.id && edge.to !== step.id);
                    render();
                });
                card.append(remove);
                container.append(card);
            });
        }
        engine.addEventListener('change', () => { definition.engine = engine.value; render(); });
        profile.addEventListener('change', () => { definition.profile = profile.value; render(); });
        document.getElementById('workflow-add').addEventListener('click', () => {
            const kind = document.getElementById('workflow-new-kind').value;
            definition.steps.push({id: 'step-' + crypto.randomUUID().slice(0, 8), name: kinds[kind], kind, config: {}, inputs: {}, worker: {}, join: 'all', retries: 0, timeout_hours: 168});
            render();
        });
        async function save(launch) {
            if (!editor.reportValidity()) return;
            const buttons = [...editor.querySelectorAll('button')];
            buttons.forEach(button => { button.disabled = true; });
            feedback.textContent = 'Saving…';
            try {
                const result = await request(identifier ? `/workflows/${identifier}/` : '/workflows/new/', {name: name.value, version, definition});
                identifier = result.id;
                version = result.version;
                history.replaceState(null, '', result.url);
                feedback.textContent = `Saved version ${version}.`;
                if (launch) {
                    const run = await request(`/workflows/${identifier}/run/`, {version, request_id: requestId});
                    location.assign(run.url);
                }
            } catch (error) { feedback.textContent = error.message; }
            finally { buttons.forEach(button => { button.disabled = false; }); }
        }
        editor.addEventListener('submit', event => { event.preventDefault(); save(false); });
        document.getElementById('workflow-start').addEventListener('click', () => save(true));
        render();
    } else {
        let data = JSON.parse(document.getElementById('workflow-run-data').textContent);
        const endpoint = `/workflows/runs/${data.id}/`;
        async function action(value, step) {
            try { data = await request(endpoint, {action: value, step}); render(); feedback.textContent = ''; }
            catch (error) { feedback.textContent = error.message; }
        }
        function render() {
            document.getElementById('workflow-state').textContent = data.state;
            for (const button of runForm.querySelectorAll('[data-run-action]')) button.disabled = button.dataset.runAction === 'pause' ? data.state !== 'RUNNING' : button.dataset.runAction === 'resume' ? data.state !== 'PAUSED' : !['RUNNING', 'PAUSED'].includes(data.state);
            container.replaceChildren();
            for (const step of data.steps) {
                const card = element('article', undefined, 'workflow-card');
                card.append(element('h2', step.name), element('p', `${step.state}${step.outcome ? ' · ' + step.outcome : ''} · Attempts: ${step.attempts}`, 'workflow-state'), element('p', step.message));
                if (step.url) { const link = element('a', 'Open job', 'button'); link.href = step.url; card.append(link); }
                if (step.state === 'WAITING') for (const [label, value] of [['Approve', 'approve'], ['Decline', 'decline']]) {
                    const button = element('button', label, 'button'); button.type = 'button'; button.addEventListener('click', () => action(value, step.id)); card.append(button);
                }
                const details = element('details'); details.append(element('summary', 'Outputs, worker settings and attempts'), element('pre', JSON.stringify({outputs: step.outputs, worker: step.worker, previous_attempts: step.history}, null, 2))); card.append(details);
                container.append(card);
            }
        }
        runForm.querySelectorAll('[data-run-action]').forEach(button => button.addEventListener('click', () => action(button.dataset.runAction)));
        async function refresh() {
            try { data = await request(endpoint + '?format=json'); render(); }
            catch (error) { feedback.textContent = error.message; }
            if (['RUNNING', 'PAUSED', 'CANCELLING'].includes(data.state)) setTimeout(refresh, document.hidden ? 10000 : 3000);
        }
        render();
        setTimeout(refresh, 3000);
    }
})();
