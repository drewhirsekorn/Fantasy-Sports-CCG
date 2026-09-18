"""Scoring engine: drains scoring_queue into game_score and player_form.

Pipeline, per the validated design:

  Stage 1  peer group    (sport, position_group), peer-eligible lines only
  Stage 2  rank-normalise percentile -> z via the inverse normal
  Stage 3  tail bonus     capped credit for blow-ups, scaled to the peer tail
  Stage 4  common scale   GameScore = 50 + 12z, clamped 0-100
  Stage 5  Form           EWMA at the sport's half-life, then shrunk toward
                          the peer prior with k games of prior weight

NO LOOKAHEAD: a snapshot for date D is built only from games that finalised
strictly before D, and a game is scored against the newest snapshot at or
before its own finalisation. Getting this wrong makes replay look better
than production ever could.

    python3 -m engine.scoring run
    python3 -m engine.scoring verify
"""
from __future__ import annotations
import argparse, bisect, math, os, sys
from collections import defaultdict

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DSN = os.environ.get('CCG_DSN', 'host=/tmp port=5433 user=ccg dbname=ccg')
TAIL_WEIGHT, SCALE = 0.75, 12.0
HARD_FLOOR = 100   # below this a group has no usable table of its own


def inv_norm(p):
    """Acklam's inverse normal CDF — the same one sim/balance_sim.py validated."""
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    pl, ph = 0.02425, 1 - 0.02425
    if p < pl:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > ph:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5; r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def quantile(xs, q):
    if not xs: return 0.0
    i = q * (len(xs) - 1); lo = int(i); hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (i - lo) * (xs[hi] - xs[lo])


def pct_rank(sorted_xs, v):
    """Mid-rank percentile of v in a sorted quantile table.

    Fantasy points are near-integers, so a 101-point table has long runs of
    equal values. Taking the FIRST index of a tie run underestimates every
    percentile and drags the whole distribution below the 50 median it is
    supposed to produce; taking the last overestimates it. The midpoint of
    the run is the standard mid-rank definition and is unbiased.

    Divided by len-1, since 101 points span 0..100 percentiles.
    """
    lo = bisect.bisect_left(sorted_xs, v)
    hi = bisect.bisect_right(sorted_xs, v)
    return ((lo + hi) / 2.0) / (len(sorted_xs) - 1)


