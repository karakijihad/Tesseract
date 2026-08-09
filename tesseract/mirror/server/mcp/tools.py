"""Verb ⇄ MCP-tool bridge (mcp-control-plane P4).

The governed verb surface (``CALL_VERBS``) is exposed to real MCP clients as
the ``tools/list`` catalog and driven via ``tools/call``. Three concerns live
here so ``protocol.py`` stays transport-only:

* **Naming** — MCP tool names are ``[a-z0-9_]`` (dots are rejected by some
  clients), so ``activity.list`` ⇄ ``activity_list``. The reverse map is built
  explicitly (never string-munged back) to avoid ``a_b`` → ``a.b`` ambiguity.
* **Schema** — ``tools/list`` advertises a real ``inputSchema`` per tool:
  ``make_tool_verb`` handlers carry their Pydantic model (``mcp_input_model``);
  the direct-param verbs get a curated schema here.
* **Result translation** — the dispatcher returns ``(http_status, body)``; a
  ``tools/call`` response is a ``CallToolResult`` (``content`` + ``isError``).
  200 → text/ok; 202 (ASK pending) and 4xx/5xx → ``isError`` with the reason.
"""

from __future__ import annotations

import json
from typing import Any

import mcp.types as types

from tesseract.config.mcp import MCPClient
from tesseract.mirror.server.mcp.verbs import CALL_VERBS

# verb → tool name (dot → underscore) and the explicit inverse.
_VERB_TO_TOOL: dict[str, str] = {v: v.replace(".", "_") for v in CALL_VERBS}
_TOOL_TO_VERB: dict[str, str] = {t: v for v, t in _VERB_TO_TOOL.items()}
assert len(_TOOL_TO_VERB) == len(_VERB_TO_TOOL), "verb→tool name collision"

# One-line human descriptions (the model reads these to decide how to call).
# Posture is NOT restated here. These once carried "(ASK)" annotations, which
# went stale the moment `mcp.yaml` moved a verb — the yaml is the sole
# authority, and a description asserting an approval that no longer happens
# misleads the model about the control boundary rather than documenting it.
# Say what the verb does; let the dispatcher say what it costs.
_DESCRIPTIONS: dict[str, str] = {
    "activity.list": "List current TESSERACT activity records (lanes, sessions, delegates).",
    "activity.cancel": "Cancel a running activity by its activity_id (delegate/lane/mcp_session).",
    "memory.search": (
        "Search the operator's long-term memory — who they are, how they work, "
        "decisions they have already made, and the history of this project. "
        "Reach for it before asking the operator anything about past work or "
        "stated preferences; the answer is usually already here. Returns at "
        "most seven entries, so it is cheap to call. Set include_work_history "
        "false for promoted memory only, without session/workshop recall."
    ),
    "memory.save": "Save a new memory entry.",
    "memory.update": "Update an existing memory entry by id.",
    "memory.recall": (
        "Recall past sessions and workshop artifacts — what was worked on, "
        "when, and what came of it. Reach for it at the start of a task to "
        "find out whether this ground has been covered before."
    ),
    "memory.get": (
        "Read one memory entry whole by id. memory.search returns truncated "
        "previews; this returns the body."
    ),
    "memory.promote": "Promote a memory entry so it survives decay.",
    "memory.forget": "Delete a memory entry by id. Destructive.",
    "vault.search": (
        "BM25 + vector search over the operator's research library. Returns "
        "matching passages with their source paths — use when you want the "
        "source text itself."
    ),
    "vault.query": (
        "Synthesised answer over the research library's compiled wiki. Use for "
        "a natural-language question when you want a conclusion rather than "
        "passages to read."
    ),
    "vault.ingest": "Ingest a local document into the vault.",
    "lane.ensure": "Ensure a named terminal lane (claude/codex) exists; returns its lane id.",
    "lane.send": "Send input to a terminal lane.",
    "lane.turn": (
        "Send a task to a terminal lane and get the completed reply — send, "
        "await and read in one call, instead of lane.send followed by polling "
        "lane.read. Pass background false when the next step in this turn must "
        "consume the reply inline."
    ),
    "lane.read": "Read recent output from a terminal lane.",
    "lane.close": "Close a terminal lane.",
    "schedule.create": "Create a scheduled job.",
    "schedule.update": "Update a scheduled job.",
    "schedule.run": "Run a scheduled job now.",
    "schedule.remove": "Remove a scheduled job.",
    "schedule.list": (
        "List the registered scheduled jobs. Call this first — the other "
        "schedule verbs take a job id, and this is where ids come from."
    ),
    "surface.open": (
        "Open anything on the operator's machine: a URL, a file, a folder, "
        "an application, or a search phrase. Renders it in the cockpit when "
        "possible, otherwise opens it in the application that owns it."
    ),
    "surface.spawn": "Spawn a cockpit surface/panel.",
    "surface.update": "Update a cockpit surface.",
    "surface.focus": "Focus a cockpit surface.",
    "surface.close": "Close a cockpit surface.",
    "budget.status": "Report cost-ledger budget status (spend, caps, paused sources).",
    "budget.set_cap": "Set a runtime spend cap for a role.",
    "budget.pause_source": "Pause billing/execution for a cost source.",
    "agent.assign": "Assign a task to a fresh controller agent session.",
    "agent.status": "Read a controller agent session's status by session_id.",
    "agent.review": "Read a controller agent session's transcript tail by session_id.",
    "workspace.post": (
        "Post to the operator-visible workspace thread — the record of what "
        "was done and why. Use it so work performed here is legible later "
        "instead of only showing up as a changed file."
    ),
    "workspace.reply": "Reply to an existing workspace thread by its event id.",
    "workspace.read": (
        "Read the operator-visible workspace thread. With no event_id you get "
        "recent events and how many comments each carries; with an event_id "
        "you get that one thread including every comment. This is how you "
        "collect an answer to something you asked."
    ),
    "workspace.ask": (
        "Ask the operator a question without blocking — it posts a card they "
        "answer in its comment thread. Nothing pushes the answer to you: poll "
        "workspace.read with the event id you get back. Do other work in the "
        "meantime, and only use urgency 'high' when you genuinely cannot "
        "proceed without it."
    ),
    "diary.append": (
        "Append to the narrative diary the assistant reads back. Use for what happened "
        "and how it went, not for facts — those belong in memory.save."
    ),
    "feedback.propose": (
        "Record a lesson as a proposal for the operator to review. Changes "
        "nothing by itself; it is how a correction survives the session."
    ),
}

