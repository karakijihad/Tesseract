// Y-2 — fallback renderer + the `json` type. Renders the descriptor's props
// (or the whole descriptor) as pretty JSON, with an "unknown type" badge
// when the type has no registered renderer. Never crashes the canvas on a
// malformed descriptor.

import type { RendererProps } from './index';
import { RENDERERS } from './index';

export function JsonDumpRenderer({ descriptor }: RendererProps) {
  const known = descriptor.type in RENDERERS;
  const body =
    descriptor.props && Object.keys(descriptor.props).length > 0
      ? descriptor.props
      : descriptor;
  return (
    <div className="surface-json">
      {!known ? (
        <div className="surface-json__badge">unknown type: {descriptor.type}</div>
      ) : null}
      <pre className="surface-json__body">{safeStringify(body)}</pre>
    </div>
  );
}

function safeStringify(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}
