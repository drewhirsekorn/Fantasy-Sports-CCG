"""Yesterday's box scores, turned into today's three questions.

Every question here is derived. Nothing is typed in from memory: the setup is
a feat the detector found in a box score, the answer is another row of the
ledger, and the wrong options are real players who have done the same thing on
other nights. That is deliberate. A trivia game whose facts were hand-entered
is only as good as whoever entered them, and a game that has to be right every
morning without a human in the loop cannot afford that.

It also means a question can be refused. `Ledger.previous` returns nothing
when the window between two occurrences was never scanned, and a date shared
by two players is dropped rather than asked, because "who was the last" has
two right answers that night and a four-option question has room for one. The
day loses a question; it never gains a wrong one.

Three shapes, in the order they are tried:

  last_before    something rare happened last night -- who did it before?
  led_the_night  who topped the box scores last night?
  most_recent    nothing happened last night -- when did it last happen?

The first is the best question when there is one, and there often is not: a
50-point game happens twenty times a season. The second is what makes a
morning never empty, because every night with games has someone who led it --
most strikeouts, most rebounds, most yards. It exists because lowering the
feat thresholds does not work: thirty-three players homer on a normal night,
so "who was the last to go deep" has thirty-three right answers. The common
ground needs a different question, not a lower bar. The third is the
fallback for a night nobody played at all.

There was a third, asking about the same calendar date in an earlier year.
It filled a lot of mornings and it is gone on purpose: the game is about last
night, and a question that opens "on this date in 2004" is a different game
wearing the same page. Every question now hangs off the night just played --
either something happened, or nothing did and that absence is the setup.
"""
from __future__ import annotations

import hashlib
import random
from datetime import date, datetime, timedelta, timezone

from trivia import feats, leaders
from trivia.ledger import Ledger

# All three initialisms take "an" when read aloud -- en-bee-ay, en-eff-el,
# em-el-bee -- and only the NBA and NFL take a definite article comfortably.
LEAGUE = {"NBA": "the NBA", "NFL": "the NFL", "MLB": "the majors"}

OPTIONS = 4
QUESTIONS = 3
SPORTS = ("NBA", "NFL", "MLB")


def _rng(day: date, salt: str = "") -> random.Random:
    """Deterministic per puzzle date, so two builds of one day agree exactly."""
    seed = hashlib.sha256(f"{day.isoformat()}|{salt}".encode()).hexdigest()
    return random.Random(int(seed[:16], 16))


def _pretty(day: str) -> str:
    d = datetime.strptime(day[:10], "%Y-%m-%d").date()
    return f"{d:%B} {d.day}, {d.year}"


def _value(occurrence: dict) -> str:
    feat = feats.BY_KEY[occurrence["feat"]]
    return f"{occurrence['value']:.0f} {feat.unit}".strip()


# --------------------------------------------------------------- distractors
def _distractors(ledger: Ledger, feat_key: str, *, exclude: set[str],
                 rng: random.Random, want: int = OPTIONS - 1) -> list[str]:
    """Wrong options that are real.

    Drawn first from players who have done this exact feat -- someone who has
    had a 50-point game is a plausible answer to "who was the last" in a way
    that a random name is not -- and topped up from the same sport's other
    feats when one feat's pool is too shallow.
    """
    feat = feats.BY_KEY[feat_key]
    taken = set(exclude)
    pool = [p for p in ledger.players(feat_key) if p not in taken]
    rng.shuffle(pool)
    picked = pool[:want]
    taken |= set(picked)
    if len(picked) < want:
        # A shallow feat borrows from the same league's other feats. A player
        # can hold several of them, so this dedupes as it goes -- two options
        # with the same name is a question with three answers.
        wider: list[str] = []
        for other in feats.BY_SPORT.get(feat.sport, ()):
            if other.key == feat_key:
                continue
            for name in ledger.players(other.key):
                if name not in taken:
                    taken.add(name)
                    wider.append(name)
        rng.shuffle(wider)
        picked += wider[:want - len(picked)]
    return picked