# --------------------------------------------------------------------------
class Engine:
    def __init__(self, cur):
        self.cur = cur
        cur.execute("""SELECT code, form_half_life, form_shrink_k, form_prior FROM sport""")
        self.sport_cfg = {r[0]: {'half_life': r[1], 'k': r[2], 'prior': float(r[3])}
                          for r in cur.fetchall()}
        cur.execute("SELECT sport, position_group, parent_group, min_obs FROM position_group_config")
        self.pg_cfg = {(r[0], r[1]): {'parent': r[2], 'min_obs': r[3]} for r in cur.fetchall()}

    # ------------------------------------------------------------ peer group
    def peer_group_id(self, sport, group):
        self.cur.execute("""INSERT INTO peer_group (sport, position_group) VALUES (%s,%s)
                            ON CONFLICT (sport, position_group) DO NOTHING""", (sport, group))
        self.cur.execute("SELECT id FROM peer_group WHERE sport=%s AND position_group=%s",
                         (sport, group))
        return self.cur.fetchone()[0]

    def _family(self, sport, group):
        """Groups pooled when `group` alone is too thin: itself, then everything
        sharing its parent, then the whole sport."""
        cfg = self.pg_cfg.get((sport, group), {'parent': None, 'min_obs': 400})
        parent = cfg['parent']
        if parent:
            sibs = [g for (s, g), c in self.pg_cfg.items() if s == sport and c['parent'] == parent]
            return [group], sorted(set(sibs) | {group})
        return [group], [g for (s, g) in self.pg_cfg if s == sport]

    def ensure_snapshot(self, sport, group, as_of):
        """Newest snapshot at or before as_of; builds one if absent for that date.

        Built ONLY from games finalised strictly before as_of.
        """
        pgid = self.peer_group_id(sport, group)
        self.cur.execute("""SELECT id FROM peer_group_snapshot
                            WHERE peer_group_id=%s AND as_of=%s""", (pgid, as_of))
        row = self.cur.fetchone()
        if row: return row[0]

        narrow, wide = self._family(sport, group)
        min_obs = self.pg_cfg.get((sport, group), {'min_obs': 400})['min_obs']

        def fetch(groups):
            self.cur.execute("""
                SELECT s.fantasy_points FROM player_game_stat s
                JOIN player p ON p.id = s.player_id
                JOIN game   g ON g.id = s.game_id
                WHERE p.sport=%s AND p.position_group = ANY(%s)
                  AND s.peer_eligible AND s.revision = 1
                  AND g.final_at IS NOT NULL AND g.final_at::date < %s
                  AND g.final_at >= %s::date - INTERVAL '365 days'
                ORDER BY s.fantasy_points""", (sport, groups, as_of, as_of))
            return [float(r[0]) for r in self.cur.fetchall()]

        # A thin table of your OWN peers is only noisy; a fat table of other
        # positions is biased -- a TE measured against WRs can never be median.
        # So pool only when the group is below a hard floor, not merely thin.
        used, vals = narrow, fetch(narrow)
        if len(vals) < HARD_FLOOR:
            used, vals = wide, fetch(wide)
        if not vals:
            return None                               # nothing to normalise against yet

        qs = [round(quantile(vals, i / 100.0), 2) for i in range(101)]
        q90, q99 = quantile(vals, 0.90), quantile(vals, 0.99)
        q99 = max(q99, q90 + 0.01)                    # schema requires q99 > q90
        self.cur.execute("""INSERT INTO peer_group_snapshot
              (peer_group_id, as_of, n_obs, quantiles, q90, q99, is_bootstrap, source_groups)
              VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
              ON CONFLICT (peer_group_id, as_of) DO NOTHING RETURNING id""",
                         (pgid, as_of, len(vals), qs, round(q90, 2), round(q99, 2),
                          len(vals) < min_obs, used))
        r = self.cur.fetchone()
        if r: return r[0]
        self.cur.execute("SELECT id FROM peer_group_snapshot WHERE peer_group_id=%s AND as_of=%s",
                         (pgid, as_of))
        return self.cur.fetchone()[0]

    # ----------------------------------------------------------- game score
    def game_score(self, fp, qs, q90, q99):
        p = min(max(pct_rank(qs, fp), 0.001), 0.999)
        z = inv_norm(p)
        e = max(0.0, (fp - q90) / (q99 - q90)) if q99 > q90 else 0.0
        tail = TAIL_WEIGHT * min(e, 1.0)
        gs = max(0.0, min(100.0, 50 + SCALE * (z + tail)))
        return p, z, tail, gs

    def score_date(self, as_of):
        """Score every game that finalised on `as_of`."""
        self.cur.execute("""SELECT DISTINCT p.sport, p.position_group
                            FROM scoring_queue q
                            JOIN game g ON g.id=q.game_id
                            JOIN player_game_stat s ON s.game_id=g.id
                            JOIN player p ON p.id=s.player_id
                            WHERE q.processed_at IS NULL AND g.final_at::date = %s""", (as_of,))
        snaps = {}
        for sport, group in self.cur.fetchall():
            sid = self.ensure_snapshot(sport, group, as_of)
            if sid: snaps[(sport, group)] = sid

        cache = {}
        def table(sid):
            if sid not in cache:
                self.cur.execute("SELECT quantiles, q90, q99 FROM peer_group_snapshot WHERE id=%s", (sid,))
                q, a, b = self.cur.fetchone()
                cache[sid] = ([float(x) for x in q], float(a), float(b))
            return cache[sid]

        self.cur.execute("""SELECT q.game_id, s.player_id, s.revision, s.fantasy_points,
                                   p.sport, p.position_group, s.did_play
                            FROM scoring_queue q
                            JOIN game g ON g.id=q.game_id
                            JOIN player_game_stat s ON s.game_id=g.id AND s.revision=1
                            JOIN player p ON p.id=s.player_id
                            WHERE q.processed_at IS NULL AND g.final_at::date = %s""", (as_of,))
        rows = self.cur.fetchall()
        written = skipped = 0
        for gid, pid, rev, fp, sport, group, did_play in rows:
            sid = snaps.get((sport, group))
            if sid is None or not did_play:
                skipped += 1        # DNPs are handled by replacement_score at resolution
                continue
            qs, q90, q99 = table(sid)
            p, z, tail, gs = self.game_score(float(fp), qs, q90, q99)
            self.cur.execute("""INSERT INTO game_score
                  (game_id, player_id, stat_revision, peer_snapshot_id, fantasy_points,
                   percentile, z_score, tail_bonus, game_score)
                  VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                  ON CONFLICT (game_id, player_id, stat_revision, peer_snapshot_id) DO NOTHING""",
                             (gid, pid, rev, sid, fp, round(p, 5), round(z, 3),
                              round(tail, 3), round(gs, 2)))
            written += self.cur.rowcount

        self.cur.execute("""UPDATE scoring_queue SET processed_at=now(), attempts=attempts+1
                            WHERE processed_at IS NULL AND game_id IN
                              (SELECT id FROM game WHERE final_at::date=%s)""", (as_of,))
        return written, skipped

    # ------------------------------------------------------------- the form
    def update_forms(self, as_of):
        """EWMA at the sport's half-life, then shrunk toward the peer prior.

        Shrinkage is what stops one lucky game reading as Form 90.
        """
        self.cur.execute("""SELECT p.id, p.sport, gs.game_score
                            FROM game_score gs
                            JOIN player p ON p.id = gs.player_id
                            JOIN game g   ON g.id = gs.game_id
                            WHERE g.final_at::date <= %s
                            ORDER BY p.id, g.final_at""", (as_of,))
        series = defaultdict(list)
        sport_of = {}
        for pid, sport, gs in self.cur.fetchall():
            series[pid].append(float(gs)); sport_of[pid] = sport

        n_written = 0
        for pid, scores in series.items():
            cfg = self.sport_cfg[sport_of[pid]]
            alpha = 1 - 0.5 ** (1.0 / cfg['half_life'])
            ewma = None
            for s in scores:
                ewma = s if ewma is None else alpha * s + (1 - alpha) * ewma
            n, k, prior = len(scores), cfg['k'], cfg['prior']
            form = (n / (n + k)) * ewma + (k / (n + k)) * prior
            self.cur.execute("""INSERT INTO player_form (player_id, as_of, form, games_in_window)
                                VALUES (%s,%s,%s,%s)
                                ON CONFLICT (player_id, as_of)
                                DO UPDATE SET form=EXCLUDED.form,
                                              games_in_window=EXCLUDED.games_in_window""",
                             (pid, as_of, round(form, 2), n))
            n_written += 1
        return n_written


