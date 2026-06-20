# Satisfactory production optimizer

## The problem

Early game, placement is trivial: a node is right there, so you drop a smelter on
top of it and a constructor next to that. It works because everything you need is
local.

As the game advances that breaks down. The parts you want take a dozen
ingredients pulled from resources scattered across the whole map, and "just put
it next to the node" stops being an option — there is no single node, there are
twenty, in different biomes. So you start patching: a train here, a drone station
there, a belt running halfway across the world because *that one* resource only
shows up in the desert. The result is **railroad spaghetti and drone stations
everywhere** — a logistics network that grew by accident, that you are forever
rerouting, and that you eventually tear down and rebuild because it was never
laid out on purpose.

## The objective

I wanted the opposite: to **build the world as stable as possible**, so it needs
as few changes as possible. Lay the map out *once*, correctly, and then spend my
time on the part that actually matters — the **beauty of the build** — without
the dread of knowing I'll have to rip it out later to make room for the thing I
didn't plan for.

The key realization is that Satisfactory makes this tractable in a way most
factory games don't: **resources are infinite and always in the same place.**
Iron node totals never run out and never move. That means the optimal *general
shape* of your factory — which regions make what, and what flows between them —
is a fixed, knowable thing. You can compute it ahead of time. So instead of
discovering the layout by playing into a corner, you can solve for it: a resolver
that figures out the **general areas** to build in and **what each area should
produce**, once, for your full end-state — and then you build incrementally
*toward* that plan, knowing every line you lay is part of the final picture and
will never need to move.

So this solver optimizes for exactly that. The rest of this README is the whole
story of how it does it.

## How it works (and why "good" is hard)

