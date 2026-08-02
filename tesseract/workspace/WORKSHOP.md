# Workshop

Your working space is `tars-workshop/`. When the operator gives you a task that needs artifacts (drafts, notes, scripts, intermediate files, experiments), put them there — not at the repo root, not in `workspace/`, not wherever the topic happens to overlap with code.

Writes inside `tars-workshop/` are AUTO (no approval prompt) — see `config/permissions.yaml` path overrides. Write paths are relative to your state root: `tars-workshop/…`, never `tars-workshop/…`. The whole folder is gitignored; it's your space, not project code.

## Layout

```
tars-workshop/
├── INDEX.md                       # living list of active tasks (newest first)
├── YYYY-MM-DD/                    # per-day folders
│   └── <task-slug>/               # one folder per task
│       ├── README.md              # required: goal + status + notes
│       └── ...                    # any other artifacts
└── archive/
    └── YYYY-MM-DD/<task-slug>/    # completed or dormant tasks land here
```

## Rules

**One folder per task.** Put everything for a task inside its folder. Don't scatter files at the date level. If a task spans days, keep it under the day it started; touch its `README.md` with progress notes.

**Slug naming.** Short, lowercase, hyphens. Examples: `brave-to-tavily`, `css-orb-bug`, `s11-plan`. No spaces, no underscores, no dates (the parent folder already has the date).

**Every task starts with a `README.md`.** Four short sections — write them when you create the folder:

- _Goal_ — one sentence, what "done" looks like.
- _Constraints_ — deadlines, out-of-scope items, dependencies.
- _Status_ — `active` / `paused` / `done`. Update as you go.
- _Notes_ — free-form working log, append-only, newest first.

**Update `INDEX.md` when you start a task.** One line: `- [YYYY-MM-DD/task-slug](YYYY-MM-DD/task-slug/README.md) — one-line description of the task`. Remove the line when the task is archived.

**Archive when done.** Move the folder to `archive/<same-date>/<same-slug>/`. Don't delete — the archive is the record of what you've been working on. No index entry for archived tasks; the folder structure is the index.

**Don't put things here that belong elsewhere.**

- Memories → `memory_save` (not a notes file)
- Session logs → `Docs/Sessions/YYYY-MM-DD.md` (operator's canonical log)
- Soul updates → `propose_change` (workspace docs are DENY to `file_write`)
- Sub-agent drafts → `agent_create` tool, not a draft file

Workshop is for _scratch work toward a task_, not for things that have their own home.

## Examples

Operator asks: _"Draft a session plan for tomorrow."_

1. `file_write` → `tars-workshop/YYYY-MM-DD/s11-plan/README.md` with the four sections (goal = "outline next session's priorities", status = active, etc.).
2. `file_write` the plan itself → `tars-workshop/YYYY-MM-DD/s11-plan/plan.md`.
3. `file_write` → `tars-workshop/INDEX.md` appending one line linking the task.

Operator asks: _"Try three hero copy variants for the Mirror landing."_

1. `tars-workshop/YYYY-MM-DD/mirror-hero-copy/README.md` with goal.
2. `tars-workshop/YYYY-MM-DD/mirror-hero-copy/variants.md` with the three drafts.
3. Update `INDEX.md`.

On completion (operator says "thanks, we'll go with variant 2"):

- Append _Status: done_ + which variant was picked to `README.md`.
- Move the whole folder to `tars-workshop/archive/YYYY-MM-DD/mirror-hero-copy/`.
- Remove the `INDEX.md` line.
