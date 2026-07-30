import { useEffect, useState } from "react";

import {
  fetchCapabilities,
  postRuntimeRestart,
  type CapabilitiesResponse,
  type CapabilityProviderStatus,
} from "../../lib/api";
import { isTauri } from "../../lib/endpoints";
import { useWebSocketStore } from "../../stores/websocket";
import { useFetchRetryTick } from "../../lib/useFetchRetry";

async function revealEnvFile(path: string): Promise<void> {
  if (!isTauri()) return;
  const { revealItemInDir } = await import("@tauri-apps/plugin-opener");
  await revealItemInDir(path);
}

function dot(available: boolean): string {
  return available ? "●" : "○";
}

// Three states, not two — "unverified" (enabled, but nothing cheap here
// confirms it) must read differently from "ready" (checked and good) and
// "unavailable" (checked and not good). Glyph-only distinction inside the
// existing .t-meta text color — no new hint color introduced.
function statusDot(status: CapabilityProviderStatus): string {
  if (status === "ready") return "●";
  if (status === "unverified") return "◐";
  return "○";
}

function statusLabel(status: CapabilityProviderStatus): string {
  if (status === "ready") return "ready";
  if (status === "unverified") return "unverified";
  return "off";
}

export function CapabilitiesSection() {
  const [caps, setCaps] = useState<CapabilitiesResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [restarting, setRestarting] = useState(false);
  const [restartNote, setRestartNote] = useState<string | null>(null);

  const refresh = () =>
    fetchCapabilities()
      .then(setCaps)
      .catch((err) =>
        setError(err instanceof Error ? err.message : String(err)),
      );

  // Re-runs on every WS (re)connection: a backend restart must replace a
  // pre-restart "Failed to fetch" with fresh data (2026-07-30).
  const wsGeneration = useWebSocketStore((s) => s.generation);
  const retryTick = useFetchRetryTick(error !== null);
  useEffect(() => {
    setError(null);
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wsGeneration, retryTick]);

  const onRestart = async () => {
    setRestarting(true);
    setRestartNote(null);
    try {
      await postRuntimeRestart(
        "operator restarted from Settings → Capabilities",
      );
      setRestartNote("Restarting TARS — this takes a few seconds.");
    } catch (err) {
      setRestartNote(err instanceof Error ? err.message : String(err));
    } finally {
      setRestarting(false);
    }
  };

  if (!caps) {
    return (
      <section className="settings-section">
        <h3 className="settings-section__title">Capabilities</h3>
        <div className="t-meta">{error ?? "(loading…)"}</div>
      </section>
    );
  }

  return (
    <section className="settings-section">
      <h3 className="settings-section__title">Capabilities</h3>
      <div className="settings-hint t-meta">
        Nothing is required — every provider and key below is optional. This
        shows what&rsquo;s live, what&rsquo;s off, and why.
      </div>
      {error && <div className="settings-error">{error}</div>}

      <div className="cost-row">
        <label className="cost-row__label">Chat</label>
        <span className="t-meta">
          {dot(caps.chat.available)}{" "}
          {caps.chat.available ? "available" : "not available"}
        </span>
        <span className="cost-row__spend t-meta">
          {caps.chat.reason ?? "a configured chat provider resolved"}
        </span>
      </div>
      {caps.chat.candidates.map((c) => (
        <div className="cost-row" key={`${c.provider}/${c.model}`}>
          <label className="cost-row__label">
            {"  "}
            {c.provider}
          </label>
          <span className="t-meta">
            {dot(c.available)} {c.model}
          </span>
          <span className="cost-row__spend t-meta">{c.reason ?? "ready"}</span>
        </div>
      ))}

      <div className="cost-row" style={{ marginTop: "0.75rem" }}>
        <label className="cost-row__label">Providers</label>
        <span className="t-meta" />
        <span className="cost-row__spend t-meta" />
      </div>
      {caps.providers.map((p) => (
        <div className="cost-row" key={`${p.tier}.${p.provider}`}>
          <label className="cost-row__label">
            {"  "}
            {p.tier}.{p.provider}
          </label>
          <span className="t-meta">
            {statusDot(p.status)} {statusLabel(p.status)} ·{" "}
            {p.key_name ?? "no key required"}
          </span>
          <span className="cost-row__spend t-meta">
            {p.reason ?? "verified working"}
          </span>
        </div>
      ))}

      <div className="cost-row" style={{ marginTop: "0.75rem" }}>
        <label className="cost-row__label">Integrations</label>
        <span className="t-meta" />
        <span className="cost-row__spend t-meta" />
      </div>
      {caps.integrations.map((i) => (
        <div className="cost-row" key={i.key_name}>
          <label className="cost-row__label">
            {"  "}
            {i.name}
          </label>
          <span className="t-meta">
            {dot(i.key_present)} {i.key_name}
          </span>
          <span className="cost-row__spend t-meta">
            {i.key_present ? "set" : "not set"}
          </span>
        </div>
      ))}

      <div className="cost-row" style={{ marginTop: "0.75rem" }}>
        <span className="t-meta">Edit this file:</span>
        <code className="t-meta">{caps.env_path}</code>
        {isTauri() && (
          <button
            type="button"
            className="cost-row__save"
            onClick={() => void revealEnvFile(caps.env_path)}
          >
            Open folder
          </button>
        )}
      </div>
      <div className="settings-hint t-meta">
        .env is read once at boot — restart TARS after editing it for changes to
        take effect.
      </div>
      <div className="cost-row cost-row--actions">
        <button
          type="button"
          className="cost-row__save"
          onClick={() => void onRestart()}
          disabled={restarting}
        >
          {restarting ? "Restarting…" : "Restart TARS"}
        </button>
        <button
          type="button"
          className="cost-row__save"
          onClick={() => void refresh()}
          style={{ marginLeft: "0.5rem" }}
        >
          Refresh
        </button>
      </div>
      {restartNote && <div className="t-meta">{restartNote}</div>}
    </section>
  );
}
