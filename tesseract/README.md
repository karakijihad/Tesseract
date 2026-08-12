# `tesseract/` — the runtime

Everything the application is: the assistant loop, its memory, its tools and
the permission model that gates them, the scheduler that runs work in the
background, and the desktop interface that fronts all of it.

The assistant that runs inside this runtime has no name in the source. It is
named once, by the person installing it, and read from
`config/mirror.yaml::identity.name`. Nothing here should ever hardcode it.

If you are here to *use* TESSERACT rather than read it, the top-level
`README.md` is the one you want; `SETUP.md` covers running it from source and
`SECURITY.md` describes what the runtime will and will not do on your behalf.

## Layout

| Directory | What lives there |
| --- | --- |
| `brain/` | The turn loop — prompt assembly, streaming, tool scheduling, compaction, provider adapters and their failover chain. |
| `kernel/` | The tool registry. One module per tool, plus the adapters that talk to each model provider. Write-locked: tools are added deliberately, never by the assistant itself. |
| `permissions/` | The policy layer every tool call passes through — AUTO/ASK/DENY resolution, path scoping, and the shell-command security checks that no policy can relax. |
| `memory/` | Long-term memory: writing, decay, consolidation, retrieval. |
| `vault/` | The append-only research library and the wiki compiled from it. Separate from memory on purpose — research never contaminates recall. |
| `orchestrator/` | Self-directed work: the autonomy kernel and its agenda, background workers, delegated coding lanes, the terminal, and browser control. |
| `scheduler/` | Cron and alarm jobs, with durable state so a job survives a restart. |
| `conscience/` | Rule-based drift checks over what the assistant has been doing. No model in the loop. |
| `agents/` | Sub-agents, written as Markdown rather than code — each one a brief plus the model it should run on. |
| `mirror/` | The desktop app — an aiohttp backend, a React frontend, and the Tauri shell that installs and updates the whole thing. |
| `voice/` | Speech in and out. Local engines by default. |
| `integrations/` | Outside channels. |
| `mcp_client/` | Governed client for external MCP servers, allowlisted by config. |
| `capability/` | The launch-time check of what this machine has against what this version needs. |
| `janitor/` | Periodic cleanup — stale processes, scratch directories, old archives. |
| `config/` | Every infrastructure value, in YAML. See `config/README.md`. |
| `scripts/` | Entry points and maintenance commands, run as `python -m tesseract.scripts.<name>`. |
| `supervisor/` | The process that starts the backend and restarts it if it dies. |

## Where your data lives

Not here. The installed application tree is replaced wholesale by every
update, so nothing writable may sit inside it. Memory, the vault, sessions,
logs, configuration and downloaded models all resolve under `TESSERACT_HOME`,
outside this directory — which is also what makes the whole thing portable:
copy that one tree to another machine and the assistant comes with it.

`memory-store/`, `vault/`, `workshop/` and `workspace/` do appear here, but
only as the starter scaffold a fresh install copies out on first run.

## Running it

```bash
python -m tesseract.supervisor        # the app, with restart-on-crash
python -m tesseract.mirror.server     # the backend alone
```

`SETUP.md` at the top level covers dependencies, the `.env` file, and picking
which model the assistant thinks with.
