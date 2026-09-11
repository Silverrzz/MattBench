(async () => {
    const data = JSON.parse(document.getElementById('training-schedule-data').textContent);
    const form = document.getElementById('schedule-form');
    const source = document.getElementById('schedule-source');
    const fileSelect = document.getElementById('schedule-file');
    const pathInput = document.getElementById('schedule-path');
    const feedback = document.getElementById('schedule-feedback');
    const settings = document.getElementById('schedule-settings');
    const shell = document.getElementById('schedule-editor-shell');
    const fileStates = new Map();
    let active = Object.hasOwn(data.files, data.settings.example_source) ? data.settings.example_source : Object.keys(data.files).find(name => name.endsWith('.rs')) || Object.keys(data.files)[0];
    let editor;
    let dirty = false;
    let saving = false;
    let revision = 0;
    const element = id => document.getElementById(id);
    const setFeedback = (text, error = false) => {
        feedback.textContent = text;
        feedback.toggleAttribute('data-error', error);
    };
    function changed() {
        if (!data.editable) return;
        dirty = true;
        revision++;
        setFeedback('Unsaved changes');
    }
    function storeActive() {
        data.files[active] = editor ? editor.getText() : source.value;
        if (editor) fileStates.set(active, editor.getState());
    }
    function selectFile(name) {
        active = name;
        source.value = data.files[name];
        if (editor) editor.setFile(name, data.files[name], fileStates.get(name));
        pathInput.value = name;
        fileSelect.value = name;
        toggleRename(false);
    }
    function toggleRename(open) {
        fileSelect.hidden = open;
        pathInput.hidden = !open;
        element('cancel-schedule-rename').hidden = !open;
        element('rename-schedule-file').textContent = open ? 'Save name' : 'Rename';
        if (open) {
            pathInput.value = active;
            pathInput.focus();
            pathInput.select();
        }
    }
    function refreshFiles() {
        fileSelect.replaceChildren(...Object.keys(data.files).map(name => new Option(name, name)));
        selectFile(active);
    }
    element('schedule-engine').value = data.engine;
    element('schedule-scope').value = data.scope;
    function updateScope() {
        const global = element('schedule-scope').value !== 'engine';
        element('schedule-engine-field').hidden = global;
        element('schedule-engine').required = !global;
        element('schedule-engine').disabled = global || !data.editable;
    }
    element('schedule-scope').addEventListener('change', updateScope);
    updateScope();
    element('bullet-repo').value = data.settings.bullet_repo;
    element('bullet-ref').value = data.settings.bullet_ref;
    const advanced = {...data.settings};
    delete advanced.bullet_repo;
    delete advanced.bullet_ref;
    for (const field of ['checkpoint_keep_last', 'delete_uploaded_checkpoints']) delete advanced[field];
    settings.value = JSON.stringify(advanced, null, 2);
    refreshFiles();
    if (!data.editable) {
        form.querySelectorAll('input, textarea, select, button').forEach(control => {
            if (!['schedule-file', 'expand-editor'].includes(control.id)) control.disabled = true;
        });
        setFeedback('Read only');
    }
    form.addEventListener('input', event => {
        if (!['schedule-file', 'schedule-path'].includes(event.target.id)) changed();
    });
    fileSelect.addEventListener('change', () => { storeActive(); selectFile(fileSelect.value); });
    element('schedule-search').addEventListener('input', event => {
        const query = event.target.value.toLowerCase();
        document.querySelectorAll('.schedule-library-row').forEach(row => { row.hidden = !row.textContent.toLowerCase().includes(query); });
    });
    element('add-schedule-file').addEventListener('click', () => {
        storeActive();
        let index = 1;
        while (`examples/schedule-${index}.rs` in data.files) index++;
        active = `examples/schedule-${index}.rs`;
        data.files[active] = '';
        refreshFiles();
        toggleRename(true);
        changed();
    });
    function renameFile() {
        const name = pathInput.value.trim();
        if (!name || name.startsWith('/') || name.includes('..') || name.includes('\\') || name.split('/').includes('.git')) return setFeedback('Use a relative path inside the Bullet fork.', true);
        if (name !== active && name in data.files) return setFeedback('A file with that path already exists.', true);
        if (name === active) {
            toggleRename(false);
            fileSelect.focus();
            return true;
        }
        storeActive();
        try {
            const config = JSON.parse(settings.value);
            if (config.example_source === active) {
                config.example_source = name;
                settings.value = JSON.stringify(config, null, 2);
            }
        } catch {}
        const value = data.files[active];
        const state = fileStates.get(active);
        fileStates.delete(active);
        delete data.files[active];
        data.files[name] = value;
        if (state) fileStates.set(name, state);
        active = name;
        refreshFiles();
        fileSelect.focus();
        changed();
        return true;
    }
    element('rename-schedule-file').addEventListener('click', () => {
        if (pathInput.hidden) toggleRename(true);
        else renameFile();
    });
    element('cancel-schedule-rename').addEventListener('click', () => {
        toggleRename(false);
        fileSelect.focus();
    });
    pathInput.addEventListener('keydown', event => {
        if (event.key === 'Enter') {
            event.preventDefault();
            renameFile();
        } else if (event.key === 'Escape') {
            event.preventDefault();
            event.stopPropagation();
            toggleRename(false);
            fileSelect.focus();
        }
    });
    element('remove-schedule-file').addEventListener('click', () => {
        if (Object.keys(data.files).length === 1) return setFeedback('A schedule needs at least one file.', true);
        if (!window.confirm(`Remove ${active} from this schedule?`)) return;
        delete data.files[active];
        fileStates.delete(active);
        active = Object.keys(data.files)[0];
        refreshFiles();
        changed();
    });
    element('schedule-upload').addEventListener('change', async event => {
        storeActive();
        for (const file of event.target.files) {
            if (file.size > 1024 * 1024) { setFeedback('Each schedule bundle must total less than 1 MB.', true); continue; }
            const name = file.name.endsWith('.rs') && active.endsWith('.rs') && !data.files[active].trim() ? active : file.name.endsWith('.rs') ? `examples/${file.name}` : file.name;
            if (data.files[name] && !window.confirm(`Replace ${name} with the uploaded file?`)) continue;
            data.files[name] = await file.text();
            fileStates.delete(name);
            active = name;
        }
        event.target.value = '';
        refreshFiles();
        changed();
    });
    function expand(value) {
        shell.classList.toggle('expanded', value);
        document.body.classList.toggle('editor-expanded', value);
        element('expand-editor').setAttribute('aria-pressed', String(value));
        element('expand-editor').textContent = value ? 'Close' : 'Expand';
        if (value) {
            shell.setAttribute('role', 'dialog');
            shell.setAttribute('aria-modal', 'true');
            shell.setAttribute('aria-label', 'Training schedule editor');
        } else {
            shell.removeAttribute('role');
            shell.removeAttribute('aria-modal');
        }
        if (editor) editor.focus();
        else source.focus();
    }
    element('expand-editor').addEventListener('click', () => expand(!shell.classList.contains('expanded')));
    document.addEventListener('keydown', event => {
        if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') { event.preventDefault(); if (data.editable) form.requestSubmit(); }
        if (event.key === 'Escape' && shell.classList.contains('expanded')) expand(false);
        if (event.key === 'Tab' && shell.classList.contains('expanded')) {
            const controls = Array.from(shell.querySelectorAll('button, input, select, textarea, [contenteditable="true"]')).filter(control => !control.disabled && control.getClientRects().length);
            const first = controls[0], last = controls[controls.length - 1];
            if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
            else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
        }
    });
    window.addEventListener('beforeunload', event => {
        if (dirty) { event.preventDefault(); event.returnValue = ''; }
    });
    form.addEventListener('submit', async event => {
        event.preventDefault();
        if (saving || !data.editable) return;
        if (!pathInput.hidden && !renameFile()) return;
        storeActive();
        let config;
        try {
            config = JSON.parse(settings.value);
            if (!config || Array.isArray(config) || typeof config !== 'object') throw new Error('Use a JSON object.');
        } catch (error) {
            setFeedback(`Build settings: ${error.message}`, true);
            element('schedule-build-settings').open = true;
            settings.focus();
            return;
        }
        const saveRevision = revision;
        const payload = {version: data.version, name: element('schedule-name').value, engine: element('schedule-scope').value !== 'engine' ? '' : element('schedule-engine').value, scope: element('schedule-scope').value, files: data.files, settings: {...config, bullet_repo: element('bullet-repo').value.trim(), bullet_ref: element('bullet-ref').value.trim()}};
        saving = true;
        element('save-schedule').disabled = true;
        setFeedback('Saving…');
        try {
            const response = await fetch(data.id ? `/training/schedules/${data.id}/` : '/training/schedules/', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRFToken': form.querySelector('[name=csrfmiddlewaretoken]').value}, body: JSON.stringify(payload)});
            const result = await response.json();
            if (!response.ok) throw new Error(result.error || 'Could not save schedule.');
            data.version = result.version;
            data.id = result.url.split('/').filter(Boolean).pop();
            history.replaceState(null, '', result.url);
            element('train-with-schedule').href = `/training/new/?schedule=${data.id}&engine=${payload.engine}`;
            element('train-with-schedule').hidden = false;
            element('delete-schedule').hidden = false;
            let row = Array.from(document.querySelectorAll('.schedule-library-row')).find(item => new URL(item.href).pathname === result.url);
            if (!row) {
                row = document.createElement('a');
                row.className = 'schedule-library-row';
                row.append(document.createElement('span'), document.createElement('strong'));
                element('schedule-library-list').append(row);
            }
            row.href = result.url;
            row.firstElementChild.textContent = `${payload.scope !== 'engine' ? 'All engines' : element('schedule-engine').selectedOptions[0].textContent} · ${element('schedule-scope').selectedOptions[0].textContent}`;
            row.lastElementChild.textContent = payload.name;
            document.querySelectorAll('.schedule-library-row[aria-current]').forEach(item => item.removeAttribute('aria-current'));
            row.setAttribute('aria-current', 'page');
            dirty = revision !== saveRevision;
            setFeedback(dirty ? 'Saved. Newer edits are still unsaved.' : `Saved · version ${result.version}`);
        } catch (error) { setFeedback(error.message, true); }
        finally { saving = false; element('save-schedule').disabled = false; }
    });
    element('delete-schedule')?.addEventListener('click', async () => {
        if (!window.confirm('Delete this schedule? Existing training runs retain their frozen copy.')) return;
        try {
            const response = await fetch(`/training/schedules/${data.id}/`, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRFToken': form.querySelector('[name=csrfmiddlewaretoken]').value}, body: JSON.stringify({action: 'delete', version: data.version})});
            const result = await response.json();
            if (!response.ok) throw new Error(result.error || 'Could not delete schedule.');
            dirty = false;
            location.assign(result.url);
        } catch (error) { setFeedback(error.message, true); }
    });
    try {
        const module = await import(element('schedule-workspace').dataset.editorUrl);
        editor = module.createEditor(source, active, source.value, changed, !data.editable);
    } catch {
        setFeedback('Code editor unavailable.');
    }
})();
