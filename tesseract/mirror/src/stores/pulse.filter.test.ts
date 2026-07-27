// Y-3 — pulse filter state lifted into the store so the pulse-filters card
// and the pulse-stream card share one filter. These lock the toggle logic
// that moved out of the old PulseView local state.

import { beforeEach, describe, expect, it } from 'vitest';

import { ALL_PULSE_TAGS, usePulseStore } from './pulse';

beforeEach(() => {
  usePulseStore.getState().resetFilter();
});

describe('pulse filter store', () => {
  it('defaults to all tags on (null)', () => {
    expect(usePulseStore.getState().enabledTags).toBeNull();
    expect(usePulseStore.getState().errorsOnly).toBe(false);
  });

  it('toggling one tag from "all" enables the rest', () => {
    usePulseStore.getState().toggleTag('tool');
    const set = usePulseStore.getState().enabledTags;
    expect(set).not.toBeNull();
    expect(set!.has('tool')).toBe(false);
    expect(set!.size).toBe(ALL_PULSE_TAGS.length - 1);
  });

  it('re-enabling the last missing tag collapses back to "all" (null)', () => {
    usePulseStore.getState().toggleTag('tool'); // now all-but-tool
    usePulseStore.getState().toggleTag('tool'); // tool back on → all
    expect(usePulseStore.getState().enabledTags).toBeNull();
  });

  it('setErrorsOnly + resetFilter', () => {
    usePulseStore.getState().setErrorsOnly(true);
    expect(usePulseStore.getState().errorsOnly).toBe(true);
    usePulseStore.getState().resetFilter();
    expect(usePulseStore.getState().errorsOnly).toBe(false);
    expect(usePulseStore.getState().enabledTags).toBeNull();
  });
});
