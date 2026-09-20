"""Box scores for one calendar day, from ESPN's public scoreboard.

Three leagues, one shape. The scoreboard for a date lists that date's games;
the summary for a game carries the box score. Neither needs a key, which is
why this is the source: the daily build has to run unattended at 07:00 ET on
a machine that holds no secrets.

The feed's job stops at normalisation. It returns `Game` objects whose
`lines` are plain dicts of numbers -- points, rebounds, passing yards, innings
pitched -- and knows nothing about what makes a line interesting. That is
feats.py, so a new feat never means touching the network layer.

ESPN is not a contract. Every field read here is read defensively: a missing
stat group, a '--' in a cell, or a game that never finished yields no line
rather than an exception, because one malformed box score must not cost the
day its puzzle.
"""
from __future__ import annotations

import http.client
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date

SPORTS = ("NBA", "NFL", "MLB")

# ESPN's path segment per league.
_PATH = {
    "NBA": "basketball/nba",
    "NFL": "football/nfl",
    "MLB": "baseball/mlb",
}
_BASE = "https://site.api.espn.com/apis/site/v2/sports"
# ESPN files these under a regular-season or postseason type, so the season
# alone does not separate them.
_EXHIBITION = {"ALLSTAR", "EXH"}
# Two agents, in order. The descriptive one is what this client would like to
# say; some edge caches and egress proxies answer 403 to any agent they do not
# recognise, so a minimal one is kept as the fallback rather than letting a
# policy quirk cost the day its puzzle. TRIVIA_USER_AGENT overrides both.
_AGENTS = [a for a in (os.environ.get("TRIVIA_USER_AGENT"),
                       "fantasy-sports-ccg-trivia/1.0 (+daily trivia build)",
                       "curl/8.7.1") if a]


class FeedError(RuntimeError):
    """The feed could not be read. The caller decides whether that is fatal."""


# --------------------------------------------------------------- transport
def _get(url: str, *, attempts: int = 4, timeout: int = 25) -> dict:
    """GET JSON, with backoff over the agents. FeedError once attempts are spent."""
    last = None
    for i in range(attempts):
        for agent in _AGENTS:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": agent})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code == 403:
                    continue          # the next agent may be accepted
                break
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError,
                    http.client.HTTPException, OSError) as exc:
                # http.client.HTTPException is the one that is easy to miss:
                # IncompleteRead is raised for a truncated chunked response and
                # descends from Exception, not OSError, so leaving it out let a
                # single cut-off box score kill a whole run.
                last = exc
                break
        if i < attempts - 1:
            time.sleep(2 ** i)
    raise FeedError(f"{url}: {last}")


# ------------------------------------------------------------------ models
@dataclass
class Game:
    """One finished game, normalised across the three leagues."""
    sport: str
    game_id: str
    day: str                 # ISO date, the league's own game date -- NOT the
                             # UTC timestamp, which puts a night game on the
                             # following day and would file it under the wrong
                             # puzzle
    name: str                # "Atlanta Braves at Detroit Tigers"
    home: str                # abbreviation
    away: str
    final: bool
    start: str = ""          # the UTC kickoff/first-pitch timestamp
    season_type: int = 2     # 1 preseason, 2 regular, 3 postseason
    comp_type: str = "STD"   # STD, ALLSTAR, EXH, SEMI, ...
    lines: list[dict] = field(default_factory=list)

    @property
    def counts(self) -> bool:
        """Does this game belong in the record?

        Playoff games do, emphatically -- half the feats worth asking about
        happen in May. Preseason, spring training and the All-Star exhibitions
        do not: a 50-point All-Star Game is not a 50-point game, and letting
        one in makes every later 'who was the last' answer wrong in a way the
        player cannot see.
        """
        return self.season_type in (2, 3) and self.comp_type not in _EXHIBITION

    @property
    def url(self) -> str:
        league = _PATH[self.sport].split("/")[1]      # nba / nfl / mlb, not the sport
        return f"https://www.espn.com/{league}/game/_/gameId/{self.game_id}"


