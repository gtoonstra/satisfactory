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

from .data import GameData
from .resources import map_limits

GEN_POWER_MW = 2500.0
# Nuclear Power Plant fuels (1.2): fuel className -> waste className (or None).
NUCLEAR_FUELS = [
    ("Desc_NuclearFuelRod_C", "Desc_NuclearWaste_C"),
    ("Desc_PlutoniumFuelRod_C", "Desc_PlutoniumWaste_C"),
    ("Desc_FicsoniumFuelRod_C", None),
]
# Reserve uranium for power: forbid non-power uranium sinks.
URANIUM_NONPOWER_RECIPES = {"Recipe_NobeliskNuke_C"}
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
    allow_waste: bool = False,
) -> Solution:
    excluded_recipes = set(excluded_recipes or set())
    if constrain_power:
        excluded_recipes |= URANIUM_NONPOWER_RECIPES
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

    # nuclear generators
    fuels = [(f, w) for f, w in NUCLEAR_FUELS if gd.energy.get(f, 0) > 0] if constrain_power else []
    G = {f: solver.NumVar(0.0, INF, f"G_{f}") for f, _ in fuels}
    burn = {f: GEN_POWER_MW / gd.energy[f] * 60.0 for f, _ in fuels}  # rods/min per plant
    for f, w in fuels:
        items.add(f)
        if w:
            items.add(w)

    E = {it: solver.NumVar(0.0, float(caps.get(it, 0.0)), f"E_{it}")
         for it in items if it in gd.raw_resources}

    T = solver.NumVar(0.0, INF, "T")
    target_items = {t.item: t.weight for t in targets}

    for it in items:
        terms = [x[r.key] * r.rate(it) for r in recipes if it in r.items]
        expr = solver.Sum(terms) if terms else solver.Sum([])
        if it in E:
            expr = expr + E[it]
        for f, w in fuels:                       # nuclear contributions
            if it == f:
                expr = expr - burn[f] * G[f]
            if w and it == w:
                expr = expr + burn[f] * gd.waste[f] * G[f]
        if it in target_items and mode == "ratio":
            solver.Add(expr - target_items[it] * T == 0)
        elif constrain_power and it in closed:
            if it == PLUTONIUM_WASTE and allow_waste:
                solver.Add(expr >= 0)        # may accumulate (no Ficsonium)
            else:
                solver.Add(expr == 0)        # must be consumed (e.g. Uranium Waste)
        else:
            solver.Add(expr >= 0)

    # power balance
    if constrain_power:
        consumed = solver.Sum([x[r.key] * r.power for r in recipes])
        generated = solver.Sum([G[f] * GEN_POWER_MW for f, _ in fuels])
        solver.Add(consumed - generated <= 0)

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

    extraction = {it: E[it].solution_value() for it in E if E[it].solution_value() > EPS}
    total_power = sum(rmap[k].power * c for k, c in used.items())
    gens = {f: G[f].solution_value() for f, _ in fuels if G[f].solution_value() > EPS}
    fuel_burn = {f: burn[f] * G[f].solution_value() for f in gens}
    generated = sum(c * GEN_POWER_MW for c in gens.values())

    # net radioactive waste left over (0 in the closed-loop default)
    waste_classes = {w for _, w in NUCLEAR_FUELS if w}
    waste_per_min = {}
    for w in waste_classes:
        net = sum(rmap[k].rate(w) * c for k, c in used.items())
        for f, wf in fuels:
            if wf == w:
                net += burn[f] * gd.waste[f] * G[f].solution_value()
        if net > EPS:
            waste_per_min[w] = net

    return Solution(
        status=status_name, scale=T.solution_value(), outputs=outputs,
        recipes=used, extraction=extraction, total_power_mw=total_power,
        power_generated_mw=generated, generators=gens, fuel_burn=fuel_burn,
        waste_per_min=waste_per_min,
    )
