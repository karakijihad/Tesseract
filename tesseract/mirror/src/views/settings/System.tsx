import { Note } from "../../components/common/Note";
import { useState } from "react";

import { Hint } from "../../components/ui/Hint";
import { fetchSystem, type CapabilitySnapshot } from "../../lib/api";
import { useCachedFetch } from "../../lib/useCachedFetch";
import { Button } from "../../components/common/Button";

function fmtMaybe(v: string | null | undefined, suffix = ""): string {
  if (v == null || v === "") return "unknown";
  return suffix ? `${v}${suffix}` : v;
}

function fmtNum(v: number | null | undefined, suffix = ""): string {
  if (v == null) return "unknown";
  return suffix ? `${v}${suffix}` : String(v);
}

export function SystemSection() {
  const {
    data: snap,
    error,
    setError,
    set: setSnap,
  } = useCachedFetch<CapabilitySnapshot>(
    "settings.system",
    () => fetchSystem(false),
  );
  const [refreshing, setRefreshing] = useState(false);


  const refresh = async () => {
    setRefreshing(true);
    setError(null);
    try {
      const fresh = await fetchSystem(true);
      setSnap(fresh);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setRefreshing(false);
    }
  };

  if (!snap) {
    return (
      <section className="settings-section">
        <div className="t-meta">{error ?? "(loading…)"}</div>
      </section>
    );
  }

  const gpuLabel =
    snap.gpu.vendor === "unknown"
      ? "no GPU detected"
      : `${snap.gpu.vendor}${snap.gpu.name ? " · " + snap.gpu.name : ""}${snap.gpu.memory_mb ? ` (${(snap.gpu.memory_mb / 1024).toFixed(1)} GB)` : ""}${snap.gpu.cuda ? " · CUDA" : ""}`;

  return (
    <section className="settings-section">
      <Note>
        What this machine can do, detected rather than assumed. The installer
        reads the same answers before it sets anything up, so what you see here
        is what it decided from.
      </Note>
      {error && <Note tone="bad">{error}</Note>}
      <div className="system-grid">
        <div className="system-row">
          <span className="system-label t-meta">platform</span>
          <span className="system-value">
            {snap.platform.system} {snap.platform.release} (
            {snap.platform.machine})
          </span>
        </div>
        <div className="system-row">
          <span className="system-label t-meta">python</span>
          <span className="system-value">{fmtMaybe(snap.python_version)}</span>
        </div>
        <div className="system-row">
          <span className="system-label t-meta">node</span>
          <span className="system-value">{fmtMaybe(snap.node_version)}</span>
        </div>
        <div className="system-row">
          <span className="system-label t-meta">pnpm</span>
          <span className="system-value">{fmtMaybe(snap.pnpm_version)}</span>
        </div>
        <div className="system-row">
          <Hint label="GPU detection prefers pynvml (NVIDIA), falls back to platform-specific tooling.">
            <span className="system-label t-meta">gpu</span>
          </Hint>
          <span className="system-value">{gpuLabel}</span>
        </div>
        <div className="system-row">
          <span className="system-label t-meta">ram</span>
          <span className="system-value">
            {fmtNum(snap.ram_total_gb, " GB")}
          </span>
        </div>
        <div className="system-row">
          <Hint label="Free space on the drive holding the tesseract repo.">
            <span className="system-label t-meta">disk free</span>
          </Hint>
          <span className="system-value">
            {fmtNum(snap.disk_free_gb, " GB")}
          </span>
        </div>
        <div className="system-row">
          <Hint label="Count of input devices reported by sounddevice (null when sounddevice not installed).">
            <span className="system-label t-meta">mic devices</span>
          </Hint>
          <span className="system-value">{fmtNum(snap.mic_devices)}</span>
        </div>
      </div>
      <div className="system-actions">
        <Button
          onClick={refresh}
          disabled={refreshing}
        >
          {refreshing ? "re-detecting…" : "re-detect"}
        </Button>
      </div>
    </section>
  );
}