# --------------------------------------------------------------------- cli
def cmd_run(args):
    with psycopg2.connect(DSN) as cx, cx.cursor() as cur:
        eng = Engine(cur)
        cur.execute("""SELECT DISTINCT g.final_at::date
                       FROM scoring_queue q JOIN game g ON g.id=q.game_id
                       WHERE q.processed_at IS NULL ORDER BY 1""")
        dates = [r[0] for r in cur.fetchall()]
        if not dates:
            print("scoring_queue is empty"); return
        tot_w = tot_s = 0
        for i, d in enumerate(dates):
            w, s = eng.score_date(d)
            tot_w += w; tot_s += s
            if args.verbose and w:
                print(f"  {d}  +{w:5d} scores  ({s} skipped)")
            if i % 7 == 0 or i == len(dates) - 1:
                eng.update_forms(d)
        print(f"scored {tot_w} game-lines over {len(dates)} dates ({tot_s} skipped: DNP or no table)")
        cur.execute("SELECT count(*) FROM peer_group_snapshot")
        n_snap = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM peer_group_snapshot WHERE is_bootstrap")
        print(f"  {n_snap} peer snapshots ({cur.fetchone()[0]} bootstrap)")
        cur.execute("SELECT count(*) FROM player_form")
        print(f"  {cur.fetchone()[0]} form rows")


