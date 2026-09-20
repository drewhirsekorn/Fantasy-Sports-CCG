"""Measure delta_p: how much better than chance a player picks tactic targets.

Every skill-expression number in this design rests on delta_p, and the README
has said from the start that it is judgment rather than measurement. This is
the measurement.

WHAT IT IS.  A tactic has a base rate p -- the chance its condition fires on
an arbitrary target. The hit/miss pair is solved so that E[multiplier] = 1.00
at exactly that rate, so a player only profits by attaching the card to
targets where the condition is likelier than p. delta_p is that edge.

HOW IT IS MEASURED.  Not against the printed base rate, which is itself a
guess. Against the counterfactual, which the replayed season makes exact:
for the very lineup the player actually fielded, evaluate the tactic against
EVERY legal target and count how many would have hit. That gives, per play:

    chosen      1 if the player's target hit, else 0
    chance      the fraction of that lineup's targets that would have hit
    delta       chosen - chance

and delta_p is the mean of the paired differences. Pairing inside one match
removes lineup quality, opponent and week from the comparison entirely, which
is what makes a usable number reachable from hundreds of plays rather than
tens of thousands.

    python3 -m engine.deltap validate     # does the estimator recover a KNOWN edge?
    python3 -m engine.deltap power        # how many plays to detect a given edge
    python3 -m engine.deltap sensitivity  # which cards can carry an edge at all?
    python3 -m engine.deltap neutrality   # are the cards even mean-neutral in play?
    python3 -m engine.deltap measure      # run it over real plays in the database
"""
from __future__ import annotations
import argparse
import json
import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.resolution import CONDITIONS, apply_floor  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POOL_FILE = os.path.join(HERE, "web", "data.json")


# ===========================================================================
#  The estimator
# ===========================================================================
def targets_that_would_hit(condition_key, own, opp, history):
    """For one tactic on one lineup: would it hit, on each legal target?

    This is the whole trick. The season is already final, so the question
    'what if you had pointed it at the other guy' has an exact answer rather
    than a simulated one.
    """
    fn = CONDITIONS.get(condition_key)
    if fn is None:
        return None
    out = []
    for s in own:
        hit, _ = fn({"slot": s, "own_slots": own, "opp_slots": opp, "history": history})
        out.append(bool(hit))
    return out


def play_delta(condition_key, own, opp, history, chosen_index):
    """One observation: (chosen hit, chance of hitting on a random target)."""
    grid = targets_that_would_hit(condition_key, own, opp, history)
    if grid is None:
        return None
    chosen = 1.0 if grid[chosen_index] else 0.0
    chance = sum(grid) / len(grid)
    return chosen, chance


def summarise(plays):
    """Paired mean difference with a normal-approximation interval.

    Paired, so the standard error is of the DIFFERENCES -- much tighter than
    comparing two independent rates, and the reason this is measurable at all
    at prototype volumes.
    """
    n = len(plays)
    if n < 2:
        return {"n": n, "delta_p": None}
    diffs = [c - r for c, r in plays]
    mean = sum(diffs) / n
    var = sum((d - mean) ** 2 for d in diffs) / (n - 1)
    se = math.sqrt(var / n)
    return {
        "n": n,
        "observed": sum(c for c, _ in plays) / n,
        "chance": sum(r for _, r in plays) / n,
        "delta_p": mean,
        "se": se,
        "lo": mean - 1.96 * se,
        "hi": mean + 1.96 * se,
        # The question that actually matters first: is there ANY edge?
        "significant": (mean - 1.96 * se) > 0,
    }


