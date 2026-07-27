import { useCostStore } from '../../../stores/cost';
import { Hint } from '../../ui/Hint';
import { colorBand, formatUsd } from './CostChip';

/**
 * Voice cost chip — Phase 16 S3.
 *
 * Displays combined `voice_tts + voice_stt` daily spend. The band is
 * driven off the shared global ceiling (same `globalState.cap_usd` as
 * the chat chip) — there's no per-voice sub-cap surfaced today. After
 * the G2 cutover (2026-04-26) both lanes are cloud-only Gemini, so a
 * provider-cap hit raises BudgetExhausted server-side and the operator
 * sees a `voice_instruction` toast rather than a chip-level block.
 * Empty state matches the chat chip before the first billed turn.
 */
export function VoiceCostChip() {
  const tts = useCostStore((s) => s.perRole['voice_tts']);
  const stt = useCostStore((s) => s.perRole['voice_stt']);
  const globalState = useCostStore((s) => s.globalState);
  const overageUnlocked = useCostStore((s) => s.overageUnlocked);
  const voiceProviders = useCostStore((s) => s.voiceProviders);

  const ttsTotal = tts?.role_total_usd ?? 0;
  const sttTotal = stt?.role_total_usd ?? 0;
  const total = ttsTotal + sttTotal;

  if ((!tts && !stt) || !globalState) {
    return (
      <Hint
        label="Voice cost — no billed voice turns yet today"
        position="top"
        maxWidth={240}
      >
        <span className="hud-stats hud-stats--empty" aria-label="No voice cost yet">
          <span className="hud-stats-text">voice —</span>
          <span className="hud-stats-bar hud-stats-bar--empty" aria-hidden="true" />
        </span>
      </Hint>
    );
  }

  const globalRatio = globalState.cap_usd > 0
    ? globalState.spent_usd / globalState.cap_usd
    : 0;
  const ratio = Math.min(Math.max(globalRatio, 0), 1);
  // Cost UX overhaul — render in red overage state when any voice
  // scope (provider OR rolled-up global) is over-cap with an active
  // approval. Snapshot's `voice_providers` carries per-provider spent
  // vs cap; check whichever was unlocked.
  const anyVoiceOverage = (() => {
    if (overageUnlocked.includes('global') && globalState.spent_usd > globalState.cap_usd) {
      return true;
    }
    if (!voiceProviders) return false;
    for (const kind of ['tts', 'stt'] as const) {
      const providers = voiceProviders[kind] ?? {};
      for (const [provider, p] of Object.entries(providers)) {
        const key = `voice:${kind}:${provider}`;
        if (overageUnlocked.includes(key) && p.cap_usd !== null && p.spent_usd > p.cap_usd) {
          return true;
        }
      }
    }
    return false;
  })();
  const band = globalState.blocked ? 'bad' : colorBand(ratio);

  const hint =
    `Voice ${formatUsd(total)} (TTS ${formatUsd(ttsTotal)} · STT ${formatUsd(sttTotal)}) · ` +
    `global ${formatUsd(globalState.spent_usd)} / ${formatUsd(globalState.cap_usd)}` +
    (anyVoiceOverage ? ' · OVERAGE (you approved)' : globalState.blocked ? ' · BLOCKED' : globalState.warning ? ' · warning' : '');

  return (
    <Hint label={hint} position="top" maxWidth={320}>
      <span className={`hud-stats hud-stats--${band}${anyVoiceOverage ? ' is-overage' : ''}`} aria-label={hint}>
        <span className="hud-stats-text">
          voice {formatUsd(total)}
        </span>
        <span className="hud-stats-bar" aria-hidden="true">
          <span
            className="hud-stats-fill hud-stats-fill--voice"
            style={{ width: `${Math.round(ratio * 100)}%` }}
          />
        </span>
      </span>
    </Hint>
  );
}
