# Cross-Sport Fantasy CCG

A collectible card game where cards are real athletes, their stats come from live
box scores, and an NBA guard can be played against an NFL running back on one
scoresheet. Matches resolve asynchronously as real games finish — neither player
is ever online at the same time.

## Play it

```bash
./db/dev.sh prototype     # replay a season, score it, build a FIXED 48-card pool
./db/dev.sh api           # serve on :8000
open http://localhost:8000
```

No packs, no live data, no waiting. Everyone gets the same 48 cards and the same
10 tactics, so nothing turns on acquisition — what is left is whether you build a
better lineup under the budget and read the matchup better than your opponent.

Because the season is already replayed, every game a match needs is final before
it starts, so a match settles the moment both sides lock. `python3 -m demo.play`
runs the same loop headlessly.

## Quickstart

Needs PostgreSQL 16 and Python 3.10+. `dev.sh` finds the server binaries
itself (they are not on `PATH` under Debian packaging or Postgres.app) and
pip-installs `psycopg2-binary`, `fastapi` and `uvicorn` on first run. It never
touches a system Postgres: it runs its own cluster in `/tmp/pgdata_ccg` on
port 5433 over a unix socket, so nothing listens on a network port.

```bash
brew install postgresql@16      # macos
apt install postgresql-16       # debian/ubuntu
```

Everything starts from cold, because that cluster does not survive a reboot:

```bash
./db/dev.sh prototype   # drop, migrate, replay, score, build the fixed pool
./db/dev.sh api         # serve on :8000
python3 -m api.smoke    # 37 checks over the whole loop
```

About 20 seconds end to end, verified from an empty container. The rest:

```bash
./db/dev.sh verify  # scoring distributions and the no-lookahead check
./db/dev.sh test    # schema assertions on a throwaway database
./db/dev.sh demo    # the other build: random collections and a pre-settled match
python3 -m engine.test_resolution   # the scoring rules, no database
python3 -m demo.play                # the same match loop, headless
```

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

The Form budget binds: `./db/dev.sh verify` prints the best legal five against
the flash budget of 275 every run, and it has never been close.

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
- Rarity floor ≤ 38; Form budget ≤ 300, above which no lineup would be
  constrained by it
- `game_score`, `dust_ledger` and `challenge_result` are append-only
- A locked entry is immutable except `is_revealed`
- The rarity floor covers a bad **game**, not an absence: a DNP keeps
  replacement level, so rarity cannot insure against a player not appearing.
  Checked three ways, because it is the rule most likely to rot back: the pure
  function (`python3 -m engine.test_resolution`), a `CHECK` that refuses to
  record a floored replacement at all, and a smoke scenario that starts a
  scratched signature card with no bench cover and reads 15.00 out of the API
- A result cannot exist for a challenge that never locked
- **No lookahead**: a game is scored against the peer snapshot that existed when
  it finalised, built only from games that finished strictly before it

Stat corrections arrive days later, so a settled challenge must never change.
Every `game_score` records the stat revision and peer snapshot that produced it;
a correction inserts a new revision and the settled result does not move.

## API

```bash
uvicorn api.main:app --port 8000     # interactive docs at /docs
python3 -m api.smoke                 # 37 checks over the whole loop
```

Bearer tokens from `POST /auth/token` (dev-grade: no expiry, do not ship).

| | |
|---|---|
| `GET /collection` | owned cards with live Form, 7-day trend, duplicate counts |
| `GET /cards/{id}` | Form history and recent games behind one card |
| `GET /tactics` | the printed set |
| `GET/POST /decks`, `PUT /decks/{id}/slots` | build a lineup |
| `GET /decks/{id}` | slots, budget, and the seven legality rules live |
| `GET/POST /challenges`, `/accept`, `/lock` | the async match lifecycle |
| `GET /challenges/{id}` | live board, opponent masked until revealed or resolved |
| `GET /challenges/{id}/recap` | settled result, tactic log, and the counterfactual |
| `POST /challenges/{id}/bot`, `/play` | demo mode: give it an opponent, settle it now |

Writes are narrow on purpose: the scoring and resolution services own
`game_score`, `slot_result` and `challenge_result`. The API never writes them.
It creates decks and challenges and snapshots a deck into `entry_slot` at lock.

`validate_deck()` mirrors `validate_entry()` so the builder can show the same
seven violations before lock that the lock itself enforces.

## Layout

```
db/         migrations 01–12, plus dev.sh
replay/     source adapters (synthetic + CSV) and the ingest harness
engine/     scoring and resolution services
api/        FastAPI app, smoke test, and static/index.html — the whole client
demo/       setup.py builds the fixed pool; play.py plays a match headlessly
sim/        balance_sim.py — the tuning harness; re-run it whenever a constant moves
data/       tactic_cards.json (36 cards), scoring_rules.json (Stage 0 + cold start)
```

## What's missing

Deliberately, because the prototype exists to test whether lineup-building and
matchup reads decide matches — not acquisition:

- **No pack service.** Specified and simulated, never built. Everyone gets the
  same 48 cards.
- **Auth is a placeholder.** Unexpiring bearer tokens with no refresh.
- **The opponent is a bot** that builds a legal lineup at random. No matchmaking.

The one open balance question, and the thing to watch while playing it:

- **Tactic multipliers are uncapped.** A 96 GameScore with Ceiling scores 163
  — over half a winning total from one card. GameScore is bounded 0–100; the
  final score is not. Left uncapped on purpose: a cap re-solves the
  mean-neutral hit/miss pair for every card in the set, and it is worth finding
  out from play whether one card running away with a match reads as unfair
  before paying that.

Everything else, for anything past the prototype:

- **No real data.** Everything runs on synthetic seasons; `CsvSource` is the path
  real box scores take but has only been tested against generated data.
- 15 of 36 tactic conditions need usage baselines, depth charts or transaction
  history the ingest layer doesn't carry. They resolve as ×1.00 and say so.
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
