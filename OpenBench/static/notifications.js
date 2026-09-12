document.addEventListener('click', (event) => {
    const button = event.target.closest('[data-notification-mode]');
    if (!button) return;
    button.closest('[data-notification-group]').querySelectorAll('select').forEach((select) => {
        select.value = button.dataset.notificationMode;
        select.dispatchEvent(new Event('change', {bubbles: true}));
    });
});
