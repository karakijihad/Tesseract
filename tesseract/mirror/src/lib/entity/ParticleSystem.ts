import * as THREE from 'three';
import { noiseDisplacement } from './noise';
import { VortexField } from './VortexField';

const MAX_PARTICLES = 5000;
const MAX_ERUPTIONS = 8;
const TAU = Math.PI * 2;

interface Eruption {
  nx: number; ny: number; nz: number;
  age: number;
  duration: number;
  strength: number;
  cosRadius: number;
}

/** Organic particle cloud driven by point-vortex flow field. */
export class ParticleSystem {
  private renderer!: THREE.WebGLRenderer;
  private scene!: THREE.Scene;
  private camera!: THREE.PerspectiveCamera;
  private geometry!: THREE.BufferGeometry;
  private material!: THREE.PointsMaterial;
  private points!: THREE.Points;
  private vortexField = new VortexField();

  /** Per-particle base positions (vortex flow applies here) */
  private basePositions!: Float32Array;
  /** Rendered positions (written each frame) */
  private livePositions!: Float32Array;
  /** Per-particle phase offsets for radial pulse */
  private phases!: Float32Array;

  private count = 0;
  private startTs = 0;

  pulseHz = 0.3;
  hueShift = 0;
  private _breathHz = 0.15;
  private _breathAmp = 0.04;
  private _coherenceFactor = 0.3;
  private _radialBias = 0.0;
  private _pulseShape: 'sin' | 'burst' | 'stutter' = 'sin';
  private _waveAmp = 0.0;
  private _waveSpeed = 2.0;
  private _curlStrength = 0.0;
  private _shellOpenness = 0.7;
  private _idealRadius = 1.2;
  private _prevTNow = 0;
  /** Phase accumulators. Integrate hz·dt so frequency changes do not
   *  produce discontinuous jumps in the sine argument (which would flicker
   *  visibly whenever intensity modulates pulseHz). */
  private _pulsePhase = 0;
  private _breathPhase = 0;
  private _wavePhase = 0;

  private accentHsl = '246 83% 68%';
  /** Color from accent + state hueShift + valence brightness/saturation lift.
   *  Pop blend layers on top in `_applyPopBlend`. */
  private _baseColor!: THREE.Color;
  /** Final color sent to material (base lerped toward pop hue while popAmp > 0). */
  private color!: THREE.Color;
  private _popColor = new THREE.Color();
  private _popAmp = 0;
  private _popHex = '#7c6cf0'; // --accent (tokens.css)
  /** Valence ∈ [-1, +1]. Drives a subtle ±10% L / ±7% S lift on the base color
   *  so positive valence reads as "lit up", negative as "subdued". Hue drift
   *  is applied separately upstream via `valenceToHueDelta`. */
  private _valence = 0;

  private eruptions: Eruption[] = [];
  private _eruptionRate = 0;
  private _eruptionStrength = 0;
  private _eruptionRadius = 0.4;
  private _eruptionTimer = 0;

  init(canvas: HTMLCanvasElement, count: number): void {
    if (this.renderer) return;
    this.renderer = new THREE.WebGLRenderer({
      canvas,
      alpha: true,
      antialias: false,
      powerPreference: 'low-power',
    });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.setClearColor(0x000000, 0);

    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(60, canvas.width / canvas.height, 0.1, 1000);
    // 4 instead of 5 — zooms the particle sphere in ~25% so it fills
    // more of the canvas inside the 200×200 corner-mode dock without
    // touching particle distribution. Full-screen mode still reads fine
    // because the sphere was comfortably smaller than the viewport.
    this.camera.position.z = 5;

    this._baseColor = new THREE.Color();
    this.color = new THREE.Color();
    this._updateColor();
    this._buildGeometry();
    this.setParticleCount(count);
    this.renderer.setSize(canvas.width, canvas.height, false);
    this.startTs = performance.now();
  }

  private _buildGeometry(): void {
    const n = MAX_PARTICLES;
    this.basePositions = new Float32Array(n * 3);
    this.livePositions = new Float32Array(n * 3);
    this.phases = new Float32Array(n);

    const radius = 1.2;

    for (let i = 0; i < n; i++) {
      const shellSpread = 0.3;
      const r = radius + (Math.random() - 0.5) * 2 * shellSpread;
      const theta = Math.random() * TAU;
      const phi = Math.acos(2 * Math.random() - 1);

      const x = r * Math.sin(phi) * Math.cos(theta);
      const y = r * Math.sin(phi) * Math.sin(theta);
      const z = r * Math.cos(phi);

      this.basePositions[i * 3]     = x;
      this.basePositions[i * 3 + 1] = y;
      this.basePositions[i * 3 + 2] = z;

      this.livePositions[i * 3]     = x;
      this.livePositions[i * 3 + 1] = y;
      this.livePositions[i * 3 + 2] = z;

      this.phases[i] = Math.random() * TAU;
    }

    this.geometry = new THREE.BufferGeometry();
    this.geometry.setAttribute('position', new THREE.BufferAttribute(this.livePositions, 3));
    this.geometry.setDrawRange(0, 0);

    this.material = new THREE.PointsMaterial({
      size: 0.018,
      sizeAttenuation: true,
      color: this.color,
      transparent: true,
      opacity: 0.42,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
    });

    this.points = new THREE.Points(this.geometry, this.material);
    this.scene.add(this.points);
  }

