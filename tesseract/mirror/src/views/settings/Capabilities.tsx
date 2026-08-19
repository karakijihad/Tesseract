import { Note } from "../../components/common/Note";
import { useMemo, useState } from "react";

import {
  fetchCapabilities,
  postCapabilitiesResetDefaults,
  postProviderEnabled,
  type CapabilitiesResponse,
  type CapabilityProvider,
  type CapabilityProviderStatus,
  type PendingDownload,
} from "../../lib/api";
import { useCachedFetch } from "../../lib/useCachedFetch";
import { useToastStore } from "../../stores/toasts";
import { Hint } from '../../components/ui/Hint';
import { Checkbox } from '../../components/common/Checkbox';
import { Button } from '../../components/common/Button';
import { ResetDefaults } from '../../components/common/ResetDefaults';

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

/** The wording per `when` the route can send. One value today, and reading it
 *  off the field is what stops a second one changing the timing there and not
 *  here. */
const WHEN_WORDING: Record<PendingDownload["when"], string> = {
  next_start: "will download the next time TESSERACT starts",
};

/** Say that turning something on has queued a download, and when.
 *
 * Nothing is fetched at the click: the switch is written and the next start
 * acts on it. Until this said so, the operator flipped a switch, watched
 * nothing happen, and met the download on a later launch with nothing
 * connecting the two.
 *
 * **Deliberately no restart button.** A backend restart is not what runs the
 * fetch — `launch_refresh` is the SHELL's pass, so restarting the backend
 * would collect the browser engine and nothing else. A control that works for
 * one of five rows is worse than the sentence alone.
 */
