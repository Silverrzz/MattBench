function format_live_content(root) {
    const testNotes = Array.from(root.querySelectorAll('[data-test-notes]'));
    const notesToggles = root.querySelectorAll('[data-show-test-notes]');
    let showNotes = false;
    try { showNotes = localStorage.getItem('mattbench.showNotes') === 'true'; } catch {}
    testNotes.forEach(notes => {
        notes.closest('.test-notes-row').hidden = !showNotes;
    });
    notesToggles.forEach(toggle => {
        toggle.closest('label').hidden = testNotes.length === 0;
        toggle.checked = showNotes;
        toggle.addEventListener('change', () => {
            showNotes = toggle.checked;
            try { localStorage.setItem('mattbench.showNotes', String(toggle.checked)); } catch {}
            notesToggles.forEach(control => { control.checked = toggle.checked; });
            testNotes.forEach(notes => {
                notes.closest('.test-notes-row').hidden = !showNotes;
            });
        });
    });
    for (const [selector, options] of [
        ['.timestamp', { year: 'numeric', month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }],
        ['.datestamp', { month: 'short', day: '2-digit' }]
    ]) {
        root.querySelectorAll(selector + ':not([data-formatted])').forEach(element => {
            const date = new Date(Number(element.textContent.trim()) * 1000);
            if (!Number.isFinite(date.getTime())) return;
            element.textContent = date.toLocaleString(undefined, options);
            element.title = date.toISOString();
            element.dataset.formatted = 'true';
        });
    }

    root.querySelectorAll('.engine-options:not([data-formatted])').forEach(cell => {
        cell.dataset.formatted = 'true';
        const value = cell.textContent.trim();
        if (value.split(/\s+/).length <= 2) return;
        const threads = value.match(/Threads=\d+/);
        const hash = value.match(/Hash=\d+/);
        if (!threads || !hash) return;
        const details = document.createElement('details');
        const summary = document.createElement('summary');
        const options = document.createElement('div');
        summary.textContent = `${threads[0]} ${hash[0]} · All options`;
        options.className = 'engine-options-expanded';
        options.textContent = value.replace(/\s+/g, '\n');
        details.append(summary, options);
        cell.replaceChildren(details);
    });

    root.querySelectorAll('table:not(.test-config)').forEach(table => {
        if (table.closest('.table-scroll')) return;
        const region = document.createElement('div');
        region.className = 'table-scroll';
        region.tabIndex = 0;
        region.setAttribute('role', 'region');
        region.setAttribute('aria-label', `${table.getAttribute('aria-label') || 'Data table'}; scroll horizontally for more columns`);
        table.before(region);
        region.append(table);
    });

}

document.addEventListener('DOMContentLoaded', () => {
    const sidebar = document.getElementById('sidebar');
    const toggle = document.getElementById('sidebar-toggle');
    const close = document.getElementById('sidebar-close');
    const backdrop = document.querySelector('.sidebar-backdrop');
    const content = document.getElementById('content-parent');
    const mobile = window.matchMedia('(max-width: 900px)');

    function setNavigation(open, restoreFocus = true) {
        const expanded = open && mobile.matches;
        document.body.classList.toggle('sidebar-open', expanded);
        toggle.setAttribute('aria-expanded', String(expanded));
        backdrop.hidden = !expanded;
        sidebar.inert = mobile.matches && !expanded;
        content.inert = expanded;
        if (expanded) close.focus();
        else if (restoreFocus && mobile.matches) toggle.focus();
    }

    toggle.addEventListener('click', () => setNavigation(true));
    close.addEventListener('click', () => setNavigation(false));
    backdrop.addEventListener('click', () => setNavigation(false));
    mobile.addEventListener('change', () => setNavigation(false, false));
    setNavigation(false, false);

    document.addEventListener('keydown', event => {
        if (!document.body.classList.contains('sidebar-open')) return;
        if (event.key === 'Escape') setNavigation(false);
        if (event.key !== 'Tab') return;
        const focusable = Array.from(sidebar.querySelectorAll('a[href], button, summary')).filter(element => element.getClientRects().length);
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first.focus();
        }
    });

    const parts = window.location.pathname.split('/').filter(Boolean);
    const section = parts[0] || 'index';
    const aliases = { index: 'tests', user: 'tests', test: 'tests', tune: 'tests', datagen: 'tests', newNetwork: 'uploadnet', profileConfig: 'profile' };
    const current = parts[1] === 'new' ? `new-${section}` : aliases[section] || section;
    sidebar.querySelector(`[data-nav="${current.replace(/[^a-z-]/g, '')}"]`)?.setAttribute('aria-current', 'page');

    format_live_content(document);
});

document.addEventListener('submit', event => {
    const form = event.target;
    if (form.dataset.submitting) {
        event.preventDefault();
        return;
    }
    if (event.defaultPrevented) return;
    form.dataset.submitting = 'true';
    form.setAttribute('aria-busy', 'true');
    const submitter = event.submitter;
    if (submitter) {
        submitter.setAttribute('aria-disabled', 'true');
        submitter.dataset.originalText = submitter.tagName === 'INPUT' ? submitter.value : submitter.textContent;
        if (submitter.tagName === 'BUTTON') submitter.textContent = 'Saving…';
    }
});

window.addEventListener('pageshow', () => {
    document.querySelectorAll('form[data-submitting]').forEach(form => {
        delete form.dataset.submitting;
        form.removeAttribute('aria-busy');
        form.querySelectorAll('[data-original-text]').forEach(button => {
            button.removeAttribute('aria-disabled');
            if (button.tagName === 'BUTTON') button.textContent = button.dataset.originalText;
            delete button.dataset.originalText;
        });
    });
});
