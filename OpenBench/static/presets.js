let presetState = JSON.parse(document.getElementById('json-presets').textContent);
let presetKind = 'TEST';
let selectedPreset = null;
let presetBusy = false;
let presetDraft = [];
let replacePreset = false;
const editedFields = new Set();
const presetColumns = 4;
const presetGroups = [
    ['test_mode', 'test_bounds', 'test_confidence', 'test_max_games'],
    ['scale_method', 'scale_nps'],
    ['dev_branch', 'dev_bench'],
    ['base_branch', 'base_bench'],
    ['base_engine', 'base_repo', 'base_network'],
    ['dev_engine', 'dev_repo', 'dev_network'],
];

function engine_presets(engine = get_dev_engine()) {
    const rows = presetState.presets.filter(p => p.engine === engine);
    return [...rows.filter(p => p.scope === 'engine'), ...rows.filter(p => p.scope === 'personal')];
}

function get_presets(engine, name) {
    const shared = engine_presets(engine).filter(p => p.scope === 'engine');
    return (shared.find(p => p.name === name) || (name === 'default' ? shared[0] : null))?.settings || {};
}

function expanded_settings(settings) {
    const result = {};
    for (const [key, value] of Object.entries(settings)) {
        if (key.startsWith('both_')) {
            result[key.replace('both_', 'dev_')] = value;
            if (presetKind !== 'TUNE') result[key.replace('both_', 'base_')] = value;
        }
    }
    for (const [key, value] of Object.entries(settings)) if (!key.startsWith('both_')) result[key] = value;
    if (!result.test_mode) {
        if (Number(result.test_max_games) > 0) result.test_mode = 'GAMES';
        else if (result.test_bounds && result.test_bounds !== 'N/A') result.test_mode = 'SPRT';
    }
    return result;
}

function preset_defaults() {
    const values = {};
    for (const name of presetState.fields) {
        const field = document.getElementById(name);
        if (!field) continue;
        values[name] = field.tagName === 'SELECT' ? (Array.from(field.options).find(option => option.defaultSelected) || field.options[0])?.value || '' : field.defaultValue;
    }
    for (const target of presetKind === 'TUNE' ? ['dev'] : ['dev', 'base']) {
        const engine = document.getElementById(target + '_engine').value;
        values[target + '_repo'] = repos[engine] || config.engines[engine]?.source || '';
        values[target + '_network'] = networks.find(network => network.engine === engine && network.default)?.sha256 || '';
    }
    if (presetKind !== 'TUNE') values.base_engine = get_base_engine();
    values.scale_method = presetKind === 'TUNE' ? 'DEV' : 'BASE';
    values.scale_nps = config.engines[presetKind === 'TUNE' ? get_dev_engine() : get_base_engine()]?.nps || '';
    return values;
}

function apply_settings(settings) {
    const explicit = expanded_settings(settings);
    let retained = 0;
    if (presetKind !== 'TUNE' && explicit.base_engine && explicit.base_engine !== get_base_engine() && !editedFields.has('base_engine') && config.engines[explicit.base_engine]) set_engine(explicit.base_engine, 'base');
    const values = {...preset_defaults(), ...explicit};
    for (const [key, value] of Object.entries(values)) {
        const field = document.getElementById(key);
        if (!field || key === 'dev_engine') continue;
        const crossEngine = presetKind !== 'TUNE' && get_base_engine() !== get_dev_engine();
        const wrongBase = presetKind !== 'TUNE' && explicit.base_engine && explicit.base_engine !== get_base_engine();
        const protectedBase = (wrongBase || (crossEngine && !explicit.base_engine)) && ['base_options', 'base_branch', 'base_bench', 'base_network', 'base_repo'].includes(key);
        if (editedFields.has(key) || protectedBase) { if (editedFields.has(key) && key in explicit && field.value !== String(value)) retained++; continue; }
        if (field.tagName === 'SELECT') {
            const option = Array.from(field.options).find(option => option.value === String(value) || option.text === String(value));
            if (option) field.value = option.value;
        } else field.value = value;
    }
    const mode = document.getElementById('test_mode');
    if (mode && values.test_mode && !editedFields.has('test_mode')) {
        if (mode.value === 'GAMES') {
            document.getElementById('test_bounds').value = 'N/A';
            document.getElementById('test_confidence').value = 'N/A';
        } else document.getElementById('test_max_games').value = 'N/A';
    }
    return retained;
}

function select_preset(preset) {
    selectedPreset = preset;
    const retained = apply_settings(preset.settings);
    render_presets();
    const message = document.getElementById('preset-message');
    message.replaceChildren();
    if (retained) {
        const text = document.createElement('span');
        text.textContent = 'Edited fields retained.';
        const overwrite = preset_button('Overwrite?', () => {
            editedFields.clear();
            apply_settings(preset.settings);
            render_presets();
            message.replaceChildren();
        }, 'Overwrite edited fields with this preset');
        const dismiss = preset_button('\u00d7', () => message.replaceChildren(), 'Dismiss preset notice');
        dismiss.className = 'preset-notice-dismiss';
        message.append(text, overwrite, dismiss);
    }
}

