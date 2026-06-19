# Satisfactory production optimizer

A constraint solver that, given a target set of end items, computes the
resource allocation and recipe mix (including **alternate recipes**) that
**maximizes a balanced/weighted basket of outputs** within the map's raw
resource limits — then suggests **where to build** (smelt at the source, ship a
compact set of refined goods to a central hub).

It is a **linear program** solved with Google OR-Tools (GLOP):

- variable per recipe = number of machines (fractional allowed),
- variable per raw resource = extraction rate, capped by whole-map node totals,
- variable per nuclear fuel = number of Nuclear Power Plants,
- every item: production ≥ consumption (byproducts may be surplus),
- machine power draw ≤ nuclear power generated,
- target items pinned to your requested ratio; maximize the basket scale.

## Data

`data.json` is the current game data (items, recipes, buildings, power) for the
live **Stable branch = game version 1.2**, pulled from the public
interactive-map data feed. Refresh with:

```
python fetch_data.py     # -> data.json  (178 items, ~290 automatable recipes)
python fetch_nodes.py    # -> nodes.json (577 resource nodes + well satellites)
```

## Install

```
pip install -r requirements.txt   # ortools
```

## Use (CLI)

```bash
# Balanced 1:1 basket of two end items, whole-map resources
python -m satisfactory_opt "Turbo Motor" "Supercomputer"

# Weighted 3:1 ratio, standard recipes only
python -m satisfactory_opt "Turbo Motor=3" "Supercomputer=1" --no-alternates

# Single item, maximized
python -m satisfactory_opt "Reinforced Iron Plate"

# Maximize total weighted output instead of locking the ratio
python -m satisfactory_opt "Plastic=1" "Rubber=1" --mode sum

# Custom raw-input budget instead of whole-map maxima
python -m satisfactory_opt "Modular Frame" --resources my_budget.json

# Add the placement strategy (smelt at source -> hub) + geo node assignment
python -m satisfactory_opt "Heavy Modular Frame" --placement --nodes nodes.json
```

Flags: `--no-alternates`, `--exclude "Recipe Name" ...`, `--miner-clock 0.4`
(scale default limits), `--mode {ratio,sum}`, `--no-power`, `--allow-waste`,
`--json`.

## Power (nuclear, endogenous, waste-free by default)

Power is a hard constraint: total machine draw must be covered by power the
factory **generates**, and generation competes for resources like everything
else. The only power source is the **nuclear chain**:

- Nuclear Power Plants (2500 MW) burn Uranium / Plutonium / Ficsonium fuel rods.
- **Uranium (2100/min) is reserved entirely for power** — the only uranium sink
  is the fuel-rod chain (Nuke Nobelisk is excluded).
**Uranium Waste is never allowed to accumulate** — it is always recycled into
plutonium (Uranium Waste → Non-Fissile Uranium → Plutonium rods), in every mode.
The two waste policies differ only in what happens to Plutonium Waste:

- **Default (closed loop, zero waste):** Plutonium Waste is also fully consumed —
  Plutonium rods → Plutonium Waste → **Ficsonium rods (no waste)** terminate the
  chain cleanly. Net power-positive, and it runs **more Ficsonium than Plutonium
  by throughput** (≈5:1 rods/min) even with *fewer* Ficsonium plants (those rods
  burn 10× faster).
- **`--allow-waste`:** skips Ficsonium and lets Plutonium Waste accumulate
  (reported per-minute, must be stored). Uranium Waste is still recycled.

Cleanliness costs ~12% output — e.g. Reinforced Iron Plate: 95.5k/min clean vs
109k/min with `--allow-waste` (which leaves ~264 Plutonium Waste/min).

`--no-power` ignores power entirely. The report shows machine draw, MW generated,
plant counts + rods/min per fuel, and any waste accumulation.

## Use (library)

```python
from satisfactory_opt import GameData, Target, solve
gd = GameData()
sol = solve(gd, [Target(gd.resolve_item("Turbo Motor"), 1.0),
                 Target(gd.resolve_item("Supercomputer"), 1.0)])
print(sol.scale, sol.outputs, sol.recipes)
```

## Placement / logistics

`--placement` (no coordinates needed): splits the solved recipes into a
**SOURCE stage** (ore→ingot smelting/foundry, built at the mines) and a **HUB
stage** (assembly/manufacturing, one central factory), and reports the belt
throughput crossing mine→hub before vs after — i.e. the transport-volume saving
from refining at the source. Each resource type is reduced near its node, and a
compact set of intermediates converges on one hub.

`--nodes nodes.json` adds **geographic** placement using **real map
coordinates**: it greedily picks physical nodes (best purity first) to meet the
required extraction, clusters them into mining sites, places a throughput-
weighted central hub, and scores transport effort = Σ(rate × distance).

`nodes.json` (577 real nodes + well satellites) is produced by:

```
python fetch_nodes.py
```

which pulls the public interactive-map data
(`static.satisfactory-calculator.com/data/json/mapData/en-Stable.json`) and
emits the schema (one object per node, coordinates in game units = cm):

```json
[{"resource": "Desc_OreIron_C", "purity": "pure", "x": -80000, "y": 20000, "z": 100, "kind": "node"}]
```

`purity` ∈ {impure, normal, pure}; `kind` ∈ {node, well}.

## Resource limits

`resources.py` caps are **derived from the real 577-node dataset** (Mk.3 miners
@ 250%, oil extractors @ 250%, well pressurizers @ 250%) and reproduce the
community reference totals exactly (Iron 92 100, Coal 42 300, Nitrogen 12 000,
Crude Oil 12 600, …). Water is treated as unlimited (extractors place on any
water surface). `derive_caps_from_nodes()` regenerates them from a newer map;
override per run with `--resources` or scale with `--miner-clock`.

## Caveats

- Power models the **nuclear chain only** (your setup). Coal/fuel/geothermal
  generators are intentionally excluded, so uranium is the hard power ceiling.
- Variable-power machines (Particle Accelerator, Quantum Encoder) use the
  average of their min/max draw.
- LP gives fractional machines; round up per line for a buildable factory.
- Geographic placement is a heuristic (best-purity selection + weighted-centroid
  hub), not a global transport optimum — good for siting guidance.