def _num(raw) -> float | None:
    """A box-score cell as a number. '--', '', None and '5-34' are not numbers."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s in ("--", "-", "—"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _split(raw, index: int) -> float | None:
    """The left or right half of a made-attempted cell: '4-9', '18/25'."""
    if raw is None:
        return None
    s = str(raw).strip()
    for sep in ("-", "/"):
        if sep in s:
            parts = s.split(sep)
            if len(parts) == 2:
                return _num(parts[index])
    return None


# ----------------------------------------------------------------- listing
def scoreboard(sport: str, day: date) -> list[Game]:
    """Every game ESPN lists for `day`, finished or not, without box scores."""
    if sport not in _PATH:
        raise ValueError(f"unknown sport {sport!r}")
    url = f"{_BASE}/{_PATH[sport]}/scoreboard?dates={day:%Y%m%d}&limit=100"
    payload = _get(url)
    games = []
    for event in payload.get("events") or []:
        comps = event.get("competitions") or [{}]
        comp = comps[0]
        status = ((comp.get("status") or event.get("status") or {}).get("type") or {})
        home = away = ""
        for c in comp.get("competitors") or []:
            abbr = (c.get("team") or {}).get("abbreviation") or ""
            if c.get("homeAway") == "home":
                home = abbr
            else:
                away = abbr
        season = (event.get("season") or {})
        comp_type = ((comp.get("type") or {}).get("abbreviation") or "STD").upper()
        games.append(Game(
            sport=sport,
            game_id=str(event.get("id")),
            day=f"{day:%Y-%m-%d}",   # the date ESPN filed it under, by league
            start=event.get("date") or "",
            name=event.get("name") or "",
            home=home, away=away,
            final=bool(status.get("completed")),
            season_type=int(season.get("type") or 2),
            comp_type=comp_type,
        ))
    return games


# ------------------------------------------------------------- box scores
def _athletes(group: dict) -> list[tuple[dict, dict]]:
    """(athlete, stats-by-key) for one stat group, keys zipped to values."""
    keys = group.get("keys") or []
    out = []
    for entry in group.get("athletes") or []:
        athlete = entry.get("athlete") or {}
        if not athlete.get("displayName"):
            continue
        stats = entry.get("stats") or []
        if len(stats) != len(keys):
            continue
        out.append((athlete, dict(zip(keys, stats))))
    return out


def _line(game: Game, team: str, opponent: str, athlete: dict, stats: dict) -> dict:
    return {
        "sport": game.sport,
        "game_id": game.game_id,
        "date": game.day,
        "player": athlete.get("displayName"),
        "player_id": str(athlete.get("id") or ""),
        "team": team,
        "opponent": opponent,
        "stage": "postseason" if game.season_type == 3 else "regular",
        **stats,
    }


def box_score(game: Game) -> Game:
    """Fill `game.lines` from the summary endpoint. Returns the same game."""
    url = f"{_BASE}/{_PATH[game.sport]}/summary?event={game.game_id}"
    payload = _get(url)
    teams = (payload.get("boxscore") or {}).get("players") or []
    abbrs = [((t.get("team") or {}).get("abbreviation") or "") for t in teams]
    lines: list[dict] = []
    for i, team_block in enumerate(teams):
        team = abbrs[i]
        opponent = abbrs[1 - i] if len(abbrs) == 2 else ""
        for group in team_block.get("statistics") or []:
            keys = group.get("keys") or []
            for athlete, stats in _athletes(group):
                parsed = _parse(game.sport, group.get("name") or "", keys, stats)
                if parsed:
                    lines.append(_line(game, team, opponent, athlete, parsed))
    game.lines = lines
    return game


def _parse(sport: str, group: str, keys: list[str], stats: dict) -> dict | None:
    """One athlete's cells -> numbers, per league. None means 'nothing usable'."""
    if sport == "NBA":
        return _parse_nba(stats)
    if sport == "NFL":
        return _parse_nfl(group, stats)
    return _parse_mlb(keys, stats)


