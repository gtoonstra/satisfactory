"""Load and normalize the current Satisfactory game data (data.json).

Source: the public interactive-map game-data dump (current Stable branch = the
live game version, 1.2). Refresh with `python fetch_data.py`. Schema notes:
  - items/recipes/buildings keyed by full UE paths; we use the short className
    (last dotted segment, e.g. Desc_IronIngot_C).
  - recipe amounts: solids are display units; FLUIDS/GASES are in mL, so we
    divide by 1000 to get m^3/craft (matches in-game per-minute rates).
  - machine power: fixed `powerUsed`, or per-recipe `powerUsedRecipes`
    [min,max] for variable-power machines (we use the average).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import cached_property

DATA_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data.json")

# Buildings that automate a recipe as part of a factory line (short classNames).
PRODUCTION_MACHINES = {
    "Build_SmelterMk1_C", "Build_FoundryMk1_C", "Build_ConstructorMk1_C",
    "Build_AssemblerMk1_C", "Build_ManufacturerMk1_C", "Build_OilRefinery_C",
    "Build_Packager_C", "Build_Blender_C", "Build_HadronCollider_C",
    "Build_Converter_C", "Build_QuantumEncoder_C",
}

FLUID_CATEGORIES = {"liquid", "gas"}

# Nuclear Power Plant (the only modelled generator). We synthesize one "burn"
# recipe per fuel so the reactor is a first-class node in the recipe graph:
# it consumes fuel rods, emits waste, and GENERATES power. This connects the
# uranium -> waste -> plutonium -> ficsonium chain end to end, so the optimizer
# can output plutonium/ficsonium and report the uranium extraction they need.
GEN_POWER_MW = 2500.0
NUCLEAR_PLANT = "Build_GeneratorNuclear_C"
NUCLEAR_FUELS = {                              # fuel rod -> waste (None = clean)
    "Desc_NuclearFuelRod_C": "Desc_NuclearWaste_C",
    "Desc_PlutoniumFuelRod_C": "Desc_PlutoniumWaste_C",
    "Desc_FicsoniumFuelRod_C": None,
}

# The map's raw extraction items.
RAW_RESOURCES = {
    "Desc_OreIron_C", "Desc_OreCopper_C", "Desc_Stone_C", "Desc_Coal_C",
    "Desc_OreGold_C", "Desc_RawQuartz_C", "Desc_Sulfur_C", "Desc_LiquidOil_C",
    "Desc_OreBauxite_C", "Desc_OreUranium_C", "Desc_NitrogenGas_C",
    "Desc_Water_C", "Desc_SAM_C",
}


def _short(path: str) -> str:
    return path.split(".")[-1] if isinstance(path, str) else path


@dataclass(frozen=True)
class Recipe:
    key: str
    name: str
    alternate: bool
    time: float                      # seconds per craft
    machine: str                     # short className of producing building
    inputs: dict[str, float]         # item className -> amount per craft
    outputs: dict[str, float]        # item className -> amount per craft
    power: float                     # average MW the machine DRAWS on this recipe
    gen_mw: float = 0.0              # MW this recipe GENERATES (nuclear reactors)

    def rate(self, item: str) -> float:
        """Net items/min of `item` for ONE machine running this recipe."""
        per_min = 60.0 / self.time
        return (self.outputs.get(item, 0.0) - self.inputs.get(item, 0.0)) * per_min

    @property
    def items(self) -> set[str]:
        return set(self.inputs) | set(self.outputs)


class GameData:
    def __init__(self, path: str = DATA_PATH):
        with open(path) as fh:
            self._raw = json.load(fh)
        self._build()

    def _build(self) -> None:
        raw = self._raw
        items = raw["itemsData"]
        self.item_name = {c: v["name"] for c, v in items.items()}
        self.sink_points = {c: v.get("resourceSinkPoints", 0) or 0 for c, v in items.items()}
        self.energy = {c: float(v.get("energy") or 0) for c, v in items.items()}     # MJ/item
        self.waste = {c: float(v.get("waste") or 0) for c, v in items.items()}       # waste/item
        self.radioactive = {c: float(v.get("radioactiveDecay") or 0) for c, v in items.items()}
        self.is_fluid = {c: v.get("category") in FLUID_CATEGORIES for c, v in items.items()}
        self.raw_resources = set(RAW_RESOURCES)

        bld = {_short(k): v for k, v in raw["buildingsData"].items()}
        self.machine_name = {m: bld.get(m, {}).get("name", m) for m in PRODUCTION_MACHINES}

        def power_for(recipe_key: str, machine: str) -> float:
            b = bld.get(machine, {})
            if b.get("powerUsed") is not None:
                return float(b["powerUsed"])
            pur = b.get("powerUsedRecipes") or {}
            if recipe_key in pur:
                lo, hi = pur[recipe_key]
                return (float(lo) + float(hi)) / 2.0
            return 0.0

        def conv(d) -> dict[str, float]:
            out = {}
            if not isinstance(d, dict):
                return out
            for fp, amt in d.items():
                c = _short(fp)
                a = float(amt)
                if self.is_fluid.get(c):
                    a /= 1000.0
                out[c] = out.get(c, 0.0) + a
            return out

        recipes: dict[str, Recipe] = {}
        for rk, v in raw["recipesData"].items():
            produced = [_short(m) for m in (v.get("mProducedIn") or [])]
            machine = next((m for m in produced if m in PRODUCTION_MACHINES), None)
            if machine is None:
                continue
            time = float(v.get("mManufactoringDuration") or 0)
            if time <= 0:
                continue
            outputs = conv(v.get("produce", {}))
            if not outputs:
                continue
            recipes[rk] = Recipe(
                key=rk, name=v["name"], alternate=("_Alternate_" in rk),
                time=time, machine=machine,
                inputs=conv(v.get("ingredients", {})), outputs=outputs,
                power=power_for(rk, machine),
            )

        # Synthesize one reactor "burn" recipe per fuel (1 plant = 1 machine).
        # Stored as per-minute amounts with time=60s, so rate() == amount/min.
        self.machine_name[NUCLEAR_PLANT] = bld.get(NUCLEAR_PLANT, {}).get(
            "name", "Nuclear Power Plant")
        self.generator_fuel: dict[str, str] = {}     # recipe key -> fuel className
        for fuel, waste in NUCLEAR_FUELS.items():
            energy = float(items.get(fuel, {}).get("energy") or 0)
            if energy <= 0:
                continue
            burn = GEN_POWER_MW / energy * 60.0       # rods/min per plant
            outs = {}
            if waste:
                outs[waste] = burn * float(items.get(fuel, {}).get("waste") or 0)
            key = f"Recipe_Burn_{fuel}"
            recipes[key] = Recipe(
                key=key, name=f"Burn {self.item_name.get(fuel, fuel)}",
                alternate=False, time=60.0, machine=NUCLEAR_PLANT,
                inputs={fuel: burn}, outputs=outs, power=0.0, gen_mw=GEN_POWER_MW,
            )
            self.generator_fuel[key] = fuel
        self.recipes = recipes

    @cached_property
    def _name_to_class(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for cls, name in self.item_name.items():
            out[name.lower()] = cls
            out[cls.lower()] = cls
        return out

    def resolve_item(self, text: str) -> str:
        t = text.strip()
        if t in self.item_name:
            return t
        key = t.lower()
        if key in self._name_to_class:
            return self._name_to_class[key]
        hits = [cls for cls, name in self.item_name.items() if key in name.lower()]
        if len(hits) == 1:
            return hits[0]
        if not hits:
            raise KeyError(f"Unknown item: {text!r}")
        raise KeyError(f"Ambiguous item {text!r}: " +
                       ", ".join(sorted(self.item_name[h] for h in hits)))

    def recipes_for(self, item: str) -> list[Recipe]:
        return [r for r in self.recipes.values() if item in r.outputs]

    @cached_property
    def item_depth(self) -> dict[str, int]:
        """Production depth ('tier') of each item: 0 for raw resources, else
        1 + the max depth of a recipe's inputs, minimised over the recipes that
        make it. Lets us label a factory by its most-processed ('top-level')
        output instead of its highest-volume one. Cyclic chains (packaged
        fluids, recycled plastic/rubber) are resolved by iterative relaxation."""
        INF = float("inf")
        depth: dict[str, float] = {it: 0.0 for it in self.raw_resources}
        for r in self.recipes.values():
            for it in list(r.inputs) + list(r.outputs):
                depth.setdefault(it, INF)
        for _ in range(64):
            changed = False
            for r in self.recipes.values():
                in_d = max((depth[i] for i in r.inputs), default=0.0)
                if in_d == INF:
                    continue
                cand = in_d + 1.0
                for o in r.outputs:
                    if cand < depth[o]:
                        depth[o] = cand
                        changed = True
            if not changed:
                break
        return {it: (int(d) if d != INF else 0) for it, d in depth.items()}

    def producible_items(self, extra_seed: set[str] | tuple = ()) -> set[str]:
        """Closure of items reachable from raw resources (plus extra_seed) by
        chaining whole recipes. extra_seed lets callers inject feedstocks that
        aren't recipe outputs — e.g. nuclear waste, which only power plants emit,
        and which unlocks the Plutonium/Ficsonium chain."""
        prod: set[str] = set(self.raw_resources) | set(extra_seed)
        changed = True
        while changed:
            changed = False
            for r in self.recipes.values():
                if all(i in prod for i in r.inputs):
                    for o in r.outputs:
                        if o not in prod:
                            prod.add(o)
                            changed = True
        return prod

    def terminal_products(self, *, producible_only: bool = True,
                          extra_seed: set[str] | tuple = ()) -> list[str]:
        """End-products: items some recipe outputs but no recipe consumes (and
        not raw). The map's 'top-level' shippable goods — fuel rods, end-game
        parts, ammo. Target the whole basket with the CLI token `all`.

        producible_only drops items unreachable from raw (e.g. FICSMAS/event
        goods) that would otherwise pin a ratio basket to zero output."""
        consumed: set[str] = set()
        produced: set[str] = set()
        for r in self.recipes.values():
            consumed |= set(r.inputs)
            produced |= set(r.outputs)
        term = produced - consumed - self.raw_resources
        if producible_only:
            term &= self.producible_items(extra_seed)
        return sorted(term)
