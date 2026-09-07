document.addEventListener('DOMContentLoaded', () => {
    const form = document.getElementById('repository-form');
    const feedback = document.getElementById('repo-feedback');
    const deleted = document.getElementById('deleted-repos');
    const engineSelect = document.getElementById('new-engine-name');
    const repository = document.getElementById('new-engine-repo');

    function updateDefault() {
        form.querySelectorAll('.repo-row').forEach(row => {
            const selected = row.querySelector('input[type="radio"]').checked;
            const remove = row.querySelector('.remove-repo');
            remove.disabled = selected;
            remove.title = selected ? 'Choose another default engine before removing this repository' : 'Remove repository';
        });
    }

    form.addEventListener('change', updateDefault);
    form.querySelectorAll('.remove-repo').forEach(button => {
        button.addEventListener('click', () => {
            const row = button.closest('.repo-row');
            if (row.querySelector('input[type="radio"]').checked) return;
            const engine = row.dataset.engine;
            const removed = JSON.parse(deleted.value);
            if (!removed.includes(engine)) removed.push(engine);
            deleted.value = JSON.stringify(removed);
            row.remove();
            if (!Array.from(engineSelect.options).some(option => option.value === engine)) {
                engineSelect.add(new Option(engine, engine));
            }
            feedback.hidden = false;
            feedback.textContent = `${engine} will be removed when you save. Reload this page to discard unsaved changes.`;
            engineSelect.focus();
        });
    });

    function validateRepository() {
        const value = repository.value.trim();
        repository.required = engineSelect.value !== 'None';
        engineSelect.setCustomValidity(value && engineSelect.value === 'None' ? 'Choose an engine for this repository.' : '');
        repository.setCustomValidity('');
        if (!value) return;
        try {
            const url = new URL(value);
            const validPath = /^\/[A-Za-z0-9-]+\/[A-Za-z0-9._-]+\/?$/.test(url.pathname);
            if (url.protocol !== 'https:' || url.host !== 'github.com' || url.username || url.password || url.search || url.hash || !validPath) {
                repository.setCustomValidity('Enter a GitHub repository URL, such as https://github.com/owner/engine.');
            }
        } catch {
            repository.setCustomValidity('Enter the full HTTPS URL of your GitHub repository.');
        }
    }

    engineSelect.addEventListener('change', validateRepository);
    repository.addEventListener('input', validateRepository);
    form.addEventListener('submit', event => {
        repository.value = repository.value.trim();
        validateRepository();
        if (!form.reportValidity()) event.preventDefault();
    });

    const password = document.getElementById('password1');
    const confirmation = document.getElementById('password2');
    function validatePassword() {
        confirmation.setCustomValidity(password.value === confirmation.value ? '' : 'The passwords do not match.');
    }
    password.addEventListener('input', validatePassword);
    confirmation.addEventListener('input', validatePassword);
    updateDefault();
});
