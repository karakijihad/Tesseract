# `tesseract/config/` — The Single Source of Truth

This folder is the **only** place where AI infrastructure values live.
Model identifiers, provider URLs, timeouts, embedding settings,
permission policies, scheduler jobs, vault knobs — all of it is here,
in YAML. Source code reads from these files; it never carries its own
defaults for the values defined here.

> **The hard rule** (also in `CLAUDE.md`): no hardcoded model names, no
> hardcoded provider URLs, no hardcoded request timeouts, no module
> constants, no `get(..., "default")` fallbacks for infrastructure
> values. Missing keys raise loudly. Edit one file in `config/`, the
> whole system follows.

Acceptable code-side references:

- Adapter class names (`OpenAIAdapter`, `GeminiAdapter`, `AnthropicAdapter`)
- Adapter dispatch branches in `boot.py::build_adapter`
- Log/error messages naming providers
- Test fixtures under `tesseract/tests/`

---

## File index (live as of 2026-04-30)

| File               | Purpose                                                                                                                                                                                                                               | Loaded by                                                 | Hot-reloadable                                                             |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------- | -------------------------------------------------------------------------- |
| `providers.yaml`   | **Catalog.** Connection settings + named model entries (per-model pricing, context window, adapter hints, env-var refs). Tiers: `api`, `cli`, `local`. Plus `availability.max_consecutive_failures` and `cost_tracking` global knobs. | `tesseract/config/loader.py::load_config`                 | yes (Mirror config watcher → `boot.rebuild_adapters` + cost-ledger reload) |
| `roles.yaml`       | **Wiring.** Each role names a `primary` + `fallbacks` list of catalog references. Plus `embeddings.primary` (local-only) and the `voice:` subsystem (STT/TTS lanes pointing at catalog entries, per-provider settings).    | `tesseract/config/loader.py::load_config`                 | yes (same reloader as `providers.yaml`)                                    |
| `permissions.yaml` | `security_mode` (max/standard/headless) + per-tool AUTO/ASK/DENY defaults + `modes.overrides` + `path_overrides`.                                                                                                                     | `tesseract/permissions/policy.py::load_permission_policy` | yes (`policy.reload`); `/mode` toggles in-memory                           |
| `mirror.yaml`      | host/port, identity, CORS origins, `session.resume_policy`, `ui.show_config_reload_toasts`, terminal block (shell profiles, max tabs/panes).                                                                                          | `tesseract/mirror/server/config.py::load_server_config`   | partially (toast toggle hot; bind/CORS requires restart)                   |
| `terminal.yaml`    | Terminal-specific overrides for the Mirror PTY manager.                                                                                                                                                                               | `tesseract/mirror/server/pty_manager.py`                  | n/a                                                                        |
| `schedule.yaml`    | Persistent scheduler jobs (cron + alarm). Round-tripped via `config_loader.persist_job_update`.                                                                                                                                       | `tesseract/scheduler/engine.py::SchedulerEngine`          | yes (`engine.reload_jobs`)                                                 |
| `vault.yaml`       | Vault subsystem knobs — `ingest.max_extract_chars`, `lint.scale_split_threshold` / `stale_grace_days` / `contradiction_pair_limit`, `query.max_seed_slugs` / `max_expanded_slugs`.                                                    | `tesseract/brain/boot.py::load_vault_config`              | no (restart)                                                               |
| `conscience.yaml`  | Drift signal definitions (rule-based; no LLM).                                                                                                                                                                                        | `tesseract/conscience/config.py::load_drift_config`       | no                                                                         |
| `defaults/`        | Seed config copies for fresh installs.                                                                                                                                                                                                | bootstrap (Phase 17)                                      | n/a                                                                        |

The tool surface lives in `Docs/Logs/CAPABILITIES.md`, generated from
the live registry by `tesseract/scripts/generate_capability_matrix.py`
(CI gated via `--check`).

---

## `providers.yaml` — Catalog

Pure data. No role wiring. Three tiers, each a dict of provider blocks.
Every provider block carries connection metadata (`adapter`,
`timeout_seconds`, `max_retries`, optional `base_url`/`api_key_env`/
`command`) plus a `models:` dict where each entry names a single model
with its own pricing and adapter knobs.

