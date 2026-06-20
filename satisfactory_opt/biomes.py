"""Named biomes for the standard Satisfactory map.

The optimiser places factories at (x, y) game-world coordinates, but the game
data carries no biome polygons. We name each factory's region from the wiki's
authoritative biome map (https://satisfactory.wiki.gg, "Biome Map") rather than
hand-guessed boxes: that map shares this project's world extent and orientation
exactly (verified by overlay on map_bg.jpg — no flip), so a factory's fractional
map position maps straight onto it.

The biome regions are encoded as **seed points** (fx, fy) read off that map, and
a factory takes the biome of its **nearest seed** (a Voronoi lookup). Pixel
colour matching was tried first but the map's pastels are too close under JPEG
noise to separate reliably; nearest-seed on accurate points is unambiguous. Large
or concave biomes get several seeds so their region is covered. fx: 0=west..1=east,
fy: 0=north..1=south — the same fractions the factories are drawn with.
"""

# (name, fx, fy) — interior seed points, multiple per large/concave biome.
BIOME_SEEDS = [
    ("Forgotten Beach",     0.20, 0.22),
    ("Sea Islands",         0.40, 0.13),
    ("Spire Coast",         0.46, 0.19), ("Spire Coast", 0.36, 0.17),
    ("Spire Coast",         0.33, 0.22), ("Spire Coast", 0.52, 0.23),
    ("Big Crater",          0.87, 0.15), ("Big Crater", 0.93, 0.30),
    ("Dune Desert",         0.78, 0.22), ("Dune Desert", 0.85, 0.36),
    ("Dune Desert",         0.72, 0.30), ("Dune Desert", 0.80, 0.46),
    ("Coral Valley",        0.10, 0.31),
    ("Rockslide Desert",    0.25, 0.27),
    ("Rocky Desert",        0.12, 0.44), ("Rocky Desert", 0.20, 0.40),
    ("Rocky Desert",        0.10, 0.52),
    ("Western Beaches",     0.09, 0.60), ("Western Beaches", 0.20, 0.72),
    ("Green Valley",        0.38, 0.34),
    ("Savanna",             0.27, 0.37),
    ("Western Slopes",      0.47, 0.34),
    ("The Great Canyon",    0.46, 0.40),
    ("Crater Lakes",        0.31, 0.47),
    ("Northern Forest",     0.47, 0.41), ("Northern Forest", 0.41, 0.38),
    ("Lake Forest",         0.40, 0.45),
    ("Maze Canyons",        0.43, 0.43),
    ("Desert Canyons",      0.58, 0.43), ("Desert Canyons", 0.63, 0.39),
    ("Titan Forest",        0.53, 0.47), ("Titan Forest", 0.50, 0.53),
    ("Red Bamboo Fields",   0.36, 0.47),
    ("Red Jungle",          0.29, 0.50),
    ("Western Dune Forest", 0.32, 0.54),
    ("Snaketree Forest",    0.34, 0.58),
    ("Eastern Dune Forest", 0.45, 0.55), ("Eastern Dune Forest", 0.50, 0.59),
    ("Northern Swamplands", 0.63, 0.49), ("Northern Swamplands", 0.60, 0.54),
    ("Swamp",               0.72, 0.60), ("Swamp", 0.79, 0.55),
    ("Abyss Cliffs",        0.70, 0.67),
    ("Blue Crater",         0.60, 0.78),
    ("Southern Forest",     0.33, 0.74), ("Southern Forest", 0.43, 0.70),
    ("Grass Fields",        0.40, 0.86), ("Grass Fields", 0.50, 0.80),
    ("Grass Fields",        0.30, 0.82),
]


def biome_for(x: float, y: float, bounds: dict) -> str:
    """Name of the biome whose seed is nearest to (x, y) in fractional map space.
    `bounds` is the MAP_BOUNDS dict."""
    w, e = bounds["west"], bounds["east"]
    n, s = bounds["north"], bounds["south"]
    fx = (x - w) / (e - w) if e != w else 0.5
    fy = (y - n) / (s - n) if s != n else 0.5
    best, best_d = "", float("inf")
    for name, sx, sy in BIOME_SEEDS:
        d = (fx - sx) ** 2 + (fy - sy) ** 2
        if d < best_d:
            best_d, best = d, name
    return best
