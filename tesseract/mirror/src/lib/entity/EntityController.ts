import type { EntityState } from '../types';
import type { ParticleSystem } from './ParticleSystem';
import type { AmbientHaze } from './AmbientHaze';
import { ENTITY_STATES } from './states';
import { IntensitySignals, valenceToHueDelta } from './IntensitySignals';
import { useEntityStore } from '../../stores/entity';

type EntityMode = 'full' | 'corner';

export type PopKind = 'success' | 'error' | 'cli_ok' | 'user';

interface FrameBudget {
  samples: number[];
  maxSamples: number;
}

/** Tokens-mirrored hex (no CSS access from canvas). Keep in sync with `tokens.css`. */
const POP_PALETTE: Record<PopKind, { amp: number; hex: string }> = {
  success: { amp: 0.5, hex: '#9d8fff' }, // --accent-bright (tokens.css)
  error:   { amp: 0.85, hex: '#ff6b6b' }, // --bad
  cli_ok:  { amp: 0.4, hex: '#4ade80' }, // --ok
  user:    { amp: 0.3, hex: '#7c6cf0' }, // --accent (tokens.css)
};

/** State machine + rAF loop. Owns the animation lifecycle. */
export class EntityController {
  private particleSystem!: ParticleSystem;
  private ambientHaze!: AmbientHaze;

  private rafId: number | null = null;
  private mode: EntityMode = 'full';

  private currentState: EntityState = 'idle';
  private isDreaming = false;
  private accentHsl = '246 83% 68%';

  /** Lerp target particle count (smooth transition) */
  private targetCount = 3500;
  private currentCount = 3500;

  /** Smooth interpolation targets */
  private _currentCoherence = 0.3;
  private _targetCoherence = 0.3;
  private _currentRadialBias = 0.0;
  private _targetRadialBias = 0.0;
  private _currentWaveAmp = 0.0;
  private _targetWaveAmp = 0.0;
  private _waveSpeed = 0.0;
  private _currentCurl = 0.12;
  private _targetCurl = 0.12;
  private _currentShellOpenness = 0.7;
  private _targetShellOpenness = 0.7;
  private _currentVortexStrength = 0.4;
  private _targetVortexStrength = 0.4;
  private _targetVortexCount = 4;
  private _currentRadius = 1.2;
  private _targetRadius = 1.2;
  private _prevLoopTs = 0;

  /** Lerp pairs for the four params that used to snap-cut on state change.
   *  `_target*` is written by `_applyState`; `_current*` is eased toward
   *  the target each frame in `_stepMotionParams` at rate ~2.5 (≈400ms
   *  to 86%), matching the existing exponential lerps. Without this,
   *  pulseHz / breathHz / breathAmp / hueShift jumped at each state
   *  flip — visible as a click on rapid intent → tool → answer turns. */
  private _currentPulseHz = 0.3;
  private _targetPulseHz = 0.3;
  private _currentBreathHz = 0.15;
  private _targetBreathHz = 0.15;
  private _currentBreathAmp = 0.04;
  private _targetBreathAmp = 0.04;
  /** Hue rotation. Linearly interpolated; deltas across the configured
   *  states (max ~150° span) never cross the 0/360 wrap. */
  private _currentHueShift = 0;
  private _targetHueShift = 0;
  /** Base snapshots written by _applyState, multiplied per-frame by intensity */
  private _baseEruptionRate = 0.3;
  private _baseEruptionStrength = 2.5;
  private _baseVortexDrift = 0.03;
  /** Per-state base haze intensity. Live haze = base · (1 - intensity·0.4). */
  private _baseHazeIntensity = 0.108;
  private _startMs = 0;

  /** Wobble amplitude (lerps to 1 in 'thinking', 0 elsewhere). 0.4Hz sin in render. */
  private _wobbleAmp = 0;
  /** Reaction pop transient amplitude. Decays at ~4/s (~250ms half-life). */
  private _popAmp = 0;
  private _popHex = '#7c6cf0';
  /** Reduced-motion media query result. Cached at init; pop/wobble/sparkles
   *  collapse to opacity-only blip + no scale + capped sparkles when set. */
  private _reducedMotion = false;
  /** Low-passed intensity. Smoothes the spiky per-chunk impulses that
   *  `computeIntensity` produces before they drive breath/wave/eruption
   *  amplitudes — a stepwise jump in those amplitudes reads as flicker.
   *  Initialised at the idle baseline (~0.4) so the orb resumes at resting
   *  energy on boot instead of ramping up from 0 over half a second. */
  private _smoothIntensity = 0.4;

