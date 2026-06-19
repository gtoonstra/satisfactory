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
                   help="end items, optionally 'Name=weight' for ratios")
    p.add_argument("--no-alternates", action="store_true",
                   help="use only standard (non-alternate) recipes")
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
    p.add_argument("--allow-waste", action="store_true",
                   help="permit PLUTONIUM Waste to accumulate, skipping Ficsonium "
                        "(higher output). Uranium Waste is always recycled to "
                        "plutonium. Default is a fully closed, waste-free loop.")
    p.add_argument("--placement", action="store_true",
                   help="also show the mine-vs-hub staging plan (smelt at source)")
    p.add_argument("--nodes", metavar="JSON",
                   help="node coords file {resource,purity,x,y,z}[]; enables geo placement")
    p.add_argument("--json", action="store_true", help="emit JSON instead of a report")
    p.add_argument("--data", default=None, help="path to data.json")
    args = p.parse_args(argv)

    gd = GameData(args.data) if args.data else GameData()

    try:
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
        allow_waste=args.allow_waste,
    )

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
        if args.nodes:
            with open(args.nodes) as fh:
                nodes = json.load(fh)
            for nd in nodes:
                # node files normally use classNames already; resolve names if not
                if not nd["resource"].startswith("Desc_"):
                    try:
                        nd["resource"] = gd.resolve_item(nd["resource"])
                    except KeyError:
                        pass
            place = assign_and_place(gd, sol, nodes)
            print_placement(gd, place)

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
        print(f"  Uranium ore -> power: {ur:,.0f}/min (reserved for fuel rods)")
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


if __name__ == "__main__":
    raise SystemExit(main())
