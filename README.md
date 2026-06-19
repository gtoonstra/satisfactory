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
python fetch_map.py      # -> satisfactory_opt/assets/map_bg.jpg (visualizer backdrop)
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

## Visualize

`--viz DIR` writes a **self-contained interactive HTML page** (no build step, no
server, no CDN — just open `DIR/index.html` in a browser) with two linked views:

```bash
# Production-flow graph only
python -m satisfactory_opt "Heavy Modular Frame" --viz out/

# Add the geographic resource map (needs node coordinates)
python -m satisfactory_opt "Heavy Modular Frame" --viz out/ --nodes nodes.json
```

- **Production flow** — a left-to-right layered graph: raw resources flow through
  recipe/building nodes (colored by machine type, labelled with machine count) to
  the shipped target on the right. Edges are belt/pipe flows sized by items/min.
  Hover a node to trace its flows; click any node for an inspector listing its
  inputs/outputs (or, for an item, what produces and consumes it).
- **Resource map** — the real Satisfactory map with every resource node at its
  true coordinates, the chosen mining sites and central hub overlaid, and belts
  drawn hub↔site weighted by throughput. Pan/zoom, hover for details, click a
  legend entry to isolate a resource.

The views are **linked**: from a raw resource's inspector, "show source nodes on
map" jumps to the map and highlights exactly the sites feeding it.

The exporter writes `index.html` (data embedded inline), `solution.json`, and —
for the map view — a copy of `map_bg.jpg`. The map backdrop lives in
`satisfactory_opt/assets/`; refresh it with `python fetch_map.py`. The
node→pixel calibration uses the public SCIM world bounds
(`west=-324698.83, east=425301.83, north=-375000, south=375000`).

## Power (nuclear, endogenous, waste-free by default)

Power is a hard constraint, modelled like a real grid: generation must exceed
total machine draw by a **margin** (`--power-margin`, default **1.2 = 20 % over
capacity**). Generation competes for resources like everything else, and the only
power source is the **nuclear chain**:

- Nuclear Power Plants (2500 MW) burn Uranium / Plutonium / Ficsonium fuel rods.
  Each reactor is a **first-class recipe** (consumes fuel rods, emits waste,
  generates MW), so the whole `uranium → fuel rod → waste → plutonium →
  ficsonium` cascade is connected: the optimizer sizes every reactor tier and
  reports the **uranium ore** (and thus uranium nodes) the chain needs — no
  manual side-calculation. Plutonium/Ficsonium are therefore **power-chain
  intermediates**, reported under POWER (plant counts + rods/min), not shippable
  end-products.
- Uranium's only sink is that fuel-rod chain (plus Nuke Nobelisk if you target
  it), so the 2100/min uranium cap is the hard power ceiling.
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

## Spatial layout (`--spatial`) — *where* each recipe runs, hub-free

`--placement`/`--nodes` above ship everything to **one** central hub, so the
highest-volume intermediates ride long belts inward and transport cost explodes.
`--spatial` solves the opposite problem: a second linear program that **keeps the
solved recipe totals** and decides *where* each recipe's machines go across the
map, so that material movement is minimized with **no central destination**.

```bash
python -m satisfactory_opt "Heavy Modular Frame" --no-power --nodes nodes.json --spatial
```

- Mining sites are clustered into **basins** (`--region-radius`, game units).
- It places `x[recipe, region]` machines and `f[item, region→region]` shipments
  to **minimize `Σ flow × weight × distance`**. Flow rate already encodes
  "volume" (ore moves at ~92 000/min, computers at ~10/min), and `weight` adds
  the medium difference (fluids = 2, needing pipes at half a belt's throughput),
  so bulk stays local and only compact high-tier parts travel far.
- **No hub term:** outputs have no downstream demand, so sub-factories settle
  next to what they exchange material with; the surviving long links are your
  train lines (reported as inter-region shipments).
- `--congestion C --free-capacity N`: a **convex cluster-size penalty** — a
  region hosts `N` machines free, then each extra machine costs progressively
  more, so no single basin swallows the whole factory. Higher `C` → more, smaller
  clusters (more transport); `0` → pure transport minimum (a few mega-clusters).

It is **deterministic and global** (one LP optimum), so the layout is **stable**:
solve your full end-state basket once and build incrementally toward it — early
lines are a subset of the final map and never need relocating. Typical results:
~90–96 % less material movement than a single hub. Use `--no-power` while you are
still on coal/fuel power and haven't committed to the uranium chain.

The report lists each build region (dominant product, machine count, power,
location) and the inter-region shipments ranked by `flow × distance` (your
train/long-belt candidates).

### Whole-map basket: `all`

Instead of one target, pass the token **`all`** to target every *terminal
end-product* (items nothing else consumes — end-game parts, fuel rods, ammo),
laying out a factory that builds the entire spread at once:

```bash
python -m satisfactory_opt all --no-power --nodes nodes.json --spatial --viz out/
```

`all` keeps only products reachable from raw resources (FICSMAS/event goods are
dropped, since an unmakeable item would pin a ratio basket to zero). `--basket`
sets how the spread is weighted:

- `equal` (default) — same items/min of each, so the build is dominated by the
  costliest items (warp drives, AI servers) and ammo is a tiny corner;
- `points` — ∝ AWESOME sink value; `inv-points` — equal points/min each
  (ammo-heavy); `inv-sqrt` — a middle ground (every product a similar-sized site).

Note: **Plutonium / Ficsonium fuel rods** are power-chain intermediates (burned
in reactors), so they are not in the shippable `all` basket — but with power on
they are produced and reported under POWER, and the spatial layout sites their
reactors. `all` runs with or without power; with power the uranium cap (2100/min)
makes the whole-everything basket genuinely tight.

### Map view (`--viz`): Factories vs Mining

With `--nodes`, the visualizer's **Resource map** gains a *Factories / Mining*
toggle. **Factories** draws the spatial layout: each build region as a marker
sized by machine count, train/long-belt lines between regions weighted by flow
(fluids in pipe-blue), and the transport saving vs a single hub. **Click any hub**
for a sizing breakdown — machine counts by building type, the inflow it must
bring in, and what it produces (no intermediate-recipe noise) — so you can size
each site. **Mining** is the original per-resource node/hub view. A simpler diagnostic, `--proximity`, runs a
geometric-median (Weiszfeld) relaxation instead; it is kept mainly to show *why*
a single-instance relaxation collapses to one blob — `--spatial` is the one that
actually spreads the factory out.

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
