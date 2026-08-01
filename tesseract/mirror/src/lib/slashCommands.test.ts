import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';

// The loader caches into module state, so each case needs a fresh module.
async function freshModule() {
  vi.resetModules();
  return import('./slashCommands');
}

function jsonResponse(commands: unknown[]) {
  return {
    ok: true,
    status: 200,
    headers: { get: () => 'application/json' },
    json: async () => ({ commands }),
  };
}

const NOT_READY = { ok: false, status: 503, headers: { get: () => 'text/plain' } };

const SPEC = {
  name: 'save',
  summary: 'save current session',
  source: 'mirror_session',
  aliases: [],
  arg_label: null,
  arg_help: null,
  mutates_session: true,
};

describe('loadSlashCommands', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it('keeps retrying past the old ~28s budget and hydrates when the registry appears', async () => {
    // Regression: the packaged install takes ~39s from backend spawn to
    // `command_registry: N specs ready`. The previous fixed 9-step schedule
    // gave up at ~28s and cached nothing, so typing `/` showed no list for
    // the lifetime of the page. 60 x 503 far exceeds that old budget.
    let calls = 0;
    const fetchMock = vi.fn(async () => {
      calls += 1;
      return calls <= 60 ? NOT_READY : jsonResponse([SPEC]);
    });
    vi.stubGlobal('fetch', fetchMock);

    const mod = await freshModule();
    const pending = mod.loadSlashCommands();
    await vi.runAllTimersAsync();
    await pending;

    expect(mod.isSlashCommandsLoaded()).toBe(true);
    expect(mod.matchingCommands('')).toHaveLength(1);
    expect(calls).toBeGreaterThan(60);
  });

  it('retries an empty 200 instead of caching the built-but-empty registry', async () => {
    let calls = 0;
    const fetchMock = vi.fn(async () => {
      calls += 1;
      return calls <= 3 ? jsonResponse([]) : jsonResponse([SPEC]);
    });
    vi.stubGlobal('fetch', fetchMock);

    const mod = await freshModule();
    const pending = mod.loadSlashCommands();
    await vi.runAllTimersAsync();
    await pending;

    expect(mod.isSlashCommandsLoaded()).toBe(true);
    expect(mod.matchingCommands('')).toHaveLength(1);
  });

  it('survives a thrown fetch (backend down / restarting) and recovers', async () => {
    let calls = 0;
    const fetchMock = vi.fn(async () => {
      calls += 1;
      if (calls <= 5) throw new Error('ECONNREFUSED');
      return jsonResponse([SPEC]);
    });
    vi.stubGlobal('fetch', fetchMock);

    const mod = await freshModule();
    const pending = mod.loadSlashCommands();
    await vi.runAllTimersAsync();
    await pending;

    expect(mod.isSlashCommandsLoaded()).toBe(true);
  });

  it('lists every command for a bare "/" once loaded', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse([SPEC, { ...SPEC, name: 'fork' }])));

    const mod = await freshModule();
    const pending = mod.loadSlashCommands();
    await vi.runAllTimersAsync();
    await pending;

    // `SlashCommandHint` parses a bare "/" into { kind: 'list', query: '' }.
    expect(mod.matchingCommands('').map((c) => c.name).sort()).toEqual(['fork', 'save']);
  });
});
