import { useEffect, useState } from 'react';

import { Hint } from '../../components/ui/Hint';
import { postCompactThreshold } from '../../lib/api';
import type { IdentityCompactThreshold } from '../../lib/types';
import { useIdentityStore } from '../../stores/identity';

export function CompactSection() {
  const thresholds = useIdentityStore((s) => s.compactThresholds);
  const setCompactThreshold = useIdentityStore((s) => s.setCompactThreshold);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const chat = thresholds?.chat_brain ?? null;
  const [draftRatio, setDraftRatio] = useState<number>(chat?.ratio ?? 0.4);
  const [keepDraft, setKeepDraft] = useState<string>(
    chat?.keep_recent_turns != null ? String(chat.keep_recent_turns) : '10',
  );

  useEffect(() => {
    if (chat) setDraftRatio(chat.ratio);
  }, [chat]);

  useEffect(() => {
    if (chat) setKeepDraft(String(chat.keep_recent_turns ?? 10));
  }, [chat]);

  const commitRatio = async () => {
    if (!chat || draftRatio === chat.ratio) return;
    setSaving(true);
    setError(null);
    try {
      const res = await postCompactThreshold({ role: 'chat_brain', ratio: draftRatio });
      setCompactThreshold('chat_brain', res as IdentityCompactThreshold);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'compact-threshold update failed');
      setDraftRatio(chat.ratio);
    } finally {
      setSaving(false);
    }
  };

  const commitKeep = async () => {
    if (!chat) return;
    const next = parseInt(keepDraft, 10);
    if (!Number.isFinite(next) || next === chat.keep_recent_turns) {
      setKeepDraft(String(chat.keep_recent_turns));
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const res = await postCompactThreshold({ role: 'chat_brain', keep_recent_turns: next });
      setCompactThreshold('chat_brain', res as IdentityCompactThreshold);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'keep_recent_turns update failed');
      setKeepDraft(String(chat.keep_recent_turns));
    } finally {
      setSaving(false);
    }
  };

  const draftTokens = chat ? Math.round(draftRatio * chat.context_window) : 0;

  return (
    <section className="settings-section">
      <h3 className="settings-section__title">Compaction</h3>
      <div className="compact-row">
        <span className="compact-row__role">chat_brain</span>
        <input
          type="range"
          min={0.10}
          max={0.95}
          step={0.01}
          value={draftRatio}
          onChange={(e) => setDraftRatio(parseFloat(e.target.value))}
          onMouseUp={commitRatio}
          onTouchEnd={commitRatio}
          onKeyUp={(e) => { if (e.key === 'Enter') commitRatio(); }}
          disabled={!chat || saving}
          className="compact-row__slider"
          aria-label="chat_brain compact threshold"
        />
        <span className="compact-row__ratio">{(draftRatio * 100).toFixed(0)}%</span>
        <span className="compact-row__tokens t-meta">→ {draftTokens.toLocaleString()} tok</span>
      </div>
      <div className="compact-row compact-row--keep">
        <span className="compact-row__role">keep_recent_turns</span>
        <Hint
          label="How many recent messages survive untouched when history is compacted."
          maxWidth={360}
        >
          <input
            type="number"
            min={2}
            max={200}
            step={1}
            value={keepDraft}
            onChange={(e) => setKeepDraft(e.target.value)}
            onBlur={commitKeep}
            onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur(); }}
            disabled={!chat || saving}
            className="cost-row__input"
            aria-label="chat_brain keep_recent_turns"
          />
        </Hint>
        <span className="t-meta">turns kept verbatim</span>
      </div>
      <div className="compact-row compact-row--disabled">
        <span className="compact-row__role">observer_agent</span>
        <span className="t-meta">Fixed reset on arm/disarm — no compaction.</span>
      </div>
      <div className="settings-hint t-meta">
        Edits the primary chat_brain entry only — fallback models keep their per-model tuning.
        For deeper knobs, edit `tesseract/config/models.yaml` directly.
      </div>
      {error && <div className="settings-error">{error}</div>}
    </section>
  );
}
