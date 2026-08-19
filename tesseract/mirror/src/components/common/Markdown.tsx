import {
  Children,
  isValidElement,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkBreaks from "remark-breaks";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeHighlight from "rehype-highlight";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";
import { ChatArtifact, isPreviewableArtifact } from "../chat/ChatArtifact";
import { copyToClipboard } from "../../lib/clipboard";
import { ExpandOverlay } from "./ExpandOverlay";
import { backendAssetUrl } from "../../lib/endpoints";

interface Props {
  children: string;
  // `inline` flattens headings, paragraphs and lists to their text so a row
  // that must stay one line stays one line, while bold/italic/code/links
  // still render. Block constructs and math are dropped rather than shown.
  variant?: "block" | "inline";
  // While a reply streams, an emphasis run whose closing delimiter has not
  // arrived yet is trimmed instead of being printed as literal syntax.
  streaming?: boolean;
}

const components: Components = {
  a({ node: _node, href, children, ...props }) {
    return (
      <a
        {...props}
        href={href}
        target="_blank"
        rel="noopener noreferrer"
        className="linked-anchor"
      >
        {children}
      </a>
    );
  },
  img({ node: _node, alt, src, ...props }) {
    // The model emits `![alt](/api/downloads/...)` referencing artifacts the
    // backend just produced. In dev, frontend (port 1420) and backend (8000)
    // are different origins, so a bare `/api/...` URL would 404 against the
    // Vite origin. `backendAssetUrl` rewrites root-relative paths to the
    // resolved BACKEND_BASE; absolute URLs and data: URIs pass through.
    const resolvedSrc = typeof src === "string" ? backendAssetUrl(src) : src;
    return (
      <img
        {...props}
        src={resolvedSrc}
        alt={alt ?? ""}
        loading="lazy"
        className="md-image"
      />
    );
  },
  pre({ node: _node, children, ...props }) {
    const code = textFromNode(children).replace(/\n$/, "");
    const language = languageFromCodeNode(children);
    if (isPreviewableArtifact(language)) {
      return <ChatArtifact code={code} language={language} />;
    }
    return (
      <div className="md-codeblock">
        <CodeExpandButton code={code} language={language} />
        <CodeCopyButton code={code} />
        <pre {...props}>{children}</pre>
      </div>
    );
  },
  table({ node: _node, children, ...props }) {
    return (
      <div className="md-table-wrap">
        <table {...props}>{children}</table>
      </div>
    );
  },
};

// KaTeX `output: 'html'` — the default dual HTML+MathML output duplicates
// every formula in text-selection copies; HTML-only keeps the DOM lean.
// `throwOnError: false` renders bad TeX in red instead of crashing the bubble.
const rehypePlugins = [
  [rehypeKatex, { output: "html", throwOnError: false }],
  [rehypeHighlight, { detect: true, ignoreMissing: true }],
] as const;

// Everything a one-line row may render. Anything else is unwrapped to its
// text, so a heading or a list item contributes its words and not its bullet.
const INLINE_ELEMENTS = ["a", "strong", "em", "code", "del"];

export function Markdown({
  children,
  variant = "block",
  streaming = false,
}: Props) {
  const source = streaming ? trimDanglingMarks(children) : children;

  // The root class is emitted here, never by the caller. A surface gets the
  // app's markdown by rendering this component; there is nothing to remember.
  if (variant === "inline") {
    return (
      <span className="md-inline">
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          allowedElements={INLINE_ELEMENTS}
          unwrapDisallowed
          components={components}
        >
          {source}
        </ReactMarkdown>
      </span>
    );
  }

  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkBreaks, remarkMath]}
        rehypePlugins={rehypePlugins as never}
        components={components}
      >
        {source}
      </ReactMarkdown>
    </div>
  );
}

// Emphasis delimiters, longest first: `**` must be consumed before the single
// `*` count is taken, or a complete bold pair reads as two odd singles.
const STREAM_MARKS = ["**", "`", "*"] as const;

