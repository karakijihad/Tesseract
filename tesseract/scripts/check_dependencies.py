"""Capability detection — what this machine can do.

Reports the host's hardware + tooling capability so the Mirror Settings
panel can render a "System" subsection, and so the bootstrap installer can
use the same answers as a pre-flight gate rather than probing twice.

Detected fields:
- python_version: e.g. "3.12.7"
- node_version: best-effort `node --version` parse, or null
- pnpm_version: best-effort `pnpm --version`, or null
- gpu: {vendor: "nvidia"|"amd"|"intel"|"apple"|"unknown", name: str|null,
        memory_mb: int|null, cuda: bool}
- ram_total_gb: int (from psutil if installed, else null)
- disk_free_gb: int (free space on the repo drive)
- mic_devices: int|null (count from sounddevice if installed; null otherwise)
- platform: {system, release, machine}

Each subsystem fails open: if the tool isn't installed or the call
errors, the field is `unknown`/`null`. The script never raises.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

log = logging.getLogger(__name__)

from tesseract.paths import TESSERACT_DIR, TESSERACT_HOME


@dataclass
class GpuInfo:
    vendor: str = "unknown"
    name: str | None = None
    memory_mb: int | None = None
    cuda: bool = False


@dataclass
class CapabilitySnapshot:
    python_version: str = ""
    node_version: str | None = None
    pnpm_version: str | None = None
    gpu: GpuInfo = field(default_factory=GpuInfo)
    ram_total_gb: int | None = None
    disk_free_gb: int | None = None
    mic_devices: int | None = None
    platform: dict[str, str] = field(default_factory=dict)


def _detect_python_version() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"


def _detect_cli_version(executable: str) -> str | None:
    """Probe `<executable> --version`. Returns stripped first line on success."""
    path = shutil.which(executable)
    if path is None:
        return None
    try:
        out = subprocess.check_output(
            [path, "--version"], stderr=subprocess.STDOUT, timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    text = out.decode("utf-8", errors="replace").strip().splitlines()
    return text[0] if text else None


def _detect_gpu() -> GpuInfo:
    """Best-effort GPU detection.

    Tries pynvml first (NVIDIA, fast + accurate), then falls back to
    `nvidia-smi`, then platform-specific tools. Returns a partial info
    on any partial success.
    """
    info = GpuInfo()
    try:
        import pynvml  # type: ignore

        try:
            pynvml.nvmlInit()
        except Exception:
            pass
        else:
            try:
                count = pynvml.nvmlDeviceGetCount()
                if count > 0:
                    handle = pynvml.nvmlDeviceGetHandleByIndex(0)
                    name_raw = pynvml.nvmlDeviceGetName(handle)
                    info.name = (
                        name_raw.decode("utf-8") if isinstance(name_raw, bytes) else str(name_raw)
                    )
                    mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
                    info.memory_mb = int(mem.total // (1024 * 1024))
                    info.vendor = "nvidia"
                    info.cuda = True
            except Exception:
                pass
            finally:
                try:
                    pynvml.nvmlShutdown()
                except Exception:
                    pass
        if info.vendor == "nvidia":
            return info
    except ImportError:
        pass

    # Phase 18 audit M5 — `nvidia-smi` fallback when pynvml is absent.
    # Common case on a Windows host with the NVIDIA driver installed
    # but the Python binding not in the env. Single CSV query, 2 s
    # timeout, no shell.
    smi = _run_nvidia_smi()
    if smi is not None:
        name, memory_mb = smi
        info.vendor = "nvidia"
        info.name = name
        info.memory_mb = memory_mb
        info.cuda = True
        return info

    # Apple Silicon / macOS
    if platform.system() == "Darwin":
        machine = platform.machine().lower()
        if machine in ("arm64", "aarch64"):
            info.vendor = "apple"
            info.name = f"Apple Silicon ({machine})"
        return info

    return info


def _run_nvidia_smi() -> tuple[str, int] | None:
    """Probe `nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits`.

    Returns `(name, memory_mb)` for the first GPU, or None on any
    failure (binary missing, non-zero exit, parse error, timeout).
    """
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0:
        return None
    line = (proc.stdout or "").strip().splitlines()
    if not line:
        return None
    parts = [p.strip() for p in line[0].split(",")]
    if len(parts) < 2:
        return None
    try:
        return parts[0], int(parts[1])
    except ValueError:
        return None


def _detect_ram_gb() -> int | None:
    try:
        import psutil  # type: ignore
    except ImportError:
        return None
    try:
        return int(psutil.virtual_memory().total // (1024**3))
    except Exception:
        return None


def _detect_disk_free_gb() -> int | None:
    try:
        usage = shutil.disk_usage(str(TESSERACT_DIR))
        return int(usage.free // (1024**3))
    except OSError:
        return None


def _detect_mic_devices() -> int | None:
    """Count audio input devices via sounddevice if available."""
    try:
        import sounddevice as sd  # type: ignore
    except ImportError:
        return None
    try:
        devices = sd.query_devices()
    except Exception:
        return None
    count = 0
    for dev in devices:
        if isinstance(dev, dict) and int(dev.get("max_input_channels", 0)) > 0:
            count += 1
    return count


def _detect_platform() -> dict[str, str]:
    return {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
    }


def collect() -> CapabilitySnapshot:
    snap = CapabilitySnapshot(
        python_version=_detect_python_version(),
        node_version=_detect_cli_version("node"),
        pnpm_version=_detect_cli_version("pnpm"),
        gpu=_detect_gpu(),
        ram_total_gb=_detect_ram_gb(),
        disk_free_gb=_detect_disk_free_gb(),
        mic_devices=_detect_mic_devices(),
        platform=_detect_platform(),
    )
    return snap


# `write_snapshot` / `read_snapshot` / `SNAPSHOT_PATH` lived here and are
# gone. This module COLLECTS the machine's facts; persisting them is
# `tesseract.capability`'s, which keeps one artifact under `runtime/` rather
# than a second one inside `runtime/logs/` — a tree the janitor prunes by age,
# so a cache of machine state quietly expired and every reader then paid a
# 10-15 s re-collect believing it was reading a cache.


def _to_dict(snap: CapabilitySnapshot) -> dict[str, Any]:
    raw = asdict(snap)
    return raw


@dataclass
class DoctorCheck:
    name: str
    ok: bool
    detail: str = ""


def _check_python_version() -> DoctorCheck:
    info = sys.version_info
    ok = (info.major, info.minor) >= (3, 12)
    return DoctorCheck(
        name="python>=3.12",
        ok=ok,
        detail=f"running {info.major}.{info.minor}.{info.micro}",
    )


def _check_disk_free(snap: CapabilitySnapshot) -> DoctorCheck:
    free = snap.disk_free_gb
    return DoctorCheck(
        name="disk_free>=2GB",
        ok=free is not None and free >= 2,
        detail=f"{free} GB free" if free is not None else "unknown (psutil missing?)",
    )


def _check_ram(snap: CapabilitySnapshot) -> DoctorCheck:
    ram = snap.ram_total_gb
    return DoctorCheck(
        name="ram>=4GB",
        ok=ram is not None and ram >= 4,
        detail=f"{ram} GB total" if ram is not None else "unknown (psutil missing?)",
    )


def _check_executable(name: str, exe: str) -> DoctorCheck:
    found = shutil.which(exe)
    return DoctorCheck(name=name, ok=found is not None, detail=found or "not found on PATH")


def _check_python_module(name: str, module: str) -> DoctorCheck:
    import importlib.util
    spec = importlib.util.find_spec(module)
    return DoctorCheck(
        name=name,
        ok=spec is not None,
        detail=f"import {module} OK" if spec is not None else f"import {module} fails",
    )


def _check_path_exists(name: str, path: Path, must_exist: bool = True) -> DoctorCheck:
    exists = path.exists()
    ok = exists if must_exist else not exists
    return DoctorCheck(
        name=name,
        ok=ok,
        detail=f"{path} {'exists' if exists else 'missing'}",
    )


def _check_env_keys(env_path: Path, required_keys: list[str]) -> DoctorCheck:
    if not env_path.exists():
        return DoctorCheck(name="env_keys", ok=False, detail=f"{env_path} missing")
    text = env_path.read_text(encoding="utf-8", errors="replace")
    missing = [k for k in required_keys if f"{k}=" not in text]
    if missing:
        return DoctorCheck(
            name="env_keys",
            ok=False,
            detail=f"missing keys in {env_path}: {', '.join(missing)}",
        )
    return DoctorCheck(
        name="env_keys",
        ok=True,
        detail=f"all required keys present ({len(required_keys)})",
    )


def _check_port_free(port: int) -> DoctorCheck:
    """Mirror's default WS port. If something is already listening,
    `python -m tesseract.mirror.server` will fail to bind."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        s.close()
        return DoctorCheck(name=f"mirror_port_{port}_free", ok=True, detail="port is bindable")
    except OSError as exc:
        return DoctorCheck(
            name=f"mirror_port_{port}_free", ok=False, detail=f"bind failed: {exc}",
        )


