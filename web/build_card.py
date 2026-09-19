"""Bake the card-face prototype: web/card.template.html + data -> card.html.

A card face needs the player, their Form and their history -- not the tactic
table or the frozen next-game scores the match engine reads, so the data is
trimmed rather than inlined whole.

    python3 web/build_card.py
"""
import json, pathlib, sys

here = pathlib.Path(__file__).parent
data = json.loads((here / "data.json").read_text())
slim = {"lock_at": data["lock_at"], "cards": [
    {k: c[k] for k in ("id", "name", "sport", "pos", "form", "form_history", "recent")}
    for c in data["cards"]]}
page = (here / "card.template.html").read_text()
marker = "/*__DATA__*/null"
if marker not in page:
    sys.exit(f"card.template.html has no {marker} placeholder")
out = page.replace(marker, json.dumps(slim, separators=(",", ":")))
(here / "card.html").write_text(out)
print(f"web/card.html  {len(out)/1024:.0f} KB  ({len(slim['cards'])} cards)")
