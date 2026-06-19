"""Linear-program optimizer for Satisfactory production, with endogenous
nuclear power.

Model
-----
Decision variables (all >= 0, continuous):
  x[r]   number of machines running recipe r
  E[i]   raw extraction of resource i (items/min), bounded by map limits
  G[f]   number of Nuclear Power Plants burning fuel rod f
  T      common throughput scale for the target basket

Item balance, for every item i:  production - consumption >= 0
  (raw items add E[i]; nuclear plants consume fuel rods and emit waste).
Target items t are pinned to the ratio:  net(t) == w_t * T.

Power (endogenous, nuclear only):
  sum_r x[r] * power(r)   <=   sum_f G[f] * 2500 MW
Each plant burns  2500/energy(f)*60  rods/min and emits  that * waste(f)  waste/min.
Uranium is reserved for power: the only uranium sink is the fuel-rod chain
(Nuke Nobelisk is excluded). The waste chain (Uranium->Plutonium->Ficsonium)
is available, so the LP recycles waste into more power when it is net-positive.

  maximize T
"""
from __future__ import annotations

from dataclasses import dataclass

from ortools.linear_solver import pywraplp

from .data import GameData, NUCLEAR_FUELS
from .resources import map_limits

# The uranium source itself is extraction-capped, not a closed-loop intermediate.
URANIUM_SOURCE = "Desc_OreUranium_C"
# Plutonium Waste is the only radioactive item --allow-waste may leave to
# accumulate (by skipping Ficsonium). Everything else -- crucially Uranium
# Waste -- must ALWAYS be consumed down the chain (Uranium Waste -> Plutonium).
PLUTONIUM_WASTE = "Desc_PlutoniumWaste_C"


@dataclass
class Target:
    item: str
    weight: float


@dataclass
class Solution:
    status: str
    scale: float
    outputs: dict[str, float]
    recipes: dict[str, float]
    extraction: dict[str, float]
    total_power_mw: float                 # machine draw
    power_generated_mw: float
    generators: dict[str, float]          # fuel className -> plant count
    fuel_burn: dict[str, float]           # fuel className -> rods/min
    waste_per_min: dict[str, float]       # radioactive waste className -> net items/min


