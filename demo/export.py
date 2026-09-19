"""Freeze the prototype into a single JSON blob.

The prototype has no live data: the season is replayed and every game a match
could need is already final. So everything the game reads is knowable up
front -- each card's next GameScore, the three before it, the tactic table --
and the whole thing fits in a file small enough to ship inside one page.

That makes a standalone build possible: no Postgres, no API, no install. The
rules still live in engine/resolution.py and db/ -- this only moves the DATA,
and anything reading it has to re-implement the rules and stay honest about
matching (see the parity check at the bottom).

    python3 -m demo.export > web/data.json
"""
from __future__ import annotations
import json
import os
import sys

import psycopg2
import psycopg2.extras

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DSN = os.environ.get("CCG_DSN", "host=/tmp port=5433 user=ccg dbname=ccg")


def main() -> int:
    cx = psycopg2.connect(DSN)
    cur = cx.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT value::timestamptz AS t FROM app_config WHERE key='demo_lock_at'")
    row = cur.fetchone()
    if not row:
        print("no demo_lock_at - run ./db/dev.sh prototype first", file=sys.stderr)
        return 1
    lock_at = row["t"]

    cur.execute("SELECT code, replacement_score FROM sport")
    replacement = {r["code"]: float(r["replacement_score"]) for r in cur.fetchall()}

    cur.execute("SELECT form_budget FROM challenge_format WHERE code='flash'")
    budget = cur.fetchone()["form_budget"]

    cur.execute("SELECT rarity, floor_score FROM rarity_floor")
    floors = {r["rarity"]: float(r["floor_score"]) for r in cur.fetchall()}

    # --- the pool -------------------------------------------------------
    # One row per card. `next` is the game that decides the match: the first
    # one its team plays after the lock date. A null score there is a DNP,
    # which is the case the bench and the replacement rule exist for.
    cur.execute("""
        WITH pool AS (
          SELECT cp.id AS card_print_id, cp.rarity, p.id AS player_id,
                 p.full_name, p.sport, p.position_group, p.team_id
          FROM card_print cp
          JOIN card_set cs ON cs.id = cp.set_id AND cs.code = 'DEMO'
          JOIN player p ON p.id = cp.player_id)
        SELECT pool.*,
               rf.floor_score,
               latest_form(pool.player_id, current_date) AS form,
               (SELECT g.id FROM game g
                 WHERE (g.home_team_id = pool.team_id OR g.away_team_id = pool.team_id)
                   AND g.final_at > %s
                 ORDER BY g.final_at LIMIT 1) AS next_game_id
        FROM pool JOIN rarity_floor rf ON rf.rarity = pool.rarity
        ORDER BY pool.sport, pool.player_id""", (lock_at,))
    pool = cur.fetchall()

    cards = []
    for c in pool:
        cur.execute("""SELECT game_score FROM game_score
                       WHERE game_id = %s AND player_id = %s""",
                    (c["next_game_id"], c["player_id"]))
        got = cur.fetchone()
        # The three GameScores before the lock, oldest first: Bounce Back
        # reads the last one, Heater the run of three.
        cur.execute("""SELECT gs.game_score FROM game_score gs
                       JOIN game g ON g.id = gs.game_id
                       WHERE gs.player_id = %s AND g.final_at <= %s
                       ORDER BY g.final_at DESC LIMIT 3""", (c["player_id"], lock_at))
        prior = [float(r["game_score"]) for r in cur.fetchall()][::-1]
        # Enough history for the card detail view.
        cur.execute("""SELECT round(form, 2) AS form, as_of::date::text AS on
                       FROM player_form WHERE player_id = %s AND as_of <= %s
                       ORDER BY as_of DESC LIMIT 12""", (c["player_id"], lock_at))
        hist = [dict(r) for r in cur.fetchall()][::-1]
        cur.execute("""SELECT round(gs.game_score, 2) AS score, g.final_at::date::text AS on
                       FROM game_score gs JOIN game g ON g.id = gs.game_id
                       WHERE gs.player_id = %s AND g.final_at <= %s
                       ORDER BY g.final_at DESC LIMIT 8""", (c["player_id"], lock_at))
        recent = [dict(r) for r in cur.fetchall()]
        cards.append({
            "id": c["player_id"],
            "name": c["full_name"],
            "sport": c["sport"],
            "pos": c["position_group"],
            "rarity": c["rarity"],
            "floor": float(c["floor_score"]),
            "form": round(float(c["form"]), 2) if c["form"] is not None else None,
            # null => did not play. Kept null rather than pre-substituted: the
            # bench rule has to be able to see the absence.
            "score": round(float(got["game_score"]), 2) if got else None,
            "prior": [round(x, 2) for x in prior],
            "form_history": [{"form": float(h["form"]), "on": h["on"]} for h in hist],
            "recent": [{"score": float(r["score"]), "on": r["on"]} for r in recent],
        })

    # --- the tactics ----------------------------------------------------
    cur.execute("""SELECT code, name, family, condition_key, base_rate,
                          hit_mult, miss_mult, requires_opponent_reveal,
                          requires_multi_sport
                   FROM tactic_card
                   WHERE id IN (SELECT DISTINCT tactic_card_id FROM tactic_instance)
                   ORDER BY code""")
    # The printed condition line lives in the design file, not the database --
    # the database stores what the engine evaluates, which is a key.
    import json as _json
    printed = {c["name"]: c["condition"]
               for c in _json.load(open(os.path.join(
                   os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "data", "tactic_cards.json")))}
    tactics = [{
        "code": t["code"], "name": t["name"],
        "text": printed.get(t["name"], t["condition_key"].replace("_", " ")),
        "family": t["family"],
        "condition": t["condition_key"],
        "base_rate": float(t["base_rate"]),
        "hit": float(t["hit_mult"]), "miss": float(t["miss_mult"]),
        "counterplay": t["requires_opponent_reveal"],
        "multi_sport": t["requires_multi_sport"],
    } for t in cur.fetchall()]

    out = {
        "generated_from": "db/dev.sh prototype",
        "lock_at": lock_at.date().isoformat(),
        "budget": budget,
        "replacement": replacement,
        "floors": floors,
        "cards": cards,
        "tactics": tactics,
    }
    json.dump(out, sys.stdout, separators=(",", ":"))
    print(f"\n{len(cards)} cards, {len(tactics)} tactics, "
          f"{sum(1 for c in cards if c['score'] is None)} scratched",
          file=sys.stderr)
    cx.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
