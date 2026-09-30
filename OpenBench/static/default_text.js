function enforce_default_text(field, text) {

    field.addEventListener('input', () => {

        if (field.value.startsWith(text))
            return;

        if (field.value.endsWith('/'))
            field.value = text + field.value.substr(text.length).replace(/\/+$/, '');
        else
            field.value = text + field.value.substr(text.length);
    });
}

document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('[data-default-text]').forEach(field => {
        enforce_default_text(field, field.dataset.defaultText);
    });
});