  private signals = new IntensitySignals();

  /** Unsubscribe handle from `useEntityStore.subscribe` — MUST be invoked
   *  in `dispose()` or a disposed controller keeps receiving store updates
   *  (memory leak on hot-reload / navigation re-mounts). */
  private _unsubEntity: (() => void) | null = null;

  private budget: FrameBudget = { samples: [], maxSamples: 60 };
  private gpuCheckTs = 0;

  /** CSS fallback div injected when GPU% > 90 */
  private cssFallback: HTMLElement | null = null;
  private canvasEl: HTMLCanvasElement | null = null;

  init(particleSystem: ParticleSystem, ambientHaze: AmbientHaze, canvas: HTMLCanvasElement): void {
    this.particleSystem = particleSystem;
    this.ambientHaze = ambientHaze;
    this.canvasEl = canvas;

    this._startMs = performance.now();
    this._reducedMotion = typeof window !== 'undefined'
      && typeof window.matchMedia === 'function'
      && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    this._subscribeStores();
    this._applyState('idle');
    this._startRAF();
  }

  /** Trigger a transient reaction pop. Idempotent: takes the larger amplitude
   *  if a pop is already in flight, so a quick error-then-success doesn't
   *  cancel the error visual. */
  pulseEvent(kind: PopKind): void {
    const cfg = POP_PALETTE[kind];
    const target = this._reducedMotion ? cfg.amp * 0.4 : cfg.amp;
    if (target > this._popAmp) {
      this._popAmp = target;
      this._popHex = cfg.hex;
    }
  }

  getSignals(): IntensitySignals {
    return this.signals;
  }

  setMode(mode: EntityMode): void {
    if (this.mode === mode) return;
    this.mode = mode;
    this._applyState(this.currentState);
  }

  resizeCanvas(w: number, h: number): void {
    this.particleSystem.resize(w, h);
  }

  private _subscribeStores(): void {
    this._unsubEntity = useEntityStore.subscribe((s) => {
      if (s.state !== this.currentState) {
        this.currentState = s.state;
        this._applyState(s.state);
      }
      if (s.dreamingInFlight !== this.isDreaming) {
        this.isDreaming = s.dreamingInFlight;
        this.ambientHaze.setDreaming(s.dreamingInFlight);
      }
      if (s.accentHsl !== this.accentHsl) {
        this.accentHsl = s.accentHsl;
        this.particleSystem.setAccentHsl(s.accentHsl);
        this.ambientHaze.setAccentHsl(s.accentHsl);
      }
    });
  }

  private _applyState(state: EntityState): void {
    const cfg = ENTITY_STATES[state];
    // Lerp targets — the per-frame loop eases _current* toward these,
    // and pushes _current* values onto the particle system / haze.
    // No direct hueShift / pulseHz / breath writes here — those would
    // re-introduce the snap-cut.
    this._targetPulseHz = cfg.pulseHz;
    this._targetHueShift = cfg.hueShift;
    this._targetBreathHz = cfg.breathHz;
    this._targetBreathAmp = cfg.breathAmp;
    this.particleSystem.setAccentHsl(this.accentHsl);
    this.particleSystem.setPulseShape(cfg.pulseShape);
    this._targetCoherence = cfg.coherenceFactor;
    this._targetRadialBias = cfg.radialBias;
    this._targetWaveAmp = cfg.waveAmp;
    this._waveSpeed = cfg.waveSpeed;
    this._targetCurl = cfg.curlStrength;
    this._targetShellOpenness = cfg.shellOpenness;
    this._targetVortexStrength = cfg.vortexStrength;
    this._targetVortexCount = cfg.vortexCount;
    this._baseVortexDrift = cfg.vortexDrift;
    this._targetRadius = cfg.entityRadius;
    this._baseEruptionRate = cfg.eruptionRate;
    this._baseEruptionStrength = cfg.eruptionStrength;
    this.ambientHaze.setAccentHsl(this.accentHsl);
    this.ambientHaze.setHazeColor(cfg.hazeColor);
    this._baseHazeIntensity = cfg.hazeIntensity;
    this.ambientHaze.setIntensity(cfg.hazeIntensity);
    this._updateTargetCount();
  }

  private _baseParticleCount(): number {
    const cfg = ENTITY_STATES[this.currentState];
    return cfg.particlesFull;
  }

  private _updateTargetCount(): void {
    this.targetCount = this._baseParticleCount();
  }

