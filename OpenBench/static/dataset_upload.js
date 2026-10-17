(() => {
    const picker = document.getElementById('hf-existing');
    const repository = document.getElementById('hf-repo');
    if (!picker || !repository) return;
    picker.addEventListener('change', () => {
        const option = picker.selectedOptions[0];
        if (!option.value) return;
        repository.value = option.value;
        if (['true', 'false'].includes(option.dataset.private)) {
            document.getElementById('hf-visibility').value = option.dataset.private === 'true' ? 'private' : 'public';
        }
    });
    repository.addEventListener('input', () => { picker.value = ''; });
})();
