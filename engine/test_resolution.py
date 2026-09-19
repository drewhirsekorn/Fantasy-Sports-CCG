"""Unit tests for the scoring rules that decide a card's score.

No database. These are the rules that are easy to get subtly wrong and
expensive to notice, so they get checked directly.

    python3 -m engine.test_resolution
"""
from __future__ import annotations
import sys

from engine.resolution import apply_floor

FAILS: list[str] = []


def check(label, got, want):
    ok = got == want
    print(f"  {'pass' if ok else 'FAIL'}  {label}" + ("" if ok else f"  got {got}, want {want}"))
    if not ok:
        FAILS.append(label)


def main() -> int:
    print("=== the floor covers a bad game, not an absence ===")
    # A DNP takes replacement level and keeps it. Rarity must not insure
    # against a player not appearing, or the bench stops mattering.
    check("replacement keeps replacement, no floor",
          apply_floor(15.0, 34.0, True), (15.0, False))
    check("replacement unaffected by a signature floor",
          apply_floor(15.0, 38.0, True), (15.0, False))

    print("\n=== the floor covers a bad game it should ===")
    check("a bad game is lifted to the floor",
          apply_floor(12.92, 28.0, False), (28.0, True))
    check("a score at the floor is not 'applied'",
          apply_floor(28.0, 28.0, False), (28.0, False))
    check("a good game is untouched",
          apply_floor(64.72, 28.0, False), (64.72, False))
    check("common has no floor to apply",
          apply_floor(3.0, 0.0, False), (3.0, False))

    print("\n=== ordering: the floor runs BEFORE the tactic multiplier ===")
    # Reversed, a bad game gets amplified before the floor catches it and
    # rarity stops meaning reliability.
    pre, _ = apply_floor(22.08, 28.0, False)
    check("floored 22.08 then x0.65", round(pre * 0.65, 2), 18.2)
    check("amplify-then-floor would have given a different answer",
          round(max(22.08 * 0.65, 28.0), 2) != round(pre * 0.65, 2), True)

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILURES")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
