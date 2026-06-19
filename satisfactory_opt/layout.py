"""Proximity layout: geometric-median (Weiszfeld) placement of every recipe.

Where `placement.assign_and_place` collapses all non-source recipes onto ONE
central hub, this places *each recipe* at the point that minimizes total
transport  Σ(flow × distance)  over the whole production graph, with only the
mining sites pinned to their real coordinates. There is **no central
destination**: every sub-factory settles next to whatever it exchanges the most
material with, so high-volume steps (ore→ingot→plate) hug the ore fields and
only the small, compacted flows travel far. The long links that survive between
dense clusters are exactly your train lines.

Because the objective Σ w·‖xᵢ − xⱼ‖ is convex and the iteration is
deterministic, the layout is **stable**: solve your full end-state basket once,
then build incrementally toward these fixed positions — early-game lines are a
subset of the final map and never need relocating.

Pipeline
--------
1. assign_and_place() gives the fixed mining sites (anchors).
2. _flow_edges() turns the solved recipe set into a weighted producer→consumer
   graph (gravity split when an item has several producers/consumers).
3. _weiszfeld() relaxes every free recipe to the flow-weighted geometric median
   of its neighbours until movement falls below tol.
4. cluster + aggregate → FactorySite list; cross-cluster edges → TrainLink list.
"""
from __future__ import annotations

from dataclasses import dataclass

from .data import GameData
from .placement import MiningSite, assign_and_place, _cluster
from .solver import Solution


# --------------------------------------------------------------------------- #
# Graph construction
# --------------------------------------------------------------------------- #
# Node ids: ("R", recipe_key) for a recipe instance (free),
#           ("M", site_index)  for a mining site    (fixed anchor).
FlowEdge = tuple[object, object, float, str]   # (src_id, dst_id, items/min, item)


def _flow_edges(gd: GameData, sol: Solution,
                mine_sites: list[MiningSite]) -> list[FlowEdge]:
    """Producer→consumer material flows.

    An item with multiple producers/consumers is split by a gravity rule: each
    producer ships in proportion to its share of total production, so the flow
    conserves the consumers' demand and ignores byproduct surplus (untransported).
    """
    prod: dict[str, list[tuple[object, float]]] = {}   # item -> [(node, rate_out)]
    cons: dict[str, list[tuple[object, float]]] = {}    # item -> [(node, rate_in)]

    for key, cnt in sol.recipes.items():
        r = gd.recipes[key]
        pm = 60.0 / r.time
        nid = ("R", key)
        for it, amt in r.outputs.items():
            prod.setdefault(it, []).append((nid, amt * pm * cnt))
        for it, amt in r.inputs.items():
            cons.setdefault(it, []).append((nid, amt * pm * cnt))

    # raw resources are produced at the mine anchors
    for idx, s in enumerate(mine_sites):
        prod.setdefault(s.resource, []).append((("M", idx), s.rate))

    edges: list[FlowEdge] = []
    for it, clist in cons.items():
        plist = prod.get(it)
        if not plist:
            continue                       # e.g. water (unlimited, local, no anchor)
        ptot = sum(p for _, p in plist)
        if ptot <= 1e-9:
            continue
        for cid, cin in clist:
            for pid, pout in plist:
                if pid == cid:
                    continue
                w = pout / ptot * cin
                if w > 1e-9:
                    edges.append((pid, cid, w, it))
    return edges


