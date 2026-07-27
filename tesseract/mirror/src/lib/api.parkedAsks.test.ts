// Parked-ask operator-approval api (trio W4 / M3): list parked + decide by the
// server-minted approval_id.

import { afterEach, describe, expect, it, vi } from "vitest";

import { decideParkedAsk, getParkedAsks } from "./api";

afterEach(() => vi.unstubAllGlobals());

const SAMPLE = {
  approval_id: "appr-1",
  call_id: "call-1",
  session_id: "s-1",
  tool_name: "file_write",
  input_summary: "path=x.txt",
  spawn_handle_id: "del-abc",
  parked_at: "2026-07-10T00:00:00+00:00",
};

describe("parked asks api", () => {
  it("getParkedAsks returns the items array", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        json: async () => ({ items: [SAMPLE] }),
      })),
    );
    expect(await getParkedAsks()).toEqual([SAMPLE]);
  });

  it("getParkedAsks returns [] on a non-ok response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: false, json: async () => ({}) })),
    );
    expect(await getParkedAsks()).toEqual([]);
  });

  it("getParkedAsks returns [] when fetch throws", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new Error("network");
      }),
    );
    expect(await getParkedAsks()).toEqual([]);
  });

  it("decideParkedAsk posts {approved} to the approval id (not the call_id)", async () => {
    const spy = vi.fn(async () => ({
      ok: true,
      json: async () => ({ approved: true }),
    }));
    vi.stubGlobal("fetch", spy);
    await decideParkedAsk("appr-1", false);
    expect(spy).toHaveBeenCalledWith(
      expect.stringContaining("/api/asks/appr-1/decision"),
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ approved: false }),
      }),
    );
  });
});
