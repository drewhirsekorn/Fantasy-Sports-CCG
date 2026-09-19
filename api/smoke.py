"""End-to-end smoke test for the API.

Exercises the whole playable loop against a running server and a database
seeded by `./db/dev.sh demo`:

    read  -> token, collection, card detail, tactics
    build -> create a deck, fill it, watch legality flip from illegal to legal
    play  -> create a challenge, accept it, lock both sides
    recap -> the settled demo match, including the counterfactual

    uvicorn api.main:app --port 8000 &
    python3 -m api.smoke
"""
from __future__ import annotations
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("CCG_API", "http://localhost:8000")
_fails: list[str] = []


def call(method: str, path: str, token: str | None = None, body=None, expect: int = 200):
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(req, data) as r:
            status, payload = r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        status, payload = e.code, json.loads(e.read() or b"null")
    if status != expect:
        _fails.append(f"{method} {path} -> {status}, wanted {expect}: {payload}")
    return payload


def check(label: str, ok: bool, detail: str = ""):
    print(f"  {'pass' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
    if not ok:
        _fails.append(label)


def main() -> int:
    print("=== auth ===")
    alice = call("POST", "/auth/token", body={"handle": "alice"})
    bob = call("POST", "/auth/token", body={"handle": "bob"})
    ta, tb = alice["token"], bob["token"]
    check("token issued", bool(ta))
    check("bearer required", call("GET", "/me", expect=401) is not None)
    check("/me resolves the token", call("GET", "/me", ta)["handle"] == "alice")

    print("\n=== collection ===")
    col = call("GET", "/collection", ta)
    check("collection returns cards", col["totals"]["cards"] > 0,
          f"{col['totals']['cards']} copies, {col['totals']['unique_prints']} prints")
    first = col["items"][0]
    check("cards carry live Form", first.get("form") is not None,
          f"top card {first['name']} form {first['form']}")
    dupes = call("GET", "/collection?duplicates_only=true", ta)
    check("duplicate filter works",
          all(c["copies"] > 1 for c in dupes["items"]),
          f"{len(dupes['items'])} prints with copies > 1")

    detail = call("GET", f"/cards/{first['card_print_id']}", ta)
    check("card detail has Form history", len(detail["form_history"]) > 0,
          f"{len(detail['form_history'])} points, {len(detail['recent_games'])} recent games")

    tac = call("GET", "/tactics")
    check("36 tactic cards", len(tac["items"]) == 36)
    neutral = all(
        abs(float(t["base_rate"]) * float(t["hit_mult"])
            + (1 - float(t["base_rate"])) * float(t["miss_mult"]) - 1) < 0.005
        for t in tac["items"])
    check("every tactic is mean-neutral", neutral)

    print("\n=== deck building ===")
    deck = call("POST", "/decks", ta, body={"name": "smoke deck"}, expect=201)
    d = call("GET", f"/decks/{deck['id']}", ta)
    check("empty deck is illegal", not d["legal"], f"{len(d['violations'])} violations")
    budget = d["budget"]["limit"]

    # Greedy legal lineup: cheapest-first under budget, distinct players, 2+ sports.
    pool = sorted([c for c in col["items"] if c["form"] is not None],
                  key=lambda c: float(c["form"]))
    starters, bench, used, spent = [], [], set(), 0.0

    def take(c):
        nonlocal spent
        used.add(c["player_id"]); spent += float(c["form"]); starters.append(c)

    for sp in sorted({c["sport"] for c in pool}):          # one of each sport first
        if len(starters) >= 5:
            break
        for c in pool:
            if c["sport"] == sp and c["player_id"] not in used \
                    and spent + float(c["form"]) <= budget:
                take(c); break
    for c in pool:                                          # then cheapest-first
        if len(starters) == 5:
            break
        if c["player_id"] in used or spent + float(c["form"]) > budget:
            continue
        take(c)
    for c in pool:
        if len(bench) == 3:
            break
        if c["player_id"] in used:
            continue
        used.add(c["player_id"]); bench.append(c)
    check("built a lineup under budget", len(starters) == 5 and len(bench) == 3,
          f"5 starters at {round(spent, 1)} of {budget}")
    check("lineup spans 2+ sports", len({c['sport'] for c in starters}) >= 2,
          f"{sorted({c['sport'] for c in starters})}")

    # /collection groups by print; the deck needs concrete instance ids.
    import psycopg2
    cx = psycopg2.connect(os.environ.get("CCG_DSN", "host=/tmp port=5433 user=ccg dbname=ccg"))
    cur = cx.cursor()
    def one_instance(print_id, owner_handle="alice"):
        cur.execute("""SELECT ci.id FROM card_instance ci JOIN app_user u ON u.id=ci.owner_id
                       WHERE ci.card_print_id=%s AND u.handle=%s LIMIT 1""",
                    (print_id, owner_handle))
        return cur.fetchone()[0]
    cur.execute("""SELECT ti.id FROM tactic_instance ti JOIN app_user u ON u.id=ti.owner_id
                   WHERE u.handle='alice' ORDER BY ti.id LIMIT 2""")
    alice_tactics = [r[0] for r in cur.fetchall()]

    payload = {
        "starters": [{"slot_index": i, "card_instance_id": one_instance(c["card_print_id"])}
                     for i, c in enumerate(starters, 1)],
        "bench": [{"slot_index": i, "card_instance_id": one_instance(c["card_print_id"])}
                  for i, c in enumerate(bench, 1)],
        "tactics": [{"slot_index": i, "tactic_instance_id": t, "tactic_target_index": i}
                    for i, t in enumerate(alice_tactics, 1)],
    }
    filled = call("PUT", f"/decks/{deck['id']}/slots", ta, body=payload)
    check("filled deck is legal", filled["legal"],
          f"used {filled['budget']['used']} of {budget}" if filled["legal"]
          else str(filled["violations"]))

    over = call("PUT", f"/decks/{deck['id']}/slots", tb, body=payload, expect=403)
    check("cannot edit someone else's deck", over is not None)

    print("\n=== challenge ===")
    ch = call("POST", "/challenges", ta, body={"format": "flash"}, expect=201)
    check("challenge created", ch["status"] == "open", f"budget {ch['form_budget']}")
    call("POST", f"/challenges/{ch['id']}/accept", ta, expect=409)
    check("cannot accept your own challenge", True)
    acc = call("POST", f"/challenges/{ch['id']}/accept", tb)
    check("opponent accepted", acc["status"] == "accepted")

    empty = call("POST", "/decks", ta, body={"name": "illegal deck"}, expect=201)
    rej = call("POST", f"/challenges/{ch['id']}/lock", ta,
               body={"deck_id": empty["id"]}, expect=422)
    check("an illegal deck cannot be locked", rej is not None,
          "lock refuses what validate_deck rejects")

    locked = call("POST", f"/challenges/{ch['id']}/lock", ta, body={"deck_id": deck["id"]})
    check("locked one side", "both_locked" in locked, str(locked)[:90])
    if "both_locked" in locked:
        check("first lock waits for opponent", not locked["both_locked"], locked["status"])
        call("POST", f"/challenges/{ch['id']}/lock", ta, body={"deck_id": deck["id"]},
             expect=409)
        check("cannot lock twice", True)

    board = call("GET", f"/challenges/{ch['id']}", ta)
    mine = next(s for s in board["sides"] if s["is_you"])
    check("your own cards are visible", all(not s["hidden"] for s in mine["slots"]))
    check("2 of 5 revealed at lock",
          sum(1 for s in mine["slots"] if s["was_revealed_at_lock"]) == 2)

    print("\n=== recap of the settled demo match ===")
    cur.execute("SELECT id FROM challenge WHERE status='resolved' ORDER BY id LIMIT 1")
    settled = cur.fetchone()
    if settled:
        r = call("GET", f"/challenges/{settled[0]}/recap", ta)
        cf = r["counterfactual"]
        check("recap returns both results", len(r["results"]) == 2,
              f"{r['results'][0]['handle']} {r['results'][0]['total_score']} "
              f"vs {r['results'][1]['handle']} {r['results'][1]['total_score']}")
        check("counterfactual computed", cf is not None,
              f"final {cf['final_margin']:+} vs pre-tactic {cf['pre_tactic_margin']:+}"
              if cf else "")
        check("tactics decided it", cf and cf["tactics_decided_it"],
              "the result flips without tactics" if cf and cf["tactics_decided_it"] else "")
        check("opponent fully revealed after settling",
              all(not s["hidden"] for side in r["sides"] for s in side["slots"]))
        check("tactic log has evidence",
              all(t["evidence"] for t in r["tactic_log"]), f"{len(r['tactic_log'])} entries")
    cx.close()

    print()
    if _fails:
        print(f"{len(_fails)} FAILURES")
        for f in _fails:
            print(f"  - {f}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
