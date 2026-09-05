# Workshop

This folder is the assistant's working space. Drafts, notes, experiments, and small applications live here, one folder per project under `projects/`.

The layout is a git repository's layout on purpose. Every project sits at a stable `projects/<name>/` path, which is what a host needs in order to deploy one, and what a stranger needs in order to find one. You are not obliged to use it that way. It is not a repository until you run `git init` here, and even then committing is local: work leaves your machine only once you add a remote and push, which is a separate decision you make on purpose.

Never put keys, tokens, or passwords in here, whether or not it is a repository. Credentials belong in your hosting provider's own settings.

The full convention, including what each project's `README.md` should say, is in `workspace/WORKSHOP.md`.
