"""Approximate named biomes for the standard Satisfactory map.

The optimiser places factories at (x, y) game-world coordinates, but the game
data carries no biome polygons. So we name each factory's region by the biome
**bounding box** it falls inside. Boxes are stored as *fractional* map positions
(fx: 0=west .. 1=east, fy: 0=north .. 1=south), as (name, fx0, fy0, fx1, fy1)
where (fx0,fy0) is the north-west corner and (fx1,fy1) the south-east corner —
so they stay correct even if MAP_BOUNDS shifts.

Containment (a point is *inside* a box) beats nearest-anchor: a single anchor
point misfiles anything near a large biome's edge onto a smaller neighbour, so
factories ended up "on the wrong side" of the anchor. With explicit boxes the
boundaries are exactly where we draw them. Boxes are deliberately approximate —
adjust the table to taste.
"""

# (name, fx0, fy0, fx1, fy1) — NW corner .. SE corner, fractional map coords.
BIOME_BOXES = [
    ("Dune Desert",         0.00, 0.00, 0.42, 0.30),
    ("Northern Forest",     0.42, 0.00, 0.70, 0.27),
    ("Rocky Desert",        0.66, 0.18, 1.00, 0.45),
    ("Blue Crater",         0.00, 0.30, 0.26, 0.50),
    ("Grass Fields",        0.30, 0.27, 0.62, 0.55),
    ("Spire Coast",         0.84, 0.30, 1.00, 0.62),
    ("Eastern Dune Forest", 0.00, 0.47, 0.30, 0.66),
    ("Crater Lakes",        0.26, 0.47, 0.48, 0.66),
    ("Lake Forest",         0.60, 0.45, 0.84, 0.66),
    ("Titan Forest",        0.40, 0.63, 0.66, 0.80),
    ("Swamp",               0.66, 0.63, 0.86, 0.82),
    ("Maelstrom Coast",     0.86, 0.62, 1.00, 0.85),
    ("Red Jungle",          0.50, 0.78, 0.78, 1.00),
    ("Abyss Cliffs",        0.20, 0.80, 0.52, 1.00),
]


def _frac(x: float, y: float, bounds: dict) -> tuple[float, float]:
    w, e = bounds["west"], bounds["east"]
    n, s = bounds["north"], bounds["south"]
    fx = (x - w) / (e - w) if e != w else 0.5
    fy = (y - n) / (s - n) if s != n else 0.5
    return fx, fy


def biome_for(x: float, y: float, bounds: dict) -> str:
    """Name of the biome whose box contains (x, y). If several boxes overlap the
    point, the smallest (most specific) wins; if none contain it (a gap between
    boxes), fall back to the box with the nearest edge. `bounds` is MAP_BOUNDS."""
    fx, fy = _frac(x, y, bounds)
    inside = [(name, (x1 - x0) * (y1 - y0))
              for (name, x0, y0, x1, y1) in BIOME_BOXES
              if x0 <= fx <= x1 and y0 <= fy <= y1]
    if inside:
        return min(inside, key=lambda t: t[1])[0]
    # gap: nearest box by clamped point-to-rectangle distance
    best, best_d = "", float("inf")
    for (name, x0, y0, x1, y1) in BIOME_BOXES:
        dx = max(x0 - fx, 0.0, fx - x1)
        dy = max(y0 - fy, 0.0, fy - y1)
        d = dx * dx + dy * dy
        if d < best_d:
            best_d, best = d, name
    return best
