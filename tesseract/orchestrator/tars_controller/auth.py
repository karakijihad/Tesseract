"""Controller IPC auth — UUID4 token, on-disk at `run/controller.token`.

The daemon mints a token at boot, the supervisor writes it via
`write_token` (chmod 0600 on POSIX), and each client reads it before
issuing the first IPC message.

Constant-time `verify_token` so a token-equality oracle can't be observed
through the network handshake.
"""

from __future__ import annotations

import hmac
import os
import stat
import uuid
from pathlib import Path

from .paths import token_file_path


def mint_token() -> str:
    return str(uuid.uuid4())


def write_token(token: str) -> Path:
    path = token_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(token, encoding="utf-8")
    if os.name == "posix":
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp, path)
    return path


def read_token() -> str | None:
    path = token_file_path()
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8").strip() or None


def verify_token(presented: str, expected: str) -> bool:
    if not presented or not expected:
        return False
    return hmac.compare_digest(presented, expected)


__all__ = ["mint_token", "read_token", "verify_token", "write_token"]
