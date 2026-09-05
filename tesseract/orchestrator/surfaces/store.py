"""SurfaceStore — process-wide authority for canvas surfaces.

Shared singleton between the ``surface_*`` kernel tools (which mutate it)
and the Mirror REST/WS routes (which read it + relay operator events). Both
run in the same Mirror process, so a plain singleton — like
``background_event_bus`` — is the right substrate; no provider plumbing.

Each mutating verb does three things: update the in-memory map, persist the
view's ``surfaces`` array (merge-preserving the frontend's tldraw snapshot),
and publish a ``surface`` event so live operators re-render. Operator-origin
events (``apply_event``) persist but do NOT re-publish — the originating
client already moved the card; echoing it back is redundant.

Render reports (``record_render``) and the in-card event log (``card_events``)
are the state here that is deliberately NOT persisted: they describe what a
*client* currently has on screen, so writing them to the canvas-state file
would let a stale ``mounted`` outlive the browser that reported it and survive
a restart with nothing rendering at all. That is precisely the over-claim this
channel exists to close, so absence has to stay readable as absence.
"""

from __future__ import annotations

import logging
import uuid
from collections import deque
from typing import Any, Callable

from tesseract.orchestrator.surfaces.descriptor import (
    BoundSession,
    SurfaceDescriptor,
    SurfacePosition,
    SurfaceSize,
    utc_now_iso,
)
from tesseract.orchestrator.surfaces.events import publish_surface_event
from tesseract.orchestrator.surfaces.persistence import (
    canvas_state_dir,
    persist_surfaces,
    read_default_layout,
    read_view_blob,
    safe_view,
)

log = logging.getLogger(__name__)

# What a client may say about a card it is holding. `mounted` is the weakest
# of the four and is documented as such everywhere it is surfaced: it means a
# renderer mounted and reported no failure, NOT that the pixels are right.
# `degraded` is the P10 shape — it drew, and a known part of it cannot work.
RENDER_STATUSES = frozenset({"mounted", "degraded", "errored", "unmounted"})

# A renderer's reason is a caption, not a log line — it goes into a tool
# result the model reads, so it is bounded here rather than at the sink.
_DETAIL_CAP = 300
#: A card listing more verbs than this is not describing itself, it is
#: filling a listing the model has to read on every turn it looks.
_CONTROLS_CAP = 12
#: How many presses and edits one card keeps. A log, not a history: what the
#: assistant needs is what happened since it last looked, and a card someone
#: is clicking through would otherwise grow without end in a process that
#: never restarts.
_EVENT_LOG_CAP = 24
#: A control's name and the value typed into it, bounded. A card's markup is
#: not always the assistant's own writing, and this text lands in a tool
#: result the model reads: a page cannot be allowed to send a paragraph and
#: have it arrive as though the operator had pressed something called that.
_EVENT_TARGET_CAP = 80
_EVENT_VALUE_CAP = 200


