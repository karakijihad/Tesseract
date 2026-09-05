"""One row's declared graph, drawn.

Pixels only. Every fact on the picture arrives in the `RowDiagram` —
`pipeline/diagram.py` owns what is true, this owns where it sits — so a stage
that moves changes the drawing without anyone opening this file.

**Why generated rather than drawn.** `Guide/diagrams/*.svg` are hand-drawn and
their claims are pinned by a sidecar, which is the right answer for a picture of
something with no machine-readable declaration: an architecture level, a
conceptual flow. The scheduler's graph is not that. It IS a declaration, so
pinning a hand-drawn copy of it would be a second copy to keep in step — the
defect the sidecars exist to survive rather than one to repeat. The rule, for
whoever adds the next diagram: **generate what is declared, draw and pin what is
not.**

**A stage with no edges is drawn as having none.** Twelve of the nightly row's
twenty-three sit at rank 0, and seven of those touch nothing at all — laying
them out as a band of chips implied an order between them that the runner does
not have, and cost the picture the thing it exists to show. They are collected
instead, under a line saying exactly what they are.

Colour comes from `currentColor` alone. The page renders in light and dark, the
app's brand file owns every colour it would use, and a drawing that hard-codes
one is a drawing that works in one of them.
"""

from __future__ import annotations

from tesseract.scheduler.pipeline.diagram import DiagramNode, EdgeKind, RowDiagram

WIDTH = 940
PAD_X = 18
CHIP_W = 166
CHIP_H = 26
CHIP_GAP = 14
RANK_GAP = 30
LOOSE_H = 20
LOOSE_GAP = 6
LEGEND_H = 44

_MONO = "'IBM Plex Mono', ui-monospace, monospace"


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _split(chart: RowDiagram) -> tuple[list[DiagramNode], list[DiagramNode]]:
    """Wired nodes, and the ones no declared edge touches."""
    touched = {e.source for e in chart.edges if e.source_row == chart.name}
    touched |= {e.target for e in chart.edges}
    wired = [n for n in chart.nodes if n.name in touched]
    loose = [n for n in chart.nodes if n.name not in touched]
    return wired, loose


def _row(names: int, index: int, total: int) -> float:
    span = total * CHIP_W + (total - 1) * CHIP_GAP
    return (WIDTH - span) / 2 + index * (CHIP_W + CHIP_GAP) + CHIP_W / 2


def _chip(node: DiagramNode, cx: float, cy: float) -> list[str]:
    x, y = cx - CHIP_W / 2, cy - CHIP_H / 2
    model = node.kind == "model"
    out = [
        f'<rect x="{x:.1f}" y="{y:.1f}" width="{CHIP_W}" height="{CHIP_H}" rx="2" '
        f'fill="currentColor" fill-opacity="{0.07 if model else 0.0}" '
        f'stroke="currentColor" stroke-opacity="{0.75 if model else 0.4}"/>',
        f'<text x="{cx:.1f}" y="{cy + 4:.1f}" text-anchor="middle" '
        f'font-family="{_MONO}" font-size="11" fill="currentColor">'
        f"{_esc(node.name)}</text>",
    ]
    if model:
        out.append(
            f'<circle cx="{x + 8:.1f}" cy="{y + 7:.1f}" r="2.6" fill="currentColor"/>'
        )
    return out


def _edge(x1: float, y1: float, x2: float, y2: float, kind: EdgeKind,
          artifact: str | None) -> list[str]:
    dash = ' stroke-dasharray="4 4"' if kind is EdgeKind.AFTER else ""
    opacity = 0.3 if kind is EdgeKind.AFTER else 0.75
    mid = (y1 + y2) / 2
    path = f"M{x1:.1f} {y1:.1f} C{x1:.1f} {mid:.1f} {x2:.1f} {mid:.1f} {x2:.1f} {y2:.1f}"
    out = [
        f'<path d="{path}" fill="none" stroke="currentColor" '
        f'stroke-opacity="{opacity}"{dash} marker-end="url(#pipe-arrow)"/>'
    ]
    if artifact:
        # Beside the line, not on it: centred labels landed on the chips they
        # pointed at, twice over where two edges carry the same artifact.
        lx = (x1 + x2) / 2 + 7
        out.append(
            f'<text x="{lx:.1f}" y="{mid + 3:.1f}" text-anchor="start" '
            f'font-family="{_MONO}" font-size="9" fill="currentColor" '
            f'fill-opacity="0.8">{_esc(artifact)}</text>'
        )
    return out


