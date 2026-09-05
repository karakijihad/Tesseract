"""What the Kernel rail draws, answered by the runtime that is running.

The rail used to import a JSON file a build script had written, so an operator
who changed a seat in `roles.yaml` kept seeing the seat the app shipped with.
This is the read that replaces it: the live tool registry, the config tree the
operator owns, and no copy in between.

Anonymous-readable like the other dashboard feeds. It names providers and tool
groups, which the panel already shows on screen, and no credential.
"""

from __future__ import annotations

import logging
from pathlib import PurePath

import yaml
from aiohttp import web

from tesseract.cockpit.kernel_manifest import (
    ManifestError,
    build_manifest,
    config_paths,
    registry_tools,
)

log = logging.getLogger(__name__)


async def get_manifest(request: web.Request) -> web.Response:
    """GET /api/cockpit/kernel-manifest"""
    registry = request.app.get("tool_registry")
    if registry is None:
        # Two states wear this one shape. `app["tool_registry"]` is None while
        # the boot stage is still running AND for good after
        # `_try_build_tool_registry` catches a build failure, so "wait and it
        # will answer" is true in one and a lie in the other. The app records
        # which, and this says whichever it is rather than guessing the kind
        # one.
        failed = request.app.get("tool_registry_failed")
        if failed:
            return web.json_response(
                {
                    "error": (
                        "The tool registry could not be built, so the kernel "
                        "rail has nothing to draw. This will not fix itself. "
                        "The startup log says what failed."
                    ),
                    "retry": False,
                },
                status=503,
            )
        return web.json_response(
            {
                "error": (
                    "The tool registry is still starting, so the kernel rail "
                    "has nothing to draw yet. It will answer once boot finishes."
                ),
                "retry": True,
            },
            status=503,
        )
    try:
        shipped, custom = registry_tools(registry)
        manifest = build_manifest(shipped, custom)
    except ManifestError as exc:
        # A config fault the operator can fix, so it is said in full, with the
        # absolute path reduced to the file name: this feed is
        # anonymous-readable and the message carried the home directory.
        log.warning("kernel manifest: %s", exc)
        return web.json_response(
            {"error": _without_paths(str(exc)), "retry": False}, status=500
        )
    except (OSError, yaml.YAMLError) as exc:
        # The other fault they can fix, and the one they were told least
        # about: a roles.yaml or cockpit.yaml that will not parse answered
        # "the startup log says why" while the operator was looking straight
        # at the file they had just broken.
        log.warning("kernel manifest: config unreadable: %s", exc)
        return web.json_response(
            {
                "error": (
                    "A config file the kernel rail reads could not be read. "
                    f"{_without_paths(str(exc))}"
                ),
                "retry": False,
            },
            status=500,
        )
    except Exception:
        log.exception("kernel manifest failed to build")
        return web.json_response(
            {
                "error": "The kernel rail could not be built. The startup log says why.",
                "retry": False,
            },
            status=500,
        )
    return web.json_response(manifest)


def _without_paths(message: str) -> str:
    """The message, with the config files it names reduced to their names.

    The builder names the file it was reading, which is the useful half, and
    names it absolutely, which on this surface is the operator's home
    directory handed to whoever asked.

    **The paths are asked for, not guessed at.** This was a regex over
    path-shaped text, which cannot be written correctly: a colon is a legal
    character in a POSIX directory name, so any pattern that treats one as the
    end of a path splits there and leaves the segment before it behind. A
    directory named with a colon therefore survived into an anonymous 500
    body. There are exactly two files this message can name and the builder
    knows both, so they are replaced literally instead.
    """
    for path in config_paths():
        message = message.replace(str(path), PurePath(path).name)
    return message


def register(app: web.Application) -> None:
    app.router.add_get("/api/cockpit/kernel-manifest", get_manifest)
