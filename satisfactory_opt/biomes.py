"""Named biomes for the standard Satisfactory map.

The optimiser places factories at (x, y) game-world coordinates, but the game
data carries no biome polygons. We name each factory's region from a
hand-painted biome map (``assets/hue_map.png``), where every biome is a single
flat colour and ``assets/color_map.txt`` maps each ``rrggbb`` hex to its name.
The map shares this project's world extent and orientation exactly, so a
factory's fractional map position maps straight onto it.

Naming is a **pixel-indexed lookup**, built once offline and baked into
``biome_raster.py`` (see ``bake_biomes.py``): every pixel is snapped to its
nearest biome colour, and off-map (void/ocean) cells are filled with their
nearest land biome so a point in the sea resolves along the coastline to the
nearest biome. ``biome_for`` just reads the cell under (x, y) and returns
``NAMES[index]``.

All positions are fractions of the map: fx 0=west..1=east, fy 0=north..1=south.
After editing ``hue_map.png`` or ``color_map.txt``, re-run
``python bake_biomes.py`` to rebuild the raster.
"""

_RASTER = None  # lazily-loaded (grid_bytes, W, H, names) or False if unavailable


def _frac(x: float, y: float, bounds: dict) -> tuple[float, float]:
    w, e = bounds["west"], bounds["east"]
    n, s = bounds["north"], bounds["south"]
    fx = (x - w) / (e - w) if e != w else 0.5
    fy = (y - n) / (s - n) if s != n else 0.5
    return fx, fy


def _load_raster():
    global _RASTER
    if _RASTER is None:
        try:
            from . import biome_raster as R
            _RASTER = (R.grid(), R.W, R.H, R.NAMES)
        except Exception:
            _RASTER = False
    return _RASTER


def biome_for(x: float, y: float, bounds: dict) -> str:
    """Name of the region containing (x, y).

    Reads the baked biome raster (the hand-painted colour map indexed per
    cell). Returns "" if the raster is unavailable. `bounds` is the MAP_BOUNDS
    dict."""
    fx, fy = _frac(x, y, bounds)

    rast = _load_raster()
    if not rast:
        return ""
    grid, w, h, names = rast
    col = min(w - 1, max(0, int(fx * w)))
    row = min(h - 1, max(0, int(fy * h)))
    return names[grid[row * w + col]]