The hard part isn't solving an optimization — it's **defining what a good layout
even is.** "Minimize transport" alone collapses everything into one giant blob
(zero distance if it's all in one place). "Spread it out" gives you spaghetti
again. A good plan is a *balance*, and most of the engineering here is in
encoding that balance so a solver can find it. It runs in two stages.

**Stage 1 — *what* to build (`solver.py`).** Given your target end-items, a
linear program (Google OR-Tools / GLOP) picks the recipe mix — including
**alternate recipes** — that maximizes a balanced/weighted basket of outputs
within the map's real raw-resource limits:

- a variable per recipe = number of machines (fractional allowed),
- a variable per raw resource = extraction rate, capped by whole-map node totals,
- every item: production ≥ consumption (byproducts may run surplus),
- target items pinned to your requested ratio; maximize the basket scale `T`.
- **Power is endogenous and part of the same LP.** Nuclear reactors are
  first-class recipes that consume fuel rods, emit waste, and generate MW, so the
  whole `uranium → fuel rod → waste → plutonium → ficsonium` cascade is solved
  together and machine draw must stay under generated MW (with a margin). Uranium
  extraction is therefore the hard power ceiling. See [Power](#power-nuclear-endogenous-waste-free-by-default).

This stage answers *how many machines of each recipe*, but says nothing about
*where*. It's a global optimum, so it's deterministic — same targets, same answer.

**Stage 2 — *where* to build it (`spatial_layout.py`).** This is where "good"
gets subtle. A second LP keeps Stage 1's recipe totals fixed and decides how many
machines of each recipe go in each **region**, plus what ships between regions. It
minimizes:

```
transport  =  Σ  flow[item, a→b] · weight[item] · distance(a, b)
congestion =  λ · Σ_region  convex(machines_in_region)
```

- **Regions** are *basins*: real mining sites clustered by proximity
  (`--region-radius`). Each region knows the raw resources it can extract locally.
- **The transport term** is what keeps bulk local. `flow` is already in items/min,
  so it carries the volume difference for free — ore moves at ~92 000/min,
  computers at ~10/min — and `weight` adds the *medium* difference (fluids = 2,
  since pipes carry half a belt's throughput). The upshot: smelting pins itself
  next to the ore because moving ore is enormously expensive, while compact,
  high-tier parts are cheap enough to travel, so they're the only things that ride
  the long lines. **There is no central hub term** — final outputs have no
  downstream demand, so nothing is forced to converge; sub-factories simply settle
  next to whatever they trade material with.
- **The congestion term** is what prevents the blob. It's a *convex* penalty on
  machines per region: each region builds `--free-capacity` machines for free,
  then every additional machine costs progressively more. Past a point it's
  cheaper to spill into a neighbouring basin than to keep packing one region, so
  no single area swallows the whole factory. Turning `--congestion` up gives you
  more, smaller, specialized clusters (more transport); turning it to `0` gives
  the pure transport minimum (a few mega-clusters). **This single knob is the
  transport-vs-sprawl tradeoff** — a solve-time CLI flag that changes the actual
  layout (not the Transport-layer density slider, which only re-clusters the
  already-solved plan for display).

Because it's one LP with one global optimum, the layout is **deterministic and
therefore stable** — exactly the property the objective demanded. Solve your full
end-state basket once; early builds are a subset of the final map and never need
relocating. Typical results are ~90–96 % less material movement than shipping
everything to a single hub.

What it deliberately does **not** optimize: the aesthetics of an individual
factory, exact machine-by-machine placement, or belt routing within a region.
Those are the parts you *want* to build by hand — the solver's job is to hand you
stable regions and a stable logistics skeleton so that hand-built beauty never has
to be torn down.

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

The headline command — lay out a factory that builds **the entire end-game
spread** across the map, with full geographic placement and the interactive
visualizer:

```bash
# Whole-map, waste-free, spatial layout + visualizer (the "build the world" run)
python -m satisfactory_opt all --basket points --nodes nodes.json --spatial --viz out/
```

That solves with power **on** and the **closed nuclear loop (zero waste)** by
default. Drop the uranium chain entirely while you're still on coal/fuel with
`--no-power`; allow Plutonium Waste to accumulate (≈12 % more output) with
`--allow-waste`. See [Power](#power-nuclear-endogenous-waste-free-by-default) for
what those two switches actually change.

Smaller, targeted runs:

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
server, no CDN — just open `DIR/index.html` in a browser) with a **Production
flow** graph plus a geographic **Resource map** that splits into three layers:

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
- **Resource map** — the real Satisfactory map, with three switchable layers
  (top-left toggle) that **decouple production from logistics** so each stays
  readable:
  - **Factories** — production sites sized by machine count, **named by the
    biome they sit in** (`biomes.py`). Biome regions come from the wiki's
    authoritative biome map (which shares this project's world extent and
    orientation exactly), encoded as seed points; a factory takes the biome of
    its nearest seed. A biome with one factory just takes the biome name (*Dune
    Desert*); where a biome has several they're numbered by size (*Rocky Desert
    I*, *Rocky Desert II*, …). Click one
    to trace its **sourcing**: green arrows back to where each input is actually
    produced or mined (following pass-through hops), orange to where its outputs
    go. The inspector lists each imported item with its **provenance** (`← which
    factory/⛏ mine`). Edge labels are filtered to *only* the selected factory's
    resources, so they stay legible.
  - **Mining** — on-site raw extraction per region (ore-coloured, sized by
    ore/min); the legend isolates a resource across the whole map.
  - **Transport** — the **logistics network**: nearby production regions are
    grid-clustered into **logistics hubs** (intra-hub flows collapse to local
    belts), and the surviving inter-hub trains are aggregated into lines sized by
    items/min. Relay hubs that mostly route cargo through are ringed. A
    **density slider** (top-centre) sets the grouping grid live — drag it to
    collapse more stations into fewer depots or keep basins separate. The default
    grid targets ~12 hubs for the map's spread; the readout shows
    `hubs · lines · grid size`.

The views are **linked**: from a raw resource's inspector, "show source nodes on
map" jumps to the Mining layer and highlights exactly the sites feeding it.

### Two overlay grids: the build plan and the transport plan

The result is really **two grids laid over the same map**, and you read them for
different things:

1. **The build plan (Factories layer)** — *where* each thing is made. This is the
   layer for reasoning about **sourcing**: click a factory and it traces every
   input back to where it is actually produced or mined, even if that's the far
   side of the map. Material effectively flows *up* this grid — raw ore and
   intermediates climb through the network toward the factory that finally
   consumes them.
2. **The transport plan (Transport layer)** — *how* to move it. Nearby regions
   collapse into a number of **logistics hubs you choose** (the density slider),
   and the surviving long links are your **trains**. Within a hub's territory you
   run local belts from the train station out to the factories it serves and load
   whatever that area ships onward. Cargo is **transitive** through hubs — a relay
   hub just passes material through — so it is still traceable to its origin here,
   but the Factories layer makes that tracing easier.

Why this matters when you actually build it:

- Every link carries a known rate in **items/min**, so each station's throughput
  is known too. You can size **exactly how many cargo platforms and parallel
  trains** a route needs up front — and never rip them out later because you
  under-built. Where a link's volume doesn't justify rail, the rate tells you
  whether **drones or trucks** fit instead.
- The hub count is yours (slider): a few large depots, or many small ones.
- **Placements are guidance, not gospel.** The solver gives you the right
  *region* and the right *flows*; the exact tile is yours to nudge. The Titan
  Forest, for one, is notoriously awkward to build in — so slide that hub onto
  friendlier ground nearby and plan around it. The plan stays valid because what
  matters is *which region trades what*, not the centimetre.

The exporter writes `index.html` (all data embedded inline — nothing is fetched
at runtime), `solution.json`, and — for the map view — a copy of `map_bg.jpg`. To
**view or post** the result you only need **`index.html` + `map_bg.jpg`** kept
side by side; `solution.json` is just for inspection and can be dropped. The map backdrop lives in
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
