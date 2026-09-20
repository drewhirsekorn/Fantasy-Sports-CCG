"""Fill the ledger by replaying past days through the same detector.

The daily build only ever sees yesterday, so on day one the ledger has no
predecessor to point at and the game has no questions. This walks a date
range through exactly the same feed and feat code the daily build uses, which
is the point: a backfilled entry and a live one are produced by one detector,
so the answer key cannot drift from the thing it is answering about.

    python3 -m trivia.backfill --sport NBA --since 2024-10-01
    python3 -m trivia.backfill --sport MLB --since 2025-03-01 --until 2025-11-02

Resumable and idempotent. Re-running a range re-reads it and adds nothing.
"""
from __future__ import annotations

import argparse
import pathlib
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

from trivia import feats, feed
from trivia.ledger import Ledger

# ESPN answers a date with no games instantly, so scanning the offseason costs
# one cheap request a day and saves the caller from knowing league calendars.


def _dates(start: date, end: date):
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


def scan_days(sport: str, days: list[date], *, workers: int = 12,
              keep_lines: bool = False) -> tuple[list[dict], set[str], list[dict]]:
    """Every feat over a list of dates, and which of those dates were fully read.

    Two fan-outs, not one. The first asks each date what it played, the second
    asks every game in every one of those dates for its box score -- because a
    Sunday with fourteen NFL games and a Tuesday with none are the same unit of
    work only if the games, not the days, are what gets parallelised.

    The second return value is the point. A date counts as read only when its
    scoreboard loaded AND every game on it gave up a box score; anything less
    and the caller must not claim to have searched it, because a box score
    that never arrived is indistinguishable from a quiet night and would make
    a later "nobody did it in between" false.
    """
    with ThreadPoolExecutor(max_workers=workers) as pool:
        boards = list(pool.map(lambda d: (d, _board(sport, d)), days))
        games = [(day, g) for day, board in boards if board is not None for g in board]
        scored = list(pool.map(lambda pair: _box(pair[1]), games))

    failed = {day.isoformat() for day, board in boards if board is None}
    for (day, _game), result in zip(games, scored):
        if result is None:
            failed.add(day.isoformat())
    read = {d.isoformat() for d in days} - failed
    lines = [line for game in scored if game for line in game.lines]
    # A month of box scores is a lot to hold; only the daily build, which
    # asks for one night, wants them back.
    return feats.detect(lines), read, (lines if keep_lines else [])


def _board(sport: str, day: date) -> list[feed.Game] | None:
    try:
        return [g for g in feed.scoreboard(sport, day) if g.final and g.counts]
    except feed.FeedError as exc:
        print(f"  ! {sport} {day}: {exc}", file=sys.stderr)
        return None


def _box(game: feed.Game):
    try:
        return feed.box_score(game)
    except feed.FeedError as exc:
        print(f"  ! {game.sport} {game.day} game {game.game_id}: {exc}", file=sys.stderr)
        return None


def runs(days: set[str]) -> list[tuple[date, date]]:
    """Contiguous stretches of read days. A hole ends a run and starts another."""
    out: list[list[date]] = []
    for iso in sorted(days):
        day = datetime.strptime(iso, "%Y-%m-%d").date()
        if out and day - out[-1][1] == timedelta(days=1):
            out[-1][1] = day
        else:
            out.append([day, day])
    return [(a, b) for a, b in out]


def _months(start: date, end: date):
    """[start, end] split at month boundaries, so a long run checkpoints."""
    chunk_start = start
    while chunk_start <= end:
        if chunk_start.month == 12:
            nxt = date(chunk_start.year + 1, 1, 1)
        else:
            nxt = date(chunk_start.year, chunk_start.month + 1, 1)
        yield chunk_start, min(end, nxt - timedelta(days=1))
        chunk_start = nxt


