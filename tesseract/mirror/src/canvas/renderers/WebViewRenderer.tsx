// Y-2 — webview surface. Embeds `props.url` in a sandboxed iframe. In the
// Tauri shell this becomes a native webview later; the browser/dev build
// uses an iframe. Cross-origin pages that set `X-Frame-Options` /
// restrictive CSP will refuse to frame — by design the operator opens those
// in `mode: external` instead (phase open-question §5, accepted).

import { useEffect, useRef } from 'react';

import { dialectFor, prepareFrameUrl } from '../dialects';
import { useFrameCommands } from '../useFrameCommands';
import type { RendererProps } from './index';

// Chat models routinely supply the shareable watch/short-link YouTube form
// instead of the frameable /embed/ endpoint, which strict-sandboxes into a
// blank pane. Rewrite the two known shapes to /embed/ so they qualify for the
// media allowlist below. The strict 11-char ID check means the rewritten URL
// is exactly the dedicated embed endpoint — no path or query smuggling.
function normalizeYouTubeUrl(u: URL): URL {
  let id = '';
  if (u.origin === 'https://www.youtube.com' && u.pathname === '/watch') {
    id = u.searchParams.get('v') ?? '';
  } else if (u.origin === 'https://youtu.be') {
    id = u.pathname.slice(1);
  }
  return /^[A-Za-z0-9_-]{11}$/.test(id) ? new URL(`https://www.youtube.com/embed/${id}`) : u;
}

// Only http/https are framed. `props.url` may be tool-supplied from
// untrusted content, so a scheme allowlist blocks `javascript:`/`data:` srcs.
function safeFrameUrl(raw: string): string {
  try {
    const u = normalizeYouTubeUrl(new URL(raw));
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return '';
    // Whatever the page's dialect needs before it will listen at all, added
    // AFTER the scheme check and the embed rewrite, so it only ever decorates
    // a URL those two already accepted.
    return prepareFrameUrl(u.toString());
  } catch {
    return '';
  }
}

// `allow-same-origin` on a scripted iframe is a sandbox-escape hazard IF the
// framed document can ever share the Mirror's own origin — a same-origin doc
// with `allow-scripts` can reach up and script the parent. A naive "grant it to
// any cross-origin URL" check is NOT safe: sandbox flags survive navigation, so
// a URL that is cross-origin at load can 302 to the Mirror's origin (an
// attacker's server, or an open redirect on a trusted host) and land a
// same-origin document that still carries the elevated sandbox.
//
// So we grant the elevated sandbox ONLY to a hardcoded allowlist of dedicated
// media-EMBED endpoints (exact origin + path prefix). These render a player and
// do not open-redirect the top frame to arbitrary origins. Everything else —
// arbitrary tool-supplied URLs, generic pages, same-origin URLs — gets the
// strict sandbox and can never escape regardless of where it navigates.
// Embedded players (YouTube et al.) need `allow-same-origin` to read
// storage/`caches` at boot, else they throw and render a black pane.
const MEDIA_EMBEDS: ReadonlyArray<{ origin: string; path: string }> = [
  { origin: 'https://www.youtube.com', path: '/embed/' },
  { origin: 'https://www.youtube-nocookie.com', path: '/embed/' },
  { origin: 'https://player.vimeo.com', path: '/video/' },
];

function isTrustedMediaEmbed(url: string): boolean {
  try {
    const u = new URL(url);
    return MEDIA_EMBEDS.some((e) => u.origin === e.origin && u.pathname.startsWith(e.path));
  } catch {
    return false;
  }
}

// Media-capable sandbox + feature policy for allowlisted embeds. `allow=`
// unlocks autoplay, DRM playback (`encrypted-media`), fullscreen and PiP.
const MEDIA_SANDBOX =
  'allow-scripts allow-forms allow-popups allow-same-origin allow-presentation';
const STRICT_SANDBOX = 'allow-scripts allow-forms allow-popups';
const MEDIA_ALLOW = 'autoplay; encrypted-media; fullscreen; picture-in-picture; clipboard-write';

/** The verbs a page with a player API answers to. */
const PLAYER_CONTROLS = ['play', 'pause', 'mute', 'unmute', 'volume', 'seek', 'read'];

export function WebViewRenderer({ descriptor, report }: RendererProps) {
  const url = safeFrameUrl(String(descriptor.props?.url ?? ''));
  const frame = useRef<HTMLIFrameElement>(null);
  const dialect = dialectFor(url);

  // This is the card kind where "what can it be asked" is a property of the
  // PAGE and not of the type: two webview cards differ by what they framed.
  // So the card says which, and `surface_list` reads it back, rather than the
  // model carrying a map of which sites publish an API.
  useEffect(() => {
    if (!report) return;
    if (!url) report('errored', 'no url: props.url is missing or not framable');
    else if (dialect) report('mounted', `${dialect.name} player`, PLAYER_CONTROLS);
    else report('mounted', 'this page publishes no way in', []);
  }, [report, url, dialect]);

  // The card obeys `surface_control` when the page publishes a way in. Note
  // this is NOT gated on the media sandbox: `postMessage` reaches a frame
  // whatever its sandbox says, and `allow-same-origin` is about whether a
  // player can boot, not about whether it can be spoken to.
  useFrameCommands(descriptor.id, descriptor.view, url, frame);
  if (!url) {
    return <div className="surface-webview surface-webview--empty t-meta">no url</div>;
  }
  const media = isTrustedMediaEmbed(url);
  return (
    <div className="surface-webview">
      <iframe
        ref={frame}
        className="surface-webview__frame"
        src={url}
        title={descriptor.title ?? url}
        sandbox={media ? MEDIA_SANDBOX : STRICT_SANDBOX}
        allow={media ? MEDIA_ALLOW : undefined}
        // `no-referrer` makes YouTube (and other referer-gated embeds) reject
        // playback with "Error 153". `strict-origin-when-cross-origin` sends
        // only the bare origin cross-site — enough to satisfy embed checks
        // without leaking the Mirror's full URL.
        referrerPolicy="strict-origin-when-cross-origin"
        allowFullScreen={media}
      />
    </div>
  );
}
