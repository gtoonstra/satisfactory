"""Command-line interface for the Satisfactory production optimizer.

Examples
--------
  # Balanced 1:1 basket of two end items, whole-map resources
  python -m satisfactory_opt "Turbo Motor" "Supercomputer"

  # Weighted basket (3 motors per 1 computer), no alternate recipes
  python -m satisfactory_opt "Turbo Motor=3" "Supercomputer=1" --no-alternates

  # Single item, maximize it
  python -m satisfactory_opt "Reinforced Iron Plate"

  # Custom resource budget
  python -m satisfactory_opt "Modular Frame" --resources budget.json
"""
from __future__ import annotations

import argparse
import json
import sys

from .data import GameData
from .solver import Target, solve


def basket_weight(gd: GameData, item: str, scheme: str) -> float:
    """Ratio weight for an end-product in the `all` basket, by sink value."""
    p = max(1.0, float(gd.sink_points.get(item, 0)))
    if scheme == "equal":
        return 1.0
    if scheme == "points":
        return p
    if scheme == "inv-points":
        return 1.0 / p
    return 1.0 / (p ** 0.5)          # inv-sqrt: balanced middle


def parse_target(gd: GameData, spec: str) -> Target:
    if "=" in spec:
        name, w = spec.rsplit("=", 1)
        weight = float(w)
    else:
        name, weight = spec, 1.0
    return Target(item=gd.resolve_item(name), weight=weight)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="satisfactory_opt", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("targets", nargs="+",
                   help="end items, optionally 'Name=weight' for ratios; or the token "
                        "'all' to target every terminal end-product (whole-map basket)")
    p.add_argument("--no-alternates", action="store_true",
                   help="use only standard (non-alternate) recipes")
    p.add_argument("--basket", choices=["equal", "points", "inv-points", "inv-sqrt"],
                   default="equal",
                   help="how the `all` token weights end-products: equal (same "
                        "items/min — build is dominated by the costliest items, ammo a "
                        "tiny corner; default), points (∝ sink value), inv-points "
                        "(equal AWESOME points/min each, ammo-heavy), inv-sqrt (middle)")
    p.add_argument("--exclude", nargs="*", default=[],
                   help="recipe names to forbid")
    p.add_argument("--resources", metavar="JSON",
                   help="file of {item: rate_per_min} overriding map limits")
    p.add_argument("--miner-clock", type=float, default=1.0,
                   help="scale default map limits (e.g. 0.4); ignored with --resources")
    p.add_argument("--mode", choices=["ratio", "sum"], default="ratio",
                   help="ratio: lock outputs to weights (default); sum: max weighted total")
    p.add_argument("--no-power", action="store_true",
                   help="disable the nuclear power constraint (ignore power)")
    p.add_argument("--power-margin", type=float, default=1.2,
                   help="required generation as a multiple of machine draw "
                        "(grid headroom). Default 1.2 = 20%% over capacity.")
    p.add_argument("--allow-waste", action="store_true",
                   help="permit PLUTONIUM Waste to accumulate, skipping Ficsonium "
                        "(higher output). Uranium Waste is always recycled to "
                        "plutonium. Default is a fully closed, waste-free loop.")
    p.add_argument("--placement", action="store_true",
                   help="also show the mine-vs-hub staging plan (smelt at source)")
    p.add_argument("--nodes", metavar="JSON",
                   help="node coords file {resource,purity,x,y,z}[]; enables geo placement")
    p.add_argument("--proximity", action="store_true",
                   help="hub-free proximity layout: place EVERY recipe to minimize "
                        "Σ(flow×distance) (Weiszfeld), cluster into sub-factories, and "
                        "report inter-site train flows. Needs --nodes.")
    p.add_argument("--spatial", action="store_true",
                   help="spatial LP: decide WHERE each recipe runs across map regions, "
                        "minimizing Σ(flow×weight×distance) + a cluster-size penalty. "
                        "Needs --nodes. Tune with --region-radius / --congestion.")
    p.add_argument("--region-radius", type=float, default=40_000.0,
                   help="basin cluster size in game units (smaller -> more, tighter "
                        "regions). Default 40000 (~400 m).")
    p.add_argument("--congestion", type=float, default=0.0,
                   help="cluster-size penalty: transport-equivalent cost (item·km/min "
                        "per machine) charged in each overflow tier above "
                        "--free-capacity (convex). 0 = off (pure transport min). "
                        "Try 10–1000; --free-capacity sets the per-region machine cap.")
    p.add_argument("--free-capacity", type=float, default=400.0,
                   help="machines a region hosts before the --congestion penalty starts")
    p.add_argument("--max-products", type=int, default=None,
                   help="cap distinct manufactured outputs per region (specialize hubs, "
                        "e.g. 2 or 3). Uses a fast greedy assignment + flow solve.")
    p.add_argument("--max-machines", type=float, default=None,
                   help="cap machines per hub; splits big production lines across "
                        "several local hubs (e.g. many small iron-smelting sites near "
                        "the ore instead of one mega-hub). Sets the hub count.")
    p.add_argument("--viz", metavar="DIR",
                   help="write an interactive HTML visualizer to DIR (open DIR/index.html). "
                        "Pass --nodes too for the geographic resource-map view.")
    p.add_argument("--json", action="store_true", help="emit JSON instead of a report")
    p.add_argument("--data", default=None, help="path to data.json")
    args = p.parse_args(argv)

    gd = GameData(args.data) if args.data else GameData()

    try:
        if any(s.strip().lower() == "all" for s in args.targets):
            # Reactors are recipes now, so the Plutonium/Ficsonium chain is
            # reachable from raw uranium without any seeding.
            items = [c for c in gd.terminal_products()
                     if gd.sink_points.get(c, 0) > 0]   # need a value to weight by
            targets = [Target(item=c, weight=basket_weight(gd, c, args.basket))
                       for c in items]
            print(f"targeting all {len(targets)} terminal end-products "
                  f"({args.basket} basket)", file=sys.stderr)
        else:
            targets = [parse_target(gd, s) for s in args.targets]
    except KeyError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    resource_caps = None
    if args.resources:
        with open(args.resources) as fh:
            raw = json.load(fh)
        resource_caps = {gd.resolve_item(k): float(v) for k, v in raw.items()}

    excluded = set()
    for name in args.exclude:
        excluded |= {r.key for r in gd.recipes.values() if r.name.lower() == name.lower()}

    sol = solve(
        gd, targets,
        allow_alternates=not args.no_alternates,
        excluded_recipes=excluded,
        resource_caps=resource_caps,
        miner_clock=args.miner_clock,
        mode=args.mode,
        constrain_power=not args.no_power,
        power_margin=args.power_margin,
        allow_waste=args.allow_waste,
    )

    nodes = None
    if args.nodes:
        with open(args.nodes) as fh:
            nodes = json.load(fh)
        for nd in nodes:                       # node files normally use classNames
            if not nd["resource"].startswith("Desc_"):
                try:
                    nd["resource"] = gd.resolve_item(nd["resource"])
                except KeyError:
                    pass

    if args.viz and sol.status in ("OPTIMAL", "FEASIBLE"):
        from .viz import write_visualization
        out = write_visualization(gd, sol, args.viz,
                                  nodes=nodes, targets=[t.item for t in targets],
                                  region_radius=args.region_radius,
                                  congestion=args.congestion,
                                  free_capacity=args.free_capacity,
                                  max_products=args.max_products,
                                  max_machines=args.max_machines)
        msg = f"visualizer written: {out}"
        if nodes is None:
            msg += "  (add --nodes nodes.json for the geographic map view)"
        print(msg, file=sys.stderr)

    if args.json:
        print(json.dumps({
            "status": sol.status, "scale": sol.scale,
            "outputs": {gd.item_name[k]: v for k, v in sol.outputs.items()},
            "extraction": {gd.item_name[k]: v for k, v in sol.extraction.items()},
            "recipes": {gd.recipes[k].name: v for k, v in sol.recipes.items()},
            "power_consumed_mw": sol.total_power_mw,
            "power_generated_mw": sol.power_generated_mw,
            "nuclear_plants": {gd.item_name.get(f, f): v for f, v in sol.generators.items()},
            "fuel_rods_per_min": {gd.item_name.get(f, f): v for f, v in sol.fuel_burn.items()},
            "radioactive_waste_per_min": {gd.item_name.get(w, w): v for w, v in sol.waste_per_min.items()},
        }, indent=2))
        return 0

    print_report(gd, sol)

    if (args.placement or args.nodes) and sol.status in ("OPTIMAL", "FEASIBLE"):
        from .placement import stage_recipes, assign_and_place
        plan = stage_recipes(gd, sol)
        print_staging(gd, plan)
        if nodes is not None:
            place = assign_and_place(gd, sol, nodes)
            print_placement(gd, place)

    if args.proximity and sol.status in ("OPTIMAL", "FEASIBLE"):
        if nodes is None:
            print("error: --proximity requires --nodes nodes.json", file=sys.stderr)
            return 2
        from .layout import proximity_layout
        lp = proximity_layout(gd, sol, nodes)
        print_layout(gd, lp)

    if args.spatial and sol.status in ("OPTIMAL", "FEASIBLE"):
        if nodes is None:
            print("error: --spatial requires --nodes nodes.json", file=sys.stderr)
            return 2
        from .spatial_layout import spatial_layout
        sp = spatial_layout(gd, sol, nodes,
                            region_radius=args.region_radius,
                            congestion_slope=args.congestion,
                            free_capacity=args.free_capacity,
                            max_products=args.max_products,
                            max_machines=args.max_machines)
        print_spatial(gd, sp)

    return 0 if sol.status in ("OPTIMAL", "FEASIBLE") else 1


