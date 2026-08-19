import type { EntityState } from '../types';

export interface EntityStateConfig {
  particlesFull: number;
  particlesAmbient: number;
  pulseHz: number;
  hueShift: number;
  hazeColor: string;
  hazeIntensity: number;
  pulseHzAmbient: number;
  breathHz: number;
  breathAmp: number;
  /** Phase coherence: 0 = random, 1 = all particles pulse in unison */
  coherenceFactor: number;
  /** Radial drift bias: -1 inward, 0 neutral, +1 outward */
  radialBias: number;
  pulseShape: 'sin' | 'burst' | 'stutter';
  waveAmp: number;
  waveSpeed: number;
  /** Simplex noise turbulence strength (secondary perturbation, 0 = off) */
  curlStrength: number;
  /** Number of active vortex centers (flow field) */
  vortexCount: number;
  /** Base vortex strength multiplier */
  vortexStrength: number;
  /** Vortex center drift speed in rad/s */
  vortexDrift: number;
  /** Shell openness: 0 = tight core, 1 = loose hollow shell */
  shellOpenness: number;
  /** Eruption spawns per second (0 = off) */
  eruptionRate: number;
  /** Eruption radial kick magnitude */
  eruptionStrength: number;
  /** Ideal sphere radius (entity size) */
  entityRadius: number;
  /**
   * s9: per-state signature energy vectors. These boosts are applied ONLY
   * when the listed state is active, so the s8 global energy/character
   * split is preserved — the boost is *part of the state's identity*, not
   * a universal knob. Undefined = no boost.
   */
  /** thinking: vortex strength climbs with agent count / effort intensity */
  vortexStrengthBoostScale?: number;
  /** error: curl turbulence climbs as the error condition festers */
  curlStrengthBoostScale?: number;
  /** dreaming: hue drifts slowly with the given frequency (Hz) */
  hueDriftHz?: number;
}

/* Each `hazeColor` below is that state's SHIPPED DEFAULT, and only that. What
   the orb actually wears is resolved by `haze.ts`: a colour the operator chose
   in Appearance wins, then the accent once they have moved the mirror colour
   (each state carrying its own `hueShift` with it), then this.

   brand-exempt: ten near-black tints that belong to the states rather than to
   the palette — they are read by one module, they are the value "restore
   default" returns to, and lifting them into tokens.css would put ten custom
   properties there that no stylesheet ever reads. */
