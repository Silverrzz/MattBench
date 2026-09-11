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
        stroke = {pointerId: event.pointerId, bucket: Number(paint.value), mirrored: field('mirrored').checked, changed: false};
        board.setPointerCapture(event.pointerId);
        paintSquare(square, stroke.bucket, stroke.mirrored);
    });
    board.addEventListener('pointermove', event => {
        if (!stroke || event.pointerId !== stroke.pointerId) return;
        if (!(event.buttons & 1)) {
            endStroke();
            return;
        }
        const square = document.elementFromPoint(event.clientX, event.clientY)?.closest('[data-square]');
        paintSquare(square, stroke.bucket, stroke.mirrored);
    });
    ['pointerup', 'pointercancel', 'lostpointercapture'].forEach(type => {
        board.addEventListener(type, event => {
            if (stroke?.pointerId === event.pointerId) endStroke();
        });
    });
    window.addEventListener('blur', endStroke);
    board.addEventListener('click', event => {
        if (event.detail !== 0 || !paint.options.length) return;
        paintSquare(event.target.closest('[data-square]'), Number(paint.value), field('mirrored').checked);
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
    byId('undo-layout').addEventListener('click', () => {
        const previous = undo.pop();
        if (!previous) return;
        layout = previous.layout;
        field('input_buckets').value = previous.count;
        field('mirrored').checked = previous.mirrored;
        updatePalette();
        changed();
    });
    const readStages = channel => [...byId(channel + '-stages').children].map(row =>
        Object.fromEntries([...row.querySelectorAll('[data-stage-field]')].map(input =>
            [input.dataset.stageField, input.type === 'number' ? input.valueAsNumber : input.value])));
    const renderStages = (channel, stages) => {
        const container = byId(channel + '-stages');
        container.replaceChildren();
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
                if (key === 'kind') {
                    input.append(new Option('Constant', 'constant'), new Option('Linear', 'linear'), new Option('Cosine', 'cosine'));
                } else {
                    Object.assign(input, {type: 'number', min: ['start', 'end'].includes(key) ? '1' : '0', max: ['start', 'end'].includes(key) ? '1000000' : '1', step: ['start', 'end'].includes(key) ? '1' : 'any', required: true});
                }
                input.value = stage[key];
                label.append(input);
                grid.append(label);
            }
            const sync = () => {
                const constant = grid.querySelector('select').value === 'constant';
                const final = grid.querySelector('[data-stage-field="final"]');
                final.disabled = constant;
                final.parentElement.hidden = constant;
                if (constant) final.value = grid.querySelector('[data-stage-field="initial"]').value;
            };
            grid.addEventListener('input', sync);
            sync();
            const remove = document.createElement('button');
            Object.assign(remove, {type: 'button', className: 'button', textContent: 'Remove stage', disabled: stages.length === 1});
            remove.addEventListener('click', () => {
                const values = readStages(channel);
                values.splice(index, 1);
                renderStages(channel, values);
                changed();
            });
            row.append(legend, grid, remove);
            container.append(row);
        });
    };
    for (const channel of ['lr', 'wdl']) {
        renderStages(channel, data.spec[channel + '_stages']);
        byId('add-' + channel + '-stage').addEventListener('click', () => {
            const stages = readStages(channel);
            const last = stages[stages.length - 1];
            const end = field('superbatches').valueAsNumber;
            let start = last.end + 1;
            if (last.end === end && last.end > last.start) {
                start = Math.floor((last.start + last.end) / 2) + 1;
                last.end = start - 1;
            }
            stages.push({start, end: Math.max(start, end), kind: 'constant', initial: last.final, final: last.final});
            renderStages(channel, stages);
            changed();
        });
    }
    let previousSuperbatches = data.spec.superbatches;
    field('superbatches').addEventListener('change', () => {
        const total = field('superbatches').valueAsNumber;
        if (!Number.isInteger(total) || total < 1) return;
        for (const channel of ['lr', 'wdl']) {
            const stages = readStages(channel);
            const last = stages[stages.length - 1];
            if (last.end === previousSuperbatches && total >= last.start) {
                last.end = total;
                renderStages(channel, stages);
            }
        }
        previousSuperbatches = total;
        changed();
    });
    const readSpec = () => {
        const spec = {...data.spec, king_layout: [...layout], layers: [...form.querySelectorAll('[data-layer]')].map(input => input.valueAsNumber)};
        for (const name of Object.keys(spec)) {
            const input = field(name);
            if (input) spec[name] = input.type === 'checkbox' ? input.checked : input.type === 'number' ? input.valueAsNumber : input.value;
        }
        for (const channel of ['lr', 'wdl']) spec[channel + '_stages'] = readStages(channel);
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
