"""Provider-credential regex shapes — single source of truth shared by the
PTY secret scrubber (`orchestrator/terminal/end_of_turn.py::scrub_secrets`)
and the production-tree secret scanner
(`tests/distributable_app/test_no_generic_pii_in_production_tree.py`,
`scripts/audit_release_tree.py`).

Only discrete, low-false-positive provider-PREFIXED token shapes live here —
each is both safe to redact on sight in a live PTY stream and safe to
exact-match against an allowlist in a static tree scan. `scrub_secrets`'s
other passes (Bearer headers, key/token/password assignments, generic
high-entropy runs) have no exact-value meaning and can't be individually
allowlisted, so they stay local to `end_of_turn.py` — folding them in here
would flood the tree scanners with false positives on ordinary hashes/base64
blobs.

If you add a pattern here for one consumer, check whether the others need it
too — this module is the reason they no longer drift independently.
"""

from __future__ import annotations

import re

CREDENTIAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),               # OpenAI / Anthropic (sk-ant-...) style
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),         # GitHub fine-grained PAT
    re.compile(r"\bgh[oprsu]_[A-Za-z0-9]{20,}"),           # GitHub PAT family (classic/oauth/server/user)
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),         # Slack bot/user/app tokens
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                    # AWS access key id
    re.compile(r"\bAIza[A-Za-z0-9_-]{35}\b"),               # Google API key
    re.compile(r"\btvly-[A-Za-z0-9_-]{10,}"),               # Tavily
    re.compile(r"\bgsk_[A-Za-z0-9]{20,}"),                  # Groq
    # Telegram bot token. No `\b` on the left: the Bot API carries it in the
    # URL PATH (`/bot<TOKEN>/getUpdates`), where the preceding character is a
    # letter and a word boundary never fires — the one place it actually
    # appears in a log line. `(?<!\d)` still anchors the digit count, and the
    # `:AA` + 30-char tail is what keeps this off ordinary text.
    re.compile(r"(?<!\d)\d{6,10}:AA[A-Za-z0-9_-]{30,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),      # PEM private key header
)