def print_report(gd: GameData, sol) -> None:
    if sol.status not in ("OPTIMAL", "FEASIBLE"):
        print(f"No solution: {sol.status}")
        return
    w = 34
    print("=" * 60)
    print(f"  STATUS: {sol.status}    basket scale T = {sol.scale:.2f}/min")
    print("=" * 60)

    print("\nOUTPUT (items/min shipped):")
    for k, v in sorted(sol.outputs.items(), key=lambda kv: -kv[1]):
        print(f"  {gd.item_name[k]:<{w}} {v:>12.2f}")

    print("\nRAW EXTRACTION (items/min):")
    for k, v in sorted(sol.extraction.items(), key=lambda kv: -kv[1]):
        print(f"  {gd.item_name[k]:<{w}} {v:>12.2f}")

    print(f"\nMACHINES ({sum(sol.recipes.values()):.1f} total, fractional ok):")
    rows = sorted(sol.recipes.items(), key=lambda kv: -kv[1])
    for key, cnt in rows:
        r = gd.recipes[key]
        tag = " [alt]" if r.alternate else ""
        mach = gd.machine_name.get(r.machine, r.machine)
        print(f"  {r.name + tag:<{w}} {cnt:>8.2f} x {mach}")

    print(f"\nPOWER:  draw {sol.total_power_mw:,.0f} MW", end="")
    if sol.power_generated_mw > 0 or sol.generators:
        print(f"   generated {sol.power_generated_mw:,.0f} MW (nuclear)")
        total_plants = sum(sol.generators.values())
        print(f"  Nuclear Power Plants: {total_plants:,.1f}")
        for f, n in sorted(sol.generators.items(), key=lambda kv: -kv[1]):
            rods = sol.fuel_burn.get(f, 0.0)
            print(f"    {gd.item_name.get(f, f):<22} {n:>8.2f} plants  "
                  f"({rods:.2f} rods/min)")
        ur = sol.extraction.get("Desc_OreUranium_C", 0.0)
        print(f"  Uranium ore -> power: {ur:,.0f}/min (feeds the fuel-rod chain)")
        if sol.waste_per_min:
            print("  RADIOACTIVE WASTE ACCUMULATING (per min):")
            for w, amt in sorted(sol.waste_per_min.items(), key=lambda kv: -kv[1]):
                print(f"    {gd.item_name.get(w, w):<22} {amt:>10.1f}  <-- must be stored")
        else:
            print("  Radioactive waste: 0  (closed loop -> Ficsonium terminates the chain)")
    else:
        print("   (power constraint disabled)")


