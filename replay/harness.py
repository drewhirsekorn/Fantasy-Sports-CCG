"""Replay harness: ingest a finished season against a virtual clock.

The harness INGESTS and advances time. It does not score. Games that finalise
land in scoring_queue for the scoring engine to drain, which keeps ingest
re-runnable without recomputing anything downstream.

Every write is idempotent, so a tick can be replayed after a crash and insert
nothing new.

    python3 -m replay.harness load  --weeks 18 --sports NFL,NBA
    python3 -m replay.harness run   --step-hours 24
    python3 -m replay.harness status
"""
from __future__ import annotations
import argparse, json, os, sys
from datetime import timedelta

import psycopg2
import psycopg2.extras

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from replay.source import SyntheticSource, CsvSource  # noqa: E402

DSN = os.environ.get('CCG_DSN', 'host=/tmp port=5433 user=ccg dbname=ccg')


def connect():
    return psycopg2.connect(DSN)


# --------------------------------------------------------------------- load
def load_catalog(cur, source):
    """Teams and players. Idempotent on (sport, code) and external_ref."""
    for t in source.teams():
        cur.execute("""INSERT INTO team (sport, code, name) VALUES (%s,%s,%s)
                       ON CONFLICT (sport, code) DO NOTHING""",
                    (t['sport'], t['code'], t['name']))
    cur.execute("SELECT sport, code, id FROM team")
    teams = {(s, c): i for s, c, i in cur.fetchall()}

    for p in source.players():
        cur.execute("""INSERT INTO player (sport, team_id, full_name, position_group, external_ref)
                       VALUES (%s,%s,%s,%s,%s) ON CONFLICT (external_ref) DO NOTHING""",
                    (p['sport'], teams[(p['sport'], p['team_code'])], p['full_name'],
                     p['position_group'], p['external_ref']))
    cur.execute("SELECT external_ref, id FROM player WHERE external_ref IS NOT NULL")
    return teams, dict(cur.fetchall())


def load_schedule(cur, source, teams):
    """The whole season's fixtures, unplayed. final_at stays NULL until a tick."""
    n = 0
    for g in source.games():
        cur.execute("""INSERT INTO game (sport, home_team_id, away_team_id, starts_at, scheduled_end)
                       VALUES (%s,%s,%s,%s,%s)
                       ON CONFLICT (sport, home_team_id, away_team_id, starts_at) DO NOTHING""",
                    (g['sport'], teams[(g['sport'], g['home_code'])],
                     teams[(g['sport'], g['away_code'])], g['starts_at'], g['ends_at']))
        n += cur.rowcount
    return n


