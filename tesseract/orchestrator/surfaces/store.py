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
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

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


class SurfaceStore:
    def __init__(self) -> None:
        # view -> {surface_id -> descriptor}
        self._views: dict[str, dict[str, SurfaceDescriptor]] = {}
        self._hydrated: set[str] = set()

    # -- hydration ---------------------------------------------------------

    def _ensure_view(self, view: str) -> dict[str, SurfaceDescriptor]:
        """Lazily load a view's surfaces from disk on first touch so cards
        survive a brain restart. When no operator file exists yet, seed the
        source-controlled baseline layout (Y-3) so a first visit looks
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
        # clicked / edited / highlighted are observational — no state change.
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
