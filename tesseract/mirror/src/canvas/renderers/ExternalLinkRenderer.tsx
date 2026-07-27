// external-link surface — for sites that can't be embedded in a webview
// (X-Frame-Options / CSP frame-ancestors: LinkedIn, Google, X, most logged-in
// apps). Best-effort auto-opens the URL in a real browser tab on mount; when
// the browser blocks that (popup blocker — window.open outside a user gesture
// is blocked in the web build), the operator clicks the always-present
// "Open ↗" button, which is a genuine user gesture and never blocked.

import { useEffect, useRef } from 'react';

import type { RendererProps } from './index';

// `props.url` may be tool-supplied; only frame-safe http/https escape the
// scheme allowlist (blocks javascript:/data:).
function safeUrl(raw: string): string {
  try {
    const u = new URL(raw);
    return u.protocol === 'http:' || u.protocol === 'https:' ? u.toString() : '';
  } catch {
    return '';
  }
}

export function ExternalLinkRenderer({ descriptor }: RendererProps) {
  const url = safeUrl(String(descriptor.props?.url ?? ''));
  const tried = useRef(false);

  useEffect(() => {
    if (!url || tried.current) return;
    tried.current = true; // attempt at most once per mount
    try {
      // Returns null when the popup blocker stops it — the button below is
      // then the operator's one-click path. No error to surface either way.
      window.open(url, '_blank', 'noopener,noreferrer');
    } catch {
      /* blocked / unavailable — fall through to the button */
    }
  }, [url]);

  if (!url) {
    return <div className="surface-extlink surface-extlink--empty t-meta">no url</div>;
  }

  let host = url;
  try {
    host = new URL(url).host;
  } catch {
    /* keep full url */
  }

  return (
    <div className="surface-extlink">
      <a
        className="surface-extlink__btn"
        href={url}
        target="_blank"
        rel="noopener noreferrer"
      >
        Open in browser ↗
      </a>
      <div className="surface-extlink__url t-meta">{host}</div>
    </div>
  );
}
