const TAU = Math.PI * 2;

interface Vortex {
  /** Unit-sphere position (nx, ny, nz) */
  nx: number;
  ny: number;
  nz: number;
  /** Signed strength — positive = CCW seen from outside */
  strength: number;
  /** Great-circle drift axis (unit vector) */
  driftAx: number;
  driftAy: number;
  driftAz: number;
  /** Remaining lifetime in seconds */
  life: number;
  /** Total lifetime (for fade-in/out) */
  maxLife: number;
}

export interface VortexParams {
  count: number;
  strength: number;
  /** Vortex center drift speed in rad/s */
  drift: number;
}

/**
 * Point-vortex flow field on a sphere.
 *
 * Each vortex induces tangential velocity on every particle via a softened
 * Biot-Savart kernel. The superposition is divergence-free by construction,
 * so particles circulate without pooling.
 */
export class VortexField {
  private vortices: Vortex[] = [];
  private targetCount = 4;
  private baseStrength = 0.4;
  private driftSpeed = 0.01;
  /** Softening parameter — prevents singularity at vortex center */
  private epsilon = 0.25;

  setParams(p: VortexParams): void {
    this.targetCount = p.count;
    this.baseStrength = p.strength;
    this.driftSpeed = p.drift;
  }

  /** Advance vortex centers, spawn/despawn as needed. */
  update(dt: number): void {
    // Age and cull dead vortices
    for (let i = this.vortices.length - 1; i >= 0; i--) {
      this.vortices[i].life -= dt;
      if (this.vortices[i].life <= 0) {
        this.vortices.splice(i, 1);
      }
    }

    // Spawn to reach target count
    while (this.vortices.length < this.targetCount) {
      this._spawnVortex();
    }

    // Drift each vortex along its great-circle axis
    for (const v of this.vortices) {
      const angle = this.driftSpeed * dt;
      this._rotateAroundAxis(v, angle);
    }
  }

  /**
   * Compute tangential velocity for a particle at (px, py, pz).
   * Returns [vx, vy, vz] — already tangent to the sphere.
   */
  evaluate(px: number, py: number, pz: number): [number, number, number] {
    const pDist = Math.sqrt(px * px + py * py + pz * pz);
    if (pDist < 0.001) return [0, 0, 0];

    const invP = 1 / pDist;
    const pnx = px * invP, pny = py * invP, pnz = pz * invP;

    let vx = 0, vy = 0, vz = 0;

    for (const vtx of this.vortices) {
      // Fade strength near birth/death (envelope over first/last 1s)
      const age = vtx.maxLife - vtx.life;
      const fadeIn = Math.min(1, age / 1.0);
      const fadeOut = Math.min(1, vtx.life / 1.0);
      const envelope = fadeIn * fadeOut;
      const str = vtx.strength * this.baseStrength * envelope;
      if (Math.abs(str) < 0.0001) continue;

      // Cross product: vortex_normal × particle_normal
      const cx = vtx.ny * pnz - vtx.nz * pny;
      const cy = vtx.nz * pnx - vtx.nx * pnz;
      const cz = vtx.nx * pny - vtx.ny * pnx;

      // Angular distance kernel: 1 / (1 - cos(angle) + epsilon)
      const cosAngle = vtx.nx * pnx + vtx.ny * pny + vtx.nz * pnz;
      const kernel = str / (1 - cosAngle + this.epsilon);

      vx += cx * kernel;
      vy += cy * kernel;
      vz += cz * kernel;
    }

    // Project onto tangent plane (remove radial component)
    const radial = vx * pnx + vy * pny + vz * pnz;
    vx -= radial * pnx;
    vy -= radial * pny;
    vz -= radial * pnz;

    return [vx, vy, vz];
  }

  private _spawnVortex(): void {
    // Random point on unit sphere
    const theta = Math.random() * TAU;
    const phi = Math.acos(2 * Math.random() - 1);
    const nx = Math.sin(phi) * Math.cos(theta);
    const ny = Math.sin(phi) * Math.sin(theta);
    const nz = Math.cos(phi);

    // Random perpendicular drift axis
    const dt = Math.random() * TAU;
    const dp = Math.acos(2 * Math.random() - 1);
    let dax = Math.sin(dp) * Math.cos(dt);
    let day = Math.sin(dp) * Math.sin(dt);
    let daz = Math.cos(dp);
    // Make perpendicular to vortex position
    const dot = dax * nx + day * ny + daz * nz;
    dax -= dot * nx;
    day -= dot * ny;
    daz -= dot * nz;
    const dLen = Math.sqrt(dax * dax + day * day + daz * daz);
    if (dLen > 0.001) {
      dax /= dLen; day /= dLen; daz /= dLen;
    }

    const life = 4 + Math.random() * 6; // 4-10s lifetime
    this.vortices.push({
      nx, ny, nz,
      strength: (Math.random() < 0.5 ? 1 : -1) * (0.6 + Math.random() * 0.8),
      driftAx: dax, driftAy: day, driftAz: daz,
      life,
      maxLife: life,
    });
  }

  /** Rotate vortex position around its drift axis by angle (Rodrigues). */
  private _rotateAroundAxis(v: Vortex, angle: number): void {
    const cos = Math.cos(angle);
    const sin = Math.sin(angle);
    const ax = v.driftAx, ay = v.driftAy, az = v.driftAz;
    const dot = ax * v.nx + ay * v.ny + az * v.nz;
    // cross(axis, pos)
    const cx = ay * v.nz - az * v.ny;
    const cy = az * v.nx - ax * v.nz;
    const cz = ax * v.ny - ay * v.nx;

    const nx = v.nx * cos + cx * sin + ax * dot * (1 - cos);
    const ny = v.ny * cos + cy * sin + ay * dot * (1 - cos);
    const nz = v.nz * cos + cz * sin + az * dot * (1 - cos);

    // Re-normalize (Rodrigues is exact but float drift accumulates)
    const len = Math.sqrt(nx * nx + ny * ny + nz * nz);
    v.nx = nx / len;
    v.ny = ny / len;
    v.nz = nz / len;
  }
}
