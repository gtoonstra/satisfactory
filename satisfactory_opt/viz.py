"""Visualization exporter.

Turns a solved `Solution` (plus optional node coordinates) into a single,
self-contained `index.html` that renders a production-flow graph plus a
geographic map that splits into three layers:

  1. PRODUCTION FLOW  -- a layered graph of the factory: raw resources on the
     left flow rightward through recipe/building nodes to the shipped targets
     on the right. Edges are belt/pipe flows sized by items-per-minute, so you
     can read at a glance which recipe a part comes from and how much.

  2. RESOURCE MAP     -- the real Satisfactory map with three switchable layers
     that decouple production from logistics:
       * Factories  -- production sites; click to trace input *sourcing*.
       * Mining     -- on-site raw extraction per region.
       * Transport  -- the logistics network: production regions grid-clustered
         into logistics hubs (tunable live via a density slider), with the
         surviving inter-hub trains aggregated into lines sized by items/min.

The views are linked: from a raw resource's inspector, "show source nodes on
map" jumps to the Mining layer and highlights the sites feeding it.

The logistics clustering is purely a *view* over the solved spatial plan -- it
re-groups regions and re-aggregates the existing shipments; it does not change
the optimization. All three layers read the same `data["spatial"]` payload.

`write_visualization()` writes `index.html` (data embedded inline so it opens
straight off the filesystem with a double-click), a copy of `map_bg.jpg`, and
`solution.json` for inspection. No network access, no build step, no CDN.
"""
from __future__ import annotations

import json
import os
import shutil
from collections import deque

from .data import GameData
from .solver import Solution
from .placement import stage_recipes, assign_and_place, node_rate
from .spatial_layout import spatial_layout

ASSET_MAP = os.path.join(os.path.dirname(__file__), "assets", "map_bg.jpg")
TEMPLATE = os.path.join(os.path.dirname(__file__), "assets", "viz_template.html")

# SCIM realistic-map calibration (game units -> 2048x2048 backdrop pixels).
MAP_BOUNDS = {"west": -324698.832031, "east": 425301.832031,
              "north": -375000.0, "south": 375000.0, "imgW": 2048, "imgH": 2048}


def _layer_assign(node_ids: list[str], edges: list[tuple[str, str]],
                  roots: set[str]) -> dict[str, int]:
    """Longest-path layering on a possibly-cyclic graph.

    Cycles are broken first by a DFS that drops edges pointing back at a node
    still on the recursion stack, then layers are the longest path from any
    root (raw resource) over the resulting DAG.
    """
    adj: dict[str, list[str]] = {n: [] for n in node_ids}
    for a, b in edges:
        if a in adj:
            adj[a].append(b)

    # DFS edge classification -> set of back-edges to ignore.
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in node_ids}
    back: set[tuple[str, str]] = set()
    order_seed = list(roots) + [n for n in node_ids if n not in roots]
    for s in order_seed:
        if color[s] != WHITE:
            continue
        stack = [(s, iter(adj[s]))]
        color[s] = GRAY
        while stack:
            node, it = stack[-1]
            advanced = False
            for nxt in it:
                if color[nxt] == GRAY:
                    back.add((node, nxt))            # back-edge -> drop
                elif color[nxt] == WHITE:
                    color[nxt] = GRAY
                    stack.append((nxt, iter(adj[nxt])))
                    advanced = True
                    break
            if not advanced:
                color[node] = BLACK
                stack.pop()

    dag: dict[str, list[str]] = {n: [] for n in node_ids}
    indeg = {n: 0 for n in node_ids}
    for a, b in edges:
        if (a, b) in back or a not in dag:
            continue
        dag[a].append(b)
        indeg[b] += 1

    layer = {n: 0 for n in node_ids}
    q = deque([n for n in node_ids if indeg[n] == 0])
    # Kahn topo order, relaxing longest path.
    seen = 0
    while q:
        n = q.popleft()
        seen += 1
        for m in dag[n]:
            if layer[n] + 1 > layer[m]:
                layer[m] = layer[n] + 1
            indeg[m] -= 1
            if indeg[m] == 0:
                q.append(m)
    return layer


def _alpha(d: dict) -> dict:
    """Sort a {name: value} dict alphabetically by name (case-insensitive)."""
    return dict(sorted(d.items(), key=lambda kv: kv[0].lower()))


def _net_flow(gd, a: dict, b: dict) -> dict:
    """{item_name: a-b} for items where a exceeds b by >0.5 — i.e. the part of
    flow `a` that isn't cancelled by opposite flow `b` (drops pass-through)."""
    out = {}
    for it, v in a.items():
        net = v - b.get(it, 0.0)
        if net > 0.5:
            out[gd.item_name.get(it, it)] = net
    return _alpha(out)


def _transit_flow(gd, imp: dict, exp: dict) -> dict:
    """{item_name: min(in,out)} — items merely routed THROUGH this hub."""
    out = {}
    for it in set(imp) & set(exp):
        t = min(imp[it], exp[it])
        if t > 0.5:
            out[gd.item_name.get(it, it)] = t
    return _alpha(out)


