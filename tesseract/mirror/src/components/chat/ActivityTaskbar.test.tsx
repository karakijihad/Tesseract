import { act } from "react";
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { createRoot } from "react-dom/client";
import { ActivityTaskbar } from "./ActivityTaskbar";
import { useActivityStore, type ActivityRecord } from "../../stores/activity";
import * as openActivityModule from "../../canvas/openActivity";

function rec(
  kind: string,
  id: string,
  extra: Partial<ActivityRecord> = {},
): ActivityRecord {
  return {
    activity_id: `${kind}:${id}`,
    kind,
    label: `${kind}-${id}`,
    state: "running",
    durability: "ephemeral",
    provider: null,
    parent_turn_id: null,
    parent_session_id: null,
    transcript_ref: null,
    started_at: "",
    updated_at: "",
    ...extra,
  };
}

describe("ActivityTaskbar", () => {
  beforeEach(() => {
    useActivityStore.setState({ byId: {} });
  });

  it("renders nothing when no records are running", () => {
    expect(renderToStaticMarkup(<ActivityTaskbar />)).toBe("");
  });

  it("counts running records across ALL kinds, including invoke_agent-shaped delegates", () => {
    // invoke_agent spawns register as kind="delegate" (brain/spawns.py
    // _spawn_record always hardcodes kind="delegate" regardless of the
    // registry `kind` string it was minted with) — same bucket as
    // delegate_claude/codex. routine/autonomy/mcp_session are kinds the old
    // RunningSpawnsChip never read at all.
    useActivityStore.setState({
      byId: {
        "delegate:1": rec("delegate", "1", {
          label: "invoke_agent(daily-brief)",
        }),
        "lane:1": rec("lane", "1"),
        "controller_session:1": rec("controller_session", "1"),
        "routine:1": rec("routine", "1"),
        "autonomy:1": rec("autonomy", "1"),
        "mcp_session:1": rec("mcp_session", "1"),
      },
    });
    const html = renderToStaticMarkup(<ActivityTaskbar />);
    expect(html).toContain("6 running");
  });

  it("ignores non-running records", () => {
    useActivityStore.setState({
      byId: { "routine:1": rec("routine", "1", { state: "done" }) },
    });
    expect(renderToStaticMarkup(<ActivityTaskbar />)).toBe("");
  });
});

describe("ActivityTaskbar (mounted) — row click wiring", () => {
  const g = globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT?: boolean };
  let host: HTMLDivElement;

  beforeEach(() => {
    g.IS_REACT_ACT_ENVIRONMENT = true;
    useActivityStore.setState({ byId: {} });
    host = document.createElement("div");
    document.body.appendChild(host);
  });

  afterEach(() => {
    host.remove();
    vi.restoreAllMocks();
  });

  it("routes an openable row (delegate) through openActivity, not the inline detail block", async () => {
    const spy = vi
      .spyOn(openActivityModule, "openActivity")
      .mockResolvedValue();
    useActivityStore.setState({
      byId: {
        "delegate:1": rec("delegate", "1", {
          label: "invoke_agent(daily-brief)",
        }),
      },
    });
    const root = createRoot(host);
    try {
      await act(async () => root.render(<ActivityTaskbar />));
      const chip = host.querySelector(
        ".activity-taskbar-chip",
      ) as HTMLButtonElement;
      await act(async () => chip.click());
      const row = host.querySelector(
        ".activity-taskbar-row",
      ) as HTMLButtonElement;
      await act(async () => row.click());
      expect(spy).toHaveBeenCalledTimes(1);
      expect(spy.mock.calls[0][0].activity_id).toBe("delegate:1");
      expect(host.querySelector(".activity-map__detail")).toBeNull();
    } finally {
      await act(async () => root.unmount());
    }
  });

  it("toggles the inline detail block for a non-openable row (routine) without calling openActivity", async () => {
    const spy = vi
      .spyOn(openActivityModule, "openActivity")
      .mockResolvedValue();
    useActivityStore.setState({
      byId: { "routine:1": rec("routine", "1", { label: "daily_brief" }) },
    });
    const root = createRoot(host);
    try {
      await act(async () => root.render(<ActivityTaskbar />));
      const chip = host.querySelector(
        ".activity-taskbar-chip",
      ) as HTMLButtonElement;
      await act(async () => chip.click());
      const row = host.querySelector(
        ".activity-taskbar-row",
      ) as HTMLButtonElement;
      await act(async () => row.click());
      expect(spy).not.toHaveBeenCalled();
      expect(host.querySelector(".activity-map__detail")).not.toBeNull();
    } finally {
      await act(async () => root.unmount());
    }
  });
});
