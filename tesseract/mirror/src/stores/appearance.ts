import { create } from 'zustand';

import type { EntityState } from '../lib/types';
import { hexToHsl, resolveHazes } from '../lib/entity/haze';

import { useEntityStore } from './entity';

const LS_KEY = 'appearance';

/** Defaults live in `tokens.css`, not here — these are the values the file
 *  declares, repeated only so "reset" can clear the overrides and land back
 *  on them without reading the stylesheet. */
/** Compact is the default (operator call, 2026-08-13) — the cockpit is a dense
 *  instrument panel, and 100% was sized for prose it does not show. */
export const DEFAULT_TYPE_SCALE = 0.9;
export const DEFAULT_ACCENT_HUE = 246;
export const DEFAULT_FONT = 'grotesk';

/** The faces the app can be set to. One font for everything — views, chat,
 *  settings — because a second face is how an app starts reading as two.
 *
 *  Only the id and what to call it live here. The stacks themselves are
 *  `--font-choice-*` in `tokens.css`, which is the one file allowed to name a
 *  font, and each is already bundled: choosing one downloads nothing.
 */
export const APP_FONTS: { id: string; label: string; note: string }[] = [
  { id: 'grotesk', label: 'Space Grotesk', note: 'The default — geometric, made for interfaces' },
  { id: 'outfit', label: 'Outfit', note: 'Rounder and wider; easier on long prose' },
  { id: 'mono', label: 'JetBrains Mono', note: 'Fixed width everywhere — a terminal for a cockpit' },
  { id: 'system', label: 'System', note: "Whatever this machine already renders best" },
];

/** Type scale bounds. The floor is deliberately far below readable — at 50%
 *  the meta tier is 5px — because the operator asked for the range, and a
 *  control that stops where someone else thinks it should is the thing they
 *  were working around. The ceiling is where fixed rails clip their labels. */
export const MIN_TYPE_SCALE = 0.5;
export const MAX_TYPE_SCALE = 1.3;

export type HazeOverrides = Partial<Record<EntityState, string>>;

interface AppearanceStore {
  typeScale: number;
  accentHue: number;
  font: string;
  /** Per-state orb hazes the operator picked by hand. Absent means the state
   *  reads the accent (once shifted) or the shipped default — see
   *  `lib/entity/haze.ts::resolveHaze`. */
  hazeOverrides: HazeOverrides;
  setTypeScale: (scale: number) => void;
  setAccentHue: (hue: number) => void;
  setFont: (id: string) => void;
  setHaze: (state: EntityState, hex: string) => void;
  /** Hand this state back to the accent / the default. */
  clearHaze: (state: EntityState) => void;
  clearHazes: () => void;
  reset: () => void;
}

interface Persisted {
  typeScale?: number;
  accentHue?: number;
  font?: string;
  hazeOverrides?: HazeOverrides;
}

function load(): Persisted {
  if (typeof window === 'undefined') return {};
  try {
    const raw = window.localStorage.getItem(LS_KEY);
    if (!raw) return {};
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object') return {};
    const { typeScale, accentHue, font, hazeOverrides } = parsed as Persisted;
    // Every stored haze is re-validated, not trusted: a hand-edited or
    // half-written entry would otherwise reach `THREE.Color` and paint the orb
    // black with nothing on screen to say why.
    const hazes: HazeOverrides = {};
    if (hazeOverrides && typeof hazeOverrides === 'object') {
      for (const [k, v] of Object.entries(hazeOverrides)) {
        if (typeof v === 'string' && hexToHsl(v)) hazes[k as EntityState] = v;
      }
    }
    return {
      typeScale: typeof typeScale === 'number' ? typeScale : undefined,
      accentHue: typeof accentHue === 'number' ? accentHue : undefined,
      font: APP_FONTS.some((f) => f.id === font) ? font : undefined,
      hazeOverrides: hazes,
    };
  } catch {
    return {};
  }
}

function persist(state: Persisted): void {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(LS_KEY, JSON.stringify(state));
  } catch {
    // storage full / disabled — the choice still holds for this session
  }
}

const clamp = (n: number, lo: number, hi: number) =>
  Math.max(lo, Math.min(hi, n));

/** Write the choice onto `:root` and into the orb.
 *
 * The CSS half is three custom properties: every type tier is a multiple of
 * `--type-scale` and every accent shade is derived from `--accent-h`. The
 * face is a `data-font` attribute rather than a third property, so the stacks
 * stay in `tokens.css` where a font stack is allowed to be written; both font
 * tokens resolve through `--font-app`, so one attribute repaints every
 * surface. The orb is the half CSS cannot reach — it is
 * WebGL — so the same hue goes through the entity store, which is already how
 * `GlobalCanvas` seeds it from the token at boot.
 */