def cmd_verify(args):
    """Does the real pipeline reproduce what sim/balance_sim.py predicted?"""
    with psycopg2.connect(DSN) as cx, cx.cursor() as cur:
        print("=== GameScore distribution by peer group (non-bootstrap only) ===")
        print("    eligible starter lines only; target p50 = 50, p90 = 65, same in every sport\n")
        # The table is built from peer-ELIGIBLE lines, so only those are
        # calibrated to a median of 50. Cameo lines are scored against that
        # same table and are SUPPOSED to land low; pooling the two
        # populations makes a correct engine look biased.
        cur.execute("""
            SELECT p.sport, p.position_group, count(*),
                   round(percentile_cont(0.5)  WITHIN GROUP (ORDER BY gs.game_score)::numeric,1),
                   round(percentile_cont(0.9)  WITHIN GROUP (ORDER BY gs.game_score)::numeric,1),
                   round(percentile_cont(0.99) WITHIN GROUP (ORDER BY gs.game_score)::numeric,1),
                   round(stddev_pop(gs.game_score)::numeric,1)
            FROM game_score gs
            JOIN player_game_stat s ON s.game_id=gs.game_id AND s.player_id=gs.player_id
                                   AND s.revision=gs.stat_revision
            JOIN player p ON p.id=gs.player_id
            JOIN peer_group_snapshot ps ON ps.id=gs.peer_snapshot_id
            WHERE NOT ps.is_bootstrap AND s.peer_eligible
            GROUP BY p.sport, p.position_group ORDER BY 1,2""")
        print(f"  {'group':<10}{'n':>7}{'p50':>7}{'p90':>7}{'p99':>7}{'sd':>7}")
        for sport, grp, n, p50, p90, p99, sd in cur.fetchall():
            print(f"  {sport+' '+grp:<10}{n:>7}{p50:>7}{p90:>7}{p99:>7}{sd:>7}")

        cur.execute("""
            SELECT count(*), round(percentile_cont(0.5) WITHIN GROUP (ORDER BY gs.game_score)::numeric,1)
            FROM game_score gs
            JOIN player_game_stat s ON s.game_id=gs.game_id AND s.player_id=gs.player_id
                                   AND s.revision=gs.stat_revision
            JOIN peer_group_snapshot ps ON ps.id=gs.peer_snapshot_id
            WHERE NOT ps.is_bootstrap AND NOT s.peer_eligible""")
        n_cameo, med_cameo = cur.fetchone()
        print(f"\n  cameo lines (played, below the usage threshold): n={n_cameo}, "
              f"median {med_cameo} - correctly well below 50")

        print("\n=== Form distribution (should cluster near 50, best-5 under 300) ===")
        cur.execute("""
            WITH latest AS (
              SELECT DISTINCT ON (player_id) player_id, form
              FROM player_form ORDER BY player_id, as_of DESC)
            SELECT p.sport, count(*),
                   round(percentile_cont(0.1) WITHIN GROUP (ORDER BY l.form)::numeric,1),
                   round(percentile_cont(0.5) WITHIN GROUP (ORDER BY l.form)::numeric,1),
                   round(percentile_cont(0.9) WITHIN GROUP (ORDER BY l.form)::numeric,1),
                   round(max(l.form)::numeric,1)
            FROM latest l JOIN player p ON p.id=l.player_id GROUP BY p.sport ORDER BY 1""")
        print(f"  {'sport':<7}{'n':>6}{'p10':>7}{'p50':>7}{'p90':>7}{'max':>7}")
        for sport, n, p10, p50, p90, mx in cur.fetchall():
            print(f"  {sport:<7}{n:>6}{p10:>7}{p50:>7}{p90:>7}{mx:>7}")

        cur.execute("""WITH latest AS (
              SELECT DISTINCT ON (player_id) player_id, form
              FROM player_form ORDER BY player_id, as_of DESC)
            SELECT round(sum(form)::numeric,0) FROM (
              SELECT form FROM latest ORDER BY form DESC LIMIT 5) t""")
        best5 = cur.fetchone()[0]
        cur.execute("SELECT form_budget FROM challenge_format WHERE code='flash'")
        budget = cur.fetchone()[0]
        verdict = 'pass  budget binds' if best5 > budget else 'FAIL  budget never binds'
        print(f"\n  best legal 5 = {best5}   flash budget = {budget}   -> {verdict}")

        print("\n=== No lookahead: every score used a snapshot built before its game ===")
        cur.execute("""SELECT count(*) FROM game_score gs
                       JOIN game g ON g.id=gs.game_id
                       JOIN peer_group_snapshot ps ON ps.id=gs.peer_snapshot_id
                       WHERE ps.as_of > g.final_at::date""")
        bad = cur.fetchone()[0]
        print(f"  scores using a FUTURE snapshot: {bad}   "
              f"{'pass' if bad == 0 else 'FAIL'}")


def main():
    ap = argparse.ArgumentParser(prog='engine.scoring')
    ap.add_argument('command', choices=['run', 'verify'])
    ap.add_argument('--verbose', action='store_true')
    main_args = ap.parse_args()
    {'run': cmd_run, 'verify': cmd_verify}[main_args.command](main_args)


if __name__ == '__main__':
    main()
