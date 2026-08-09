// Y-1 — the assistant orb's anchor point inside a canvas.
//
// The orb itself is the singleton WebGL particle field rendered by
// `components/layout/GlobalCanvas.tsx` at App root (full-screen on
// OrbView, corner-docked elsewhere). OrbAnchor does NOT spawn a second
// orb — it is a zero-size, non-interactive marker that names where the
// orb belongs within the canvas so Y-2 can bind orb-relative surfaces to
// it. For Y-1 it only needs to exist and not block canvas interaction.

export function OrbAnchor() {
  return <div className="orb-anchor" aria-hidden="true" />;
}