// A COMPLETE inline-code span. Its contents are code, not prose, so the
// delimiters inside it must not be counted or trimmed — `**/*.py`, `x**y` and
// a shell-quoted `"**"` all carry an odd emphasis count that belongs to the
// code. Masked to a same-length run so indices into the mask still address the
// original string.
//
// The leading run is captured and back-referenced, so a span opened with two
// backticks (CommonMark's way of writing a span that itself contains one)
// masks as ONE unit. Matching a single backtick greedily read ``x**y`` as two
// empty spans with bare text between them, which put the `**` back in play —
// the same defect one form over.
const CLOSED_CODE_SPAN = /(`+)(?:[^`\n]|`(?!\1))*?\1(?!`)/g;

function maskCodeSpans(text: string): string {
  // A space, not a NUL. The mask only has to be free of the three delimiters
  // being counted, and a literal NUL in the source makes Git classify this
  // file as binary — which silently costs every future diff of it.
  return text.replace(CLOSED_CODE_SPAN, (span) => " ".repeat(span.length));
}

function occurrences(text: string, mark: string): number {
  return text.split(mark).length - 1;
}

/**
 * Drop the last unpaired emphasis delimiter from a partially streamed reply.
 *
 * `**Intro` renders as `Intro` and thickens when the closing `**` arrives,
 * rather than printing asterisks and re-laying-out a moment later. Text inside
 * an open code fence is left exactly as it is: remark already renders an
 * unclosed fence as a code block, and its contents are not prose.
 *
 * A lone `*` used as multiplication is trimmed too, for as long as the stream
 * is open. It returns at the final render, which is not streaming.
 *
 * Counting is done over a copy with every CLOSED inline-code span masked out.
 * Without that, `run `echo "**"`` has one `**` — odd — and the trim reached
 * inside the backticks and deleted it, rewriting the operator's command while
 * it streamed. A glob (`**​/*.py`) or an exponent (`x**y`) does the same.
 */
export function trimDanglingMarks(text: string): string {
  if (occurrences(text, "```") % 2 === 1) return text;

  let out = text;
  for (const mark of STREAM_MARKS) {
    const masked = maskCodeSpans(out);
    if (occurrences(masked, mark) % 2 === 0) continue;
    // Index from the mask: same length, so it addresses the same character,
    // and it can never land inside a span the mask hid.
    const last = masked.lastIndexOf(mark);
    out = out.slice(0, last) + out.slice(last + mark.length);
  }
  return out;
}

function textFromNode(node: ReactNode): string {
  if (node === null || node === undefined || typeof node === "boolean")
    return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textFromNode).join("");
  if (isValidElement(node)) {
    const props = node.props as { children?: ReactNode };
    return textFromNode(props.children);
  }
  return Children.toArray(node).map(textFromNode).join("");
}

function languageFromCodeNode(node: ReactNode): string {
  const first = Array.isArray(node) ? node[0] : node;
  if (!isValidElement(first)) return "";
  const props = first.props as { className?: string };
  const match = props.className?.match(/language-([\w-]+)/);
  return match?.[1] ?? "";
}

function CodeExpandButton({
  code,
  language,
}: {
  code: string;
  language: string;
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        type="button"
        className="md-expand"
        onClick={() => setOpen(true)}
        aria-label="Expand code"
      >
        ⤢
      </button>
      <ExpandOverlay
        open={open}
        onClose={() => setOpen(false)}
        title={language ? `Code (${language})` : "Code"}
        actions={<CodeCopyButton code={code} />}
      >
        <pre>
          <code>{code}</code>
        </pre>
      </ExpandOverlay>
    </>
  );
}

function CodeCopyButton({ code }: { code: string }) {
  const [copied, setCopied] = useState(false);
  const timerRef = useRef<number | null>(null);

  useEffect(
    () => () => {
      if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    },
    [],
  );

  const onCopy = async () => {
    await copyToClipboard(code);
    setCopied(true);
    if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    timerRef.current = window.setTimeout(() => setCopied(false), 1200);
  };

  return (
    <button
      type="button"
      className="md-copy"
      onClick={onCopy}
      aria-label="Copy code"
    >
      {copied ? "Copied" : "Copy"}
    </button>
  );
}
