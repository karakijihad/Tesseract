import { useEffect, useMemo, useState } from 'react';

import { postCostSettings, postVoiceCostSettings } from '../../lib/api';
import { useCostStore } from '../../stores/cost';
import { useIdentityStore } from '../../stores/identity';
import { formatUsd } from '../../lib/money';

type VoiceKind = 'tts' | 'stt';

const VOICE_RATE_LABEL: Record<VoiceKind, string> = {
  tts: '$ / 1M chars',
  stt: '$ / audio-hour',
};

interface VoiceDraft {
  rate: string;
  cap: string;
}

export function CostSection() {
  const costTracking = useIdentityStore((s) => s.costTracking);
  const setCostTracking = useIdentityStore((s) => s.setCostTracking);
  const perRoleSpend = useCostStore((s) => s.perRole);
  const globalState = useCostStore((s) => s.globalState);
  const voiceProviders = useCostStore((s) => s.voiceProviders);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [warnPctDraft, setWarnPctDraft] = useState<string>('');
  const [perRoleDraft, setPerRoleDraft] = useState<Record<string, string>>({});
  const [voiceDraft, setVoiceDraft] = useState<Record<VoiceKind, Record<string, VoiceDraft>>>({
    tts: {},
    stt: {},
  });

  useEffect(() => {
    if (!costTracking) return;
    // Old backends (pre-refactor) don't ship `warning_at_pct`; show 75 as
    // the harness default so the input isn't NaN/blank until the operator
    // restarts the server.
    const pct = costTracking.warning_at_pct ?? 0.75;
    setWarnPctDraft((pct * 100).toString());
    setPerRoleDraft(
      Object.fromEntries(
        Object.entries(costTracking.per_role).map(([k, v]) => [k, String(v)]),
      ),
    );
    const voice = costTracking.voice ?? { tts: {}, stt: {} };
    setVoiceDraft({
      tts: Object.fromEntries(
        Object.entries(voice.tts).map(([provider, p]) => [
          provider,
          { rate: String(p.rate), cap: String(p.cap_usd) },
        ]),
      ),
      stt: Object.fromEntries(
        Object.entries(voice.stt).map(([provider, p]) => [
          provider,
          { rate: String(p.rate), cap: String(p.cap_usd) },
        ]),
      ),
    });
  }, [costTracking]);

  // Derived daily cap = sum of per-role drafts + sum of voice cap drafts.
  // Recomputes as the operator edits, mirroring how the backend derives it
  // on save. Fall back to costTracking.daily_budget_usd when drafts haven't
  // hydrated yet.
  const derivedDaily = useMemo(() => {
    const perRoleTotal = Object.values(perRoleDraft).reduce(
      (acc, v) => acc + (parseFloat(v) || 0),
      0,
    );
    const voiceTotal = (['tts', 'stt'] as VoiceKind[]).reduce((acc, kind) => {
      return acc + Object.values(voiceDraft[kind]).reduce(
        (sub, d) => sub + (parseFloat(d.cap) || 0),
        0,
      );
    }, 0);
    return perRoleTotal + voiceTotal;
  }, [perRoleDraft, voiceDraft]);

  const dirty = useMemo(() => {
    if (!costTracking) return false;
    if (parseFloat(warnPctDraft) / 100 !== costTracking.warning_at_pct) return true;
    for (const [role, cap] of Object.entries(costTracking.per_role)) {
      if (parseFloat(perRoleDraft[role] ?? '') !== cap) return true;
    }
    const voice = costTracking.voice;
    if (voice) {
      for (const kind of ['tts', 'stt'] as VoiceKind[]) {
        for (const [provider, p] of Object.entries(voice[kind])) {
          const d = voiceDraft[kind][provider];
          if (!d) continue;
          if (parseFloat(d.rate) !== p.rate) return true;
          if (parseFloat(d.cap) !== p.cap_usd) return true;
        }
      }
    }
    return false;
  }, [costTracking, warnPctDraft, perRoleDraft, voiceDraft]);

  const onSave = async () => {
    if (!costTracking || !dirty) return;
    setSaving(true);
    setError(null);
    try {
      const pct = parseFloat(warnPctDraft) / 100;
      if (!Number.isFinite(pct) || pct < 0 || pct > 1) {
        throw new Error('Warning must be between 0% and 100%');
      }
      const perRole: Record<string, number> = {};
      for (const [role, raw] of Object.entries(perRoleDraft)) {
        const v = parseFloat(raw);
        if (!Number.isFinite(v) || v < 0) throw new Error(`${role} cap must be >= 0`);
        perRole[role] = v;
      }
      const voicePayload: {
        tts: Record<string, { cost_per_million_chars: number; daily_budget_usd: number }>;
        stt: Record<string, { cost_per_audio_hour: number; daily_budget_usd: number }>;
      } = { tts: {}, stt: {} };
      let voiceDirty = false;
      const voice = costTracking.voice ?? { tts: {}, stt: {} };
      for (const kind of ['tts', 'stt'] as VoiceKind[]) {
        for (const [provider, p] of Object.entries(voice[kind])) {
          const d = voiceDraft[kind][provider];
          if (!d) continue;
          const rate = parseFloat(d.rate);
          const cap = parseFloat(d.cap);
          if (!Number.isFinite(rate) || rate < 0) {
            throw new Error(`${kind} ${provider}: rate must be >= 0`);
          }
          if (!Number.isFinite(cap) || cap <= 0) {
            throw new Error(`${kind} ${provider}: daily cap must be > 0`);
          }
          if (rate === p.rate && cap === p.cap_usd) continue;
          voiceDirty = true;
          if (kind === 'tts') {
            voicePayload.tts[provider] = {
              cost_per_million_chars: rate,
              daily_budget_usd: cap,
            };
          } else {
            voicePayload.stt[provider] = {
              cost_per_audio_hour: rate,
              daily_budget_usd: cap,
            };
          }
        }
      }
      // Two endpoints because chat caps + warning_pct live in the
      // top-level cost_tracking block while voice rates/caps live under
      // cost_tracking.voice — different validators server-side. Run the
      // chat write first; it's the cheaper of the two and its failure
      // mode (per_role validation error) is the more common operator
      // mistake. Voice write last so its response is the freshest
      // identity snapshot.
      let res = await postCostSettings({ warning_at_pct: pct, per_role: perRole });
      if (voiceDirty) {
        res = await postVoiceCostSettings(voicePayload);
      }
      setCostTracking(res);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'cost update failed');
    } finally {
      setSaving(false);
    }
  };

  if (!costTracking) {
    return (
      <section className="settings-section">
        <h3 className="settings-section__title">Cost &amp; budgets</h3>
        <div className="t-meta">(loading…)</div>
      </section>
    );
  }

  const perRoleKeys = Object.keys(costTracking.per_role);

  const renderVoiceRows = (kind: VoiceKind) => {
    const voice = costTracking.voice;
    if (!voice) return null;
    const providers = Object.entries(voice[kind]);
    if (providers.length === 0) return null;
    return providers.map(([provider]) => {
      const d = voiceDraft[kind][provider] ?? { rate: '', cap: '' };
      const spent = voiceProviders?.[kind]?.[provider]?.spent_usd ?? 0;
      return (
        <div key={`${kind}-${provider}`} className="cost-row">
          <label className="cost-row__label">
            voice_{kind} · {provider}
          </label>
          <input
            type="number"
            min={0}
            step={0.001}
            value={d.rate}
            onChange={(e) =>
              setVoiceDraft((prev) => ({
                ...prev,
                [kind]: {
                  ...prev[kind],
                  [provider]: { ...d, rate: e.target.value },
                },
              }))
            }
            disabled={saving}
            className="cost-row__input"
            aria-label={`${provider} rate ${VOICE_RATE_LABEL[kind]}`}
            title={VOICE_RATE_LABEL[kind]}
          />
          <input
            type="number"
            min={0}
            step={0.1}
            value={d.cap}
            onChange={(e) =>
              setVoiceDraft((prev) => ({
                ...prev,
                [kind]: {
                  ...prev[kind],
                  [provider]: { ...d, cap: e.target.value },
                },
              }))
            }
            disabled={saving}
            className="cost-row__input"
            aria-label={`${provider} daily cap`}
            title="Daily cap USD"
          />
          <span className="cost-row__spend t-meta">
            spent {formatUsd(spent)}
          </span>
        </div>
      );
    });
  };

  return (
    <section className="settings-section">
      <h3 className="settings-section__title">Cost &amp; budgets</h3>
      <div className="settings-hint t-meta">
        Daily cap derives from the sum of every per-role and per-voice cap.
        Warning fires at the same percentage on every cap.
      </div>
      <div className="cost-row">
        <label className="cost-row__label">Daily cap</label>
        <input
          type="number"
          value={derivedDaily.toFixed(2)}
          disabled
          className="cost-row__input"
          title="Derived: sum of every per-role cap + every voice provider cap"
        />
        <span className="cost-row__spend t-meta">
          spent {formatUsd(globalState?.spent_usd ?? 0)}
        </span>
      </div>
      <div className="cost-row">
        <label className="cost-row__label" htmlFor="cost-warn-pct">Warning at %</label>
        <input
          id="cost-warn-pct"
          type="number"
          min={0}
          max={100}
          step={1}
          value={warnPctDraft}
          onChange={(e) => setWarnPctDraft(e.target.value)}
          disabled={saving}
          className="cost-row__input"
        />
        <span className="cost-row__spend t-meta">
          fires at {formatUsd(((parseFloat(warnPctDraft) || 0) / 100) * derivedDaily)}
        </span>
      </div>
      {perRoleKeys.map((role) => (
        <div key={role} className="cost-row">
          <label className="cost-row__label" htmlFor={`cost-${role}`}>
            {role}
          </label>
          <input
            id={`cost-${role}`}
            type="number"
            min={0}
            step={0.1}
            value={perRoleDraft[role] ?? ''}
            onChange={(e) =>
              setPerRoleDraft((prev) => ({ ...prev, [role]: e.target.value }))
            }
            disabled={saving}
            className="cost-row__input"
          />
          <span className="cost-row__spend t-meta">
            spent {formatUsd(perRoleSpend[role]?.role_total_usd ?? 0)}
          </span>
        </div>
      ))}
      {renderVoiceRows('tts')}
      {renderVoiceRows('stt')}
      <div className="cost-row cost-row--actions">
        <button
          type="button"
          className="cost-row__save"
          onClick={onSave}
          disabled={!dirty || saving}
        >
          {saving ? 'Saving…' : 'Save'}
        </button>
        {!costTracking.enabled && (
          <span className="t-meta">Cost tracking disabled in models.yaml.</span>
        )}
      </div>
      {error && <div className="settings-error">{error}</div>}
    </section>
  );
}