# --------------------------------------------------------------------------- #
# Weiszfeld relaxation (weighted geometric median, anchors fixed)
# --------------------------------------------------------------------------- #
def _weiszfeld(node_ids: list[object], fixed: set[object],
               pos: dict[object, tuple[float, float]], edges: list[FlowEdge],
               *, iters: int = 600, tol: float = 1.0
               ) -> tuple[dict[object, tuple[float, float]], int]:
    adj: dict[object, list[tuple[object, float]]] = {n: [] for n in node_ids}
    for a, b, w, _ in edges:
        adj[a].append((b, w))
        adj[b].append((a, w))
    free = [n for n in node_ids if n not in fixed]
    EPS = 1e-6
    done = 0
    for done in range(1, iters + 1):
        max_move = 0.0
        for n in free:                       # Gauss-Seidel: use freshest positions
            nbrs = adj[n]
            if not nbrs:
                continue
            px, py = pos[n]
            sx = sy = sw = 0.0
            for m, w in nbrs:
                mx, my = pos[m]
                d = ((px - mx) ** 2 + (py - my) ** 2) ** 0.5
                if d < EPS:
                    d = EPS
                k = w / d
                sx += k * mx
                sy += k * my
                sw += k
            if sw <= 0:
                continue
            nx, ny = sx / sw, sy / sw
            mv = ((nx - px) ** 2 + (ny - py) ** 2) ** 0.5
            if mv > max_move:
                max_move = mv
            pos[n] = (nx, ny)
        if max_move < tol:
            break
    return pos, done


def _effort(pos: dict[object, tuple[float, float]], edges: list[FlowEdge]) -> float:
    tot = 0.0
    for a, b, w, _ in edges:
        ax, ay = pos[a]
        bx, by = pos[b]
        tot += w * ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5
    return tot


# --------------------------------------------------------------------------- #
# Output structures
# --------------------------------------------------------------------------- #
@dataclass
class FactorySite:
    id: int
    pos: tuple[float, float]            # game units (cm)
    recipes: dict[str, float]          # recipe key -> machines
    machines: float
    power_mw: float
    label: str                         # dominant produced item (className)
    outbound: dict[str, float]         # item -> items/min leaving to OTHER sites
    inbound: dict[str, float]          # item -> items/min arriving from elsewhere


@dataclass
class TrainLink:
    src: object                        # site id (int) or ("mine", resource)
    dst: int
    item: str
    rate: float                        # items/min
    dist: float                        # game units (cm)


@dataclass
class LayoutPlan:
    sites: list[FactorySite]
    mine_sites: list[MiningSite]
    links: list[TrainLink]             # cross-site material flows (train candidates)
    effort_proximity: float            # Σ flow·dist for this layout
    effort_single_hub: float           # Σ flow·dist if all recipes sat on one hub
    hub: tuple[float, float]           # the single-hub baseline point
    iterations: int


