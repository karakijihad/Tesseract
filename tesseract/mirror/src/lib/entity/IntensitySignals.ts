import type { EntityState } from '../types';

/**
 * Layer 1 (s8) + Layer 2 (s9) activity signals for intensity modulation.
 * Plain class (not a store) — polled from EntityController's rAF loop.
 *
 * Layer 1: three frontend EMAs (tokenRate / typingRate / activityRate) fed
 * by WS events (text_delta, turn_start, step_started) and ChatInput keystrokes.
 *
 * Layer 2: backend `entity_signals` envelope with per-session truth that
 * the frontend cannot observe — agents_active, consolidation_depth,
 * tokens_per_sec (at generator level), dreaming_cycle, mood_intensity,
 * mood_valence. Ingested via ingestBackend(); backend fields go stale
 * (treated as 0) if the envelope hasn't arrived within 2000ms.
 */

export interface EntitySignalsPayload {
  v?: number;
  agents_active?: number;
  consolidation_depth?: number;
  tokens_per_sec?: number;
  dreaming_cycle?: number | null;
  mood_intensity?: number;
  mood_valence?: number;
  effort_level?: number;  // 0.0 (off) to 1.0 (max)
}

// 3000ms, not 2000, so the idle pump's 2000ms cadence always lands inside the
// freshness window with jitter margin. Staleness==pump-rate leaves the last
// frame before each pump exactly at the boundary and produces mood dropout.
const BACKEND_STALENESS_MS = 3000;

export class IntensitySignals {
  private tokenRate = 0;
  private typingRate = 0;
  private activityRate = 0;

  private lastStepMs = 0;
  private lastErrorMs = -Infinity;

  // k·N ≈ 180 for tokens so a reflective calm chunk stream (~60 chars/s) lands
  // near i≈0.35 and an urgent burst (~180 chars/s) saturates at i≈1.0. Decay
  // of 5/s keeps the EMA responsive (τ≈200ms) without over-smoothing.
  private readonly TOKEN_DECAY = 5;
  private readonly TYPING_DECAY = 3;
  private readonly ACTIVITY_DECAY = 1;

  private readonly TOKEN_NORM = 36;
  private readonly TYPING_NORM = 8;
  private readonly ACTIVITY_NORM = 2;

  // Backend fields (Layer 2). All default to "empty" until an envelope arrives.
  private backendTokensPerSec = 0;
  private backendAgents = 0;
  private backendConsolidation = 0;
  private backendMoodIntensity = 0.5;
  private backendMoodValence = 0;
  private backendDreamingCycle: number | null = null;
  private backendEffortLevel = 0.5;  // default medium
  private lastBackendMs = -Infinity;

  onTextDelta(charCount: number): void {
    this.tokenRate += charCount;
  }

  onUserInput(charsAdded: number): void {
    if (charsAdded > 0) this.typingRate += charsAdded;
  }

  onStep(): void {
    this.activityRate += 1;
    this.lastStepMs = performance.now();
  }

  onError(): void {
    this.lastErrorMs = performance.now();
  }

  onReset(): void {
    this.tokenRate = 0;
    this.typingRate = 0;
    this.activityRate = 0;
    this.lastStepMs = 0;
    this.backendTokensPerSec = 0;
    this.backendAgents = 0;
    this.backendConsolidation = 0;
    this.backendDreamingCycle = null;
    this.backendEffortLevel = 0.5;
    // mood_intensity / mood_valence are sticky — preserved across reconnects
    // so a quick WS blip doesn't reset the entity's chosen affect.
  }

  ingestBackend(payload: EntitySignalsPayload): void {
    this.lastBackendMs = performance.now();
    if (typeof payload.tokens_per_sec === 'number') this.backendTokensPerSec = payload.tokens_per_sec;
    if (typeof payload.agents_active === 'number') this.backendAgents = payload.agents_active;
    if (typeof payload.consolidation_depth === 'number') this.backendConsolidation = payload.consolidation_depth;
    if (typeof payload.mood_intensity === 'number') this.backendMoodIntensity = clamp01(payload.mood_intensity);
    if (typeof payload.mood_valence === 'number') this.backendMoodValence = Math.max(-1, Math.min(1, payload.mood_valence));
    if (typeof payload.effort_level === 'number') this.backendEffortLevel = clamp01(payload.effort_level);
    this.backendDreamingCycle = payload.dreaming_cycle ?? null;
  }

  /** Valence [-1, +1], zero if backend is stale. Used by hue coupling. */
  getValence(): number {
    if (!this._backendFresh()) return 0;
    return this.backendMoodValence;
  }

