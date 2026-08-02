# MCP

MCP (Model Context Protocol) servers expose tool-like capabilities that live outside TESSERACT. Today, access is via direct HTTP wrapping — you don't talk to MCP servers through a full MCP client yet. Each capability is a standalone kernel tool with its own input schema and permission posture.

## Available MCP tools

| Tool              | Posture | Purpose                                             |
| ----------------- | ------- | --------------------------------------------------- |
| `context7_lookup` | AUTO    | Fetch current library docs by name + optional topic |

## When to use

### `context7_lookup`

Use any time you'd otherwise guess at a library's API shape from training knowledge. Any question about FastAPI, React, Next.js, pytest, httpx, Pydantic, or any other library where recency matters — especially new versions, deprecated patterns, or anything that changed since mid-2025.

<example>
<user>How do I define a lifespan handler in FastAPI?</user>
<tars_thought>API shape question — check current docs instead of guessing.</tars_thought>
<tool_call name="context7_lookup">{"library": "fastapi", "topic": "lifespan startup shutdown", "tokens": 5000}</tool_call>
</example>

<example>
<user>What's the correct way to mock httpx.AsyncClient in pytest?</user>
<tars_thought>Library API question — resolve then fetch docs.</tars_thought>
<tool_call name="context7_lookup">{"library": "httpx", "topic": "testing mock AsyncClient", "tokens": 4000}</tool_call>
</example>

## TESSERACT as an MCP server (inbound control plane)

TESSERACT also _is_ an MCP server — a spec-compliant Streamable-HTTP endpoint embedded in the Mirror backend, so a terminal-side agent (Claude Code CLI, Codex CLI, or any MCP client) can connect and drive TARS's governed verb surface. This is the "Unreal Engine" model: the editor (Mirror) embeds the server; the CLI in the terminal is the occupant driving it. Every verb call flows through the same permission → cost → audit → ActivityRecord stack as an in-process tool call — no MCP path bypasses a gate.

- **Endpoint:** `http://127.0.0.1:8000/mcp` — served on the Mirror backend's socket (the MCP server is embedded in the Mirror app; port = `mirror.yaml::server.port`). Local-only.
- **Auth:** bearer token from `$TESSERACT_MCP_SECRET` (the operator client). Default-deny: an unknown token is rejected.
- **Transport:** Streamable-HTTP — POST JSON-RPC (`initialize` → `tools/list` → `tools/call`). GET → 405 (no server-push stream in this build). DELETE ends the session.
- **Session:** `initialize` returns an `Mcp-Session-Id` = the `mcp_session` Activity record ("who's in the chair"); killable via `activity.cancel`.
- **Orientation:** `initialize` also returns server `instructions` — a live brief telling the connecting CLI what the memory and vault actually hold (current entry counts and date range), which verbs are worth reaching for, and that `app/` and `runtime/` are not to be written. It is computed per handshake, so it never goes stale.
- **Tools:** the verbs across `activity / memory / vault / lane / mission / schedule / surface / budget / agent` families (dots → underscores, e.g. `activity_list`). ASK-posture verbs return an `awaiting_operator` result — approve in Mirror, then re-issue.
- **`surface_open`** is the one to reach for when you want the operator to *see* something: a URL, a file, a folder, an app, or a search phrase. It resolves the target and renders it in the cockpit when it can, or opens it in the owning application when it can't, and the result says which. Use it instead of `surface_spawn` unless you are authoring a card from content you generated.

### How a CLI gets connected

Registration is automatic and nobody has to type it. Opening a terminal pane, starting a lane, or running a delegate provisions both CLIs at **user scope** — `~/.claude.json` (`mcpServers.tesseract`) and `~/.codex/config.toml` (`[mcp_servers.tesseract]`). Neither file records a secret; both reference the bearer token by environment-variable name.

That environment variable is the real gate. Panes and lanes are children of the Mirror backend and inherit it; a shell the operator opened from the desktop does not. So a `claude` or `codex` launched inside TESSERACT wakes up connected from whatever directory it is in, and the same binary launched outside sees no hub.

In a connected session the verbs appear as `mcp__tesseract__memory_search`, `mcp__tesseract__activity_list`, and so on.

Manual registration, if a client ever needs it:

```bash
export TESSERACT_MCP_SECRET=<the operator token from tesseract/.env>
claude mcp add --scope user --transport http tesseract http://127.0.0.1:8000/mcp \
  --header "Authorization: Bearer $TESSERACT_MCP_SECRET"
```

## Deferred

A full MCP _client_ (TESSERACT consuming external stdio/sse servers — Playwright, Gmail, Canva, Calendar; the ConnectorGateway direction) is a later initiative. Server-push over the MCP GET SSE stream (live `activity.watch` to MCP clients) is also deferred — clients poll `activity_list` today. Until then, each external MCP capability TARS needs is wrapped as a direct HTTP tool and listed above.
