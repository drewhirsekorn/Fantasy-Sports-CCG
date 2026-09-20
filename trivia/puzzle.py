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

  last_before   something happened yesterday -- who did it before?
  anniversary   it happened on this date in an earlier year -- who was it?
  most_recent   nothing happened yesterday -- when did it last happen?

The first is the game. The other two are what keeps an All-Star break, a
February Tuesday and the second week of July from being blank.
"""
from __future__ import annotations

import hashlib
import random
from datetime import date, datetime, timedelta, timezone

from trivia import feats
from trivia.ledger import Ledger

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
    }


def anniversary(ledger: Ledger, source_day: date, rng: random.Random) -> list[dict]:
    """Same calendar date, earlier year. Rarest feat first."""
    out = []
    candidates = ledger.on_day(source_day.month, source_day.day,
                               before_year=source_day.year)
    candidates.sort(key=lambda o: (feats.BY_KEY[o["feat"]].rank, -o["value"]))
    for occurrence in candidates:
        feat = feats.BY_KEY[occurrence["feat"]]
        if _ambiguous(ledger, occurrence):
            continue
        wrong = _distractors(ledger, feat.key, rng=rng, exclude={occurrence["player"]})
        if len(wrong) < OPTIONS - 1:
            continue
        built = _multiple_choice(occurrence["player"], wrong, rng)
        if not built:
            continue
        options, answer = built
        year = occurrence["date"][:4]
        out.append({
            "kind": "anniversary",
            "sport": feat.sport,
            "setup": f"On this date in {year}, a {feat.sport} player had {feat.label}.",
            "prompt": f"Who was it? ({_value(occurrence)} against {occurrence['opponent']})",
            "options": options,
            "answer": answer,
            "explain": f"{occurrence['player']}, {feats.headline(occurrence)}, {_pretty(occurrence['date'])}.",
            "links": [l for l in (_source_link(occurrence),) if l],
        })
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
    for feat in sorted(feats.BY_SPORT.get(sport, ()), key=lambda f: f.rank):
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
            "kind": "most_recent",
            "sport": sport,
            "setup": f"Nobody managed {feat.label} in the {sport} yesterday.",
            "prompt": f"Who had the most recent one?",
            "options": options,
            "answer": answer,
            "explain": (f"{previous['player']} — {_value(previous)} for {previous['team']} "
                        f"vs {previous['opponent']} on {_pretty(previous['date'])}."),
            "links": [l for l in (_source_link(previous),) if l],
        })
    return out


# ------------------------------------------------------------------- build
def build(puzzle_day: date, ledger: Ledger, *, questions: int = QUESTIONS) -> dict:
    """The puzzle that goes live at 07:00 ET on `puzzle_day`.

    Sources from the day before, which is the day whose games are all final by
    the time anyone opens the page.
    """
    source_day = puzzle_day - timedelta(days=1)
    rng = _rng(puzzle_day)
    chosen: list[dict] = []
    used_sports: set[str] = set()

    # 1. Yesterday's feats, rarest first, at most one per sport so a wild night
    #    in one league does not take the whole board.
    yesterday = [o for o in ledger.occurrences if o["date"] == source_day.isoformat()]
    yesterday.sort(key=lambda o: (feats.BY_KEY[o["feat"]].rank, -o["value"]))
    for occurrence in yesterday:
        if len(chosen) >= questions or occurrence["sport"] in used_sports:
            continue
        question = last_before(ledger, occurrence, rng)
        if question:
            chosen.append(question)
            used_sports.add(occurrence["sport"])

    # 2. Same date, earlier years.
    if len(chosen) < questions:
        for question in anniversary(ledger, source_day, rng):
            if len(chosen) >= questions:
                break
            if question["sport"] in used_sports and len(used_sports) < len(SPORTS):
                continue
            chosen.append(question)
            used_sports.add(question["sport"])

    # 3. Whatever the ledger can still answer, preferring leagues not yet asked
    #    about and the sports that were actually playing yesterday.
    if len(chosen) < questions:
        order = sorted(SPORTS, key=lambda s: (s in used_sports,
                                              not ledger.covers(s, source_day)))
        for sport in order:
            for question in most_recent(ledger, sport, source_day, rng):
                if len(chosen) >= questions:
                    break
                if any(q["setup"] == question["setup"] and q["prompt"] == question["prompt"]
                       for q in chosen):
                    continue
                chosen.append(question)
                used_sports.add(sport)

    for i, question in enumerate(chosen, 1):
        question["id"] = f"{puzzle_day.isoformat()}-{i}"

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
