# MCP

MCP (Model Context Protocol) servers expose tool-like capabilities that live outside TESSERACT. TESSERACT talks to them through a real MCP client (`mcp_client/`): one supervised connection per server listed in `config/mcp_servers.yaml`, each remote tool namespaced and registered with its own input schema and permission posture. Remote tools are `ask` and untrusted-source by default, and stay in the extended tier — they become model-visible only through `tool_search`.

## Available MCP tools

| Tool              | Posture | Purpose                                             |
| ----------------- | ------- | --------------------------------------------------- |
| `context7_lookup` | AUTO    | Fetch current library docs by name + optional topic |

## When to use

### `context7_lookup`

Use any time you'd otherwise guess at a library's API shape from training knowledge. Any question about FastAPI, React, Next.js, pytest, httpx, Pydantic, or any other library where recency matters — especially new versions, deprecated patterns, or anything that changed since mid-2025.

<example>
<user>How do I define a lifespan handler in FastAPI?</user>
<agent_thought>API shape question — check current docs instead of guessing.</agent_thought>
<tool_call name="context7_lookup">{"library": "fastapi", "topic": "lifespan startup shutdown", "tokens": 5000}</tool_call>
</example>

<example>
<user>What's the correct way to mock httpx.AsyncClient in pytest?</user>
<agent_thought>Library API question — resolve then fetch docs.</agent_thought>
<tool_call name="context7_lookup">{"library": "httpx", "topic": "testing mock AsyncClient", "tokens": 4000}</tool_call>
</example>

## TESSERACT as an MCP server (inbound control plane)

TESSERACT also _is_ an MCP server — a spec-compliant Streamable-HTTP endpoint embedded in the Mirror backend, so a terminal-side agent (Claude Code CLI, Codex CLI, or any MCP client) can connect and drive {{agent_name}}'s governed verb surface. This is the "Unreal Engine" model: the editor (Mirror) embeds the server; the CLI in the terminal is the occupant driving it. Every verb call flows through the same permission → cost → audit → ActivityRecord stack as an in-process tool call — no MCP path bypasses a gate.

- **Endpoint:** `http://127.0.0.1:8000/mcp` — served on the Mirror backend's socket (the MCP server is embedded in the Mirror app; port = `mirror.yaml::server.port`). Local-only.
- **Auth:** bearer token from `$TESSERACT_MCP_SECRET` (the operator client). Default-deny: an unknown token is rejected.
- **Transport:** Streamable-HTTP — POST JSON-RPC (`initialize` → `tools/list` → `tools/call`). GET with `Accept: text/event-stream` opens the server-push stream (below). DELETE ends the session.
- **Session:** `initialize` returns an `Mcp-Session-Id` = the `mcp_session` Activity record ("who's in the chair"); killable via `activity.cancel`.
- **Orientation:** `initialize` also returns server `instructions` — a live brief telling the connecting CLI what the memory and vault actually hold (current entry counts and date range), which verbs are worth reaching for, and that `app/` and `runtime/` are not to be written. It is computed per handshake, so it never goes stale.
- **Tools:** the verbs across `activity / memory / vault / workspace / diary / feedback / lane / schedule / surface / budget / agent` families (dots → underscores, e.g. `activity_list`). ASK-posture verbs return an `awaiting_operator` result — approve in Mirror, then re-issue.
- **One shared base.** The point of the surface is that work done in a CLI is not lost when that session ends. `workspace_post` / `workspace_reply` / `workspace_read` are the thread the operator reads, in both directions; `memory_recall` and `memory_get` read back what earlier sessions established; `memory_save` / `memory_update` / `memory_promote` / `memory_forget` curate rather than only accumulate; `diary_append` carries the narrative; `feedback_propose` records a lesson as a proposal that changes nothing by itself; `vault_ingest` files research. Whoever did the work — {{agent_name}}, a delegated CLI, or a terminal you opened yourself — the next turn sees it.
- **`workspace_ask`** asks the operator a question without blocking on it. It posts a card they answer in its comment thread; nothing pushes the answer back, so poll `workspace_read` with the event id you got. Do other work meanwhile — that is the point of it over stopping and waiting.
- **`lane_turn`** sends a task to another lane and returns its completed reply in one call, rather than `lane_send` followed by polling `lane_read`.
- **`surface_open`** is the one to reach for when you want the operator to *see* something: a URL, a file, a folder, an app, or a search phrase. It resolves the target and renders it in the cockpit when it can, or opens it in the owning application when it can't, and the result says which. Use it instead of `surface_spawn` unless you are authoring a card from content you generated.

