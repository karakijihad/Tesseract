"""logsetup — file pipeline for mirror-backend / tars-controller logs.

Every test monkeypatches TESSERACT_HOME to tmp_path BEFORE attaching the
handler (project hard rule: tests must never write tesseract/logs/**).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from tesseract.logsetup import attach_file_logging, load_logging_config

_YAML_OK = """
logging:
  dir: logs
  max_bytes: 1048576
  backup_count: 2
  file_min_levels:
    aiohttp.access: WARNING
    httpx: WARNING
"""


@pytest.fixture()
def config_path(tmp_path: Path) -> Path:
    p = tmp_path / "mirror.yaml"
    p.write_text(_YAML_OK, encoding="utf-8")
    return p


@pytest.fixture()
def isolated_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """TESSERACT_HOME → tmp_path and restore the root logger's handlers.

    Production order is basicConfig(level=INFO) then attach — mirror the
    INFO root level here or no INFO record ever reaches the handler."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    root = logging.getLogger()
    before = list(root.handlers)
    prev_level = root.level
    root.setLevel(logging.INFO)
    yield root
    root.setLevel(prev_level)
    for h in root.handlers:
        if h not in before:
            root.removeHandler(h)
            h.close()


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_attach_writes_process_file_under_home(
    tmp_path: Path, config_path: Path, isolated_root: logging.Logger
) -> None:
    path = attach_file_logging("mirror-backend", config_path=config_path)
    assert path == tmp_path / "logs" / "mirror-backend.log"
    logging.getLogger("tesseract.something").info("hello file")
    assert "hello file" in _read(path)


def test_noise_floor_drops_info_keeps_warning(
    tmp_path: Path, config_path: Path, isolated_root: logging.Logger
) -> None:
    path = attach_file_logging("mirror-backend", config_path=config_path)
    logging.getLogger("aiohttp.access").info("GET /api/sessions 200")
    logging.getLogger("httpx").info("HTTP Request: GET http://localhost:11434/api/tags")
    logging.getLogger("aiohttp.access").warning("boom access")
    logging.getLogger("httpx.client").info("descendant chatter")
    content = _read(path)
    assert "GET /api/sessions 200" not in content
    assert "api/tags" not in content
    assert "descendant chatter" not in content  # dotted-descendant floor applies
    assert "boom access" in content
    # Other loggers are unaffected by the floors.
    logging.getLogger("aiohttp.server").info("normal line")
    assert "normal line" in _read(path)


def test_missing_logging_block_raises(tmp_path: Path) -> None:
    p = tmp_path / "mirror.yaml"
    p.write_text("server:\n  host: x\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="missing required 'logging' block"):
        load_logging_config(p)


def test_missing_key_raises(tmp_path: Path) -> None:
    p = tmp_path / "mirror.yaml"
    p.write_text("logging:\n  dir: logs\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="missing key"):
        load_logging_config(p)


def test_unknown_level_raises(tmp_path: Path) -> None:
    p = tmp_path / "mirror.yaml"
    p.write_text(
        "logging:\n  dir: logs\n  max_bytes: 10\n  backup_count: 1\n"
        "  file_min_levels:\n    httpx: LOUD\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="unknown level"):
        load_logging_config(p)


def test_repo_yaml_logging_block_is_valid() -> None:
    """The checked-in mirror.yaml must satisfy the strict loader — a broken
    block would kill both process boots."""
    cfg = load_logging_config()
    assert cfg["dir"] == "logs"
    assert cfg["file_min_levels"]["aiohttp.access"] == logging.WARNING