```yaml
api:
  openai:
    base_url: "${OPENAI_BASE_URL:-https://api.openai.com/v1}"
    api_key_env: OPENAI_API_KEY
    adapter: openai
    timeout_seconds: 60
    max_retries: 3
    models:
      gpt54_mini:
        model: gpt-5.4-mini
        context_window: 400000
        max_output_tokens: 8192
        reasoning_effort: high
        use_responses_api: true
        cost_per_mtok_in: 0.75
        cost_per_mtok_out: 4.50
```

### Reference shape

References from `roles.yaml`, `agents/INDEX.md`, agent frontmatter, and
the loader API all use:

```
<tier>.<provider>.<model_id>
```

e.g. `api.openai.gpt54_mini`, `api.anthropic.opus_5`,
`cli.claude.opus_5`, `local.ollama.nomic_embed`. Anything else (a bare
role name like `chat_brain`) resolves through `roles.yaml::roles.<r>.primary`.

### `availability:`

Single key: `max_consecutive_failures`. Authoritative source for the
shared circuit-breaker threshold (observer breaker, scheduler breaker).

### `cost_tracking:`

Global knobs only — `enabled`, `warning_at_pct`, `log_file`. Per-model
unit pricing lives on the model entries (`cost_per_mtok_in/out`,
`cost_per_million_chars`, `cost_per_audio_hour`). Per-role caps and
per-voice-lane caps live in `roles.yaml`.

### Environment variable interpolation

`${VAR}` and `${VAR:-default}` in string values expand at load time via
`loader.resolve_env`. Used for `OLLAMA_BASE_URL` and similar.

---

## `roles.yaml` — Wiring

Each role names a `primary` ref and an optional ordered `fallbacks`
list. Code references roles by name; `boot.py` resolves them through
`loader.load_config()` to typed `ResolvedRef` tuples.

```yaml
roles:
  chat_brain:
    mode: active
    primary: api.openai.gpt56_luna
    fallbacks:
      - api.openai.gpt54_mini
      - api.xai.grok_43
    compact_threshold: 0.7
    keep_recent_turns: 10
    daily_budget_usd: 3.0
```

`fallbacks` is tried in order via `FallbackAdapter` when the prior
entry fails (network error, 5xx, rate limit). Mid-turn failover is
automatic.

### `chains:` — one chain, many roles

A role may name a shared chain instead of writing its own refs:

```yaml
chains:
  chain_1:
    - api.nim.gpt_oss_120b
    - api.google.gemini_36_flash
    - api.openai.gpt56_luna

roles:
  observer_agent:
    mode: active
    chain: chain_1
    daily_budget_usd: 1.0
```

First ref is the primary, the rest are the fallbacks in order — a chain
role is indistinguishable from a longhand one everywhere downstream. It
keeps its own `daily_budget_usd` and every other override; only the refs
are shared.

`chain` and `primary`/`fallbacks` on the same role is a `ConfigError`,
not a precedence rule. The alias exists so one edit moves every role on
the chain, and a role quietly overriding it would defeat that without
saying so.

Chains are numbered rather than named. Any descriptive name is a claim
that expires the moment the chain is repointed, which is the operation it
exists for.

Picking a model for a chain-backed role in Settings **detaches that
role**: its refs are written out in place, its `chain:` key is dropped,
and the other roles on the chain do not move. Repointing the whole chain
is an edit to `chains:`.

### Universality (the load-bearing rule)

**Any role can reference any catalog entry.** Roles do not own
providers — they pick from the catalog. Concrete consequences:

- Swap `chat_brain.primary` from `api.openai.gpt56_luna` to
  `api.anthropic.sonnet_5` (or `api.google.gemini_36_flash`, or
  `cli.claude.opus_5`) — that's the whole change. The Mirror config
  watcher detects the edit, calls `boot.rebuild_adapters`, the next
  turn uses the new primary.
- `observer_agent`, `agents_default`, `subagents_default` work the
  same way. Every cognition slot is provider-agnostic.
- Per-agent overrides (markdown frontmatter `model: api.openai.gpt54_nano`)
  bypass roles entirely and resolve straight to the catalog.

If you find yourself writing a code path that prefers one provider
over another — stop. Push that decision into `roles.yaml`.

### `embeddings:`

Single key: `primary`. Local-only by contract — per-turn retrieval
can't afford a LAN round-trip for 768-dim vectors. No fallback chain.

### `voice:`

