"""What a shipped agent card must satisfy before the runtime will boot.

The same move `check_tool_contract` makes for tools and the manifest makes for
scheduled work: a thing that runs on its own declares what it is, and the
declaration is checked at boot rather than discovered when it fires.

**The defect this exists for was live.** `vision.md` declared
`model_role: vision_agent`, and no such role is in `roles.yaml` — the nearest
is `channel_vision`. `build_chain_for_role` logs "role missing from roles.yaml"
at WARNING, returns an empty chain, and the card resolves to no model at all.
Nothing surfaced it; the card simply did nothing whenever it was reached.

**Boot asks "can this run", not "should this exist", and the split between
raising and reporting follows that line exactly.**

Raised: four ways a card resolves to nothing while looking fine. No
`model_role` at all; a role that is not in `roles.yaml`; a pin naming a catalog
entry `providers.yaml` does not hold; and a role naming a command line tool
subscription (`claude_cli`, `cli.codex.*`) rather than a model.

The last two are the quiet ones, because in both the declaration looks fine.
A CLI has no in-process adapter, so `invoke_agent` and `build_sub_session`
both refuse the card, and those two are the only ways a card ever becomes a
call: the card is unreachable and nothing said so. A pin the catalog does not
hold is quieter still, because it does not even fail — the ref resolves to no
adapter and the call runs on the PARENT model, which is the one thing pinning
a model exists to prevent.

Reported: a card with no `description`. It still runs, and whether it should
exist is a ruling. A boot that refuses over it would force that ruling out of
whoever is holding the traceback.

`agent_create` refuses everything on both lists, because a card that has not
been written yet costs nothing to redirect.

**A card carries no `use_when`/`not_when`, deliberately.** A tool carries the
pair because the model is handed every tool's schema on every turn and has to
choose between them, so the disambiguation lands on a surface it always reads.
Nothing renders a card that way: `invoke_agent`'s `name` is a free string, and
the roster a choosing model consults is `agents/INDEX.md`, whose columns are
name, role and description. Requiring the pair would add two strings per card
that no reader ever sees. What the card owes is a `description` good enough to
choose by, which is the field the roster already prints.

**Shipped only.** The operator's own cards under `home/agents/` are reported,
never refused — the same line drawn for `home/config/schedule.yaml`, for
the same reason: their file, their mistake to see and fix.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tesseract.config.loader import _REF_RE as _CATALOG_REF_RE

if TYPE_CHECKING:
    from tesseract.config.loader import ConfigBundle

logger = logging.getLogger(__name__)


class AgentContractError(RuntimeError):
    """A shipped card the runtime cannot honour."""


def is_provider_ref(model_role: str) -> bool:
    """True for a `<tier>.<provider>.<model_id>` entry out of the catalog.

    A card may name one instead of a role, and `invoke_agent` builds a
    single-entry adapter from it. Checked against the catalog's own pattern
    rather than a second copy of it, so the two cannot drift into disagreeing
    about what a ref looks like.
    """
    return bool(_CATALOG_REF_RE.match(model_role or ""))


def is_cli_role(model_role: str) -> bool:
    """True for every shape that names a CLI subscription rather than a model.

    Three shapes reached the tree at different times and all three mean the
    same thing: `claude_cli`, `cli_claude`, `cli.<provider>.<model>`.

    Nothing that runs a card can drive one. A CLI has no in-process adapter,
    so `invoke_agent` and `build_sub_session` both refuse, and those two are
    the only ways a card is ever turned into a call. A card wearing one is
    therefore unreachable by construction, which is why this is part of "can
    it run" rather than a preference.
    """
    return bool(model_role) and (
        model_role.startswith("cli_")
        or model_role.endswith("_cli")
        or model_role.startswith("cli.")
    )


@dataclass(frozen=True)
class CardGap:
    """One shipped-or-operator card and what it fails to declare.

    `blocking` separates the two questions the module docstring draws apart:
    True means the card cannot run at all, False means nothing can say what it
    is for. The roster prints both; boot raises only on the first, and only for
    a shipped card.
    """

    name: str
    origin: str
    field: str
    detail: str
    blocking: bool


def catalog_or_none(bundle: "ConfigBundle | None" = None) -> "ConfigBundle | None":
    """The loaded config, or `None` when it cannot be read.

    `None` is not "no roles and no models" — it is "no answer", and the
    difference matters: treating an unreadable `roles.yaml` as an empty set
    would report every card as naming a missing role. Config that will not
    load raises where config is loaded; this guard is about cards.
    """
    if bundle is not None:
        return bundle
    from tesseract.config.loader import ConfigError, load_config

    try:
        return load_config()
    except (ConfigError, OSError) as exc:
        logger.info("agent contract: config unreadable, skipping role and model checks (%s)", exc)
        return None


def knows_ref(model_role: str, bundle: "ConfigBundle | None" = None) -> bool | None:
    """Whether the catalog holds this `<tier>.<provider>.<model_id>` entry.

    `None` again means "no answer": no config to ask, or a bundle that cannot
    answer this question. Only a catalog that says NO makes a card blocking,
    because the cost of the two mistakes is not symmetric — refusing a card
    over an unreadable file would stop the runtime on a card that runs.

    **`ConfigError` is the only swallowed failure, and the narrowness is the
    point.** A catalog whose entry is malformed rather than missing raises
    plain `ValueError` out of the resolver, and catching that too would make a
    broken `providers.yaml` indistinguishable from a clean tree: every pin
    would read as unchecked and this guard would stop guarding with nothing
    said anywhere. Config that will not parse raises where config is read.

    **Shape alone was the whole check until this asked.** A ref is honoured by
    building a single-entry adapter, and `resolve_provider_ref_runtime`
    returns `None` for one the catalog does not hold, at which point
    `invoke_agent` falls back to the PARENT adapter. So a card pinned to a
    misspelled or since-deleted entry did not fail: it quietly ran on whatever
    model the caller was using, which is the one thing a pin exists to prevent.
    """
    catalog = catalog_or_none(bundle)
    if catalog is None:
        return None
    resolve = getattr(catalog, "resolve", None)
    if resolve is None:
        return None
    from tesseract.config.loader import ConfigError

    try:
        resolve(model_role)
    except ConfigError:
        return False
    return True


def inspect_cards(
    *,
    bundle: "ConfigBundle | None" = None,
    agents_dir: Path | None = None,
) -> list[CardGap]:
    """Every gap in every card the runtime can see, shipped and operator's.

    Never raises over a CARD: one that cannot be parsed is itself a gap,
    reported with the parse error as its detail. Config that will not parse is
    a different thing and is not caught here. The caller decides what to do
    about each — `check_shipped_cards` raises on the blocking ones, and
    `blocking_gaps` is what the roster and the Managed system room print so an
    operator's own broken card is visible somewhere they look.
    """
    from tesseract.agents.loader import list_agents_by_origin, load_agent

    catalog = catalog_or_none(bundle)
    gaps: list[CardGap] = []
    for origin, names in list_agents_by_origin(agents_dir=agents_dir).items():
        for name in names:
            try:
                card = load_agent(name, agents_dir=agents_dir)
            except Exception as exc:  # noqa: BLE001 — an unreadable card IS the gap
                gaps.append(unreadable_gap(name, origin, exc))
                continue
            gaps.extend(gaps_for_card(name, origin, card, bundle=catalog))
    return gaps


def unreadable_gap(name: str, origin: str, exc: BaseException) -> CardGap:
    """A card that will not parse, as the gap it is.

    Public because two surfaces load their own cards and hit this before any
    check can run, and a card that is skipped where it should be shown is how
    the roster came to list every card except the broken one.
    """
    return CardGap(
        name=name,
        origin=origin,
        field="card",
        detail=f"could not be read: {exc}",
        blocking=True,
    )


def gaps_for_card(
    name: str,
    origin: str,
    card: Any,
    *,
    bundle: "ConfigBundle | None" = None,
) -> list[CardGap]:
    """Every gap in one ALREADY-LOADED card. No file is opened here.

    Split out because the readers of this answer have the card in their hands
    already: the roster and the Managed system room each walk every card to
    render it, and calling `inspect_cards` beside that loop parsed all of them
    a second time on a polled request path. The checks live here, once, and
    whoever holds a card asks about the card they hold.

    `bundle` is the loaded config, or None for "no answer", which is what
    `catalog_or_none` returns for config that cannot be read. A caller with many
    cards resolves it once and passes it in.
    """
    catalog = bundle
    roles = set(catalog.roles) if catalog is not None else None
    gaps: list[CardGap] = []
    role = (getattr(card, "model_role", "") or "").strip()
    if not role:
        gaps.append(CardGap(
            name=name,
            origin=origin,
            field="model_role",
            detail="declares no model_role",
            blocking=True,
        ))
    elif is_cli_role(role):
        gaps.append(CardGap(
            name=name,
            origin=origin,
            field="model_role",
            detail=(
                f"names {role!r}, which is a command line tool "
                "subscription rather than a model. Nothing that runs a "
                "card can drive one, so this card can never be "
                "invoked. Point it at a role that names a model, or "
                "delete the card and hand the work to delegate_coder "
                "or delegate_auditor."
            ),
            blocking=True,
        ))
    elif is_provider_ref(role):
        # A catalog entry, not a role. `invoke_agent` builds a single-entry
        # adapter from it, so the roles listing has nothing to say about it
        # and checking it against one would report a card that runs perfectly
        # well as broken. It is checked against the CATALOG instead, which is
        # the list that can answer: a ref nothing holds resolves to no adapter
        # and the call falls through to the parent model.
        if catalog is not None and knows_ref(role, catalog) is False:
            gaps.append(CardGap(
                name=name,
                origin=origin,
                field="model_role",
                detail=(
                    f"pins {role!r}, which providers.yaml does not "
                    "hold. A pin that cannot be resolved does not "
                    "fail: the card runs on whichever model called "
                    "it. Name an entry the catalog carries, or a "
                    "role from roles.yaml."
                ),
                blocking=True,
            ))
    elif roles is not None and role not in roles:
        gaps.append(CardGap(
            name=name,
            origin=origin,
            field="model_role",
            detail=(
                f"names role {role!r}, which is not in roles.yaml. "
                f"Known roles: {', '.join(sorted(roles)) or '(none)'}"
            ),
            blocking=True,
        ))
    if not (getattr(card, "description", "") or "").strip():
        gaps.append(CardGap(
            name=name,
            origin=origin,
            field="description",
            detail="has no description, so nothing can say what it is for",
            blocking=False,
        ))
    return gaps


def blocking_gaps(
    *,
    bundle: "ConfigBundle | None" = None,
    agents_dir: Path | None = None,
) -> dict[str, CardGap]:
    """Per card name, the gap that stops it running, for a surface to show.

    **Written because "reported" was not true.** A shipped card that cannot
    run stops the boot, and an operator's own card is reported instead — which
    was one `logger.warning` from a module the Mirror's log forwarder does not
    carry, and no surface read `inspect_cards` at all. So the card sat in the
    Managed system room looking like every healthy card beside it, and the one
    sentence saying otherwise went to a log file.

    One gap per name: the first blocking one, because a card that cannot run
    for two reasons is still a card that cannot run, and a row shows a
    sentence rather than a list.

    For a caller that has NOT already loaded the cards. One that has — the
    roster, the Managed system room — calls `gaps_for_card` on what it holds
    instead, and parses each card once.
    """
    found: dict[str, CardGap] = {}
    for gap in inspect_cards(bundle=bundle, agents_dir=agents_dir):
        if gap.blocking and gap.name not in found:
            found[gap.name] = gap
    return found


def check_shipped_cards(
    *,
    bundle: "ConfigBundle | None" = None,
    agents_dir: Path | None = None,
) -> None:
    """Raise if a SHIPPED card cannot run. Log everything else.

    Called once from `build_tool_registry`, beside the tool contract, because
    that is the assembly point every entry into the runtime goes through.
    """
    gaps = inspect_cards(bundle=bundle, agents_dir=agents_dir)
    blocking = [g for g in gaps if g.blocking and g.origin == "system"]
    if blocking:
        raise AgentContractError(
            "shipped agent cards the runtime cannot honour:\n  - "
            + "\n  - ".join(f"{g.name}: {g.detail}" for g in blocking)
        )
    for gap in gaps:
        if gap.blocking:
            # An operator card, since the shipped ones raised above.
            logger.warning("agent card %s: %s", gap.name, gap.detail)
        else:
            logger.info("agent card %s: %s", gap.name, gap.detail)


__all__ = [
    "AgentContractError",
    "CardGap",
    "check_shipped_cards",
    "blocking_gaps",
    "gaps_for_card",
    "inspect_cards",
    "unreadable_gap",
    "knows_ref",
]
