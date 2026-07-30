import { useEffect, useState } from "react";

import { Hint } from "../../components/ui/Hint";
import { fetchSystem, type CapabilitySnapshot } from "../../lib/api";
import { useWebSocketStore } from "../../stores/websocket";

function fmtMaybe(v: string | null | undefined, suffix = ""): string {
  if (v == null || v === "") return "unknown";
  return suffix ? `${v}${suffix}` : v;
}

function fmtNum(v: number | null | undefined, suffix = ""): string {
  if (v == null) return "unknown";
  return suffix ? `${v}${suffix}` : String(v);
}

export function SystemSection() {
  const [snap, setSnap] = useState<CapabilitySnapshot | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Re-runs on every WS (re)connection: a backend restart must replace a
  // pre-restart "Failed to fetch" with fresh data (2026-07-30).
  const wsGeneration = useWebSocketStore((s) => s.generation);
  useEffect(() => {
    setError(null);
    fetchSystem(false)
      .then(setSnap)
      .catch((err) =>
        setError(err instanceof Error ? err.message : String(err)),
      );
  }, [wsGeneration]);

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
        <h3 className="settings-section__title">System</h3>
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
      <h3 className="settings-section__title">System</h3>
      <div className="settings-hint t-meta">
        Capability snapshot from{" "}
        <code>tesseract/scripts/check_dependencies.py</code>. Phase 17 reuses
        this as a pre-flight gate for the bootstrap installer.
      </div>
      {error && <div className="settings-error">{error}</div>}
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
        <button
          type="button"
          className="system-redetect"
          onClick={refresh}
          disabled={refreshing}
        >
          {refreshing ? "re-detecting…" : "re-detect"}
        </button>
      </div>
    </section>
  );
}
