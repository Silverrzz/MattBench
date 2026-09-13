(() => {
    const root = document.querySelector('#engine-maintainers');
    if (!root) return;
    const picker = root.querySelector('details');
    const label = root.querySelector('[data-picker-label]');
    const search = root.querySelector('#maintainer-search');
    const select = root.querySelector('#maintainer-account');
    const add = root.querySelector('[data-add-maintainer]');
    const list = root.querySelector('.maintainer-list');
    const status = root.querySelector('[role="status"]');
    const accounts = Array.from(select.options, option => ({id: option.value, name: option.textContent}));
    let pending = null;
    const refresh = () => {
        const selected = new Set(Array.from(list.children, row => row.dataset.maintainer));
        const query = search.value.trim().toLocaleLowerCase();
        select.replaceChildren(...accounts.filter(account => !selected.has(account.id) && account.name.toLocaleLowerCase().includes(query)).map(account => new Option(account.name, account.id)));
        select.value = pending?.id || '';
        root.querySelector('[data-no-matches]').hidden = select.options.length > 0;
        select.hidden = select.options.length === 0;
        root.querySelector('[data-empty-maintainers]').hidden = selected.size > 0;
        add.disabled = !pending;
    };
    const choose = () => {
        pending = accounts.find(account => account.id === select.value) || null;
        label.textContent = pending?.name || 'Select an account';
        add.disabled = !pending;
        if (pending) {
            picker.open = false;
            add.focus();
        }
    };
    search.addEventListener('input', refresh);
    search.addEventListener('keydown', event => {
        if (event.key === 'Enter') {
            event.preventDefault();
            if (select.options.length) { select.selectedIndex = 0; choose(); }
        } else if (event.key === 'ArrowDown' && select.options.length) {
            event.preventDefault();
            select.focus();
        }
    });
    select.addEventListener('change', choose);
    select.addEventListener('keydown', event => {
        if (event.key === 'Enter') { event.preventDefault(); choose(); }
    });
    picker.addEventListener('toggle', () => { if (picker.open) { refresh(); search.focus(); } });
    picker.addEventListener('keydown', event => {
        if (event.key === 'Escape') { picker.open = false; picker.querySelector('summary').focus(); }
    });
    document.addEventListener('click', event => { if (!picker.contains(event.target)) picker.open = false; });
    add.addEventListener('click', () => {
        if (!pending || Array.from(list.children).some(row => row.dataset.maintainer === pending.id)) return;
        const row = document.createElement('li');
        row.dataset.maintainer = pending.id;
        const name = document.createElement('span');
        name.textContent = pending.name;
        const input = document.createElement('input');
        input.type = 'hidden'; input.name = 'maintainers'; input.value = pending.id;
        const remove = document.createElement('button');
        remove.type = 'button'; remove.className = 'button maintainer-icon'; remove.dataset.removeMaintainer = '';
        remove.textContent = '×'; remove.setAttribute('aria-label', `Remove ${pending.name} as maintainer`);
        row.append(name, input, remove); list.append(row);
        status.textContent = `${pending.name} added.`;
        pending = null; search.value = ''; label.textContent = 'Select an account';
        refresh(); picker.querySelector('summary').focus();
    });
    list.querySelectorAll('button').forEach(button => { button.disabled = false; });
    list.addEventListener('click', event => {
        const button = event.target.closest('[data-remove-maintainer]');
        if (!button) return;
        const row = button.closest('li');
        status.textContent = `${row.querySelector('span').textContent} removed.`;
        const next = row.nextElementSibling || row.previousElementSibling;
        row.remove(); refresh();
        (next?.querySelector('button') || picker.querySelector('summary')).focus();
    });
    refresh();
})();
