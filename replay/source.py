"""Season sources for the replay harness.

A source yields a finished season: teams, players, a schedule, and the box
score for each game. The harness does not care where it came from, so a
synthetic season and a directory of real CSVs are interchangeable.

Stat keys MUST match fantasy_rule.stat_key, plus the key named by
qualification_rule (snap_share / minutes / appearances).
"""
from __future__ import annotations
import csv, json, os, random
from datetime import datetime, timedelta, timezone

# --------------------------------------------------------------------------
POSITIONS = {
    'NFL': ['QB', 'RB', 'WR', 'WR', 'TE'],
    'NBA': ['G', 'G', 'F', 'F', 'C'],
    'MLB': ['BAT', 'BAT', 'BAT', 'SP', 'RP'],
}
# Real-world cadence, which is the whole reason cross-sport scoring is hard.
GAMES_PER_WEEK = {'NFL': 1, 'NBA': 3.5, 'MLB': 6}


class SyntheticSource:
    """A plausible finished season. Deterministic for a given seed.

    Deliberately emits DNPs and cameo lines so the qualification filter and
    the replacement-score path are both exercised.
    """

    def __init__(self, sports=('NFL', 'NBA'), season_year=2025, weeks=18,
                 teams_per_sport=12, players_per_team=10, seed=42,
                 dnp_rate=0.07, cameo_rate=0.12):
        self.sports, self.season_year, self.weeks = list(sports), season_year, weeks
        self.teams_per_sport, self.players_per_team = teams_per_sport, players_per_team
        self.dnp_rate, self.cameo_rate = dnp_rate, cameo_rate
        self.rng = random.Random(seed)
        self.start = datetime(season_year, 9, 7, tzinfo=timezone.utc)
        self._teams, self._players, self._games = [], [], []
        self._talent = {}
        self._build()

    # ---------------------------------------------------------------- build
    def _build(self):
        for sport in self.sports:
            for t in range(self.teams_per_sport):
                code = f'{sport}{t:02d}'   # must be unique ACROSS sports: external_ref is global
                self._teams.append({'sport': sport, 'code': code, 'name': f'{sport} Club {t:02d}'})
                for i in range(self.players_per_team):
                    pos = POSITIONS[sport][i % len(POSITIONS[sport])]
                    ref = f'{code}-{i:02d}'
                    self._players.append({'sport': sport, 'team_code': code, 'external_ref': ref,
                                          'full_name': f'{sport} Player {t:02d}{i:02d}',
                                          'position_group': pos})
                    # Latent talent: the signal the scoring pipeline must recover.
                    self._talent[ref] = self.rng.lognormvariate(0, 0.30)

        for sport in self.sports:
            codes = [t['code'] for t in self._teams if t['sport'] == sport]
            per_week = GAMES_PER_WEEK[sport]
            for wk in range(self.weeks):
                n = int(per_week) + (1 if self.rng.random() < (per_week % 1) else 0)
                for slot in range(n):
                    shuffled = codes[:]
                    self.rng.shuffle(shuffled)
                    for j in range(0, len(shuffled) - 1, 2):
                        tip = self.start + timedelta(weeks=wk, days=slot, hours=17 + (j % 5))
                        self._games.append({
                            'sport': sport, 'home_code': shuffled[j], 'away_code': shuffled[j + 1],
                            'starts_at': tip, 'ends_at': tip + timedelta(hours=3),
                        })
        self._games.sort(key=lambda g: g['ends_at'])

    # ------------------------------------------------------------ interface
    def teams(self):   return list(self._teams)
    def players(self): return list(self._players)
    def games(self):   return list(self._games)

    def stat_lines(self, game):
        """Box score for one game: every player on both clubs."""
        out = []
        for p in self._players:
            if p['team_code'] not in (game['home_code'], game['away_code']):
                continue
            t = self._talent[p['external_ref']]
            r = self.rng.random()
            if r < self.dnp_rate:
                out.append({'external_ref': p['external_ref'], 'did_play': False, 'stat_line': {}})
                continue
            cameo = r < self.dnp_rate + self.cameo_rate
            out.append({'external_ref': p['external_ref'], 'did_play': True,
                        'stat_line': self._line(p['sport'], p['position_group'], t, cameo)})
        return out

    # --------------------------------------------------------------- lines
    def _line(self, sport, pos, t, cameo):
        R, g = self.rng, lambda a, b: self.rng.gammavariate(a, b)
        scale = 0.25 if cameo else 1.0
        if sport == 'NFL':
            snap = R.uniform(0.04, 0.18) if cameo else R.uniform(0.45, 0.95)
            s = {'snap_share': round(snap, 3)}
            if pos == 'QB':
                s.update(pass_yd=round(g(9, 26) * t * scale), pass_td=self._pois(1.6 * t * scale),
                         pass_int=self._pois(0.7), rush_yd=round(g(1.2, 8) * scale))
            elif pos == 'RB':
                s.update(rush_yd=round(g(2.2, 26) * t * scale), rush_td=self._pois(0.55 * t * scale),
                         rec=self._pois(2.6 * scale), rec_yd=round(g(1.4, 12) * t * scale))
            elif pos == 'WR':
                s.update(rec=self._pois(4.4 * t * scale), rec_yd=round(g(2.0, 24) * t * scale),
                         rec_td=self._pois(0.45 * t * scale))
            else:  # TE
                s.update(rec=self._pois(3.2 * t * scale), rec_yd=round(g(1.7, 18) * t * scale),
                         rec_td=self._pois(0.35 * t * scale))
            if R.random() < 0.05: s['fumble_lost'] = 1
            return s
        if sport == 'NBA':
            mins = R.uniform(3, 11) if cameo else R.uniform(24, 38)
            base = mins / 32.0 * t
            pt, reb, ast = self._pois(19 * base), self._pois(5 * base), self._pois(4 * base)
            if pos == 'C':   reb, ast = self._pois(9 * base), self._pois(2 * base)
            elif pos == 'G': reb, ast = self._pois(3.5 * base), self._pois(6 * base)
            s = {'minutes': round(mins, 1), 'pt': pt, 'reb': reb, 'ast': ast,
                 'stl': self._pois(1.1 * base), 'blk': self._pois(0.7 * base),
                 'tov': self._pois(2.0 * base)}
            big = sum(1 for v in (pt, reb, ast) if v >= 10)
            if big >= 2: s['dbl_dbl'] = 1
            if big >= 3: s['trp_dbl'] = 1
            return s
        # MLB
        if pos == 'BAT':
            ab = 1 if cameo else R.randint(3, 5)
            s = {'appearances': ab}
            for _ in range(ab):
                x = R.random() * (1.0 / max(t, 0.4))
                if   x < 0.16: s['single'] = s.get('single', 0) + 1
                elif x < 0.22: s['double'] = s.get('double', 0) + 1
                elif x < 0.235: s['triple'] = s.get('triple', 0) + 1
                elif x < 0.29: s['hr'] = s.get('hr', 0) + 1
                elif x < 0.38: s['bb'] = s.get('bb', 0) + 1
            s['rbi'] = self._pois(0.55 * t); s['run'] = self._pois(0.55 * t)
            if R.random() < 0.06 * t: s['sb'] = 1
            return s
        if pos == 'SP':
            outs = R.randint(3, 8) if cameo else R.randint(12, 22)
            s = {'appearances': 1, 'out': outs, 'k': self._pois(outs * 0.32 * t),
                 'er': self._pois(max(0.2, 3.2 / max(t, 0.5))),
                 'hit_allowed': self._pois(outs * 0.28 / max(t, 0.5)),
                 'bb_allowed': self._pois(1.8)}
            if R.random() < 0.42 * t: s['win'] = 1
            if outs >= 21: s['complete_game'] = 1
            return s
        outs = R.randint(1, 4)  # RP
        s = {'appearances': 1, 'out': outs, 'k': self._pois(outs * 0.38 * t),
             'er': self._pois(0.45 / max(t, 0.5)), 'hit_allowed': self._pois(outs * 0.25),
             'bb_allowed': self._pois(0.5)}
        if R.random() < 0.18 * t: s['save'] = 1
        return s

    def _pois(self, lam):
        """Knuth sampler — small means only, which is all these stats need."""
        if lam <= 0: return 0
        import math
        L, k, p = math.exp(-min(lam, 30)), 0, 1.0
        while True:
            p *= self.rng.random()
            if p <= L: return k
            k += 1
            if k > 60: return k


