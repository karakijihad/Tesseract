import type { TerminalKeybinding } from '../types';

// Parses a combo like "Ctrl+Shift+T" or "Meta+F" or "Ctrl+`" into a canonical
// form: modifiers normalized to { ctrl, shift, alt, meta } bools and the key
// lowercased. Non-modifier names use the `KeyboardEvent.key` convention
// (e.g., "PageUp", "ArrowLeft") — case-insensitive match at runtime.
interface ParsedCombo {
  ctrl: boolean;
  shift: boolean;
  alt: boolean;
  meta: boolean;
  key: string;
}

function parseCombo(combo: string): ParsedCombo {
  const parts = combo.split('+').map((p) => p.trim());
  const out: ParsedCombo = { ctrl: false, shift: false, alt: false, meta: false, key: '' };
  for (const p of parts) {
    const lower = p.toLowerCase();
    if (lower === 'ctrl' || lower === 'control') out.ctrl = true;
    else if (lower === 'shift') out.shift = true;
    else if (lower === 'alt' || lower === 'option') out.alt = true;
    else if (lower === 'meta' || lower === 'cmd' || lower === 'command' || lower === 'win') out.meta = true;
    else out.key = lower;
  }
  return out;
}

export function matches(event: KeyboardEvent, combo: string): boolean {
  const c = parseCombo(combo);
  if (event.ctrlKey !== c.ctrl) return false;
  if (event.shiftKey !== c.shift) return false;
  if (event.altKey !== c.alt) return false;
  if (event.metaKey !== c.meta) return false;
  return event.key.toLowerCase() === c.key;
}

/**
 * If any configured binding matches `event`, run its command and return true.
 * Returns false when no binding matched (caller may continue default handling).
 * Ignores events originating inside text inputs unless the command is marked
 * terminal.search (Ctrl+F works even when a field is focused).
 */
export function dispatch(
  event: KeyboardEvent,
  bindings: TerminalKeybinding[] | undefined,
  commands: Record<string, () => void>,
): boolean {
  if (!bindings || bindings.length === 0) return false;

  const target = event.target as HTMLElement | null;
  // xterm's hidden textarea (`.xterm-helper-textarea`) lives inside `.xterm`.
  // We treat focus there as terminal focus, not "typing into a form input".
  const inXterm = !!target?.closest?.('.xterm');
  const inEditable = !inXterm && !!target && (
    target.tagName === 'INPUT' ||
    target.tagName === 'TEXTAREA' ||
    target.isContentEditable
  );

  for (const b of bindings) {
    if (!matches(event, b.combo)) continue;
    // Always allow search binding to pass through inputs; other bindings are
    // suppressed when typing into a real form input so we don't steal keystrokes.
    if (inEditable && b.command !== 'terminal.search') continue;
    const fn = commands[b.command];
    if (!fn) continue;
    event.preventDefault();
    // Stop xterm's textarea from also receiving the chord (capture-phase listener).
    event.stopPropagation();
    fn();
    return true;
  }
  return false;
}

// Built-in fallback bindings used when `terminal.yaml` does not define any.
// Avoid Ctrl+Shift+W / Ctrl+Shift+T — the browser owns those (close tab /
// reopen tab) and the keypress reaches the OS-level shortcut before any
// JS preventDefault can fire.
export const DEFAULT_BINDINGS: TerminalKeybinding[] = [
  { combo: 'Ctrl+Shift+K', command: 'terminal.newTab' },
  { combo: 'Ctrl+Shift+X', command: 'terminal.closeTab' },
  { combo: 'Ctrl+Shift+D', command: 'terminal.splitVertical' },
  { combo: 'Ctrl+Shift+E', command: 'terminal.splitHorizontal' },
  { combo: 'Alt+Shift+X', command: 'terminal.closePane' },
  { combo: 'Ctrl+PageUp', command: 'terminal.prevTab' },
  { combo: 'Ctrl+PageDown', command: 'terminal.nextTab' },
  { combo: 'Ctrl+F', command: 'terminal.search' },
  { combo: 'Meta+F', command: 'terminal.search' },
];
