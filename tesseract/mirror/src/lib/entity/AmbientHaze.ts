import * as THREE from 'three';

/** Radial nebula haze behind the particle cloud. Rendered as a large low-opacity plane sprite. */
export class AmbientHaze {
  private mesh!: THREE.Mesh;
  private material!: THREE.MeshBasicMaterial;
  private texture!: THREE.CanvasTexture;
  private canvas!: HTMLCanvasElement;

  private hazeColor = '#050508';
  private intensity = 0.04;
  private accentHsl = '246 83% 68%';
  private hueShift = 0;

  /** Dreaming overlay: slow purple pulse */
  private dreamingActive = false;
  private dreamPhase = 0;

  init(scene: THREE.Scene): void {
    this.canvas = document.createElement('canvas');
    this.canvas.width = 256;
    this.canvas.height = 256;

    this.texture = new THREE.CanvasTexture(this.canvas);
    this._draw();

    this.material = new THREE.MeshBasicMaterial({
      map: this.texture,
      transparent: true,
      blending: THREE.NormalBlending,
      depthWrite: false,
      side: THREE.FrontSide,
    });

    const geometry = new THREE.PlaneGeometry(6, 6);
    this.mesh = new THREE.Mesh(geometry, this.material);
    this.mesh.position.z = -2;
    scene.add(this.mesh);
  }

  private _draw(): void {
    const ctx = this.canvas.getContext('2d')!;
    const { width, height } = this.canvas;
    ctx.clearRect(0, 0, width, height);

    const cx = width / 2;
    const cy = height / 2;
    const r = width * 0.5;

    const effectiveIntensity = this.dreamingActive ? this.intensity * (0.7 + 0.3 * Math.sin(this.dreamPhase)) : this.intensity;

    // Core glow uses accent color (darkened); outer haze uses state hazeColor
    const accentHex = this.dreamingActive ? '#6b21a8' : this._accentToHex();
    const outerColor = this.dreamingActive ? '#6b21a8' : this.hazeColor;

    const gradient = ctx.createRadialGradient(cx, cy, 0, cx, cy, r);
    gradient.addColorStop(0, this._withAlpha(accentHex, Math.min(effectiveIntensity * 2.0, 0.85)));
    gradient.addColorStop(0.35, this._withAlpha(accentHex, effectiveIntensity * 0.8));
    gradient.addColorStop(0.6, this._withAlpha(outerColor, effectiveIntensity * 0.4));
    gradient.addColorStop(1, 'transparent');

    ctx.fillStyle = gradient;
    ctx.fillRect(0, 0, width, height);

    this.texture.needsUpdate = true;
  }

  /** Convert accent HSL string to a darkened hex for the core glow */
  private _accentToHex(): string {
    const parts = this.accentHsl.split(' ');
    const h = ((parseFloat(parts[0]) + this.hueShift) % 360 + 360) % 360;
    const s = parseFloat(parts[1]) / 100;
    const l = Math.min(parseFloat(parts[2]) / 100, 0.25); // darken for glow
    // HSL → RGB
    const c = (1 - Math.abs(2 * l - 1)) * s;
    const x = c * (1 - Math.abs(((h / 60) % 2) - 1));
    const m = l - c / 2;
    let r0 = 0, g0 = 0, b0 = 0;
    if (h < 60)       { r0 = c; g0 = x; }
    else if (h < 120) { r0 = x; g0 = c; }
    else if (h < 180) { g0 = c; b0 = x; }
    else if (h < 240) { g0 = x; b0 = c; }
    else if (h < 300) { r0 = x; b0 = c; }
    else              { r0 = c; b0 = x; }
    const ri = Math.round((r0 + m) * 255);
    const gi = Math.round((g0 + m) * 255);
    const bi = Math.round((b0 + m) * 255);
    return `#${ri.toString(16).padStart(2, '0')}${gi.toString(16).padStart(2, '0')}${bi.toString(16).padStart(2, '0')}`;
  }

  private _withAlpha(hex: string, alpha: number): string {
    let r = 0, g = 0, b = 0;
    if (hex.length === 4) {
      r = parseInt(hex[1] + hex[1], 16);
      g = parseInt(hex[2] + hex[2], 16);
      b = parseInt(hex[3] + hex[3], 16);
    } else if (hex.length === 7) {
      r = parseInt(hex.slice(1, 3), 16);
      g = parseInt(hex.slice(3, 5), 16);
      b = parseInt(hex.slice(5, 7), 16);
    }
    return `rgba(${r},${g},${b},${Math.max(0, Math.min(1, alpha))})`;
  }

  setHazeColor(color: string): void {
    this.hazeColor = color;
    this._draw();
  }

  setAccentHsl(hsl: string): void {
    this.accentHsl = hsl;
    this._draw();
  }

  setHueShift(shift: number): void {
    if (this.hueShift === shift) return;
    this.hueShift = shift;
    this._draw();
  }

  setIntensity(intensity: number): void {
    // Early-return on no-op so EntityController's frame-by-frame vignette
    // modulation doesn't burn the haze canvas redraw cost. Threshold of
    // 0.005 hides sub-perceptual jitter and keeps redraw count low even
    // with continuous intensity flux.
    if (Math.abs(intensity - this.intensity) < 0.005) return;
    this.intensity = intensity;
    this._draw();
  }

  setDreaming(active: boolean): void {
    this.dreamingActive = active;
    if (!active) this.dreamPhase = 0;
    this._draw();
  }

  /** Called each frame for dreaming pulse animation */
  tick(tNow: number): void {
    if (!this.dreamingActive) return;
    this.dreamPhase = (tNow / 1000) * Math.PI * 2 * 0.3;
    this._draw();
  }

  dispose(): void {
    this.texture?.dispose();
    this.material?.dispose();
    (this.mesh?.geometry as THREE.BufferGeometry)?.dispose();
  }
}
