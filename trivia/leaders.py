"""Who led last night, for the things that happen every night.

The feat table in feats.py asks "who did it before?", and that question only
works when the thing is rare. Thirty-three players homered in the majors on a
normal Saturday; "who was the last to go deep" has thirty-three right answers
and the most recent one is four minutes old. Lowering the feat thresholds
does not fix that -- it breaks the question.

So the common ground needs a different question, and it is the one a fan
actually asks: **who led?** Exactly one pitcher struck out the most last
night. That is a real question with one answer, it is available every night
there are games, and the wrong options are the other men who played.

Which makes this the shape that guarantees a morning is never empty. A rare
feat is a better question when there is one; this is what there always is.

Two guards. A board with the top value **tied** is dropped, because "who led"
then has two answers. A board whose leader is below `minimum` is dropped too:
nobody wants to be asked who led the majors in strikeouts on a night the
answer is four.
"""
from __future__ import annotations

from dataclasses import dataclass

TOP_N = 10          # kept per board: one answer plus nine possible distractors


@dataclass(frozen=True)
class Leader:
    key: str
    sport: str
    role: str            # which box-score group the line comes from
    stat: str
    minimum: float       # below this, leading is not an achievement
    asks: str            # the question
    describe: str        # the line that justifies the answer
    noun: str = "players"   # what the rest of the field were
    unit: str = ""


MLB = [
    Leader("mlb_strikeouts", "MLB", "pitching", "strikeouts_thrown", 7,
           "Who struck out the most batters in the majors last night?",
           "{value:.0f} strikeouts over {innings:.1f} innings for {team} against {opponent}",
           unit="strikeouts", noun="pitchers"),
    Leader("mlb_innings", "MLB", "pitching", "innings", 8,
           "Which pitcher went deepest into his game in the majors last night?",
           "{innings:.1f} innings, {hits_allowed:.0f} hits and {runs_allowed:.0f} runs "
           "for {team} against {opponent}",
           unit="innings", noun="pitchers"),
    Leader("mlb_home_runs", "MLB", "batting", "home_runs", 2,
           "Who hit the most home runs in the majors last night?",
           "{value:.0f} home runs and {rbi:.0f} driven in for {team} against {opponent}",
           unit="home runs", noun="batters"),
    Leader("mlb_rbi", "MLB", "batting", "rbi", 5,
           "Who drove in the most runs in the majors last night?",
           "{value:.0f} RBI for {team} against {opponent}", unit="RBI", noun="batters"),
    Leader("mlb_hits", "MLB", "batting", "hits", 5,
           "Who had the most hits in the majors last night?",
           "{value:.0f} hits in {at_bats:.0f} at-bats for {team} against {opponent}",
           unit="hits", noun="batters"),
]

NBA = [
    Leader("nba_points", "NBA", "player", "points", 30,
           "Who scored the most in the NBA last night?",
           "{value:.0f} points in {minutes:.0f} minutes for {team} against {opponent}",
           unit="points", noun="players"),
    Leader("nba_rebounds", "NBA", "player", "rebounds", 14,
           "Who grabbed the most rebounds in the NBA last night?",
           "{value:.0f} rebounds for {team} against {opponent}", unit="rebounds", noun="players"),
    Leader("nba_assists", "NBA", "player", "assists", 11,
           "Who had the most assists in the NBA last night?",
           "{value:.0f} assists for {team} against {opponent}", unit="assists", noun="players"),
    Leader("nba_threes", "NBA", "player", "threes", 6,
           "Who made the most three-pointers in the NBA last night?",
           "{value:.0f} threes and {points:.0f} points for {team} against {opponent}",
           unit="threes", noun="players"),
    Leader("nba_blocks", "NBA", "player", "blocks", 4,
           "Who blocked the most shots in the NBA last night?",
           "{value:.0f} blocks for {team} against {opponent}", unit="blocks", noun="players"),
]

NFL = [
    Leader("nfl_pass_yards", "NFL", "passing", "pass_yards", 280,
           "Who threw for the most yards in the NFL yesterday?",
           "{value:.0f} yards and {pass_td:.0f} touchdowns for {team} against {opponent}",
           unit="yards", noun="quarterbacks"),
    Leader("nfl_rush_yards", "NFL", "rushing", "rush_yards", 90,
           "Who ran for the most yards in the NFL yesterday?",
           "{value:.0f} yards on {carries:.0f} carries for {team} against {opponent}",
           unit="yards", noun="ball-carriers"),
    Leader("nfl_rec_yards", "NFL", "receiving", "rec_yards", 90,
           "Who had the most receiving yards in the NFL yesterday?",
           "{value:.0f} yards on {receptions:.0f} catches for {team} against {opponent}",
           unit="yards", noun="pass-catchers"),
    Leader("nfl_pass_td", "NFL", "passing", "pass_td", 3,
           "Who threw the most touchdown passes in the NFL yesterday?",
           "{value:.0f} touchdowns and {pass_yards:.0f} yards for {team} against {opponent}",
           unit="touchdowns", noun="quarterbacks"),
    Leader("nfl_tackles", "NFL", "defensive", "tackles", 10,
           "Who made the most tackles in the NFL yesterday?",
           "{value:.0f} tackles for {team} against {opponent}", unit="tackles", noun="defenders"),
]

ALL = MLB + NBA + NFL
BY_KEY = {l.key: l for l in ALL}
BY_SPORT: dict[str, list[Leader]] = {}
for _l in ALL:
    BY_SPORT.setdefault(_l.sport, []).append(_l)


def _describe(leader: Leader, line: dict, value: float) -> str:
    fields = {k: v for k, v in line.items() if isinstance(v, (int, float))}
    fields.update({k: line.get(k, "") for k in ("player", "team", "opponent")})
    fields["value"] = value
    try:
        return leader.describe.format(**fields)
    except (KeyError, ValueError, TypeError):
        return f"{value:.0f} {leader.unit}".strip()


def board(leader: Leader, lines: list[dict]) -> list[dict] | None:
    """The top of one leaderboard for one night, or None if it is not a question.

    Returns the leader first and the runners-up behind, deduplicated by player
    -- a man who appears in two stat groups must not appear twice in one set
    of options.
    """
    rows: dict[str, dict] = {}
    for line in lines:
        if line.get("sport") != leader.sport or line.get("role") != leader.role:
            continue
        value = line.get(leader.stat)
        if value is None or not line.get("player"):
            continue
        kept = rows.get(line["player"])
        if kept and kept["value"] >= value:
            continue
        rows[line["player"]] = {
            "player": line["player"],
            "team": line.get("team", ""),
            "opponent": line.get("opponent", ""),
            "value": float(value),
            "detail": _describe(leader, line, float(value)),
            "game_id": line.get("game_id", ""),
        }
    ranked = sorted(rows.values(), key=lambda r: (-r["value"], r["player"]))
    field = len(ranked)
    if len(ranked) < 4:
        return None                       # not enough people to build options
    if ranked[0]["value"] < leader.minimum:
        return None                       # leading is not an achievement here
    if ranked[0]["value"] == ranked[1]["value"]:
        return None                       # a tie has no single answer
    for row in ranked[:TOP_N]:
        row["field"] = field          # how many were in the running
    return ranked[:TOP_N]


def collect(day: str, lines: list[dict]) -> dict:
    """Every board worth asking about, trimmed to what a question needs."""
    boards = {}
    for leader in ALL:
        rows = board(leader, lines)
        if rows:
            boards[leader.key] = {"sport": leader.sport, "rows": rows}
    return {"date": day, "boards": boards}