def print_staging(gd, plan) -> None:
    w = 34
    print("\n" + "=" * 60)
    print("  PLACEMENT STRATEGY  (smelt at source -> ship to hub)")
    print("=" * 60)
    print(f"\nSOURCE stage @ mines ({sum(plan.source_recipes.values()):.1f} machines):")
    for key, cnt in sorted(plan.source_recipes.items(), key=lambda kv: -kv[1]):
        r = gd.recipes[key]
        print(f"  {r.name:<{w}} {cnt:>8.2f} x {gd.machine_name.get(r.machine, r.machine)}")
    print(f"\nHUB stage @ central factory ({sum(plan.hub_recipes.values()):.1f} machines): "
          f"{len(plan.hub_recipes)} recipes")
    print("\nGoods crossing mine -> hub (items/min):")
    for it, amt in sorted(plan.boundary_flows.items(), key=lambda kv: -kv[1]):
        print(f"  {gd.item_name[it]:<{w}} {amt:>12.2f}")
    raw, staged = plan.raw_belt_per_min, plan.staged_belt_per_min
    print(f"\n  raw ore if smelted centrally : {raw:>12.2f} items/min on long belts")
    print(f"  refined goods (smelt@source) : {staged:>12.2f} items/min on long belts")
    if raw > 0:
        print(f"  => transport volume reduced by {100*(1-staged/raw):.1f}% "
              f"by smelting at the source")


