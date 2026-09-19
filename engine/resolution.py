"""Resolution service: walks locked challenges as their real games land.

Per card, in this order:
    1. collect the K GameScores for the games linked to the slot
    2. on a DNP, substitute the first eligible bench card; if none, take
       the sport's replacement score
    3. apply the rarity FLOOR
    4. evaluate the tactic condition and apply its multiplier
    5. write slot_result, then settle the challenge when every slot is in

Floor before multiplier, always. Reversed, a bad game gets amplified before
the floor catches it and rarity stops meaning reliability.

Only the 22 tactic conditions that need nothing beyond a GameScore are wired
up here. The rest need final scores, usage baselines, depth charts or
transaction history that the ingest layer does not carry yet; they resolve as
a no-op (x1.00) and say so in their evidence.

    python3 -m engine.resolution seed      # build a demo challenge and lock it
    python3 -m engine.resolution arm       # pick each starter's K games
    python3 -m engine.resolution resolve   # score, fire tactics, settle
"""
from __future__ import annotations
import argparse, os, random, sys
from collections import defaultdict

import psycopg2
import psycopg2.extras

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DSN = os.environ.get('CCG_DSN', 'host=/tmp port=5433 user=ccg dbname=ccg')
MEDIAN = 50.0


# ===========================================================================
#  Tactic conditions.
#  Each returns (hit, evidence). ctx carries everything a Tier-0 condition can
#  need: this slot, all of the entry's slots, and the opponent's REVEALED ones.
# ===========================================================================
def _own(ctx):    return ctx['own_slots']
def _slot(ctx):   return ctx['slot']
def _revealed(ctx): return [s for s in ctx['opp_slots'] if s['is_revealed']]


def c_steady(ctx):
    s = _slot(ctx); return s['pre'] >= MEDIAN, {'score': s['pre'], 'threshold': MEDIAN}
def c_reliable(ctx):
    s = _slot(ctx); return s['pre'] >= 40, {'score': s['pre'], 'threshold': 40}
def c_ceiling(ctx):
    s = _slot(ctx); return s['pre'] >= 65, {'score': s['pre'], 'threshold': 65}
def c_blow_up(ctx):
    s = _slot(ctx); return s['pre'] >= 75, {'score': s['pre'], 'threshold': 75}

def c_bounce_back(ctx):
    s = _slot(ctx); prev = ctx['history'].get(s['player_id'], [])
    last = prev[-1] if prev else None
    hit = s['pre'] >= 55 and last is not None and last < 40
    return hit, {'score': s['pre'], 'previous_game': last}

def c_heater(ctx):
    s = _slot(ctx); prev = ctx['history'].get(s['player_id'], [])
    run = ([s['pre']] + list(reversed(prev)))[:3]
    return len(run) == 3 and all(x >= 55 for x in run), {'last_three': run}

# ---- counterplay: attaches to YOUR card, reads the OPPONENT's -------------
def c_cold_snap(ctx):
    r = _revealed(ctx); worst = min((x['pre'] for x in r), default=None)
    return (worst is not None and worst < MEDIAN), {'worst_revealed_opponent': worst}
def c_lockdown(ctx):
    r = _revealed(ctx); worst = min((x['pre'] for x in r), default=None)
    return (worst is not None and worst < 40), {'worst_revealed_opponent': worst}
def c_bust(ctx):
    r = _revealed(ctx); worst = min((x['pre'] for x in r), default=None)
    return (worst is not None and worst < 30), {'worst_revealed_opponent': worst}
def c_shutout(ctx):
    r = _revealed(ctx); dnp = [x for x in r if x['was_replacement']]
    return bool(dnp), {'opponent_dnps': len(dnp)}
def c_overrated(ctx):
    r = _revealed(ctx)
    if not r: return False, {'reason': 'no opponent card revealed'}
    top_form = max(r, key=lambda x: x['form_at_lock'])
    mine = max(x['pre'] for x in _own(ctx))
    return mine > top_form['pre'], {'their_top_form_card': top_form['pre'], 'my_best': mine}