# ===========================================================================
#  A world to measure in: the frozen pool, the same one the prototype plays
# ===========================================================================
class Pool:
    def __init__(self, path=POOL_FILE):
        with open(path) as f:
            d = json.load(f)
        self.budget = d["budget"]
        self.floors = d["floors"]
        self.replacement = d["replacement"]
        self.tactics = {t["code"]: t for t in d["tactics"]}
        self.cards = [c for c in d["cards"] if c["form"] is not None]
        self.history = {c["id"]: list(c["prior"]) for c in d["cards"]}

    def lineup(self, rng):
        """A legal five: two sports, under budget, distinct players."""
        pool = self.cards[:]
        rng.shuffle(pool)
        picked, used, spent = [], set(), 0.0

        def affordable(c):
            need = 4 - len(picked)
            rest = sorted(x["form"] for x in pool
                          if x["id"] not in used and x["id"] != c["id"])
            return spent + c["form"] + sum(rest[:max(0, need)]) <= self.budget

        for sport in sorted({c["sport"] for c in pool}):
            c = next((x for x in pool if x["sport"] == sport and x["id"] not in used
                      and affordable(x)), None)
            if c:
                used.add(c["id"]); spent += c["form"]; picked.append(c)
        for c in sorted(pool, key=lambda x: -x["form"]):
            if len(picked) == 5:
                break
            if c["id"] in used or not affordable(c):
                continue
            used.add(c["id"]); spent += c["form"]; picked.append(c)
        return picked if len(picked) == 5 else None

    def slots(self, cards):
        """Score a lineup the way the resolver does, floors and all."""
        out = []
        for i, c in enumerate(cards, 1):
            was_repl = c["score"] is None
            raw = self.replacement[c["sport"]] if was_repl else c["score"]
            pre, floored = apply_floor(raw, self.floors[c["rarity"]], was_repl)
            out.append({
                "entry_slot_id": None, "slot_index": i, "player_id": c["id"],
                "sport": c["sport"], "rarity": c["rarity"],
                "form_at_lock": c["form"],
                # Starters 1 and 2 are face-up at lock; counterplay tactics
                # read exactly those.
                "is_revealed": i <= 2,
                "raw": raw, "pre": pre, "floor_applied": floored,
                "was_replacement": was_repl, "resolved_at": i,
            })
        return out


# ===========================================================================
#  Policies: how a player picks the target. Used to test the estimator
#  against edges we already know the size of.
# ===========================================================================
def policy_chance(grid, rng):
    """Pick blind. True delta_p is zero by construction."""
    return rng.randrange(len(grid))


def policy_oracle(grid, rng):
    """Pick a target that hits, if one exists. The ceiling."""
    winners = [i for i, h in enumerate(grid) if h]
    return rng.choice(winners) if winners else rng.randrange(len(grid))


def policy_noisy(q):
    """Read the matchup correctly a fraction q of the time.

    This is what a real player is: right sometimes. It gives a known edge to
    recover -- q of the way from chance to the ceiling.
    """
    def pick(grid, rng):
        return policy_oracle(grid, rng) if rng.random() < q else policy_chance(grid, rng)
    return pick


POLICIES = [
    ("blind (edge = 0)", policy_chance, 0.0),
    ("reads it 25%", policy_noisy(0.25), 0.25),
    ("reads it 50%", policy_noisy(0.50), 0.50),
    ("perfect (ceiling)", policy_oracle, 1.0),
]


def simulate(pool, policy, n_matches, rng, codes=None):
    """Play n matches, two tactics each, and record every play."""
    codes = codes or sorted(pool.tactics)
    plays, by_card = [], {}
    truth = []
    for _ in range(n_matches):
        mine, theirs = pool.lineup(rng), pool.lineup(rng)
        if not mine or not theirs:
            continue
        own, opp = pool.slots(mine), pool.slots(theirs)
        hist = {s["player_id"]: pool.history.get(s["player_id"], []) for s in own}
        for code in rng.sample(codes, 2):
            key = pool.tactics[code]["condition"]
            grid = targets_that_would_hit(key, own, opp, hist)
            if grid is None:
                continue
            idx = policy(grid, rng)
            obs = play_delta(key, own, opp, hist, idx)
            plays.append(obs)
            by_card.setdefault(code, []).append(obs)
            # What the policy's edge actually was on this lineup, known
            # exactly because we can see the whole grid.
            truth.append((1.0 if grid[idx] else 0.0) - sum(grid) / len(grid))
    return plays, by_card, (sum(truth) / len(truth) if truth else 0.0)


