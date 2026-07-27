// Slice 2 — region capture rect math (pure). The DOM→image capture + send path
// is exercised live (needs canvas + the backend); these guard the geometry.

import { describe, expect, it } from 'vitest';

import { rectFromPoints, isCapturable } from './regionCaptureStore';

describe('regionCapture rect helpers', () => {
  it('rectFromPoints normalizes any drag direction to a positive-dim rect', () => {
    expect(rectFromPoints(100, 80, 300, 240)).toEqual({ x: 100, y: 80, w: 200, h: 160 });
    expect(rectFromPoints(300, 240, 100, 80)).toEqual({ x: 100, y: 80, w: 200, h: 160 }); // reversed
    expect(rectFromPoints(300, 80, 100, 240)).toEqual({ x: 100, y: 80, w: 200, h: 160 }); // mixed
  });

  it('isCapturable rejects stray clicks / paper-thin drags', () => {
    expect(isCapturable({ x: 0, y: 0, w: 4, h: 200 })).toBe(false);
    expect(isCapturable({ x: 0, y: 0, w: 200, h: 4 })).toBe(false);
    expect(isCapturable({ x: 0, y: 0, w: 20, h: 20 })).toBe(true);
  });
});
