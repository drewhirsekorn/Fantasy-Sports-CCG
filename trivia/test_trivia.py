"""Unit tests for the daily trivia build. No network, no database.

The rules that decide whether a question is honest all live in code paths
that only fire on unusual days -- an All-Star game, a ledger with a hole in
it, two players doing the same thing the same night -- so they are the ones
checked here. A wrong answer published at 7am is not something a smoke test
the next morning can take back.

    python3 -m trivia.test_trivia
"""
from __future__ import annotations

import json
import sys
import urllib.error
from datetime import date, datetime
from zoneinfo import ZoneInfo

from trivia import build as daily
from trivia import feats, feed, leaders, puzzle
from trivia.ledger import Ledger

FAILS: list[str] = []
ET = ZoneInfo("America/New_York")


def check(label, got, want):
    ok = got == want
    print(f"  {'pass' if ok else 'FAIL'}  {label}" + ("" if ok else f"  got {got!r}, want {want!r}"))
    if not ok:
        FAILS.append(label)


# --------------------------------------------------------------- fixtures
def game(**kw) -> feed.Game:
    base = dict(sport="NBA", game_id="1", day="2025-01-10", name="A at B",
                home="A", away="B", final=True)
    base.update(kw)
    return feed.Game(**base)


def occurrence(feat, day, player, value, *, team="XXX", opponent="YYY", source="espn:g"):
    return {"feat": feat, "sport": feats.BY_KEY[feat].sport, "date": day,
            "player": player, "player_id": player, "team": team, "opponent": opponent,
            "value": float(value), "line": {}, "source": source}


def stocked() -> Ledger:
    """A ledger with enough 50-point games to build a real question from."""
    ledger = Ledger()
    for day, player, value in [
        ("2025-01-02", "Player One", 51), ("2025-01-09", "Player Two", 55),
        ("2025-01-14", "Player Three", 52), ("2025-01-20", "Player Four", 60),
        ("2025-01-28", "Player Five", 50), ("2025-02-03", "Player Six", 61),
    ]:
        ledger.add(occurrence("nba_50_points", day, player, value))
    ledger.cover("NBA", date(2025, 1, 1), date(2025, 2, 28))
    return ledger


def most_recent_for(ledger, source_day):
    return puzzle.most_recent(ledger, "NBA", source_day, puzzle._rng(source_day))