function announceDownload(pending: PendingDownload | null | undefined) {
  if (!pending) return;
  const size = pending.size_mb
    ? pending.size_mb >= 1000
      ? ` (${(pending.size_mb / 1000).toFixed(1)} GB)`
      : ` (${pending.size_mb} MB)`
    : "";
  // Read off the field rather than assumed. The route sends one value today,
  // and the timing had two independent sources — a second value would have
  // changed the wording there and not here, silently.
  const when = WHEN_WORDING[pending.when];
  if (!when) return;
  useToastStore
    .getState()
    .push(`${pending.names.join(" and ")}${size} ${when}.`, "info");
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
  const {
    data: caps,
    error,
    set: setCaps,
    refresh,
  } = useCachedFetch<CapabilitiesResponse>("capabilities", fetchCapabilities);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [savingKey, setSavingKey] = useState<string | null>(null);

  // The route returns the refreshed report along with what moved; the shared
  // button owns the pending state, the toast and the nothing-moved case.
  const onResetDefaults = async () => {
    const res = await postCapabilitiesResetDefaults();
    setCaps(res);
    return res.reset;
  };

  const tierGroups = useMemo(
    () => (caps ? groupByTier(caps.providers) : []),
    [caps],
  );

  // `provider: null` targets the tier switch. The POST returns the refreshed
  // report, so there's no follow-up GET and no optimistic local state to
  // reconcile — what renders next is what landed on disk.
  const onToggle = async (
    tier: string,
    provider: string | null,
    next: boolean,
  ) => {
    setSavingKey(provider ? `${tier}.${provider}` : tier);
    setSaveError(null);
    try {
      const report = await postProviderEnabled(tier, provider, next);
      setCaps(report);
      announceDownload(report.pending_download);
    } catch (err) {
      setSaveError(
        err instanceof Error ? err.message : "provider toggle failed",
      );
    } finally {
      setSavingKey(null);
    }
  };

  // Null when no service row reported it — a backend that predates the field,
  // or a config with no services at all. The switch is not drawn rather than
  // guessed, because a box that shows a state nobody sent is worse than none.
  const servicesSection =
    caps?.integrations.find((i) => i.service)?.section_enabled ?? null;

  if (!caps) {
    return (
      <section className="settings-section">
        <div className="t-meta">{error ?? "(loading…)"}</div>
      </section>
    );
  }

  return (
    <section className="settings-section">
      <Note>
        Nothing is required — every provider and key below is optional. This
        shows what&rsquo;s live, what&rsquo;s off, and why.
      </Note>
      {(error || saveError) && (
        <Note tone="bad">{error ?? saveError}</Note>
      )}

      {/* Grouped and indented the way a tier groups its providers — the rows
          under Chat sat flush with it and read as four unrelated entries. And
          they are named for what they are: the chain is ordered, so the first
          is what speaks and the rest are what catch it when it cannot. */}
      <div>
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
        {caps.chat.candidates.map((c, index) => (
          <div className="cost-row" key={`${c.provider}/${c.model}`}>
            <label className="cost-row__label">
              {"    "}
              <span className="provider-row__toggle-gap" aria-hidden="true" />{" "}
              {c.provider}
            </label>
            <span className="t-meta">
              {dot(c.available)} {c.model}
            </span>
            <span className="cost-row__spend t-meta">
              {index === 0 ? "primary" : `fallback ${index}`} ·{" "}
              {c.reason ?? "ready"}
            </span>
          </div>
        ))}
      </div>

      {/* Deliberately a pointer and not an editor. Reordering, adding and
          dropping a fallback already have one home — the chain the role
          follows — and a second set of the same verbs over the same list is
          two writers and two places to read the order from. This screen
          reports; that one edits, and it shows the same per-entry status. */}
      <Note>
        This is a report. To change what serves chat &mdash; reorder the
        fallbacks, add one, drop one &mdash; edit its chain in Settings &rarr;
        Chains.
      </Note>

      <div className="cost-row" style={{ marginTop: "0.75rem" }}>
        <label className="cost-row__label">Providers</label>
        <span className="t-meta" />
        <span className="cost-row__spend t-meta" />
      </div>
      <Note>
        Switching one off writes <code>enabled: false</code> to providers.yaml
        and reloads without a restart. Nothing warns you first &mdash; if a role
        still points at it, the failure names the flag that is off.
      </Note>
      {tierGroups.map((g) => {
        const onCount = g.providers.filter((p) => p.provider_enabled).length;
        return (
          <div key={g.tier}>
            <div className="cost-row">
              <label className="cost-row__label">
                <Checkbox
                  checked={g.tierEnabled}
                  disabled={savingKey !== null}
                  onChange={(next) => void onToggle(g.tier, null, next)}
                  ariaLabel={`${g.tier} tier enabled`}
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
                  <Hint label={g.tierEnabled
                        ? undefined
                        : `the ${g.tier} tier switch is off — turn it on to use this provider`}>
                    <Checkbox
                      checked={p.provider_enabled}
                      // The tier switch already gates this provider, so editing
                      // its own flag would change nothing visible. It keeps its
                      // stored value for when the tier comes back on.
                      disabled={!g.tierEnabled || savingKey !== null}
                      onChange={(next) => void onToggle(p.tier, p.provider, next)}
                      ariaLabel={`${p.tier}.${p.provider} enabled`}
                    />
                  </Hint>{" "}
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

      {/* The section's own switch, which was the missing half of gating the
          service boxes on it: the route has always taken
          `{tier: "services", provider: null}`, and with no control for it the
          section could be turned off from here and never back on. It is the
          tier row above, one section down. */}
      <div className="cost-row" style={{ marginTop: "0.75rem" }}>
        <label className="cost-row__label">
          {servicesSection === null ? (
            <span className="provider-row__toggle-gap" aria-hidden="true" />
          ) : (
            <Checkbox
              checked={servicesSection}
              disabled={savingKey !== null}
              onChange={(next) => void onToggle("services", null, next)}
              ariaLabel="services section enabled"
            />
          )}{" "}
          Integrations
        </label>
        <span className="t-meta">
          {servicesSection === null
            ? ""
            : servicesSection
              ? "section on"
              : "section off"}
        </span>
        <span className="cost-row__spend t-meta">
          {servicesSection === false ? "gates every service below" : ""}
        </span>
      </div>
      {caps.integrations.map((i) => {
        // Every row here is governed by a switch, so every row has one — a
        // service's lives in providers.yaml and a channel's in channels.yaml,
        // which is a fact about files and was never a reason for one row on
        // the screen to be the only one you cannot throw.
        const section = i.service ? "services" : "channels";
        const block = i.service ?? i.channel;
        return (
          <div className="cost-row" key={`${section}.${block ?? i.name}`}>
            {/* The same three columns as the providers above, in the same
                order: the switch sits beside the NAME it switches, and the key
                sits in the state column with the dot that reports it. */}
            <label className="cost-row__label">
              {"    "}
              {block ? (
                <Hint
                  label={
                    !(i.section_enabled ?? true)
                      ? "the services section switch is off in providers.yaml — turn it on to use this"
                      : i.channel
                        ? "takes effect on the next start — a running bridge is not stopped live"
                        : undefined
                  }
                >
                  <Checkbox
                    // `?? ` on both, for the reason the boundary exists: a
                    // backend a release behind this screen sends neither
                    // field, and `disabled={!undefined}` is every switch on
                    // the panel dead with nothing on screen saying why.
                    checked={i.service_enabled ?? i.enabled}
                    // The box shows the flag it WRITES, not the AND. Showing
                    // the AND made a service read off whenever the `services`
                    // section switch was off, and clicking it then rewrote an
                    // already-true per-service flag with nothing visible
                    // happening. Gated instead, like a provider under a tier.
                    disabled={!(i.section_enabled ?? true) || savingKey !== null}
                    onChange={(next) => void onToggle(section, block, next)}
                    ariaLabel={`${section}.${block} enabled`}
                  />
                </Hint>
              ) : (
                <span className="provider-row__toggle-gap" aria-hidden="true" />
              )}{" "}
              {i.name}
              {/* Two verbs and a count, with the rest on hover. The label was
                  the whole joined list, which made the browser row seven times
                  the width of the ones above it and pushed its own columns off
                  the grid the providers share. */}
              {i.unlocks.length > 0 && (
                <Hint label={i.unlocks.join(", ")}>
                  <span className="t-meta">
                    {" — "}
                    {i.unlocks.slice(0, 2).join(", ")}
                    {i.unlocks.length > 2
                      ? ` +${i.unlocks.length - 2} more`
                      : ""}
                  </span>
                </Hint>
              )}
            </label>
            <span className="t-meta">
              {/* A service gated by a download rather than a key reports the
                  switch alone — there is no token to be present, and naming a
                  key it does not have would be the third column lying. */}
              {dot(i.key_name ? i.key_present && i.enabled : i.enabled)}{" "}
              {i.enabled
                ? "on"
                : i.service_enabled ?? i.enabled
                  ? "off — section"
                  : "off"}{" "}
              · {i.key_name ?? "no key required"}
            </span>
            <span className="cost-row__spend t-meta">
              {!i.enabled
                ? i.key_present
                  ? "switched off in config — key is set"
                  : "switched off in config"
                : !i.key_name
                  ? "downloads on next start if missing"
                  : i.key_present
                    ? "set"
                    : "not set"}
            </span>
          </div>
        );
      })}

      {/* Setting a key, the file it lives in, and the restart that loads it
          all moved to Settings → Keys below. Two restart buttons on one
          screen is two answers to "did that apply?". */}
      <Note>
        A missing key is set in Settings &rarr; Keys, directly below.
      </Note>
      <div className="cost-row cost-row--actions">
        <Button
          onClick={() => void refresh()}
          tone="primary"
        >
          Refresh
        </Button>
        <ResetDefaults
          run={onResetDefaults}
          reach="every switch above — your keys and your channels are not touched"
        />
        <span className="t-meta">
          An update never changes a switch you already have — resetting is how
          you take a new default.
        </span>
      </div>
    </section>
  );
}
