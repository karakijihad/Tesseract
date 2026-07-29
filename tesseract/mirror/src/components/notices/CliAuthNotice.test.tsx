// Client-rendered (createRoot + act), not renderToStaticMarkup: zustand v5's
// SSR path (`useSyncExternalStore`'s getServerSnapshot) reads the store's
// frozen `getInitialState()`, not live `setState()` mutations, for plain
// data fields — renderToStaticMarkup would silently ignore every setState
// call this test makes. CSR uses the live snapshot, matching what the real
// app does (hydrateRoot/createRoot), so this is the correct render path to
// test against, not just a workaround.
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const fetchCapabilities = vi.fn();
const postCapabilitiesReverify = vi.fn();
const postCapabilitiesDismiss = vi.fn();

vi.mock("../../lib/api", () => ({
  fetchCapabilities: (...args: unknown[]) => fetchCapabilities(...args),
  postCapabilitiesReverify: (...args: unknown[]) =>
    postCapabilitiesReverify(...args),
  postCapabilitiesDismiss: (...args: unknown[]) =>
    postCapabilitiesDismiss(...args),
}));

import { CliAuthNotice } from "./CliAuthNotice";
import { useCapabilityNoticeStore } from "../../stores/capabilityNotice";

const brokenRole = {
  role: "claude_cli",
  broken: true,
  reason: "installed, not signed in",
  login_hint: "claude auth login",
};

const okRole = {
  role: "chat_brain",
  broken: false,
  reason: null,
  login_hint: null,
};

function resetStore() {
  useCapabilityNoticeStore.setState({
    roles: [],
    noticeDismissed: false,
    loaded: false,
    verifying: false,
    error: null,
  });
}

function findButton(container: HTMLElement, label: string): HTMLButtonElement {
  const btn = Array.from(container.querySelectorAll("button")).find((b) =>
    b.textContent?.startsWith(label),
  );
  if (!btn) throw new Error(`no button starting with "${label}"`);
  return btn;
}

describe("CliAuthNotice", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    resetStore();
    fetchCapabilities.mockReset().mockReturnValue(new Promise(() => {})); // never resolves — tests drive state directly
    postCapabilitiesReverify.mockReset();
    postCapabilitiesDismiss.mockReset();
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  it("renders nothing before the report has loaded", () => {
    act(() => {
      root.render(<CliAuthNotice />);
    });
    expect(container.innerHTML).toBe("");
  });

  it("renders the card with role name, login_hint, Verify, and Dismiss when a role is broken", () => {
    act(() => {
      root.render(<CliAuthNotice />);
    });
    act(() => {
      useCapabilityNoticeStore.setState({ roles: [brokenRole], loaded: true });
    });
    expect(container.innerHTML).toContain("claude_cli");
    expect(container.innerHTML).toContain("claude auth login");
    expect(container.textContent).toContain("Verify");
    expect(container.textContent).toContain("Dismiss");
  });

  it("does not render when no role is broken", () => {
    act(() => {
      root.render(<CliAuthNotice />);
    });
    act(() => {
      useCapabilityNoticeStore.setState({ roles: [okRole], loaded: true });
    });
    expect(container.innerHTML).toBe("");
  });

  it("self-suppresses when no role is broken even if never dismissed", () => {
    act(() => {
      root.render(<CliAuthNotice />);
    });
    act(() => {
      useCapabilityNoticeStore.setState({
        roles: [okRole],
        loaded: true,
        noticeDismissed: false,
      });
    });
    expect(container.innerHTML).toBe("");
  });

  it("does not render once dismissed, even with a broken role", () => {
    act(() => {
      root.render(<CliAuthNotice />);
    });
    act(() => {
      useCapabilityNoticeStore.setState({
        roles: [brokenRole],
        loaded: true,
        noticeDismissed: true,
      });
    });
    expect(container.innerHTML).toBe("");
  });

  it("clicking Verify calls reverify and hides the card once the role is fixed", async () => {
    postCapabilitiesReverify.mockResolvedValue({
      roles: [{ ...brokenRole, broken: false, reason: null, login_hint: null }],
      notice_dismissed: false,
    });
    act(() => {
      root.render(<CliAuthNotice />);
    });
    act(() => {
      useCapabilityNoticeStore.setState({ roles: [brokenRole], loaded: true });
    });
    const verifyBtn = findButton(container, "Verify");
    await act(async () => {
      verifyBtn.click();
    });
    expect(postCapabilitiesReverify).toHaveBeenCalledTimes(1);
    expect(container.innerHTML).toBe("");
  });

  it("clicking Dismiss calls the dismiss endpoint and hides the card", async () => {
    postCapabilitiesDismiss.mockResolvedValue({
      roles: [brokenRole],
      notice_dismissed: true,
    });
    act(() => {
      root.render(<CliAuthNotice />);
    });
    act(() => {
      useCapabilityNoticeStore.setState({ roles: [brokenRole], loaded: true });
    });
    const dismissBtn = findButton(container, "Dismiss");
    await act(async () => {
      dismissBtn.click();
    });
    expect(postCapabilitiesDismiss).toHaveBeenCalledTimes(1);
    expect(container.innerHTML).toBe("");
  });
});