class CsvSource:
    """A season from CSVs on disk — the path real historical data takes.

    Expects, in `directory`:
      teams.csv    sport,code,name
      players.csv  sport,team_code,external_ref,full_name,position_group
      games.csv    sport,home_code,away_code,starts_at,ends_at        (ISO 8601)
      stats.csv    game_key,external_ref,did_play,stat_json
    where game_key is "sport|home_code|away_code|starts_at".
    """

    def __init__(self, directory):
        self.dir = directory
        self._teams = self._read('teams.csv')
        self._players = self._read('players.csv')
        self._games = []
        for g in self._read('games.csv'):
            g['starts_at'] = datetime.fromisoformat(g['starts_at'])
            g['ends_at'] = datetime.fromisoformat(g['ends_at'])
            self._games.append(g)
        self._games.sort(key=lambda g: g['ends_at'])
        self._stats = {}
        for row in self._read('stats.csv'):
            self._stats.setdefault(row['game_key'], []).append({
                'external_ref': row['external_ref'],
                'did_play': row['did_play'].lower() in ('1', 'true', 't', 'yes'),
                'stat_line': json.loads(row['stat_json'] or '{}'),
            })

    def _read(self, name):
        with open(os.path.join(self.dir, name), newline='') as fh:
            return list(csv.DictReader(fh))

    @staticmethod
    def game_key(g):
        return f"{g['sport']}|{g['home_code']}|{g['away_code']}|{g['starts_at'].isoformat()}"

    def teams(self):   return list(self._teams)
    def players(self): return list(self._players)
    def games(self):   return list(self._games)
    def stat_lines(self, game): return self._stats.get(self.game_key(game), [])


