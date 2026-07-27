"""Pydantic v2 schemas for the catalog YAMLs.

Loaded by the ``yaml_change_proposal`` apply path (MO-10-2) to validate
the proposed-after state before atomic write. Loader (``loader.py``) MAY
optionally validate at boot; today it does not — these schemas are
gating-only, not boot-blocking, so a schema bug can't brick the runtime.
"""

from tesseract.config._schemas.providers import ProvidersConfig
from tesseract.config._schemas.roles import RolesConfig

__all__ = ["ProvidersConfig", "RolesConfig"]
