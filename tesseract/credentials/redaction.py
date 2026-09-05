"""The one thing in the runtime that knows a stored credential by sight.

Everything else in ``credentials/`` exists to keep a value away from callers.
This module is the deliberate exception: it holds the plaintext of every
credential the store has, because finding a value in a string is the only way
to stop that string from being written down.

**Where it is called from, and why those places.** The wall is on the way IN,
at ``brain/tools.py::execute_tool`` — the single function every tool result in
this runtime comes out of. A service echoing a token back in a 401 body is the
leak the threat model actually names, and stopping it there means the value
never enters the conversation, so the session file, the provider request,
compaction, the observer and durable memory are clean without any of them
knowing this module exists.

There is a second wall, for the same reason: ``brain/spawns.py::register`` is
the one place a BACKGROUND spawn's coroutine enters, and what that coroutine
returns never passes ``execute_tool``. It is read by the durable completion
record, the operator-visible activity summary, and ``spawn_check``.

The four remaining call sites are backstops for a value that arrived some
other way, and they are the four boundaries ``_shared/threat-model.md``
enumerates: ``kernel/adapters/screened.py`` (the provider request, the only
one that fails closed), ``memory/store.py`` (durable memory, which refuses the
write), ``mirror/server/chat_content.py`` (the session file) and
``logsetup.py`` (the log tree). ``mirror/server/log_forwarder.py`` calls
``logsetup.scrub_log_text`` directly rather than this module, because it
builds its payload from a raw exception object no log filter can reach.

**Two classes of rule, and they answer different questions.** The stored
values are EXACT: whatever the operator saved, found by substring. The other is
a SHAPE that needs no store at all, because its subject is a secret by
construction and there is nothing for anyone to have registered: the userinfo
of a URL (see `redact_url_credentials`, and the note below it for the two
shapes deliberately left out). Both run on every string every entry point here
touches.

**What it still does not know.** Anything DERIVED from a stored value whose
shape is unremarkable: a token an API hands back, a cookie bought with a
stored password, a key generated before anyone saved it, or a stored value
base64'd or percent-encoded on its way into a payload. Those are different
strings, they match no shape, and none of them is caught. Closing that means
letting a caller register a derived value at the moment it receives one, which
is its own mechanism with its own failure modes.

The invariants below hold AT ONCE, and a change must be checked against all of
them rather than against whichever one prompted it:

1. never stale after a write this process made — the store calls
   :func:`invalidate` from its own save;
2. picks up a store edited by something else, within a second;
3. cheap enough for the log handler, which is the hottest caller — a cached
   substring scan, never a decrypt, and at most one ``stat`` per second;
4. silent — it never logs, because the log handler calls it and a record
   emitted from inside that call is an unbounded recursion;
5. no value ever appears in a repr, an exception message, or a return value
   other than the caller's own redacted text;
6. a runtime with no store never raises, so a fresh install and a test that
   never made one behave normally. It is no longer a NO-OP: the structural
   rule needs no store and runs anyway, which is the whole point of it, and
   an install with nothing saved is exactly where a token in a clone address
   still had to be caught;
7. a store that EXISTS and cannot be read raises, so a caller that must fail
   closed can.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any

#: The whole userinfo of a URL, whatever shape it is in.
#:
#: The rule below needs no store and no lookup, which is what separates it from
#: everything else here: a secret nobody registered is still a secret when it is
#: sitting in a URL. Composed text is where that matters. The active-project
#: block renders `git remote get-url origin` verbatim, so a repo cloned from
#: `https://<token>@host/repo` put that token in the system prompt on every
#: turn, and the store-backed redaction below never saw it because nobody had
#: typed it into the panel.
#:
#: Four properties, all at once, because a redactor that holds three of them
#: reads as done and is not:
#:
#:   1. `https://user:token@host` is masked. This is the shape git writes for
#:      a token remote and the only one the pattern used to know.
#:   2. `https://token@host` is masked too. A token needs no username, and
#:      requiring the colon let exactly that form through untouched.
#:   3. Nothing outside a URL's userinfo is touched. `://` must sit directly
#:      before the run, and the run may not cross `/` or whitespace, so an
#:      email address in a commit line and a plain `https://host/path` both
#:      survive.
#:   4. A username that is not a secret is masked anyway, so `ssh://git@host`
#:      prints as `ssh://***@host`. Nothing can tell a username from a token
#:      by looking, and losing a word of output is the cheaper mistake.
_URL_CREDENTIALS = re.compile(r"(?<=://)[^/@\s]+(?=@)")


def redact_url_credentials(text: str) -> str:
    """Blank the userinfo of every URL in ``text``.

    Pure: no store read, no I/O, cannot raise on ordinary input. That is what
    lets it run on the outbound path, where an exception is the request not
    happening.

    The two substring tests are not tidiness. This runs on every string of
    every payload, including the log handler's, and both characters have to be
    present for the pattern to fire at all — so a scan that cannot match is
    settled by `memchr` rather than by the regex engine. `re.sub` returns the
    ORIGINAL object when nothing matched, which is what keeps
    :func:`redact_payload`'s no-copy property intact through this rule.
    """
    if not text or "://" not in text or "@" not in text:
        return text
    return _URL_CREDENTIALS.sub("***", text)


# The rule above is the store-free half of what this module does, and it is
# ONE function rather than a registry of them. A tuple and a loop over a single
# entry is a shape that reads as extensible and is not: `redact_url_credentials`
# already has callers of its own outside this module, so it is reusable without
# the wrapper, and a second rule can be composed here the day one exists.
#
# **Two shapes considered and deliberately left out.**
#
# A URL's query string. The desktop shell strips whole query strings from
# anything it logs (`src-tauri/src/provision.rs::scrub_query_strings`) because a
# pre-signed asset URL carries its authorisation there and nothing downstream
# parses the string. Neither half holds here: a tool result returns URLs the
# model then uses, so removing every query would break search results and API
# responses to close a hole that surface already closes where it exists.
#
# SCP-style git remotes (`alice@host:group/repo`, no `://`). The userinfo there
# really is unmatched by the rule above. It is left out because the shape is
# indistinguishable from an ordinary sentence: `jane@example.com: hello` in a
# log line matches it exactly, and this runs on every string of every payload
# including the log handler's. What the rule would mask is also almost never a
# secret, because SSH carries no password in that position and the name is
# nearly always a fixed service account. Masking every email address in every
# log to blank the word `git` is the wrong trade.


# How long a stat reading is trusted before the store is checked again. The
# panel writes through `CredentialStore`, which invalidates directly, so this
# only has to catch an edit made outside this process — and the log handler
# asks on every record, where a syscall each is not free.
_RESTAT_INTERVAL_S = 1.0

# What a removed span reads as. Named, not blank: a person reading a transcript
# has to be able to tell that something was there and which account it belonged
# to, and a silently shortened line looks like the tool simply said less.
_MARKER = "[redacted: {credential_id}]"


class RedactionUnavailable(RuntimeError):
    """There is a store and its values could not be read.

    Raised only when a store file exists. A missing store means there is
    nothing to redact, which is not a failure.
    """


_LOCK = threading.Lock()
# (stat key, monotonic time it was read, values). `None` until first use.
_CACHE: tuple[object | None, float, tuple[tuple[str, str], ...]] | None = None
# Bumped by every invalidation. A reader captures it before it reads and
# refuses to publish a cache entry if it moved, which is what stops a store
# write that landed mid-read from being silently overwritten by values read
# before it. Clearing `_CACHE` alone does not do that: the reader's own write
# comes AFTER the clear and puts the stale values straight back.
_GENERATION = 0
# How many times a reader will start over when a write beats it to the lock.
# Bounded rather than `while True`: a pathological writer must not be able to
# hold a reader in a loop, and returning uncached values is a correct answer.
_MAX_REREADS = 3


def invalidate() -> None:
    """Forget the cached values. Called by the store after every write.

    Not left to the stat check: a write that only changes a value can land in
    the same filesystem tick at the same length, and the memory store already
    carries that exact defect on record.
    """
    global _CACHE, _GENERATION
    with _LOCK:
        _CACHE = None
        _GENERATION += 1


def _stat_key() -> object | None:
    """The store's identity for cache purposes, or ``None`` if there is no store.

    Only a MISSING file answers ``None``. Every other ``OSError`` raises, and
    the distinction is the whole point: a locked or unreadable store answering
    "there is no store" makes :func:`secret_values` return an empty set, which
    tells every boundary there is nothing to look for. The provider gate is
    built on that call raising in order to fail closed, so swallowing a
    ``PermissionError`` here turned the one fail-closed boundary in the feature
    into a silent fail-OPEN — and invariant 4 forbids logging from this module,
    so nothing anywhere would have said so.
    """
    from .paths import store_path

    try:
        stat = store_path().stat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RedactionUnavailable(
            f"the credential store exists but could not be read, so nothing "
            f"can be checked for credentials: {exc}"
        ) from exc
    return (stat.st_mtime_ns, stat.st_size)


def secret_values() -> tuple[tuple[str, str], ...]:
    """``(label, value)`` for every FIELD of every credential holding one.

    The label is what a person reads in place of the value
    (``models.py::secret_label``): the account's id alone for the primary
    field, ``id.field`` for the rest. Not a bare credential id, because an
    account holds a set of values now and a marker that named only the account
    would not say which of them was removed.

    Ordered longest value first, so a value that contains another is replaced
    before its own substring is, and the shorter one does not carve a hole in
    the middle of the longer one's marker.
    """
    global _CACHE
    values: tuple[tuple[str, str], ...] = ()
    for _ in range(_MAX_REREADS):
        now = time.monotonic()
        with _LOCK:
            generation = _GENERATION
            cached = _CACHE
            if cached is not None and now - cached[1] < _RESTAT_INTERVAL_S:
                return cached[2]

        key = _stat_key()
        if key is None:
            values = ()
        else:
            with _LOCK:
                cached = _CACHE
                if cached is not None and cached[0] == key:
                    # Reuse what was decrypted, but re-stamp it so the throttle
                    # measures from this reading of the file rather than the
                    # first one. `_CACHE` is None after an invalidation, so
                    # this branch cannot serve values a write has superseded.
                    _CACHE = (key, now, cached[2])
                    return cached[2]
            values = _read_values()

        with _LOCK:
            if _GENERATION == generation:
                _CACHE = (key, time.monotonic(), values)
                return values
        # A store write landed while this call was reading. Publishing now
        # would put pre-write values back into the cache and the write's own
        # invalidation would be lost, so start again against the new file.
    return values


def _read_values() -> tuple[tuple[str, str], ...]:
    """Decrypt every field of every stored value. Raises :class:`RedactionUnavailable`.

    The exception deliberately carries the store's own message and no value.
    `CredentialStore` raises with a credential id at most, which is what the
    panel shows the operator anyway.
    """
    from .store import CredentialStore, CredentialStoreError

    try:
        found = CredentialStore().iter_secrets()
    except CredentialStoreError as exc:
        raise RedactionUnavailable(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — anything unreadable fails the same way
        raise RedactionUnavailable(
            f"the credential store could not be read, so nothing can be "
            f"checked for credentials: {exc}"
        ) from exc
    found.sort(key=lambda pair: len(pair[1]), reverse=True)
    return tuple(found)


def _scrub(text: str, values: tuple[tuple[str, str], ...]) -> str:
    """One string, both classes of rule, in the one order there is.

    The whole sequence, so `redact` and the string branch of `_walk` cannot
    answer differently. They were two copies of these four lines, which is two
    places for a rule to be added to one of.

    **The shapes run first, and that costs something worth naming.** A stored
    value sitting inside a URL's userinfo is replaced by the structural pass
    before the loop below can put its named marker there, so the operator
    reads `***` rather than which account it was. Running the loop first would
    keep the marker, because `_MARKER` carries a space and the pattern cannot
    cross whitespace, but it would leave the REST of the userinfo standing:
    `https://alice:<stored>@host` would keep `alice`, and property 4 of the
    URL rule exists to say that a username nobody can tell from a token is
    masked anyway. Losing a word of provenance is the cheaper mistake than
    leaving a name on the wire, so the order stays. Do not "fix" it.
    """
    out = redact_url_credentials(text)
    for label, secret in values:
        if secret and secret in out:
            out = out.replace(secret, _MARKER.format(credential_id=label))
    return out


def redact(text: str) -> str:
    """``text`` with every secret removed: the shapes first, then the stored
    values."""
    if not text:
        return text
    return _scrub(text, secret_values())


def redact_payload(value: Any) -> Any:
    """:func:`redact` applied through dicts, lists and tuples.

    Keys are redacted as well as values. A payload that put a credential in a
    key would be a strange one, but the cost of covering it is a line and the
    cost of not covering it is a hole shaped like an assumption.

    **Returns the ORIGINAL object when nothing matched**, at every level, not
    only at the top. That is what stops this from being a deep copy of the
    prompt on every tool-loop iteration once the operator stores their first
    credential — the worst shape a performance regression can have, since it
    is invisible in every test and in every install that has not used the
    feature yet. It is also what lets a caller test identity to learn whether
    anything was removed.

    **It no longer returns early on an empty store**, because the structural
    rules do not need one and an install with no credentials saved is exactly
    where a token pasted into a clone address still had to be caught. The
    no-copy property is what makes that affordable: the walk still hands back
    the identical object at every level where nothing matched, and each rule
    rejects a string it cannot fire on with a substring test before the regex.
    """
    return _walk(value, secret_values())


def _walk(value: Any, values: tuple[tuple[str, str], ...]) -> Any:
    if isinstance(value, str):
        return _scrub(value, values)
    if isinstance(value, dict):
        # Built key by key rather than as a comprehension, because two keys
        # holding different credentials redact to two different markers but
        # two keys holding the SAME one collapse to a single key and the later
        # entry silently replaces the earlier. Losing a field is not a
        # redaction, so a collision is suffixed and both survive.
        walked: dict[Any, Any] = {}
        changed = False
        for key, item in value.items():
            new_key = _walk(key, values)
            new_item = _walk(item, values)
            if new_key is not key or new_item is not item:
                changed = True
            if new_key is not key and new_key in walked:
                suffix = 2
                while f"{new_key}#{suffix}" in walked:
                    suffix += 1
                new_key = f"{new_key}#{suffix}"
            walked[new_key] = new_item
        return walked if changed else value
    if isinstance(value, list):
        walked_list = [_walk(item, values) for item in value]
        if any(new is not old for new, old in zip(walked_list, value)):
            return walked_list
        return value
    if isinstance(value, tuple):
        walked_tuple = tuple(_walk(item, values) for item in value)
        if any(new is not old for new, old in zip(walked_tuple, value)):
            return walked_tuple
        return value
    return value


__all__ = [
    "RedactionUnavailable",
    "invalidate",
    "redact",
    "redact_payload",
    "redact_url_credentials",
    "secret_values",
]