# ---- cross-sport ---------------------------------------------------------
def _sports_clearing(ctx, threshold=MEDIAN):
    by = defaultdict(list)
    for s in _own(ctx): by[s['sport']].append(s['pre'])
    return [sp for sp, v in by.items() if any(x >= threshold for x in v)]
def c_two_sport(ctx):
    sp = _sports_clearing(ctx); return len(sp) >= 2, {'sports_clearing_median': sp}
def c_triple_threat(ctx):
    sp = _sports_clearing(ctx); return len(sp) >= 3, {'sports_clearing_median': sp}
def c_hedge(ctx):
    own = _own(ctx)
    if len(own) < 2: return False, {'reason': 'too few slots'}
    lo = min(own, key=lambda x: x['pre']); hi = max(own, key=lambda x: x['pre'])
    return lo['sport'] != hi['sport'], {'lowest': lo['sport'], 'highest': hi['sport']}
def c_global_slate(ctx):
    by = defaultdict(list)
    for s in _own(ctx): by[s['sport']].append(s['pre'])
    ok = all(min(v) >= 45 for v in by.values())
    return ok, {'min_by_sport': {k: min(v) for k, v in by.items()}}

# ---- risk structure ------------------------------------------------------
def c_double_down(ctx):
    s = _slot(ctx); return s['pre'] >= MEDIAN, {'score': s['pre']}
def c_insurance(ctx):
    s = _slot(ctx); return s['pre'] < MEDIAN, {'score': s['pre'], 'pays_on': 'downside'}
def c_all_in(ctx):
    s = _slot(ctx); best = max(x['pre'] for x in _own(ctx))
    return s['pre'] >= best, {'score': s['pre'], 'best_in_lineup': best}
def c_slow_burn(ctx):
    s = _slot(ctx); last = max(_own(ctx), key=lambda x: (x['resolved_at'] or 0))
    return s['slot_index'] == last['slot_index'] and s['pre'] >= MEDIAN, {'score': s['pre']}
def c_opening_act(ctx):
    s = _slot(ctx); first = min(_own(ctx), key=lambda x: (x['resolved_at'] or 0))
    return s['slot_index'] == first['slot_index'] and s['pre'] >= MEDIAN, {'score': s['pre']}
def c_split_decision(ctx):
    n = sum(1 for x in _own(ctx) if x['pre'] >= MEDIAN)
    return n in (2, 3), {'starters_clearing_median': n}


CONDITIONS = {
    'steady': c_steady, 'reliable': c_reliable, 'ceiling': c_ceiling, 'blow_up': c_blow_up,
    'bounce_back': c_bounce_back, 'heater': c_heater,
    'cold_snap': c_cold_snap, 'lockdown': c_lockdown, 'bust': c_bust,
    'shutout': c_shutout, 'overrated': c_overrated,
    'two_sport': c_two_sport, 'triple_threat': c_triple_threat,
    'hedge': c_hedge, 'global_slate': c_global_slate,
    'double_down': c_double_down, 'insurance': c_insurance, 'all_in': c_all_in,
    'slow_burn': c_slow_burn, 'opening_act': c_opening_act, 'split_decision': c_split_decision,
}


