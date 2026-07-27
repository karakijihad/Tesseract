# TESSERACT

The runtime — memory, permissions, tool registry, session store, adapters. **TARS** is the operator-facing assistant persona that runs inside it.

See the top-level `CLAUDE.md` for the authoritative operating notes, and `Docs/` for session logs, plan folders, and the architecture map.

## Future — standalone repo

This directory is slated to become its own clean repository once the current tars-reboot + Mirror revival stabilise. Target state:

- **Repo name:** `tesseract` (runtime) or `tars` (operator persona) — TBD by operator.
- **Scope of the new repo:** everything currently under `tesseract/` (which now includes `tesseract/scripts/` for REPL + helper entry points), the `Docs/` folder we consider keepable, and the `.env.example` template. No `Research/`, no historical plan folders that don't apply, no audit archives.
- **Bootstrap story:** a `tesseract/scripts/bootstrap.{ps1,sh}` that pins Python + pnpm versions, installs deps, copies a default `config/` tree, creates an empty `memory-store/` and `vault/`, and emits a ready-to-run `.env`. Operator clones, runs bootstrap, launches the supervisor (`python -m tesseract.supervisor`) or Mirror.
- **Portability:** all runtime state (memory, vault, sessions, logs) remains under `tesseract/` so the repo is copy-and-go — matches the existing architecture principle #6.

**Do not create the new repo yet.** We finish Phase 4–15 of the Mirror revival first, stabilise the system prompt (memory-save behaviour, path resolution, session resume + compact), then extract. Until then, this README is the pointer and the rest of the playbook lives in the parent repo's `CLAUDE.md` + `Docs/`.
