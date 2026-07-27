# Opening a URL in the canvas

When the operator wants a website/media shown in the Mirror, pick the path by what the URL can do — don't guess-and-flail:

1. **Embeddable media → `webview` surface.** YouTube, Vimeo and other purpose-built embeds frame fine (`surface_create type:webview props:{url}`). Then VERIFY it rendered (see rule 06). Paste-a-link cases get converted to an `/embed/` URL automatically.

2. **A site that refuses to embed → don't retry a blank webview.** LinkedIn, Google, X/Twitter, Facebook, banks and most logged-in apps send `X-Frame-Options` / CSP `frame-ancestors` — the browser refuses to frame them and NO sandbox flag fixes it. A blank/again-blank webview is the signal. When you hit this, tell the operator it can't be embedded and offer the two real ways to open it, then act on their choice:
   - **Open the real browser** — `surface_create type:external-link props:{url}`. Best-effort auto-opens a browser tab and drops a one-click "Open ↗" card on the canvas. Use this for "open X for me".
   - **Show it inside the canvas** — `browser_navigate url:<url>`. Renders the live page as an image card _in_ the Mirror (works for any site, since a headless browser isn't framed). Use this for "show me the page / the profile". `browser_screenshot` refreshes it.

3. **Direct media/files** (`.mp4`, `.mp3`, images, `.pdf`) and **your own HTML** render directly (`image`/`html` surfaces, or a webview for a raw media URL).

The failure mode to avoid: calling `surface_create` and _assuming_ it worked. `ok` is not proof — a webview can be blank, and `mode:external` does nothing in the web build. Verify, or use the paths above that actually surface something.

**One surface per task — don't pile up cards.** A single "show me X" request should leave the operator with ONE card, not the wreckage of every approach you tried:

- **Browsing a flow?** `browser_navigate` reuses the session's current card by default — just call it again with the next URL. Only pass `new_card:true` when you genuinely want two pages side by side.
- **Falling back after a failed approach?** When you replace a dead surface (a webview that came back blank, an embed that errored) with a different one, pass `surface_create replaces:<dead_surface_id>` — it closes the old card once the new one exists. Don't leave the broken embed on screen next to its replacement.