# ===========================================================================
class Resolver:
    def __init__(self, cur):
        self.cur = cur
        cur.execute("SELECT code, k_games, replacement_score FROM sport")
        self.sport = {r[0]: {'k': r[1], 'replacement': float(r[2])} for r in cur.fetchall()}
        cur.execute("SELECT rarity, floor_score FROM rarity_floor")
        self.floor = {r[0]: float(r[1]) for r in cur.fetchall()}

    # ------------------------------------------------------------------ arm
    def arm(self, challenge_id):
        """Pick each starter's next K games after lock. Idempotent."""
        self.cur.execute("""SELECT e.id, e.locked_at FROM challenge_entry e
                            WHERE e.challenge_id=%s AND e.locked_at IS NOT NULL""", (challenge_id,))
        entries = self.cur.fetchall()
        linked = 0
        for entry_id, locked_at in entries:
            self.cur.execute("""SELECT id, player_id, sport FROM entry_slot
                                WHERE entry_id=%s AND slot_type='starter' ORDER BY slot_index""",
                             (entry_id,))
            for slot_id, player_id, sport in self.cur.fetchall():
                k = self.sport[sport]['k']
                self.cur.execute("""SELECT g.id FROM game g, player p
                                    WHERE p.id=%s
                                      AND (g.home_team_id=p.team_id OR g.away_team_id=p.team_id)
                                      AND g.starts_at > %s
                                    ORDER BY g.starts_at LIMIT %s""", (player_id, locked_at, k))
                for seq, (gid,) in enumerate(self.cur.fetchall(), start=1):
                    self.cur.execute("""INSERT INTO entry_slot_game (entry_slot_id, sequence, game_id)
                                        VALUES (%s,%s,%s) ON CONFLICT DO NOTHING""",
                                     (slot_id, seq, gid))
                    linked += self.cur.rowcount
        return linked

    # -------------------------------------------------------------- collect
    def _slot_scores(self, entry_id):
        """Raw GameScores per starter, with bench substitution and floor."""
        self.cur.execute("""
            SELECT es.id, es.slot_index, es.player_id, es.sport, es.rarity,
                   es.form_at_lock, es.is_revealed,
                   avg(gs.game_score), count(esg.game_id), count(gs.id), max(g.final_at)
            FROM entry_slot es
            LEFT JOIN entry_slot_game esg ON esg.entry_slot_id=es.id
            LEFT JOIN game g   ON g.id=esg.game_id
            LEFT JOIN game_score gs ON gs.game_id=esg.game_id AND gs.player_id=es.player_id
            WHERE es.entry_id=%s AND es.slot_type='starter'
            GROUP BY es.id, es.slot_index, es.player_id, es.sport, es.rarity,
                     es.form_at_lock, es.is_revealed
            ORDER BY es.slot_index""", (entry_id,))
        rows = self.cur.fetchall()

        # Bench cards, in order, each usable once.
        self.cur.execute("""SELECT id, player_id, sport, rarity FROM entry_slot
                            WHERE entry_id=%s AND slot_type='bench' ORDER BY slot_index""",
                         (entry_id,))
        bench = list(self.cur.fetchall())
        used_bench = set()

        slots = []
        for (sid, idx, pid, sport, rarity, form, revealed,
             avg_score, n_linked, n_scored, final_at) in rows:
            sub_slot = None; was_replacement = False
            if n_linked and n_scored == n_linked and avg_score is not None:
                raw = float(avg_score)
            else:
                # Starter did not play. Try the bench, else replacement level.
                raw, sub_slot = None, None
                for b_id, b_pid, b_sport, b_rar in bench:
                    if b_id in used_bench: continue
                    self.cur.execute("""SELECT avg(gs.game_score)
                                        FROM entry_slot_game esg
                                        JOIN game_score gs ON gs.game_id=esg.game_id
                                                          AND gs.player_id=%s
                                        WHERE esg.entry_slot_id=%s""", (b_pid, sid))
                    got = self.cur.fetchone()[0]
                    if got is not None:
                        raw, sub_slot = float(got), b_id
                        used_bench.add(b_id)
                        break
                if raw is None:
                    raw = self.sport[sport]['replacement']
                    was_replacement = True

            floor = self.floor.get(rarity, 0.0)
            pre = max(raw, floor)
            slots.append({'entry_slot_id': sid, 'slot_index': idx, 'player_id': pid,
                          'sport': sport, 'rarity': rarity,
                          'form_at_lock': float(form or 50), 'is_revealed': revealed,
                          'raw': round(raw, 2), 'floor': floor,
                          'floor_applied': raw < floor, 'pre': round(pre, 2),
                          'substitute_slot_id': sub_slot, 'was_replacement': was_replacement,
                          'resolved_at': final_at})
        return slots

    def _history(self, slots, before):
        """Prior GameScores per player, for streak conditions."""
        hist = {}
        for s in slots:
            self.cur.execute("""SELECT gs.game_score FROM game_score gs
                                JOIN game g ON g.id=gs.game_id
                                WHERE gs.player_id=%s AND g.final_at < %s
                                ORDER BY g.final_at DESC LIMIT 3""", (s['player_id'], before))
            hist[s['player_id']] = [float(r[0]) for r in reversed(self.cur.fetchall())]
        return hist

    # ------------------------------------------------------------- resolve
    def resolve(self, challenge_id):
        self.cur.execute("""SELECT id, locked_at FROM challenge WHERE id=%s AND locked_at IS NOT NULL""",
                         (challenge_id,))
        row = self.cur.fetchone()
        if not row: return None, 'challenge not locked'
        locked_at = row[1]

        self.cur.execute("SELECT id, user_id FROM challenge_entry WHERE challenge_id=%s ORDER BY id",
                         (challenge_id,))
        entries = self.cur.fetchall()
        if len(entries) != 2: return None, 'needs exactly two entries'

        scored = {eid: self._slot_scores(eid) for eid, _ in entries}
        if any(s['resolved_at'] is None and not s['was_replacement']
               for v in scored.values() for s in v):
            return None, 'not every slot has landed yet'

        totals = {}
        for i, (eid, uid) in enumerate(entries):
            opp_eid = entries[1 - i][0]
            own, opp = scored[eid], scored[opp_eid]
            hist = self._history(own, locked_at)

            # Tactics resolve against POST-floor, PRE-multiplier scores, so no
            # tactic can feed another and the order stays well defined.
            self.cur.execute("""SELECT es.id, es.tactic_target_index, t.id, t.code, t.condition_key,
                                       t.name, t.hit_mult, t.miss_mult
                                FROM entry_slot es JOIN tactic_card t ON t.id=es.tactic_card_id
                                WHERE es.entry_id=%s AND es.slot_type='tactic'
                                ORDER BY es.slot_index""", (eid,))
            mult = defaultdict(lambda: 1.0)
            for tac_slot_id, target_idx, tid, code, ckey, tname, hit_m, miss_m in self.cur.fetchall():
                target = next((s for s in own if s['slot_index'] == target_idx), None)
                fn = CONDITIONS.get(ckey)
                if fn is None or target is None:
                    hit, evidence, m = False, {'unsupported_condition': ckey}, 1.0
                else:
                    ctx = {'slot': target, 'own_slots': own, 'opp_slots': opp, 'history': hist}
                    hit, evidence = fn(ctx)
                    m = float(hit_m) if hit else float(miss_m)
                    mult[target_idx] *= m
                self.cur.execute("""INSERT INTO tactic_resolution
                      (entry_slot_id, tactic_card_id, target_slot_id, condition_key,
                       did_hit, multiplier, evidence)
                      VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                                 (tac_slot_id, tid, target['entry_slot_id'] if target else None,
                                  ckey, hit, round(m, 2),
                                  psycopg2.extras.Json({'tactic': tname, **evidence})))

            total = 0.0
            for s in own:
                m = mult[s['slot_index']]
                final = round(s['pre'] * m, 2)
                total += final
                self.cur.execute("""INSERT INTO slot_result
                      (entry_slot_id, raw_game_score, floor_applied, substituted_from_bench,
                       substitute_slot_id, tactic_multiplier, final_score)
                      VALUES (%s,%s,%s,%s,%s,%s,%s)
                      ON CONFLICT (entry_slot_id) DO NOTHING""",
                                 (s['entry_slot_id'], s['raw'], s['floor_applied'],
                                  s['substitute_slot_id'] is not None, s['substitute_slot_id'],
                                  round(m, 2), final))
            totals[eid] = round(total, 2)

        win = max(totals, key=lambda e: totals[e])
        for eid, _ in entries:
            self.cur.execute("""INSERT INTO challenge_result
                  (challenge_id, entry_id, total_score, is_winner, tiebreak_top_card)
                  VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                             (challenge_id, eid, totals[eid], eid == win,
                              max((s['pre'] for s in scored[eid]), default=0)))
        self.cur.execute("UPDATE challenge SET status='resolved', resolved_at=now() WHERE id=%s",
                         (challenge_id,))
        return totals, None


