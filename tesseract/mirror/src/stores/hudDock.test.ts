// Sectioned dock (2026-07-31) — one stack open at a time; tucking closes any
// open stack so the restored bar never reappears with a stale popover.
import { describe, it, expect, beforeEach } from "vitest";
import { useHudDockStore } from "./hudDock";

describe("hudDock store", () => {
  beforeEach(() => {
    useHudDockStore.setState({ openSection: null, tucked: false });
  });

  it("toggles a section open and closed", () => {
    useHudDockStore.getState().toggleSection("views");
    expect(useHudDockStore.getState().openSection).toBe("views");
    useHudDockStore.getState().toggleSection("views");
    expect(useHudDockStore.getState().openSection).toBeNull();
  });

  it("opening a second section closes the first", () => {
    useHudDockStore.getState().toggleSection("stage");
    useHudDockStore.getState().toggleSection("views");
    expect(useHudDockStore.getState().openSection).toBe("views");
  });

  it("tucking the bar closes any open stack", () => {
    useHudDockStore.getState().toggleSection("observer");
    useHudDockStore.getState().setTucked(true);
    const s = useHudDockStore.getState();
    expect(s.tucked).toBe(true);
    expect(s.openSection).toBeNull();
  });
});