def build_viz_data(gd: GameData, sol: Solution, *,
                   nodes: list[dict] | None = None,
                   targets: list[str] | None = None,
                   region_radius: float = 40_000.0,
                   congestion: float = 0.0,
                   free_capacity: float = 400.0,
                   max_products: int | None = None,
                   max_machines: float | None = None) -> dict:
    """Assemble the JSON the HTML view consumes."""
    # ---- flow graph ------------------------------------------------------ #
    plan = stage_recipes(gd, sol)
    item_ids: set[str] = set()
    recipe_nodes = []
    edges: list[tuple[str, str]] = []
    flow_edges = []  # (src_id, dst_id, item, rate)

    for key, cnt in sol.recipes.items():
        r = gd.recipes[key]
        per_min = 60.0 / r.time
        rid = "R:" + key
        ins = [{"item": it, "rate": amt * per_min * cnt}
               for it, amt in r.inputs.items()]
        outs = [{"item": it, "rate": amt * per_min * cnt}
                for it, amt in r.outputs.items()]
        stage = "source" if key in plan.source_recipes else "hub"
        recipe_nodes.append({
            "id": rid, "name": r.name, "alt": r.alternate,
            "machine": gd.machine_name.get(r.machine, r.machine),
            "machine_key": r.machine,
            "count": cnt, "power_each": r.power, "power_total": r.power * cnt,
            "stage": stage, "inputs": ins, "outputs": outs,
        })
        for d in ins:
            iid = "I:" + d["item"]
            item_ids.add(d["item"])
            edges.append((iid, rid))
            flow_edges.append({"src": iid, "dst": rid,
                               "item": d["item"], "rate": d["rate"]})
        for d in outs:
            iid = "I:" + d["item"]
            item_ids.add(d["item"])
            edges.append((rid, iid))
            flow_edges.append({"src": rid, "dst": iid,
                               "item": d["item"], "rate": d["rate"]})

    item_ids |= {it for it in sol.extraction}
    item_ids |= {it for it in sol.outputs}
    target_set = set(targets or list(sol.outputs))

    item_nodes = []
    for it in sorted(item_ids):
        item_nodes.append({
            "id": "I:" + it, "cls": it, "name": gd.item_name.get(it, it),
            "fluid": bool(gd.is_fluid.get(it)),
            "raw": it in gd.raw_resources,
            "target": it in target_set,
            "extraction": sol.extraction.get(it, 0.0),
            "shipped": sol.outputs.get(it, 0.0),
            "sink_points": gd.sink_points.get(it, 0),
        })

    all_node_ids = [n["id"] for n in item_nodes] + [n["id"] for n in recipe_nodes]
    roots = {"I:" + it for it in sol.extraction}
    layer = _layer_assign(all_node_ids, edges, roots)
    for n in item_nodes:
        n["layer"] = layer.get(n["id"], 0)
    for n in recipe_nodes:
        n["layer"] = layer.get(n["id"], 0)

    data: dict = {
        "meta": {
            "status": sol.status, "scale": sol.scale,
            "targets": [gd.item_name.get(t, t) for t in target_set],
            "power_consumed_mw": sol.total_power_mw,
            "power_generated_mw": sol.power_generated_mw,
            "machines_total": sum(sol.recipes.values()),
            "recipe_count": len(sol.recipes),
            "staged_belt_per_min": plan.staged_belt_per_min,
            "raw_belt_per_min": plan.raw_belt_per_min,
        },
        "items": item_nodes,
        "recipes": recipe_nodes,
        "flows": flow_edges,
        "outputs": {gd.item_name.get(k, k): v for k, v in sol.outputs.items()},
        "extraction": {gd.item_name.get(k, k): v for k, v in sol.extraction.items()},
        "generators": {gd.item_name.get(f, f): v for f, v in sol.generators.items()},
        "waste_per_min": {gd.item_name.get(w, w): v for w, v in sol.waste_per_min.items()},
        "map": None,
    }

    # ---- geographic map -------------------------------------------------- #
    if nodes:
        place = assign_and_place(gd, sol, nodes)
        chosen_keys = {(round(nd["x"]), round(nd["y"]), nd["resource"])
                       for s in place.sites for nd in s.nodes}
        all_nodes = [{
            "res": nd["resource"], "res_name": gd.item_name.get(nd["resource"], nd["resource"]),
            "purity": nd.get("purity", "normal"), "kind": nd.get("kind", "node"),
            "x": nd["x"], "y": nd["y"],
            "rate": node_rate(nd),
            "used": (round(nd["x"]), round(nd["y"]), nd["resource"]) in chosen_keys,
        } for nd in nodes]
        sites = [{
            "res": s.resource, "res_name": gd.item_name.get(s.resource, s.resource),
            "rate": s.rate, "n": len(s.nodes),
            "x": s.centroid[0], "y": s.centroid[1],
        } for s in place.sites]
        data["map"] = {
            "bounds": MAP_BOUNDS,
            "hub": {"x": place.hub[0], "y": place.hub[1]},
            "sites": sites,
            "nodes": all_nodes,
            "transport_effort": place.transport_effort,
            "unmet": {gd.item_name.get(r, r): v for r, v in place.unmet.items()},
        }

        # ---- spatial layout: where each recipe runs, hub-free ------------- #
        sp = spatial_layout(gd, sol, nodes, region_radius=region_radius,
                            congestion_slope=congestion, free_capacity=free_capacity,
                            max_products=max_products, max_machines=max_machines)
        if sp.status in ("OPTIMAL", "FEASIBLE"):
            target_set2 = set(targets or list(sol.outputs))
            regions = []
            for rp in sp.regions:
                # net flow per item across the whole region (production - consumption).
                # net > 0 -> the region PRODUCES it (ships out or final);
                # net < 0 -> the region needs it as INPUT (raw mined here or imported).
                net: dict[str, float] = {}
                for k, c in rp.recipes.items():
                    r = gd.recipes[k]
                    for it in r.items:
                        net[it] = net.get(it, 0.0) + r.rate(it) * c
                # net > 0 -> region produces it; net < 0 -> region needs it as input.
                # Threshold 0.5/min drops items made and consumed in-region (net ~0
                # float residue) that would otherwise clutter the sizing breakdown.
                produces, inputs = {}, {}
                for it, v in net.items():
                    if v > 0.5 and it not in gd.raw_resources:
                        produces[gd.item_name.get(it, it)] = v
                    elif v < -0.5:
                        inputs[gd.item_name.get(it, it)] = -v
                # count machines by building type for the sizing breakdown
                by_machine: dict[str, float] = {}
                for k, c in rp.recipes.items():
                    m = gd.machine_name.get(gd.recipes[k].machine, gd.recipes[k].machine)
                    by_machine[m] = by_machine.get(m, 0.0) + c
                regions.append({
                    "id": rp.id, "x": rp.pos[0], "y": rp.pos[1],
                    "machines": rp.machines, "power": rp.power_mw,
                    "label": rp.label, "label_name": gd.item_name.get(rp.label, rp.label),
                    "machine_key": gd.recipes[max(rp.recipes, key=rp.recipes.get)].machine
                                   if rp.recipes else "",
                    "by_machine": _alpha(by_machine),
                    "produces": _alpha(produces),
                    # inflow split by provenance: mined here vs shipped in
                    "mined": _alpha({gd.item_name.get(i, i): v
                                     for i, v in (rp.mined or {}).items()}),
                    # mined_by_cls stays rate-sorted — its first key is the dominant
                    # ore used to colour the mining-outpost marker.
                    "mined_by_cls": dict(sorted((rp.mined or {}).items(),
                                                key=lambda kv: -kv[1])),
                    "mined_total": sum((rp.mined or {}).values()),
                    # net out transit (item shipped both in and out = pass-through):
                    # imported = consumed here, exported = produced here, transit = routed through
                    "imported": _net_flow(gd, rp.imports, rp.exports),
                    "exported": _net_flow(gd, rp.exports, rp.imports),
                    "transit": _transit_flow(gd, rp.imports, rp.exports),
                    "footprint": [[x, y] for x, y in (rp.footprint or [])],
                })
            data["spatial"] = {
                "regions": regions,
                "shipments": [{
                    "src": s.src, "dst": s.dst, "item": s.item,
                    "item_name": gd.item_name.get(s.item, s.item),
                    "fluid": bool(gd.is_fluid.get(s.item)),
                    "rate": s.rate, "dist": s.dist,
                } for s in sp.shipments],
                "transport_cost": sp.transport_cost,
                "single_hub_cost": sp.single_hub_cost,
                "congestion": congestion, "free_capacity": free_capacity,
            }
    return data


def write_visualization(gd: GameData, sol: Solution, out_dir: str, *,
                        nodes: list[dict] | None = None,
                        targets: list[str] | None = None,
                        region_radius: float = 40_000.0,
                        congestion: float = 0.0,
                        free_capacity: float = 400.0,
                        max_products: int | None = None,
                        max_machines: float | None = None) -> str:
    os.makedirs(out_dir, exist_ok=True)
    data = build_viz_data(gd, sol, nodes=nodes, targets=targets,
                          region_radius=region_radius, congestion=congestion,
                          free_capacity=free_capacity, max_products=max_products, max_machines=max_machines)

    with open(os.path.join(out_dir, "solution.json"), "w") as fh:
        json.dump(data, fh, indent=2)

    if data["map"] is not None:
        shutil.copyfile(ASSET_MAP, os.path.join(out_dir, "map_bg.jpg"))

    with open(TEMPLATE) as fh:
        html = fh.read()
    html = html.replace("/*__DATA__*/", "const DATA = " + json.dumps(data) + ";")
    out_html = os.path.join(out_dir, "index.html")
    with open(out_html, "w") as fh:
        fh.write(html)
    return out_html