function apply(
  typeScale: number,
  accentHue: number,
  font: string,
  hazeOverrides: HazeOverrides,
): void {
  if (typeof document === 'undefined') return;
  const root = document.documentElement;
  root.style.setProperty('--type-scale', String(typeScale));
  root.style.setProperty('--accent-h', String(accentHue));
  root.dataset.font = font;

  const cs = getComputedStyle(root);
  const hsl = cs.getPropertyValue('--accent-hsl').trim();
  if (hsl) useEntityStore.getState().setAccentHsl(hsl);

  // The orb's two brand colours, read from the file that owns them rather than
  // repeated here. `--orb-ground` forwards `--bg-void`, so moving the app's
  // background moves the haze's ground with it.
  const ground = cs.getPropertyValue('--orb-ground').trim();
  const dreaming = cs.getPropertyValue('--orb-dreaming').trim();
  if (ground && dreaming) {
    useEntityStore.getState().setOrbPalette(ground, dreaming);
  }

  // "Shifted" is what turns the derived hazes on, per the operator's rule: the
  // mirror colour moving off its default is the signal that the moods should
  // follow it. Rounded, because the slider emits fractions.
  const shifted = Math.round(accentHue) !== DEFAULT_ACCENT_HUE;
  useEntityStore.getState().setHazes(resolveHazes(accentHue, shifted, hazeOverrides));
}

const initial = load();
const initialScale = clamp(
  initial.typeScale ?? DEFAULT_TYPE_SCALE,
  MIN_TYPE_SCALE,
  MAX_TYPE_SCALE,
);
const initialHue = clamp(initial.accentHue ?? DEFAULT_ACCENT_HUE, 0, 360);
const initialFont = initial.font ?? DEFAULT_FONT;

const initialHazes = initial.hazeOverrides ?? {};

export const useAppearanceStore = create<AppearanceStore>((set, get) => {
  /** Every setter does the same three things in the same order — paint, save,
   *  store — and each new field otherwise has to be threaded through four
   *  call sites by hand. One of them being forgotten is how a setting comes
   *  back on the next launch but not on this one. */
  const commit = (next: Partial<Persisted>) => {
    const s = get();
    const typeScale = next.typeScale ?? s.typeScale;
    const accentHue = next.accentHue ?? s.accentHue;
    const font = next.font ?? s.font;
    const hazeOverrides = next.hazeOverrides ?? s.hazeOverrides;
    apply(typeScale, accentHue, font, hazeOverrides);
    persist({ typeScale, accentHue, font, hazeOverrides });
    set({ typeScale, accentHue, font, hazeOverrides });
  };

  return {
    typeScale: initialScale,
    accentHue: initialHue,
    font: initialFont,
    hazeOverrides: initialHazes,

    setTypeScale: (scale) =>
      commit({ typeScale: clamp(scale, MIN_TYPE_SCALE, MAX_TYPE_SCALE) }),

    setAccentHue: (hue) => commit({ accentHue: clamp(hue, 0, 360) }),

    setFont: (id) => {
      // An id the app does not know would resolve to an undefined custom
      // property, which drops the declaration and leaves the operator on
      // whatever they had — silently. Refuse it instead.
      if (!APP_FONTS.some((f) => f.id === id)) return;
      commit({ font: id });
    },

    // Refused rather than stored: an unparseable colour reaching the canvas
    // paints the orb black, and the control it came from would still be
    // showing the operator the swatch they picked.
    setHaze: (state, hex) => {
      if (!hexToHsl(hex)) return;
      commit({ hazeOverrides: { ...get().hazeOverrides, [state]: hex } });
    },

    clearHaze: (state) => {
      const next = { ...get().hazeOverrides };
      delete next[state];
      commit({ hazeOverrides: next });
    },

    clearHazes: () => commit({ hazeOverrides: {} }),

    // `commit` merges against current state, which is exactly wrong for a
    // reset — it has to name every field, including the empty override map.
    reset: () => {
      apply(DEFAULT_TYPE_SCALE, DEFAULT_ACCENT_HUE, DEFAULT_FONT, {});
      persist({});
      set({
        typeScale: DEFAULT_TYPE_SCALE,
        accentHue: DEFAULT_ACCENT_HUE,
        font: DEFAULT_FONT,
        hazeOverrides: {},
      });
    },
  };
});

/** Called once at boot, before the first paint, so a persisted choice never
 *  flashes the default first. */
export function installAppearance(): void {
  const { typeScale, accentHue, font, hazeOverrides } = useAppearanceStore.getState();
  apply(typeScale, accentHue, font, hazeOverrides);
}
