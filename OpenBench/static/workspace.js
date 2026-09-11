(() => {
    function filter(scope) {
        const query = scope.querySelector('[data-search]')?.value.trim().toLowerCase() || '';
        const filters = [...scope.querySelectorAll('[data-filter-select]')];
        let shown = 0;
        for (const row of scope.querySelectorAll('[data-search-row]')) {
            const matches = (!query || row.dataset.searchRow.toLowerCase().includes(query)) && filters.every(select => !select.value || row.dataset[select.dataset.filterSelect] === select.value || select.dataset.filterSelect === 'engine' && row.dataset.engine === '*');
            row.hidden = !matches;
            if (matches) shown++;
        }
        for (const section of scope.querySelectorAll('[data-filter-section]')) {
            const rows = [...section.querySelectorAll('[data-search-row]')];
            section.hidden = Boolean((query || filters.some(select => select.value)) && !rows.some(row => !row.hidden));
        }
        const empty = scope.querySelector('[data-search-empty]');
        if (empty) empty.hidden = shown > 0 || !(query || filters.some(select => select.value));
    }
    for (const scope of document.querySelectorAll('[data-filter-scope]')) {
        scope.querySelectorAll('[data-search], [data-filter-select]').forEach(input => input.addEventListener('input', () => filter(scope)));
        filter(scope);
    }
    window.addEventListener('live-content', () => document.querySelectorAll('[data-filter-scope]').forEach(filter));
    for (const scope of document.querySelectorAll('[data-tabs]')) {
        const buttons = [...scope.querySelectorAll('[data-tab]')];
        function select(name, focus = false) {
            for (const button of buttons) {
                const active = button.dataset.tab === name;
                button.setAttribute('aria-selected', String(active));
                button.tabIndex = active ? 0 : -1;
                if (active && focus) button.focus();
            }
            for (const panel of scope.querySelectorAll('[data-tab-panel]')) {
                const active = panel.dataset.tabPanel === name;
                panel.hidden = !active;
                if (panel.tagName === 'DETAILS') panel.open = active;
            }
        }
        buttons.forEach((button, index) => {
            button.addEventListener('click', () => select(button.dataset.tab));
            button.addEventListener('keydown', event => {
                if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
                event.preventDefault();
                const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + buttons.length) % buttons.length;
                select(buttons[next].dataset.tab, true);
            });
        });
        select(scope.dataset.tabs || buttons[0]?.dataset.tab);
    }
})();