# ===========================================================================
RARITY_MIX = ['common'] * 5 + ['uncommon'] * 3 + ['rare'] * 3 + ['elite'] * 2 + ['signature']


def mint(cur, print_id, owner_id):
    """Give a user one numbered copy of a print, keeping minted_count honest."""
    cur.execute("""INSERT INTO card_instance (card_print_id, serial_no, owner_id, acquired_via)
                   VALUES (%s, (SELECT COALESCE(max(serial_no),0)+1 FROM card_instance
                                WHERE card_print_id=%s), %s, 'seed')
                   RETURNING id""", (print_id, print_id, owner_id))
    new_id = cur.fetchone()[0]   # must fetch BEFORE the next execute resets the cursor
    cur.execute("UPDATE card_print SET minted_count = minted_count + 1 WHERE id=%s", (print_id,))
    return new_id


def seed_collection(cur, set_id, user_id, pool, rng, n=40):
    """A browsable collection: mixed rarities, some duplicates."""
    for pid, _sport, _form in rng.sample(pool, min(n, len(pool))):
        rarity = rng.choice(RARITY_MIX)
        cur.execute("""INSERT INTO card_print (set_id, player_id, rarity, print_run_limit)
                       VALUES (%s,%s,%s,5000)
                       ON CONFLICT (set_id,player_id,rarity) DO NOTHING""", (set_id, pid, rarity))
        cur.execute("SELECT id FROM card_print WHERE set_id=%s AND player_id=%s AND rarity=%s",
                    (set_id, pid, rarity))
        print_id = cur.fetchone()[0]
        for _ in range(rng.choice([1, 1, 1, 2, 3])):     # duplicates feed the dust economy
            mint(cur, print_id, user_id)


