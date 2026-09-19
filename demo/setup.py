"""Build the demo: a fixed card pool, no packs, no live data.

Every player gets an identical collection, so nothing depends on acquisition
and nobody has a collection advantage. What is left is the part worth
testing: can you build a better lineup under the budget, and can you read a
matchup better than your opponent.

    python3 -m demo.setup            # build (idempotent)
    python3 -m demo.setup --reset    # wipe demo decks/challenges and rebuild
"""
from __future__ import annotations
import argparse
import os
import random
import sys

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DSN = os.environ.get("CCG_DSN", "host=/tmp port=5433 user=ccg dbname=ccg")

POOL_PER_SPORT = 24
SPORTS = ("NFL", "NBA")

# Rarity is independent of how good the player is -- it moves the floor, not
# the ceiling. So it is dealt evenly across the pool rather than by Form.
RARITY_CYCLE = ["common", "uncommon", "rare", "rare", "elite", "signature"]

# Only tactics whose conditions are actually implemented. Offering a card that
# silently resolves to x1.00 would teach a tester the wrong thing.
DEMO_TACTICS = ["STEADY", "RELIABLE", "CEILING", "BOUNCE_BACK", "COLD_SNAP",
                "LOCKDOWN", "OVERRATED", "TWO_SPORT", "DOUBLE_DOWN", "ALL_IN"]

DEMO_USERS = ["alice", "bob"]


