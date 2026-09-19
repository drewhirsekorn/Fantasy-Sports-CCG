"""Play a full demo match through the API, headlessly.

Mirrors exactly what the browser client does, so a broken loop shows up here
without opening a browser.

    uvicorn api.main:app --port 8000 &
    python3 -m demo.play
"""
from __future__ import annotations
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("CCG_API", "http://localhost:8000")
TOKEN = None


def api(path, method="GET", body=None):
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header("Content-Type", "application/json")
    if TOKEN:
        req.add_header("Authorization", "Bearer " + TOKEN)
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(req, data) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        detail = json.loads(e.read() or b"null")
        raise SystemExit(f"{method} {path} -> {e.code}: {detail}")


def main():
    global TOKEN
    TOKEN = api("/auth/token", "POST", {"handle": "alice"})["token"]

    pool = api("/collection?limit=200")["items"]
    tactics = api("/my/tactics")["items"]
    deck = api("/decks", "POST", {"name": "demo lineup"})
    budget = api(f"/decks/{deck['id']}")["budget"]["limit"]
    print(f"pool {len(pool)} cards · {len(tactics)} tactics · budget {budget}")

    # Same greedy shape the client nudges a human toward: cover both sports,
    # then spend what is left on the best cards that still fit.
    by_form = sorted(pool, key=lambda c: -float(c["form"]))
    starters, bench, used, spent = [], [], set(), 0.0

    # Reserve room for the slots still to fill, priced on the cheapest cards
    # STILL AVAILABLE -- reserving against the pool minimum overstates what is
    # left once that card is taken, and the last slot misses by a fraction.
    def affordable(c):
        need = 4 - len(starters)
        rest = sorted(float(x["form"]) for x in pool
                      if x["player_id"] not in used and x["player_id"] != c["player_id"])
        return spent + float(c["form"]) + sum(rest[:need]) <= budget

    for sport in sorted({c["sport"] for c in pool}):
        for c in by_form:
            if c["sport"] == sport and c["player_id"] not in used and affordable(c):
                used.add(c["player_id"]); spent += float(c["form"]); starters.append(c); break
    for c in by_form:
        if len(starters) == 5:
            break
        if c["player_id"] in used or not affordable(c):
            continue
        used.add(c["player_id"]); spent += float(c["form"]); starters.append(c)
    for c in by_form:
        if len(bench) == 3:
            break
        if c["player_id"] not in used:
            used.add(c["player_id"]); bench.append(c)

    picked, counterplay = [], 0
    for t in tactics:
        if len(picked) == 2:
            break
        if t["requires_opponent_reveal"]:
            if counterplay:
                continue
            counterplay += 1
        picked.append(t)

    saved = api(f"/decks/{deck['id']}/slots", "PUT", {
        "starters": [{"slot_index": i, "card_instance_id": c["instance_id"]}
                     for i, c in enumerate(starters, 1)],
        "bench": [{"slot_index": i, "card_instance_id": c["instance_id"]}
                  for i, c in enumerate(bench, 1)],
        "tactics": [{"slot_index": i, "tactic_instance_id": t["instance_id"],
                     "tactic_target_index": i} for i, t in enumerate(picked, 1)],
    })
    print(f"lineup  {saved['budget']['used']} of {budget}  "
          f"{'LEGAL' if saved['legal'] else saved['violations']}")
    for c in starters:
        print(f"   {c['sport']:<4} {c['name']:<20} {c['rarity']:<10} form {round(float(c['form']))}")
    for t in picked:
        print(f"   tactic  {t['name']}  {t['hit_mult']}x / {t['miss_mult']}x")
    if not saved["legal"]:
        raise SystemExit("deck is not legal")

    ch = api("/challenges", "POST", {"format": "flash"})
    api(f"/challenges/{ch['id']}/lock", "POST", {"deck_id": deck["id"]})
    opp = api(f"/challenges/{ch['id']}/bot", "POST")
    r = api(f"/challenges/{ch['id']}/play", "POST")

    print(f"\n=== challenge {ch['id']} vs {opp['opponent']} ===")
    for res in r["results"]:
        print(f"  {res['handle']:<8} {res['total_score']:>8}  "
              f"{'WINNER' if res['is_winner'] else ''}")
    cf = r["counterfactual"]
    print(f"\n  final margin {cf['final_margin']:+}   "
          f"pre-tactic margin {cf['pre_tactic_margin']:+}")
    print(f"  tactics reversed the result: {cf['tactics_decided_it']}")

    you = next(s for s in r["sides"] if s["is_you"])
    print("\n  your lineup")
    for s in you["slots"]:
        notes = [n for n, on in (("floor", s["floor_applied"]),
                                 ("bench sub", s["substituted_from_bench"]),
                                 (f"tactic {s['tactic_multiplier']}x",
                                  float(s["tactic_multiplier"]) != 1.0)) if on]
        print(f"    {s['sport']:<4} {str(s['name'])[:18]:<18} raw {s['raw_game_score']:>6} "
              f"-> {s['final_score']:>7}  {', '.join(notes)}")
    print("\n  tactic log")
    for t in r["tactic_log"]:
        ev = " · ".join(f"{k}: {v}" for k, v in (t["evidence"] or {}).items() if k != "tactic")
        print(f"    {t['handle']:<7} {t['name']:<12} "
              f"{'HIT ' if t['did_hit'] else 'MISS'} {t['multiplier']}x   {ev}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