def solve(
    gd: GameData,
    targets: list[Target],
    *,
    allow_alternates: bool = True,
    excluded_recipes: set[str] | None = None,
    resource_caps: dict[str, float] | None = None,
    miner_clock: float = 1.0,
    mode: str = "ratio",
    constrain_power: bool = True,
    power_margin: float = 1.2,
    allow_waste: bool = False,
) -> Solution:
    excluded_recipes = set(excluded_recipes or set())
    # Closed nuclear loop: every radioactive item (fuel rods, cells, pellets,
    # Uranium Waste) must be fully consumed -- the subsystem's only output is
    # electricity. Excludes the uranium-ore source and any explicit target.
    # With allow_waste, only Plutonium Waste may accumulate (Ficsonium skipped).
    target_set = {t.item for t in targets}
    closed = {c for c, d in gd.radioactive.items()
              if d > 0 and c != URANIUM_SOURCE and c not in target_set}
    caps = resource_caps if resource_caps is not None else map_limits(miner_clock)

    recipes = [
        r for r in gd.recipes.values()
        if r.key not in excluded_recipes and (allow_alternates or not r.alternate)
    ]

    solver = pywraplp.Solver.CreateSolver("GLOP")
    if solver is None:
        raise RuntimeError("Could not create GLOP solver")
    INF = solver.infinity()

    x = {r.key: solver.NumVar(0.0, INF, f"x_{r.key}") for r in recipes}

    items: set[str] = set()
    for r in recipes:
        items |= r.items
    items |= {t.item for t in targets}

    E = {it: solver.NumVar(0.0, float(caps.get(it, 0.0)), f"E_{it}")
         for it in items if it in gd.raw_resources}

    T = solver.NumVar(0.0, INF, "T")
    target_items = {t.item: t.weight for t in targets}

    for it in items:
        terms = [x[r.key] * r.rate(it) for r in recipes if it in r.items]
        expr = solver.Sum(terms) if terms else solver.Sum([])
        if it in E:
            expr = expr + E[it]
        if it in target_items and mode == "ratio":
            solver.Add(expr - target_items[it] * T == 0)
        elif constrain_power and it in closed:
            if it == PLUTONIUM_WASTE and allow_waste:
                solver.Add(expr >= 0)        # may accumulate (no Ficsonium)
            else:
                solver.Add(expr == 0)        # must be consumed (e.g. Uranium Waste)
        else:
            solver.Add(expr >= 0)

    # power balance: like a real grid, generation must exceed draw by a margin
    # (default 20% headroom). Nuclear reactors are recipes, so this just relates
    # their generated MW to every machine's drawn MW.
    if constrain_power:
        draw = solver.Sum([x[r.key] * r.power for r in recipes])
        gen = solver.Sum([x[r.key] * r.gen_mw for r in recipes])
        solver.Add(gen - power_margin * draw >= 0)

    if mode == "ratio":
        solver.Maximize(T)
    elif mode == "sum":
        obj = solver.Sum([
            target_items[it] * (
                solver.Sum([x[r.key] * r.rate(it) for r in recipes if it in r.items])
                + (E[it] if it in E else 0))
            for it in target_items])
        solver.Maximize(obj)
    else:
        raise ValueError(f"unknown mode {mode!r}")

    status = solver.Solve()
    status_name = {
        pywraplp.Solver.OPTIMAL: "OPTIMAL", pywraplp.Solver.FEASIBLE: "FEASIBLE",
        pywraplp.Solver.INFEASIBLE: "INFEASIBLE", pywraplp.Solver.UNBOUNDED: "UNBOUNDED",
        pywraplp.Solver.ABNORMAL: "ABNORMAL", pywraplp.Solver.NOT_SOLVED: "NOT_SOLVED",
    }.get(status, str(status))

    if status not in (pywraplp.Solver.OPTIMAL, pywraplp.Solver.FEASIBLE):
        return Solution(status_name, 0.0, {}, {}, {}, 0.0, 0.0, {}, {}, {})

    EPS = 1e-7
    used = {k: v.solution_value() for k, v in x.items() if v.solution_value() > EPS}
    rmap = {r.key: r for r in recipes}

    outputs: dict[str, float] = {}
    for it in target_items:
        net = sum(rmap[k].rate(it) * c for k, c in used.items())
        net += E[it].solution_value() if it in E else 0.0
        if net > EPS:
            outputs[it] = net

    # Report ACTUAL extraction need = net the recipes consume, not E's solution
    # value: nothing in the objective pushes E down, so GLOP parks it at the cap
    # (e.g. iron would read 92 100 when only ~32 000 is used). The true need is
    # the recipes' net consumption of each raw item.
    extraction = {}
    for it in E:
        need = -sum(rmap[k].rate(it) * c for k, c in used.items())  # consumed > 0
        if need > EPS:
            extraction[it] = need
    total_power = sum(rmap[k].power * c for k, c in used.items())
    generated = sum(rmap[k].gen_mw * c for k, c in used.items())

    # reactors are recipes now: derive plant counts + fuel burn from their use.
    gens: dict[str, float] = {}
    fuel_burn: dict[str, float] = {}
    for k, c in used.items():
        fuel = gd.generator_fuel.get(k)
        if fuel is None:
            continue
        gens[fuel] = gens.get(fuel, 0.0) + c                 # 1 machine = 1 plant
        fuel_burn[fuel] = fuel_burn.get(fuel, 0.0) - rmap[k].rate(fuel) * c

    # net radioactive waste left over (0 in the closed-loop default)
    waste_classes = {w for w in NUCLEAR_FUELS.values() if w}
    waste_per_min = {}
    for w in waste_classes:
        net = sum(rmap[k].rate(w) * c for k, c in used.items())
        if net > EPS:
            waste_per_min[w] = net

    return Solution(
        status=status_name, scale=T.solution_value(), outputs=outputs,
        recipes=used, extraction=extraction, total_power_mw=total_power,
        power_generated_mw=generated, generators=gens, fuel_burn=fuel_burn,
        waste_per_min=waste_per_min,
    )