def build(cur, rng):
    # --- the set --------------------------------------------------------
    cur.execute("""INSERT INTO card_set (code, name, season_year, released_at)
                   VALUES ('DEMO', 'Demo Set', 2025, '2025-08-01')
                   ON CONFLICT (code) DO NOTHING""")
    cur.execute("SELECT id FROM card_set WHERE code = 'DEMO'")
    set_id = cur.fetchone()[0]

    # A lock date with games still ahead of it, so matches can resolve.
    cur.execute("SELECT max(final_at) - INTERVAL '21 days' FROM game")
    lock_at = cur.fetchone()[0]
    if lock_at is None:
        raise SystemExit("no games in the database - run ./db/dev.sh demo first")
    cur.execute("""INSERT INTO app_config (key, value) VALUES ('demo_lock_at', %s)
                   ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
                (lock_at.isoformat(),))

    # --- the pool -------------------------------------------------------
    # Stratified across the Form range so the budget forces real choices
    # rather than "take the five best".
    chosen = []
    for sport in SPORTS:
        cur.execute("""
            WITH latest AS (SELECT DISTINCT ON (player_id) player_id, form
                            FROM player_form ORDER BY player_id, as_of DESC)
            SELECT p.id, l.form FROM latest l
            JOIN player p ON p.id = l.player_id
            WHERE p.sport = %s AND l.form IS NOT NULL
              AND EXISTS (SELECT 1 FROM game g
                          WHERE (g.home_team_id = p.team_id OR g.away_team_id = p.team_id)
                            AND g.starts_at > %s)
            ORDER BY l.form DESC""", (sport, lock_at))
        ranked = cur.fetchall()
        if len(ranked) < POOL_PER_SPORT:
            raise SystemExit(f"only {len(ranked)} eligible {sport} players")
        step = len(ranked) / POOL_PER_SPORT
        chosen += [ranked[int(i * step)] for i in range(POOL_PER_SPORT)]

    prints = []
    for i, (player_id, _form) in enumerate(chosen):
        rarity = RARITY_CYCLE[i % len(RARITY_CYCLE)]
        cur.execute("""INSERT INTO card_print (set_id, player_id, rarity, print_run_limit)
                       VALUES (%s, %s, %s, NULL)
                       ON CONFLICT (set_id, player_id, rarity) DO NOTHING""",
                    (set_id, player_id, rarity))
        cur.execute("""SELECT id FROM card_print
                       WHERE set_id=%s AND player_id=%s AND rarity=%s""",
                    (set_id, player_id, rarity))
        prints.append(cur.fetchone()[0])

    # --- identical collections -----------------------------------------
    for handle in DEMO_USERS:
        cur.execute("INSERT INTO app_user (handle) VALUES (%s) ON CONFLICT DO NOTHING",
                    (handle,))
        cur.execute("SELECT id FROM app_user WHERE handle = %s", (handle,))
        uid = cur.fetchone()[0]
        for print_id in prints:
            cur.execute("""SELECT 1 FROM card_instance
                           WHERE card_print_id=%s AND owner_id=%s""", (print_id, uid))
            if cur.fetchone():
                continue
            cur.execute("""INSERT INTO card_instance
                             (card_print_id, serial_no, owner_id, acquired_via)
                           VALUES (%s, (SELECT COALESCE(max(serial_no),0)+1
                                        FROM card_instance WHERE card_print_id=%s),
                                   %s, 'demo')""", (print_id, print_id, uid))
            cur.execute("UPDATE card_print SET minted_count = minted_count + 1 WHERE id=%s",
                        (print_id,))
        for code in DEMO_TACTICS:
            cur.execute("SELECT id FROM tactic_card WHERE code = %s", (code,))
            row = cur.fetchone()
            if not row:
                continue
            cur.execute("""SELECT 1 FROM tactic_instance
                           WHERE tactic_card_id=%s AND owner_id=%s""", (row[0], uid))
            if not cur.fetchone():
                cur.execute("""INSERT INTO tactic_instance (tactic_card_id, owner_id)
                               VALUES (%s, %s)""", (row[0], uid))
    # A fixed set means exactly this set. Earlier seeding may have minted
    # other cards; drop the ones no deck still references, then recompute
    # minted_count so the print runs stay honest.
    cur.execute("SELECT id FROM app_user WHERE handle = ANY(%s)", (DEMO_USERS,))
    uids = [r[0] for r in cur.fetchall()]
    cur.execute("""DELETE FROM card_instance ci USING card_print cp
                   WHERE ci.card_print_id = cp.id
                     AND cp.set_id <> %s AND ci.owner_id = ANY(%s)
                     AND NOT EXISTS (SELECT 1 FROM deck_slot ds
                                     WHERE ds.card_instance_id = ci.id)""", (set_id, uids))
    cur.execute("""DELETE FROM tactic_instance ti USING tactic_card tc
                   WHERE ti.tactic_card_id = tc.id
                     AND NOT (tc.code = ANY(%s)) AND ti.owner_id = ANY(%s)
                     AND NOT EXISTS (SELECT 1 FROM deck_slot ds
                                     WHERE ds.tactic_instance_id = ti.id)""",
                (DEMO_TACTICS, uids))
    cur.execute("""UPDATE card_print cp SET minted_count =
                   (SELECT count(*) FROM card_instance ci WHERE ci.card_print_id = cp.id)""")
    return set_id, lock_at, len(prints)


def main():
    ap = argparse.ArgumentParser(prog="demo.setup")
    ap.add_argument("--reset", action="store_true",
                    help="drop demo decks and challenges before rebuilding")
    args = ap.parse_args()
    rng = random.Random(11)

    with psycopg2.connect(DSN) as cx, cx.cursor() as cur:
        if args.reset:
            # Settled results are append-only and locked entry slots are
            # immutable -- on purpose, so a late stat correction can never
            # rewrite a finished match. Application code does not get to
            # switch those off, so --reset clears only what is legitimately
            # clearable: decks, and challenges nobody has locked into.
            # For a genuine wipe: ./db/dev.sh reset && python3 -m demo.setup
            cur.execute("""SELECT c.id FROM challenge c
                           WHERE NOT EXISTS (SELECT 1 FROM challenge_entry e
                                             WHERE e.challenge_id = c.id
                                               AND e.locked_at IS NOT NULL)""")
            open_ids = [r[0] for r in cur.fetchall()]
            if open_ids:
                cur.execute("""DELETE FROM entry_slot WHERE entry_id IN
                               (SELECT id FROM challenge_entry WHERE challenge_id = ANY(%s))""",
                            (open_ids,))
                cur.execute("DELETE FROM challenge_entry WHERE challenge_id = ANY(%s)", (open_ids,))
                cur.execute("DELETE FROM challenge WHERE id = ANY(%s)", (open_ids,))
            # A settled entry points at its deck for provenance, so decks
            # referenced by any entry stay too.
            cur.execute("""DELETE FROM deck_slot WHERE deck_id NOT IN
                           (SELECT deck_id FROM challenge_entry)""")
            cur.execute("""DELETE FROM deck WHERE id NOT IN
                           (SELECT deck_id FROM challenge_entry)""")
            cur.execute("SELECT count(*) FROM challenge")
            print(f"  cleared decks and {len(open_ids)} unlocked challenges "
                  f"({cur.fetchone()[0]} locked/settled kept - they are immutable)")

        set_id, lock_at, n = build(cur, rng)
        cur.execute("""SELECT count(*) FROM card_instance ci JOIN app_user u ON u.id=ci.owner_id
                       WHERE u.handle = 'alice'""")
        owned = cur.fetchone()[0]
        cur.execute("""WITH latest AS (SELECT DISTINCT ON (player_id) player_id, form
                                       FROM player_form ORDER BY player_id, as_of DESC)
                       SELECT round(min(l.form)), round(max(l.form))
                       FROM card_print cp JOIN latest l ON l.player_id = cp.player_id
                       WHERE cp.set_id = %s""", (set_id,))
        lo, hi = cur.fetchone()
        cur.execute("SELECT form_budget FROM challenge_format WHERE code='flash'")
        budget = cur.fetchone()[0]

    print(f"  pool: {n} cards, Form {lo}-{hi}, identical for {len(DEMO_USERS)} players")
    print(f"  tactics: {len(DEMO_TACTICS)} (implemented conditions only)")
    print(f"  budget: {budget} for 5 starters  |  locks at {lock_at:%Y-%m-%d}")
    print(f"  alice owns {owned} cards")


if __name__ == "__main__":
    main()
