"""Owner batch 1 round 3 — frontend `VALID_CATEGORIES` must include
every category the backend emits.

The HUD cost chips were stuck at empty because the backend sends
`cost_state` / `cost_delta` / `cost_warning` envelopes with
`category: "cost"`, but the frontend `isEnvelope` validator
(`tesseract/mirror/src/lib/envelope.ts`) was missing `'cost'` from
`VALID_CATEGORIES`. Every cost envelope was rejected at WS receive
with `[ws] non-envelope message received`, never reaching `_handleCost`.

Round 2 fixed the `state.role` AttributeError on the broadcast side, but
the frontend gate killed the data on receive — chips stayed `--empty`
even though `/api/cost/state` returned correct totals and the WS frame
arrived with the right payload. Verified live via Playwright 2026-04-29.

This test parses the TypeScript source for the `VALID_CATEGORIES` set
and asserts every category strung through `make_envelope` in the backend
is represented. A backend that emits a category not whitelisted on the
frontend is silently dropped — the same failure mode this regression
prevents.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
ENVELOPE_TS = REPO_ROOT / "tesseract" / "mirror" / "src" / "lib" / "envelope.ts"
ENVELOPE_PY = REPO_ROOT / "tesseract" / "mirror" / "server" / "envelope.py"


def _frontend_valid_categories() -> set[str]:
    src = ENVELOPE_TS.read_text(encoding="utf-8")
    match = re.search(
        r"VALID_CATEGORIES\s*=\s*new\s+Set<string>\(\[(?P<body>.*?)\]\)",
        src,
        flags=re.DOTALL,
    )
    assert match, "VALID_CATEGORIES set literal not found in envelope.ts"
    body = match.group("body")
    return set(re.findall(r"'([a-z_]+)'", body))


def _backend_emitted_categories() -> set[str]:
    """Return every category passed as the second positional arg of
    `make_envelope(...)` across the Mirror server. The signature is
    `make_envelope(type, category, session_id, data)` — we extract
    string-literal `category` arguments only (dynamic categories are
    out of scope for static analysis)."""
    found: set[str] = set()
    for path in (REPO_ROOT / "tesseract" / "mirror" / "server").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(
            r'make_envelope\(\s*"[^"]+"\s*,\s*"([a-z_]+)"',
            text,
        ):
            found.add(m.group(1))
    return found


def test_cost_in_frontend_valid_categories():
    """The actual fix: 'cost' must be in VALID_CATEGORIES."""
    assert "cost" in _frontend_valid_categories(), (
        "VALID_CATEGORIES in tesseract/mirror/src/lib/envelope.ts must include "
        "'cost' — backend emits cost_state/cost_delta/cost_warning under that "
        "category and isEnvelope() drops them silently otherwise (HUD chips "
        "stuck on empty state)."
    )


def test_every_backend_category_is_whitelisted():
    """Generic guardrail: any category the backend emits must be in
    the frontend allowlist. Catches the next 'cost'-style omission
    without having to enumerate categories by name here."""
    fe = _frontend_valid_categories()
    be = _backend_emitted_categories()
    missing = sorted(be - fe)
    assert not missing, (
        f"Backend emits categories not whitelisted on the frontend: {missing}. "
        f"Add them to VALID_CATEGORIES in tesseract/mirror/src/lib/envelope.ts "
        f"or isEnvelope() will drop those envelopes at WS receive."
    )
