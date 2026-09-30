function delete_repo_row(control) {

    const engine = control.dataset.deleteRepo;
    const radio = document.getElementById('radio-' + engine);

    if (!engine || (radio && radio.checked))
        return;

    control.closest('.row').remove();

    const deleted_repos_input = document.getElementById('deleted-repos');
    const deleted_repos = JSON.parse(deleted_repos_input.value);

    deleted_repos.push(engine);
    deleted_repos_input.value = JSON.stringify(deleted_repos);
}

document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('[data-delete-repo]').forEach(control => {
        control.addEventListener('click', () => delete_repo_row(control));
    });
});
