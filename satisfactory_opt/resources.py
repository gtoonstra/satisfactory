"""Whole-map raw resource availability (items / minute).

Defaults are DERIVED from the real 577-node map dataset (see fetch_nodes.py),
not guessed: every node worked with a Mk.3 Miner @ 250% (solids 300/600/1200
by impure/normal/pure), Oil Extractors @ 250% (150/300/600) on crude-oil nodes,
and Resource Well Pressurizers @ 250% (75/150/300 per satellite). These figures
reproduce the community reference numbers exactly (Iron 92 100, Coal 42 300,
Nitrogen 12 000, Crude Oil 12 600, ...).

Water is treated as effectively unlimited: Water Extractors place on any water
surface, so availability is build-space limited, not node limited.

Override per run with --resources (a JSON of {item: rate_per_min}) or rescale
with --miner-clock. Regenerate from a newer map with derive_caps_from_nodes().
"""
from __future__ import annotations

# Extraction per node by purity, @ 250% overclock.
SOLID_RATE = {"impure": 300.0, "normal": 600.0, "pure": 1200.0}    # Miner Mk.3
OIL_RATE = {"impure": 150.0, "normal": 300.0, "pure": 600.0}       # Oil Extractor
WELL_RATE = {"impure": 75.0, "normal": 150.0, "pure": 300.0}       # Well satellite

# keyed by item className (derived from the real node map; water forced unlimited)
MAP_MAX_PER_MIN: dict[str, float] = {
    "Desc_OreIron_C": 92_100.0,
    "Desc_Stone_C": 69_300.0,        # Limestone
    "Desc_Coal_C": 42_300.0,
    "Desc_OreCopper_C": 36_900.0,
    "Desc_OreGold_C": 15_000.0,      # Caterium
    "Desc_RawQuartz_C": 13_500.0,
    "Desc_LiquidOil_C": 12_600.0,    # Crude Oil (nodes + oil wells)
    "Desc_OreBauxite_C": 12_300.0,
    "Desc_NitrogenGas_C": 12_000.0,  # wells
    "Desc_Sulfur_C": 10_800.0,
    "Desc_SAM_C": 10_200.0,
    "Desc_OreUranium_C": 2_100.0,
    "Desc_Water_C": 1_000_000_000.0,  # extractors are placement-limited only
}


def derive_caps_from_nodes(nodes: list[dict]) -> dict[str, float]:
    """Recompute caps from a nodes list ({resource,purity,kind}) — keeps limits
    accurate if the map dataset changes. Water stays unlimited."""
    caps: dict[str, float] = {}
    for n in nodes:
        r, p = n["resource"], n["purity"]
        if n.get("kind") == "well":
            rate = WELL_RATE[p]
        elif r == "Desc_LiquidOil_C":
            rate = OIL_RATE[p]
        else:
            rate = SOLID_RATE[p]
        caps[r] = caps.get(r, 0.0) + rate
    caps["Desc_Water_C"] = 1_000_000_000.0
    return caps


def map_limits(scale: float = 1.0) -> dict[str, float]:
    """Return resource limits, optionally scaled (e.g. 0.4 ~ Mk2 @ 100%)."""
    if scale == 1.0:
        return dict(MAP_MAX_PER_MIN)
    return {k: (v if v >= 1e9 else v * scale) for k, v in MAP_MAX_PER_MIN.items()}