def _parse_nba(s: dict) -> dict | None:
    minutes = _num(s.get("minutes"))
    if minutes is None:          # DNP rows carry no minutes
        return None
    out = {
        "role": "player",
        "minutes": minutes,
        "points": _num(s.get("points")) or 0.0,
        "rebounds": _num(s.get("rebounds")) or 0.0,
        "assists": _num(s.get("assists")) or 0.0,
        "steals": _num(s.get("steals")) or 0.0,
        "blocks": _num(s.get("blocks")) or 0.0,
        "threes": _split(s.get("threePointFieldGoalsMade-threePointFieldGoalsAttempted"), 0) or 0.0,
        "fgm": _split(s.get("fieldGoalsMade-fieldGoalsAttempted"), 0) or 0.0,
    }
    return out


_NFL_GROUPS = {
    "passing": {"passingYards": "pass_yards", "passingTouchdowns": "pass_td",
                "interceptions": "interceptions"},
    "rushing": {"rushingYards": "rush_yards", "rushingTouchdowns": "rush_td",
                "rushingAttempts": "carries"},
    "receiving": {"receivingYards": "rec_yards", "receivingTouchdowns": "rec_td",
                  "receptions": "receptions"},
    "defensive": {"sacks": "sacks", "totalTackles": "tackles"},
    "interceptions": {"interceptions": "picks", "interceptionTouchdowns": "pick_six"},
    "kicking": {"longFieldGoalMade": "long_fg", "totalKickingPoints": "kick_points"},
}


def _parse_nfl(group: str, s: dict) -> dict | None:
    mapping = _NFL_GROUPS.get(group)
    if not mapping:
        return None
    out = {"role": group}
    saw = False
    for key, name in mapping.items():
        value = _num(s.get(key))
        if value is not None:
            out[name] = value
            saw = True
    if group == "passing":
        out["completions"] = _split(s.get("completions/passingAttempts"), 0) or 0.0
        out["attempts"] = _split(s.get("completions/passingAttempts"), 1) or 0.0
    return out if saw else None


def _parse_mlb(keys: list[str], s: dict) -> dict | None:
    # ESPN does not name MLB's two groups, so the keys identify them.
    if "fullInnings.partInnings" in keys:
        innings = _num(s.get("fullInnings.partInnings"))
        if innings is None:
            return None
        return {
            "role": "pitching",
            "innings": innings,
            "hits_allowed": _num(s.get("hits")) or 0.0,
            "runs_allowed": _num(s.get("runs")) or 0.0,
            "earned_runs": _num(s.get("earnedRuns")) or 0.0,
            "walks_allowed": _num(s.get("walks")) or 0.0,
            "strikeouts_thrown": _num(s.get("strikeouts")) or 0.0,
            "pitches": _num(s.get("pitches")) or 0.0,
        }
    if "hits-atBats" in keys:
        at_bats = _num(s.get("atBats"))
        if at_bats is None:
            return None
        return {
            "role": "batting",
            "at_bats": at_bats,
            "runs": _num(s.get("runs")) or 0.0,
            "hits": _num(s.get("hits")) or 0.0,
            "rbi": _num(s.get("RBIs")) or 0.0,
            "home_runs": _num(s.get("homeRuns")) or 0.0,
            "walks": _num(s.get("walks")) or 0.0,
        }
    return None


def day_lines(sport: str, day: date, *, only_final: bool = True,
              counting_only: bool = True) -> tuple[list[dict], list[Game]]:
    """Every box-score line for one sport on one date, plus the games behind them."""
    games = [g for g in scoreboard(sport, day)
             if (g.final or not only_final) and (g.counts or not counting_only)]
    lines: list[dict] = []
    kept: list[Game] = []
    for game in games:
        try:
            box_score(game)
        except FeedError:
            continue          # one unreadable game, not a lost day
        lines.extend(game.lines)
        kept.append(game)
    return lines, kept
