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

Each window also names the feats it was scanned for. Adding a feat to the
catalogue would otherwise be silently retroactive: the old windows would
claim to have searched for something the detector could not yet see, and the
first question about it would have a wrong answer. Instead a new feat simply
has no coverage until someone backfills it, and until then it is not asked
about.

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


def _merge(windows: list[tuple[date, date, tuple[str, ...]]]) -> list[dict]:
    """Sort and fuse anything touching or adjoining. A real gap survives.

    Windows only fuse when they were scanned for the same feats, so a range
    re-read with a wider catalogue sits alongside the older claim instead of
    inheriting its dates.
    """
    out: list[dict] = []
    by_feats: dict[tuple[str, ...], list[list[date]]] = {}
    for start, through, keys in sorted(windows, key=lambda w: (w[2], w[0], w[1])):
        runs = by_feats.setdefault(keys, [])
        if runs and start - runs[-1][1] <= _DAY:
            runs[-1][1] = max(runs[-1][1], through)
        else:
            runs.append([start, through])
    for keys, runs in by_feats.items():
        for a, b in runs:
            out.append({"from": a.isoformat(), "through": b.isoformat(),
                        "feats": list(keys)})
    # A re-scan with a wider catalogue says everything the narrower claim said
    # and more, so the narrower one is redundant rather than wrong. Dropping it
    # keeps the ledger from accumulating a window per catalogue revision.
    kept = []
    for w in out:
        covered = any(
            other is not w
            and other["from"] <= w["from"] and other["through"] >= w["through"]
            and set(w["feats"]) <= set(other["feats"])
            and (set(w["feats"]) != set(other["feats"])
                 or (other["from"], other["through"]) != (w["from"], w["through"]))
            for other in out)
        if not covered:
            kept.append(w)
    kept.sort(key=lambda w: (w["from"], w["through"], w["feats"]))
    return kept


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
                self.cover(sport, _day(w["from"]), _day(w["through"]), w.get("feats"))
        return added

    def replace(self, occurrences: list[dict]) -> None:
        """Swap the contents wholesale. Used by the audit, which removes rows."""
        self.occurrences = list(occurrences)
        self._seen = {self._identity(o) for o in self.occurrences}

    def cover(self, sport: str, start: date, through: date,
              feat_keys: list[str] | None = None) -> None:
        """Record that every day in [start, through] was scanned for `sport`.

        Adjacent is not a gap: a scan of January followed by a scan of
        February leaves nothing unsearched between them, so the two fuse. A
        day nobody looked at does break the claim, and stays broken -- the
        window on either side of it is kept, and nothing spans it.

        `feat_keys` is what the detector was looking for. It defaults to the
        catalogue as it stands, which is right for a scan happening now and
        wrong for one that happened before a feat was added -- hence the
        argument.
        """
        if feat_keys is None:
            from trivia import feats as _feats          # local: ledger stays importable alone
            feat_keys = [f.key for f in _feats.BY_SPORT.get(sport, ())]
        keys = tuple(sorted(feat_keys))
        existing = [(_day(w["from"]), _day(w["through"]), tuple(sorted(w.get("feats") or [])))
                    for w in self.coverage.get(sport, [])]
        self.coverage[sport] = _merge(existing + [(start, through, keys)])

    def window_for(self, sport: str, day: date,
                   feat: str | None = None) -> tuple[date, date] | None:
        """The scanned window containing `day` that looked for `feat`, if any."""
        for w in self.coverage.get(sport, []):
            start, through = _day(w["from"]), _day(w["through"])
            if start <= day <= through and (feat is None or feat in (w.get("feats") or [])):
                return start, through
        return None

    # --------------------------------------------------------------- reads
    def covers(self, sport: str, day: date, feat: str | None = None) -> bool:
        return self.window_for(sport, day, feat) is not None

    def of(self, feat: str) -> list[dict]:
        return sorted((o for o in self.occurrences if o["feat"] == feat),
                      key=lambda o: o["date"])

    def previous(self, feat: str, before: date, *, sport: str) -> dict | None:
        """The last occurrence of `feat` strictly before `before`.

        Returns None unless the whole span from that occurrence to `before`
        was scanned -- an unscanned gap could hide a more recent one, and a
        question nobody can vouch for is worse than no question.
        """
        window = self.window_for(sport, before, feat)
        if not window:
            return None
        start, _through = window
        earlier = [o for o in self.of(feat) if _day(o["date"]) < before]
        if not earlier:
            return None
        candidate = earlier[-1]
        return candidate if _day(candidate["date"]) >= start else None

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
            for w in windows:
                lines.append(f"  {sport}: {w['from']} -> {w['through']}  "
                             f"({len(w.get('feats') or [])} feats)")
        for feat, count in sorted(by_feat.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {count:4d}  {feat}")
        return "\n".join(lines)