def _bounded(value: Any) -> Any:
    """A logged value, small enough that a page cannot write an essay into
    the assistant's reading of what the operator did. Numbers and booleans
    pass through; anything else becomes bounded text."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:_EVENT_VALUE_CAP]


class SurfaceStore:
    def __init__(self) -> None:
        # view -> {surface_id -> descriptor}
        self._views: dict[str, dict[str, SurfaceDescriptor]] = {}
        self._hydrated: set[str] = set()
        # surface_id -> {status, detail, at}. In-memory by design (see module
        # docstring); a report never outlives the process that heard it.
        self._render: dict[str, dict[str, str]] = {}
        # surface_id -> the last few things the operator did IN a card, as
        # opposed to the things they did TO one. Same reasoning as the render
        # reports: it describes a live client, so it is never persisted.
        self._events: dict[str, deque[dict[str, Any]]] = {}
        # surface_id -> the chat that drew it. Provenance, not client state,
        # but still in memory only: a chat id names a conversation in THIS
        # process, and one restored from a canvas file would name a chat that
        # no longer exists. A card that outlives its chat is simply unowned.
        self._owner: dict[str, str] = {}
        # Called when the operator presses something inside a card whose owner
        # is known. Set by the Mirror at boot; None everywhere else, which is
        # what keeps this module free of any import of the server. A press is
        # recorded either way, so nothing is lost when nobody is listening.
        self.press_notifier: Callable[[str, str, dict[str, Any]], None] | None = None

    # -- hydration ---------------------------------------------------------

    def _ensure_view(self, view: str) -> dict[str, SurfaceDescriptor]:
        """Lazily load a view's surfaces from disk on first touch so cards
        survive a brain restart. When no operator file exists yet, seed the
        source-controlled baseline layout so a first visit looks
        familiar; seeding is in-memory only — the descriptors carry stable
        ids, so a re-seed on the next boot is idempotent, and the first
        operator interaction persists the layout (`apply_event` → `_persist`).
        A view whose file *exists* (even with an empty `surfaces`) is never
        re-seeded — the operator may have deliberately closed every card.

        An illegal view name gets a throwaway dict and is NOT registered.
        `create` refuses one outright, but `apply_event` and `list_for_view`
        take a view straight off a canvas event, and registering it would grow
        `_views`/`_hydrated` by one entry per distinct name for a view that can
        never load or persist."""
        if safe_view(view) is None:
            return {}
        if view in self._hydrated:
            return self._views.setdefault(view, {})
        self._hydrated.add(view)
        surfaces = self._views.setdefault(view, {})
        blob = read_view_blob(view)
        if blob is None:
            for raw in read_default_layout(view) or []:
                desc = self._coerce_default(view, raw)
                if desc is not None:
                    surfaces[desc.id] = desc
            return surfaces
        for raw in blob.get("surfaces", []) or []:
            try:
                desc = SurfaceDescriptor.model_validate(raw)
            except Exception as exc:  # noqa: BLE001 — skip a bad row, keep the rest
                log.warning("surface hydrate: dropping malformed descriptor: %s", exc)
                continue
            surfaces[desc.id] = desc
        return surfaces

    @staticmethod
    def _coerce_default(view: str, raw: dict[str, Any]) -> SurfaceDescriptor | None:
        """Validate one baseline-layout descriptor, stamping the view name and
        first-visit timestamps the source file omits."""
        now = utc_now_iso()
        data = {
            **raw,
            "view": view,
            "created_at_utc": raw.get("created_at_utc", now),
            "updated_at_utc": raw.get("updated_at_utc", now),
        }
        try:
            return SurfaceDescriptor.model_validate(data)
        except Exception as exc:  # noqa: BLE001 — skip a bad default, keep the rest
            log.warning("surface seed: dropping malformed default for %s: %s", view, exc)
            return None

    def _persist(self, view: str) -> None:
        surfaces = self._views.get(view, {})
        persist_surfaces(view, [d.model_dump(mode="json") for d in surfaces.values()])

    def _get(self, surface_id: str) -> tuple[str, SurfaceDescriptor] | None:
        for view in list(self._hydrated):
            desc = self._views.get(view, {}).get(surface_id)
            if desc is not None:
                return view, desc
        # Not in a hydrated view. After a brain restart the kernel tools
        # (surface_close/update/focus/…) may target a surface whose view was
        # never touched in this process — `create` hydrates a view, but a
        # mutate-only call would otherwise miss it. Hydrate every on-disk
        # view once and retry so a persisted surface is always reachable.
        for view in self._unhydrated_disk_views():
            self._ensure_view(view)
            desc = self._views.get(view, {}).get(surface_id)
            if desc is not None:
                return view, desc
        return None

    def _unhydrated_disk_views(self) -> list[str]:
        directory = canvas_state_dir()
        if not directory.exists():
            return []
        views: list[str] = []
        for path in directory.glob("*.json"):
            if path.name.endswith(".tmp.json"):
                continue
            view = path.stem
            if view not in self._hydrated:
                views.append(view)
        return views

    # -- queries -----------------------------------------------------------

    def list_for_view(self, view: str) -> list[dict[str, Any]]:
        surfaces = self._ensure_view(view)
        ordered = sorted(surfaces.values(), key=lambda d: d.z)
        return [d.model_dump(mode="json") for d in ordered]

    def get(self, surface_id: str) -> dict[str, Any] | None:
        found = self._get(surface_id)
        return found[1].model_dump(mode="json") if found else None

    # -- render reports (client → tool) ------------------------------------

    def record_render(
        self,
        surface_id: str,
        *,
        status: str,
        detail: str = "",
        controls: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Record what a client says it did with this card, and what it says
        the card can be asked to do.

        `controls` is the card answering "what can I be told?" in its own
        words. It is reported rather than derived because only the renderer
        knows: whether a framed page publishes a way in depends on the page,
        and a second copy of that judgement in Python would be one more thing
        to keep in step with the one that is actually true. An empty list is
        meaningful and different from absent: the card mounted and can be
        asked nothing.

        Returns the stored report, or None when the surface is unknown — a
        report for a card the store never had is dropped rather than kept,
        so `surface_list` cannot grow rows for surfaces that do not exist.
        Raises ValueError on an unknown status; the caller is a route that
        turns that into a 400.
        """
        if status not in RENDER_STATUSES:
            raise ValueError(
                f"unknown render status {status!r}: expected one of "
                f"{', '.join(sorted(RENDER_STATUSES))}"
            )
        if self._get(surface_id) is None:
            return None
        report: dict[str, Any] = {
            "status": status,
            "detail": detail[:_DETAIL_CAP],
            "at": utc_now_iso(),
        }
        if controls is not None:
            report["controls"] = [str(c) for c in controls][:_CONTROLS_CAP]
        self._render[surface_id] = report
        return report

    def owner_of(self, surface_id: str) -> str:
        """The chat that drew this card, or `""` for one nobody claims: a card
        from a previous run, or one the operator's own client created."""
        return self._owner.get(surface_id, "")

    def _notify_press(
        self, surface_id: str, event: str, entry: dict[str, Any], *, first: bool = False
    ) -> None:
        """Tell the owning conversation that a control was used.

        Only a press. An `edited` fires on every keystroke a field reports, and
        the assistant reads those when it looks; a press is the deliberate act,
        and it is the one worth interrupting for.

        Never raises. The press is already recorded by the time this runs, so a
        notifier that fails costs proactivity and nothing else, and the caller
        is a route serving a client that did its part.
        """
        if event != "clicked" or self.press_notifier is None:
            return
        owner = self._owner.get(surface_id, "")
        if not owner:
            # Said out loud, once per card, because the silent version of this
            # cost a live game: presses were recorded, everything returned 200,
            # and nothing woke, with no line anywhere saying why. A card is
            # unowned when it was drawn outside a conversation, or when the
            # conversation that drew it never learned its own id.
            if first:
                log.info(
                    "surface %s has no owning chat, so a press in it wakes "
                    "nobody; it is still readable through surface_control",
                    surface_id,
                )
            return
        try:
            self.press_notifier(surface_id, owner, entry)
        except Exception:  # noqa: BLE001 — a press is recorded whatever this does
            log.exception("surface press notifier failed for %s", surface_id)

    def card_events(self, surface_id: str) -> list[dict[str, Any]]:
        """What the operator did inside this card, oldest first.

        Empty is the answer for a card nobody has touched and for one whose
        page sends nothing, and the caller cannot tell those apart. That is
        the honest state of it: a page reports a press because the bridge in
        it does, and nothing here can know whether one happened unheard.
        """
        return list(self._events.get(surface_id, ()))

    def render_report(self, surface_id: str) -> dict[str, Any] | None:
        """The last report for this card, or None if no client ever said
        anything. None is meaningful: nothing is holding it on screen, or
        nothing has since this process started."""
        return self._render.get(surface_id)

    # -- verbs (tool → canvas) --------------------------------------------

    def create(
        self,
        *,
        type: str,
        view: str,
        props: dict[str, Any] | None = None,
        position: dict[str, float] | None = None,
        size: dict[str, float] | None = None,
        mode: str = "embedded",
        title: str | None = None,
        owner_chat: str = "",
    ) -> str:
        if safe_view(view) is None:
            # Refused here and not only at the sink. `write_view_blob` returns
            # without writing, and nothing on this path reads that: the card
            # would go into `self._views`, publish `surface_created` and hand
            # the caller an id, while persisting nothing and vanishing on the
            # next boot. `surface_create` is AUTO posture, so the caller is
            # usually the model — it has to be told the name is unusable, or it
            # cannot correct it.
            raise ValueError(
                f"invalid canvas view {view!r}: letters, digits, underscore and "
                f"dash only, up to 64 characters"
            )
        surfaces = self._ensure_view(view)
        now = utc_now_iso()
        next_z = (max((d.z for d in surfaces.values()), default=0)) + 1
        desc = SurfaceDescriptor(
            id=f"{type}-{view}-{uuid.uuid4().hex[:8]}",
            type=type,
            view=view,
            position=SurfacePosition(**(position or {"x": 80.0, "y": 80.0})),
            size=SurfaceSize(**(size or {"w": 640.0, "h": 460.0})),
            title=title,
            mode=mode,  # type: ignore[arg-type]
            z=next_z,
            props=dict(props or {}),
            created_at_utc=now,
            updated_at_utc=now,
        )
        # mode=external surfaces are OS-native (Tauri shell::open); the canvas
        # records the descriptor for audit/recall but never renders a card.
        if desc.mode != "external":
            surfaces[desc.id] = desc
            self._persist(view)
        if owner_chat:
            self._owner[desc.id] = owner_chat
        publish_surface_event(
            kind="surface_created", view=view, data=desc.model_dump(mode="json")
        )
        return desc.id

    def _mutate(self, surface_id: str, **changes: Any) -> tuple[str, SurfaceDescriptor] | None:
        found = self._get(surface_id)
        if found is None:
            return None
        view, desc = found
        updated = desc.model_copy(update={**changes, "updated_at_utc": utc_now_iso()})
        self._views[view][surface_id] = updated
        self._persist(view)
        return view, updated

    def update(
        self, surface_id: str, *, props: dict[str, Any] | None = None, title: str | None = None
    ) -> dict[str, Any] | None:
        found = self._get(surface_id)
        if found is None:
            return None
        _, current = found
        changes: dict[str, Any] = {}
        if props is not None:
            changes["props"] = {**current.props, **props}
        if title is not None:
            changes["title"] = title
        result = self._mutate(surface_id, **changes)
        if result is None:
            return None
        view, updated = result
        payload = updated.model_dump(mode="json")
        publish_surface_event(kind="surface_updated", view=view, data=payload)
        return payload

    def focus(self, surface_id: str) -> dict[str, Any] | None:
        found = self._get(surface_id)
        if found is None:
            return None
        view, _ = found
        top = max((d.z for d in self._views[view].values()), default=0) + 1
        result = self._mutate(surface_id, z=top)
        if result is None:
            return None
        _, updated = result
        publish_surface_event(
            kind="surface_focused",
            view=view,
            data={"surface_id": surface_id, "z": updated.z},
        )
        return updated.model_dump(mode="json")

    def _remove(self, surface_id: str) -> str | None:
        """Delete + persist a surface without publishing. Returns its view."""
        found = self._get(surface_id)
        if found is None:
            return None
        view, _ = found
        del self._views[view][surface_id]
        self._render.pop(surface_id, None)
        self._events.pop(surface_id, None)
        self._owner.pop(surface_id, None)
        self._persist(view)
        return view

    def close(self, surface_id: str) -> bool:
        view = self._remove(surface_id)
        if view is None:
            return False
        publish_surface_event(
            kind="surface_closed", view=view, data={"surface_id": surface_id}
        )
        return True

    def lock(self, surface_id: str, *, locked: bool) -> dict[str, Any] | None:
        result = self._mutate(surface_id, locked=locked)
        if result is None:
            return None
        view, updated = result
        publish_surface_event(
            kind="surface_locked",
            view=view,
            data={"surface_id": surface_id, "locked": locked},
        )
        return updated.model_dump(mode="json")

    def highlight(self, surface_id: str, *, persistent: bool = False) -> bool:
        found = self._get(surface_id)
        if found is None:
            return False
        view, _ = found
        # Highlight is a transient visual cue — published, not persisted,
        # unless persistent=true (which the renderer keeps lit until cleared).
        publish_surface_event(
            kind="surface_highlighted",
            view=view,
            data={"surface_id": surface_id, "persistent": persistent},
        )
        return True

    def bind_session(
        self, surface_id: str, *, session_kind: str, session_id: str
    ) -> dict[str, Any] | None:
        result = self._mutate(
            surface_id, bound_session=BoundSession(kind=session_kind, id=session_id)
        )
        if result is None:
            return None
        view, updated = result
        publish_surface_event(
            kind="surface_bound",
            view=view,
            data={
                "surface_id": surface_id,
                "session_kind": session_kind,
                "session_id": session_id,
            },
        )
        return updated.model_dump(mode="json")

    # -- operator events (canvas → tool) ----------------------------------

    def apply_event(
        self, *, view: str, surface_id: str, event: str, detail: dict[str, Any]
    ) -> bool:
        """Apply an operator-origin interaction (move / resize / close).
        Persists the new geometry; does not re-publish (the originating
        client already reflects it). ``view`` hydrates the view first so an
        event after a fresh Mirror boot still resolves the surface."""
        self._ensure_view(view)
        if event == "moved":
            pos = detail.get("position") or {}
            return self._mutate(surface_id, position=SurfacePosition(x=pos["x"], y=pos["y"])) is not None
        if event == "resized":
            sz = detail.get("size") or {}
            return self._mutate(surface_id, size=SurfaceSize(w=sz["w"], h=sz["h"])) is not None
        if event == "closed":
            # No re-publish: the operator's client already removed the card.
            return self._remove(surface_id) is not None
        if event in ("clicked", "edited"):
            # Kept rather than dropped, which is the whole of this half of the
            # bridge: a card the assistant authored can now tell it that a
            # button was pressed, and `surface_control read` is where it looks.
            # Still no state change and still no re-publish: the client that
            # sent it already knows.
            if self._get(surface_id) is None:
                return False
            log_ = self._events.setdefault(surface_id, deque(maxlen=_EVENT_LOG_CAP))
            entry = {
                "event": event,
                "target": str(detail.get("target") or "")[:_EVENT_TARGET_CAP],
                "value": _bounded(detail.get("value")),
                "at": utc_now_iso(),
            }
            log_.append(entry)
            self._notify_press(surface_id, event, entry, first=len(log_) == 1)
            return True
        # highlighted is observational — no state change.
        return self._get(surface_id) is not None


_store: SurfaceStore | None = None


def get_surface_store() -> SurfaceStore:
    """Return the process-wide surface store singleton."""
    global _store
    if _store is None:
        _store = SurfaceStore()
    return _store


def reset_surface_store() -> None:
    """Reset the singleton. Test-only helper."""
    global _store
    _store = None