def cmd_seed(args):
    """Build a demo challenge out of the replayed season and lock it."""
    rng = random.Random(args.seed)
    with psycopg2.connect(DSN) as cx, cx.cursor() as cur:
        cur.execute("""INSERT INTO card_set (code,name,season_year,released_at)
                       VALUES ('S1','Series One',2025,'2025-08-01')
                       ON CONFLICT (code) DO NOTHING""")
        cur.execute("SELECT id FROM card_set WHERE code='S1'")
        set_id = cur.fetchone()[0]
        for h in ('alice', 'bob'):
            cur.execute("INSERT INTO app_user (handle) VALUES (%s) ON CONFLICT (handle) DO NOTHING", (h,))
        cur.execute("SELECT id, handle FROM app_user WHERE handle IN ('alice','bob') ORDER BY handle")
        users = dict((h, i) for i, h in cur.fetchall())

        # Players with a settled Form and a game still ahead of the lock date.
        cur.execute("""SELECT max(final_at) - INTERVAL '21 days' FROM game""")
        lock_at = cur.fetchone()[0]
        cur.execute("""
            WITH latest AS (SELECT DISTINCT ON (player_id) player_id, form
                            FROM player_form ORDER BY player_id, as_of DESC)
            SELECT p.id, p.sport, l.form FROM latest l
            JOIN player p ON p.id=l.player_id
            WHERE EXISTS (SELECT 1 FROM game g WHERE (g.home_team_id=p.team_id OR g.away_team_id=p.team_id)
                          AND g.starts_at > %s)
            ORDER BY l.form DESC""", (lock_at,))
        pool = cur.fetchall()
        by_sport = defaultdict(list)
        for pid, sport, form in pool: by_sport[sport].append((pid, sport, float(form)))

        cur.execute("SELECT form_budget FROM challenge_format WHERE code='flash'")
        budget = cur.fetchone()[0]
        cur.execute("""INSERT INTO challenge (format,status,created_by,opponent_id,form_budget,locked_at)
                       VALUES ('flash','locked',%s,%s,%s,%s) RETURNING id""",
                    (users['alice'], users['bob'], budget, lock_at))
        chal = cur.fetchone()[0]

        taken = set()
        def pick(n, sports, budget_left=None):
            """Greedy and always terminating: every candidate is considered at
            most once. Returns fewer than n only if the pool is exhausted.
            The budget is enforced DURING selection, so no repair loop is
            needed (a repair loop here span forever once the pool ran dry)."""
            order = []
            depth = max(len(by_sport[sp]) for sp in sports)
            for i in range(depth):
                for sp in sports:
                    if i < len(by_sport[sp]): order.append(by_sport[sp][i])
            rng.shuffle(order)
            out, spent = [], 0.0
            for pid, sport, form in order:
                if len(out) >= n: break
                if pid in taken: continue
                if budget_left is not None and spent + form > budget_left: continue
                taken.add(pid); out.append((pid, sport, form)); spent += form
            return out

        tactic_codes = ['STEADY', 'TWO_SPORT', 'COLD_SNAP', 'ALL_IN']
        for n, (handle, sports) in enumerate([('alice', ['NFL', 'NBA']), ('bob', ['NBA', 'NFL'])]):
            cur.execute("INSERT INTO deck (user_id,name) VALUES (%s,%s) RETURNING id",
                        (users[handle], f'{handle} main'))
            deck = cur.fetchone()[0]
            starters = pick(5, sports, budget_left=budget)
            benchers = pick(3, sports)
            if len(starters) < 5 or len(benchers) < 3:
                print("  not enough eligible players to field a legal deck"); return
            cur.execute("""INSERT INTO challenge_entry (challenge_id,user_id,deck_id,form_budget_used,locked_at)
                           VALUES (%s,%s,%s,%s,%s) RETURNING id""",
                        (chal, users[handle], deck, sum(f for _, _, f in starters), lock_at))
            entry = cur.fetchone()[0]
            for kind, group in (('starter', starters), ('bench', benchers)):
                for i, (pid, sport, form) in enumerate(group, start=1):
                    cur.execute("""INSERT INTO card_print (set_id,player_id,rarity,print_run_limit)
                                   VALUES (%s,%s,'rare',5000)
                                   ON CONFLICT (set_id,player_id,rarity) DO NOTHING""", (set_id, pid))
                    cur.execute("SELECT id FROM card_print WHERE set_id=%s AND player_id=%s AND rarity='rare'",
                                (set_id, pid))
                    print_id = cur.fetchone()[0]
                    mint(cur, print_id, users[handle])
                    # Two of five starters are revealed at lock, per the design.
                    revealed = (kind == 'starter' and i <= 2)
                    cur.execute("""INSERT INTO entry_slot
                          (entry_id,slot_type,slot_index,card_print_id,player_id,sport,
                           rarity,floor_score,form_at_lock,is_revealed)
                          VALUES (%s,%s,%s,%s,%s,%s,'rare',28,%s,%s)""",
                                (entry, kind, i, print_id, pid, sport, form, revealed))
            for i, code in enumerate(tactic_codes[n*2:n*2+2], start=1):
                cur.execute("SELECT id FROM tactic_card WHERE code=%s", (code,))
                tid = cur.fetchone()[0]
                cur.execute("""INSERT INTO tactic_instance (tactic_card_id, owner_id)
                               VALUES (%s,%s)""", (tid, users[handle]))
                cur.execute("""INSERT INTO entry_slot
                      (entry_id,slot_type,slot_index,tactic_card_id,tactic_target_index)
                      VALUES (%s,'tactic',%s,%s,%s)""", (entry, i, tid, i))
        for handle in ('alice', 'bob'):
            seed_collection(cur, set_id, users[handle], pool, rng)
        cur.execute("SELECT count(*) FROM card_instance")
        print(f"minted {cur.fetchone()[0]} card copies across 2 collections")
        print(f"challenge {chal} locked at {lock_at:%Y-%m-%d}  budget {budget}")
        cur.execute("SELECT violation FROM challenge_entry e, validate_entry(e.id) WHERE e.challenge_id=%s",
                    (chal,))
        bad = [r[0] for r in cur.fetchall()]
        print("  legality:", "clean" if not bad else bad)
        return chal


