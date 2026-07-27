// SC-4 — command / VOX dispatch. Parsing + routing, using injected deps so the
// test touches no DOM, no store, and no network.

import { describe, expect, it, vi } from 'vitest';

import { dispatchCommand, _internal, type CommandDeps } from './commandDispatch';

const { parseSurfaceCommand, parseViewCommand, isTrioCommand } = _internal;

function deps() {
  const openPanel = vi.fn();
  const spawnTrio = vi.fn(async () => []);
  const spawnSurface = vi.fn(async () => undefined);
  const d: CommandDeps = { openPanel, spawnTrio, spawnSurface };
  return { d, openPanel, spawnTrio, spawnSurface };
}

describe('SC-4 dispatchCommand — open views', () => {
  it('opens a view by bare exact name', () => {
    const { d, openPanel } = deps();
    expect(dispatchCommand('schedule', d)).toBe(true);
    expect(openPanel).toHaveBeenCalledWith('schedule');
  });

  it('opens a view with a verb prefix ("show me pulse")', () => {
    const { d, openPanel } = deps();
    expect(dispatchCommand('show me pulse', d)).toBe(true);
    expect(openPanel).toHaveBeenCalledWith('pulse');
  });

  it('strips articles and trailing "tab"/"view" ("open the settings tab")', () => {
    const { d, openPanel } = deps();
    expect(dispatchCommand('open the settings tab', d)).toBe(true);
    expect(openPanel).toHaveBeenCalledWith('settings');
  });

  it('resolves an alias ("logs" → pulse)', () => {
    const { d, openPanel } = deps();
    expect(dispatchCommand('show logs', d)).toBe(true);
    expect(openPanel).toHaveBeenCalledWith('pulse');
  });

  it('does NOT hijack ordinary prose ("what\'s on my schedule")', () => {
    const { d, openPanel } = deps();
    expect(dispatchCommand("what's on my schedule", d)).toBe(false);
    expect(openPanel).not.toHaveBeenCalled();
  });

  it('does NOT open the orb home by name ("tars" is not a panel)', () => {
    expect(parseViewCommand('tars')).toBeNull();
  });

  it('a verb with no target is not a command', () => {
    expect(parseViewCommand('show')).toBeNull();
    expect(parseViewCommand('open')).toBeNull();
  });

  it('does NOT open a rail by name (kernel/lifeline are not views)', () => {
    const { d, openPanel } = deps();
    expect(dispatchCommand('kernel', d)).toBe(false);
    expect(dispatchCommand('lifeline', d)).toBe(false);
    expect(openPanel).not.toHaveBeenCalled();
  });
});

describe('SC-4 dispatchCommand — trio', () => {
  it('spawns the trio on "spawn trio"', () => {
    const { d, spawnTrio } = deps();
    expect(dispatchCommand('spawn trio', d)).toBe(true);
    expect(spawnTrio).toHaveBeenCalledWith('tars');
  });

  it('recognizes "launch the trio"', () => {
    expect(isTrioCommand('launch the trio')).toBe(true);
  });

  it('a bare "trio" with no verb is not a trio command', () => {
    expect(isTrioCommand('trio')).toBe(false);
  });
});

describe('SC-4 dispatchCommand — spawn surfaces', () => {
  it('spawns a webview for an http(s) URL', () => {
    const s = parseSurfaceCommand('open https://example.com/docs');
    expect(s?.type).toBe('webview');
    expect(s?.props.url).toBe('https://example.com/docs');
  });

  it('spawns a youtube embed (watch URL → /embed/<id>)', () => {
    const s = parseSurfaceCommand('youtube https://www.youtube.com/watch?v=dQw4w9WgXcQ');
    expect(s?.type).toBe('webview');
    expect(s?.props.url).toBe('https://www.youtube.com/embed/dQw4w9WgXcQ');
  });

  it('spawns a youtube embed from youtu.be short link', () => {
    const s = parseSurfaceCommand('open the youtu.be/dQw4w9WgXcQ clip');
    expect(s?.props.url).toBe('https://www.youtube.com/embed/dQw4w9WgXcQ');
  });

  it('spawns an html surface carrying the markup', () => {
    const s = parseSurfaceCommand('create html <h1>hello</h1>');
    expect(s?.type).toBe('html');
    expect(s?.props.html).toBe('<h1>hello</h1>');
  });

  it('spawns an html surface with a placeholder when no markup given', () => {
    const s = parseSurfaceCommand('create an html');
    expect(s?.type).toBe('html');
    expect(String(s?.props.html)).toContain('New HTML surface');
  });

  it('spawns a folder surface carrying the path', () => {
    const s = parseSurfaceCommand('open folder: /home/op/project');
    expect(s?.type).toBe('folder');
    expect(s?.props.root).toBe('/home/op/project');
  });

  it('spawns a file surface carrying the path', () => {
    const s = parseSurfaceCommand('open file README.md');
    expect(s?.type).toBe('file');
    expect(s?.props.text).toBe('README.md');
  });

  it('does NOT hijack a bare "folder:"/"file:" chat line (verb mandatory)', () => {
    expect(parseSurfaceCommand('folder: my notes on the project')).toBeNull();
    expect(parseSurfaceCommand('file: thoughts')).toBeNull();
  });

  it('routes a surface command through spawnSurface, not openPanel', () => {
    const { d, openPanel, spawnSurface } = deps();
    expect(dispatchCommand('open https://example.com', d)).toBe(true);
    expect(spawnSurface).toHaveBeenCalledTimes(1);
    expect(spawnSurface).toHaveBeenCalledWith('tars', expect.objectContaining({ type: 'webview' }));
    expect(openPanel).not.toHaveBeenCalled();
  });

  it('returns false for unrecognized text', () => {
    const { d } = deps();
    expect(dispatchCommand('tell me a joke', d)).toBe(false);
  });
});