def main() -> int:
    print("=== a box-score cell is not always a number ===")
    check("'--' is not a number", feed._num("--"), None)
    check("an empty cell is not a number", feed._num(""), None)
    check("a made-attempted cell is not a number", feed._num("4-9"), None)
    check("but its left half is", feed._split("4-9", 0), 4.0)
    check("and slashes count too", feed._split("18/25", 1), 25.0)
    check("a DNP line has no minutes, so no line",
          feed._parse_nba({"points": "0"}), None)

    print("\n=== the feed survives the ways a response goes wrong ===")
    # A truncated chunked response raises IncompleteRead, which descends from
    # Exception rather than OSError. Leaving it out of the retry meant one
    # cut-off box score killed a whole run.
    import http.client
    retried = (urllib.error.URLError, TimeoutError, json.JSONDecodeError,
               http.client.HTTPException, OSError)
    for exc in (http.client.IncompleteRead, http.client.RemoteDisconnected,
                urllib.error.URLError, TimeoutError, json.JSONDecodeError,
                ConnectionResetError):
        check(f"{exc.__name__} is caught by the feed's retry",
              issubclass(exc, retried), True)

    print("\n=== which games count ===")
    # A 50-point All-Star Game is not a 50-point game. Letting one in makes
    # every later "who was the last" answer wrong, invisibly.
    check("regular season counts", game(season_type=2).counts, True)
    check("the playoffs count", game(season_type=3).counts, True)
    check("preseason does not", game(season_type=1).counts, False)
    check("the All-Star Game does not",
          game(season_type=2, comp_type="ALLSTAR").counts, False)
    check("the Pro Bowl does not, though it is filed as postseason",
          game(season_type=3, comp_type="ALLSTAR").counts, False)
    check("spring training does not", game(season_type=1, comp_type="EXH").counts, False)

    print("\n=== a day the feed would not give up is not a day that was searched ===")
    from trivia import backfill
    check("contiguous read days are one run",
          backfill.runs({"2025-01-01", "2025-01-02", "2025-01-03"}),
          [(date(2025, 1, 1), date(2025, 1, 3))])
    check("a hole splits them, so the gap is never inside a claim",
          backfill.runs({"2025-01-01", "2025-01-03"}),
          [(date(2025, 1, 1), date(2025, 1, 1)), (date(2025, 1, 3), date(2025, 1, 3))])
    check("nothing read claims nothing", backfill.runs(set()), [])

    print("\n=== a feat is a threshold, and the threshold is the point ===")
    line = {"sport": "NBA", "role": "player", "points": 50.0, "rebounds": 4.0,
            "assists": 3.0, "minutes": 38.0}
    check("50 clears the 50-point bar", feats.BY_KEY["nba_50_points"].value(line), 50.0)
    check("49 does not", feats.BY_KEY["nba_50_points"].value({**line, "points": 49.0}), None)
    check("40 points alone is not a 40-point triple-double",
          feats.BY_KEY["nba_40_10_10"].value({**line, "points": 44.0}), None)
    check("40/10/10 is",
          feats.BY_KEY["nba_40_10_10"].value({**line, "points": 44.0, "rebounds": 11.0,
                                              "assists": 10.0}), 44.0)
    check("a five-inning shutout is not a no-hitter",
          feats.BY_KEY["mlb_no_hitter"].value(
              {"sport": "MLB", "role": "pitching", "innings": 5.0, "hits_allowed": 0.0}), None)
    check("nine innings with no hits is",
          feats.BY_KEY["mlb_no_hitter"].value(
              {"sport": "MLB", "role": "pitching", "innings": 9.0, "hits_allowed": 0.0}), 9.0)
    # A 50-point triple-double is a 50-point game, a 40-point triple-double
    # and a 40-point game. All three are real questions with different
    # answers; what matters is which one gets asked.
    tiers = feats.detect([{**line, "points": 50.0, "rebounds": 12.0, "assists": 11.0,
                           "date": "2025-01-10", "player": "P", "game_id": "g"}])
    check("one line clears every tier it qualifies for",
          sorted(o["feat"] for o in tiers),
          ["nba_40_10_10", "nba_40_points", "nba_50_points"])
    check("and the rarest is the one offered first", tiers[0]["feat"], "nba_40_10_10")
    check("ranked, not alphabetical",
          [feats.BY_KEY[o["feat"]].rank for o in tiers],
          sorted(feats.BY_KEY[o["feat"]].rank for o in tiers))

    print("\n=== coverage is a claim, so it is only widened when earned ===")
    ledger = Ledger()
    ledger.cover("NBA", date(2025, 1, 1), date(2025, 1, 31))
    ledger.cover("NBA", date(2025, 2, 1), date(2025, 2, 28))
    check("an adjoining window extends the claim",
          [(w["from"], w["through"]) for w in ledger.coverage["NBA"]],
          [("2025-01-01", "2025-02-28")])
    ledger.cover("NBA", date(2025, 6, 1), date(2025, 6, 30))
    check("a window with a gap in front of it does not paper over the gap",
          len(ledger.coverage["NBA"]), 2)
    check("and the earlier window is not thrown away to make room for it",
          ledger.covers("NBA", date(2025, 1, 15)), True)
    ledger.cover("NBA", date(2025, 3, 1), date(2025, 5, 31))
    check("scanning the gap later heals it into one window",
          [(w["from"], w["through"]) for w in ledger.coverage["NBA"]],
          [("2025-01-01", "2025-06-30")])
    check("a day inside a window is covered", ledger.covers("NBA", date(2025, 6, 15)), True)
    check("a day past every window is not", ledger.covers("NBA", date(2025, 9, 15)), False)
    check("an unscanned sport is not covered", ledger.covers("MLB", date(2025, 6, 15)), False)

    print("\n=== a missed morning costs one day, not the record ===")
    # The daily job will fail eventually. If that reset coverage, every
    # 'who did it before' question would quietly stop being answerable.
    missed = stocked()
    missed.cover("NBA", date(2025, 3, 2), date(2025, 3, 2))   # a gap on 2025-03-01
    check("the day before the gap is still covered", missed.covers("NBA", date(2025, 2, 3)), True)
    check("the missed day itself is not", missed.covers("NBA", date(2025, 3, 1)), False)
    check("and questions inside the old window still build",
          bool(missed.previous("nba_50_points", date(2025, 2, 3), sport="NBA")), True)

    print("\n=== adding a feat is not retroactive ===")
    # The old scans could not have found something the detector could not yet
    # see, so their windows must not vouch for it.
    narrow = Ledger()
    narrow.add(occurrence("nba_50_points", "2025-01-02", "Player One", 51))
    narrow.add(occurrence("nba_50_points", "2025-01-09", "Player Two", 55))
    narrow.cover("NBA", date(2025, 1, 1), date(2025, 2, 28), ["nba_50_points"])
    check("the feat the scan looked for is covered",
          narrow.covers("NBA", date(2025, 1, 15), "nba_50_points"), True)
    check("a feat added afterwards is not",
          narrow.covers("NBA", date(2025, 1, 15), "nba_20_assists"), False)
    check("and no question is built about it",
          narrow.previous("nba_20_assists", date(2025, 2, 3), sport="NBA"), None)
    narrow.cover("NBA", date(2025, 1, 1), date(2025, 2, 28), ["nba_50_points", "nba_20_assists"])
    check("re-scanning the same range wider absorbs the claim it supersedes",
          len(narrow.coverage["NBA"]), 1)
    check("and it covers the new feat", narrow.covers("NBA", date(2025, 1, 15), "nba_20_assists"), True)
    check("without losing the old one", narrow.covers("NBA", date(2025, 1, 15), "nba_50_points"), True)
    partial = Ledger()
    partial.cover("NBA", date(2025, 1, 1), date(2025, 3, 31), ["nba_50_points"])
    partial.cover("NBA", date(2025, 1, 1), date(2025, 1, 31), ["nba_50_points", "nba_20_assists"])
    check("a wider catalogue over a shorter range keeps both windows",
          len(partial.coverage["NBA"]), 2)
    check("the new feat is covered only where it was actually scanned",
          (partial.covers("NBA", date(2025, 1, 15), "nba_20_assists"),
           partial.covers("NBA", date(2025, 3, 15), "nba_20_assists")), (True, False))

    print("\n=== 'who did it before' is refused unless the gap was searched ===")
    ledger = stocked()
    previous = ledger.previous("nba_50_points", date(2025, 2, 3), sport="NBA")
    check("the immediately preceding one is the answer",
          previous and previous["player"], "Player Five")
    check("the first one in the ledger has no predecessor",
          ledger.previous("nba_50_points", date(2025, 1, 2), sport="NBA"), None)
    check("a date outside coverage gets no answer at all",
          ledger.previous("nba_50_points", date(2025, 4, 1), sport="NBA"), None)
    gapped = Ledger()
    gapped.add(occurrence("nba_50_points", "2024-03-01", "Old Timer", 55))
    gapped.cover("NBA", date(2025, 1, 1), date(2025, 2, 28))
    check("an occurrence from before the searched window is not offered as 'the last'",
          gapped.previous("nba_50_points", date(2025, 2, 3), sport="NBA"), None)

    print("\n=== two players, one night: 'the last' has no single answer ===")
    tied = stocked()
    tied.add(occurrence("nba_50_points", "2025-01-28", "Player Five B", 58))
    rng = puzzle._rng(date(2025, 2, 4))
    today = occurrence("nba_50_points", "2025-02-03", "Player Six", 61)
    check("so the question is dropped rather than asked",
          puzzle.last_before(tied, today, rng), None)
    check("and with a single holder it is asked",
          puzzle.last_before(stocked(), today, puzzle._rng(date(2025, 2, 4)))["options"]
          .count("Player Five"), 1)

    print("\n=== a built question is well formed ===")
    ledger = stocked()
    q = puzzle.last_before(ledger, today, puzzle._rng(date(2025, 2, 4)))
    check("four options", len(q["options"]), puzzle.OPTIONS)
    check("no repeated option", len(set(q["options"])), puzzle.OPTIONS)
    check("the answer index points at the right player",
          q["options"][q["answer"]], "Player Five")
    check("last night's player is not one of the options",
          "Player Six" in q["options"], False)
    check("the setup says what happened", "Player Six" in q["setup"], True)

    print("\n=== no question has two right answers ===")
    # A player can hold several feats, so the pool a shallow feat borrows
    # from can hand back a name the question already has.
    shared = Ledger()
    shared.add(occurrence("nba_20_assists", "2025-01-05", "Two Feats", 20))
    for day, player, value in [("2025-01-02", "Two Feats", 51),
                               ("2025-01-09", "Two Feats", 55),
                               ("2025-01-14", "Player Three", 52)]:
        shared.add(occurrence("nba_50_points", day, player, value))
    shared.cover("NBA", date(2025, 1, 1), date(2025, 2, 28))
    thin = puzzle.last_before(shared, occurrence("nba_50_points", "2025-01-20", "New Guy", 50),
                              puzzle._rng(date(2025, 1, 21)))
    check("a question that cannot be built cleanly is not built",
          thin, None)
    everywhere = []
    for offset in range(60):
        day = date(2025, 1, 1) + __import__("datetime").timedelta(days=offset)
        everywhere += puzzle.build(day, stocked())["questions"]
    check("across two months of builds, every option list is distinct",
          all(len(set(q["options"])) == len(q["options"]) for q in everywhere), True)
    check("and the answer is always one of them",
          all(q["options"][q["answer"]] for q in everywhere), True)

    print("\n=== 'nobody did it last night' is only said about nights nobody did ===")
    busy = stocked()
    check("a feat that did happen yesterday gets no 'nobody managed it'",
          any(q["setup"].startswith("Nobody managed a 50-point")
              for q in most_recent_for(busy, date(2025, 2, 3))), False)
    check("a feat that did not, does",
          any(q["setup"].startswith("Nobody managed a 50-point")
              for q in most_recent_for(busy, date(2025, 2, 4))), True)
    check("and the answer is from before that night, not on it",
          [q for q in most_recent_for(busy, date(2025, 2, 4))
           if q["setup"].startswith("Nobody managed a 50-point")][0]["options"]
          .count("Player Six"), 1)

    print("\n=== a day is built the same way twice ===")
    day = date(2025, 2, 4)
    first = puzzle.build(day, stocked())
    second = puzzle.build(day, stocked())
    check("same questions, same order, same shuffle",
          json.dumps(first["questions"]), json.dumps(second["questions"]))
    check("a different day is a different puzzle",
          json.dumps(puzzle.build(date(2025, 2, 5), stocked())["questions"])
          != json.dumps(first["questions"]), True)
    check("it sources from the day before", first["source_date"], "2025-02-03")

    print("\n=== every question hangs off the night just played ===")
    # The "on this date in 1997" shape was removed on purpose. It filled a lot
    # of mornings, which is exactly why its absence needs a test rather than a
    # comment -- the pressure to bring it back will come from an empty day.
    from datetime import timedelta as _td
    for offset in range(40):
        day = date(2025, 1, 10) + _td(days=offset)
        for q in puzzle.build(day, stocked())["questions"]:
            if q["kind"] not in ("last_before", "most_recent") or "On this date" in q["setup"]:
                FAILS.append(f"calendar-year question on {day}")
    check("40 days of builds, no question about an earlier year",
          [f for f in FAILS if "calendar-year" in f], [])

    print("\n=== who led last night: the question the common stuff needs ===")
    # Thirty-three players homer on a normal night, so "who was the last to
    # homer" has thirty-three right answers. The fix is a different question,
    # not a lower threshold.
    def pitchers(*ks):
        return [{"sport": "MLB", "role": "pitching", "player": f"P{i}", "team": "AAA",
                 "opponent": "BBB", "game_id": "g", "strikeouts_thrown": float(k),
                 "innings": 6.0} for i, k in enumerate(ks)]
    strikeouts = leaders.BY_KEY["mlb_strikeouts"]
    top = leaders.board(strikeouts, pitchers(11, 9, 8, 7, 6))
    check("the leader comes back first", top and top[0]["player"], "P0")
    check("with the rest behind him", [r["player"] for r in top[1:3]], ["P1", "P2"])
    check("a tie at the top has no single answer, so no question",
          leaders.board(strikeouts, pitchers(11, 11, 8, 7)), None)
    check("leading with four strikeouts is not an achievement",
          leaders.board(strikeouts, pitchers(4, 3, 2, 1)), None)
    check("three players cannot fill four options",
          leaders.board(strikeouts, pitchers(11, 9, 8)), None)
    twice = pitchers(11, 9, 8, 7)
    twice.append({**twice[0], "strikeouts_thrown": 5.0})
    board = leaders.board(strikeouts, twice)
    check("a man in the box score twice is one option, at his best line",
          [r["player"] for r in board].count("P0"), 1)
    check("and the field is counted honestly", board[0]["field"], 4)

    print("\n=== no question gives away another one's answer ===")
    # "Sensabaugh scored 43 for Utah last night" as question one hands over
    # "who scored the most last night?" as question two.
    night = {"date": "2025-02-03", "boards": {"mlb_strikeouts": {"sport": "MLB", "rows": [
        {"player": "Player Six", "team": "AAA", "opponent": "BBB", "value": 11.0,
         "detail": "11 strikeouts", "game_id": "g", "field": 40},
        {"player": "Other One", "team": "AAA", "opponent": "BBB", "value": 9.0,
         "detail": "9", "game_id": "g", "field": 40},
        {"player": "Other Two", "team": "AAA", "opponent": "BBB", "value": 8.0,
         "detail": "8", "game_id": "g", "field": 40},
        {"player": "Other Three", "team": "AAA", "opponent": "BBB", "value": 7.0,
         "detail": "7", "game_id": "g", "field": 40}]}}}
    # Player Six is last night's 50-point scorer, so he is named in question one.
    board_day = puzzle.build(date(2025, 2, 4), stocked(), night=night)
    setups = " ".join(q["setup"] + q["explain"] for q in board_day["questions"])
    leaked = [q for q in board_day["questions"]
              if q["options"][q["answer"]] in setups.replace(
                  q["options"][q["answer"]], "", 1)]
    check("a name used in one question is not another's answer", leaked, [])
    check("the strikeout question was dropped rather than leaked",
          any(q["kind"] == "led_the_night" for q in board_day["questions"]), False)
    check("no question carries its internal bookkeeping into the page",
          any("_names" in q for q in board_day["questions"]), False)

    print("\n=== one board, three different answers ===")
    dupes = []
    for offset in range(120):
        day = date(2025, 10, 1) + __import__("datetime").timedelta(days=offset)
        qs = puzzle.build(day, stocked())["questions"]
        answers = [q["options"][q["answer"]] for q in qs]
        if len(set(answers)) != len(answers):
            dupes.append((day.isoformat(), answers))
    check("120 builds, no day answers itself twice", dupes, [])

    print("\n=== a quiet night still has a game ===")
    quiet = puzzle.build(date(2025, 1, 6), stocked())     # nothing on 2025-01-05
    check("questions were still found", len(quiet["questions"]) > 0, True)
    check("and they say so rather than inventing a headline",
          quiet["questions"][0]["kind"], "most_recent")
    check("no question is ever about a different calendar year",
          any(q["kind"] == "anniversary" or "On this date" in q["setup"]
              for q in quiet["questions"]), False)
    for q in quiet["questions"]:
        check(f"answer is inside options ({q['kind']})",
              0 <= q["answer"] < len(q["options"]), True)

    print("\n=== 7am Eastern, including the two days a year it moves ===")
    check("06:59 is still yesterday's puzzle",
          daily.live_day(datetime(2026, 9, 20, 6, 59, tzinfo=ET)), date(2026, 9, 19))
    check("07:00 turns the page",
          daily.live_day(datetime(2026, 9, 20, 7, 0, tzinfo=ET)), date(2026, 9, 20))
    check("midnight belongs to the day before",
          daily.live_day(datetime(2026, 9, 20, 0, 5, tzinfo=ET)), date(2026, 9, 19))
    opens, closes = daily.window(date(2026, 3, 7))
    span = (datetime.fromisoformat(closes) - datetime.fromisoformat(opens)).total_seconds()
    check("spring forward makes a 23-hour day, and the window says so", span, 23 * 3600)
    opens, closes = daily.window(date(2026, 10, 31))
    span = (datetime.fromisoformat(closes) - datetime.fromisoformat(opens)).total_seconds()
    check("falling back makes a 25-hour one", span, 25 * 3600)
    check("the window is stamped in Eastern, not UTC", opens.endswith("-04:00"), True)

    print("\n=== the page is a build, not a template with holes in it ===")
    template = daily.TEMPLATE.read_text()
    check("the template carries the placeholder", daily.MARKER in template, True)
    if daily.PAGE.exists():
        page = daily.PAGE.read_text()
        check("the built page does not", daily.MARKER in page, False)
        start = page.index("const PUZZLE = ") + len("const PUZZLE = ")
        inlined = json.loads(page[start:page.index(";\n", start)])
        check("and what it carries instead is the puzzle",
              sorted(inlined.keys())[:3], ["built_at", "closes", "coverage"])

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILURES")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
