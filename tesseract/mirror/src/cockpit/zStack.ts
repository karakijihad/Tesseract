// AS-2 — one monotonic stacking-order allocator shared by BOTH panel
// systems (SC-2 panelStore / GlassPanel view panels AND Y-2 SurfaceLayer
// cards), so whatever the operator touches last comes to the top
// regardless of which system owns it. Base sits above both legacy ranges
// (panels grew from Z_BASE=10; surfaces from 100+descriptor.z) so the very
// first interaction after load jumps cleanly above any un-raised element.

const BASE = 1000;
let _counter = BASE;
// Highest z ever assigned to a raised surface (written by surfacesStore
// raiseSurface, read by panelStore focus so panels can compare against the
// surface-only peak without importing surfacesStore).
let _surfacePeak = 0;

export function nextZ(): number {
  _counter += 1;
  return _counter;
}

// Current top of the shared stack WITHOUT consuming a new value.
export function peekZ(): number {
  return _counter;
}

// Record that a surface was raised to `z`. Only surfacesStore should call this.
export function recordSurfaceZ(z: number): void {
  if (z > _surfacePeak) _surfacePeak = z;
}

// Highest z ever assigned to a raised surface; 0 if no surface was ever raised.
export function surfacePeakZ(): number {
  return _surfacePeak;
}

// Test-only: reset both counters between tests.
export function __resetZForTest(): void {
  _counter = BASE;
  _surfacePeak = 0;
}