  setParticleCount(count: number): void {
    const clamped = Math.min(Math.max(0, count), MAX_PARTICLES);
    if (clamped === this.count) return;
    this.count = clamped;
    this.geometry.setDrawRange(0, clamped);
  }

  setAccentHsl(hsl: string): void {
    this.accentHsl = hsl;
    this._updateColor();
  }

  setBreath(hz: number, amp: number): void {
    this._breathHz = hz;
    this._breathAmp = amp;
  }

  setCoherence(factor: number): void {
    this._coherenceFactor = Math.max(0, Math.min(1, factor));
  }

  setRadialBias(bias: number): void {
    this._radialBias = Math.max(-1, Math.min(1, bias));
  }

  setPulseShape(shape: 'sin' | 'burst' | 'stutter'): void {
    this._pulseShape = shape;
  }

  setWave(amp: number, speed: number): void {
    this._waveAmp = amp;
    this._waveSpeed = speed;
  }

  setCurlStrength(strength: number): void {
    this._curlStrength = strength;
  }

  setShellOpenness(openness: number): void {
    this._shellOpenness = Math.max(0, Math.min(1, openness));
  }

  getVortexField(): VortexField {
    return this.vortexField;
  }

  setIdealRadius(r: number): void {
    this._idealRadius = Math.max(0.5, Math.min(2.0, r));
  }

  setEruptions(rate: number, strength: number): void {
    // Clamp accumulated timer so a state transition out of a long idle
    // (_eruptionRate ≈ 0 or very small) can't instantly drain an N-second
    // accumulator through the emission while-loop when a hotter state
    // raises the rate — the orb would emit a burst of eruptions on frame 1.
    if (rate > 0) {
      const interval = 1 / rate;
      if (this._eruptionTimer > interval) this._eruptionTimer = interval;
    } else {
      this._eruptionTimer = 0;
    }
    this._eruptionRate = rate;
    this._eruptionStrength = strength;
  }

  private _shapedPulse(rawOsc: number, tSec: number): number {
    switch (this._pulseShape) {
      case 'burst':
        return Math.pow(Math.max(0, rawOsc), 0.4);
      case 'stutter':
        return rawOsc
          * Math.sin(this._pulsePhase * 7.3 + 0.9)
          * (0.5 + 0.5 * Math.sin(tSec * 2.17));
      default:
        return rawOsc;
    }
  }

  private _updateColor(): void {
    const parts = this.accentHsl.split(' ');
    const h = ((parseFloat(parts[0]) + this.hueShift) % 360 + 360) % 360;
    const s = parseFloat(parts[1]) / 100;
    const l = parseFloat(parts[2]) / 100;
    // Valence brightness/saturation lift — the orb itself glows brighter when
    // TARS feels good, dims when subdued. Tuned subtle: ±10% L / ±7% S.
    const sFinal = Math.max(0, Math.min(1, s + this._valence * 0.07));
    const lFinal = Math.max(0, Math.min(1, l + this._valence * 0.10));
    this._baseColor.setHSL(h / 360, sFinal, lFinal);
    this._applyPopBlend();
  }

  /** Compose final material color from `_baseColor` and the transient pop.
   *  Blend ceiling 0.85 keeps a hint of base hue at peak so the pop reads as
   *  a tint, not a full color swap — but high enough that error/success/user
   *  pops are visually distinct (not the previous 0.6 ceiling which made them
   *  near-identical at typical amplitudes). */
  private _applyPopBlend(): void {
    if (this._popAmp > 0.001) {
      this._popColor.set(this._popHex);
      this.color.lerpColors(this._baseColor, this._popColor, Math.min(this._popAmp * 0.7, 0.85));
    } else {
      this.color.copy(this._baseColor);
    }
    if (this.material) this.material.color.copy(this.color);
  }

  /** Translate the cloud by (x, y) world units. */
  setWobble(x: number, y: number): void {
    if (!this.points) return;
    this.points.position.set(x, y, 0);
  }