_STRING = {"type": "string"}
# Curated input schemas for the direct-param verbs (the make_tool_verb verbs
# supply their own via the attached Pydantic model).
_CURATED_SCHEMAS: dict[str, dict[str, Any]] = {
    "activity.list": {"type": "object", "properties": {}, "additionalProperties": False},
    "activity.cancel": {
        "type": "object",
        "properties": {"activity_id": _STRING},
        "required": ["activity_id"],
    },
    # memory.search / memory.save / memory.update / vault.ingest carry their
    # kernel input models via `mcp_input_model` (verbs/memory.py,
    # verbs/vault.py) — the former placeholders here were dead weight that
    # would shadow nothing but mislead readers, and a hand-copied field list
    # drifts from the model the verb actually validates against.
    "vault.search": {
        "type": "object",
        "properties": {"query": _STRING, "top_k": {"type": "integer"}, "category": _STRING},
        "required": ["query"],
    },
    "vault.query": {
        "type": "object",
        "properties": {"query": _STRING, "topic_filter": _STRING},
        "required": ["query"],
    },
    "budget.status": {"type": "object", "properties": {"role": _STRING}},
    "budget.set_cap": {
        "type": "object",
        "properties": {"role": _STRING, "cap_usd": {"type": "number"}},
        "required": ["role", "cap_usd"],
    },
    "budget.pause_source": {
        "type": "object", "properties": {"source": _STRING}, "required": ["source"],
    },
    "agent.status": {"type": "object", "properties": {"session_id": _STRING},
                     "required": ["session_id"]},
    "agent.review": {"type": "object", "properties": {"session_id": _STRING},
                     "required": ["session_id"]},
    "workspace.read": {
        "type": "object",
        "properties": {
            "event_id": _STRING,
            "kinds": {"type": "array", "items": _STRING},
            "status": _STRING,
            "limit": {"type": "integer"},
        },
    },
}

_PERMISSIVE = {"type": "object", "additionalProperties": True}


def verb_for_tool(tool_name: str) -> str | None:
    """Resolve an MCP tool name back to its verb (None if unknown)."""
    return _TOOL_TO_VERB.get(tool_name)


def _input_schema(verb: str) -> dict[str, Any]:
    handler = CALL_VERBS[verb]
    model = getattr(handler, "mcp_input_model", None)
    if model is not None:
        return model.model_json_schema()
    return _CURATED_SCHEMAS.get(verb, _PERMISSIVE)


def list_tools(dispatcher: Any, client: MCPClient) -> list[types.Tool]:
    """The ``tools/list`` catalog for one client — every allowlisted verb whose
    effective posture is not DENY (a client never sees tools it cannot call).

    Returns SDK ``types.Tool`` models (Phase 2b — spec-validated); ``protocol``
    wraps them in a ``ListToolsResult`` whose ``model_dump`` is the wire shape.
    """
    tools: list[types.Tool] = []
    for verb in CALL_VERBS:
        if dispatcher.resolve_posture(verb, client) == "deny":
            continue
        tools.append(
            types.Tool(
                name=_VERB_TO_TOOL[verb],
                description=_DESCRIPTIONS.get(verb, verb),
                inputSchema=_input_schema(verb),
            )
        )
    return tools


def _call_text(text: str, *, is_error: bool) -> dict[str, Any]:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)], isError=is_error
    ).model_dump(by_alias=True, exclude_none=True)


def call_result(status: int, body: dict[str, Any]) -> dict[str, Any]:
    """Translate a dispatcher ``(status, body)`` into an MCP ``CallToolResult``
    (SDK ``types.CallToolResult`` — Phase 2b; same wire shape as before)."""
    if status == 200:
        data = body.get("data")
        text = data if isinstance(data, str) else json.dumps(data, default=str)
        return _call_text(text, is_error=False)
    if status == 202:
        approval_id = body.get("approval_id", "")
        text = (
            f"awaiting_operator: approval_id={approval_id}. The operator must "
            "approve this in Mirror; this call does not auto-resume (bounded-hold "
            "model) — re-issue the call after approval."
        )
        return _call_text(text, is_error=True)
    text = f"[{body.get('code', status)}] {body.get('error', 'error')}"
    return _call_text(text, is_error=True)


__all__ = ["verb_for_tool", "list_tools", "call_result"]
