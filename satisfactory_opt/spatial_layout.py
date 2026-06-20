"""Spatial production LP: decide WHERE each recipe runs, hub-free.

Stage 2 on top of `solver.solve`. It keeps the solved recipe *totals* (the WHAT)
and decides the *spatial allocation* (the WHERE): how many machines of each
recipe run in each map region, and what material ships between regions — so
that total transport plus a cluster-size penalty is minimized.

Cost model
----------
transport  =  Σ  f[item, g→h] · weight[item] · dist(g, h)
    - f is items/min shipped between regions over a sparse (train-like) network.
    - `weight[item]` = belts/pipes the flow needs (rate already encodes "volume";
      weight adds the physical solid-vs-fluid difference — fluids need pipes at
      half a belt's throughput, so piping them far is costlier).
    - Bulk (high items/min: ore, ingots) is thus pinned near its source; only
      compact high-tier parts can afford to travel.

congestion =  λ · Σ_region  convex(machines_in_region)
    - a free local capacity, then an increasing marginal cost per extra machine.
    - This *confines clusters*: past a point it is cheaper to spread to a
      neighbouring basin than to keep packing one region — the bigger a cluster
      grows, the more each added machine costs. No single region can swallow the
      whole factory.

There is **no hub term**: final outputs have no downstream demand, so nothing is
forced to converge. Sub-factories settle next to what they exchange material
with; the surviving long links are your train lines.

Single LP (GLOP), one global optimum, deterministic → stable. Solve the full
end-state once and build incrementally toward it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ortools.linear_solver import pywraplp

from .data import GameData
from .placement import MiningSite, assign_and_place, _cluster, _dist
from .solver import Solution


# --------------------------------------------------------------------------- #
# Regions
# --------------------------------------------------------------------------- #
@dataclass
class Region:
    id: int
    pos: tuple[float, float]
    supply: dict[str, float]           # raw resource -> items/min available here
    mine_sites: list[MiningSite] = field(default_factory=list)


def _build_regions(mine_sites: list[MiningSite], radius: float) -> list[Region]:
    """Cluster mining sites into compact build regions (basins)."""
    pts = [(s.centroid[0], s.centroid[1], 0.0) for s in mine_sites]
    regions: list[Region] = []
    for rid, grp in enumerate(_cluster(pts, radius)):
        members = [mine_sites[i] for i in grp]
        rate = sum(s.rate for s in members) or 1.0
        cx = sum(s.centroid[0] * s.rate for s in members) / rate
        cy = sum(s.centroid[1] * s.rate for s in members) / rate
        supply: dict[str, float] = {}
        for s in members:
            supply[s.resource] = supply.get(s.resource, 0.0) + s.rate
        regions.append(Region(rid, (cx, cy), supply, members))
    return regions


def _ship_network(regions: list[Region], k: int) -> list[tuple[int, int]]:
    """Sparse, symmetric, connected region graph (each region to k nearest)."""
    n = len(regions)
    pos = [(r.pos[0], r.pos[1], 0.0) for r in regions]
    adj: set[tuple[int, int]] = set()
    for i in range(n):
        order = sorted(range(n), key=lambda j: _dist(pos[i], pos[j]))
        for j in order[1:k + 1]:
            adj.add((min(i, j), max(i, j)))
    # ensure connectivity (union-find; link nearest cross-component pair)
    parent = list(range(n))
    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]; a = parent[a]
        return a
    for a, b in adj:
        parent[find(a)] = find(b)
    while len({find(i) for i in range(n)}) > 1:
        comps: dict[int, list[int]] = {}
        for i in range(n):
            comps.setdefault(find(i), []).append(i)
        roots = list(comps)
        best = None
        for a in comps[roots[0]]:
            for b in [x for r in roots[1:] for x in comps[r]]:
                d = _dist(pos[a], pos[b])
                if best is None or d < best[0]:
                    best = (d, a, b)
        _, a, b = best
        adj.add((min(a, b), max(a, b)))
        parent[find(a)] = find(b)
    return sorted(adj)


# --------------------------------------------------------------------------- #
# Transport weights ("volume")
# --------------------------------------------------------------------------- #
def default_weights(gd: GameData, *, solid: float = 1.0, fluid: float = 2.0
                    ) -> dict[str, float]:
    """belts/pipes per item/min, normalized: solids 1, fluids 2 (pipe = ½ belt
    throughput). rate already carries the bulk; this adds the medium difference."""
    return {c: (fluid if gd.is_fluid.get(c) else solid) for c in gd.item_name}


# --------------------------------------------------------------------------- #
# Output structures
# --------------------------------------------------------------------------- #
@dataclass
class RegionPlan:
    id: int
    pos: tuple[float, float]
    recipes: dict[str, float]          # recipe key -> machines here
    machines: float
    power_mw: float
    label: str
    imports: dict[str, float]          # item -> items/min shipped IN from other hubs
    exports: dict[str, float]          # item -> items/min shipped OUT to other hubs
    mined: dict[str, float] = None     # raw resource -> items/min extracted ON-SITE
    footprint: list = None             # [(x,y)] member mining-site centroids (territory)


@dataclass
class Shipment:
    src: int
    dst: int
    item: str
    rate: float
    dist: float


@dataclass
class SpatialPlan:
    regions: list[RegionPlan]
    shipments: list[Shipment]
    transport_cost: float              # Σ f·weight·dist (weight·item·units/min)
    single_hub_cost: float             # same metric if everything sat on one hub
    congestion_machines_cap: float
    status: str


def _greedy_assign(gd: GameData, sol: Solution, regions: list[Region],
                   raw_supplied: set[str], local_free: set[str],
                   K: int, weights: dict[str, float],
                   max_machines: float | None = None) -> dict[tuple, float]:
    """Assign each recipe to ONE region so that no region net-produces more than
    K distinct (non-raw) items. Deterministic and fast: walk recipes bottom-up
    (raw → end products), and place each in the nearest region to its inputs that
    still has a free product slot. Returns {(recipe_key, region): machines}."""
    G = len(regions)
    rpos = [(r.pos[0], r.pos[1]) for r in regions]
    cx = sum(p[0] for p in rpos) / G
    cy = sum(p[1] for p in rpos) / G

    # raw feedstock position = supply-weighted centroid of the regions mining it
    raw_pos: dict[str, tuple[float, float]] = {}
    for res in raw_supplied:
        sx = sy = tot = 0.0
        for reg in regions:
            s = reg.supply.get(res, 0.0)
            sx += reg.pos[0] * s; sy += reg.pos[1] * s; tot += s
        if tot > 0:
            raw_pos[res] = (sx / tot, sy / tot)

    # topological waves: a recipe is ready once all its inputs are produced
    available = set(raw_supplied) | set(local_free)
    remaining = dict(sol.recipes)
    order: list[str] = []
    while remaining:
        wave = [k for k in remaining if all(i in available
                                            for i in gd.recipes[k].inputs)]
        if not wave:                              # cycle (e.g. waste recycle)
            order.extend(remaining)
            break
        order.extend(wave)
        for k in wave:
            del remaining[k]
        for k in wave:
            available.update(gd.recipes[k].outputs)

    item_pos: dict[str, tuple[float, float]] = dict(raw_pos)
    region_net = [dict() for _ in range(G)]

    def products(g: int) -> int:
        return sum(1 for it, v in region_net[g].items()
                   if v > 0.5 and it not in gd.raw_resources)

    region_mach = [0.0] * G
    assign: dict[tuple, float] = {}
    INF = float("inf")

    def add(g, k, amt):                           # commit `amt` machines of k to g
        r = gd.recipes[k]
        for it in r.items:
            region_net[g][it] = region_net[g].get(it, 0.0) + r.rate(it) * amt
        assign[(k, g)] = assign.get((k, g), 0.0) + amt
        region_mach[g] += amt
        for it in r.outputs:
            if r.rate(it) > 0:
                item_pos.setdefault(it, rpos[g])

    def undo(g, k, amt):
        r = gd.recipes[k]
        for it in r.items:
            region_net[g][it] -= r.rate(it) * amt

    for k in order:
        r = gd.recipes[k]
        rem = sol.recipes[k]
        wx = wy = wt = 0.0
        for it, amt in r.inputs.items():
            p = item_pos.get(it)
            if p is None:
                continue
            w = amt * (60.0 / r.time) * sol.recipes[k] * weights.get(it, 1.0)
            wx += p[0] * w; wy += p[1] * w; wt += w
        ideal = (wx / wt, wy / wt) if wt > 0 else (cx, cy)
        cand = sorted(range(G), key=lambda g: (rpos[g][0] - ideal[0]) ** 2
                                              + (rpos[g][1] - ideal[1]) ** 2)
        # fill nearest hubs first, up to the per-hub machine cap, splitting the
        # recipe across hubs so a big line (e.g. iron smelting) becomes several
        # local factories near the ore instead of one mega-hub.
        for g in cand:
            if rem <= 1e-9:
                break
            room = INF if max_machines is None else max(0.0, max_machines - region_mach[g])
            if room <= 1e-9:
                continue
            amt = min(rem, room)
            add(g, k, amt)
            if products(g) <= K:
                rem -= amt
            else:
                undo(g, k, amt)
                assign[(k, g)] -= amt
                region_mach[g] -= amt
        if rem > 1e-9:                            # nowhere left within caps: nearest
            add(cand[0], k, rem)
    return {kg: v for kg, v in assign.items() if v > 1e-9}


def spatial_layout(
    gd: GameData, sol: Solution, nodes: list[dict],
    *,
    region_radius: float = 40_000.0,
    neighbors: int = 6,
    weights: dict[str, float] | None = None,
    free_capacity: float = 400.0,          # machines per region before congestion bites
    congestion_slope: float = 0.0,         # cost per machine·overflow tier (in item·units/min)
    congestion_tiers: int = 4,
    congestion_tier_width: float = 400.0,
    max_products: int | None = None,       # cap distinct manufactured outputs per region
    max_machines: float | None = None,     # cap machines per hub (splits big lines)
    time_limit_s: float = 120.0,
) -> SpatialPlan:
    """region_radius: basin clustering size (smaller -> more, tighter clusters).
    congestion_slope: marginal transport-equivalent cost charged per machine in
    each overflow tier above free_capacity; each next tier costs one slope more
    (convex). 0 disables the cluster-size penalty.
    max_products: if set, each region may net-produce at most this many distinct
    NON-raw items (specialize hubs). Turns the LP into a MILP (SCIP)."""
    weights = weights or default_weights(gd)
    place = assign_and_place(gd, sol, nodes)
    regions = _build_regions(place.sites, region_radius)
    G = len(regions)
    if G == 0:
        return SpatialPlan([], [], 0.0, 0.0, free_capacity, "NO_REGIONS")
    edges = _ship_network(regions, neighbors)

    # items in play
    raw_supplied = {res for r in regions for res in r.supply}
    items: set[str] = set()
    for k in sol.recipes:
        items |= gd.recipes[k].items
    items |= set(sol.extraction) | set(sol.outputs)
    # water (and any unsupplied raw) is treated as locally free everywhere
    local_free = {it for it in items
                  if it in gd.raw_resources and it not in raw_supplied}
    # Items consumed by recipes but produced by NO recipe and NO extraction come
    # from outside the recipe set (e.g. nuclear waste emitted by power plants,
    # which aren't recipes). Their balance can't close via shipping, so we assume
    # the source (a reactor) is co-located with the consumer and skip them.
    producible, consumable = set(), set()
    for k in sol.recipes:
        r = gd.recipes[k]
        for it in r.items:
            rt = r.rate(it)
            if rt > 0:
                producible.add(it)
            elif rt < 0:
                consumable.add(it)
    local_free |= {it for it in (consumable - producible)
                   if it not in raw_supplied}
    ship_items = sorted(items - local_free)

    solver = pywraplp.Solver.CreateSolver("GLOP")
    INF = solver.infinity()

    # x[r,g] machines of recipe r in region g. With max_products and/or
    # max_machines, a fast greedy assignment FIXES x (each recipe placed near its
    # inputs, ≤K products and ≤cap machines per hub, splitting big lines across
    # hubs) and we solve only the flows — the MILP is intractable tech-tree-wide.
    greedy = max_products is not None or max_machines is not None
    if greedy:
        x = {(k, g): 0.0 for k in sol.recipes for g in range(G)}
        x.update(_greedy_assign(gd, sol, regions, raw_supplied, local_free,
                                max_products or 10**9, weights,
                                max_machines=max_machines))
    else:
        x = {(k, g): solver.NumVar(0.0, INF, f"x_{k}_{g}")
             for k in sol.recipes for g in range(G)}
        for k, cnt in sol.recipes.items():
            solver.Add(solver.Sum([x[(k, g)] for g in range(G)]) == cnt)

    # E[res,g] raw extraction in region g, capped by local supply. We do NOT pin
    # Σ E == need: the fixed recipe totals already determine consumption, and the
    # per-region balance pulls exactly the extraction needed (over-extraction only
    # creates free in-region surplus, never shipped). Pinning the total would make
    # the LP infeasible by float noise when the map is fully maxed (Σ supply ==
    # consumption to ~1e-7).
    E = {}
    for res in raw_supplied:
        need = sol.extraction.get(res, 0.0)
        for g in range(G):
            cap = regions[g].supply.get(res, 0.0) if need > 0 else 0.0
            E[(res, g)] = solver.NumVar(0.0, cap, f"E_{res}_{g}")

    # f[i, a, b] directed shipment over each undirected edge (both directions)
    f = {}
    for it in ship_items:
        for (a, b) in edges:
            f[(it, a, b)] = solver.NumVar(0.0, INF, f"f_{it}_{a}_{b}")
            f[(it, b, a)] = solver.NumVar(0.0, INF, f"f_{it}_{b}_{a}")
    out_edges: dict[int, list[int]] = {g: [] for g in range(G)}
    in_edges: dict[int, list[int]] = {g: [] for g in range(G)}
    for (a, b) in edges:
        out_edges[a].append(b); out_edges[b].append(a)
        in_edges[b].append(a); in_edges[a].append(b)

    # per-region material balance: local production + imports - exports >= 0
    for it in ship_items:
        producers = [k for k in sol.recipes if it in gd.recipes[k].items]
        is_raw = it in raw_supplied
        for g in range(G):
            terms = [x[(k, g)] * gd.recipes[k].rate(it) for k in producers]
            expr = solver.Sum(terms) if terms else solver.Sum([])
            if is_raw:
                expr = expr + E[(it, g)]
            expr = expr + solver.Sum([f[(it, h, g)] for h in in_edges[g]])
            expr = expr - solver.Sum([f[(it, g, h)] for h in out_edges[g]])
            solver.Add(expr >= -1e-3)        # tiny slack absorbs LP float noise

    # congestion: convex PWL on machines per region (only when x is variable)
    cong_terms = []
    if congestion_slope > 0 and not greedy:
        for g in range(G):
            load = solver.Sum([x[(k, g)] for k in sol.recipes])
            segs = []
            # free segment
            s0 = solver.NumVar(0.0, free_capacity, f"c0_{g}")
            segs.append(s0)
            for t in range(1, congestion_tiers + 1):
                st = solver.NumVar(0.0, congestion_tier_width, f"c{t}_{g}")
                segs.append(st)
                cong_terms.append(congestion_slope * t * st)   # convex: slope grows
            # overflow beyond last tier (steep)
            sov = solver.NumVar(0.0, INF, f"cov_{g}")
            segs.append(sov)
            cong_terms.append(congestion_slope * (congestion_tiers + 2) * sov)
            solver.Add(load == solver.Sum(segs))

    # Objective in km (dist/1e5) to keep coefficients well-scaled — raw cm
    # distances make transport coeffs ~1e10, which combined with the congestion
    # penalty drives GLOP into ABNORMAL. (Reported costs below stay in cm.)
    KM = 1e5
    def edge_km(a, b):
        return _dist((regions[a].pos[0], regions[a].pos[1], 0.0),
                     (regions[b].pos[0], regions[b].pos[1], 0.0)) / KM
    transport = solver.Sum([
        var * weights.get(it, 1.0) * edge_km(a, b)
        for (it, a, b), var in f.items()
    ])
    solver.Minimize(transport + (solver.Sum(cong_terms) if cong_terms else solver.Sum([])))

    status = solver.Solve()
    status_name = {pywraplp.Solver.OPTIMAL: "OPTIMAL",
                   pywraplp.Solver.FEASIBLE: "FEASIBLE"}.get(status, str(status))
    if status not in (pywraplp.Solver.OPTIMAL, pywraplp.Solver.FEASIBLE):
        return SpatialPlan([], [], 0.0, 0.0, free_capacity, status_name)

    EPS = 1e-6
    xval = (lambda k, g: x[(k, g)]) if greedy \
        else (lambda k, g: x[(k, g)].solution_value())
    rplans: list[RegionPlan] = []
    for g in range(G):
        recs = {k: xval(k, g) for k in sol.recipes if xval(k, g) > EPS}
        mined = {res: E[(res, g)].solution_value() for res in raw_supplied
                 if E[(res, g)].solution_value() > 0.5}
        if not recs and not mined:        # truly empty region: skip
            continue
        machines = sum(recs.values())
        power = sum(gd.recipes[k].power * c for k, c in recs.items())
        net: dict[str, float] = {}
        for k, c in recs.items():
            for it in gd.recipes[k].outputs:
                net[it] = net.get(it, 0.0) + gd.recipes[k].rate(it) * c
        net = {it: v for it, v in net.items()
               if v > EPS and it not in gd.raw_resources}
        # name the factory by its most-processed ('top-level') output, breaking
        # ties on volume; a pure-mining outpost falls back to its dominant ore
        label = (max(net, key=lambda it: (gd.item_depth.get(it, 0), net[it])) if net
                 else (max(mined, key=mined.get) if mined else ""))
        footprint = [(s.centroid[0], s.centroid[1]) for s in regions[g].mine_sites]
        rplans.append(RegionPlan(
            id=regions[g].id, pos=regions[g].pos, recipes=recs,
            machines=machines, power_mw=power, label=label,
            imports={}, exports={}, mined=mined, footprint=footprint,
        ))
    rp_by_id = {rp.id: rp for rp in rplans}

    shipments: list[Shipment] = []
    transport_cost = 0.0
    for (it, a, b), var in f.items():
        v = var.solution_value()
        if v <= EPS:
            continue
        d = _dist((regions[a].pos[0], regions[a].pos[1], 0.0),
                  (regions[b].pos[0], regions[b].pos[1], 0.0))
        transport_cost += v * weights.get(it, 1.0) * d   # cost counts every flow
        if v < 0.5:                       # but don't record negligible noise flows
            continue
        shipments.append(Shipment(regions[a].id, regions[b].id, it, v, d))
        if a in rp_by_id:
            rp_by_id[a].exports[it] = rp_by_id[a].exports.get(it, 0.0) + v
        if b in rp_by_id:
            rp_by_id[b].imports[it] = rp_by_id[b].imports.get(it, 0.0) + v
    shipments.sort(key=lambda s: -s.rate * s.dist)

    # single-hub baseline on the SAME metric: every recipe + all extraction on
    # one weighted-centroid hub; only raw legs (mine region -> hub) move.
    hub = place.hub
    single = 0.0
    for res in raw_supplied:
        for g in range(G):
            r = E[(res, g)].solution_value()
            if r > EPS:
                single += r * weights.get(res, 1.0) * _dist(
                    (regions[g].pos[0], regions[g].pos[1], 0.0), hub)

    rplans.sort(key=lambda rp: -rp.machines)
    return SpatialPlan(
        regions=rplans, shipments=shipments,
        transport_cost=transport_cost, single_hub_cost=single,
        congestion_machines_cap=free_capacity, status=status_name,
    )
