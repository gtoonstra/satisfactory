"""Download real resource-node coordinates from the public SCIM map data and
emit nodes.json in the schema the optimizer's placement layer expects.

Source: satisfactory-calculator.com interactive map
  https://static.satisfactory-calculator.com/data/json/mapData/en-Stable.json
Coordinates are raw game units (cm). Run:  python fetch_nodes.py
"""
from __future__ import annotations

import json
import urllib.request

URL = "https://static.satisfactory-calculator.com/data/json/mapData/en-Stable.json"
PURITY = {"RP_Inpure": "impure", "RP_Normal": "normal", "RP_Pure": "pure"}


def _markers(o):
    out = []
    if isinstance(o, dict):
        if isinstance(o.get("markers"), list):
            out += o["markers"]
        for v in o.values():
            if isinstance(v, (dict, list)):
                out += _markers(v)
    elif isinstance(o, list):
        for v in o:
            out += _markers(v)
    return out


def build(raw: dict) -> list[dict]:
    tabs = {t.get("tabId"): t for t in raw["options"]}
    nodes: list[dict] = []
    for tab_id, kind in (("resource_nodes", "node"), ("resource_wells", "well")):
        tab = tabs.get(tab_id)
        if not tab:
            continue
        for m in _markers(tab):
            t = m.get("type")
            if not t or m.get("purity") not in PURITY:
                continue                       # skip geysers / untyped markers
            nodes.append({
                "resource": t,
                "purity": PURITY[m["purity"]],
                "x": m["x"], "y": m["y"], "z": m["z"],
                "kind": kind,
                "core": m.get("core"),         # wells share a pressurizer core
            })
    return nodes


def main() -> None:
    req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
    raw = json.load(urllib.request.urlopen(req, timeout=60))
    nodes = build(raw)
    with open("nodes.json", "w") as fh:
        json.dump(nodes, fh)
    from collections import Counter
    print(f"wrote nodes.json: {len(nodes)} nodes "
          f"(map version {raw.get('version')})")
    by = Counter(n["resource"] for n in nodes)
    for k, v in by.most_common():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
