import { describe, it, expect } from "vitest";
import { canSteer } from "./steer";

describe("canSteer", () => {
  it("is false when not streaming, regardless of draft", () => {
    expect(canSteer(false, "redirect this")).toBe(false);
  });

  it("is false when streaming but the draft is empty", () => {
    expect(canSteer(true, "")).toBe(false);
  });

  it("is false when streaming but the draft is whitespace-only", () => {
    expect(canSteer(true, "   \n\t")).toBe(false);
  });

  it("is true when streaming with a non-empty draft", () => {
    expect(canSteer(true, "actually do this instead")).toBe(true);
  });
});
