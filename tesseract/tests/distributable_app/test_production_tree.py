from pathlib import Path

import pytest
from ruamel.yaml import YAML

from tesseract.paths import ROOT
from tesseract.scripts import _production_manifest as man
from tesseract.scripts.build_production_tree import build, tracked_files


def _make_src(tmp_path: Path) -> Path:
    """On-disk fixture tree. Files that would NOT be git-tracked in reality
    (state dirs, secrets) are created on disk but deliberately left OUT of
    the explicit `files` list each test passes to `build()` — that omission
    is what the allowlist model relies on instead of a manifest rule.

    `tesseract/tests` is git-tracked AND ships as of Task 8 (PII externalized
    to `.pii-tokens.local.json`, gitignored) — it belongs with the tracked
    fixtures below, not the untracked/must-not-ship ones.
    """
    src = tmp_path / "src"
    (src / "tesseract" / "brain").mkdir(parents=True)
    (src / "tesseract" / "brain" / "boot.py").write_text("x = 1\n", encoding="utf-8")
    (src / "tesseract" / "config" / "_shipping").mkdir(parents=True)
    (src / "tesseract" / "config" / "mirror.yaml").write_text(
        "mirror:\n  operator_name: Jane Doe\n", encoding="utf-8"
    )
    # mirror.yaml ships from its _shipping/ template (Task 8b) — the live
    # text above must never reach the output.
    (src / "tesseract" / "config" / "_shipping" / "mirror.yaml").write_text(
        "mirror:\n  operator_name: Operator\n", encoding="utf-8"
    )
    (src / "tesseract" / "config" / "providers.yaml").write_text(
        "whisper:\n  device: cuda\n  compute_type: int8_float16\n  model: large-v3-turbo\n",
        encoding="utf-8",
    )
    # providers.yaml ships from its _shipping/ template too (Task 8c) — the
    # live GPU-specific text above must never reach the output.
    (src / "tesseract" / "config" / "_shipping" / "providers.yaml").write_text(
        "whisper:\n  device: cpu\n  compute_type: int8\n  model: base\n", encoding="utf-8"
    )
    # tracked and shippable as of Task 8 (see docstring above)
    (src / "tesseract" / "tests").mkdir(parents=True)
    (src / "tesseract" / "tests" / "test_x.py").write_text("pass\n", encoding="utf-8")
    # things that must NOT ship
    (src / "Docs").mkdir(parents=True)
    (src / "Docs" / "notes.md").write_text("private\n", encoding="utf-8")
    (src / "tesseract" / "workspace").mkdir(parents=True)
    (src / "tesseract" / "workspace" / "SOUL.md").write_text("private\n", encoding="utf-8")
    (src / "tesseract" / ".env").write_text("TAVILY_API_KEY=real-secret\n", encoding="utf-8")
    (src / "tesseract" / "work_index.sqlite").write_bytes(b"\x00")
    # dev-process files — tracked in reality, but must not ship (info disclosure)
    (src / "CLAUDE.md").write_text("security architecture notes\n", encoding="utf-8")
    (src / ".github" / "workflows").mkdir(parents=True)
    (src / ".github" / "workflows" / "x.yml").write_text("name: x\n", encoding="utf-8")
    return src


_TRACKED = (
    "tesseract/brain/boot.py",
    "tesseract/config/mirror.yaml",
    "tesseract/config/providers.yaml",
    "tesseract/tests/test_x.py",
)
_UNTRACKED_ON_DISK = (
    "Docs/notes.md",
    "tesseract/workspace/SOUL.md",
    "tesseract/.env",
    "tesseract/work_index.sqlite",
)
_DEV_PROCESS_FILES = (
    "CLAUDE.md",
    ".github/workflows/x.yml",
)


def test_excluded_paths_never_ship(tmp_path):
    src, out = _make_src(tmp_path), tmp_path / "out"
    # Simulate git ls-files listing EVERYTHING (i.e. even these were somehow
    # tracked) so the belt-and-braces EXCLUDE_PATHS/EXCLUDE_GLOBS rules are
    # what must catch them, independent of tracking status.
    files = list(_TRACKED) + list(_UNTRACKED_ON_DISK) + list(_DEV_PROCESS_FILES)
    build(src, out, files=files)
    assert (out / "tesseract" / "tests" / "test_x.py").exists()
    assert not (out / "Docs").exists()
    assert not (out / "tesseract" / ".env").exists()
    assert not (out / "tesseract" / "work_index.sqlite").exists()
    assert not (out / "CLAUDE.md").exists()
    assert not (out / ".github").exists()


def test_no_secret_material_anywhere_in_output(tmp_path):
    src, out = _make_src(tmp_path), tmp_path / "out"
    files = list(_TRACKED) + list(_UNTRACKED_ON_DISK) + list(_DEV_PROCESS_FILES)
    build(src, out, files=files)
    for path in out.rglob("*"):
        if not path.is_file():
            continue
        body = path.read_text(encoding="utf-8", errors="ignore")
        assert "real-secret" not in body, f"secret leaked into {path}"
        assert "Jane Doe" not in body, f"operator name leaked into {path}"


def test_source_and_templated_config_do_ship(tmp_path):
    src, out = _make_src(tmp_path), tmp_path / "out"
    build(src, out, files=list(_TRACKED))
    assert (out / "tesseract" / "brain" / "boot.py").read_text(encoding="utf-8") == "x = 1\n"
    providers = (out / "tesseract" / "config" / "providers.yaml").read_text(encoding="utf-8")
    assert "device: cpu" in providers  # ships from _shipping/providers.yaml, not the live GPU config