def _multiple_choice(answer: str, wrong: list[str],
                     rng: random.Random) -> tuple[list[str], int] | None:
    """Four distinct names, one of them right. None if that cannot be met.

    The last line of defence rather than the first: a duplicate option would
    silently make a second choice correct, and no amount of downstream care
    can recover from that, so a question that cannot be built cleanly is not
    built at all.
    """
    options = [answer] + list(wrong)
    if len(set(options)) != len(options) or len(options) != OPTIONS:
        return None
    rng.shuffle(options)
    return options, options.index(answer)


def _source_link(occurrence: dict) -> dict | None:
    source = occurrence.get("source") or ""
    if not source.startswith("espn:") or not source[5:]:
        return None
    sport = occurrence["sport"]
    path = {"NBA": "nba", "NFL": "nfl", "MLB": "mlb"}[sport]
    return {"label": f"{_pretty(occurrence['date'])} box score",
            "url": f"https://www.espn.com/{path}/boxscore/_/gameId/{source[5:]}"}


# ----------------------------------------------------------------- shapes
def _ambiguous(ledger: Ledger, occurrence: dict) -> bool:
    """Did anyone else do the same thing the same day? Then 'the last' is two people."""
    same_day = [o for o in ledger.of(occurrence["feat"])
                if o["date"] == occurrence["date"] and o["player"] != occurrence["player"]]
    return bool(same_day)


def last_before(ledger: Ledger, occurrence: dict, rng: random.Random) -> dict | None:
    feat = feats.BY_KEY[occurrence["feat"]]
    day = datetime.strptime(occurrence["date"], "%Y-%m-%d").date()
    previous = ledger.previous(feat.key, day, sport=feat.sport)
    if not previous or _ambiguous(ledger, previous):
        return None
    if previous["player"] == occurrence["player"]:
        # He did it again. Still a fine question, but the setup has to say so.
        pass
    wrong = _distractors(ledger, feat.key, rng=rng,
                         exclude={previous["player"], occurrence["player"]})
    if len(wrong) < OPTIONS - 1:
        return None
    built = _multiple_choice(previous["player"], wrong, rng)
    if not built:
        return None
    options, answer = built
    gap = (day - datetime.strptime(previous["date"], "%Y-%m-%d").date()).days
    return {
        "kind": "last_before",
        "sport": feat.sport,
        "setup": feats.headline(occurrence) + " yesterday.",
        "prompt": feat.asks.format(player=occurrence["player"]),
        "options": options,
        "answer": answer,
        "explain": (f"{previous['player']} — {_value(previous)} for {previous['team']} "
                    f"on {_pretty(previous['date'])}, {gap:,} days earlier."),
        "links": [l for l in (_source_link(occurrence), _source_link(previous)) if l],
        "_names": set(options) | {occurrence["player"], previous["player"]},
    }


def led_the_night(night: dict, rng: random.Random) -> list[dict]:
    """Who topped each of last night's leaderboards.

    Everything here is decided in leaders.py, which has already dropped the
    boards with a tied leader and the ones where leading was not an
    achievement. What is left is a board whose first row is the single right
    answer and whose other rows are men who played the same night and did
    less -- so every wrong option is wrong for a checkable reason.
    """
    out = []
    for key, data in (night.get("boards") or {}).items():
        leader = leaders.BY_KEY.get(key)
        rows = data.get("rows") or []
        if not leader or len(rows) < OPTIONS:
            continue
        answer_row, rest = rows[0], rows[1:]
        wrong = [r["player"] for r in rest if r["player"] != answer_row["player"]]
        rng.shuffle(wrong)
        built = _multiple_choice(answer_row["player"], wrong[:OPTIONS - 1], rng)
        if not built:
            continue
        options, answer = built
        field = answer_row.get("field") or len(rows)
        out.append({
            "kind": "led_the_night",
            "sport": leader.sport,
            "setup": f"Last night's box scores list {field} {leader.noun}.",
            "prompt": leader.asks,
            "options": options,
            "answer": answer,
            "explain": f"{answer_row['player']} — {answer_row['detail']}.",
            "_names": set(options) | {answer_row["player"]},
            "links": [l for l in (_source_link({
                "sport": leader.sport, "date": night.get("date", ""),
                "source": f"espn:{answer_row.get('game_id','')}"}),) if l],
        })
    rng.shuffle(out)
    return out


