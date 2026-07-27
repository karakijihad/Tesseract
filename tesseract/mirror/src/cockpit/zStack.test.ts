import { describe, it, expect, beforeEach } from 'vitest';
import { nextZ, peekZ, recordSurfaceZ, surfacePeakZ, __resetZForTest } from './zStack';

describe('zStack', () => {
  beforeEach(() => __resetZForTest());

  it('returns strictly increasing values starting above 1000', () => {
    const a = nextZ();
    const b = nextZ();
    const c = nextZ();
    expect(a).toBeGreaterThan(1000);
    expect(b).toBeGreaterThan(a);
    expect(c).toBeGreaterThan(b);
  });

  it('peekZ reports the current top without consuming a value', () => {
    const a = nextZ();
    expect(peekZ()).toBe(a);
    const b = nextZ();
    expect(b).toBeGreaterThan(a);
    expect(peekZ()).toBe(b);
  });

  it('surfacePeakZ starts at 0 and tracks the highest recorded surface z', () => {
    expect(surfacePeakZ()).toBe(0);
    const z1 = nextZ();
    recordSurfaceZ(z1);
    expect(surfacePeakZ()).toBe(z1);
    const z2 = nextZ();
    recordSurfaceZ(z2);
    expect(surfacePeakZ()).toBe(z2);
    // Recording a lower value does not decrease the peak.
    recordSurfaceZ(z1);
    expect(surfacePeakZ()).toBe(z2);
  });
});
