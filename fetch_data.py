"""Download current Satisfactory game data (items, recipes, buildings) and save
as data.json. Source: the public interactive-map game data (current Stable =
live game version, 1.2 as of 2026-06). Run:  python fetch_data.py
"""
import json
import urllib.request

URL = "https://static.satisfactory-calculator.com/data/json/gameData/en-Stable.json"


def main():
    req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
    raw = json.load(urllib.request.urlopen(req, timeout=90))
    with open("data.json", "w") as fh:
        json.dump(raw, fh)
    print(f"wrote data.json: branch={raw['branch']} "
          f"items={len(raw['itemsData'])} recipes={len(raw['recipesData'])}")


if __name__ == "__main__":
    main()