def _check_ollama_reachable(timeout: float = 1.5) -> DoctorCheck:
    """Best-effort poke at the embedding endpoint declared in providers.yaml.
    Embeddings are optional (memory degrades cleanly to BM25), so this
    is informational, not a hard fail."""
    try:
        import urllib.request
        url = "http://127.0.0.1:11434/api/tags"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ok = 200 <= resp.status < 300
            return DoctorCheck(
                name="ollama_reachable",
                ok=ok,
                detail=f"{url} returned {resp.status}",
            )
    except Exception as exc:
        return DoctorCheck(
            name="ollama_reachable",
            ok=False,
            detail=f"unreachable ({type(exc).__name__}); embeddings will be off",
        )


DoctorMode = Literal["full", "text-only", "voice-only"]


def _text_runtime_checks(snap: CapabilitySnapshot) -> list[DoctorCheck]:
    return [
        _check_python_version(),
        _check_disk_free(snap),
        _check_ram(snap),
        _check_path_exists("vault_dir", TESSERACT_HOME / "vault"),
        _check_path_exists("memory_store_dir", TESSERACT_HOME / "memory-store"),
        _check_path_exists("env_file", TESSERACT_HOME / ".env"),
        _check_env_keys(TESSERACT_HOME / ".env", ["OPENAI_API_KEY"]),
    ]


