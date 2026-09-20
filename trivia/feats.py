"""What counts as interesting, and how to spot it in a box score.

A feat is a line in a box score rare enough that the last time anyone did it
is worth asking about. That rarity bound is the whole design: "scored 30" is
not a question, because the answer is "someone, last night". Every threshold
here is set so the feat lands somewhere between a handful and a few dozen
times a season -- often enough that the ledger fills, rare enough that the
previous holder is a real recall.

Each feat carries its own prose. `headline` says what happened yesterday and
`asks` is the question put to the player, so adding a feat is one entry in
one table and nothing else in the codebase learns its name.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Feat:
    key: str
    sport: str
    role: str                       # which box-score group the line comes from
    label: str                      # "a 50-point game"
    stat: str                       # the key carrying the number
    threshold: float
    headline: str                   # "{player} scored {value:.0f} for {team}"
    asks: str                       # the question, minus the options
    unit: str = ""
    rank: int = 50                  # lower is rarer; breaks ties for the headline
    extra: Callable[[dict], bool] | None = None   # a second condition

    def value(self, line: dict) -> float | None:
        if line.get("role") != self.role or line.get("sport") != self.sport:
            return None
        raw = line.get(self.stat)
        if raw is None or raw < self.threshold:
            return None
        if self.extra and not self.extra(line):
            return None
        return float(raw)


# --------------------------------------------------------------------------
# NBA. A 50-point game happens ~20 times a season, 20 assists ~5, and a
# 25-rebound game fewer than that.
NBA = [
    Feat("nba_50_points", "NBA", "player", "a 50-point game", "points", 50,
         "{player} scored {value:.0f} points for {team} against {opponent}",
         "Before {player}, who was the last NBA player to score 50 in a game?",
         unit="points", rank=20),
    Feat("nba_20_assists", "NBA", "player", "a 20-assist game", "assists", 20,
         "{player} handed out {value:.0f} assists for {team}",
         "Before {player}, who was the last NBA player with 20 assists in a game?",
         unit="assists", rank=10),
    Feat("nba_25_rebounds", "NBA", "player", "a 25-rebound game", "rebounds", 25,
         "{player} pulled down {value:.0f} rebounds for {team}",
         "Before {player}, who was the last NBA player with 25 rebounds in a game?",
         unit="rebounds", rank=10),
    Feat("nba_10_threes", "NBA", "player", "a 10-three game", "threes", 10,
         "{player} made {value:.0f} threes for {team}",
         "Before {player}, who was the last NBA player to make 10 threes in a game?",
         unit="threes", rank=30),
    Feat("nba_40_10_10", "NBA", "player", "a 40-point triple-double", "points", 40,
         "{player} went for {value:.0f} points with a triple-double",
         "Before {player}, who had the last 40-point triple-double in the NBA?",
         unit="points", rank=5,
         extra=lambda l: l.get("rebounds", 0) >= 10 and l.get("assists", 0) >= 10),
]

# NFL. 17 games a week means the bar has to be high: 6 passing touchdowns is
# roughly a once-a-season event, 200 rushing yards two or three times.
NFL = [
    Feat("nfl_6_pass_td", "NFL", "passing", "a six-touchdown game", "pass_td", 6,
         "{player} threw {value:.0f} touchdown passes for {team}",
         "Before {player}, who was the last NFL quarterback to throw six touchdowns in a game?",
         unit="touchdowns", rank=5),
    Feat("nfl_450_pass_yards", "NFL", "passing", "a 450-yard passing game", "pass_yards", 450,
         "{player} threw for {value:.0f} yards for {team}",
         "Before {player}, who was the last NFL quarterback to throw for 450 yards?",
         unit="yards", rank=15),
    Feat("nfl_200_rush_yards", "NFL", "rushing", "a 200-yard rushing game", "rush_yards", 200,
         "{player} ran for {value:.0f} yards for {team}",
         "Before {player}, who was the last NFL player to rush for 200 yards in a game?",
         unit="yards", rank=15),
    Feat("nfl_4_rush_td", "NFL", "rushing", "a four-rushing-touchdown game", "rush_td", 4,
         "{player} scored {value:.0f} rushing touchdowns for {team}",
         "Before {player}, who was the last NFL player with four rushing touchdowns in a game?",
         unit="touchdowns", rank=10),
    Feat("nfl_200_rec_yards", "NFL", "receiving", "a 200-yard receiving game", "rec_yards", 200,
         "{player} caught {receptions:.0f} passes for {value:.0f} yards",
         "Before {player}, who was the last NFL player with 200 receiving yards in a game?",
         unit="yards", rank=20),
    Feat("nfl_4_sacks", "NFL", "defensive", "a four-sack game", "sacks", 4,
         "{player} sacked the quarterback {value:.0f} times for {team}",
         "Before {player}, who was the last NFL player with four sacks in a game?",
         unit="sacks", rank=10),
    Feat("nfl_3_picks", "NFL", "interceptions", "a three-interception game", "picks", 3,
         "{player} intercepted {value:.0f} passes for {team}",
         "Before {player}, who was the last NFL player to intercept three passes in a game?",
         unit="interceptions", rank=10),
]

# MLB. A no-hitter is the rarest thing a box score can show on its own, and
# the innings clause is what keeps a rained-out five-inning shutout out of it.
MLB = [
    Feat("mlb_no_hitter", "MLB", "pitching", "a no-hitter", "innings", 9,
         "{player} threw a no-hitter for {team} against {opponent}",
         "Before {player}, who threw the last no-hitter in the majors?",
         rank=1, extra=lambda l: l.get("hits_allowed", 99) == 0),
    Feat("mlb_15_k", "MLB", "pitching", "a 15-strikeout game", "strikeouts_thrown", 15,
         "{player} struck out {value:.0f} for {team}",
         "Before {player}, who was the last pitcher to strike out 15 in a game?",
         unit="strikeouts", rank=10),
    Feat("mlb_3_hr", "MLB", "batting", "a three-homer game", "home_runs", 3,
         "{player} hit {value:.0f} home runs for {team}",
         "Before {player}, who was the last player to hit three home runs in a game?",
         unit="home runs", rank=15),
    Feat("mlb_7_rbi", "MLB", "batting", "a seven-RBI game", "rbi", 7,
         "{player} drove in {value:.0f} runs for {team}",
         "Before {player}, who was the last player to drive in seven in a game?",
         unit="RBI", rank=20),
    Feat("mlb_5_hits", "MLB", "batting", "a five-hit game", "hits", 5,
         "{player} had {value:.0f} hits for {team}",
         "Before {player}, who was the last player with five hits in a game?",
         unit="hits", rank=25),
]

ALL = NBA + NFL + MLB
BY_KEY = {f.key: f for f in ALL}
BY_SPORT: dict[str, list[Feat]] = {}
for _f in ALL:
    BY_SPORT.setdefault(_f.sport, []).append(_f)


def detect(lines: list[dict]) -> list[dict]:
    """Every feat in a day's box-score lines, rarest first.

    One line can clear two feats (a 50-point triple-double is both), and both
    are kept: they are different questions.
    """
    found = []
    for line in lines:
        for feat in BY_SPORT.get(line.get("sport"), ()):
            value = feat.value(line)
            if value is None:
                continue
            found.append({
                "feat": feat.key,
                "sport": feat.sport,
                "date": line["date"],
                "player": line["player"],
                "player_id": line.get("player_id", ""),
                "team": line.get("team", ""),
                "opponent": line.get("opponent", ""),
                "value": value,
                "line": {k: v for k, v in line.items()
                         if isinstance(v, (int, float)) and k != "value"},
                "source": f"espn:{line.get('game_id','')}",
            })
    found.sort(key=lambda o: (BY_KEY[o["feat"]].rank, -o["value"], o["player"]))
    return found


def headline(occurrence: dict) -> str:
    feat = BY_KEY[occurrence["feat"]]
    fields = dict(occurrence.get("line") or {})
    fields.update({k: occurrence.get(k, "") for k in ("player", "team", "opponent")})
    fields["value"] = occurrence["value"]
    try:
        return feat.headline.format(**fields)
    except (KeyError, ValueError):
        return f"{occurrence['player']} recorded {feat.label}"
