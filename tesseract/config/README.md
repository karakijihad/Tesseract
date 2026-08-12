# `config/` — the single source of truth

Every infrastructure value TESSERACT runs on lives in this folder, in YAML:
which model serves which job, connection settings and prices, timeouts,
permission postures, scheduled jobs, subsystem knobs. Source code reads these
files. It never carries its own copy of a value defined here.

> **The rule:** no hardcoded model names, no hardcoded provider URLs, no
> hardcoded request timeouts, no `get(..., "default")` fallback for an
> infrastructure value. A missing key raises loudly at startup rather than
> being quietly substituted, because a silent default is a setting you cannot
> see and cannot change. Edit one file here and the whole system follows.

What code is still allowed to name directly: adapter class names, the dispatch
branch that maps an `adapter:` string to one of them, and error messages that
mention a provider by name.

Most files below open with a comment block explaining their own keys. That
comment is the detailed documentation; this page is the map.

## The files

| File | What it decides |
| --- | --- |
| `providers.yaml` | **The catalog.** Every model TESSERACT can reach, grouped by tier (`api`, `cli`, `local`), with connection settings, context windows and per-token prices. Plus `services:` for keyed web services, and the shared circuit-breaker threshold. |
| `roles.yaml` | **The wiring.** Which catalog entry serves each role — the chat brain, the observer, background agents, the two voice lanes, embeddings. Also per-role budgets and overrides. |
| `permissions.yaml` | Per-tool AUTO / ASK / DENY, the security mode, and path-scoped overrides. |
| `mirror.yaml` | Where the backend binds, the assistant's name and identity, session resume policy, terminal settings. |
| `identity.yaml` | Anchor facts about this instance, re-read on every prompt build. |
| `schedule.yaml` | The persistent job registry — cron and alarm entries, written back in place when a job is toggled from the UI. |
| `agenda.yaml` | How self-directed proposals are scored and capped. Deterministic; no model involved. |
| `agenda-mappers.yaml` | How each autonomy signal turns into a proposal, and which sources are switched on. |
| `autonomy-watchlist.yaml` | Sources watched for ecosystem awareness. Empty until you add your own. |
| `conscience.yaml` | Thresholds for the rule-based drift checks. |
| `memory.yaml` | Memory subsystem knobs — recall, decay, consolidation. |
| `vault.yaml` | Vault knobs — ingest limits, lint thresholds, query breadth. |
| `channels.yaml` | Per-channel adapter settings for outside messaging. |
| `mcp.yaml` | The MCP server TESSERACT exposes about itself, on localhost. |
| `mcp_servers.yaml` | The allowlist of external MCP servers it may connect out to. Curated by you; never extended by the assistant. |
| `open_verb.yaml` | How `open` resolves what it is given — a path, a URL, an application. |
| `cockpit.yaml` | Cockpit UI configuration and the verification gate for delegated work. |
| `hardware.yaml` | What this machine should be given, decided from what it turns out to have. Speech models especially. |
| `janitor.yaml` | Which orphaned processes may be reaped, and when. |
| `runtime.yaml` | Runtime concurrency and size limits. |
| `terminal.yaml` | Shell profiles and pane/tab limits for the built-in terminal. |
| `tokenjuice.yaml` | Compression applied to tool output before the model sees it. |

## Reference shape

A model is named the same way everywhere — in `roles.yaml`, in an agent's
frontmatter, and in the loader API:

```
<tier>.<provider>.<model_id>
```

For example `api.openai.gpt54_mini`, `api.anthropic.opus_5`,
`cli.claude.opus_5`, `local.ollama.nomic_embed`. A bare role name such as
`chat_brain` resolves through `roles.yaml` instead.

Environment variables interpolate in string values as `${VAR}` or
`${VAR:-fallback}`, expanded when the file is loaded.

## Roles are slots; providers fill them

**Any role can reference any catalog entry.** Roles do not own providers — they
pick from the catalog. Repointing the chat brain from one provider to another
is a single line in `roles.yaml`; the config watcher notices the edit, rebuilds
the adapters, and the next turn uses the new model without a restart. Failover
carries over: if the primary fails, the fallbacks are tried in order,
mid-turn.

If you find yourself writing code that prefers one provider over another, stop.
That decision belongs in `roles.yaml`.

Several roles can share one chain from the `chains:` block instead of each
listing its own refs, so one edit moves all of them. A role may use a `chain:`
or its own `primary`/`fallbacks`, never both — that is an error rather than a
precedence rule, because a role quietly overriding a shared chain would defeat
the reason to have one.

## Adding a provider

1. Add a block under the right tier in `providers.yaml` with its connection
   settings and at least one entry under `models:`.
2. If no adapter exists for it yet, write one under `kernel/adapters/` and add
   a dispatch branch keyed off the `adapter:` string.
3. Reference the new entry from any role that should use it. No further code
   change.

Adding a *model* to a provider that already works is step 1 and step 3 only.

## What does not belong here

- API keys — those go in `.env`.
- Anything that changes as the system runs: memory, sessions, logs, downloaded
  models. All of that resolves under `TESSERACT_HOME`, outside the code tree.
- Code.

## When a file is missing or malformed

By design, loudly. A missing required key raises with the file and key named.
`providers.yaml` or `roles.yaml` missing fails boot outright. Invalid YAML
fails at startup rather than at first use, and a reference to a catalog entry
that does not exist is reported at load time with both the role and the
dangling ref named.
