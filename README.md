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
./db/dev.sh reproducible   # build the season twice and compare, hash by hash
./db/dev.sh demo    # the other build: random collections and a pre-settled match
python3 -m engine.test_resolution   # the scoring rules, no database
python3 -m demo.play                # the same match loop, headless
```

## The standalone build

`web/` freezes the prototype into one HTML file with no server: the season is
already replayed, so every card's next GameScore is knowable up front and the
whole game fits in 67 KB.

```bash
python3 -m demo.export > web/data.json   # the frozen pool, from a live database
python3 web/build.py                     # inline it -> web/index.html
python3 web/parity.py                    # prove it scores like the real engine
```

`web/data.json` is committed so the published page is pinned to one export,
and `./db/dev.sh reproducible` is what makes regenerating it safe.

The page re-implements the resolution rules in JavaScript, which is exactly
the setup where two copies of one rule quietly drift apart. `parity.py` plays
a real match through the API and feeds both lineups into the page's engine,
comparing every slot's raw score, floor, multiplier and final. Its fixture
deliberately starts the players who do not play that week, so bench cover and
the replacement rule are both exercised rather than the happy path.

It is a demo, not the system: the database enforces the invariants, and the
page only obeys them.

`web/card.template.html` (built by `python3 web/build_card.py`) is a separate
prototype for a single card face, front and back. Its job is to make the
rarity rule legible rather than merely true: Form sits on a 0-100 rail and the
floor is drawn as a shelf under it, so stepping a real player up the ladder
shows the shelf rise while the ceiling does not move. The back puts the floor
straight across that player's last eight GameScores and counts how many it
would have caught.

It draws on the same ordinal rarity ramp as the grid, and keeps three
encodings apart rather than letting them inherit colours: the rarity
**ladder** in blue, the **floor** in gold, and **Form** -- the card's primary
datum -- in plain ink, each separable from the others under all three
colour-vision models. The five-hue palette it replaced collided with itself
twice over: its Signature was the same gold as the floor mark, so on a
Signature card the tier and the floor were one colour.

Pip count is the channel that has to survive with no colour at all, so it
never takes a rung of the ramp too dark to count.

`web/grid.template.html` (built by `python3 web/build_grid.py`) is the
collection: all 48 at once. Since everyone holds the same pool it is not a
trophy case but a finding tool, so it carries a budget lens — set what you
have left and the cards that would strand you step back — plus sorts that
answer the budget's actual question, including floor bought per point of Form.

It is also where the palette decision gets forced, so it carries the evidence
rather than the claim. Rarity is a **ladder, not a set of identities**: one
hue stepped by lightness, with pip count as a second channel. Switch the
palette and the vision model on the page and look.

| worst pair, all five tiers on screen | ordinal | five hues |
|---|---|---|
| colour-vision separation | ΔE 10.1 pass | ΔE 2.7 fail |
| normal-vision separation | ΔE 10.4 | ΔE 11.8 |
| survives losing colour entirely | yes, lightness | no |

Under deuteranopia the five-hue Uncommon, Elite and Signature are one colour.
Neither palette clears the ΔE 15 bar for *categorical* use, which is the right
answer: adjacent rungs of a ladder are meant to look adjacent.

## The daily trivia game

`web/trivia.html` is a second standalone build, unrelated to the card game
except that it reads the same kind of data: three questions about what
happened in the NBA, NFL and MLB the night before, replaced at 07:00 Eastern
every morning.

```bash
python3 -m trivia.build            # scan yesterday, extend the ledger, bake the page
open web/trivia.html               # or ./db/dev.sh api and visit /trivia
python3 -m trivia.test_trivia      # the rules that decide whether a question is honest
```

The question is the one the example asks for — *X did this last night; who did
it before?* — and the interesting part is not asking it but being sure of the
answer.

### Nothing is typed in from memory

A trivia game written by hand is only as correct as whoever wrote it, and this
one has to be right every morning with nobody watching. So no fact in it is
authored. Every question is assembled out of box scores:

- the **setup** is a line the detector found in last night's box score,
- the **answer** is another row of the same ledger,
- the **wrong options** are real players who have done the same thing on other
  nights,
- and the **explanation** links the two box scores it came from.

That is what `trivia/feats.py` is for. A feat is a threshold rare enough that
the last person to clear it is a real recall. "Scored 30" is not a question,
because the answer is "someone, last night".

The table is in two tiers, and both are needed. The rare tier — a no-hitter,
six passing touchdowns, a 40-point triple-double — is what makes a good
morning, but those nights are uncommon enough that most days would have
nothing from last night to ask about at all. The second tier, a few dozen to a
hundred-odd times a season, is what makes *somebody did this last night* the
usual case rather than the exception. `rank` keeps them in their place: when
both fire on one night, the rarer one is the question.

Adding a feat is a single row in that table — the feed, the ledger, the
question builder and the page all learn it without being touched.

### The ledger says what it searched, not just what it found

`data/trivia_ledger.json` is the answer key, and its correctness argument is
one sentence: **"who did it before" is only asked when the whole span between
the two occurrences was actually scanned.**

So the ledger stores coverage windows per sport alongside the occurrences.
`Ledger.previous` refuses to answer when no window spans the gap, because an
unscanned day could hide a more recent holder and a question nobody can vouch
for is worse than no question. Coverage only widens when it is earned:

- Two adjoining scans merge into one window. Two scans with a day nobody
  looked at between them do not, and coverage is a *list* of windows for
  exactly that reason — the daily job will miss a morning eventually, and one
  window would have to choose between forgetting everything before the gap and
  lying about it. A missed morning costs one day.
- A day whose box scores would not load is left out of the claim rather than
  assumed empty. A box score that never arrived and a quiet night look
  identical from the outside, and only one of them is safe to build on.
- Each window names **which feats it was scanned for**. Adding a feat would
  otherwise be silently retroactive: the old windows would vouch for something
  the detector could not yet see, and the first question about it would have a
  wrong answer. A new feat simply has no coverage until someone backfills it.

Four more refusals, each of which produced a wrong answer before it existed:

| what happened | what the build does |
|---|---|
| a 50-point **All-Star Game** landed in the ledger next to real ones | exhibitions are filtered at the feed — preseason, spring training, All-Star, the Pro Bowl. The playoffs emphatically count. |
| **two players** cleared the same feat the same night | the question is dropped. "The last" has two right answers that night and four options have room for one. |
| one player holds two feats, so the **distractor pool handed back a name already in the question** | options are deduped, and a question that cannot be built with four distinct names is not built |
| **"nobody managed it last night"** was sayable about a night somebody did — the setup denying the very game the answer came from | the claim is only made about a feat that did not occur, on a night that was scanned |

`python3 -m trivia.backfill --audit` re-checks that every entry still comes
from a game that counts. A date that will not load drops nothing: *could not
check* and *does not count* are different answers, and only one of them is
grounds for deleting evidence.

### Two shapes, both about last night

The leagues do not cooperate. Mid-July has no NBA and no NFL; late February
has no MLB; and some nights nothing rare happens anywhere.

| shape | when | the question |
|---|---|---|
| `last_before` | something rare happened last night | who did it before them? |
| `most_recent` | a quiet night | who had the most recent one? |

There was a third, asking who did something on this same date in an earlier
year. It filled two thirds of the mornings and it was **removed on purpose**:
the game is about last night, and a question opening "on this date in 2004"
is a different game wearing the same page. Both remaining shapes hang off the
night just played — either something happened, or nothing did and the absence
is the setup. A test walks forty days of builds asserting no question about
another calendar year, because the pressure to bring it back will come from a
morning that looks thin.

Losing it cost nothing in coverage: every day of a test year still fills.
What it cost was concentration, and three things had to be fixed before the
remaining shapes could carry the load alone.

- **`most_recent` picked the rarest feat, so the rarest feat won every quiet
  night.** Same question, same eight-month-old answer, every week. It now
  sorts by how recently the feat last happened and shuffles among the
  freshest few. Median answer age across a test year: **8 days**, a quarter
  of them inside 4.
- **It asked about leagues that were not playing.** "Nobody managed four
  rushing touchdowns in the NFL yesterday" is true in mid-July, deadpan and
  faintly ridiculous. Leagues that were actually playing are asked about
  first; a dark one is only reached for if the board would be short.
- **Two questions could share an answer.** Get one and you have the other,
  and the day is really two questions long.

If the ledger cannot honestly support even one question, the build **fails
rather than publishing a thin day**.

### 07:00 Eastern, and the two mornings a year it moves

GitHub's scheduler is UTC and does not know about daylight saving, so
`.github/workflows/daily-trivia.yml` fires at both 11:00 and 12:00 UTC and
`trivia/build.py` decides which one is actually 07:00 in New York. The other
run finds the day already published and exits without touching a file.

The same function draws the 24-hour window the player gets, which is why it is
computed in Eastern rather than by adding 86,400 seconds: the day the clocks
go forward is 23 hours long and the day they go back is 25, and the countdown
on the page says so.

### One day at a time, on purpose

The page holds exactly one day. There is no archive, no back button to
yesterday, and nothing server-side that remembers a player.

What the browser keeps is deliberately small — a date and two numbers per day,
capped at a fortnight. The questions themselves are never written to storage,
which is what makes "yesterday's questions are gone" true rather than merely
claimed. A tab left open overnight is not an exception: the countdown is also
the guard, and at zero the board locks and asks for a reload rather than
accepting answers to a day that has ended.

Right and wrong are told three ways — border weight, tint and a glyph — for
the same reason the collection grid puts rarity on a ladder instead of five
hues: the page has to survive losing colour entirely.

### Coverage, and what deepens it

ESPN's box scores reach back further than the seeded ledger does:

| | box scores available from | scanned here |
|---|---|---|
| NBA | 1993 | `1993-11-01` → `1995-11-30` · `2018-10-01` → `2019-01-31` · `2023-10-01` → `2026-09-19` |
| NFL | 2002 | `2002-09-01` → `2005-12-31` · `2023-09-01` → `2026-09-19` |
| MLB | 2003 | `2022-01-01` → `2023-06-30` · `2025-01-01` → `2026-09-19` |

3,582 occurrences across 28 feats, 832 of them from the 1990s. 27 feats have
happened at least once in those windows; a
six-touchdown game has not, which is the kind of thing the rare tier is for.

The windows are deliberately not one span each: scanning ran backward from
the present in one process and forward from 1993 in another, and the middle
was never reached. The gaps are honest — no question is built across one.

Depth now buys less than it did. The 1990s were scanned to give the
anniversary questions somewhere to reach, and those questions are gone, so
the archive is no longer load-bearing. What deepening still buys is a denser
recent ledger: more nights with something on them, so more mornings open with
something that actually happened rather than something that did not.

```bash
python3 -m trivia.backfill --sport NBA --since 1993-11-01     # the whole archive
python3 -m trivia.backfill --sport MLB --since 2003-03-30
python3 -m trivia.backfill --audit                            # then re-check it
```

Backfill and the daily run share one detector, so a backfilled entry and a
live one are produced by the same code — the answer key cannot drift from the
thing it is answering about. It is resumable, checkpoints by month, and
re-running a range adds nothing.

Two things the detector cannot see, both because it reads a box score one line
at a time.

A **combined no-hitter** is not in any single pitcher's line, which is why
that feat and the question it asks both say *complete-game* — the question is
asked about what was actually searched for, rather than a category it would
miss nights from.

And **career milestones are the missing feat.** The example that prompted this —
*"player X hit their 300th home run"* — cannot be read off a box score, which
knows a player hit two home runs last night but not that they now have 300.
That needs career totals at the time of each historical game, which is a
different data problem from the one solved here, not a threshold to add to the
table.

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
| GameScore p50, all 7 peer groups | 49.2 – 51.5 (target 50) |
| GameScore p90 | 64.7 – 69.2 (target 65) |
| Scores using a future snapshot | 0 |
| Cameo lines (below usage threshold) | median 22.1, correctly well below 50 |
| Full rebuild | bit-identical (`./db/dev.sh reproducible`) |
| Replay resumed in a second process | bit-identical to running straight through |

Those figures are now stable across rebuilds, which they were not before — see
**Reproducibility** below. The remaining spread is sampling noise on the small
groups (NFL RB, n=526).

The Form budget binds: `./db/dev.sh verify` prints the best legal five against
the flash budget of 275 every run, and it has never been close.

From simulation (`sim/balance_sim.py`):

- Equal decks land at 48–51% cross-sport — the normalisation is fair.
- A +12% roster edge plus good tactical reads wins **68.5%** against a casual
  player. Roster alone is 62.3%; reads alone 56.7%.
- Tactics compress roster dominance as well as adding skill, which is what keeps
  collection size from deciding matches.

## Reproducibility

Every calibration number above is measured on one build, so they mean nothing
unless another build produces the same season. For a long time it did not, and
the README claimed otherwise. Two defects, found by hashing the pipeline stage
by stage — load matched, replay did not:

**The tick query was not totally ordered.** It read due games with
`ORDER BY g.scheduled_end`, and a slate shares end times, so Postgres returned
ties in whatever order it liked. **The source drew every stat line from one
shared generator**, so a game's box score depended on how many games had been
asked for before it. Together: two consecutive builds shared 5 of 48 pool
cards, and the players they shared came back with different Form and different
GameScores.

The ordering fix alone makes a rebuild reproducible. The generator fix is what
makes it *robust* — a box score is now a property of its fixture, seeded from
the fixture's identity, so it no longer depends on tick size, ordering, or
where a replay was interrupted. That second one was hiding a bug of its own:
the harness promises a tick can be replayed after a crash, and resuming used
to produce a different season from that point on. Nothing checked.

`./db/dev.sh reproducible` now checks both, and was confirmed to fail against
each defect before being kept.

## Mean-neutral is not measured

Every tactic's hit/miss pair is solved so `E[multiplier] = 1.00` at its printed
base rate, and a `CHECK` enforces that arithmetic. The constraint verifies the
**solve**, not the game: `base_rate` is a designed number, and nothing compared
it to how often the condition actually fires until now.

`python3 -m engine.deltap neutrality` does. Against the replayed season, **8 of
the 10 demo cards are not neutral in play**:

| card | printed rate | actual | pays |
|---|---|---|---|
| Two-Sport | 0.25 | 0.73 / 0.51 | **1.41× / 1.22×** |
| Overrated | 0.40 | 0.93 / 0.75 | **1.38× / 1.25×** |
| Bounce Back | 0.38 | 0.00 | **0.64×** — never fires at all |
| Lockdown | 0.32 | 0.64 / 0.62 | 1.25× / 1.23× |
| Cold Snap | 0.50 | 0.83 | 1.23× |
| Ceiling | 0.10 | 0.40 / 0.11 | 1.23× / 1.00× |

Two rates per card because how people build changes how often a condition
fires: the first column is the best legal five, the second any legal five.
A single printed base rate cannot be right for both, which is a design problem
before it is a tuning one.

Every one of these satisfies the `CHECK`. Re-solving is not applied —
`--resolve` prints what the pairs would become, and for four cards the
required miss goes below the 0.35 floor, meaning the spread has to come down
rather than the miss: a card that fires 83% of the time cannot carry a 1.35×
hit and stay neutral.

## Measuring delta_p

`delta_p` is the edge a player gets from pointing a tactic at a better target
than chance would. It is measured against the counterfactual, which the
replayed season makes exact — for the lineup actually fielded, evaluate the
tactic against **every** legal target:

    chosen   1 if the player's target hit
    chance   the fraction of that lineup's targets that would have hit
    delta    chosen - chance

Pairing inside one match removes lineup quality, opponent and week, which is
what brings the sample size within reach:

| true edge | plays to detect it 80% of the time |
|---|---|
| 0.50 | 100 |
| 0.25 | 400 |
| 0.10 | more than 800 |

The estimator is calibrated against edges of known size, established on an
independent large sample — a 200-play interval covers the truth 91–97% of the
time, and a blind player is called skilled 2.5% of the time, which is what a
95% interval should do. (The first version of this test compared the estimate
to the edge on the same plays, which is the same arithmetic twice and passes
unconditionally.)

**Half the demo cards cannot carry a target edge at all.**
`python3 -m engine.deltap sensitivity` separates three reasons: Cold Snap,
Lockdown, Overrated and Two-Sport read the lineup or the opponent, so they hit
the same way wherever they are pinned — their skill is in bringing them, not
aiming them. Bounce Back is per-slot but never satisfiable here. Pooling those
into one number would dilute it with plays that were never able to contribute.

**What is missing is the choice itself.** Both clients hard-wire tactic *n* to
starter *n*. The API and schema carry `tactic_target_index` and always have;
the interfaces never expose it. Until they do there is nothing to measure.

## Invariants, enforced rather than documented

These are constraints and triggers, not conventions — `./db/dev.sh test` proves
each one rejects its violation:

- Tactic payoffs satisfy their own solve (`CHECK` on hit/miss against the
  **printed** base rate). Note what that does and does not say — see
  **Mean-neutral is not measured** below
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
trivia/     the daily trivia build: feed -> feats -> ledger -> puzzle -> page
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

**`delta_p` is still a guess, but there is now a way to stop guessing.**
`engine/deltap.py` measures it against an exact counterfactual rather than
against the printed base rate — see **Measuring delta_p** below. What blocks
it is not the estimator: it is that no client lets a player choose a tactic's
target, so the decision `delta_p` is *about* is never made.

## Design

Screens and card faces live on a canvas: card anatomy, the rarity ladder, tactic
cards, pack reveal, the live challenge board, card resolution states, the deck
builder, collection, and post-match recap.
