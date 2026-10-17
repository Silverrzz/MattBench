(() => {
    const sync = root => {
        const delay = '-' + (Date.now() % 4800) + 'ms';
        root.querySelectorAll('.test-error-pulse').forEach(row => row.style.setProperty('--error-pulse-delay', delay));
    };
    sync(document);
    window.addEventListener('live-content', event => {
        const root = document.getElementById(event.detail.id);
        if (root) sync(root);
    });
})();