  /** Apply transient reaction pop — scales cloud by (1 + amp·0.1) and tints
   *  the material toward `hex`. amp ∈ [0,1], driven by EntityController decay. */
  setPopVisual(amp: number, hex: string): void {
    this._popAmp = Math.max(0, Math.min(1, amp));
    this._popHex = hex;
    this._applyPopBlend();
    if (this.points) {
      this.points.scale.setScalar(1 + this._popAmp * 0.1);
    }
  }

  /** Valence ∈ [-1, +1]. Drives a brightness/saturation lift on the base
   *  color via `_updateColor`. Threshold-gated so we don't redraw color on
   *  sub-perceptual valence flutter from the EMA tick. */
  setValence(v: number): void {
    const clamped = Math.max(-1, Math.min(1, v));
    if (Math.abs(clamped - this._valence) < 0.01) return;
    this._valence = clamped;
    this._updateColor();
  }

  resize(w: number, h: number): void {
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
  }

  render(tNow: number): void {
    const dt = this._prevTNow > 0 ? Math.min((tNow - this._prevTNow) / 1000, 0.05) : 0.016;
    this._prevTNow = tNow;

    const tSec = (tNow - this.startTs) / 1000;

    // Advance phase accumulators with current frequencies. Doing this per
    // frame (rather than multiplying tSec by an instantaneous hz) keeps the
    // sine arguments continuous through frequency modulation.
    this._pulsePhase += dt * TAU * this.pulseHz;
    this._breathPhase += dt * TAU * this._breathHz;
    this._wavePhase += dt * TAU * this._waveSpeed;

    // Advance vortex field + eruptions
    this.vortexField.update(dt);
    this._updateEruptions(dt);

    // Shell containment params derived from shellOpenness, scaled to entity radius
    const idealRadius = this._idealRadius;
    const springK = 0.15 + (1 - this._shellOpenness) * 0.6;
    const innerClamp = idealRadius * (0.25 + (1 - this._shellOpenness) * 0.333);
    const outerClamp = idealRadius * (1.333 + this._shellOpenness * 0.5);

    // Envelope breath — overall scale
    const breath = 1 + Math.sin(this._breathPhase) * this._breathAmp;

    const coh = this._coherenceFactor;
    const rBias = this._radialBias;
    const wavA = this._waveAmp;
    const wPhase = this._wavePhase;
    const curlStr = this._curlStrength;
    const curlTime = tSec * 0.3;
    const hasEruptions = this.eruptions.length > 0;

    for (let i = 0; i < this.count; i++) {
      const ix = i * 3, iy = ix + 1, iz = ix + 2;

      let bx = this.basePositions[ix];
      let by = this.basePositions[iy];
      let bz = this.basePositions[iz];

      // === VORTEX FLOW (primary motion driver) ===
      let [vfx, vfy, vfz] = this.vortexField.evaluate(bx, by, bz);
      const vfMag = Math.sqrt(vfx * vfx + vfy * vfy + vfz * vfz);
      if (vfMag > 3.0) {
        const cap = 3.0 / vfMag;
        vfx *= cap; vfy *= cap; vfz *= cap;
      }
      const flowScale = dt * 0.8;
      bx += vfx * flowScale;
      by += vfy * flowScale;
      bz += vfz * flowScale;

      // === CURL NOISE (secondary turbulence — breaks mechanical vortex paths) ===
      if (curlStr > 0.001) {
        const dist0 = Math.sqrt(bx * bx + by * by + bz * bz);
        if (dist0 > 0.001) {
          const invD = 1 / dist0;
          const ux = bx * invD, uy = by * invD, uz = bz * invD;
          const [nx, ny, nz] = noiseDisplacement(bx * 1.2, by * 1.2, bz * 1.2, 1, curlTime);
          const nDot = nx * ux + ny * uy + nz * uz;
          bx += (nx - nDot * ux) * curlStr * dt;
          by += (ny - nDot * uy) * curlStr * dt;
          bz += (nz - nDot * uz) * curlStr * dt;
        }
      }

      // === TANGENTIAL DIFFUSION (anti-clustering) ===
      {
        const d0 = Math.sqrt(bx * bx + by * by + bz * bz);
        if (d0 > 0.001) {
          const inv0 = 1 / d0;
          const ux = bx * inv0, uy = by * inv0, uz = bz * inv0;
          const jx = Math.random() - 0.5, jy = Math.random() - 0.5, jz = Math.random() - 0.5;
          const jDot = jx * ux + jy * uy + jz * uz;
          const diff = 0.3 * dt;
          bx += (jx - jDot * ux) * diff;
          by += (jy - jDot * uy) * diff;
          bz += (jz - jDot * uz) * diff;
        }
      }

      // === ERUPTIONS (radial burst) ===
      if (hasEruptions) {
        const d1 = Math.sqrt(bx * bx + by * by + bz * bz);
        if (d1 > 0.001) {
          const inv1 = 1 / d1;
          const pnx = bx * inv1, pny = by * inv1, pnz = bz * inv1;
          for (let ei = 0; ei < this.eruptions.length; ei++) {
            const e = this.eruptions[ei];
            const cosAngle = pnx * e.nx + pny * e.ny + pnz * e.nz;
            if (cosAngle < e.cosRadius) continue;
            const proximity = (cosAngle - e.cosRadius) / (1 - e.cosRadius);
            const env = this._eruptionEnvelope(e.age / e.duration);
            const kick = e.strength * proximity * env * dt;
            bx += pnx * kick;
            by += pny * kick;
            bz += pnz * kick;
          }
        }
      }

      // === RADIAL SPRING (soft containment) ===
      let dist = Math.sqrt(bx * bx + by * by + bz * bz);
      if (dist > 0.0001) {
        const displacement = dist - idealRadius;
        const springForce = displacement * springK * dt;
        const id = 1 / dist;
        bx -= bx * id * springForce;
        by -= by * id * springForce;
        bz -= bz * id * springForce;
      }

      // Soft clamp
      dist = Math.sqrt(bx * bx + by * by + bz * bz);
      if (dist > outerClamp && dist > 0.0001) {
        const scale = outerClamp / dist;
        bx *= scale; by *= scale; bz *= scale;
      }
      if (dist < innerClamp && dist > 0.0001) {
        const scale = innerClamp / dist;
        bx *= scale; by *= scale; bz *= scale;
      }

      this.basePositions[ix] = bx;
      this.basePositions[iy] = by;
      this.basePositions[iz] = bz;

      // === VISUAL LAYERS (livePositions only) ===
      dist = Math.sqrt(bx * bx + by * by + bz * bz);
      const invDist = dist > 0.0001 ? 1 / dist : 0;
      const rx = bx * invDist;
      const ry = by * invDist;
      const rz = bz * invDist;

      // Coherent radial pulse + radialBias
      const effectivePhase = this.phases[i] * (1 - coh);
      const particleOsc = Math.sin(this._pulsePhase + effectivePhase);
      const wave = this._shapedPulse(particleOsc, tSec) * 0.08 + rBias * 0.04;

      // Traveling wave (speaking state mainly)
      let wdx = 0, wdy = 0, wdz = 0;
      if (wavA > 0.001) {
        wdx = wavA * Math.sin(by * 2.5 + wPhase);
        wdy = wavA * Math.sin(bz * 2.8 + wPhase * 1.1 + 1.0);
        wdz = wavA * Math.sin(bx * 3.0 + wPhase * 0.9 + 2.0);
      }

      // Compose
      this.livePositions[ix] = (bx + rx * wave + wdx) * breath;
      this.livePositions[iy] = (by + ry * wave + wdy) * breath;
      this.livePositions[iz] = (bz + rz * wave + wdz) * breath;
    }

    (this.geometry.attributes['position'] as THREE.BufferAttribute).needsUpdate = true;
    this.renderer.render(this.scene, this.camera);
  }