def cmd_arm(args):
    with psycopg2.connect(DSN) as cx, cx.cursor() as cur:
        cur.execute("SELECT id FROM challenge WHERE status='locked' ORDER BY id DESC LIMIT 1")
        r = cur.fetchone()
        if not r: print("no locked challenge"); return
        print(f"linked {Resolver(cur).arm(r[0])} slot-games for challenge {r[0]}")


def cmd_resolve(args):
    with psycopg2.connect(DSN) as cx, cx.cursor() as cur:
        cur.execute("SELECT id FROM challenge WHERE status='locked' ORDER BY id DESC LIMIT 1")
        r = cur.fetchone()
        if not r: print("no locked challenge"); return
        chal = r[0]
        totals, err = Resolver(cur).resolve(chal)
        if err: print(f"challenge {chal}: {err}"); return

        cur.execute("""SELECT u.handle, cr.total_score, cr.is_winner
                       FROM challenge_result cr JOIN challenge_entry e ON e.id=cr.entry_id
                       JOIN app_user u ON u.id=e.user_id WHERE cr.challenge_id=%s
                       ORDER BY cr.total_score DESC""", (chal,))
        print(f"=== challenge {chal} settled ===")
        for h, tot, win in cur.fetchall():
            print(f"  {h:<8} {tot:>8}  {'WINNER' if win else ''}")

        print("\n  slot detail")
        cur.execute("""SELECT u.handle, es.slot_index, es.sport, sr.raw_game_score,
                              sr.floor_applied, sr.substituted_from_bench,
                              sr.tactic_multiplier, sr.final_score
                       FROM slot_result sr
                       JOIN entry_slot es ON es.id=sr.entry_slot_id
                       JOIN challenge_entry e ON e.id=es.entry_id
                       JOIN app_user u ON u.id=e.user_id
                       WHERE e.challenge_id=%s ORDER BY u.handle, es.slot_index""", (chal,))
        for h, idx, sport, raw, fl, sub, m, fin in cur.fetchall():
            notes = []
            if fl: notes.append('floor')
            if sub: notes.append('bench sub')
            if float(m) != 1.0: notes.append(f'tactic x{m}')
            print(f"    {h:<7} #{idx} {sport:<4} raw {raw:>6} -> {fin:>7}   {', '.join(notes)}")

        print("\n  tactic log")
        cur.execute("""SELECT u.handle, tr.condition_key, tr.did_hit, tr.multiplier, tr.evidence
                       FROM tactic_resolution tr
                       JOIN entry_slot es ON es.id=tr.entry_slot_id
                       JOIN challenge_entry e ON e.id=es.entry_id
                       JOIN app_user u ON u.id=e.user_id
                       WHERE e.challenge_id=%s ORDER BY u.handle""", (chal,))
        for h, ck, hit, m, ev in cur.fetchall():
            print(f"    {h:<7} {ck:<15} {'HIT ' if hit else 'MISS'} x{m}  {ev}")


def main():
    ap = argparse.ArgumentParser(prog='engine.resolution')
    ap.add_argument('command', choices=['seed', 'arm', 'resolve'])
    ap.add_argument('--seed', type=int, default=7)
    a = ap.parse_args()
    {'seed': cmd_seed, 'arm': cmd_arm, 'resolve': cmd_resolve}[a.command](a)


if __name__ == '__main__':
    main()