def proximity_layout(
    gd: GameData, sol: Solution, nodes: list[dict],
    *, mine_cluster_radius: float = 30_000.0,
    site_cluster_radius: float = 60_000.0,
    iters: int = 600,
) -> LayoutPlan:
    """Compute a transport-minimal, hub-free layout for a solved factory.

    mine_cluster_radius groups raw nodes into mining sites (anchors);
    site_cluster_radius groups the relaxed recipe positions into sub-factories.
    """
    place = assign_and_place(gd, sol, nodes, cluster_radius=mine_cluster_radius)
    mine_sites = place.sites
    edges = _flow_edges(gd, sol, mine_sites)

    # node set
    recipe_ids = [("R", k) for k in sol.recipes]
    mine_ids = [("M", i) for i in range(len(mine_sites))]
    node_ids = recipe_ids + mine_ids
    fixed = set(mine_ids)

    pos: dict[object, tuple[float, float]] = {}
    for i, s in enumerate(mine_sites):
        pos[("M", i)] = (s.centroid[0], s.centroid[1])

    # init each free recipe at the rate-weighted centroid of its anchor
    # neighbours (if any), else at the global mine centroid.
    anchor_pull: dict[object, list[tuple[float, float, float]]] = {}
    for a, b, w, _ in edges:
        for x, y in ((a, b), (b, a)):
            if x in fixed and y not in fixed:
                px, py = pos[x]
                anchor_pull.setdefault(y, []).append((px, py, w))
    if mine_sites:
        gw = sum(s.rate for s in mine_sites) or 1.0
        gx = sum(s.centroid[0] * s.rate for s in mine_sites) / gw
        gy = sum(s.centroid[1] * s.rate for s in mine_sites) / gw
    else:
        gx = gy = 0.0
    for rid in recipe_ids:
        pulls = anchor_pull.get(rid)
        if pulls:
            tw = sum(w for _, _, w in pulls) or 1.0
            pos[rid] = (sum(x * w for x, _, w in pulls) / tw,
                        sum(y * w for _, y, w in pulls) / tw)
        else:
            pos[rid] = (gx, gy)

    pos, iterations = _weiszfeld(node_ids, fixed, pos, edges, iters=iters)
    effort_prox = _effort(pos, edges)

    # ---- single-hub baseline: every recipe on one throughput-weighted hub --- #
    hub = (place.hub[0], place.hub[1])
    pos_hub = dict(pos)
    for rid in recipe_ids:
        pos_hub[rid] = hub
    effort_hub = _effort(pos_hub, edges)

    # ---- cluster relaxed recipe positions into sub-factory sites ------------ #
    rpts = [pos[rid] for rid in recipe_ids]
    rkeys = [rid[1] for rid in recipe_ids]
    site_of: dict[str, int] = {}          # recipe key -> site id
    sites: list[FactorySite] = []
    for sid, grp in enumerate(_cluster([(p[0], p[1], 0.0) for p in rpts],
                                       site_cluster_radius)):
        keys = [rkeys[i] for i in grp]
        gpts = [rpts[i] for i in grp]
        recs = {k: sol.recipes[k] for k in keys}
        cx = sum(p[0] for p in gpts) / len(gpts)
        cy = sum(p[1] for p in gpts) / len(gpts)
        machines = sum(recs.values())
        power = sum(gd.recipes[k].power * c for k, c in recs.items())
        sites.append(FactorySite(
            id=sid, pos=(cx, cy), recipes=recs, machines=machines,
            power_mw=power, label="", outbound={}, inbound={},
        ))
        for k in keys:
            site_of[k] = sid

    def site_for(nid) -> object:
        return site_of[nid[1]] if nid[0] == "R" else ("mine", mine_sites[nid[1]].resource)

    # ---- cross-site flows (train candidates) + per-site in/out -------------- #
    link_agg: dict[tuple, tuple[float, float]] = {}   # (src,dst,item) -> (rate, dist)
    for a, b, w, it in edges:
        sa, sb = site_for(a), site_for(b)
        if sa == sb:
            continue
        ax, ay = pos[a]
        bx, by = pos[b]
        d = ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5
        key = (sa, sb, it)
        r0, _ = link_agg.get(key, (0.0, d))
        link_agg[key] = (r0 + w, d)
        if isinstance(sb, int):
            sites[sb].inbound[it] = sites[sb].inbound.get(it, 0.0) + w
        if isinstance(sa, int):
            sites[sa].outbound[it] = sites[sa].outbound.get(it, 0.0) + w

    links = [TrainLink(src=s, dst=d, item=it, rate=r, dist=dist)
             for (s, d, it), (r, dist) in link_agg.items()]
    links.sort(key=lambda l: -l.rate * l.dist)

    # label each site by the produced item with the largest net output
    for st in sites:
        net: dict[str, float] = {}
        for k, c in st.recipes.items():
            r = gd.recipes[k]
            for it in r.outputs:
                net[it] = net.get(it, 0.0) + r.rate(it) * c
        net = {it: v for it, v in net.items()
               if v > 1e-6 and it not in gd.raw_resources}
        st.label = max(net, key=net.get) if net else ""

    sites.sort(key=lambda s: -s.machines)
    return LayoutPlan(
        sites=sites, mine_sites=mine_sites, links=links,
        effort_proximity=effort_prox, effort_single_hub=effort_hub,
        hub=hub, iterations=iterations,
    )