def print_placement(gd, place, top: int = 12) -> None:
    print("\n" + "=" * 60)
    print("  GEOGRAPHIC PLACEMENT  (real map nodes -> central hub)")
    print("=" * 60)
    hx, hy = place.hub[0] / 100, place.hub[1] / 100   # game units (cm) -> m
    print(f"\nCentral hub @ (x={hx:.0f} m, y={hy:.0f} m)")
    print(f"Transport effort = {place.transport_effort/1e5:,.0f} item*km/min (lower=better)")

    # per-resource summary
    by: dict[str, list] = {}
    for s in place.sites:
        by.setdefault(s.resource, []).append(s)
    print(f"\n{'resource':<16}{'sites':>6}{'nodes':>7}{'rate/min':>11}")
    for res, ss in sorted(by.items(), key=lambda kv: -sum(s.rate for s in kv[1])):
        nm = gd.item_name.get(res, res)
        print(f"  {nm:<14}{len(ss):>6}{sum(len(s.nodes) for s in ss):>7}"
              f"{sum(s.rate for s in ss):>11.0f}")

    print(f"\nLargest mining sites (smelt here, ship refined to hub):")
    for s in sorted(place.sites, key=lambda s: -s.rate)[:top]:
        nm = gd.item_name.get(s.resource, s.resource)
        print(f"  {nm:<14} {s.rate:>7.0f}/min  {len(s.nodes):>2} nodes "
              f"@ ({s.centroid[0]/100:>7.0f}, {s.centroid[1]/100:>7.0f}) m")
    if len(place.sites) > top:
        print(f"  ... and {len(place.sites)-top} smaller sites")
    if place.unmet:
        print("\n  WARNING: not enough nodes for:")
        for r, sh in place.unmet.items():
            print(f"    {gd.item_name.get(r, r)}: short {sh:.0f}/min")


