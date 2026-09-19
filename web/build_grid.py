"""Bake the collection grid: web/grid.template.html + data -> grid.html.

    python3 web/build_grid.py
"""
import json, pathlib, sys

here = pathlib.Path(__file__).parent
data = json.loads((here / "data.json").read_text())
slim = {"cards": [{k: c[k] for k in ("id","name","sport","pos","rarity","form","recent")}
                  for c in data["cards"]]}
page = (here / "grid.template.html").read_text()
marker = "/*__DATA__*/null"
if marker not in page:
    sys.exit(f"grid.template.html has no {marker} placeholder")
out = page.replace(marker, json.dumps(slim, separators=(",", ":")))
(here / "grid.html").write_text(out)
print(f"web/grid.html  {len(out)/1024:.0f} KB  ({len(slim['cards'])} cards)")