  /** Current dreaming cycle number, or null. Null if backend is stale. */
  getDreamingCycle(): number | null {
    if (!this._backendFresh()) return null;
    return this.backendDreamingCycle;
  }

  /** Current effort level 0..1. Returns 0.5 (medium) if backend is stale. */
  getEffortLevel(): number {
    if (!this._backendFresh()) return 0.5;
    return this.backendEffortLevel;
  }

  private _backendFresh(): boolean {
    return (performance.now() - this.lastBackendMs) <= BACKEND_STALENESS_MS;
  }

  /** Apply exponential decay to all rates. Call every frame with dt in seconds. */
  tick(dt: number): void {
    if (dt <= 0) return;
    this.tokenRate *= Math.exp(-this.TOKEN_DECAY * dt);
    this.typingRate *= Math.exp(-this.TYPING_DECAY * dt);
    this.activityRate *= Math.exp(-this.ACTIVITY_DECAY * dt);
  }

  /** Compute 0..1 intensity for the active state. */
  computeIntensity(state: EntityState): number {
    const nowMs = performance.now();
    const tSec = nowMs / 1000;
    const fresh = this._backendFresh();
    const bAgents = fresh ? this.backendAgents : 0;
    const bTps = fresh ? this.backendTokensPerSec : 0;
    const bConsol = fresh ? this.backendConsolidation : 0;
    const bMood = fresh ? this.backendMoodIntensity : 0.5;
    const bDream = fresh ? (this.backendDreamingCycle ?? 0) : 0;

    switch (state) {
      case 'speaking': {
        const frontend = this.tokenRate / this.TOKEN_NORM;
        const backend = bTps / 20;
        return clamp01(Math.max(frontend, backend));
      }
      case 'listening': {
        return clamp01(this.typingRate / this.TYPING_NORM);
      }
      case 'thinking': {
        const base = this.activityRate / this.ACTIVITY_NORM;
        const sinceStepSec = (nowMs - this.lastStepMs) / 1000;
        const freshness = this.lastStepMs > 0 ? 0.3 * Math.exp(-sinceStepSec / 2) : 0;
        const agentWeight = bAgents / 4;
        const effortBoost = fresh ? this.backendEffortLevel * 0.3 : 0;
        return clamp01(base + freshness + agentWeight + effortBoost);
      }
      case 'spawning': {
        const frontend = this.activityRate / this.ACTIVITY_NORM;
        const backend = bAgents / 3;
        const effortBoost = fresh ? this.backendEffortLevel * 0.2 : 0;
        return clamp01(Math.max(frontend, backend) + effortBoost);
      }
      case 'error': {
        const sinceErrorSec = (nowMs - this.lastErrorMs) / 1000;
        return clamp01(0.8 + 0.2 * Math.exp(-sinceErrorSec / 3));
      }
      case 'deep_focus': {
        return clamp01(0.2 + 0.2 * Math.sin(tSec * 0.13) + bConsol * 0.1);
      }
      case 'idle': {
        const dreamDrift = bDream > 0 ? 0.15 * Math.sin(tSec * 0.08) : 0;
        return clamp01(0.3 + 0.15 * Math.sin(tSec * 0.17 + 1.3) + dreamDrift);
      }
      case 'dreaming': {
        // Depth-driven with gentle breathing. Cycle depth 0..5+ → 0..0.4.
        const depth = Math.min(1, bDream / 5);
        return clamp01(0.3 + 0.4 * depth + 0.15 * Math.sin(tSec * 0.08));
      }
      case 'council':
      case 'happy': {
        // Mood is the signal here. Low mood → still present but subdued.
        // Layer 1 fallback: when the backend is stale, drift on a gentle
        // oscillator so the state is still visibly alive. Without this,
        // `bMood` falls back to 0.5 and the state renders as a constant.
        if (!fresh) return clamp01(0.45 + 0.1 * Math.sin(tSec * 0.19));
        return clamp01(0.3 + 0.7 * bMood);
      }
      default:
        return 0.5;
    }
  }
}

/**
 * Couple valence ∈ [-1, +1] into a hue shift delta (degrees). Positive = warm,
 * negative = cool. ±15° is subtle but visible against state hueShifts in the
 * -160..+30 range.
 */
export function valenceToHueDelta(valence: number): number {
  const v = Math.max(-1, Math.min(1, valence));
  return v * 15;
}

function clamp01(x: number): number {
  if (x < 0) return 0;
  if (x > 1) return 1;
  return x;
}