def print_layout(gd, lp, top_sites: int = 14, top_links: int = 16) -> None:
    print("\n" + "=" * 60)
    print("  PROXIMITY LAYOUT  (hub-free: place every recipe by Σ flow×distance)")
    print("=" * 60)
    ep, eh = lp.effort_proximity, lp.effort_single_hub
    print(f"\nTransport effort  (item·km/min, lower=better):")
    print(f"  single central hub : {eh/1e5:>12,.0f}")
    print(f"  proximity layout   : {ep/1e5:>12,.0f}   ({lp.iterations} iters)")
    if eh > 0:
        print(f"  => {100*(1-ep/eh):.1f}% less material movement, "
              f"spread over {len(lp.sites)} sub-factory sites")

    print(f"\nSUB-FACTORY SITES ({len(lp.sites)} total; build big ones here):")
    print(f"  {'#':>3} {'dominant product':<26}{'mach':>7}{'MW':>9}"
          f"   {'location (m)':>18}")
    for st in lp.sites[:top_sites]:
        nm = gd.item_name.get(st.label, st.label or "—")
        loc = f"({st.pos[0]/100:>7.0f},{st.pos[1]/100:>7.0f})"
        print(f"  {st.id:>3} {nm:<26}{st.machines:>7.1f}{st.power_mw:>9.0f}   {loc:>18}")
    if len(lp.sites) > top_sites:
        rest = lp.sites[top_sites:]
        print(f"  ... and {len(rest)} smaller sites "
              f"({sum(s.machines for s in rest):.0f} machines total)")

    def lbl(node):
        if isinstance(node, tuple) and node[0] == "mine":
            return f"⛏ {gd.item_name.get(node[1], node[1])}"
        return f"site {node}"

    print(f"\nINTER-SITE FLOWS (train/long-belt candidates, by flow×distance):")
    print(f"  {'from':<20}{'to':<10}{'item':<24}{'rate/min':>10}{'km':>7}")
    for ln in lp.links[:top_links]:
        nm = gd.item_name.get(ln.item, ln.item)
        print(f"  {lbl(ln.src):<20}{lbl(ln.dst):<10}{nm:<24}"
              f"{ln.rate:>10.0f}{ln.dist/1e5:>7.1f}")
    if len(lp.links) > top_links:
        print(f"  ... and {len(lp.links)-top_links} smaller links")


def print_spatial(gd, sp, top_regions: int = 16, top_ships: int = 18) -> None:
    print("\n" + "=" * 60)
    print("  SPATIAL LAYOUT  (LP: where each recipe runs, hub-free)")
    print("=" * 60)
    if sp.status not in ("OPTIMAL", "FEASIBLE"):
        print(f"  no spatial solution: {sp.status}")
        return
    tc, hc = sp.transport_cost, sp.single_hub_cost
    print(f"\nTransport cost  (weight·item·km/min, lower=better):")
    print(f"  single central hub : {hc/1e5:>12,.0f}")
    print(f"  spatial layout     : {tc/1e5:>12,.0f}")
    if hc > 0:
        print(f"  => {100*(1-tc/hc):.1f}% less material movement, "
              f"across {len(sp.regions)} build regions")

    print(f"\nBUILD REGIONS ({len(sp.regions)} active; recipe machines per region):")
    print(f"  {'#':>3} {'dominant product':<26}{'mach':>7}{'MW':>9}"
          f"   {'location (m)':>18}")
    for rp in sp.regions[:top_regions]:
        nm = gd.item_name.get(rp.label, rp.label or "—")
        loc = f"({rp.pos[0]/100:>7.0f},{rp.pos[1]/100:>7.0f})"
        print(f"  {rp.id:>3} {nm:<26}{rp.machines:>7.1f}{rp.power_mw:>9.0f}   {loc:>18}")
    if len(sp.regions) > top_regions:
        rest = sp.regions[top_regions:]
        print(f"  ... and {len(rest)} smaller regions "
              f"({sum(r.machines for r in rest):.0f} machines total)")

    print(f"\nINTER-REGION SHIPMENTS (train/long-belt lines, by flow×distance):")
    print(f"  {'from':>5} ->{'to':>5}  {'item':<26}{'rate/min':>10}{'km':>7}")
    for s in sp.shipments[:top_ships]:
        nm = gd.item_name.get(s.item, s.item)
        print(f"  {s.src:>5} ->{s.dst:>5}  {nm:<26}{s.rate:>10.0f}{s.dist/1e5:>7.1f}")
    if len(sp.shipments) > top_ships:
        print(f"  ... and {len(sp.shipments)-top_ships} smaller shipments")


if __name__ == "__main__":
    raise SystemExit(main())
