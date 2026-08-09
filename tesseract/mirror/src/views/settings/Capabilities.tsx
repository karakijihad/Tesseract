import { useEffect, useMemo, useState } from "react";

import {
  fetchCapabilities,
  postProviderEnabled,
  postRuntimeRestart,
  type CapabilitiesResponse,
  type CapabilityProvider,
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

interface TierGroup {
  tier: string;
  tierEnabled: boolean;
  providers: CapabilityProvider[];
}

// Tiers come from the report's own row order, and providers within a tier are
// whatever the backend discovered in providers.yaml — adding a provider to the
// YAML makes it appear here with no frontend change.
function groupByTier(providers: CapabilityProvider[]): TierGroup[] {
  const order: string[] = [];
  const byTier = new Map<string, CapabilityProvider[]>();
  for (const p of providers) {
    const rows = byTier.get(p.tier);
    if (rows) {
      rows.push(p);
    } else {
      byTier.set(p.tier, [p]);
      order.push(p.tier);
    }
  }
  return order.map((tier) => {
    const rows = byTier.get(tier) as CapabilityProvider[];
    return { tier, tierEnabled: rows[0].tier_enabled, providers: rows };
  });
}

export function CapabilitiesSection() {
  const [caps, setCaps] = useState<CapabilitiesResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [restarting, setRestarting] = useState(false);
  const [restartNote, setRestartNote] = useState<string | null>(null);
  const [savingKey, setSavingKey] = useState<string | null>(null);

  const tierGroups = useMemo(
    () => (caps ? groupByTier(caps.providers) : []),
    [caps],
  );

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

  // `provider: null` targets the tier switch. The POST returns the refreshed
  // report, so there's no follow-up GET and no optimistic local state to
  // reconcile — what renders next is what landed on disk.
  const onToggle = async (
    tier: string,
    provider: string | null,
    next: boolean,
  ) => {
    setSavingKey(provider ? `${tier}.${provider}` : tier);
    setError(null);
    try {
      setCaps(await postProviderEnabled(tier, provider, next));
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "provider toggle failed",
      );
    } finally {
      setSavingKey(null);
    }
  };

  const onRestart = async () => {
    setRestarting(true);
    setRestartNote(null);
    try {
      await postRuntimeRestart(
        "operator restarted from Settings → Capabilities",
      );
      setRestartNote("Restarting TESSERACT — this takes a few seconds.");
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
      <div className="settings-hint t-meta">
        Switching one off writes <code>enabled: false</code> to providers.yaml
        and reloads without a restart. Nothing warns you first &mdash; if a role
        still points at it, the failure names the flag that is off.
      </div>
      {tierGroups.map((g) => {
        const onCount = g.providers.filter((p) => p.provider_enabled).length;
        return (
          <div key={g.tier}>
            <div className="cost-row">
              <label className="cost-row__label">
                <input
                  type="checkbox"
                  className="provider-row__toggle"
                  checked={g.tierEnabled}
                  disabled={savingKey !== null}
                  onChange={(e) => void onToggle(g.tier, null, e.target.checked)}
                  aria-label={`${g.tier} tier enabled`}
                />{" "}
                {g.tier}
              </label>
              <span className="t-meta">
                {g.tierEnabled ? "tier on" : "tier off"}
              </span>
              <span className="cost-row__spend t-meta">
                {g.tierEnabled
                  ? `${onCount} of ${g.providers.length} on`
                  : "gates every provider below"}
              </span>
            </div>
            {g.providers.map((p) => (
              <div className="cost-row" key={`${p.tier}.${p.provider}`}>
                <label className="cost-row__label">
                  {"    "}
                  <input
                    type="checkbox"
                    className="provider-row__toggle"
                    checked={p.provider_enabled}
                    // The tier switch already gates this provider, so editing
                    // its own flag would change nothing visible. It keeps its
                    // stored value for when the tier comes back on.
                    disabled={!g.tierEnabled || savingKey !== null}
                    title={
                      g.tierEnabled
                        ? undefined
                        : `the ${g.tier} tier switch is off — turn it on to use this provider`
                    }
                    onChange={(e) =>
                      void onToggle(p.tier, p.provider, e.target.checked)
                    }
                    aria-label={`${p.tier}.${p.provider} enabled`}
                  />{" "}
                  {p.provider}
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
          </div>
        );
      })}

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
        {/* Span the remaining tracks — the 140px value column truncated the
            path and the button used to sit on top of it (2026-07-30). */}
        <code className="t-meta" style={{ gridColumn: "2 / -1" }}>
          {caps.env_path}
        </code>
      </div>
      {isTauri() && (
        <div className="cost-row cost-row--actions">
          <button
            type="button"
            className="cost-row__save"
            onClick={() => void revealEnvFile(caps.env_path)}
          >
            Open folder
          </button>
        </div>
      )}
      <div className="settings-hint t-meta">
        .env is read once at boot — restart TESSERACT after editing it for changes to
        take effect.
      </div>
      <div className="cost-row cost-row--actions">
        <button
          type="button"
          className="cost-row__save"
          onClick={() => void onRestart()}
          disabled={restarting}
        >
          {restarting ? "Restarting…" : "Restart TESSERACT"}
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
