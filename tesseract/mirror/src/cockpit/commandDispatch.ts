// SC-4 — command / VOX dispatch. Parses a line of command (or committed voice)
// text and routes it to ONE of two cockpit actions, returning `true` when it
// consumed the text so the caller skips the chat-brain send:
//
//   1. open a whole view as a glass panel        ("show me schedule")
//   2. spawn a Surface-Protocol content surface   ("open https://…", "youtube …",
//      "create html …", "open folder …", "open file …")
//
// Unrecognized text returns `false` → the caller hands it to the assistant as normal
// chat. Recognition is intentionally conservative (explicit verb + known
// target, or a bare exact view name) so ordinary prose like "what's on my
// schedule" is NOT hijacked into opening a panel.
//
// Surfaces are spawned through the existing Surface Protocol REST route and
// render in the cockpit's `SurfaceLayer` (re-homed into `CockpitStage`), reusing
// the Y-2 renderers + `stores/surfaces.ts` verbatim — no new panel machinery.

import { BACKEND_BASE } from '../lib/endpoints';
import type { View } from '../stores/ui';
import { usePanelStore } from './panelStore';

// The cockpit hosts every spawned surface on the canonical `orb` view (the orb
// home); `SurfaceLayer view="orb"` renders them over the orb.
const COCKPIT_VIEW = 'orb';

// Views the operator can summon by name. `orb` is excluded — it is the orb
// home, reached by closing panels, not opening one (openPanel routes it to
// resetAll anyway, but naming it here would let "orb" masquerade as a panel).
const VIEW_NAMES: Exclude<View, 'orb'>[] = [
  'autonomy',
  'chat',
  'terminal',
  'pulse',
  'identity',
  'schedule',
  'agents',
  'conscience',
  'channels',
  'workspace',
  'settings',
];

// A handful of natural aliases → canonical view.
const VIEW_ALIASES: Record<string, Exclude<View, 'orb'>> = {
  logs: 'pulse',
  feed: 'pulse',
  events: 'pulse',
  agent: 'agents',
  config: 'settings',
  preferences: 'settings',
  conversation: 'chat',
  shell: 'terminal',
  console: 'terminal',
  inbox: 'workspace',
  // The Identity tab still renders SOUL.md and now owns the voice picker,
  // so both stay usable as spoken targets after the AS-5 rename.
  soul: 'identity',
  voice: 'identity',
};

// Leading command verbs we strip before matching a view target. Longest first
// so "show me" wins over "show".
const VIEW_VERBS = ['show me', 'go to', 'open up', 'switch to', 'open', 'show', 'summon', 'display', 'view', 'launch'];

interface SurfaceSpawn {
  type: string;
  title: string;
  props: Record<string, unknown>;
  size: { w: number; h: number };
}

export interface CommandDeps {
  openPanel: (view: View) => void;
  spawnSurface: (view: string, spawn: SurfaceSpawn) => Promise<void>;
}

const defaultDeps: CommandDeps = {
  openPanel: (view) => usePanelStore.getState().openPanel(view),
  spawnSurface: postSurface,
};

