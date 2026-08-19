import type { ITheme } from '@xterm/xterm';
import type { TerminalTheme, TerminalAnsiPalette } from '../types';

// Built-in fallbacks if tokens are unresolvable (e.g., SSR, edge cases).
const FALLBACK: Required<Pick<ITheme,
  'background' | 'foreground' | 'cursor' | 'cursorAccent' | 'selectionBackground' |
  'black' | 'red' | 'green' | 'yellow' | 'blue' | 'magenta' | 'cyan' | 'white' |
  'brightBlack' | 'brightRed' | 'brightGreen' | 'brightYellow' |
  'brightBlue' | 'brightMagenta' | 'brightCyan' | 'brightWhite'
>> = {
  background: '#050508',
  foreground: '#f5f5fa',
  cursor: '#9d8fff',
  cursorAccent: '#050508',
  /* brand-exempt: this whole object is the palette used when the tokens are
     unresolvable, so it cannot itself read one. */
  selectionBackground: 'rgba(124,108,240,0.25)',
  black: '#080810',
  red: '#ff6b6b',
  green: '#4ade80',
  yellow: '#ffb454',
  blue: '#60a5fa',
  magenta: '#f472b6',
  cyan: '#22d3ee',
  white: '#f5f5fa',
  /* brand-exempt: see selectionBackground above — fallback palette. */
  brightBlack: 'rgba(234,234,248,0.55)',
  brightRed: '#ff4d6a',
  brightGreen: '#00e88f',
  brightYellow: '#fb923c',
  brightBlue: '#4da6ff',
  brightMagenta: '#9d8fff',
  brightCyan: '#22d3ee',
  brightWhite: '#f5f5fa',
};

// The built-in Mirror theme, expressed as token references. Used when no
// config.themes.mirror is present, and as the fallback when user selects a
// theme that is missing keys.
const MIRROR_DEFAULTS: TerminalTheme = {
  background: 'token:--bg-void',
  foreground: 'token:--ink-100',
  cursor: 'token:--accent-bright',
  cursorAccent: 'token:--bg-void',
  selectionBackground: 'token:--accent-glow',
  ansi: {
    black: 'token:--bg-deep',
    red: 'token:--bad',
    green: 'token:--ok',
    yellow: 'token:--warn',
    blue: 'token:--info',
    magenta: 'token:--pink',
    cyan: 'token:--cyan',
    white: 'token:--ink-100',
  },
  bright_ansi: {
    black: 'token:--ink-55',
    red: 'token:--red',
    green: 'token:--green',
    yellow: 'token:--orange',
    blue: 'token:--blue',
    magenta: 'token:--accent-bright',
    cyan: 'token:--cyan',
    white: 'token:--ink-100',
  },
};

function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function resolve(value: string | undefined, fallback: string): string {
  if (!value) return fallback;
  if (value.startsWith('token:')) {
    const v = cssVar(value.slice(6));
    return v || fallback;
  }
  return value;
}

type Ansi8 = { black: string; red: string; green: string; yellow: string; blue: string; magenta: string; cyan: string; white: string };

function resolveAnsi(
  palette: TerminalAnsiPalette | undefined,
  defaults: TerminalAnsiPalette,
  fallback: Ansi8,
): Ansi8 {
  const merged: TerminalAnsiPalette = { ...defaults, ...(palette ?? {}) };
  return {
    black: resolve(merged.black, fallback.black),
    red: resolve(merged.red, fallback.red),
    green: resolve(merged.green, fallback.green),
    yellow: resolve(merged.yellow, fallback.yellow),
    blue: resolve(merged.blue, fallback.blue),
    magenta: resolve(merged.magenta, fallback.magenta),
    cyan: resolve(merged.cyan, fallback.cyan),
    white: resolve(merged.white, fallback.white),
  };
}

// xterm takes a resolved font string, not a CSS var, so the token is read at
// construction. Same source as every other surface — tokens.css, never a
// stack pasted into a component.
export function monoFontStack(): string {
  return cssVar('--font-mono') || 'ui-monospace, monospace';
}

export function resolveTheme(theme?: TerminalTheme | null): ITheme {
  const t = theme ?? MIRROR_DEFAULTS;
  const ansi = resolveAnsi(t.ansi, MIRROR_DEFAULTS.ansi!, {
    black: FALLBACK.black,
    red: FALLBACK.red,
    green: FALLBACK.green,
    yellow: FALLBACK.yellow,
    blue: FALLBACK.blue,
    magenta: FALLBACK.magenta,
    cyan: FALLBACK.cyan,
    white: FALLBACK.white,
  });
  const bright = resolveAnsi(t.bright_ansi, MIRROR_DEFAULTS.bright_ansi!, {
    black: FALLBACK.brightBlack,
    red: FALLBACK.brightRed,
    green: FALLBACK.brightGreen,
    yellow: FALLBACK.brightYellow,
    blue: FALLBACK.brightBlue,
    magenta: FALLBACK.brightMagenta,
    cyan: FALLBACK.brightCyan,
    white: FALLBACK.brightWhite,
  });

  return {
    background: resolve(t.background ?? MIRROR_DEFAULTS.background, FALLBACK.background),
    foreground: resolve(t.foreground ?? MIRROR_DEFAULTS.foreground, FALLBACK.foreground),
    cursor: resolve(t.cursor ?? MIRROR_DEFAULTS.cursor, FALLBACK.cursor),
    cursorAccent: resolve(t.cursorAccent ?? MIRROR_DEFAULTS.cursorAccent, FALLBACK.cursorAccent),
    selectionBackground: resolve(
      t.selectionBackground ?? MIRROR_DEFAULTS.selectionBackground,
      FALLBACK.selectionBackground,
    ),
    black: ansi.black,
    red: ansi.red,
    green: ansi.green,
    yellow: ansi.yellow,
    blue: ansi.blue,
    magenta: ansi.magenta,
    cyan: ansi.cyan,
    white: ansi.white,
    brightBlack: bright.black,
    brightRed: bright.red,
    brightGreen: bright.green,
    brightYellow: bright.yellow,
    brightBlue: bright.blue,
    brightMagenta: bright.magenta,
    brightCyan: bright.cyan,
    brightWhite: bright.white,
  };
}
