"""The ledger: every feat the detector has ever seen, and how far it has looked.

This file is the answer key. A question of the form "who was the last player
to do this before yesterday" is only honest if nobody did it in between, so
the ledger records not just what it found but the window it searched. A feat
that happened inside a covered window and has a predecessor inside the same
window yields an answer that is true by construction; anything else is not
asked.

That is the whole correctness argument, and it is why coverage is stored per
sport rather than per feat: the detector runs every feat over every day it
scans, so "this sport is covered from A to B" already means "every feat of
that sport is covered from A to B".

Coverage is a list of windows rather than one, because the daily job will
miss a morning eventually -- a workflow outage, a feed that will not answer --
and a single window would have to choose between forgetting everything before
the gap and lying about the gap. Segments cost a missed day exactly one day.

The daily build appends to it, so the ledger deepens on its own: tomorrow's
question is answered by an entry today's run wrote.
"""
from __future__ import annotations

import json
import pathlib
from datetime import date, datetime, timedelta

_DAY = timedelta(days=1)
PATH = pathlib.Path(__file__).resolve().parent.parent / "data" / "trivia_ledger.json"
VERSION = 1


def _day(value: str) -> date:
    return datetime.strptime(value[:10], "%Y-%m-%d").date()


def _windows(value) -> list[dict]:
    """Accept either shape. Older ledgers stored one window, not a list."""
    if isinstance(value, dict):
        return [dict(value)]
    return [dict(w) for w in (value or [])]


def _merge(windows: list[tuple[date, date]]) -> list[dict]:
    """Sort, and fuse anything touching or adjoining. A real gap survives."""
    out: list[list[date]] = []
    for start, through in sorted(windows):
        if out and start - out[-1][1] <= _DAY:
            out[-1][1] = max(out[-1][1], through)
        else:
            out.append([start, through])
    return [{"from": a.isoformat(), "through": b.isoformat()} for a, b in out]


class Ledger:
    def __init__(self, data: dict | None = None):
        data = data or {}
        self.version = data.get("version", VERSION)
        # sport -> [{"from": iso, "through": iso}, ...], disjoint and sorted.
        self.coverage: dict[str, list[dict]] = {
            sport: _windows(value) for sport, value in (data.get("coverage") or {}).items()
        }
        self.occurrences: list[dict] = data.get("occurrences") or []
        self._seen = {self._identity(o) for o in self.occurrences}

    # ------------------------------------------------------------- storage
    @classmethod
    def load(cls, path: pathlib.Path | None = None) -> "Ledger":
        path = path or PATH
        if not path.exists():
            return cls()
        return cls(json.loads(path.read_text()))

    def save(self, path: pathlib.Path | None = None) -> pathlib.Path:
        path = path or PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        self.occurrences.sort(key=lambda o: (o["date"], o["feat"], o["player"]))
        path.write_text(json.dumps({
            "version": self.version,
            "coverage": dict(sorted(self.coverage.items())),
            "occurrences": self.occurrences,
        }, indent=1, sort_keys=False) + "\n")
        return path

    # -------------------------------------------------------------- writes
    @staticmethod
    def _identity(o: dict) -> tuple:
        return (o["feat"], o["date"], o["player"], o.get("source", ""))

    def add(self, occurrence: dict) -> bool:
        """Append one occurrence. False if the ledger already had it."""
        ident = self._identity(occurrence)
        if ident in self._seen:
            return False
        self._seen.add(ident)
        self.occurrences.append(occurrence)
        return True

    def merge(self, other: "Ledger") -> int:
        """Absorb another ledger. Three sports can then scan in parallel.

        Coverage is merged through `cover`, so a hole between two windows is
        still refused here -- merging never manufactures a claim that neither
        side made.
        """
        added = sum(1 for o in other.occurrences if self.add(dict(o)))
        for sport, windows in other.coverage.items():
            for w in windows:
                self.cover(sport, _day(w["from"]), _day(w["through"]))
        return added

    def replace(self, occurrences: list[dict]) -> None:
        """Swap the contents wholesale. Used by the audit, which removes rows."""
        self.occurrences = list(occurrences)
        self._seen = {self._identity(o) for o in self.occurrences}

    def cover(self, sport: str, start: date, through: date) -> None:
        """Record that every day in [start, through] was scanned for `sport`.

        Adjacent is not a gap: a scan of January followed by a scan of
        February leaves nothing unsearched between them, so the two fuse. A
        day nobody looked at does break the claim, and stays broken -- the
        window on either side of it is kept, and nothing spans it.
        """
        existing = [(_day(w["from"]), _day(w["through"]))
                    for w in self.coverage.get(sport, [])]
        self.coverage[sport] = _merge(existing + [(start, through)])

    def window_for(self, sport: str, day: date) -> tuple[date, date] | None:
        """The scanned window containing `day`, if any."""
        for w in self.coverage.get(sport, []):
            start, through = _day(w["from"]), _day(w["through"])
            if start <= day <= through:
                return start, through
        return None

    # --------------------------------------------------------------- reads
    def covers(self, sport: str, day: date) -> bool:
        return self.window_for(sport, day) is not None

    def of(self, feat: str) -> list[dict]:
        return sorted((o for o in self.occurrences if o["feat"] == feat),
                      key=lambda o: o["date"])

    def previous(self, feat: str, before: date, *, sport: str) -> dict | None:
        """The last occurrence of `feat` strictly before `before`.

        Returns None unless the whole span from that occurrence to `before`
        was scanned -- an unscanned gap could hide a more recent one, and a
        question nobody can vouch for is worse than no question.
        """
        window = self.window_for(sport, before)
        if not window:
            return None
        start, _through = window
        earlier = [o for o in self.of(feat) if _day(o["date"]) < before]
        if not earlier:
            return None
        candidate = earlier[-1]
        return candidate if _day(candidate["date"]) >= start else None

    def on_day(self, month: int, day: int, *, before_year: int) -> list[dict]:
        """Occurrences on this calendar day in an earlier year -- anniversaries."""
        out = []
        for o in self.occurrences:
            d = _day(o["date"])
            if d.month == month and d.day == day and d.year < before_year:
                out.append(o)
        return sorted(out, key=lambda o: o["date"], reverse=True)

    def players(self, feat: str) -> list[str]:
        """Everyone who has ever done it -- the pool that distractors come from."""
        seen, out = set(), []
        for o in self.of(feat):
            if o["player"] not in seen:
                seen.add(o["player"])
                out.append(o["player"])
        return out

    def summary(self) -> str:
        by_feat: dict[str, int] = {}
        for o in self.occurrences:
            by_feat[o["feat"]] = by_feat.get(o["feat"], 0) + 1
        lines = [f"{len(self.occurrences)} occurrences over {len(by_feat)} feats"]
        for sport, windows in sorted(self.coverage.items()):
            spans = ", ".join(f"{w['from']} -> {w['through']}" for w in windows)
            lines.append(f"  {sport}: {spans}")
        for feat, count in sorted(by_feat.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {count:4d}  {feat}")
        return "\n".join(lines)
