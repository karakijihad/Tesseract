"""Asking a channel how it reads a `Message`.

An adapter that can do better than plain sentences says so with a
`render_message` method. One that cannot, or one that raises trying, gets
`render_plain`, which is the default rather than a fallback: a channel is
expected to be reachable before it is expected to be pretty.
"""

from __future__ import annotations

from typing import Any

from tesseract.orchestrator.autonomy.message import Message, render_plain



def render_for(adapter: Any, message: Message) -> str:
    """The adapter's own reading of `message`, or plain sentences.

    Asked for by `getattr` rather than declared on `ChannelAdapter`, for the
    same reason `send_text` is: the protocol is `runtime_checkable` and
    `register_channel` refuses a member it cannot find, so declaring this would
    make "cannot render prettily" a boot failure instead of a channel that
    reads plainly.
    """
    render = getattr(adapter, "render_message", None)
    if render is None:
        return render_plain(message)
    try:
        return render(message)
    except Exception:  # noqa: BLE001 — a channel that cannot render still speaks
        import logging

        logging.getLogger(__name__).exception(
            "outbound: %s could not render a message; sending it plainly",
            getattr(adapter, "name", "?"),
        )
        return render_plain(message)


__all__ = ["render_for"]
