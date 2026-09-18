# Cross-Sport Fantasy CCG

A collectible card game where cards are real athletes, their stats come from live
box scores, and an NBA guard can be played against an NFL running back on one
scoresheet. Matches resolve asynchronously as real games finish — neither player
is ever online at the same time.

## Quickstart

```bash
./db/dev.sh up      # start postgres (initdb on first run)
./db/dev.sh load    # apply migrations
./db/dev.sh demo    # replay a season, score it, settle a challenge
./db/dev.sh verify  # check the scoring distributions
./db/dev.sh test    # assertions on a throwaway database
```

A cold container needs `up && load && demo` — about 15 seconds end to end.

## How it works

```
replay/          a finished season, ingested against a virtual clock
  └─ Stage 0     fantasy points, applied by a trigger so no path can skip it
engine/scoring   peer snapshots → GameScore → Form
engine/resolution  floors → bench subs → tactics → settle
```

**The scoring problem.** Raw fantasy points aren't comparable across sports, so
don't compare them. Rank-normalise instead: a game's percentile within its peer
group (sport × position) becomes a z-score, plus a capped tail bonus, mapped onto
`GameScore = 50 + 12z`. Distribution-free, so a tight end and an NBA centre land
on the same scale.

**Form** is an EWMA of recent GameScores at a sport-specific half-life, shrunk
toward the peer prior by `k=6` games. It is live and identical for every copy of
a player.

**Rarity** buys reliability, never ceiling: a floor under bad games
(0/20/28/34/38), and trait slots. Floors above 38 measured pay-to-win.

**Tactics** are the skill layer. Each has a condition, a base rate, and a matched
hit/miss pair derived so that `E[multiplier] = 1.000` at that base rate. Reading
a matchup better than the field is the only way to profit from one.

## What's validated

Against a replayed 18-week, two-sport season (1,264 games, 25,280 stat lines):

| check | result |
|---|---|
| GameScore p50, all 7 peer groups | 49.8 – 50.6 (target 50) |
| GameScore p90 | 64.9 – 67.8 (target 65) |
| Scores using a future snapshot | 0 |
| Cameo lines (below usage threshold) | median 22.1, correctly well below 50 |
| Full rebuild | deterministic, bit-identical results |

From simulation (`sim/balance_sim.py`):

- Equal decks land at 48–51% cross-sport — the normalisation is fair.
- A +12% roster edge plus good tactical reads wins **68.5%** against a casual
  player. Roster alone is 62.3%; reads alone 56.7%.
- Tactics compress roster dominance as well as adding skill, which is what keeps
  collection size from deciding matches.

## Invariants, enforced rather than documented

These are constraints and triggers, not conventions — `./db/dev.sh test` proves
each one rejects its violation:

- Tactic payoffs are mean-neutral (`CHECK` on hit/miss against base rate)
- Rarity floor ≤ 38; Form budget ≤ 300 (the best legal 5 is ~310–322, so a
  higher budget could never bind)
- `game_score`, `dust_ledger` and `challenge_result` are append-only
- A locked entry is immutable except `is_revealed`
- A result cannot exist for a challenge that never locked
- **No lookahead**: a game is scored against the peer snapshot that existed when
  it finalised, built only from games that finished strictly before it

Stat corrections arrive days later, so a settled challenge must never change.
Every `game_score` records the stat revision and peer snapshot that produced it;
a correction inserts a new revision and the settled result does not move.

## Layout

```
db/         migrations 01–09, plus dev.sh
replay/     source adapters (synthetic + CSV) and the ingest harness
engine/     scoring and resolution services
sim/        balance_sim.py — the tuning harness; re-run it whenever a constant moves
data/       tactic_cards.json (36 cards), scoring_rules.json (Stage 0 + cold start)
```

## What's missing

- **No API and no client.** Everything is CLI against Postgres. This is the
  largest remaining gap.
- **No pack service.** Specified and simulated, never built.
- **No real data.** Everything runs on synthetic seasons; `CsvSource` is the path
  real box scores take but has only been tested against generated data.
- 15 of 36 tactic conditions need usage baselines, depth charts or transaction
  history the ingest layer doesn't carry. They resolve as ×1.00 and say so.
- Bench substitution is implemented but has never executed — no starter DNP'd in
  the demo run.
- Push notifications, the retention mechanism, are unbuilt.

## Two things to know before shipping

**Licensing.** Player names and likenesses need union agreements
(MLBPA/NFLPA/NBPA). Every name in this repo and in the designs is fictional on
purpose.

**`delta_p` is a guess.** The per-card forecastability figures driving every
skill-expression number are judgment, not measurement. Measuring them is the
prototype's main job, and the whole card list re-solves once they're real.

## Design

Screens and card faces live on a canvas: card anatomy, the rarity ladder, tactic
cards, pack reveal, the live challenge board, card resolution states, the deck
builder, collection, and post-match recap.