  private _stepMotionParams(dt: number): void {
    this.signals.tick(dt);
    const rawIntensity = this.signals.computeIntensity(this.currentState);

    // Low-pass. rate=3 → τ≈330ms. Fast enough to track conversation energy,
    // slow enough to dissolve the per-chunk impulses from `onTextDelta`
    // before they become step-jumps in breathAmp / waveAmp / pulseHz.
    this._smoothIntensity += (rawIntensity - this._smoothIntensity) * 3 * dt;
    const intensity = this._smoothIntensity;

    const pulseM = 0.5 + intensity;
    const breathM = 0.7 + intensity * 0.6;
    const waveM = 0.5 + intensity;
    const eruptM = this._baseEruptionRate > 0 ? Math.min(0.5 + intensity * 0.8, 1.3) : 0;
    const driftM = 0.6 + intensity * 0.8;

    const rate = 2.5;
    this._currentCoherence += (this._targetCoherence - this._currentCoherence) * rate * dt;
    this._currentRadialBias += (this._targetRadialBias - this._currentRadialBias) * rate * dt;
    this._currentWaveAmp += (this._targetWaveAmp - this._currentWaveAmp) * rate * dt;
    this._currentCurl += (this._targetCurl - this._currentCurl) * rate * dt;
    this._currentShellOpenness += (this._targetShellOpenness - this._currentShellOpenness) * rate * dt;
    this._currentVortexStrength += (this._targetVortexStrength - this._currentVortexStrength) * rate * dt;
    this._currentRadius += (this._targetRadius - this._currentRadius) * rate * dt;
    this._currentPulseHz += (this._targetPulseHz - this._currentPulseHz) * rate * dt;
    this._currentBreathHz += (this._targetBreathHz - this._currentBreathHz) * rate * dt;
    this._currentBreathAmp += (this._targetBreathAmp - this._currentBreathAmp) * rate * dt;
    this._currentHueShift += (this._targetHueShift - this._currentHueShift) * rate * dt;

    // s9 signature energy vectors — per-state boosts applied on top of the
    // s8 global energy split. Each boost is gated on its home state so the
    // global rule ("character params don't scale") is preserved for every
    // state that doesn't opt in.
    const cfg = ENTITY_STATES[this.currentState];
    let curlFinal = this._currentCurl;
    let vortexStrengthFinal = this._currentVortexStrength;
    let hueFinal = this._currentHueShift;

    if (this.currentState === 'thinking' && cfg.vortexStrengthBoostScale) {
      vortexStrengthFinal = this._currentVortexStrength * (1 + intensity * cfg.vortexStrengthBoostScale);
    }
    if (this.currentState === 'error' && cfg.curlStrengthBoostScale) {
      curlFinal = this._currentCurl * (1 + intensity * cfg.curlStrengthBoostScale);
    }
    if (this.currentState === 'dreaming' && cfg.hueDriftHz) {
      const tSec = (performance.now() - this._startMs) / 1000;
      hueFinal = this._currentHueShift + Math.sin(tSec * cfg.hueDriftHz * Math.PI * 2) * 10;
    }

    // s9 valence → hue delta. Global, small (±15°), re-applied every frame
    // so the `_applyState` write is always superseded with the live value.
    const valence = this.signals.getValence();
    hueFinal += valenceToHueDelta(valence);

    this.particleSystem.pulseHz = this._currentPulseHz * pulseM;
    this.particleSystem.setBreath(this._currentBreathHz, this._currentBreathAmp * breathM);
    this.particleSystem.setCoherence(this._currentCoherence);
    this.particleSystem.setRadialBias(this._currentRadialBias);
    this.particleSystem.setWave(this._currentWaveAmp * waveM, this._waveSpeed);
    this.particleSystem.setCurlStrength(curlFinal);
    this.particleSystem.setShellOpenness(this._currentShellOpenness);
    this.particleSystem.setIdealRadius(this._currentRadius);
    this.particleSystem.setEruptions(this._baseEruptionRate * eruptM, this._baseEruptionStrength);
    this.particleSystem.setValence(valence);

    if (this.particleSystem.hueShift !== hueFinal) {
      this.particleSystem.hueShift = hueFinal;
      this.particleSystem.setAccentHsl(this.accentHsl);
      this.ambientHaze.setHueShift(hueFinal);
    }

    // Vignette modulation: high intensity → sharper haze; calm states → softer.
    // 40% reduction at peak intensity. AmbientHaze.setIntensity early-returns
    // on sub-perceptual deltas so frame-rate redraw is not a perf issue.
    this.ambientHaze.setIntensity(this._baseHazeIntensity * (1 - intensity * 0.4));

    // Wobble: low-frequency sine while 'thinking', off elsewhere. Reduced-motion
    // locks the target to 0 (no scale/translate, only color blip remains).
    const wobbleTarget = (!this._reducedMotion && this.currentState === 'thinking') ? 1 : 0;
    this._wobbleAmp += (wobbleTarget - this._wobbleAmp) * 4 * dt;
    if (this._wobbleAmp > 0.001) {
      const tSec = (performance.now() - this._startMs) / 1000;
      const w = tSec * Math.PI * 2 * 0.4;
      const wobbleX = Math.sin(w) * this._wobbleAmp * 0.05;
      const wobbleY = Math.sin(w + 1.3) * this._wobbleAmp * 0.05;
      this.particleSystem.setWobble(wobbleX, wobbleY);
    } else if (this._wobbleAmp !== 0) {
      this.particleSystem.setWobble(0, 0);
      this._wobbleAmp = 0;
    }

    // Pop decay — linear, ~250ms half-life at amp=1.
    if (this._popAmp > 0) {
      this._popAmp = Math.max(0, this._popAmp - dt * 4);
      this.particleSystem.setPopVisual(this._popAmp, this._popHex);
    }

    this.particleSystem.getVortexField().setParams({
      count: this._targetVortexCount,
      strength: vortexStrengthFinal,
      drift: this._baseVortexDrift * driftM,
    });
  }