def export_csv(source, directory):
    """Write any source out as CSVs — used to round-trip synthetic -> CsvSource."""
    os.makedirs(directory, exist_ok=True)
    def dump(name, rows, cols):
        with open(os.path.join(directory, name), 'w', newline='') as fh:
            w = csv.DictWriter(fh, fieldnames=cols); w.writeheader()
            for r in rows: w.writerow({c: r[c] for c in cols})
    dump('teams.csv', source.teams(), ['sport', 'code', 'name'])
    dump('players.csv', source.players(),
         ['sport', 'team_code', 'external_ref', 'full_name', 'position_group'])
    games = source.games()
    with open(os.path.join(directory, 'games.csv'), 'w', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=['sport', 'home_code', 'away_code', 'starts_at', 'ends_at'])
        w.writeheader()
        for g in games:
            w.writerow({'sport': g['sport'], 'home_code': g['home_code'], 'away_code': g['away_code'],
                        'starts_at': g['starts_at'].isoformat(), 'ends_at': g['ends_at'].isoformat()})
    with open(os.path.join(directory, 'stats.csv'), 'w', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=['game_key', 'external_ref', 'did_play', 'stat_json'])
        w.writeheader()
        for g in games:
            key = CsvSource.game_key(g)
            for s in source.stat_lines(g):
                w.writerow({'game_key': key, 'external_ref': s['external_ref'],
                            'did_play': s['did_play'], 'stat_json': json.dumps(s['stat_line'])})
