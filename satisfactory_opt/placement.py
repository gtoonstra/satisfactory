"""Factory placement / logistics layer.

Two independent pieces:

1. stage_recipes()  -- needs NO map coordinates.
   Splits the solved recipe set into a SOURCE stage (run at / next to the
   mine: ore -> ingot smelting and other raw-fed refining) and a HUB stage
   (central factory: assembly & manufacturing). It reports how much belt
   throughput crosses the mine->hub boundary BEFORE vs AFTER pushing the
   source stage out to the mines -- i.e. the transport-volume saving from
   "turn ore into ingots close to the source". This directly encodes the
   requested strategy: each resource type is reduced near its node, then a
   compact set of intermediate goods flows to one central location.

2. assign_and_place()  -- needs a node coordinate file (see nodes.py).
   Greedily selects physical nodes (best purity first) to meet the extraction
   the solver asked for, clusters them into mining sites, places a per-resource
   gathering point and a single throughput-weighted central hub, and scores
   transport effort = sum(rate * distance). Pure heuristic, no game data needed
   beyond the solution + nodes.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .data import GameData
from .resources import SOLID_RATE, OIL_RATE, WELL_RATE
from .solver import Solution


def node_rate(nd: dict) -> float:
    """Extraction items/min for one node, @250%, by kind + purity."""
    p = nd.get("purity", "normal")
    if nd.get("kind") == "well":
        return WELL_RATE.get(p, 150.0)
    if nd["resource"] == "Desc_LiquidOil_C":
        return OIL_RATE.get(p, 300.0)
    return SOLID_RATE.get(p, 600.0)

# Machines that REDUCE a raw resource to a refined feedstock. Running these at
# the mine keeps long-distance belts carrying ingots/refined goods, not ore.
SOURCE_MACHINES = {"Desc_SmelterMk1_C", "Desc_FoundryMk1_C"}


@dataclass
class StagePlan:
    source_recipes: dict[str, float]   # recipe key -> machines, built at mines
    hub_recipes: dict[str, float]      # recipe key -> machines, built at hub
    raw_belt_per_min: float            # ore that WOULD cross to hub if smelting central
    staged_belt_per_min: float         # goods crossing to hub when smelting at source
    boundary_flows: dict[str, float]   # item className -> items/min crossing to hub


def _net_per_item(gd: GameData, recipes: dict[str, float]) -> dict[str, float]:
    net: dict[str, float] = {}
    for key, cnt in recipes.items():
        r = gd.recipes[key]
        for it in r.items:
            net[it] = net.get(it, 0.0) + r.rate(it) * cnt
    return net


def stage_recipes(gd: GameData, sol: Solution) -> StagePlan:
    source, hub = {}, {}
    for key, cnt in sol.recipes.items():
        r = gd.recipes[key]
        feeds_on_raw = any(i in gd.raw_resources for i in r.inputs)
        if r.machine in SOURCE_MACHINES and feeds_on_raw:
            source[key] = cnt
        else:
            hub[key] = cnt

    # What the source stage hands off to the hub = its positive net products
    # (minus anything the source stage itself re-consumes).
    source_net = _net_per_item(gd, source)
    boundary = {it: amt for it, amt in source_net.items()
                if amt > 1e-7 and it not in gd.raw_resources}

    # Belt load if we instead shipped raw ore to a central smelter: the raw
    # resource the source stage consumes (negative net on raw items).
    raw_belt = sum(-amt for it, amt in source_net.items()
                   if it in gd.raw_resources and amt < 0)
    staged_belt = sum(boundary.values())

    return StagePlan(
        source_recipes=source, hub_recipes=hub,
        raw_belt_per_min=raw_belt, staged_belt_per_min=staged_belt,
        boundary_flows=boundary,
    )


# --------------------------------------------------------------------------- #
# Geographic placement (optional, needs node coordinates)
# --------------------------------------------------------------------------- #
@dataclass
class MiningSite:
    resource: str
    nodes: list[dict]                  # selected node records
    rate: float                        # items/min from this site
    centroid: tuple[float, float, float]


@dataclass
class PlacementPlan:
    sites: list[MiningSite]
    hub: tuple[float, float, float]
    transport_effort: float            # sum(rate * distance) in item*units/min
    unmet: dict[str, float]            # resource -> shortfall if nodes insufficient


def _dist(a, b):
    return ((a[0]-b[0])**2 + (a[1]-b[1])**2 + (a[2]-b[2])**2) ** 0.5


def _cluster(points: list[tuple], radius: float) -> list[list[int]]:
    """Trivial single-link clustering by distance threshold (no deps)."""
    n = len(points)
    parent = list(range(n))
    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]; i = parent[i]
        return i
    for i in range(n):
        for j in range(i+1, n):
            if _dist(points[i], points[j]) <= radius:
                parent[find(i)] = find(j)
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def assign_and_place(
    gd: GameData, sol: Solution, nodes: list[dict],
    *, cluster_radius: float = 30_000.0, fluids_unlimited: bool = True,
) -> PlacementPlan:
    """nodes: list of {resource, purity, x, y, z} (resource = item className)."""
    by_res: dict[str, list[dict]] = {}
    for nd in nodes:
        by_res.setdefault(nd["resource"], []).append(nd)

    sites: list[MiningSite] = []
    unmet: dict[str, float] = {}
    for res, need in sol.extraction.items():
        if res == "Desc_Water_C" or need >= 1e8:
            continue
        # best purity first
        avail = sorted(by_res.get(res, []), key=lambda n: -node_rate(n))
        chosen, got = [], 0.0
        for nd in avail:
            if got >= need:
                break
            chosen.append(nd)
            got += node_rate(nd)
        if got < need - 1e-6:
            unmet[res] = need - got
        if not chosen:
            continue
        pts = [(n["x"], n["y"], n["z"]) for n in chosen]
        # split a resource's chosen nodes into geographic clusters -> one site each
        for grp in _cluster(pts, cluster_radius):
            gpts = [pts[i] for i in grp]
            gc = tuple(sum(p[k] for p in gpts) / len(gpts) for k in range(3))
            site_rate = sum(node_rate(chosen[i]) for i in grp)
            sites.append(MiningSite(res, [chosen[i] for i in grp], site_rate, gc))

    # Hub at throughput-weighted centroid of all sites (resources converge here).
    if sites:
        wsum = sum(s.rate for s in sites) or 1.0
        hub = tuple(sum(s.centroid[k] * s.rate for s in sites) / wsum for k in range(3))
    else:
        hub = (0.0, 0.0, 0.0)
    effort = sum(s.rate * _dist(s.centroid, hub) for s in sites)
    return PlacementPlan(sites=sites, hub=hub, transport_effort=effort, unmet=unmet)
