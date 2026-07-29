import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const fetchCapabilities = vi.fn();
const postCapabilitiesReverify = vi.fn();
const postCapabilitiesDismiss = vi.fn();

vi.mock("../lib/api", () => ({
  fetchCapabilities: (...args: unknown[]) => fetchCapabilities(...args),
  postCapabilitiesReverify: (...args: unknown[]) =>
    postCapabilitiesReverify(...args),
  postCapabilitiesDismiss: (...args: unknown[]) =>
    postCapabilitiesDismiss(...args),
}));

import {
  selectBrokenRoles,
  useCapabilityNoticeStore,
} from "./capabilityNotice";

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

describe("useCapabilityNoticeStore", () => {
  beforeEach(() => {
    resetStore();
    fetchCapabilities.mockReset();
    postCapabilitiesReverify.mockReset();
    postCapabilitiesDismiss.mockReset();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("fetch() populates roles and notice_dismissed from the report", async () => {
    fetchCapabilities.mockResolvedValue({
      roles: [brokenRole, okRole],
      notice_dismissed: false,
    });
    await useCapabilityNoticeStore.getState().fetch();
    const s = useCapabilityNoticeStore.getState();
    expect(s.roles).toEqual([brokenRole, okRole]);
    expect(s.noticeDismissed).toBe(false);
    expect(s.loaded).toBe(true);
    expect(s.error).toBeNull();
  });

  it("fetch() surfaces a rejected promise as a readable error without throwing", async () => {
    fetchCapabilities.mockRejectedValue(new Error("network unreachable"));
    await expect(
      useCapabilityNoticeStore.getState().fetch(),
    ).resolves.toBeUndefined();
    expect(useCapabilityNoticeStore.getState().error).toBe(
      "network unreachable",
    );
  });

  it("verify() replaces roles with the fresh reverify report — a fixed role clears broken", async () => {
    useCapabilityNoticeStore.setState({ roles: [brokenRole], loaded: true });
    postCapabilitiesReverify.mockResolvedValue({
      roles: [{ ...brokenRole, broken: false, reason: null, login_hint: null }],
      notice_dismissed: false,
    });
    await useCapabilityNoticeStore.getState().verify();
    const s = useCapabilityNoticeStore.getState();
    expect(selectBrokenRoles(s.roles)).toHaveLength(0);
    expect(s.verifying).toBe(false);
  });

  it("a second verify() call while one is in flight is a client-side no-op", async () => {
    let resolve!: (v: unknown) => void;
    postCapabilitiesReverify.mockReturnValue(new Promise((r) => (resolve = r)));
    const first = useCapabilityNoticeStore.getState().verify();
    const second = useCapabilityNoticeStore.getState().verify();
    resolve({ roles: [], notice_dismissed: false });
    await Promise.all([first, second]);
    expect(postCapabilitiesReverify).toHaveBeenCalledTimes(1);
  });

  it("dismiss() persists via the backend and sets noticeDismissed from the response", async () => {
    postCapabilitiesDismiss.mockResolvedValue({
      roles: [brokenRole],
      notice_dismissed: true,
    });
    await useCapabilityNoticeStore.getState().dismiss();
    expect(useCapabilityNoticeStore.getState().noticeDismissed).toBe(true);
    expect(postCapabilitiesDismiss).toHaveBeenCalledTimes(1);
  });

  it("dismiss() still hides the card client-side even if the backend write fails", async () => {
    postCapabilitiesDismiss.mockRejectedValue(new Error("write failed"));
    await useCapabilityNoticeStore.getState().dismiss();
    const s = useCapabilityNoticeStore.getState();
    expect(s.noticeDismissed).toBe(true);
    expect(s.error).toBe("write failed");
  });

  describe("selectBrokenRoles", () => {
    it("returns only broken roles", () => {
      expect(selectBrokenRoles([brokenRole, okRole])).toEqual([brokenRole]);
    });

    it("returns an empty list when nothing is broken", () => {
      expect(selectBrokenRoles([okRole])).toEqual([]);
    });
  });
});