  private _startRAF(): void {
    const loop = (tNow: number) => {
      const t0 = performance.now();
      const dt = this._prevLoopTs > 0 ? Math.min((tNow - this._prevLoopTs) / 1000, 0.05) : 0.016;
      this._prevLoopTs = tNow;

      this._checkGpuDegradation(tNow);
      this._stepParticleCount();
      this._stepMotionParams(dt);
      this.ambientHaze.tick(tNow);
      this.particleSystem.render(tNow);

      const elapsed = performance.now() - t0;
      this._recordFrame(elapsed);

      this.rafId = requestAnimationFrame(loop);
    };
    this.rafId = requestAnimationFrame(loop);
  }

  private _stepParticleCount(): void {
    if (this.currentCount === this.targetCount) return;
    const delta = this.targetCount - this.currentCount;
    const step = Math.sign(delta) * Math.min(Math.abs(delta), 150);
    this.currentCount += step;
    this.particleSystem.setParticleCount(this.currentCount);
  }

  private _recordFrame(ms: number): void {
    const { samples, maxSamples } = this.budget;
    samples.push(ms);
    if (samples.length > maxSamples) samples.shift();
    if (samples.length === maxSamples) {
      const avg = samples.reduce((a, b) => a + b, 0) / maxSamples;
      // 16ms = 60 FPS standard. The previous 8ms budget tripped on
      // anything that wasn't a 120 Hz monitor and produced log spam.
      if (avg > 16) {
        console.debug(`[entity] avg frame ${avg.toFixed(1)}ms > 16ms budget`);
        this.budget.samples = [];
      }
    }
  }

  private _checkGpuDegradation(tNow: number): void {
    if (tNow - this.gpuCheckTs < 3000) return;
    this.gpuCheckTs = tNow;
    this._exitCssFallback();
    this._updateTargetCount();
  }

  private _exitCssFallback(): void {
    if (!this.cssFallback || !this.canvasEl) return;
    this.canvasEl.style.display = '';
    this.cssFallback.remove();
    this.cssFallback = null;
  }

  /** Halt the rAF loop without tearing down GL state — used when the orb is
   *  hidden (HUD toggle) or the tab is backgrounded. A `display:none` canvas
   *  still burns GPU/CPU if the loop keeps running; pausing is the only way
   *  to actually stop the draw calls. State (particles, lerp targets, store
   *  subscriptions) is kept so resume is seamless. */
  pause(): void {
    if (this.rafId !== null) {
      cancelAnimationFrame(this.rafId);
      this.rafId = null;
    }
  }

  /** Restart the rAF loop after `pause()`. Resets frame timing so the first
   *  resumed frame doesn't integrate the entire paused span as one dt. */
  resume(): void {
    if (this.rafId !== null) return;
    this._prevLoopTs = 0;
    this._startRAF();
  }

  dispose(): void {
    this._unsubEntity?.();
    this._unsubEntity = null;
    if (this.rafId !== null) {
      cancelAnimationFrame(this.rafId);
      this.rafId = null;
    }
    this._exitCssFallback();
    this.particleSystem.dispose();
    this.ambientHaze.dispose();
  }
}

export type { EntityMode };
