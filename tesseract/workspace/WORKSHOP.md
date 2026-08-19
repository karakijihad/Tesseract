# WORKSHOP — scratch work and task artifacts

Read this when a task starts producing artifacts: drafts, notes, scripts, experiments, intermediate files.

`workshop/` is that space. Writes there need no approval, the whole folder is gitignored, and paths are relative to your state root — `workshop/…`, never `tesseract/workshop/…`. Nothing goes at the repo root, in `workspace/`, or beside whatever code the topic happens to touch.

## Layout

```text
workshop/
├── INDEX.md                       # active tasks, newest first
├── YYYY-MM-DD/
│   └── <task-slug>/
│       ├── README.md              # required
│       └── ...
└── archive/
    └── YYYY-MM-DD/<task-slug>/
```

## Rules

**One folder per task.** Everything for it goes inside; nothing loose at the date level. A task spanning days stays under the day it started, with progress in its `README.md`.

**Short slugs.** Lowercase, hyphens, no spaces, underscores or dates — the parent folder carries the date. `css-orb-bug`, `mirror-hero-copy`, `s11-plan`.

**Every task opens with `README.md`**, written when you create the folder:

- **Goal** — one sentence: what "done" looks like.
- **Constraints** — deadlines, exclusions, dependencies.
- **Status** — `active`, `paused`, or `done`.
- **Notes** — working log, newest first.

**`INDEX.md` gets one line when a task starts**, linking the README with a one-line description. The line goes when the task is archived.

**Archive, never delete.** Move the folder to `archive/<same-date>/<same-slug>/` and drop its index line. The archive is the record of what you have worked on; there is no index for it, the structure is the index.

## What belongs somewhere else

- Durable operator or project facts → `memory_save`.
- Observations about yourself → `diary_append`.
- Changes to a workspace document → `propose_change`; `file_write` is denied on them.
- A new sub-agent → `agent_create`, and the operator approves it.

Workshop is scratch work *toward* a task, not a second home for anything that already has one.

## One worked example

*"Try three hero copy variants for the Mirror landing."*

1. `workshop/YYYY-MM-DD/mirror-hero-copy/README.md` — goal, status `active`.
2. `workshop/YYYY-MM-DD/mirror-hero-copy/variants.md` — the drafts.
3. One line in `workshop/INDEX.md`.

When they pick one: append the decision and `Status: done` to the README, move the folder into `archive/YYYY-MM-DD/mirror-hero-copy/`, remove the index line.
