(() => {
    if (window.parent === window) return;
    let resolve;
    const initial = new Promise(done => { resolve = done; });
    window.addEventListener('message', event => {
        if (event.origin !== location.origin || event.source !== window.parent || event.data?.type !== 'mattbench-node-init') return;
        resolve(event.data.payload);
    });
    const send = value => window.parent.postMessage({type: 'mattbench-node-config', value}, location.origin);
    const apply = (form, values) => {
        for (const input of form.elements) {
            if (!(input.name in values)) continue;
            if (input.type === 'checkbox') input.checked = values[input.name] === true || values[input.name] === 'on';
            else if (input.type !== 'hidden' || input.name !== 'csrfmiddlewaretoken') input.value = values[input.name] ?? '';
        }
    };
    window.WorkflowNodeEditor = {receive: () => initial, send, apply};
    document.addEventListener('submit', event => {
        if (!['workload-form', 'training-new-form'].includes(event.target.id)) return;
        event.preventDefault();
        const values = Object.fromEntries(new FormData(event.target));
        delete values.csrfmiddlewaretoken; delete values.submit;
        for (const input of event.target.querySelectorAll('input[type=checkbox][name]')) values[input.name] = event.target.id === 'workload-form' ? (input.checked ? 'on' : 'off') : input.checked;
        for (const key of Object.keys(values)) if (key.startsWith('environment-row-')) delete values[key];
        send({values, preset: typeof selectedPreset === 'undefined' ? '' : selectedPreset?.id || '', edited: typeof editedFields === 'undefined' ? [] : [...editedFields]});
    });
    window.parent.postMessage({type: 'mattbench-node-ready'}, location.origin);
})();