def test_entities_people_stripped_from_shipped_tree(tmp_path):
    src, out = _make_src(tmp_path), tmp_path / "out"
    (src / "tesseract" / "memory").mkdir(parents=True)
    (src / "tesseract" / "memory" / "entities.yaml").write_text(
        "people:\n"
        "  - name: Jane Doe\n"
        "    aliases: [operator]\n"
        "projects:\n"
        "  - name: TESSERACT\n"
        "    notes: runtime\n",
        encoding="utf-8",
    )
    files = list(_TRACKED) + ["tesseract/memory/entities.yaml"]
    build(src, out, files=files)

    out_entities = (out / "tesseract" / "memory" / "entities.yaml").read_text(encoding="utf-8")
    assert "Jane Doe" not in out_entities

    data = YAML().load(out_entities)
    assert data["people"] == []
    assert len(data["projects"]) == 1
    assert data["projects"][0]["name"] == "TESSERACT"


def test_state_dirs_created_empty(tmp_path):
    src, out = _make_src(tmp_path), tmp_path / "out"
    build(src, out, files=list(_TRACKED))
    for rel in man.EMPTY_DIRS:
        d = out / rel
        assert d.is_dir()
        assert [p.name for p in d.iterdir()] == [".gitkeep"]


def test_refuses_to_write_into_source(tmp_path):
    src = _make_src(tmp_path)
    with pytest.raises(ValueError):
        build(src, src, files=list(_TRACKED))


def test_refuses_when_src_root_lives_inside_out_root(tmp_path):
    """[MOST URGENT] Guards must work in both directions. Swapped CLI args
    (`out_root` passed as the repo root, `src_root` as a subdir of it) must
    not let `rmtree(out_root)` delete the entire source tree."""
    src = _make_src(tmp_path)
    marker = src / "tesseract" / "brain" / "boot.py"
    with pytest.raises(ValueError):
        build(src, src.parent, files=list(_TRACKED))
    assert marker.exists(), "src_root was deleted despite the ancestor guard"


def test_source_dirs_whose_names_collide_with_state_dirs_still_ship(tmp_path):
    """Regression: `workspace`/`tars_controller` name real SOURCE dirs as well
    as runtime-state dirs. In the allowlist model the state-dir files are
    simply untracked (never in `files`) — the collision-named source dirs
    ship because they ARE in `files`, proving the mechanism is tracking
    status, not any path/name heuristic re-implemented in the builder."""
    src = _make_src(tmp_path)
    # real source that must survive — tracked. Iterate MUST_SHIP itself so
    # this regression can't silently diverge from the manifest's own list.
    must_ship = tuple(f"{rel}/code.ts" for rel in man.MUST_SHIP)
    for rel in must_ship:
        f = src / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("export const x = 1\n", encoding="utf-8")
    # state dirs at their real anchored paths — present on disk, but untracked
    must_not_ship = (
        "tesseract/workspace/private.txt",
        "tesseract/tars_controller/private.txt",
    )
    for rel in must_not_ship:
        f = src / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("real-secret\n", encoding="utf-8")

    out = tmp_path / "out"
    build(src, out, files=list(_TRACKED) + list(must_ship))

    for rel in must_ship:
        assert (out / rel).exists(), f"source wrongly dropped: {rel}"
    for rel in must_not_ship:
        assert not (out / rel).exists(), f"state wrongly shipped: {rel}"


def test_untracked_files_never_ship_even_if_present_on_disk(tmp_path):
    """The allowlist mechanism itself: a `.venv`/`node_modules`/build-cache
    file physically present on disk never ships because iteration is driven
    entirely by `files`, never by walking the source tree."""
    src = _make_src(tmp_path)
    for rel in (".venv/lib/junk.py", "node_modules/pkg/junk.js", "__pycache__/junk.pyc"):
        f = src / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("junk\n", encoding="utf-8")
    out = tmp_path / "out"
    build(src, out, files=list(_TRACKED))
    for rel in (".venv", "node_modules", "__pycache__"):
        assert not (out / rel).exists(), f"{rel} must not ship"


def test_symlinks_never_shipped_even_if_tracked(tmp_path):
    """Git tracks the path string, not what gets copied — `is_file()` and
    `copy2` both resolve through symlinks, so a tracked symlink pointing
    outside the src tree must be skipped explicitly or its target's live
    content leaks into the output."""
    src = _make_src(tmp_path)
    outside_target = tmp_path / "outside-secret.txt"
    outside_target.write_text("outside-secret\n", encoding="utf-8")
    link = src / "tesseract" / "linked.txt"
    try:
        link.symlink_to(outside_target)
    except OSError:
        pytest.skip("symlink creation not permitted in this environment")
    out = tmp_path / "out"
    build(src, out, files=list(_TRACKED) + ["tesseract/linked.txt"])
    assert not (out / "tesseract" / "linked.txt").exists()


def test_tracked_files_returns_real_repo_paths():
    """Proves the allowlist works against THIS repo, not just synthetic
    fixtures: known source ships, known gitignored/build-cache dirs don't."""
    files = tracked_files(ROOT)
    assert "tesseract/paths.py" in files
    prefixes = (".venv", "node_modules", "tesseract/mirror/src-tauri/target")
    offenders = [f for f in files if any(f == p or f.startswith(f"{p}/") for p in prefixes)]
    assert offenders == [], f"gitignored paths leaked into tracked_files(): {offenders}"
