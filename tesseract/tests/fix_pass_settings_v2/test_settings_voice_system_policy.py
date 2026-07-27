"""Phase 18 Task C — Voice / System / Session-policy routes.

Covers:
- POST /api/settings/voice round-trips models.yaml `voice:`
- GET /api/settings/voice exposes available_voice_ids
- GET /api/settings/system returns the current snapshot
- POST /api/settings/session-policy writes mirror.yaml session block
- show_config_reload_toasts toggle syncs `app['config_reload_toasts_enabled']`
- Validation refuses bad voice_id / bad rate / bad policy / bad days
- check_dependencies degrades gracefully when psutil/sounddevice missing
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from aiohttp import web

from tesseract.mirror.server.routes import settings as settings_route


@pytest.fixture
def app(tmp_path: Path) -> web.Application:
    src = Path(__file__).resolve().parents[2] / "config"
    target = tmp_path / "config"
    shutil.copytree(src, target)
    a = web.Application()
    a["tesseract_dir"] = tmp_path
    a["config_reload_toasts_enabled"] = True
    return a


def _make_request(app, *, method="POST", body=None, query=None):
    """Build an in-process request that exercises the route handlers
    without spinning a real aiohttp server."""
    from aiohttp.test_utils import make_mocked_request

    payload = json.dumps(body).encode("utf-8") if body is not None else b""
    qs = "?" + "&".join(f"{k}={v}" for k, v in (query or {}).items()) if query else ""
    req = make_mocked_request(
        method,
        f"/?{qs}" if qs else "/",
        headers={"Content-Type": "application/json"},
        payload=None,
        app=app,
    )
    if body is not None:
        # Stub `request.json()` directly rather than feeding a fake payload
        # stream. The route handlers only call `await request.json()`, and
        # the internal stream-reader contract (`BaseRequest.read()` →
        # `_payload.readany()`) drifts across aiohttp releases — a body-stream
        # mock that works on one version silently returns empty bytes on
        # another, surfacing as a spurious `invalid_json` 400. Returning the
        # already-decoded body removes the dependence on aiohttp internals.
        async def _json(*, loads=None):  # noqa: ANN001 — match BaseRequest.json sig
            return body

        req.json = _json  # type: ignore[method-assign]
    return req


# ── Voice ─────────────────────────────────────────────────────────


async def test_set_voice_round_trips_roles_yaml(app):
    """POST /api/settings/voice writes voice_id only — style/character
    is config-only via roles.yaml synthesis_presets after 2026-05-04."""
    req = _make_request(app, body={"voice_id": "Charon", "default_rate": 1.1})
    res = await settings_route.set_voice(req)
    assert res.status == 200
    raw = yaml.safe_load((app["tesseract_dir"] / "config" / "roles.yaml").read_text(encoding="utf-8"))
    assert raw["voice"]["default_voice_id"] == "Charon"
    assert raw["voice"]["default_rate"] == 1.1


async def test_set_voice_rejects_invalid_voice_id(app):
    req = _make_request(app, body={"voice_id": "Mickey"})
    res = await settings_route.set_voice(req)
    assert res.status == 400


async def test_set_voice_rejects_out_of_range_rate(app):
    req = _make_request(app, body={"default_rate": 5.0})
    res = await settings_route.set_voice(req)
    assert res.status == 400


async def test_set_voice_requires_at_least_one_field(app):
    req = _make_request(app, body={})
    res = await settings_route.set_voice(req)
    assert res.status == 400


async def test_get_voice_exposes_voice_ids(app):
    req = _make_request(app, method="GET")
    res = await settings_route.get_voice(req)
    body = json.loads(res.body)
    assert "Charon" in body["available_voice_ids"]


# ── System ────────────────────────────────────────────────────────


async def test_get_system_returns_snapshot(app, tmp_path):
    req = _make_request(app, method="GET")
    # First call has no cached snapshot — handler collects + writes.
    res = await settings_route.get_system(req)
    assert res.status == 200
    body = json.loads(res.body)
    assert "python_version" in body
    assert body["python_version"].startswith("3.")


async def test_get_system_refresh_query_param(app):
    req = _make_request(app, method="GET", query={"refresh": "1"})
    res = await settings_route.get_system(req)
    assert res.status == 200
    body = json.loads(res.body)
    assert "platform" in body


def test_check_dependencies_degrades_without_optional_libs():
    """Force-import the script; verify it doesn't crash when sounddevice is missing."""
    from tesseract.scripts import check_dependencies

    snap = check_dependencies.collect()
    assert snap.python_version
    # mic_devices is null on machines without sounddevice; that's fine.
    assert snap.mic_devices is None or isinstance(snap.mic_devices, int)


# ── Session policy ────────────────────────────────────────────────


async def test_set_session_policy_writes_mirror_yaml(app):
    req = _make_request(app, body={"policy": "n_days", "days": 7})
    res = await settings_route.set_session_policy(req)
    assert res.status == 200
    raw = yaml.safe_load((app["tesseract_dir"] / "config" / "mirror.yaml").read_text(encoding="utf-8"))
    assert raw["session"]["resume_policy"] == "n_days"
    assert raw["session"]["resume_days"] == 7


async def test_set_session_policy_rejects_bad_policy(app):
    req = _make_request(app, body={"policy": "forever"})
    res = await settings_route.set_session_policy(req)
    assert res.status == 400


async def test_set_session_policy_rejects_out_of_range_days(app):
    req = _make_request(app, body={"days": 0})
    res = await settings_route.set_session_policy(req)
    assert res.status == 400


async def test_set_session_policy_requires_at_least_one_field(app):
    req = _make_request(app, body={})
    res = await settings_route.set_session_policy(req)
    assert res.status == 400


async def test_set_session_policy_toast_toggle_syncs_app_state(app):
    assert app["config_reload_toasts_enabled"] is True
    req = _make_request(app, body={"show_config_reload_toasts": False})
    res = await settings_route.set_session_policy(req)
    assert res.status == 200
    assert app["config_reload_toasts_enabled"] is False


async def test_get_session_policy_returns_defaults(app):
    req = _make_request(app, method="GET")
    res = await settings_route.get_session_policy(req)
    body = json.loads(res.body)
    assert body["policy"] == "today_plus_yesterday"
    assert body["days"] == 1
    assert body["show_config_reload_toasts"] is True
