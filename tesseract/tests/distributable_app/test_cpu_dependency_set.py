import tomllib
from pathlib import Path

from tesseract.paths import TESSERACT_DIR

PYPROJECT = TESSERACT_DIR / "pyproject.toml"


def _load():
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def test_core_deps_have_no_cuda_wheels():
    core = _load()["project"]["dependencies"]
    offenders = [
        d for d in core
        if d.startswith("nvidia-") and not d.startswith("nvidia-ml-py")
    ]
    assert offenders == [], f"CUDA wheels must live in the [gpu] extra: {offenders}"


def test_core_keeps_nvidia_ml_py():
    core = _load()["project"]["dependencies"]
    assert any(d.startswith("nvidia-ml-py") for d in core)


def test_gpu_extra_exists_and_carries_the_cuda_wheels():
    extras = _load()["project"]["optional-dependencies"]
    assert "gpu" in extras
    names = " ".join(extras["gpu"])
    assert "nvidia-cublas-cu12" in names
    assert "nvidia-cudnn-cu12" in names