def render(chart: RowDiagram) -> str:
    """Inline SVG for one row. Self-contained, themed by `currentColor`."""
    wired, loose = _split(chart)
    by_rank: dict[int, list[DiagramNode]] = {}
    for node in wired:
        by_rank.setdefault(node.rank, []).append(node)

    parts: list[str] = [
        '<defs><marker id="pipe-arrow" viewBox="0 0 10 8" refX="9" refY="4" '
        'markerWidth="7" markerHeight="6" orient="auto">'
        '<polygon points="0,0 10,4 0,8" fill="currentColor"/></marker></defs>',
    ]

    y = 14.0
    # Cross-row sources have no chip here — a stub names where the arrow is from.
    stub_y = y
    stubs = sorted({
        f"{e.source_row}.{e.source}"
        for e in chart.edges if e.kind is EdgeKind.IMPORTS
    })
    if stubs:
        parts.append(
            f'<text x="{WIDTH / 2:.1f}" y="{stub_y:.1f}" text-anchor="middle" '
            f'font-family="{_MONO}" font-size="9.5" fill="currentColor" '
            f'fill-opacity="0.65">from the other row: {_esc(", ".join(stubs))}</text>'
        )
        y += 18

    ranks = {n.name: n.rank for n in wired}
    centres: dict[str, tuple[float, float]] = {}
    y += CHIP_H / 2
    for rank in sorted(by_rank):
        nodes = sorted(by_rank[rank], key=lambda n: n.lane)
        for index, node in enumerate(nodes):
            centres[node.name] = (_row(len(nodes), index, len(nodes)), y)
        y += CHIP_H + RANK_GAP
    bands_bottom = y - RANK_GAP - CHIP_H / 2 + CHIP_H / 2

    for edge in chart.edges:
        if edge.target not in centres:
            continue
        tx, ty = centres[edge.target]
        if edge.kind is EdgeKind.IMPORTS:
            parts += _edge(tx, stub_y + 6, tx, ty - CHIP_H / 2, edge.kind, edge.artifact)
            continue
        if edge.source not in centres:
            continue
        sx, sy = centres[edge.source]
        # A label only sits safely on an edge between ADJACENT ranks. A longer
        # one passes behind the chips in between and its midpoint lands on one
        # of them — `atlas` printed straight through `atlas_verify`. The
        # artifact is already named on the short edge leaving the same stage.
        span = ranks[edge.target] - ranks[edge.source]
        label = edge.artifact if span == 1 else None
        parts += _edge(sx, sy + CHIP_H / 2, tx, ty - CHIP_H / 2, edge.kind, label)

    for node in wired:
        parts += _chip(node, *centres[node.name])

    if loose:
        top = bands_bottom + 16
        per_line = 4
        lines = -(-len(loose) // per_line)
        box_h = 22 + lines * (LOOSE_H + LOOSE_GAP)
        parts.append(
            f'<rect x="{PAD_X}" y="{top:.1f}" width="{WIDTH - 2 * PAD_X}" '
            f'height="{box_h:.1f}" rx="3" fill="none" stroke="currentColor" '
            'stroke-opacity="0.25" stroke-dasharray="3 4"/>'
        )
        parts.append(
            f'<text x="{PAD_X + 12}" y="{top + 15:.1f}" font-family="{_MONO}" '
            'font-size="9.5" letter-spacing="0.8" fill="currentColor" '
            f'fill-opacity="0.65">{len(loose)} MORE, WITH NO DECLARED EDGE AT ALL — '
            'NOTHING WAITS ON THEM AND THEY WAIT ON NOTHING</text>'
        )
        cell_w = (WIDTH - 2 * PAD_X - 24) / per_line
        for index, node in enumerate(sorted(loose, key=lambda n: n.name)):
            line, column = divmod(index, per_line)
            lx = PAD_X + 12 + column * cell_w
            ly = top + 22 + line * (LOOSE_H + LOOSE_GAP) + 13
            dot = (
                f'<circle cx="{lx + 3:.1f}" cy="{ly - 4:.1f}" r="2.4" '
                'fill="currentColor"/>' if node.kind == "model" else ""
            )
            parts.append(dot)
            parts.append(
                f'<text x="{lx + (10 if node.kind == "model" else 0):.1f}" '
                f'y="{ly:.1f}" font-family="{_MONO}" font-size="10.5" '
                f'fill="currentColor" fill-opacity="0.85">{_esc(node.name)}</text>'
            )
        bands_bottom = top + box_h

    ly = bands_bottom + 26
    parts += [
        f'<line x1="{PAD_X}" y1="{bands_bottom + 10:.1f}" x2="{WIDTH - PAD_X}" '
        f'y2="{bands_bottom + 10:.1f}" stroke="currentColor" stroke-opacity="0.18"/>',
        f'<line x1="{PAD_X}" y1="{ly:.1f}" x2="{PAD_X + 26}" y2="{ly:.1f}" '
        'stroke="currentColor" stroke-opacity="0.75" marker-end="url(#pipe-arrow)"/>',
        f'<text x="{PAD_X + 34}" y="{ly + 4:.1f}" font-family="{_MONO}" '
        'font-size="10" fill="currentColor" fill-opacity="0.75">'
        "consumes its output — skipped if it did not succeed</text>",
        f'<line x1="{PAD_X + 336}" y1="{ly:.1f}" x2="{PAD_X + 362}" y2="{ly:.1f}" '
        'stroke="currentColor" stroke-opacity="0.3" stroke-dasharray="4 4" '
        'marker-end="url(#pipe-arrow)"/>',
        f'<text x="{PAD_X + 370}" y="{ly + 4:.1f}" font-family="{_MONO}" '
        'font-size="10" fill="currentColor" fill-opacity="0.75">'
        "runs after it — still runs if it failed</text>",
        f'<circle cx="{PAD_X + 640:.1f}" cy="{ly - 3:.1f}" r="2.6" fill="currentColor"/>',
        f'<text x="{PAD_X + 650}" y="{ly + 4:.1f}" font-family="{_MONO}" '
        'font-size="10" fill="currentColor" fill-opacity="0.75">calls a model</text>',
    ]

    height = ly + LEGEND_H - 26
    depth = max((n.rank for n in wired), default=0) + 1
    alt = (
        f"{chart.name}: {len(chart.nodes)} stages. {len(wired)} of them form a "
        f"chain {depth} deep, drawn with every declared edge; {len(loose)} have "
        "no edge at all."
    )
    return (
        f'<svg viewBox="0 0 {WIDTH} {height:.0f}" role="img" '
        f'aria-label="{_esc(alt)}" style="width:100%;height:auto;max-width:100%">'
        + "".join(parts)
        + "</svg>"
    )


__all__ = ["render"]
