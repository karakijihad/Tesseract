# WORKSHOP: where work in progress lives

Read this when a task starts producing artifacts: drafts, notes, scripts, experiments, a small app, anything with files.

`workshop/` is that space. Writes there need no approval, and paths are relative to your state root: `workshop/…`, never `tesseract/workshop/…`. Nothing goes at the repo root, in `workspace/`, or beside whatever code the topic happens to touch.

The layout is shaped so the folder can become a git repository if the operator wants one. Each project sits at a stable path a host can point at. Nobody is obliged to use it that way, and it is not one until they run `git init` here. Even then a commit is local and publishes nothing; work leaves the machine only once they add a remote and push, which is a separate decision.

## Layout

```text
workshop/
├── README.md
├── INDEX.md                   # active projects, newest first
├── projects/
│   └── <project-slug>/
│       ├── README.md          # required
│       └── ...
├── archive/
│   └── <project-slug>/
└── audits/
    └── YYYY-MM-DD/            # audit runs, which are a time series, not projects
```

## Rules

**One folder per project, at a stable path.** Everything for it goes inside; nothing loose under `projects/`. The path never carries a date, because a deployment points at `projects/<slug>/` and a folder that moves breaks it. When the work started is in the README and in git history.

**Short slugs.** Lowercase, hyphens, no spaces, underscores or dates. `css-orb-bug`, `mirror-hero-copy`, `weather-card`.

**Every project opens with `README.md`**, written when you create the folder:

- **Goal.** One sentence: what "done" looks like.
- **Constraints.** Deadlines, exclusions, dependencies.
- **Status.** `active`, `paused`, or `done`.
- **Notes.** Working log, newest first.

That file is also what a stranger reads first if the repository is ever shared, so write it in plain sentences.

**`INDEX.md` gets one line when a project starts**, linking the README with a one-line description. The line goes when the project is archived.

**Archive, never delete.** Move the folder to `archive/<same-slug>/` and drop its index line. The archive is the record of what you have worked on; there is no index for it, the structure is the index.

**Anything secret stays out.** No keys, tokens, passwords or credential files, in any project, at any time. This is a tree that can acquire a remote later, and a secret committed once is a secret to rotate. Keys belong in the hosting provider's own settings.

## What belongs somewhere else

- Durable operator or project facts → `memory_save`.
- Observations about yourself → `diary_append`.
- Changes to a workspace document → `propose_change`; `file_write` is denied on them.
- A new sub-agent → `agent_create`, and the operator approves it.
- A codebase you work in regularly → `project_link` or `project_new`, which register it and record how it verifies. Workshop is for work you are producing, not for repositories you visit.
- A new callable tool → `tools/`. A workshop project is a program someone runs; a file in `tools/` is a class the runtime imports and hands you as a tool you can call by name. That write asks for approval, because it is what makes the file executable.

## One worked example

*"Try three hero copy variants for the Mirror landing."*

1. `workshop/projects/mirror-hero-copy/README.md`, with the goal and status `active`.
2. `workshop/projects/mirror-hero-copy/variants.md`, holding the drafts.
3. One line in `workshop/INDEX.md`.

When they pick one: append the decision and `Status: done` to the README, move the folder into `archive/mirror-hero-copy/`, remove the index line.
