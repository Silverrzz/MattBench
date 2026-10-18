(() => {
    const dialog = document.getElementById('profile-dialog');
    if (!dialog) return;
    const data = JSON.parse(document.getElementById('profile-editor-data').textContent), form = document.getElementById('profile-form');
    const field = key => form.elements.namedItem(key), defaults = data.profiles.find(row => row.id === 'standard');
    let trigger;
    function tools() {
        const duck = field('generator').value === 'duckchess';
        document.getElementById('profile-combiner').hidden = duck;
        field('tool_repo').required = true; field('tool_commit').required = true; field('combiner_repo').required = !duck; field('combiner_commit').required = !duck;
    }
    function open(profile, button, submitted) {
        trigger = button; form.reset();
        document.getElementById('profile-dialog-title').textContent = profile ? 'Edit profile' : 'New profile';
        field('id').value = profile?.id || ''; field('id').readOnly = Boolean(profile);
        field('version').value = profile?.version || 0; field('name').value = profile?.name || '';
        field('generator').value = profile?.generator || 'standard';
        field('generator').style.pointerEvents = profile ? 'none' : '';
        document.getElementById('profile-generator-field').hidden = Boolean(profile);
        field('bullet_repo').value = profile?.bullet_repo || ''; field('bullet_ref').value = profile?.bullet_ref || '';
        field('tool_repo').value = profile?.data_tool?.repo || defaults?.data_tool?.repo || '';
        field('tool_commit').value = profile?.data_tool?.commit || defaults?.data_tool?.commit || '';
        field('combiner_repo').value = profile?.data_tool?.combiner?.repo || data.profiles.find(row => row.id === 'standard')?.data_tool?.combiner?.repo || '';
        field('combiner_commit').value = profile?.data_tool?.combiner?.commit || data.profiles.find(row => row.id === 'standard')?.data_tool?.combiner?.commit || '';
        field('enabled').checked = !profile?.disabled;
        if (submitted) for (const [key, value] of Object.entries(submitted)) if (field(key)) {
            if (key === 'enabled') field(key).checked = value === 'on'; else field(key).value = value;
        }
        if (submitted) field('enabled').checked = submitted.enabled === 'on';
        const error = document.getElementById('profile-error'); error.hidden = !submitted; error.textContent = submitted ? data.error : '';
        tools(); dialog.showModal(); field('name').focus();
    }
    document.getElementById('profile-create').addEventListener('click', event => open(null, event.currentTarget));
    document.querySelectorAll('[data-profile-edit]').forEach(button => button.addEventListener('click', () => open(data.profiles.find(row => row.id === button.dataset.profileEdit), button)));
    dialog.querySelectorAll('[data-profile-close]').forEach(button => button.addEventListener('click', () => dialog.close()));
    dialog.addEventListener('close', () => trigger?.focus());
    dialog.addEventListener('click', event => { if (event.target === dialog) { const rect = dialog.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close(); } });
    field('generator').addEventListener('change', () => { const source = data.profiles.find(row => row.generator === field('generator').value); if (source) { field('tool_repo').value = source.data_tool.repo; field('tool_commit').value = source.data_tool.commit; } tools(); });
    if (data.error) open(data.profiles.find(row => row.id === data.submitted.id), null, data.submitted);
})();
