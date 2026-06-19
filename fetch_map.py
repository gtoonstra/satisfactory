"""Download the realistic-map backdrop used by the HTML visualizer.

Source: the same public SCIM interactive-map assets the node data comes from
  https://static.satisfactory-calculator.com/js/InteractiveMap/img/backgroundGame_2048.jpg
This is a 2048x2048 render of the in-game map. The visualizer overlays nodes
on it using the SCIM world-coordinate calibration (game units -> pixels):
  west=-324698.832031  east=425301.832031  north=-375000  south=375000
Run:  python fetch_map.py   ->  satisfactory_opt/assets/map_bg.jpg
"""
from __future__ import annotations

import os
import urllib.request

URL = ("https://static.satisfactory-calculator.com/js/InteractiveMap/img/"
       "backgroundGame_2048.jpg")
DEST = os.path.join(os.path.dirname(__file__),
                    "satisfactory_opt", "assets", "map_bg.jpg")


def main() -> None:
    os.makedirs(os.path.dirname(DEST), exist_ok=True)
    req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
    data = urllib.request.urlopen(req, timeout=60).read()
    with open(DEST, "wb") as fh:
        fh.write(data)
    print(f"wrote {DEST}: {len(data):,} bytes")


if __name__ == "__main__":
    main()
