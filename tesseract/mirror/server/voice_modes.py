"""The operator's mic modes, in one place.

`voice_io` validates the incoming `voice_mode_set`, `tts` decides whether
to synthesize, and `session_model` stores the value — three modules that
must agree on the same four strings. A mode added to one and missed by
another is a mode that dispatches turns nobody can hear, or speaks in a
mode the operator picked for silence.
"""

from __future__ import annotations

#: Every mode the HUD pill can cycle to.
#:
#: - ``transcribe`` — speech fills the chat input; nothing dispatches.
#: - ``command``    — speech dispatches a turn; the reply is text only.
#: - ``speak``      — speech dispatches a turn; the reply is spoken.
#: - ``terminal``   — speech is typed into the focused Terminal pane.
VOICE_MODES: tuple[str, ...] = ("transcribe", "command", "speak", "terminal")


#: Modes in which the assistant does not speak. `speak` is the only one that does.
SILENT_VOICE_MODES = frozenset({"transcribe", "command", "terminal"})


#: Where a transcript goes once the server has resolved the mic mode. This
#: is what rides `voice_final`, not the raw mode: the frontend must not
#: re-derive the routing, or a fifth mode added to one side and missed by
#: the other sends speech somewhere neither half intended.
#:
#: `chat` and `input` are the two backend contracts (dispatch a turn vs
#: hand the text back); `terminal` shares `input`'s contract and differs
#: only in which surface receives it, which is why it cannot collapse
#: into it.
VOICE_DESTINATIONS: tuple[str, ...] = ("chat", "input", "terminal")

_DESTINATION_BY_MODE = {
    "transcribe": "input",
    "terminal": "terminal",
    "command": "chat",
    "speak": "chat",
}


#: The default a missing or unrecognised mode falls back to. `transcribe`
#: because the one thing a voice system must never do on bad state is
#: speak unbidden — and it is what `ServerSession.voice_mode` itself
#: defaults to, so the two answers agree.
DEFAULT_VOICE_MODE = "transcribe"


def normalize_voice_mode(mode: object) -> str:
    """The single answer to "what mode is this session in".

    Four call sites used to substitute `speak` for a missing value while
    the session dataclass defaulted to `transcribe` — two different
    answers to the same question, and the wrong one is the audible one.
    """
    if not isinstance(mode, str):
        return DEFAULT_VOICE_MODE
    cleaned = mode.strip().lower()
    return cleaned if cleaned in VOICE_MODES else DEFAULT_VOICE_MODE


def destination_for(mode: str) -> str:
    """Resolve a mic mode to its wire-level routing target.

    An unknown mode resolves to `input`: handing the operator their own
    words back is the only outcome that is wrong in no dangerous way — it
    neither speaks unbidden nor types into a shell.
    """
    return _DESTINATION_BY_MODE.get((mode or "").strip().lower(), "input")


#: Modes whose transcript is handed to the operator rather than dispatched
#: as a turn. Never wake-word gated — the operator reads every word before
#: anything acts on it, so a gate would only be in the way.
#:
#: Derived, not written out again. It held the same fact as the destination
#: map, and once `_handle_voice_commit` began deriving its dispatch decision
#: from `destination_for` this became a second encoding with no production
#: reader — the kind that goes stale silently because only a test still
#: consults it.
LOCAL_VOICE_MODES = frozenset(m for m in VOICE_MODES if destination_for(m) != "chat")
