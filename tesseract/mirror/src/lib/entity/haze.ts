import type { EntityState } from '../types';

import { ENTITY_STATES } from './states';

/** The states in the order the operator meets them in the orb control —
 *  resting first, then the ones the runtime drives, then the two moods. */
export const HAZE_STATES: EntityState[] = [
  'idle',
  'listening',
  'thinking',
  'deep_focus',
  'speaking',
  'spawning',
  'council',
  'happy',
  'dreaming',
  'error',
];

export interface Hsl {
  h: number;
  s: number;
  l: number;
}

/** `#rgb` / `#rrggbb` → HSL. Returns null for anything else, so a bad stored
 *  override falls through to the default rather than painting the orb black. */
export function hexToHsl(hex: string): Hsl | null {
  const m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) return null;
  const raw = m[1];
  const full =
    raw.length === 3
      ? raw
          .split('')
          .map((c) => c + c)
          .join('')
      : raw;
  const r = parseInt(full.slice(0, 2), 16) / 255;
  const g = parseInt(full.slice(2, 4), 16) / 255;
  const b = parseInt(full.slice(4, 6), 16) / 255;
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const l = (max + min) / 2;
  const d = max - min;
  if (d === 0) return { h: 0, s: 0, l };
  const s = d / (1 - Math.abs(2 * l - 1));
  let h: number;
  if (max === r) h = ((g - b) / d) % 6;
  else if (max === g) h = (b - r) / d + 2;
  else h = (r - g) / d + 4;
  h = (h * 60 + 360) % 360;
  return { h, s, l };
}

/** HSL → `#rrggbb`. */
export function hslToHex({ h, s, l }: Hsl): string {
  const c = (1 - Math.abs(2 * l - 1)) * s;
  const hp = (((h % 360) + 360) % 360) / 60;
  const x = c * (1 - Math.abs((hp % 2) - 1));
  const [r0, g0, b0] =
    hp < 1 ? [c, x, 0]
    : hp < 2 ? [x, c, 0]
    : hp < 3 ? [0, c, x]
    : hp < 4 ? [0, x, c]
    : hp < 5 ? [x, 0, c]
    : [c, 0, x];
  const m = l - c / 2;
  const to = (v: number) =>
    Math.round(Math.min(1, Math.max(0, v + m)) * 255)
      .toString(16)
      .padStart(2, '0');
  return `#${to(r0)}${to(g0)}${to(b0)}`;
}

/** What ships: the hand-picked tint for this state. */
export function defaultHaze(state: EntityState): string {
  return ENTITY_STATES[state].hazeColor;
}

/** The accent's hue, carried to this state by the state's OWN offset, at the
 *  default's saturation and lightness.
 *
 * `hueShift` already exists and is already what separates the states from each
 * other in the particle field and the core glow — reusing it is what makes a
 * derived haze read as the same state rather than as a new one. Keeping S and L
 * from the default is what keeps it a haze: these are near-black tints, and a
 * hue taken at the accent's own lightness would put a bright wash behind the
 * orb.
 */
export function derivedHaze(state: EntityState, accentHue: number): string {
  const base = hexToHsl(defaultHaze(state));
  if (!base) return defaultHaze(state);
  return hslToHex({ h: accentHue + ENTITY_STATES[state].hueShift, s: base.s, l: base.l });
}

/** One state's haze, by the operator's rule of 2026-08-15.
 *
 * Precedence, highest first:
 *   1. a colour they chose here, which wins until they restore the default;
 *   2. the accent, once they have moved the mirror colour off its default —
 *      so changing one control still carries all ten moods with it;
 *   3. the shipped default.
 */
export function resolveHaze(
  state: EntityState,
  accentHue: number,
  accentShifted: boolean,
  overrides: Partial<Record<EntityState, string>>,
): string {
  const custom = overrides[state];
  if (custom && hexToHsl(custom)) return custom;
  return accentShifted ? derivedHaze(state, accentHue) : defaultHaze(state);
}

export function resolveHazes(
  accentHue: number,
  accentShifted: boolean,
  overrides: Partial<Record<EntityState, string>>,
): Record<EntityState, string> {
  const out = {} as Record<EntityState, string>;
  for (const state of HAZE_STATES) {
    out[state] = resolveHaze(state, accentHue, accentShifted, overrides);
  }
  return out;
}

/** Which of the three sources a state is currently reading from — the orb
 *  control says so in words rather than leaving the operator to infer it from
 *  a swatch. */
export type HazeSource = 'custom' | 'derived' | 'default';

export function hazeSource(
  state: EntityState,
  accentShifted: boolean,
  overrides: Partial<Record<EntityState, string>>,
): HazeSource {
  const custom = overrides[state];
  if (custom && hexToHsl(custom)) return 'custom';
  return accentShifted ? 'derived' : 'default';
}