export const ENTITY_STATES: Record<EntityState, EntityStateConfig> = {
  idle: {
    particlesFull: 3500,
    particlesAmbient: 1400,
    pulseHz: 0.3,
    pulseHzAmbient: 0.15,
    hueShift: 0,
    hazeColor: '#0a0d1e',
    hazeIntensity: 0.108,
    breathHz: 0.15,
    breathAmp: 0.04,
    coherenceFactor: 0.3,
    radialBias: 0.0,
    pulseShape: 'sin',
    waveAmp: 0,
    waveSpeed: 0,
    curlStrength: 0.12,
    vortexCount: 4,
    vortexStrength: 0.4,
    vortexDrift: 0.03,
    shellOpenness: 0.7,
    eruptionRate: 0.3,
    eruptionStrength: 2.5,
    entityRadius: 1.2,
  },
  thinking: {
    particlesFull: 3500,
    particlesAmbient: 1400,
    pulseHz: 1.5,
    pulseHzAmbient: 0.75,
    hueShift: 36,
    hazeColor: '#140a1e',
    hazeIntensity: 0.168,
    breathHz: 0.5,
    breathAmp: 0.08,
    coherenceFactor: 0.15,
    radialBias: -0.3,
    pulseShape: 'sin',
    waveAmp: 0,
    waveSpeed: 0,
    curlStrength: 0.2,
    vortexCount: 5,
    vortexStrength: 0.7,
    vortexDrift: 0.06,
    shellOpenness: 0.5,
    eruptionRate: 1.0,
    eruptionStrength: 1.5,
    entityRadius: 1.0,
    vortexStrengthBoostScale: 0.6,
  },
  speaking: {
    particlesFull: 3500,
    particlesAmbient: 1400,
    pulseHz: 0.8,
    pulseHzAmbient: 0.4,
    hueShift: 150,
    hazeColor: '#1a1008',
    hazeIntensity: 0.18,
    breathHz: 0.35,
    breathAmp: 0.10,
    coherenceFactor: 0.8,
    radialBias: 0.3,
    pulseShape: 'burst',
    waveAmp: 0.10,
    waveSpeed: 0.6,
    curlStrength: 0.1,
    vortexCount: 4,
    vortexStrength: 0.6,
    vortexDrift: 0.05,
    shellOpenness: 0.85,
    eruptionRate: 0.5,
    eruptionStrength: 3.5,
    entityRadius: 1.3,
  },
  spawning: {
    particlesFull: 3500,
    particlesAmbient: 1400,
    pulseHz: 1.0,
    pulseHzAmbient: 0.5,
    hueShift: -138,
    hazeColor: '#0e1a08',
    hazeIntensity: 0.192,
    breathHz: 0.4,
    breathAmp: 0.10,
    coherenceFactor: 0.6,
    radialBias: 0.3,
    pulseShape: 'sin',
    waveAmp: 0,
    waveSpeed: 0,
    curlStrength: 0.25,
    vortexCount: 5,
    vortexStrength: 0.8,
    vortexDrift: 0.07,
    shellOpenness: 0.6,
    eruptionRate: 1.2,
    eruptionStrength: 3.0,
    entityRadius: 1.1,
  },
  council: {
    particlesFull: 3500,
    particlesAmbient: 1400,
    pulseHz: 0.8,
    pulseHzAmbient: 0.4,
    hueShift: -102,
    hazeColor: '#0a1e14',
    hazeIntensity: 0.15,
    breathHz: 0.25,
    breathAmp: 0.06,
    coherenceFactor: 0.4,
    radialBias: 0.0,
    pulseShape: 'sin',
    waveAmp: 0,
    waveSpeed: 0,
    curlStrength: 0.08,
    vortexCount: 4,
    vortexStrength: 0.5,
    vortexDrift: 0.04,
    shellOpenness: 0.65,
    eruptionRate: 0.3,
    eruptionStrength: 1.5,
    entityRadius: 1.15,
  },
  listening: {
    particlesFull: 3500,
    particlesAmbient: 1400,
    pulseHz: 0.5,
    pulseHzAmbient: 0.25,
    hueShift: -66,
    hazeColor: '#081e1e',
    hazeIntensity: 0.12,
    breathHz: 0.2,
    breathAmp: 0.05,
    coherenceFactor: 0.2,
    radialBias: -0.15,
    pulseShape: 'sin',
    waveAmp: 0.08,
    waveSpeed: 0.6,
    curlStrength: 0.06,
    vortexCount: 4,
    vortexStrength: 0.35,
    vortexDrift: 0.025,
    shellOpenness: 0.7,
    eruptionRate: 0.2,
    eruptionStrength: 1.0,
    entityRadius: 1.1,
  },
  error: {
    particlesFull: 2800,
    particlesAmbient: 1120,
    pulseHz: 4.0,
    pulseHzAmbient: 2.0,
    hueShift: 114,
    hazeColor: '#280808',
    hazeIntensity: 0.252,
    breathHz: 1.5,
    breathAmp: 0.06,
    coherenceFactor: 0.0,
    radialBias: 0.0,
    pulseShape: 'stutter',
    waveAmp: 0,
    waveSpeed: 0,
    curlStrength: 0.35,
    vortexCount: 6,
    vortexStrength: 1.2,
    vortexDrift: 0.12,
    shellOpenness: 0.3,
    eruptionRate: 2.5,
    eruptionStrength: 2.0,
    entityRadius: 0.9,
    curlStrengthBoostScale: 0.8,
  },
  happy: {
    particlesFull: 3500,
    particlesAmbient: 1400,
    pulseHz: 1.2,
    pulseHzAmbient: 0.6,
    hueShift: 186,
    hazeColor: '#1a1c08',
    hazeIntensity: 0.21,
    breathHz: 0.6,
    breathAmp: 0.15,
    coherenceFactor: 0.7,
    radialBias: 0.2,
    pulseShape: 'burst',
    waveAmp: 0,
    waveSpeed: 0,
    curlStrength: 0.15,
    vortexCount: 4,
    vortexStrength: 0.55,
    vortexDrift: 0.05,
    shellOpenness: 0.75,
    eruptionRate: 1.0,
    eruptionStrength: 4.0,
    entityRadius: 1.35,
  },
  deep_focus: {
    particlesFull: 3500,
    particlesAmbient: 1400,
    pulseHz: 0.2,
    pulseHzAmbient: 0.1,
    hueShift: -36,
    hazeColor: '#081520',
    hazeIntensity: 0.132,
    breathHz: 0.1,
    breathAmp: 0.03,
    coherenceFactor: 0.5,
    radialBias: -0.1,
    pulseShape: 'sin',
    waveAmp: 0,
    waveSpeed: 0,
    curlStrength: 0.04,
    vortexCount: 3,
    vortexStrength: 0.3,
    vortexDrift: 0.02,
    shellOpenness: 0.55,
    eruptionRate: 0.1,
    eruptionStrength: 1.0,
    entityRadius: 0.85,
  },
  dreaming: {
    particlesFull: 3500,
    particlesAmbient: 1400,
    pulseHz: 0.25,
    pulseHzAmbient: 0.12,
    hueShift: 72,
    hazeColor: '#1e0820',
    hazeIntensity: 0.22,
    breathHz: 0.12,
    breathAmp: 0.06,
    coherenceFactor: 0.35,
    radialBias: -0.05,
    pulseShape: 'sin',
    waveAmp: 0.04,
    waveSpeed: 0.2,
    curlStrength: 0.08,
    vortexCount: 3,
    vortexStrength: 0.45,
    vortexDrift: 0.025,
    shellOpenness: 0.8,
    eruptionRate: 0,
    eruptionStrength: 0,
    entityRadius: 1.1,
    hueDriftHz: 0.08,
  },
};