def _voice_runtime_checks() -> list[DoctorCheck]:
    return [
        _check_executable("ffmpeg", "ffmpeg"),
    ]


def run_doctor(*, mode: DoctorMode = "full") -> tuple[list[DoctorCheck], list[DoctorCheck]]:
    """Run the Phase-17 pre-flight gate.

    Returns (hard_checks, soft_checks). Hard failures cause `--doctor`
    to exit non-zero; soft failures are reported but don't block.

    ``mode`` partitions the runtime checks so an operator can verify the
    text path independently of the voice path:

    - ``"full"`` (default, matches the original ``--doctor`` behaviour):
      both text-runtime and voice-runtime checks are hard.
    - ``"text-only"``: text-runtime checks are hard; voice-runtime checks
      become soft. Mirror can boot without ``ffmpeg`` on PATH.
    - ``"voice-only"``: voice-runtime checks are hard; text-runtime
      checks become soft. Useful to verify a voice install in isolation.
    """
    snap = collect()
    text = _text_runtime_checks(snap)
    voice = _voice_runtime_checks()
    soft_baseline: list[DoctorCheck] = [
        _check_executable("node", "node"),
        _check_executable("pnpm", "pnpm"),
        _check_python_module("pywinpty (terminal panes)", "winpty"),
        _check_port_free(8765),
        _check_ollama_reachable(),
    ]
    if mode == "text-only":
        return text, voice + soft_baseline
    if mode == "voice-only":
        return voice, text + soft_baseline
    return text + voice, soft_baseline


def _format_doctor_report(hard: list[DoctorCheck], soft: list[DoctorCheck]) -> str:
    lines = ["TESSERACT doctor — pre-flight checks", "=" * 40, "", "[hard checks]"]
    for c in hard:
        mark = "PASS" if c.ok else "FAIL"
        lines.append(f"  [{mark}] {c.name}: {c.detail}")
    lines.extend(["", "[soft checks]"])
    for c in soft:
        mark = "OK  " if c.ok else "WARN"
        lines.append(f"  [{mark}] {c.name}: {c.detail}")
    failed = [c.name for c in hard if not c.ok]
    if failed:
        lines.extend(["", f"FAIL — {len(failed)} hard check(s) failed: {', '.join(failed)}"])
    else:
        lines.extend(["", "OK — all hard checks pass"])
    return "\n".join(lines)


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="Run full pre-flight checks (text + voice runtime); exit non-zero on hard failure.",
    )
    parser.add_argument(
        "--text-only",
        action="store_true",
        help="Run pre-flight checks with voice-runtime moved to soft (Mirror text-mode boot).",
    )
    parser.add_argument(
        "--voice-only",
        action="store_true",
        help="Run pre-flight checks with text-runtime moved to soft (verify voice install).",
    )
    args = parser.parse_args()

    chosen = sum([bool(args.doctor), bool(args.text_only), bool(args.voice_only)])
    if chosen > 1:
        print("error: --doctor / --text-only / --voice-only are mutually exclusive", file=sys.stderr)
        return 2

    if args.doctor or args.text_only or args.voice_only:
        mode: DoctorMode = (
            "text-only" if args.text_only
            else "voice-only" if args.voice_only
            else "full"
        )
        hard, soft = run_doctor(mode=mode)
        print(_format_doctor_report(hard, soft))
        return 0 if all(c.ok for c in hard) else 1

    # Prints and does not persist. Writing was this script's half of a second
    # artifact; `python -m tesseract.scripts.reconcile_capabilities` is what
    # records machine state now, and it records the dependencies beside it.
    print(json.dumps(_to_dict(collect()), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
