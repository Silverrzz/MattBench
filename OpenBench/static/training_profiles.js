(() => {
    const dialog = document.getElementById('profile-dialog');
    if (!dialog) return;
    const data = JSON.parse(document.getElementById('profile-editor-data').textContent), form = document.getElementById('profile-form');
    const field = key => form.elements.namedItem(key);
    const selectedAdapter = () => data.templates[field('generator').value] || Object.values(data.templates)[0];
    let trigger;
    function tools() {
        const provider = data.tools[field('tool_kind').value] || data.tools[selectedAdapter().data_tool.kind];
        document.getElementById('profile-combiner').hidden = !provider.combiner;
        field('tool_repo').required = true; field('tool_commit').required = true;
        field('combiner_repo').required = provider.combiner; field('combiner_commit').required = provider.combiner;
        for (const option of field('tool_kind').options) option.disabled = !data.tools[option.value].formats.includes(selectedAdapter().format);
    }
    function capabilities(profile) {
        const adapter = selectedAdapter();
        for (const key of ['features', 'optimizers']) for (const input of form.querySelectorAll('[name="' + key + '"]')) {
            input.disabled = !adapter[key].includes(input.value);
            input.closest('label').hidden = input.disabled;
            input.checked = !input.disabled && (profile[key] || adapter[key]).includes(input.value);
        }
    }
    function populate(profile) {
        field('variant').value = profile.variant;
        field('bullet_repo').value = profile.bullet_repo; field('bullet_ref').value = profile.bullet_ref;
        field('tool_kind').value = profile.data_tool.kind;
        field('tool_repo').value = profile.data_tool.repo; field('tool_commit').value = profile.data_tool.commit;
        const combiner = profile.data_tool.combiner || Object.values(data.tools).find(tool => tool.combiner)?.combiner_defaults || {repo: '', commit: ''};
        field('combiner_repo').value = combiner.repo; field('combiner_commit').value = combiner.commit;
        field('rust_minimum').value = profile.rust_minimum || '';
        for (const key of ['defaults', 'training_settings']) { field(key).value = JSON.stringify(profile[key] || {}, null, 2); field(key).setCustomValidity(''); }
        capabilities(profile); tools();
    }
    function open(profile, button, submitted) {
        trigger = button; form.reset();
        document.getElementById('profile-dialog-title').textContent = profile ? 'Edit profile' : 'New profile';
        field('id').value = profile?.id || ''; field('id').readOnly = Boolean(profile);
        field('version').value = profile?.version || 0; field('name').value = profile?.name || '';
        field('generator').value = profile?.generator || Object.keys(data.templates)[0];
        populate(profile || selectedAdapter()); field('enabled').checked = !profile?.disabled;
        if (submitted) {
            for (const [key, value] of Object.entries(submitted)) {
                if (['features', 'optimizers'].includes(key) || !field(key)) continue;
                if (key === 'enabled') field(key).checked = value === 'on'; else field(key).value = value;
            }
            capabilities(submitted); tools(); field('enabled').checked = submitted.enabled === 'on';
        }
        const error = document.getElementById('profile-error'); error.hidden = !submitted; error.textContent = submitted ? data.error : '';
        dialog.showModal(); field('name').focus();
    }
    document.getElementById('profile-create').addEventListener('click', event => open(null, event.currentTarget));
    document.querySelectorAll('[data-profile-edit]').forEach(button => button.addEventListener('click', () => open(data.profiles.find(row => row.id === button.dataset.profileEdit), button)));
    dialog.querySelectorAll('[data-profile-close]').forEach(button => button.addEventListener('click', () => dialog.close()));
    dialog.addEventListener('close', () => trigger?.focus());
    dialog.addEventListener('click', event => { if (event.target === dialog) { const rect = dialog.getBoundingClientRect(); if (event.clientX < rect.left || event.clientY < rect.top || event.clientX > rect.right || event.clientY > rect.bottom) dialog.close(); } });
    field('generator').addEventListener('change', () => populate(selectedAdapter()));
    field('tool_kind').addEventListener('change', () => { const provider = data.tools[field('tool_kind').value]; field('tool_repo').value = provider.repo; field('tool_commit').value = provider.commit; tools(); });
    for (const key of ['defaults', 'training_settings']) field(key).addEventListener('input', () => {
        try { const value = JSON.parse(field(key).value); if (!value || Array.isArray(value) || typeof value !== 'object') throw Error('Use a JSON object.'); field(key).setCustomValidity(''); }
        catch (error) { field(key).setCustomValidity(error.message); }
    });
    form.addEventListener('invalid', event => { for (let details = event.target.closest('details'); details; details = details.parentElement.closest('details')) details.open = true; }, true);
    if (data.error) open(data.profiles.find(row => row.id === data.submitted.id), null, data.submitted);
})();
