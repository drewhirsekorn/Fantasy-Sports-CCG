"""Bake the standalone build: one HTML file, no server.

Reads web/page.html (the template, with a DATA placeholder) and web/data.json,
and writes web/index.html with the data inlined. The prototype has no live
data, so everything the game reads is already final and fits in the page.

    python3 -m demo.export > web/data.json && python3 web/build.py
"""
import json, pathlib, sys

here = pathlib.Path(__file__).parent
data = json.loads((here / "data.json").read_text())
page = (here / "page.html").read_text()
marker = "/*__DATA__*/null"
if marker not in page:
    sys.exit(f"page.html has no {marker} placeholder")
out = page.replace(marker, json.dumps(data, separators=(",", ":")))
(here / "index.html").write_text(out)
print(f"web/index.html  {len(out)/1024:.0f} KB  "
      f"({len(data['cards'])} cards, {len(data['tactics'])} tactics)")
