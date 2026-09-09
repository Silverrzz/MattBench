function prepare_responsive_tables(root) {
    root.querySelectorAll('table:not(.test-config)').forEach(table => {
        if (table.parentElement.classList.contains('table-scroll')) return;
        const wrapper = document.createElement('div');
        wrapper.className = 'table-scroll';
        wrapper.tabIndex = 0;
        wrapper.setAttribute('role', 'region');
        wrapper.setAttribute('aria-label', 'Scrollable data table');
        table.before(wrapper);
        wrapper.appendChild(table);
    });
}

document.addEventListener('DOMContentLoaded', () => {
    const sidebar = document.getElementById('sidebar');
    const toggle = document.getElementById('sidebar-toggle');
    const content = document.getElementById('content-body');
    const compact = window.matchMedia('(max-width: 1024px)');

    function set_open(open, restore_focus = false) {
        open = open && compact.matches;
        document.body.classList.toggle('sidebar-open', open);
        toggle.setAttribute('aria-expanded', String(open));
        toggle.setAttribute('aria-label', open ? 'Close navigation' : 'Open navigation');
        sidebar.inert = compact.matches && !open;
        content.inert = open;
        if (open) sidebar.querySelector('a').focus();
        else if (restore_focus) toggle.focus();
    }

    toggle.addEventListener('click', () => {
        set_open(!document.body.classList.contains('sidebar-open'), true);
    });
    document.getElementById('sidebar-backdrop').addEventListener('click', () => set_open(false, true));
    sidebar.addEventListener('click', event => {
        if (event.target.closest('a')) set_open(false);
    });
    compact.addEventListener('change', () => set_open(false));
    document.addEventListener('keydown', event => {
        if (!document.body.classList.contains('sidebar-open')) return;
        if (event.key === 'Escape') set_open(false, true);
        if (event.key === 'Tab') {
            const links = [...sidebar.querySelectorAll('a[href]'), toggle];
            const index = links.indexOf(document.activeElement);
            event.preventDefault();
            links[(index + (event.shiftKey ? -1 : 1) + links.length) % links.length].focus();
        }
    });
    set_open(false);
    prepare_responsive_tables(document);
});
