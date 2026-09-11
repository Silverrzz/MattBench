(() => {
    const form = document.getElementById('schedule-builder');
    if (!form) return;
    form.noValidate = true;
    const data = JSON.parse(document.getElementById('schedule-builder-data').textContent);
    const field = name => form.elements.namedItem(name);
    const byId = id => document.getElementById('builder-' + id);
    const tabs = [...form.querySelectorAll('[data-tab]')];
    const board = byId('board');
    const paint = byId('paint-bucket');
    const feedback = byId('feedback');
    const previewStatus = byId('preview-status');
    const source = byId('source');
    let layout = [...data.spec.king_layout];
    let undo = [];
    let editor;
    let timer;
    let controller;
    let generation = 0;
    let dirty = false;
    let saving = false;
    let sourceCurrent = true;
    let endpoint = data.post_url;
    let version = data.version;

    const message = (node, text, error = false) => {
        node.textContent = text;
        node.toggleAttribute('data-error', error);
    };
    const showTab = name => {
        tabs.forEach(tab => {
            const active = tab.dataset.tab === name;
            tab.setAttribute('aria-selected', String(active));
            tab.tabIndex = active ? 0 : -1;
            document.getElementById('panel-' + tab.dataset.tab).hidden = !active;
        });
    };
    tabs.forEach((tab, index) => {
        tab.addEventListener('click', () => showTab(tab.dataset.tab));
        tab.addEventListener('keydown', event => {
            let next;
            if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
            if (event.key === 'ArrowLeft') next = (index + tabs.length - 1) % tabs.length;
            if (event.key === 'Home') next = 0;
            if (event.key === 'End') next = tabs.length - 1;
            if (next === undefined) return;
            event.preventDefault();
            showTab(tabs[next].dataset.tab);
            tabs[next].focus();
        });
    });
    const scopeChanged = () => {
        const global = byId('scope').value !== 'engine';
        byId('engine-field').hidden = global;
        byId('engine').required = !global;
    };
    byId('scope').value = data.scope;
    byId('engine').value = data.engine;
    byId('scope').addEventListener('change', scopeChanged);
    scopeChanged();
    for (const [name, value] of Object.entries(data.spec)) {
        const input = field(name);
        if (!input) continue;
        if (input.type === 'checkbox') input.checked = value;
        else input.value = value;
    }
    const initialPairwiseLayers = data.spec.pairwise_layers.join(',');
    if (![...byId('pairwise-layers').options].some(option => option.value === initialPairwiseLayers)) {
        byId('pairwise-layers').add(new Option(data.spec.pairwise_layers.map(layer => 'L' + layer).join(' and '), initialPairwiseLayers));
    }
    byId('pairwise-layers').value = initialPairwiseLayers;
    const selectedPairwiseLayers = () => field('pairwise_activation').checked ? byId('pairwise-layers').value.split(',').map(Number) : [];
    data.spec.piece_count_keep.forEach((value, index) => {
        const label = document.createElement('label');
        label.className = 'field';
        label.append(document.createTextNode((index + 2) + ' pieces'));
        const input = document.createElement('input');
        Object.assign(input, {type: 'number', min: '0', max: '1', step: 'any', required: true, value: String(value)});
        input.dataset.pieceCount = index + 2;
        input.setAttribute('aria-label', 'Keep probability for ' + (index + 2) + ' pieces');
        label.append(input);
        byId('piece-count-keep').append(label);
    });
    const syncFilters = () => {
        byId('position-filters').hidden = !field('position_filtering').checked;
        byId('position-filters').querySelectorAll('input').forEach(input => { input.disabled = !field('position_filtering').checked; });
        byId('piece-count-keep').hidden = !field('piece_count_sampling').checked;
        byId('piece-count-keep').querySelectorAll('input').forEach(input => { input.disabled = !field('piece_count_sampling').checked; });
    };
    field('position_filtering').addEventListener('input', syncFilters);
    field('piece_count_sampling').addEventListener('input', syncFilters);
    syncFilters();
    const syncNetwork = () => {
        for (const name of ['score', 'wdl', 'uncertainty']) {
            field(name + '_buckets').disabled = !field(name + '_outputs').checked;
        }
        const missingPrediction = !field('score_outputs').checked && !field('wdl_outputs').checked;
        field('score_outputs').setCustomValidity(missingPrediction ? 'Enable a score or WDL output. The uncertainty head needs a prediction error to learn from.' : '');
        const pieces = field('merged_king_planes').checked ? 704 : 768;
        const bucketedInputs = field('psqt_inputs').checked || field('half_move_clock').checked;
        field('input_buckets').disabled = !bucketedInputs;
        field('merged_king_planes').disabled = !field('psqt_inputs').checked;
        byId('assign-buckets').hidden = !bucketedInputs || field('input_buckets').valueAsNumber <= 1;
        if (byId('assign-buckets').hidden && byId('bucket-dialog').open) byId('bucket-dialog').close();
        const anyInputs = ['psqt_inputs', 'threat_inputs', 'pawn_pair_inputs', 'half_move_clock'].some(name => field(name).checked);
        field('psqt_inputs').setCustomValidity(anyInputs ? '' : 'Enable at least one input feature.');
        const count = name => Number.isInteger(field(name).valueAsNumber) ? field(name).valueAsNumber : '?';
        const layers = [...byId('layers').querySelectorAll('[data-layer]')].map(input => Number.isInteger(input.valueAsNumber) ? input.valueAsNumber : '?');
        const kingInputs = [];
        if (field('psqt_inputs').checked) kingInputs.push(pieces + ' PSQT');
        if (field('half_move_clock').checked) kingInputs.push('11 HMC');
        const featureGroups = [];
        if (kingInputs.length) featureGroups.push('(' + kingInputs.join(' + ') + ')x' + count('input_buckets') + (field('mirrored').checked ? 'hm' : ''));
        if (field('threat_inputs').checked) featureGroups.push('60144 TIhm');
        if (field('pawn_pair_inputs').checked) featureGroups.push('4560 PPhm');
        const inputs = featureGroups.join(' + ') || 'no inputs';
        const heads = [];
        if (field('score_outputs').checked) heads.push('SCOREx' + count('score_buckets'));
        if (field('wdl_outputs').checked) heads.push('WDLx' + count('wdl_buckets'));
        if (field('uncertainty_outputs').checked) heads.push('UNCx' + count('uncertainty_buckets'));
        const pairwiseLayers = selectedPairwiseLayers();
        const pairwise = field('pairwise_activation').checked;
        byId('pairwise-options').hidden = !pairwise;
        byId('pairwise-options').querySelectorAll('select').forEach(input => { input.disabled = !pairwise; });
        const pairwiseLabel = '-pw(' + field('pairwise_left_activation').value.toUpperCase() + '*' + field('pairwise_right_activation').value.toUpperCase() + ')';
        const layerLabel = (size, index) => pairwiseLayers.includes(index + 1) ? size + pairwiseLabel + '=' + (Number.isInteger(size) ? size / 2 : '?') : size;
        const dense = [...layers.slice(1).map((size, index) => layerLabel(size, index + 1)), '(' + (heads.join(' + ') || 'no outputs') + ')'].join(' -> ');
        const skip = field('skip_connection').checked ? ' · skip L2 -> L3' : '';
        field('pairwise_activation').setCustomValidity(pairwiseLayers.some(layer => !Number.isInteger(layers[layer - 1]) || layers[layer - 1] % 2) ? 'Each selected pairwise layer must exist and have an even number of neurons.' : '');
        byId('architecture').textContent = '(' + inputs + ' -> ' + layerLabel(layers[0] ?? '?', 0) + ')x2 -> (' + dense + ')' + ' · ' + field('activation').value.toUpperCase() + skip;
    };
    const syncSkipConnection = () => {
        const pairwiseLayers = selectedPairwiseLayers();
        const layers = [...byId('layers').querySelectorAll('[data-layer]')].map((input, index) => input.valueAsNumber / (pairwiseLayers.includes(index + 1) ? 2 : 1));
        const invalid = field('skip_connection').checked && (layers.length < 3 || layers[1] !== layers[2]);
        field('skip_connection').setCustomValidity(invalid ? 'The skip connection requires Layer 2 and Layer 3 to have equal output widths after activation.' : '');
    };
    field('skip_connection').addEventListener('input', syncSkipConnection);
    byId('layers').addEventListener('input', syncSkipConnection);
    const renumberLayers = () => {
        const rows = [...byId('layers').children];
        rows.forEach((row, index) => {
            const input = row.querySelector('input');
            const label = row.querySelector('label');
            input.id = 'builder-layer-' + index;
            input.setAttribute('aria-label', 'Layer ' + (index + 1) + ' neurons');
            label.htmlFor = input.id;
            label.textContent = index ? 'Layer ' + (index + 1) : 'Feature layer';
            row.querySelector('button').disabled = rows.length === 1;
            row.querySelector('button').setAttribute('aria-label', 'Remove layer ' + (index + 1));
        });
        byId('add-layer').disabled = rows.length >= 8;
        syncSkipConnection();
        syncNetwork();
    };
    const addLayer = value => {
        const row = document.createElement('div');
        row.className = 'builder-layer';
        const label = document.createElement('label');
        const input = document.createElement('input');
        Object.assign(input, {type: 'number', min: '1', max: '8192', required: true, value: String(value)});
        input.dataset.layer = '';
        const remove = document.createElement('button');
        Object.assign(remove, {type: 'button', className: 'button', textContent: '×'});
        remove.addEventListener('click', () => {
            row.remove();
            renumberLayers();
            byId('add-layer').focus();
            changed();
        });
        row.append(label, input, remove);
        byId('layers').append(row);
        renumberLayers();
        return input;
    };
    data.spec.layers.forEach(addLayer);
    byId('add-layer').addEventListener('click', () => {
        if (byId('layers').children.length >= 8) return;
        addLayer(32).focus();
        changed();
    });
    const squareName = index => 'abcdefgh'[index % 8] + (Math.floor(index / 8) + 1);
    const coordinate = text => {
        const span = document.createElement('span');
        span.className = 'builder-coordinate';
        span.textContent = text;
        board.append(span);
    };
    coordinate('');
    [...'abcdefgh'].forEach(coordinate);
    for (let rank = 7; rank >= 0; rank--) {
        coordinate(String(rank + 1));
        for (let file = 0; file < 8; file++) {
            const square = document.createElement('button');
            Object.assign(square, {type: 'button', className: 'builder-square'});
            square.dataset.square = rank * 8 + file;
            square.dataset.dark = String((rank + file) % 2 === 0);
            square.tabIndex = rank === 7 && file === 0 ? 0 : -1;
            board.append(square);
        }
    }
    const drawBoard = () => {
        board.querySelectorAll('[data-square]').forEach(square => {
            const index = Number(square.dataset.square);
            const bucket = layout[index];
            square.textContent = bucket;
            square.setAttribute('aria-label', squareName(index) + ', bucket ' + bucket);
            square.dataset.assigned = String(bucket > 0);
            square.dataset.paint = String(bucket === Number(paint.value));
            square.style.setProperty('--bucket-hue', (bucket * 137.5 + 210) % 360);
        });
        const count = field('input_buckets').valueAsNumber;
        const missing = Array.from({length: Math.min(count || 0, 64)}, (_, i) => i).filter(bucket => !layout.includes(bucket));
        message(byId('layout-status'), missing.length ? 'Unassigned buckets: ' + missing.join(', ') : '', !!missing.length);
        byId('undo-layout').disabled = !undo.length;
    };
    const updatePalette = () => {
        field('input_buckets').max = field('mirrored').checked ? '32' : '64';
        const count = field('input_buckets').valueAsNumber;
        if (!Number.isInteger(count) || count < 1 || count > Number(field('input_buckets').max)) return;
        const previous = paint.value;
        paint.replaceChildren(...Array.from({length: count}, (_, i) => new Option('Bucket ' + i, String(i))));
        paint.value = Number(previous) < count ? previous || '0' : '0';
        drawBoard();
    };
    updatePalette();
    byId('assign-buckets').addEventListener('click', () => {
        updatePalette();
        byId('bucket-dialog').showModal();
    });
    byId('close-buckets').addEventListener('click', () => byId('bucket-dialog').close());
    paint.addEventListener('change', drawBoard);
    const rememberLayout = () => {
        undo.push({layout: [...layout], count: field('input_buckets').value, mirrored: field('mirrored').checked});
        if (undo.length > 50) undo.shift();
    };
    let stroke;
    const paintSquare = (square, bucket, mirrored) => {
        if (!square || !board.contains(square) || square.matches(':disabled')) return;
        const index = Number(square.dataset.square);
        const mirror = Math.floor(index / 8) * 8 + 7 - index % 8;
        if (layout[index] === bucket && (!mirrored || layout[mirror] === bucket)) return;
        if (!stroke || !stroke.changed) rememberLayout();
        if (stroke) stroke.changed = true;
        layout[index] = bucket;
        if (mirrored) layout[mirror] = bucket;
        drawBoard();
        changed();
    };
    const endStroke = () => {
        if (!stroke) return;
        const pointerId = stroke.pointerId;
        stroke = undefined;
        if (board.hasPointerCapture(pointerId)) board.releasePointerCapture(pointerId);
    };
    board.addEventListener('pointerdown', event => {
        const square = event.target.closest('[data-square]');
        if (stroke || event.button !== 0 || !event.isPrimary || !square || square.matches(':disabled') || !paint.options.length) return;
        event.preventDefault();
        square.focus();
        stroke = {pointerId: event.pointerId, square, bucket: Number(paint.value), mirrored: field('mirrored').checked, changed: false, dragged: false};
        board.setPointerCapture(event.pointerId);
    });
    board.addEventListener('pointermove', event => {
        if (!stroke || event.pointerId !== stroke.pointerId) return;
        if (!(event.buttons & 1)) {
            endStroke();
            return;
        }
        const square = document.elementFromPoint(event.clientX, event.clientY)?.closest('[data-square]');
        if (!square || !board.contains(square) || square.matches(':disabled') || (!stroke.dragged && square === stroke.square)) return;
        if (!stroke.dragged) {
            stroke.dragged = true;
            paintSquare(stroke.square, stroke.bucket, stroke.mirrored);
        }
        paintSquare(square, stroke.bucket, stroke.mirrored);
    });
    ['pointerup', 'pointercancel', 'lostpointercapture'].forEach(type => {
        board.addEventListener(type, event => {
            if (stroke?.pointerId !== event.pointerId) return;
            if (type === 'pointerup' && !stroke.dragged) {
                const square = document.elementFromPoint(event.clientX, event.clientY)?.closest('[data-square]');
                if (square === stroke.square) paintSquare(square, (layout[Number(square.dataset.square)] + 1) % paint.options.length, stroke.mirrored);
            }
            endStroke();
        });
    });
    window.addEventListener('blur', endStroke);
    board.addEventListener('click', event => {
        if (event.detail !== 0 || !paint.options.length) return;
        const square = event.target.closest('[data-square]');
        if (square) paintSquare(square, (layout[Number(square.dataset.square)] + 1) % paint.options.length, field('mirrored').checked);
    });
    board.addEventListener('focusin', event => {
        if (!event.target.matches('[data-square]')) return;
        board.querySelectorAll('[data-square]').forEach(square => { square.tabIndex = square === event.target ? 0 : -1; });
    });
    board.addEventListener('keydown', event => {
        if (!event.target.matches('[data-square]')) return;
        const index = Number(event.target.dataset.square);
        const delta = {ArrowRight: 1, ArrowLeft: -1, ArrowUp: 8, ArrowDown: -8}[event.key];
        if (!delta) return;
        event.preventDefault();
        if (Math.abs(delta) === 1 && Math.floor(index / 8) !== Math.floor((index + delta) / 8)) return;
        board.querySelector('[data-square="' + (index + delta) + '"]')?.focus();
    });
    field('input_buckets').addEventListener('input', updatePalette);
    field('input_buckets').addEventListener('change', () => {
        const count = field('input_buckets').valueAsNumber;
        if (!Number.isInteger(count) || count < 1 || count > Number(field('input_buckets').max)) return;
        if (layout.some(bucket => bucket >= count)) {
            rememberLayout();
            undo[undo.length - 1].count = String(Math.max(...layout) + 1);
            layout = layout.map(bucket => bucket < count ? bucket : 0);
        }
        updatePalette();
        changed();
    });
    field('mirrored').addEventListener('change', () => {
        rememberLayout();
        undo[undo.length - 1].mirrored = !field('mirrored').checked;
        if (field('mirrored').checked) {
            for (let rank = 0; rank < 8; rank++) {
                for (let file = 0; file < 4; file++) layout[rank * 8 + 7 - file] = layout[rank * 8 + file];
            }
        }
        updatePalette();
        changed();
    });
    byId('flip-layout').addEventListener('click', () => {
        endStroke();
        rememberLayout();
        layout = layout.map((_, index) => layout[index ^ 56]);
        drawBoard();
        changed();
    });
    byId('undo-layout').addEventListener('click', () => {
        const previous = undo.pop();
        if (!previous) return;
        layout = previous.layout;
        field('input_buckets').value = previous.count;
        field('mirrored').checked = previous.mirrored;
        updatePalette();
        changed();
    });
    const totalSuperbatches = () => Math.max(1, Math.floor(field('superbatches').valueAsNumber) || data.spec.superbatches);
    const normalizeWdl = values => {
        const total = totalSuperbatches();
        const stages = values.slice(0, total).map(stage => ({...stage}));
        let start = 1;
        stages.forEach((stage, index) => {
            stage.start = start;
            stage.end = index === stages.length - 1 ? total : Math.max(start, Math.min(Number.isInteger(stage.end) ? stage.end : start, total - (stages.length - index - 1)));
            if (stage.kind === 'constant' || stage.start === stage.end) stage.final = stage.initial;
            start = stage.end + 1;
        });
        return stages;
    };
    const readWdl = () => [...byId('wdl-stages').children].map(row => Object.fromEntries(
        [...row.querySelectorAll('[data-stage-field]')].map(input => [input.dataset.stageField, input.type === 'number' ? input.valueAsNumber : input.value])));
    const syncWdl = () => {
        const stages = normalizeWdl(readWdl());
        const rows = [...byId('wdl-stages').children];
        stages.forEach((stage, index) => {
            const row = rows[index];
            const get = name => row.querySelector('[data-stage-field="' + name + '"]');
            get('start').value = stage.start;
            get('end').value = stage.end;
            get('start').readOnly = true;
            get('end').readOnly = index === stages.length - 1;
            get('end').min = stage.start;
            get('end').max = totalSuperbatches() - (stages.length - index - 1);
            const constant = stage.kind === 'constant' || stage.start === stage.end;
            get('final').readOnly = constant;
            if (constant) get('final').value = stage.initial;
        });
        byId('add-wdl-stage').disabled = stages.length >= totalSuperbatches();
    };
    const renderWdl = values => {
        const stages = normalizeWdl(values);
        byId('wdl-stages').replaceChildren();
        stages.forEach((stage, index) => {
            const row = document.createElement('fieldset');
            row.className = 'builder-stage';
            const legend = document.createElement('legend');
            legend.textContent = 'Stage ' + (index + 1);
            const grid = document.createElement('div');
            grid.className = 'field-grid';
            for (const [key, title] of [['start', 'Starting superbatch'], ['end', 'Ending superbatch'], ['kind', 'Curve'], ['initial', 'Initial value'], ['final', 'Final value']]) {
                const label = document.createElement('label');
                label.className = 'field';
                label.append(document.createTextNode(title));
                const input = document.createElement(key === 'kind' ? 'select' : 'input');
                input.dataset.stageField = key;
                if (key === 'kind') input.append(new Option('Constant', 'constant'), new Option('Linear', 'linear'), new Option('Cosine', 'cosine'));
                else Object.assign(input, {type: 'number', min: ['start', 'end'].includes(key) ? '1' : '0', max: ['start', 'end'].includes(key) ? '1000000' : '1', step: ['start', 'end'].includes(key) ? '1' : 'any', required: true});
                input.value = stage[key];
                label.append(input);
                grid.append(label);
            }
            grid.addEventListener('input', syncWdl);
            const remove = document.createElement('button');
            Object.assign(remove, {type: 'button', className: 'button', textContent: 'Remove stage', disabled: stages.length === 1});
            remove.addEventListener('click', () => {
                const values = readWdl();
                values.splice(index, 1);
                renderWdl(values);
                changed();
            });
            row.append(legend, grid, remove);
            byId('wdl-stages').append(row);
        });
        syncWdl();
    };
    renderWdl(data.spec.wdl_stages);
    byId('add-wdl-stage').addEventListener('click', () => {
        const stages = normalizeWdl(readWdl());
        if (stages.length >= totalSuperbatches()) return;
        const last = stages[stages.length - 1];
        last.end = Math.floor((last.start + last.end) / 2);
        stages.push({start: last.end + 1, end: totalSuperbatches(), kind: 'constant', initial: last.final, final: last.final});
        renderWdl(stages);
        changed();
    });
    const initialLr = data.spec.lr_stages[0];
    byId('lr-kind').value = initialLr.kind;
    byId('lr-initial').value = initialLr.initial;
    byId('lr-final').value = data.spec.lr_stages[data.spec.lr_stages.length - 1].final;
    const syncLr = () => {
        const constant = byId('lr-kind').value === 'constant' || totalSuperbatches() === 1;
        byId('lr-final').readOnly = constant;
        if (constant) byId('lr-final').value = byId('lr-initial').value;
    };
    byId('lr-kind').addEventListener('input', syncLr);
    byId('lr-initial').addEventListener('input', syncLr);
    syncLr();
    field('superbatches').addEventListener('input', () => {
        if (!Number.isInteger(field('superbatches').valueAsNumber) || field('superbatches').valueAsNumber < 1) return;
        renderWdl(readWdl());
        syncLr();
    });
    const readSpec = () => {
        const spec = {...data.spec, king_layout: [...layout], layers: [...form.querySelectorAll('[data-layer]')].map(input => input.valueAsNumber)};
        for (const name of Object.keys(spec)) {
            const input = field(name);
            if (input) spec[name] = input.type === 'checkbox' ? input.checked : input.type === 'number' ? input.valueAsNumber : input.value;
        }
        spec.lr_stages = [{start: 1, end: totalSuperbatches(), kind: byId('lr-kind').value, initial: byId('lr-initial').valueAsNumber, final: byId('lr-final').valueAsNumber}];
        spec.pairwise_layers = byId('pairwise-layers').value.split(',').map(Number);
        spec.wdl_stages = normalizeWdl(readWdl());
        if (!spec.psqt_inputs && !spec.half_move_clock) {
            spec.input_buckets = 1;
            spec.king_layout = Array(64).fill(0);
        }
        spec.piece_count_keep = [...byId('piece-count-keep').querySelectorAll('input')].map(input => input.valueAsNumber);
        return spec;
    };
    const request = async (body, signal) => {
        const response = await fetch(endpoint, {
            method: 'POST', credentials: 'same-origin', signal,
            headers: {'Content-Type': 'application/json', 'X-CSRFToken': field('csrfmiddlewaretoken').value},
            body: JSON.stringify(body),
        });
        const result = await response.json().catch(() => ({}));
        if (!response.ok || response.redirected) throw new Error(result.error || 'Could not save or generate the schedule. Check your connection and sign-in.');
        return result;
    };
    const updateSource = text => {
        source.value = text;
        editor?.setFile('examples/mattbench.rs', text);
        sourceCurrent = true;
        byId('download').disabled = false;
        message(previewStatus, 'Generated Rust · ' + text.split('\n').length + ' lines');
    };
    const preview = async () => {
        const serial = ++generation;
        controller?.abort();
        controller = new AbortController();
        try {
            const result = await request({action: 'preview', spec: readSpec()}, controller.signal);
            if (serial === generation) updateSource(result.source);
        } catch (error) {
            if (error.name !== 'AbortError' && serial === generation) message(previewStatus, error.message, true);
        }
    };
    function changed(regenerate = true) {
        syncNetwork();
        syncSkipConnection();
        dirty = true;
        byId('use').hidden = true;
        message(feedback, 'Unsaved changes');
        if (!regenerate) return;
        sourceCurrent = false;
        byId('download').disabled = true;
        generation++;
        controller?.abort();
        clearTimeout(timer);
        message(previewStatus, 'Updating Rust…');
        timer = setTimeout(preview, 400);
    }
    form.addEventListener('input', event => {
        if (event.target === paint || event.target === source) return;
        changed(!event.target.closest('.builder-identity'));
    });
    const revealInvalid = input => {
        const panel = input.closest('[role=tabpanel]');
        if (panel) showTab(panel.id.replace('panel-', ''));
        const details = input.closest('details');
        if (details) details.open = true;
    };
    form.addEventListener('invalid', event => revealInvalid(event.target), true);
    form.addEventListener('submit', async event => {
        event.preventDefault();
        if (saving) return;
        const invalid = form.querySelector('input:invalid, select:invalid, textarea:invalid');
        if (invalid) {
            revealInvalid(invalid);
            invalid.reportValidity();
            return;
        }
        const spec = readSpec();
        const body = {action: 'save', spec, version, name: byId('name').value.trim(), engine: byId('engine').value, scope: byId('scope').value};
        saving = true;
        generation++;
        clearTimeout(timer);
        controller?.abort();
        byId('fields').disabled = true;
        byId('save').disabled = true;
        message(feedback, 'Saving…');
        try {
            const result = await request(body);
            version = result.version;
            endpoint = result.url;
            data.spec = result.spec;
            history.replaceState(null, '', result.url);
            dirty = false;
            byId('open-source').href = result.source_url;
            byId('open-source').hidden = false;
            byId('use').href = result.train_url;
            byId('use').hidden = false;
            form.querySelector('.builder-notice')?.remove();
            updateSource(result.source);
            message(feedback, 'Saved · v' + result.version);
        } catch (error) {
            message(feedback, error.message, true);
            if (!sourceCurrent) message(previewStatus, 'Preview is out of date', true);
        } finally {
            saving = false;
            byId('fields').disabled = false;
            byId('save').disabled = false;
        }
    });
    byId('download').addEventListener('click', () => {
        if (!sourceCurrent) return;
        const url = URL.createObjectURL(new Blob([source.value], {type: 'text/plain;charset=utf-8'}));
        const link = document.createElement('a');
        link.href = url;
        link.download = 'mattbench.rs';
        link.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
    });
    document.addEventListener('keydown', event => {
        if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
            event.preventDefault();
            form.requestSubmit();
        }
    });
    window.addEventListener('beforeunload', event => {
        if (!dirty && !saving) return;
        event.preventDefault();
        event.returnValue = '';
    });
    import(form.dataset.editorUrl).then(module => {
        editor = module.createEditor(source, 'examples/mattbench.rs', source.value, () => {}, true);
    }).catch(() => {});
})();
