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
    power: float                     # average MW for the machine on this recipe

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