  private _updateEruptions(dt: number): void {
    for (let i = this.eruptions.length - 1; i >= 0; i--) {
      this.eruptions[i].age += dt;
      if (this.eruptions[i].age >= this.eruptions[i].duration) {
        this.eruptions.splice(i, 1);
      }
    }

    if (this._eruptionRate > 0) {
      this._eruptionTimer += dt;
      const interval = 1 / this._eruptionRate;
      while (this._eruptionTimer >= interval && this.eruptions.length < MAX_ERUPTIONS) {
        this._spawnEruption();
        this._eruptionTimer -= interval;
      }
    }
  }

  private _spawnEruption(): void {
    const theta = Math.random() * TAU;
    const phi = Math.acos(2 * Math.random() - 1);
    this.eruptions.push({
      nx: Math.sin(phi) * Math.cos(theta),
      ny: Math.sin(phi) * Math.sin(theta),
      nz: Math.cos(phi),
      age: 0,
      duration: 0.4 + Math.random() * 0.8,
      strength: this._eruptionStrength,
      cosRadius: Math.cos(this._eruptionRadius),
    });
  }

  private _eruptionEnvelope(t: number): number {
    if (t < 0.15) {
      const s = t / 0.15;
      return s * s * (3 - 2 * s);
    }
    const s = (t - 0.15) / 0.85;
    return (1 - s) * (1 - s);
  }

  getScene(): THREE.Scene {
    return this.scene;
  }

  dispose(): void {
    if (this.scene && this.points) this.scene.remove(this.points);
    this.geometry?.dispose();
    this.material?.dispose();
    this.renderer?.dispose();
  }
}