### What is yours, and what is shared

Work belongs to the client that created it. A lane you open is owned by your configured principal (`lane-claude`, `lane-codex`, `terminal-manual`, …) and another client cannot read it, send to it, turn it, attach to it, interrupt it or close it. `activity_list` and the SSE stream show you your own records and nothing else. The check runs in the controller daemon, not in the Mirror, so it holds on the raw IPC path too — Mirror-side filtering would be usability, not a boundary.

Collaboration is deliberate rather than default: `lane_ensure` takes `shared_with`, naming the principals allowed in alongside you. The `operator` principal reaches everything on purpose — these are one operator's collaborating workers, and an operator locked out of a lane they can see in the cockpit is the worse failure.

**What this is not.** It stops accidental cross-client reach; it is not tenant isolation. Lanes have filesystem access, and the bearer token proves that a gateway is speaking rather than which principal it speaks for. Treat another client's work as none of your business, not as unreachable.

### How a CLI gets connected

Registration is automatic and nobody has to type it. Opening a terminal pane, starting a lane, or running a delegate provisions both CLIs at **user scope** — `~/.claude.json` (`mcpServers.tesseract`) and `~/.codex/config.toml` (`[mcp_servers.tesseract]`). Neither file records a secret; both reference the bearer token by environment-variable name.

That environment variable is the real gate. Panes and lanes are children of the Mirror backend and inherit it; a shell the operator opened from the desktop does not. So a `claude` or `codex` launched inside TESSERACT wakes up connected from whatever directory it is in, and the same binary launched outside sees no hub.

In a connected session the verbs appear as `mcp__tesseract__memory_search`, `mcp__tesseract__activity_list`, and so on.

Manual registration, if a client ever needs it:

```bash
export TESSERACT_MCP_SECRET=<the operator token — Settings → API keys mints one, or read it from .env>
claude mcp add --scope user --transport http tesseract http://127.0.0.1:8000/mcp \
  --header "Authorization: Bearer $TESSERACT_MCP_SECRET"
```

### Watching activity instead of polling

`activity_list` is a snapshot. Between two polls a lane can spawn, run and close without ever appearing in one — so a client that only polls is not merely late, it is blind to transitions. `activity.watch` is the subscription that fixes it, and it is not a tool: it is the GET SSE stream on the same endpoint.

```bash
curl -N http://127.0.0.1:8000/mcp \
  -H "Authorization: Bearer $TESSERACT_MCP_SECRET" \
  -H "Accept: text/event-stream" \
  -H "Mcp-Session-Id: <the id initialize returned>"
```

Each frame is a JSON-RPC notification, `notifications/tesseract/activity`, carrying the same record shape `activity_list` returns. You see only your own work and whatever was shared with you — the stream applies the same ownership filter as the snapshot.

Reconnecting with `Last-Event-ID: <the last id you saw>` replays exactly what you missed. If it cannot — the cursor is older than the retained window, or it came from a previous run of the backend — you get `notifications/tesseract/activity_gap` instead, telling you to re-hydrate with `activity_list`. A gap notice is never silent, and a partial history is never served as a complete one. The handshake advertises all of this under `capabilities.experimental["tesseract/activityStream"]`.