Voice subsystem block: the `stt:` and `tts:` lanes. Each lane has the same
shape as a cognition role — `mode`, a `primary` catalog reference, an
ordered `fallbacks` list, and a `settings:` map keyed by catalog reference
carrying that provider's own knobs (`voice_id`, `timeout_seconds`,
`daily_budget_usd`, `synthesis_presets`). There is no global timbre or tone
prompt — a local voice IS its model file, and the cloud fallback names its
own `voice_id` under its `settings:` entry. Voice is decoupled from
MoodState — `set_mood` drives the orb only.

### Role-level overrides

Roles can layer overrides on top of the catalog entry: `reasoning_effort_override`,
`max_output_tokens_override`. The loader applies these to the resolved
`ProviderModel` so `boot.py::build_adapter` sees the final values.

---

## `permissions.yaml` — Tool Permission Policy

Central config for per-tool-call permissions. Read by
`tesseract/permissions/policy.py::load_permission_policy()` and
consulted on every tool call via `brain/tools.py::execute_tool`.

- `security_mode:` — `max` (writes/outbound/subprocess all ASK), `standard` (mature posture), `headless` (auto-allow what the security layer permits).
- `tools:` — default posture per tool name (`auto` / `ask` / `deny`). Covers every tool in the live registry.
- `modes:` — per-mode overrides on top of `tools:` defaults.
- `path_overrides:` — path-prefix-scoped rules.

**Security layer is separate and non-negotiable.** Hardcoded rules in
`tesseract/permissions/bash_security.py` (25 numbered checks — 20
absolute DENY for injection/escape/privesc/kernel-and-host attacks + 5
forced-ASK). No posture in this yaml can override a security DENY.

---

## How to swap the chat_brain primary

Edit `roles.yaml::roles.chat_brain.primary` to any catalog ref. The
Mirror config watcher detects the change, calls
`boot.rebuild_adapters`, and the next turn uses the new primary —
without restart. Failover behavior carries over: if the new primary
5xxs, `FallbackAdapter` walks the rest of the chain transparently.

---

## How to add a new provider

1. Add the provider block to `providers.yaml` under the right tier
   (`api`, `cli`, or `local`). Fill in `adapter`, `timeout_seconds`,
   `max_retries`, and one or more `models:` entries with pricing.
2. If the adapter doesn't exist yet, create
   `tesseract/kernel/adapters/<provider>.py` and add a dispatch branch
   in `boot.py::build_adapter` keyed off the `adapter:` string.
3. Reference the new model from any role that should reach it —
   `roles.yaml::roles.<r>.primary` or `fallbacks`. Same provider can
   appear under any number of roles. No code change needed.

## How to add a new model to an existing provider

1. Add a new entry under that provider's `models:` dict in
   `providers.yaml`. Mirror the existing entries' shape (the adapter
   already knows how to talk to the provider).
2. Reference it from wherever you want it — a role, an agent's
   frontmatter, a sub-agent default. Done.

---

## What does NOT belong in this folder

- API keys → `tesseract/.env` (`OPENAI_API_KEY`, `GOOGLE_API_KEY`, `ANTHROPIC_API_KEY`, etc.)
- Per-session state → `tesseract/sessions/` (anchored to `TESSERACT_HOME`)
- Memory data → `memory-store/` (anchored to `TESSERACT_HOME`)
- Logs → `tesseract/logs/` (anchored to `TESSERACT_HOME`)
- Generated docs (capability matrix) → `Docs/Logs/CAPABILITIES.md`
- Code

---

## When config is missing or malformed

- **Missing required key** → raise `RuntimeError` / `KeyError` with a message naming the file and key. No silent fallback.
- **File missing entirely** → `providers.yaml` or `roles.yaml` missing fails boot. `mirror.yaml` missing fails Mirror startup. `vault.yaml` missing fails `vault_*` tools.
- **Invalid YAML syntax** → fail fast at startup.
- **Broken catalog reference** (e.g. `api.openai.does_not_exist`) → `loader.load_config()` raises naming the role/lane and the dangling ref. Failures surface at boot, not at first use.

---

## See also

- `CLAUDE.md` — Model Configuration section
- `Docs/Logs/CODEMAP.md` — codebase map
- `Docs/Logs/CAPABILITIES.md` — generated tool surface
- `tesseract/config/loader.py` — single shared loader; `ConfigBundle` typed view
- `tesseract/brain/boot.py::build_adapter` — adapter dispatch
- `tesseract/brain/adapter_chain.py::FallbackAdapter` — per-turn failover