def most_recent(ledger: Ledger, sport: str, source_day: date, rng: random.Random) -> list[dict]:
    """Nothing last night. So: who did it last, before last night?

    "Nobody managed it" is a claim about last night, and the build only gets
    to make it about a feat that did not in fact happen -- otherwise the setup
    denies the very game the answer comes from. `previous` carries the other
    half: it refuses outside a scanned window, so this is never asserted about
    a night the detector did not read.
    """
    out = []
    happened = {o["feat"] for o in ledger.occurrences if o["date"] == source_day.isoformat()}
    candidates = list(feats.BY_SPORT.get(sport, ()))
    for feat in candidates:
        if feat.key in happened:
            continue
        previous = ledger.previous(feat.key, source_day, sport=sport)
        if not previous or _ambiguous(ledger, previous):
            continue
        wrong = _distractors(ledger, feat.key, rng=rng, exclude={previous["player"]})
        if len(wrong) < OPTIONS - 1:
            continue
        built = _multiple_choice(previous["player"], wrong, rng)
        if not built:
            continue
        options, answer = built
        out.append({
            "_when": previous["date"],
            "kind": "most_recent",
            "sport": sport,
            "setup": f"Nobody managed {feat.label} in {LEAGUE[sport]} yesterday.",
            "prompt": f"Who had the most recent one?",
            "options": options,
            "answer": answer,
            "explain": (f"{previous['player']} — {_value(previous)} for {previous['team']} "
                        f"vs {previous['opponent']} on {_pretty(previous['date'])}."),
            "_names": set(options) | {previous["player"]},
            "links": [l for l in (_source_link(previous),) if l],
        })

    # Freshest first, then shuffled among the freshest few.
    #
    # Ranking by rarity picked the rarest feat every time, and the rarest feat
    # is by definition the one whose answer is oldest -- so the same question
    # with the same eight-month-old answer came back every quiet night. Sorting
    # by how recently the feat last happened does the opposite: the answer is
    # something from this month, and it rotates on its own as the season runs.
    # The shuffle over the top few keeps consecutive quiet days from being
    # identical, and is seeded by the puzzle date so a day still builds twice
    # the same way.
    out.sort(key=lambda q: q["_when"], reverse=True)
    head = out[:4]
    rng.shuffle(head)
    out = head + out[4:]
    for question in out:
        question.pop("_when", None)
    return out


def _playing(ledger: Ledger, sport: str, source_day: date, *, within: int = 21) -> bool:
    """Is this league in season?

    Asked of the ledger rather than a calendar, because the ledger is the
    thing that knows: a league that produced a feat in the last three weeks
    was playing. It decides ordering only -- a wrong guess costs a question
    its place in the list, never its correctness.
    """
    floor = (source_day - timedelta(days=within)).isoformat()
    return any(o["sport"] == sport and floor <= o["date"] <= source_day.isoformat()
               for o in ledger.occurrences)


