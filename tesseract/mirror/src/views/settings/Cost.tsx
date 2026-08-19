import { Note } from '../../components/common/Note';
import { useEffect, useMemo, useState } from 'react';

import { postCostSettings, postResetDefaults, postVoiceCostSettings } from '../../lib/api';
import { ResetDefaults } from '../../components/common/ResetDefaults';
import { useCostStore } from '../../stores/cost';
import { useIdentityStore } from '../../stores/identity';
import { formatUsd } from '../../lib/money';
import { Hint } from '../../components/ui/Hint';
import { Button } from '../../components/common/Button';
import { Input } from '../../components/common/Input';

type VoiceKind = 'tts' | 'stt';

// The unit belongs to the LANE, not to the side of the subsystem. A local
// voice bills per character of text; a generative cloud one bills per
// second of the speech it produced. Labelling the second as dollars per
// million characters does not read as merely wrong — $2.30 shown against
// "1M chars" reads as free.
const VOICE_RATE_LABEL: Record<'chars' | 'audio_hour', string> = {
  chars: '$ / 1M chars',
  audio_hour: '$ / audio-hour',
};

const DEFAULT_RATE_UNIT: Record<VoiceKind, 'chars' | 'audio_hour'> = {
  tts: 'chars',
  stt: 'audio_hour',
};

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
  // The daily cap only. The RATE is a catalog fact — what the provider
  // charges — not something this pane sets, and offering an input for it made
  // every voice row the odd one out on a screen where every other row is one
  // cap. It also let a typo write a fabricated price into the catalog that the
  // ledger then billed against.
  const [voiceDraft, setVoiceDraft] = useState<Record<VoiceKind, Record<string, string>>>({
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
        Object.entries(voice.tts).map(([provider, p]) => [provider, String(p.cap_usd)]),
      ),
      stt: Object.fromEntries(
        Object.entries(voice.stt).map(([provider, p]) => [provider, String(p.cap_usd)]),
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
        (sub, cap) => sub + (parseFloat(cap) || 0),
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
          const cap = voiceDraft[kind][provider];
          if (cap === undefined) continue;
          if (parseFloat(cap) !== p.cap_usd) return true;
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
      // The cap ONLY. Both rate fields are optional server-side, so leaving
      // them out means the catalog price is the one thing this pane cannot
      // change — which is the point: a rate is what the provider charges, not
      // an operator preference, and the ledger bills against whatever is
      // written there.
      const voicePayload: {
        tts: Record<string, Record<string, number>>;
        stt: Record<string, Record<string, number>>;
      } = { tts: {}, stt: {} };
      let voiceDirty = false;
      const voice = costTracking.voice ?? { tts: {}, stt: {} };
      for (const kind of ['tts', 'stt'] as VoiceKind[]) {
        for (const [provider, p] of Object.entries(voice[kind])) {
          const raw = voiceDraft[kind][provider];
          if (raw === undefined) continue;
          const cap = parseFloat(raw);
          if (!Number.isFinite(cap) || cap <= 0) {
            throw new Error(`${kind} ${provider}: daily cap must be > 0`);
          }
          if (cap === p.cap_usd) continue;
          voiceDirty = true;
          voicePayload[kind][provider] = { daily_budget_usd: cap };
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
    return providers.map(([provider, cfg]) => {
      const cap = voiceDraft[kind][provider] ?? '';
      const spent = voiceProviders?.[kind]?.[provider]?.spent_usd ?? 0;
      const rateLabel =
        VOICE_RATE_LABEL[cfg.rate_unit ?? DEFAULT_RATE_UNIT[kind]];
      const inputId = `cost-voice-${kind}-${provider}`;
      return (
        <div key={`${kind}-${provider}`} className="cost-row">
          {/* One label, one cap, one spend — the same three columns as every
              row above. The price the provider charges rides under the name as
              a fact, because it explains what the cap buys and is not
              something set here. */}
          <label className="cost-row__label" htmlFor={inputId}>
            voice_{kind} · {provider}
            <span className="cost-row__rate t-meta">
              {cfg.rate} {rateLabel}
            </span>
          </label>
          <Input
            id={inputId}
            type="number"
            min={0}
            step={0.1}
            value={cap}
            onChange={(next) =>
              setVoiceDraft((prev) => ({
                ...prev,
                [kind]: { ...prev[kind], [provider]: next },
              }))
            }
            disabled={saving}
            className="cost-row__input"
            ariaLabel={`${provider} daily cap`}
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
      <Note>
        Daily cap derives from the sum of every per-role and per-voice cap.
        Warning fires at the same percentage on every cap.
      </Note>
      <div className="cost-row">
        <label className="cost-row__label">Daily cap</label>
        <Hint label="Derived: sum of every per-role cap + every voice provider cap">
          <Input
            type="number"
            value={derivedDaily.toFixed(2)}
            onChange={() => {}}
            disabled
            className="cost-row__input"
            ariaLabel="Daily cap, derived"
          />
        </Hint>
        <span className="cost-row__spend t-meta">
          spent {formatUsd(globalState?.spent_usd ?? 0)}
        </span>
      </div>
      <div className="cost-row">
        <label className="cost-row__label" htmlFor="cost-warn-pct">Warning at %</label>
        <Input
          id="cost-warn-pct"
          type="number"
          min={0}
          max={100}
          step={1}
          value={warnPctDraft}
          onChange={setWarnPctDraft}
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
          <Input
            id={`cost-${role}`}
            type="number"
            min={0}
            step={0.1}
            value={perRoleDraft[role] ?? ''}
            onChange={(next) =>
              setPerRoleDraft((prev) => ({ ...prev, [role]: next }))
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
        <Button
          onClick={onSave}
          disabled={!dirty || saving}
          tone="primary"
        >
          {saving ? 'Saving…' : 'Save'}
        </Button>
        <ResetDefaults
          run={() => postResetDefaults('cost')}
          reach="every budget and rate on this pane"
          // The pane renders `costTracking` off the identity payload, which
          // carries the per-role caps and the voice rates alike.
          onDone={() => void useIdentityStore.getState().fetchIdentity()}
        />
        {!costTracking.enabled && (
          <span className="t-meta">Cost tracking disabled in models.yaml.</span>
        )}
      </div>
      {error && <Note tone="bad">{error}</Note>}
    </section>
  );
}
