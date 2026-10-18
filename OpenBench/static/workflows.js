(() => {
    const runForm = document.getElementById('workflow-run');
    if (!runForm) return;
    const form = runForm;
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
})();