# ------------------------------------------------------------------- build
def build(puzzle_day: date, ledger: Ledger, *, questions: int = QUESTIONS,
          night: dict | None = None) -> dict:
    """The puzzle that goes live at 07:00 ET on `puzzle_day`.

    Sources from the day before, which is the day whose games are all final by
    the time anyone opens the page.
    """
    source_day = puzzle_day - timedelta(days=1)
    rng = _rng(puzzle_day)
    chosen: list[dict] = []
    used_sports: set[str] = set()
    # Two questions with the same right answer is a thin board: get one and
    # you have the other, and the day is really two questions long.
    used_answers: set[str] = set()
    # Worse than a repeated answer is a leaked one. "Sensabaugh scored 43 for
    # Utah last night" as the setup of question one hands over "who scored the
    # most last night?" as question two. The questions are different; the
    # second is no longer a question. So anyone an earlier question named --
    # in its setup, its options or its explanation -- cannot be a later
    # question's answer.
    named: set[str] = set()

    def take(question: dict | None) -> bool:
        if not question:
            return False
        answer = question["options"][question["answer"]]
        if answer in used_answers or answer in named:
            return False
        if any(q["setup"] == question["setup"] and q["prompt"] == question["prompt"]
               for q in chosen):
            return False
        chosen.append(question)
        used_sports.add(question["sport"])
        used_answers.add(answer)
        named.update(question.pop("_names", ()) or ())
        named.add(answer)
        return True

    # 1. Yesterday's feats, rarest first. One per league on the first pass so
    #    a wild night in one does not take the whole board -- then a second
    #    pass over what is left, because three things that actually happened
    #    last night beat two of them plus a question about a night nothing did.
    #
    #    A player is only asked about once. The feat table is tiered, so a
    #    50-point game is also a 40-point game, and without this the same man
    #    headlines two questions in a row.
    yesterday = [o for o in ledger.occurrences if o["date"] == source_day.isoformat()]
    yesterday.sort(key=lambda o: (feats.BY_KEY[o["feat"]].rank, -o["value"]))
    used_players: set[str] = set()
    # Capped at two on the first sweep, so a board is never three variations
    # of "who did it before" when last night also had leaders worth asking
    # about. The rest of them come back below if the leaders run out.
    for first_pass in (True,):
        for occurrence in yesterday:
            if len(chosen) >= min(questions - 1, questions):
                break
            if occurrence["player"] in used_players:
                continue
            if first_pass and occurrence["sport"] in used_sports:
                continue
            if take(last_before(ledger, occurrence, rng)):
                used_players.add(occurrence["player"])

    # 2. Who led last night. This is the shape that makes a morning never
    #    empty, so it is tried before falling back to nights nobody played.
    if night and len(chosen) < questions:
        for question in led_the_night(night, rng):
            if len(chosen) >= questions:
                break
            take(question)

    # 3. Any remaining feats from last night, now that the leaders have had
    #    their turn at the board.
    if len(chosen) < questions:
        for occurrence in yesterday:
            if len(chosen) >= questions:
                break
            if occurrence["player"] in used_players:
                continue
            if take(last_before(ledger, occurrence, rng)):
                used_players.add(occurrence["player"])

    # 4. Whatever the ledger can still answer. Taken one league at a time so a
    #    September morning -- NBA dark, NFL two games in, MLB the only thing
    #    that played -- does not hand back three NBA questions in a row.
    if len(chosen) < questions:
        pools = {sport: most_recent(ledger, sport, source_day, rng) for sport in SPORTS}
        # A league in its offseason is exhausted before it is interesting. In
        # mid-July the NFL has not played for five months, and "nobody managed
        # four rushing touchdowns in the NFL yesterday" is true, deadpan and
        # faintly ridiculous. So the leagues that were playing get asked
        # about first, and a dark one is only reached for if the board would
        # otherwise be short.
        active = [s for s in SPORTS if _playing(ledger, s, source_day)]
        dark = [s for s in SPORTS if s not in active]
        for tier in (active, dark):
            order = sorted(tier, key=lambda s: s in used_sports)
            while len(chosen) < questions and any(pools[s] for s in order):
                took = False
                for sport in order:
                    if len(chosen) >= questions or not pools[sport]:
                        continue
                    if take(pools[sport].pop(0)):
                        took = True
                if not took:
                    break

    for i, question in enumerate(chosen, 1):
        question["id"] = f"{puzzle_day.isoformat()}-{i}"
        question.pop("_names", None)

    # A rebuild of the same day with a deeper ledger can produce a different
    # set. The stamp lets the page notice that and start the day over rather
    # than scoring old answers against new questions.
    stamp = hashlib.sha256("|".join(
        q["prompt"] + "".join(q["options"]) for q in chosen).encode()).hexdigest()[:12]

    return {
        "version": 1,
        "date": puzzle_day.isoformat(),
        "stamp": stamp,
        "source_date": source_day.isoformat(),
        "opens": f"{puzzle_day.isoformat()}T07:00:00-04:00",   # replaced by build.py
        "questions": chosen,
        "coverage": ledger.coverage,
        "built_at": datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)
                    .isoformat() + "Z",
    }
