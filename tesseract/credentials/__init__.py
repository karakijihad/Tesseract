"""The assistant's own accounts.

A credential the operator provisioned FOR the assistant — its GitHub, its
Gmail, its Cloudflare — encrypted at rest under the operator's Windows account
and never handed to the model. The model names one; the runtime spends it.
"""

from __future__ import annotations

from .models import Credential, Injection, mint_credential_id
from .paths import credentials_dir, store_path
from .redaction import RedactionUnavailable, redact, redact_payload
from .store import (
    CredentialNotSet,
    CredentialStore,
    CredentialStoreError,
    HostNotAllowed,
    UnknownCredentialError,
)

__all__ = [
    "Credential",
    "CredentialNotSet",
    "CredentialStore",
    "CredentialStoreError",
    "HostNotAllowed",
    "Injection",
    "RedactionUnavailable",
    "UnknownCredentialError",
    "credentials_dir",
    "mint_credential_id",
    "redact",
    "redact_payload",
    "store_path",
]