function preset_button(text, action, label) {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = text;
    button.onclick = action;
    if (label) { button.title = label; button.setAttribute('aria-label', label); }
    return button;
}

function render_presets() {
    const root = document.getElementById('preset-slots');
    root.replaceChildren();
    const rows = engine_presets();
    for (const scope of ['engine', 'personal']) {
        const presets = rows.filter(preset => preset.scope === scope);
        const editable = Boolean(config.engines[get_dev_engine()]) && (scope === 'personal' || presetState.editable[get_dev_engine()]);
        if (!presets.length && !editable) continue;
        const group = document.createElement('div');
        group.className = 'preset-group';
        group.setAttribute('role', 'group');
        group.setAttribute('aria-labelledby', 'preset-label-' + scope);
        const label = document.createElement('div');
        label.id = 'preset-label-' + scope;
        label.className = 'preset-group-label';
        label.textContent = scope === 'engine' ? get_dev_engine() + ' presets' : 'My presets';
        const buttons = document.createElement('div');
        buttons.className = 'preset-group-buttons';
        buttons.style.setProperty('--preset-columns', presetColumns);
        for (const preset of presets) {
            const button = preset_button(preset.name, () => {
                if (replacePreset && preset.editable) edit_preset_slot(preset.scope, preset);
                else select_preset(preset);
            });
            button.className = 'anchorbutton btn-start';
            button.setAttribute('aria-pressed', String(selectedPreset?.id === preset.id));
            button.classList.toggle('preset-target', replacePreset && preset.editable);
            button.title = (preset.scope === 'engine' ? get_dev_engine() : 'Personal') + (preset.id === rows[0].id ? ' - Default preset' : '');
            buttons.append(button);
        }
        if (editable) {
            const placeholder = preset_button('+', () => edit_preset_slot(scope, null, presets.length), 'Save a new ' + (scope === 'engine' ? 'shared' : 'personal') + ' preset');
            placeholder.className = 'anchorbutton preset-placeholder';
            buttons.append(placeholder);
        }
        group.append(label, buttons);
        root.append(group);
    }
    document.getElementById('preset-manage-open').hidden = !rows.some(p => p.editable);
    const replace = document.getElementById('preset-replace');
    replace.hidden = !rows.some(p => p.editable);
    replace.textContent = replacePreset ? 'Cancel replace' : 'Replace preset';
    replace.setAttribute('aria-pressed', String(replacePreset));
}

function change_engine(engine, target, kind) {
    document.getElementById('preset-message').replaceChildren();
    presetKind = kind;
    replacePreset = false;
    document.getElementById('preset-manager').hidden = true;
    if (!config.engines[engine]) engine = document.getElementById(target + '_engine')?.value;
    if (!config.engines[engine]) { render_presets(); return; }
    set_engine(engine, target);
    if (target === 'dev' && kind !== 'TUNE' && !editedFields.has('base_engine')) set_engine(engine, 'base');
    if (!editedFields.has('scale_nps')) set_option('scale_nps', config.engines[engine].nps);
    if (!editedFields.has('scale_method')) set_option('scale_method', kind === 'TUNE' ? 'DEV' : 'BASE');
    if (target === 'base') {
        const defaults = expanded_settings(get_presets(engine, 'default'));
        for (const key of ['options', 'branch', 'network']) {
            const field = 'base_' + key;
            if (!editedFields.has(field) && defaults[field] !== undefined) set_option(field, defaults[field]);
        }
        render_presets();
        return;
    }
    selectedPreset = null;
    const first = engine_presets(engine)[0];
    if (first) select_preset(first);
    else { apply_settings({}); render_presets(); }
}

function capture_preset() {
    const settings = {};
    for (const name of presetState.fields) {
        const field = document.getElementById(name);
        if (field && !field.disabled) settings[name] = field.value;
    }
    return settings;
}

function edit_preset_slot(scope, preset = null, index = null) {
    if (presetBusy) return;
    render_presets();
    const group = document.getElementById('preset-label-' + scope).parentElement;
    const rows = engine_presets().filter(row => row.scope === scope);
    const target = group.querySelector('.preset-group-buttons').children[preset ? rows.findIndex(row => row.id === preset.id) : index];
    const editor = document.createElement('div');
    editor.className = 'preset-slot-editor';
    editor.style.height = target.getBoundingClientRect().height + 'px';
    const name = document.createElement('input');
    name.value = preset?.name || '';
    name.maxLength = 128;
    name.placeholder = 'Preset name';
    name.setAttribute('aria-label', 'Preset name');
    const save = () => {
        if (!name.value.trim()) { name.focus(); return; }
        save_presets({action: 'save', scope, id: preset?.id, name: name.value.trim(), settings: capture_preset()});
    };
    name.onkeydown = event => {
        if (event.key === 'Enter') { event.preventDefault(); save(); }
        if (event.key === 'Escape') { event.preventDefault(); render_presets(); }
    };
    editor.append(name,
        preset_button('\u2713', save, preset ? 'Replace preset (Enter)' : 'Save preset (Enter)'),
        preset_button('\u00d7', render_presets, 'Cancel (Escape)'));
    target.replaceWith(editor);
    name.focus();
}

