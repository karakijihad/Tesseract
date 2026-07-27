import { describe, it, expect } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { ActivityDetail } from "./ActivityDetail";
import type { ActivityRecord } from "../../stores/activity";

function rec(extra: Partial<ActivityRecord> = {}): ActivityRecord {
  return {
    activity_id: "routine:1",
    kind: "routine",
    label: "daily_brief",
    state: "running",
    durability: "persistent",
    provider: null,
    parent_turn_id: null,
    parent_session_id: null,
    transcript_ref: null,
    started_at: "",
    updated_at: "",
    ...extra,
  };
}

describe("ActivityDetail", () => {
  it("renders state/id and omits optional fields when absent", () => {
    const html = renderToStaticMarkup(<ActivityDetail record={rec()} />);
    expect(html).toContain("state running");
    expect(html).toContain("id routine:1");
    expect(html).not.toContain("provider");
    expect(html).not.toContain("session");
    expect(html).not.toContain("turn");
    expect(html).not.toContain("transcript");
  });

  it("renders provider/session/turn/transcript when present", () => {
    const html = renderToStaticMarkup(
      <ActivityDetail
        record={rec({
          provider: "claude",
          parent_session_id: "sess-1",
          parent_turn_id: "turn-1",
          transcript_ref: "ref-1",
        })}
      />,
    );
    expect(html).toContain("provider claude");
    expect(html).toContain("session sess-1");
    expect(html).toContain("turn turn-1");
    expect(html).toContain("transcript ref-1");
  });

  it("shows the result as an error line only when failed", () => {
    const html = renderToStaticMarkup(
      <ActivityDetail record={rec({ state: "failed", result: "boom" })} />,
    );
    expect(html).toContain("activity-map__error");
    expect(html).toContain("boom");
  });

  it("omits the error line when not failed even if result is set", () => {
    const html = renderToStaticMarkup(
      <ActivityDetail record={rec({ state: "done", result: "ok" })} />,
    );
    expect(html).not.toContain("activity-map__error");
  });
});