def backfill(sport: str, start: date, end: date, *, workers: int = 12,
             ledger: Ledger | None = None, verbose: bool = True,
             checkpoint: pathlib.Path | None | bool = True) -> Ledger:
    ledger = ledger or Ledger.load()
    added = 0
    unread: list[str] = []
    for chunk_start, chunk_end in _months(start, end):
        found, read, _ = scan_days(sport, list(_dates(chunk_start, chunk_end)), workers=workers)
        new = [o for o in found if ledger.add(o)]
        added += len(new)
        # Cover as we go, and only what was actually read: a run killed halfway
        # claims the months it finished, and a day the feed would not give up
        # is left out of the claim rather than assumed empty.
        keys = [f.key for f in feats.BY_SPORT.get(sport, ())]
        for run_start, run_end in runs(read):
            ledger.cover(sport, run_start, run_end, keys)
        unread += sorted({d.isoformat() for d in _dates(chunk_start, chunk_end)} - read)
        if checkpoint:
            ledger.save(checkpoint if isinstance(checkpoint, pathlib.Path) else None)
        if verbose:
            print(f"  {sport} {chunk_start:%Y-%m}: {len(new)} new", flush=True)
            for o in sorted(new, key=lambda o: o["date"]):
                print(f"     + {o['date']} {o['feat']:22s} {o['player']}", flush=True)
    if verbose:
        print(f"{sport} {start} -> {end}: {added} new", flush=True)
        if unread:
            print(f"  {len(unread)} date(s) could not be read and are not claimed: "
                  f"{', '.join(unread[:6])}{' ...' if len(unread) > 6 else ''}", flush=True)
    return ledger


def audit(ledger: Ledger, *, workers: int = 12, verbose: bool = True) -> list[dict]:
    """Re-check that every entry still comes from a game that counts.

    The exhibition filter arrived after the first scans did, so the ledger had
    All-Star lines in it. This re-reads each date the ledger touches, asks
    which games on it count, and drops entries whose game is positively known
    not to. A date that will not load drops nothing: "could not check" and
    "does not count" are different answers and only one of them is grounds for
    deleting evidence.
    """
    dates: dict[str, set[str]] = {}
    for o in ledger.occurrences:
        dates.setdefault(o["sport"], set()).add(o["date"])
    ok: set[str] = set()
    checked: set[tuple[str, str]] = set()
    for sport, days in dates.items():
        days = sorted(days)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            boards = pool.map(lambda d: (d, _all_games(sport, d)), days)
            for day, games in boards:
                if games is None:
                    continue                      # unreadable; leave its entries alone
                checked.add((sport, day))
                ok |= {f"espn:{g.game_id}" for g in games if g.counts}
    dropped = []
    kept = []
    for o in ledger.occurrences:
        known = (o["sport"], o["date"]) in checked
        if known and o.get("source", "").startswith("espn:") and o["source"] not in ok:
            dropped.append(o)
        else:
            kept.append(o)
    ledger.replace(kept)
    if verbose:
        for o in dropped:
            print(f"  - {o['date']} {o['feat']:22s} {o['player']}  (exhibition)")
        print(f"audit: {len(dropped)} dropped, {len(kept)} kept")
    return dropped


def _all_games(sport: str, day: str) -> list[feed.Game] | None:
    try:
        return feed.scoreboard(sport, datetime.strptime(day, "%Y-%m-%d").date())
    except feed.FeedError:
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sport", action="append", choices=list(feed.SPORTS),
                    help="repeatable; defaults to all three")
    ap.add_argument("--since", help="YYYY-MM-DD; required unless --merge")
    ap.add_argument("--until", help="YYYY-MM-DD, default yesterday")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--audit", action="store_true",
                    help="re-check every entry's game still counts, and drop exhibitions")
    ap.add_argument("--merge", nargs="+", metavar="PATH",
                    help="fold these ledger files into the main one and stop")
    ap.add_argument("--ledger", help="write somewhere other than data/trivia_ledger.json; "
                                     "lets three sports scan in parallel and merge after")
    args = ap.parse_args(argv)

    if args.audit:
        out = pathlib.Path(args.ledger) if args.ledger else None
        ledger = Ledger.load(out)
        audit(ledger, workers=args.workers, verbose=not args.quiet)
        ledger.save(out)
        print(ledger.summary())
        return 0

    if args.merge:
        target = Ledger.load(pathlib.Path(args.ledger) if args.ledger else None)
        for path in args.merge:
            added = target.merge(Ledger.load(pathlib.Path(path)))
            print(f"  {path}: +{added}")
        target.save(pathlib.Path(args.ledger) if args.ledger else None)
        print(target.summary())
        return 0

    if not args.since:
        ap.error("--since is required unless --merge or --audit is given")
    start = datetime.strptime(args.since, "%Y-%m-%d").date()
    end = (datetime.strptime(args.until, "%Y-%m-%d").date() if args.until
           else date.today() - timedelta(days=1))
    out = pathlib.Path(args.ledger) if args.ledger else None
    ledger = Ledger.load(out)
    for sport in (args.sport or list(feed.SPORTS)):
        backfill(sport, start, end, workers=args.workers, ledger=ledger,
                 verbose=not args.quiet, checkpoint=out or True)
        ledger.save(out)       # checkpoint per sport, so a kill keeps the work
    print(ledger.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