function render_preset_editor() {
    const root = document.getElementById('preset-editor');
    root.replaceChildren();
    for (const [index, preset] of presetDraft.entries()) {
        const row = document.createElement('div');
        row.className = 'preset-edit-row';
        const input = document.createElement('input');
        input.value = preset.name;
        input.maxLength = 128;
        input.setAttribute('aria-label', 'Preset ' + (index + 1) + ' name');
        input.oninput = () => { preset.name = input.value; document.getElementById('preset-manage-scope').disabled = true; };
        input.onkeydown = event => { if (event.key === 'Enter') event.preventDefault(); };
        row.append(input);
        for (const [delta, text] of [[-1, 'Up'], [1, 'Down']]) {
            const button = preset_button(text, () => {
                const target = index + delta;
                [presetDraft[index], presetDraft[target]] = [presetDraft[target], presetDraft[index]];
                document.getElementById('preset-manage-scope').disabled = true;
                render_preset_editor();
            }, text + ': ' + preset.name);
            button.disabled = index + delta < 0 || index + delta >= presetDraft.length;
            row.append(button);
        }
        row.append(preset_button('Delete', () => {
            presetDraft.splice(index, 1);
            document.getElementById('preset-manage-scope').disabled = true;
            render_preset_editor();
        }, 'Delete: ' + preset.name));
        root.append(row);
    }
}

async function save_presets(payload) {
    if (presetBusy) return;
    const engine = get_dev_engine();
    const message = document.getElementById('preset-message');
    message.replaceChildren();
    const controls = document.querySelectorAll('#preset-slots button, #preset-slots input, #preset-manager button, #preset-manager input, #preset-manager select, .preset-bar > button, #dev_engine, #base_engine, #workload-form [type=submit]');
    const disabled = Array.from(controls, control => control.disabled);
    controls.forEach(control => { control.disabled = true; });
    presetBusy = true;
    try {
        const response = await fetch('/presets/' + presetKind + '/', {method: 'POST',
            headers: {'Content-Type': 'application/json', 'X-CSRFToken': document.querySelector('[name=csrfmiddlewaretoken]').value},
            body: JSON.stringify({...payload, version: presetState.versions[engine][payload.scope], engine})});
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || 'Unable to save presets.');
        presetState = data;
        selectedPreset = engine_presets().find(p => p.id === selectedPreset?.id) || null;
        if (payload.action === 'save') selectedPreset = engine_presets().find(p => p.scope === payload.scope && p.name === payload.name);
        replacePreset = false;
        document.getElementById('preset-manager').hidden = true;
        render_presets();
        message.textContent = 'Presets saved.';
    } catch (error) {
        if (payload.action === 'manage') document.getElementById('preset-manager').hidden = false;
        message.textContent = error instanceof SyntaxError ? 'Unable to save presets.' : error.message;
    } finally {
        presetBusy = false;
        controls.forEach((control, index) => { control.disabled = disabled[index]; });
    }
}

function initialize_presets(kind) {
    presetKind = kind;
    const form = document.getElementById('workload-form');
    for (const event of ['input', 'change']) form.addEventListener(event, e => {
        if (!e.target.name) return;
        document.getElementById('preset-message').replaceChildren();
        editedFields.add(e.target.name);
        for (const group of presetGroups) if (group.includes(e.target.name)) for (const field of group) editedFields.add(field);
    }, true);
    document.getElementById('preset-replace').onclick = () => {
        replacePreset = !replacePreset;
        document.getElementById('preset-manager').hidden = true;
        render_presets();
    };
    const manager = document.getElementById('preset-manager');
    const scope = document.getElementById('preset-manage-scope');
    scope.onchange = () => {
        presetDraft = engine_presets().filter(p => p.scope === scope.value && p.editable).map(p => ({id: p.id, name: p.name}));
        render_preset_editor();
    };
    document.getElementById('preset-manage-open').onclick = () => {
        replacePreset = false;
        render_presets();
        scope.replaceChildren();
        if (presetState.editable[get_dev_engine()]) scope.add(new Option(get_dev_engine() + ' presets', 'engine'));
        scope.add(new Option('My presets', 'personal'));
        scope.disabled = false;
        scope.onchange();
        manager.hidden = false;
        scope.focus();
    };
    document.getElementById('preset-manage-save').onclick = () => {
        if (presetBusy) return;
        manager.hidden = true;
        save_presets({action: 'manage', scope: scope.value, presets: presetDraft});
    };
    document.getElementById('preset-manage-cancel').onclick = () => { manager.hidden = true; };
}
