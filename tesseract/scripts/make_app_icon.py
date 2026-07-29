"""TESSERACT app icon — hypercube projection, tuned to stay legible at 32px.

Regenerate the shipped icon set with:
    python -m tesseract.scripts.make_app_icon
    cd tesseract/mirror && pnpm tauri icon src-tauri/icons/source.png
"""
import pathlib

from PIL import Image, ImageDraw

S, SS = 1024, 4
W = S * SS
BG = (13, 12, 22, 255)
ACCENT = (141, 128, 255)
INK = (248, 248, 255)

img = Image.new("RGBA", (W, W), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

pad = int(W * 0.030)
d.rounded_rectangle([pad, pad, W - pad, W - pad], radius=int(W * 0.215), fill=BG)

cx = cy = W / 2
outer, inner = W * 0.355, W * 0.170

def sq(h):
    return [(cx - h, cy - h), (cx + h, cy - h), (cx + h, cy + h), (cx - h, cy + h)]

O, I = sq(outer), sq(inner)
wide = max(3, int(W * 0.030))   # outer: carries the silhouette
mid  = max(3, int(W * 0.026))   # inner
thin = max(3, int(W * 0.020))   # connectors

for a, b in zip(O, I):
    d.line([a, b], fill=ACCENT + (255,), width=thin, joint="curve")
d.line(I + [I[0]], fill=ACCENT + (255,), width=mid, joint="curve")
d.line(O + [O[0]], fill=INK + (255,), width=wide, joint="curve")

img = img.resize((S, S), Image.LANCZOS)
out = pathlib.Path(__file__).resolve().parents[1] / "mirror" / "src-tauri" / "icons" / "source.png"
img.save(out)
print("wrote", out)
