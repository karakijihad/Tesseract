"""P15 audit-3 follow-up: SoulView markdown stack must remain XSS-safe.

`SoulView.tsx` switched from `<pre>` blocks to `react-markdown` + `remark-gfm`
+ `remark-breaks` (substituted from the plan's `marked + DOMPurify`). Out of
the box, `react-markdown` does NOT render embedded HTML — `<script>`/`<iframe>`
tags appear as escaped text. The risk vector is someone adding `rehype-raw`
(or a similar HTML-passthrough plugin) later, which would re-enable raw HTML.

This is a static-analysis regression: it asserts the SoulView source keeps
the safe configuration. The full e2e proof (mount Mirror, seed SOUL with a
script payload, assert no execution) is heavyweight; this check catches the
realistic regression vector at zero infra cost.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SOUL_VIEW = REPO_ROOT / "tesseract" / "mirror" / "src" / "views" / "SoulView.tsx"
PACKAGE_JSON = REPO_ROOT / "tesseract" / "mirror" / "package.json"


@pytest.fixture(scope="module")
def soul_view_source() -> str:
    assert SOUL_VIEW.exists(), f"SoulView.tsx not found at {SOUL_VIEW}"
    return SOUL_VIEW.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def package_json_text() -> str:
    return PACKAGE_JSON.read_text(encoding="utf-8")


def test_soul_view_uses_react_markdown(soul_view_source: str) -> None:
    """The chosen substitution must still be present — if SoulView regresses
    to raw `<pre>` or `dangerouslySetInnerHTML`, this test fails first."""
    assert "react-markdown" in soul_view_source
    assert "ReactMarkdown" in soul_view_source


def test_soul_view_does_not_pass_through_raw_html(soul_view_source: str) -> None:
    """The XSS-relevant escape hatches must stay absent."""
    assert "rehype-raw" not in soul_view_source, (
        "rehype-raw re-enables raw HTML rendering in react-markdown — "
        "would let <script>/<iframe> in SOUL.md execute. Remove it."
    )
    assert "dangerouslySetInnerHTML" not in soul_view_source, (
        "dangerouslySetInnerHTML on SoulView bypasses react-markdown's "
        "default sanitization. Render through ReactMarkdown instead."
    )
    # If someone adds `rehypePlugins={[...]}` with anything in it, force the
    # author to update this test consciously rather than slipping a raw-html
    # plugin past review unnoticed.
    assert "rehypePlugins" not in soul_view_source, (
        "SoulView should not pass rehypePlugins. If a safe plugin is being "
        "added, update this test with the explicit allowlist."
    )


def test_soul_view_does_not_depend_on_html_passthrough_plugins(package_json_text: str) -> None:
    """Belt-and-braces: the package itself must not pull in raw-html plugins.
    Catches the case where rehype-raw is added to package.json by a refactor
    even before SoulView starts using it."""
    assert "rehype-raw" not in package_json_text
    assert "rehype-html" not in package_json_text
