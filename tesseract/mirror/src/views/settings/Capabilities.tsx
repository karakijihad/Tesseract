import { useEffect, useMemo, useState } from "react";

import {
  fetchCapabilities,
  postProviderEnabled,
  type CapabilitiesResponse,
  type CapabilityProvider,
  type CapabilityProviderStatus,
} from "../../lib/api";
import { useWebSocketStore } from "../../stores/websocket";
import { useFetchRetryTick } from "../../lib/useFetchRetry";

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
            {/* Services carry the same switch the providers above do, and
                the operator throws it for the same reason: having the key
                and not wanting the capability to run. Channels are not
                writable here — they have their own panel. */}
            {i.service ? (
              <input
                type="checkbox"
                className="provider-row__toggle"
                checked={i.enabled}
                disabled={savingKey !== null}
                onChange={(e) =>
                  void onToggle("services", i.service as string, e.target.checked)
                }
                aria-label={`services.${i.service} enabled`}
              />
            ) : (
              dot(i.key_present && i.enabled)
            )}{" "}
            {i.key_name}
          </span>
          <span className="cost-row__spend t-meta">
            {!i.enabled
              ? i.key_present
                ? "switched off in config — key is set"
                : "switched off in config"
              : i.key_present
                ? "set"
                : "not set"}
          </span>
        </div>
      ))}

      {/* Setting a key, the file it lives in, and the restart that loads it
          all moved to Settings → API keys below. Two restart buttons on one
          screen is two answers to "did that apply?". */}
      <div className="settings-hint t-meta">
        A missing key is set in Settings &rarr; API keys, directly below.
      </div>
      <div className="cost-row cost-row--actions">
        <button
          type="button"
          className="cost-row__save"
          onClick={() => void refresh()}
        >
          Refresh
        </button>
      </div>
    </section>
  );
}
