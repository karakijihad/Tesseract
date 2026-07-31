// LaTeX-in-chat (2026-07-31) — $$...$$ and $...$ used to render as literal
// escaped text; remark-math + rehype-katex now typeset them. CSS import is
// stubbed (vitest has no bundler CSS pipeline for bare imports in deps).
import { describe, it, expect, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

vi.mock("katex/dist/katex.min.css", () => ({}));

import { ChatMarkdown } from "./ChatMarkdown";

describe("ChatMarkdown math rendering", () => {
  it("renders display math as KaTeX markup, not literal dollars", () => {
    const html = renderToStaticMarkup(
      <ChatMarkdown>{"The formula:\n\n$$E = mc^2$$"}</ChatMarkdown>,
    );
    expect(html).toContain("katex");
    expect(html).not.toContain("$$E = mc^2$$");
  });

  it("renders inline math", () => {
    const html = renderToStaticMarkup(
      <ChatMarkdown>{"Given $x = 2$, the result follows."}</ChatMarkdown>,
    );
    expect(html).toContain("katex");
  });

  it("leaves plain text and code untouched", () => {
    const html = renderToStaticMarkup(
      <ChatMarkdown>{"No math here, just `code` and text."}</ChatMarkdown>,
    );
    expect(html).not.toContain("katex");
    expect(html).toContain("code");
  });

  it("renders invalid TeX without crashing (throwOnError: false)", () => {
    const html = renderToStaticMarkup(
      <ChatMarkdown>{"$\\unknowncommand{x}$"}</ChatMarkdown>,
    );
    expect(html.length).toBeGreaterThan(0);
  });
});