def create_run(cur, source, label, season_year):
    games = source.games()
    lo = min(g['starts_at'] for g in games)
    hi = max(g['ends_at'] for g in games)
    cur.execute("""INSERT INTO replay_run (label, season_year, sim_now, sim_start, sim_end, games_loaded)
                   VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
                (label, season_year, lo, lo, hi, len(games)))
    return cur.fetchone()[0]


# --------------------------------------------------------------------- tick
def tick(cur, run_id, source, players, step_hours):
    """Advance the clock and finalise every game whose scheduled end has passed."""
    cur.execute("SELECT sim_now, sim_end FROM replay_run WHERE id=%s", (run_id,))
    sim_now, sim_end = cur.fetchone()
    sim_now = min(sim_now + timedelta(hours=step_hours), sim_end)

    cur.execute("""SELECT g.id, g.sport, g.starts_at, g.scheduled_end,
                          ht.code, at.code
                   FROM game g
                   JOIN team ht ON ht.id = g.home_team_id
                   JOIN team at ON at.id = g.away_team_id
                   WHERE g.final_at IS NULL AND g.scheduled_end <= %s
                   -- g.id breaks the tie. scheduled_end is far from unique
                   -- (a slate shares end times), so ordering by it alone let
                   -- Postgres return ties in whatever order it liked.
                   ORDER BY g.scheduled_end, g.id""", (sim_now,))
    due = cur.fetchall()

    finalised = lines = skipped = 0
    for gid, sport, starts_at, ends_at, home, away in due:
        stub = {'sport': sport, 'home_code': home, 'away_code': away,
                'starts_at': starts_at, 'ends_at': ends_at}
        for s in source.stat_lines(stub):
            pid = players.get(s['external_ref'])
            if pid is None:
                skipped += 1
                continue
            # fantasy_points and peer_eligible are filled by the stage0 trigger,
            # so no ingest path can invent its own scoring.
            cur.execute("""INSERT INTO player_game_stat
                             (game_id, player_id, revision, did_play, stat_line)
                           VALUES (%s,%s,1,%s,%s)
                           ON CONFLICT (game_id, player_id, revision) DO NOTHING""",
                        (gid, pid, s['did_play'], json.dumps(s['stat_line'])))
            lines += cur.rowcount
        cur.execute("UPDATE game SET final_at=%s WHERE id=%s AND final_at IS NULL",
                    (ends_at, gid))
        finalised += cur.rowcount
        cur.execute("""INSERT INTO scoring_queue (game_id, sim_final_at) VALUES (%s,%s)
                       ON CONFLICT (game_id) DO NOTHING""", (gid, ends_at))

    cur.execute("""UPDATE replay_run SET sim_now=%s, games_final=games_final+%s,
                     finished_at = CASE WHEN %s >= sim_end THEN now() ELSE NULL END
                   WHERE id=%s""", (sim_now, finalised, sim_now, run_id))
    return sim_now, finalised, lines, skipped


# ---------------------------------------------------------------------- cli
def build_source(args):
    if args.csv:
        return CsvSource(args.csv)
    return SyntheticSource(sports=tuple(args.sports.split(',')), season_year=args.season,
                           weeks=args.weeks, seed=args.seed, teams_per_sport=args.teams)


def cmd_load(args):
    source = build_source(args)
    with connect() as cx, cx.cursor() as cur:
        teams, players = load_catalog(cur, source)
        added = load_schedule(cur, source, teams)
        run_id = create_run(cur, source, args.label, args.season)
        cur.execute("SELECT count(*) FROM game")
        total = cur.fetchone()[0]
    print(f"run {run_id}: {len(teams)} teams, {len(players)} players, "
          f"{added} new fixtures ({total} total)")
    return run_id


def cmd_run(args):
    source = build_source(args)
    with connect() as cx, cx.cursor() as cur:
        cur.execute("SELECT id FROM replay_run ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        if not row:
            print("no replay run; load first"); return
        run_id = row[0]
        cur.execute("SELECT external_ref, id FROM player WHERE external_ref IS NOT NULL")
        players = dict(cur.fetchall())
        cur.execute("SELECT sim_now, sim_end FROM replay_run WHERE id=%s", (run_id,))
        sim_now, sim_end = cur.fetchone()
        ticks = tot_g = tot_l = tot_skip = 0
        while sim_now < sim_end:
            if args.max_ticks and ticks >= args.max_ticks:
                break
            sim_now, g, l, skipped = tick(cur, run_id, source, players, args.step_hours)
            ticks += 1; tot_g += g; tot_l += l; tot_skip += skipped
            if args.verbose and g:
                print(f"  {sim_now:%Y-%m-%d %H:%M}  +{g:3d} games  +{l:5d} lines")
        done = "" if sim_now >= sim_end else f", clock at {sim_now:%Y-%m-%d} of {sim_end:%Y-%m-%d}"
        print(f"replayed {tot_g} games / {tot_l} stat lines over {ticks} ticks{done}")
        if tot_skip:
            # Silence here once hid an entire sport going missing. Never again.
            print(f"WARNING: {tot_skip} stat lines dropped - unknown external_ref")


def cmd_status(args):
    with connect() as cx, cx.cursor() as cur:
        cur.execute("""SELECT id, label, sim_now, sim_end, games_loaded, games_final, finished_at
                       FROM replay_run ORDER BY id DESC LIMIT 1""")
        r = cur.fetchone()
        if not r: print("no replay run"); return
        print(f"run {r[0]} ({r[1]})  clock {r[2]:%Y-%m-%d} of {r[3]:%Y-%m-%d}  "
              f"{r[5]}/{r[4]} games final  {'DONE' if r[6] else 'running'}")
        cur.execute("SELECT count(*) FROM scoring_queue WHERE processed_at IS NULL")
        print(f"  scoring_queue pending: {cur.fetchone()[0]}")


def main():
    ap = argparse.ArgumentParser(prog='replay.harness')
    ap.add_argument('command', choices=['load', 'run', 'status'])
    ap.add_argument('--sports', default='NFL,NBA')
    ap.add_argument('--season', type=int, default=2025)
    ap.add_argument('--weeks', type=int, default=18)
    ap.add_argument('--teams', type=int, default=32, help='teams per sport')
    ap.add_argument('--seed', type=int, default=42)
    ap.add_argument('--csv', default=None, help='directory of real box-score CSVs')
    ap.add_argument('--label', default='synthetic replay')
    ap.add_argument('--step-hours', type=float, default=24.0)
    ap.add_argument('--max-ticks', type=int, default=0,
                    help='stop after N ticks; a later run resumes from the clock. '
                         'Resuming must reproduce an uninterrupted replay exactly '
                         '-- ./db/dev.sh reproducible checks that it does.')
    ap.add_argument('--verbose', action='store_true')
    args = ap.parse_args()
    {'load': cmd_load, 'run': cmd_run, 'status': cmd_status}[args.command](args)


if __name__ == '__main__':
    main()