async function postSurface(view: string, spawn: SurfaceSpawn): Promise<void> {
  await fetch(`${BACKEND_BASE}/api/surfaces/${encodeURIComponent(view)}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      type: spawn.type,
      title: spawn.title,
      position: { x: 140, y: 110 },
      size: spawn.size,
      props: spawn.props,
    }),
  });
}

// youtu.be/<id>, youtube.com/watch?v=<id>, youtube.com/embed/<id>.
function youtubeEmbedUrl(text: string): string | null {
  const m = text.match(
    /(?:youtube\.com\/(?:watch\?v=|embed\/)|youtu\.be\/)([A-Za-z0-9_-]{11})/i,
  );
  if (m) return `https://www.youtube.com/embed/${m[1]}`;
  // Bare 11-char id after the word "youtube".
  const idOnly = text.match(/youtube\s+([A-Za-z0-9_-]{11})\b/i);
  return idOnly ? `https://www.youtube.com/embed/${idOnly[1]}` : null;
}

// Parse a surface-spawn command. Returns null when the text isn't one. Patterns
// are anchored to imperative command forms (a leading verb / keyword) so that
// ordinary chat sharing the same box — "show me the html spec", "what does
// https://x.com say?" — is NOT hijacked into a spawn.
function parseSurfaceCommand(text: string): SurfaceSpawn | null {
  // YouTube — explicit keyword or a youtube link, converted to an embed URL.
  if (/\byoutube\b|youtu\.be/i.test(text)) {
    const embed = youtubeEmbedUrl(text);
    if (embed) {
      return { type: 'webview', title: 'YouTube', props: { url: embed }, size: { w: 560, h: 360 } };
    }
  }

  // Inline HTML — anchored "create|render|new|open (an) html [<markup>]" or
  // "html: <markup>". The remainder after the keyword is the markup; empty
  // markup yields a minimal placeholder document.
  if (/^(create|render|new|open)\s+(an?\s+)?html\b/i.test(text) || /^html[:\s]/i.test(text)) {
    const markup = text.replace(/^.*?\bhtml\b[:\s]*/i, '').trim();
    return {
      type: 'html',
      title: 'HTML',
      props: { html: markup || '<!doctype html><body style="font-family:sans-serif;padding:1rem">New HTML surface</body>' },
      size: { w: 520, h: 400 },
    };
  }

  // A web page — a URL plus an anchored open-verb, a bare URL, or an explicit
  // url/website/browser keyword. A URL buried in a chat question is left alone.
  const url = text.match(/\bhttps?:\/\/[^\s]+/i)?.[0] ?? null;
  if (url) {
    const anchoredOpen = /^(open|show|browse|visit|go to|load)\b/i.test(text);
    const bareUrl = /^https?:\/\/\S+$/i.test(text);
    const keyword = /\b(url|website|web ?page|browser)\b/i.test(text);
    if (anchoredOpen || bareUrl || keyword) {
      return { type: 'webview', title: url, props: { url }, size: { w: 640, h: 460 } };
    }
  }

  // Folder — anchored "(open|show) folder <path>". The verb is mandatory so a
  // bare chat line like "folder: my notes" is left for the assistant, not hijacked.
  const folder = text.match(/^(?:open|show)\s+folder[:\s]+(.+)$/i);
  if (folder) {
    const root = folder[1].trim();
    return { type: 'folder', title: root || 'folder', props: { root }, size: { w: 420, h: 360 } };
  }

  // File — anchored "(open|show) file <path>" (verb mandatory, same reason as
  // folder). The frontend has no file contents; carry the path as the title +
  // body (a backend tool spawns rich file surfaces with real content).
  const file = text.match(/^(?:open|show)\s+file[:\s]+(.+)$/i);
  if (file) {
    const path = file[1].trim();
    return { type: 'file', title: path || 'file', props: { text: path }, size: { w: 480, h: 380 } };
  }

  return null;
}

// Parse an open-view command. Strips a leading verb, then matches the remainder
// against a view name / alias. A bare exact view name (no verb) also matches.
function parseViewCommand(lower: string): View | null {
  let rest = lower;
  for (const verb of VIEW_VERBS) {
    if (rest === verb) return null; // verb with no target
    if (rest.startsWith(verb + ' ')) {
      rest = rest.slice(verb.length + 1).trim();
      break;
    }
  }
  // Drop a leading article + trailing "tab"/"view"/"panel" noise.
  rest = rest.replace(/^(the|a|an|my)\s+/, '').replace(/\s+(tab|view|panel)$/, '').trim();
  if (!rest) return null;

  if ((VIEW_NAMES as string[]).includes(rest)) return rest as View;
  if (rest in VIEW_ALIASES) return VIEW_ALIASES[rest];
  return null;
}

// Route a command line. Returns true when consumed (caller skips chat send).
export function dispatchCommand(raw: string, deps: CommandDeps = defaultDeps): boolean {
  const text = raw.trim();
  if (!text) return false;
  const lower = text.toLowerCase();

  const surface = parseSurfaceCommand(text);
  if (surface) {
    void deps.spawnSurface(COCKPIT_VIEW, surface);
    return true;
  }

  const view = parseViewCommand(lower);
  if (view) {
    deps.openPanel(view);
    return true;
  }

  return false;
}

// Exposed for tests.
export const _internal = { parseSurfaceCommand, parseViewCommand };