# ===========================================================================
#  Commands
# ===========================================================================
def cmd_validate(args):
    """An estimator nobody has tested against a known answer is a guess with
    error bars.

    The obvious test is a trap, and this file shipped it for one revision:
    comparing the estimate to 'the edge the policy got on those same plays'
    compares a number to itself, and passes no matter what. The real question
    is whether a SMALL sample's interval covers the policy's TRUE edge --
    which has to be established independently, on its own large sample.
    """
    pool = Pool()
    print("=== ground truth: each policy's edge, on a large independent sample ===")
    truth = {}
    for label, policy, q in POLICIES:
        rng = random.Random(args.seed + 999)
        plays, _, _ = simulate(pool, policy, args.matches, rng)
        truth[label] = summarise(plays)["delta_p"]
        print(f"  {label:<22}{truth[label]:>+8.3f}   ({len(plays)} plays)")

    print(f"\n=== calibration: does a {args.sample}-play interval cover it? ===")
    print(f"  {args.trials} independent replications per policy\n")
    print(f"  {'policy':<22}{'true':>8}{'mean est':>10}{'covered':>10}{'':>6}")
    ok = True
    for label, policy, q in POLICIES:
        covered = 0
        ests = []
        for i in range(args.trials):
            rng = random.Random(args.seed + i * 31 + 1)
            plays, _, _ = simulate(pool, policy, args.sample // 2, rng)
            r = summarise(plays)
            ests.append(r["delta_p"])
            if r["lo"] <= truth[label] <= r["hi"]:
                covered += 1
        rate = covered / args.trials
        # A 95% interval that covers 95% of the time is calibrated. Well under
        # means the intervals lie; well over means they are wastefully wide.
        good = 0.88 <= rate <= 0.99
        ok = ok and good
        print(f"  {label:<22}{truth[label]:>+8.3f}{sum(ests)/len(ests):>+10.3f}"
              f"{rate:>10.0%}{'  ok' if good else '  MIScALIBRATED'}")

    print(f"\n=== the null: a blind player must not look skilled ===")
    falsely = sum(
        1 for i in range(args.trials)
        if summarise(simulate(pool, policy_chance, args.sample // 2,
                              random.Random(args.seed + 7000 + i))[0])["significant"])
    rate = falsely / args.trials
    null_ok = rate < 0.08
    print(f"  called skilled: {falsely}/{args.trials} = {rate:.1%}"
          f"  (a 95% interval should, about 2.5% of the time)"
          f"{'' if null_ok else '   TOO HIGH'}")

    print(f"\n  {'calibrated' if ok and null_ok else 'NOT CALIBRATED'}")
    return 0 if ok and null_ok else 1


def cmd_sensitivity(args):
    """Which cards can carry an edge from target choice at all?

    Three different reasons a card might never reward aiming, and they need
    telling apart -- the first version of this lumped them together and
    reported a per-slot card as lineup-level, which is simply false:

      LINEUP-LEVEL  the condition reads the whole lineup or the opponent, so
                    it hits or misses the same way whichever starter it is
                    pinned to. Skill is in bringing it, not aiming it.
      NEVER FIRES   per-slot, but the condition is not satisfiable in this
                    pool. A dead card, which is a balance problem, not a
                    measurement one.
      ALWAYS FIRES  per-slot, but every target hits. Free money.

    Only cards whose grid VARIES can express target skill, and only those
    belong in an aggregate delta_p.
    """
    pool = Pool()
    rng = random.Random(args.seed)
    stat = {c: {"varies": 0, "all_hit": 0, "none_hit": 0, "n": 0, "hits": 0, "slots": 0}
            for c in pool.tactics}
    for _ in range(args.matches):
        mine, theirs = pool.lineup(rng), pool.lineup(rng)
        if not mine or not theirs:
            continue
        own, opp = pool.slots(mine), pool.slots(theirs)
        hist = {s["player_id"]: pool.history.get(s["player_id"], []) for s in own}
        for code, t in pool.tactics.items():
            grid = targets_that_would_hit(t["condition"], own, opp, hist)
            if grid is None:
                continue
            st = stat[code]
            st["n"] += 1
            st["hits"] += sum(grid)
            st["slots"] += len(grid)
            if len(set(grid)) > 1:
                st["varies"] += 1
            elif grid[0]:
                st["all_hit"] += 1
            else:
                st["none_hit"] += 1

    # A card is lineup-level when its condition cannot read the slot at all.
    # That is a property of the code, not of a sample, so read it from the
    # code rather than inferring it from a run that might just be unlucky.
    LINEUP_LEVEL = {"cold_snap", "lockdown", "bust", "shutout", "overrated",
                    "two_sport", "triple_threat", "hedge", "global_slate",
                    "split_decision"}

    print(f"=== can target choice move this card? ({args.matches} lineups) ===\n")
    print(f"  {'card':<14}{'fires':>7}{'target mattered':>17}   verdict")
    usable, dead = [], []
    for code in sorted(pool.tactics, key=lambda c: -stat[c]["varies"] / max(1, stat[c]["n"])):
        st = stat[code]
        frac = st["varies"] / max(1, st["n"])
        rate = st["hits"] / max(1, st["slots"])
        key = pool.tactics[code]["condition"]
        if key in LINEUP_LEVEL:
            verdict = "lineup-level by design"
            dead.append(code)
        elif frac >= 0.05:
            verdict = "usable"
            usable.append(code)
        elif rate < 0.02:
            verdict = f"NEVER FIRES in this pool"
            dead.append(code)
        else:
            verdict = "always fires"
            dead.append(code)
        print(f"  {code:<14}{rate:>7.0%}{frac:>17.0%}   {verdict}")

    print(f"\n  {len(usable)} of {len(pool.tactics)} cards can express target skill:")
    print(f"    {', '.join(usable)}")
    print(f"\n  The rest cannot, for different reasons, and pooling them into one")
    print(f"  delta_p would dilute it toward zero with plays that were never")
    print(f"  able to carry an edge. `measure` reports them separately.")
    print(f"\n  Printed base rates, against what the conditions actually do here:")
    print(f"  {'card':<14}{'printed':>9}{'actual':>9}")
    for code in sorted(pool.tactics):
        st = stat[code]
        print(f"  {code:<14}{pool.tactics[code]['base_rate']:>9.2f}"
              f"{st['hits'] / max(1, st['slots']):>9.2f}")
    return 0

def random_legal_lineup(pool, rng):
    """Any legal five, not the greedy-best five.

    How people build changes how often a condition fires, so a base rate
    measured against one build policy is not a base rate. Measuring under both
    is the honest minimum.
    """
    for _ in range(300):
        pick = rng.sample(pool.cards, 5)
        if (sum(c["form"] for c in pick) <= pool.budget
                and len({c["sport"] for c in pick}) >= 2):
            return pick
    return None


def measured_rates(pool, lineup_fn, n, seed):
    """How often each condition actually fires, per target, over n lineups."""
    rng = random.Random(seed)
    hits = {c: 0 for c in pool.tactics}
    slots = {c: 0 for c in pool.tactics}
    for _ in range(n):
        mine, theirs = lineup_fn(rng), lineup_fn(rng)
        if not mine or not theirs:
            continue
        own, opp = pool.slots(mine), pool.slots(theirs)
        hist = {s["player_id"]: pool.history.get(s["player_id"], []) for s in own}
        for code, t in pool.tactics.items():
            grid = targets_that_would_hit(t["condition"], own, opp, hist)
            if grid is None:
                continue
            hits[code] += sum(grid)
            slots[code] += len(grid)
    return {c: hits[c] / max(1, slots[c]) for c in pool.tactics}


def cmd_neutrality(args):
    """Are the cards actually mean-neutral? They are certified, not measured.

    Every tactic's hit/miss pair is solved so that E[multiplier] = 1.00 at its
    printed base rate, and a CHECK constraint enforces that arithmetic. But
    the constraint checks the SOLVE, not the game: base_rate is a designed
    number, and nothing until now compared it to how often the condition fires
    against a season. This does.
    """
    pool = Pool()
    greedy = measured_rates(pool, pool.lineup, args.matches, args.seed)
    rand = measured_rates(pool, lambda r: random_legal_lineup(pool, r),
                          args.matches, args.seed + 1)

    print(f"=== is E[multiplier] really 1.00? ({args.matches} lineups each) ===")
    print("  'greedy' builds the best legal five; 'random' takes any legal five.")
    print("  Both are things players do, and the rate differs between them.\n")
    print(f"  {'card':<14}{'printed':>8}{'greedy':>8}{'random':>8}"
          f"{'E[mult]':>10}{'E[mult]':>9}")
    print(f"  {'':<14}{'rate':>8}{'rate':>8}{'rate':>8}{'greedy':>10}{'random':>9}")
    bad = []
    for code in sorted(pool.tactics):
        t = pool.tactics[code]
        eg = greedy[code] * t["hit"] + (1 - greedy[code]) * t["miss"]
        er = rand[code] * t["hit"] + (1 - rand[code]) * t["miss"]
        flag = "" if max(abs(eg - 1), abs(er - 1)) < 0.05 else "   <-"
        if flag:
            bad.append((code, t, greedy[code], rand[code], eg, er))
        print(f"  {code:<14}{t['base_rate']:>8.2f}{greedy[code]:>8.2f}{rand[code]:>8.2f}"
              f"{eg:>10.3f}{er:>9.3f}{flag}")

    print(f"\n  {len(bad)} of {len(pool.tactics)} cards are not neutral in play:")
    for code, t, g, r, eg, er in sorted(bad, key=lambda x: -max(abs(x[4] - 1), abs(x[5] - 1))):
        print(f"    {code:<14} pays {eg:.2f}x / {er:.2f}x instead of 1.00x"
              f"   (printed rate {t['base_rate']:.2f}, actual {g:.2f}/{r:.2f})")

    print("\n  The CHECK constraint is satisfied by every one of these. It verifies")
    print("  base_rate * hit + (1 - base_rate) * miss = 1, which is true --")
    print("  base_rate is simply not the rate.")

    if args.resolve:
        print("\n=== what the pairs would be, solved on the measured rate ===")
        print("  Keeping each card's spread, so its character does not change:")
        print("  hit = 1 + V, miss = 1 - V*p/(1-p).\n")
        print(f"  {'card':<14}{'now':>14}{'resolved':>16}")
        for code in sorted(pool.tactics):
            t = pool.tactics[code]
            p_new = (greedy[code] + rand[code]) / 2
            if not (0.02 < p_new < 0.98):
                print(f"  {code:<14}{t['hit']:>6.2f} /{t['miss']:>6.2f}"
                      f"{'  unsolvable -- rate is ' + format(p_new, '.2f'):>16}")
                continue
            V = t["hit"] - 1
            miss = 1 - V * p_new / (1 - p_new)
            # The floor exists so a miss never wipes a card out entirely.
            note = "" if miss >= 0.35 else "   miss below the 0.35 floor: needs a smaller spread"
            print(f"  {code:<14}{t['hit']:>6.2f} /{t['miss']:>6.2f}"
                  f"{t['hit']:>10.2f} /{miss:>5.2f}{note}")
        print("\n  Not applied. Re-solving the set is a design decision, and it")
        print("  should be made once, on rates measured from real play rather")
        print("  than from a bot's idea of a lineup.")
    return 0


def cmd_power(args):
    """How many plays before the answer means anything?"""
    pool = Pool()
    print("=== how many plays to detect an edge ===")
    print("  A match yields 2 plays, so halve these for matches.\n")
    print(f"  {'edge':>7}{'plays':>9}{'detected':>11}")
    for q, label in [(0.10, None), (0.25, None), (0.50, None)]:
        for n in (50, 100, 200, 400, 800):
            hits = 0
            for i in range(args.trials):
                plays, _, truth = simulate(pool, policy_noisy(q),
                                           n // 2, random.Random(args.seed + i))
                if summarise(plays)["significant"]:
                    hits += 1
            det = hits / args.trials
            print(f"  {'q=' + str(q):>7}{n:>9}{det:>10.0%}" +
                  ("   <- enough" if det >= .8 and label is None else ""))
            if det >= .8:
                label = n
                break
        print()
    print("  'Enough' is 80% of the time, the usual bar. The first question is")
    print("  not per-card at all -- it is whether the aggregate edge is above")
    print("  zero, and that answers far sooner than any single card does.")
    return 0


def cmd_measure(args):
    """The real thing: every tactic played by a human, in the database."""
    import psycopg2
    dsn = os.environ.get("CCG_DSN", "host=/tmp port=5433 user=ccg dbname=ccg")
    from engine.resolution import Resolver

    cx = psycopg2.connect(dsn)
    cur = cx.cursor()
    cur.execute("""SELECT tr.id, tr.entry_slot_id, es.entry_id, tc.code, tc.condition_key,
                          tr.target_slot_id, tr.did_hit, u.handle
                   FROM tactic_resolution tr
                   JOIN entry_slot es ON es.id = tr.entry_slot_id
                   JOIN challenge_entry e ON e.id = es.entry_id
                   JOIN app_user u ON u.id = e.user_id
                   JOIN tactic_card tc ON tc.id = tr.tactic_card_id
                   ORDER BY tr.id""")
    rows = cur.fetchall()
    if not rows:
        print("no tactic plays on record yet -- play some matches first")
        cx.close()
        return 1

    resolver = Resolver(cur)
    cache = {}
    plays, by_card, by_player = [], {}, {}
    skipped = 0
    for _tid, _esid, entry_id, code, key, target_id, did_hit, handle in rows:
        if entry_id not in cache:
            own = resolver._slot_scores(entry_id)
            cur.execute("""SELECT e2.id FROM challenge_entry e1
                           JOIN challenge_entry e2 ON e2.challenge_id = e1.challenge_id
                                                  AND e2.id <> e1.id
                           WHERE e1.id = %s""", (entry_id,))
            opp_row = cur.fetchone()
            opp = resolver._slot_scores(opp_row[0]) if opp_row else []
            cache[entry_id] = (own, opp, resolver._history(own, None))
        own, opp, hist = cache[entry_id]
        idx = next((i for i, s in enumerate(own) if s["entry_slot_id"] == target_id), None)
        if idx is None:
            skipped += 1
            continue
        obs = play_delta(key, own, opp, hist, idx)
        if obs is None:
            skipped += 1
            continue
        plays.append(obs)
        by_card.setdefault(code, []).append(obs)
        by_player.setdefault(handle, []).append(obs)
    cx.close()

    print(f"=== delta_p over {len(plays)} real tactic plays ===")
    if skipped:
        print(f"  ({skipped} skipped: unsupported condition or missing target)")
    r = summarise(plays)
    if r["delta_p"] is None:
        print("  too few plays to say anything")
        return 1
    print(f"\n  overall     chosen {r['observed']:.3f}   chance {r['chance']:.3f}"
          f"   delta_p {r['delta_p']:+.3f}  [{r['lo']:+.3f}, {r['hi']:+.3f}]")
    print(f"  {'players beat chance' if r['significant'] else 'NO edge detected yet'}")

    print(f"\n  {'card':<14}{'n':>5}{'chosen':>9}{'chance':>9}{'delta_p':>10}{'95% CI':>20}")
    for code in sorted(by_card, key=lambda c: -len(by_card[c])):
        s = summarise(by_card[code])
        if s["delta_p"] is None:
            print(f"  {code:<14}{s['n']:>5}   too few")
            continue
        print(f"  {code:<14}{s['n']:>5}{s['observed']:>9.2f}{s['chance']:>9.2f}"
              f"{s['delta_p']:>+10.3f}   [{s['lo']:+.3f}, {s['hi']:+.3f}]")
    return 0


def main():
    ap = argparse.ArgumentParser(prog="engine.deltap")
    ap.add_argument("command",
                    choices=["validate", "power", "measure", "sensitivity",
                             "neutrality"])
    ap.add_argument("--matches", type=int, default=4000,
                    help="lineups for the large-sample ground truth")
    ap.add_argument("--sample", type=int, default=200,
                    help="plays per replication, i.e. the realistic sample size")
    ap.add_argument("--trials", type=int, default=200)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--resolve", action="store_true",
                    help="also print the hit/miss pairs the measured rates imply")
    args = ap.parse_args()
    return {"validate": cmd_validate, "power": cmd_power, "measure": cmd_measure,
            "sensitivity": cmd_sensitivity,
            "neutrality": cmd_neutrality}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
