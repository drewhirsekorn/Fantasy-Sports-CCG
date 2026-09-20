"""The 07:00 ET job: read yesterday, extend the ledger, bake today's page.

One command does the whole day, and doing it twice does nothing the second
time. That matters more than it sounds: GitHub's scheduler runs in UTC and
does not know about daylight saving, so the workflow fires at both 11:00 and
12:00 UTC and lets this decide which one is actually 07:00 in New York.

    python3 -m trivia.build                 # the daily job
    python3 -m trivia.build --force         # rebuild today even if it exists
    python3 -m trivia.build --date 2025-07-05 --no-scan   # any past day, offline

What it writes:
    data/trivia_ledger.json   yesterday's feats, appended
    web/trivia_data.json      today's questions, pinned so the page is a build
    web/trivia.html           the standalone page, data inlined

Yesterday's questions are not archived anywhere, by design. The page holds one
day and the player holds their own record; see web/trivia.template.html.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from trivia import backfill, feed, puzzle
from trivia.ledger import Ledger

ET = ZoneInfo("America/New_York")
ROLLOVER_HOUR = 7

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "web" / "trivia.template.html"
DATA = ROOT / "web" / "trivia_data.json"
PAGE = ROOT / "web" / "trivia.html"
MARKER = "/*__PUZZLE__*/null"


def live_day(now: datetime | None = None) -> date:
    """Which puzzle should be on the page right now.

    Before 07:00 in New York, yesterday's puzzle is still the live one; the
    build run at 06:55 must not publish tomorrow.
    """
    now = now or datetime.now(ET)
    now = now.astimezone(ET)
    return now.date() if now.hour >= ROLLOVER_HOUR else now.date() - timedelta(days=1)


def window(day: date) -> tuple[str, str]:
    """(opens, closes) in ET. Exactly 24 hours, DST included: on the spring
    forward the day is 23 hours long, and the page says so rather than
    counting down to a wall-clock time that never happens."""
    opens = datetime(day.year, day.month, day.day, ROLLOVER_HOUR, tzinfo=ET)
    nxt = day + timedelta(days=1)
    closes = datetime(nxt.year, nxt.month, nxt.day, ROLLOVER_HOUR, tzinfo=ET)
    return opens.isoformat(), closes.isoformat()


def scan(ledger: Ledger, day: date, *, workers: int = 12, verbose: bool = True) -> int:
    """Put one day's feats into the ledger, per sport, extending coverage.

    A sport whose feed would not answer is not covered for that day. The
    puzzle then simply has one fewer place to draw from, which is the right
    failure: the alternative is a ledger that says it searched a night it
    never saw.
    """
    added = 0
    for sport in feed.SPORTS:
        found, read = backfill.scan_days(sport, [day], workers=workers)
        new = [o for o in found if ledger.add(o)]
        added += len(new)
        if day.isoformat() in read:
            ledger.cover(sport, day, day)
        elif verbose:
            print(f"  ! {sport} {day} could not be read; not claiming it", file=sys.stderr)
        if verbose:
            for o in new:
                print(f"  + {o['date']} {o['feat']:22s} {o['player']}")
    return added


def bake(data: dict) -> int:
    template = TEMPLATE.read_text()
    if MARKER not in template:
        sys.exit(f"{TEMPLATE} has no {MARKER} placeholder")
    # The puzzle is inlined inside a <script>, so a "</" anywhere in it would
    # end the block early. Nothing in a box score should contain one, which is
    # exactly why it is cheap to make sure.
    inlined = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    page = template.replace(MARKER, inlined)
    PAGE.write_text(page)
    return len(page)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", help="build this puzzle day instead of the live one")
    ap.add_argument("--no-scan", action="store_true",
                    help="use the ledger as it stands; touches no network")
    ap.add_argument("--force", action="store_true",
                    help="rebuild even if this day is already published")
    ap.add_argument("--questions", type=int, default=puzzle.QUESTIONS)
    ap.add_argument("--workers", type=int, default=12)
    args = ap.parse_args(argv)

    day = (datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else live_day())
    source = day - timedelta(days=1)

    if DATA.exists() and not args.force:
        current = json.loads(DATA.read_text())
        if current.get("date") == day.isoformat():
            print(f"{day} is already published ({len(current.get('questions', []))} "
                  f"questions). Nothing to do.")
            return 0

    ledger = Ledger.load()
    if not args.no_scan:
        print(f"scanning {source} ...")
        added = scan(ledger, source, workers=args.workers)
        ledger.save()
        print(f"  {added} new occurrences; ledger holds {len(ledger.occurrences)}")

    data = puzzle.build(day, ledger, questions=args.questions)
    if not data["questions"]:
        print("no question could be built from the ledger -- refusing to publish "
              "an empty day. Deepen the ledger with trivia.backfill.", file=sys.stderr)
        return 1

    opens, closes = window(day)
    data["opens"], data["closes"] = opens, closes
    DATA.write_text(json.dumps(data, indent=1) + "\n")
    size = bake(data)

    print(f"{day}  {len(data['questions'])} questions, live {opens} -> {closes}")
    for q in data["questions"]:
        print(f"  [{q['sport']}/{q['kind']}] {q['setup']} {q['prompt']}")
        print(f"      -> {q['options'][q['answer']]}")
    print(f"web/trivia.html  {size/1024:.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
