"""Prove the standalone build scores a match exactly like the real engine.

The page re-implements engine/resolution.py in JavaScript so it can run from a
link with no server. Two implementations of the same rules is exactly the
setup where one quietly drifts, so: play a real match through the API, feed
BOTH lineups into the page's engine, and compare every slot.

    ./db/dev.sh api && python3 web/parity.py
"""
from __future__ import annotations
import json, os, subprocess, sys, urllib.request, urllib.error

import psycopg2, psycopg2.extras

BASE = os.environ.get("CCG_API", "http://localhost:8000")
DSN = os.environ.get("CCG_DSN", "host=/tmp port=5433 user=ccg dbname=ccg")


def call(method, path, token=None, body=None):
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(req, data) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        sys.exit(f"{method} {path} -> {e.code}: {e.read().decode()[:300]}")


def main() -> int:
    tok = call("POST", "/auth/token", body={"handle": "alice"})["token"]
    col = call("GET", "/collection?limit=200", tok)["items"]
    cx = psycopg2.connect(DSN)
    cur = cx.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    def inst(print_id):
        cur.execute("""SELECT ci.id FROM card_instance ci JOIN app_user u ON u.id=ci.owner_id
                       WHERE ci.card_print_id=%s AND u.handle='alice' LIMIT 1""", (print_id,))
        return cur.fetchone()["id"]

    # Deliberately awkward: lead with the players who do NOT play that week and
    # bench the rest of them, so the run exercises bench substitution AND the
    # replacement rule rather than only the happy path. Cheapest-first after
    # that, to stay inside the budget.
    scratched = {c["id"] for c in json.load(open("web/data.json"))["cards"]
                 if c["score"] is None}
    pool = sorted([c for c in col if c["form"] is not None],
                  key=lambda c: (c["player_id"] not in scratched, float(c["form"])))
    starters, bench, used = [], [], set()
    for c in pool:
        if len(starters) < 5 and c["player_id"] not in used:
            used.add(c["player_id"]); starters.append(c)
    if len({c["sport"] for c in starters}) < 2:                 # tactics need 2 sports
        other = next(c for c in pool if c["sport"] != starters[0]["sport"])
        used.discard(starters[-1]["player_id"]); starters[-1] = other; used.add(other["player_id"])
    for c in pool:
        if len(bench) < 3 and c["player_id"] not in used:
            used.add(c["player_id"]); bench.append(c)
    cur.execute("""SELECT ti.id, tc.code FROM tactic_instance ti
                   JOIN tactic_card tc ON tc.id=ti.tactic_card_id
                   JOIN app_user u ON u.id=ti.owner_id
                   WHERE u.handle='alice' ORDER BY ti.id LIMIT 2""")
    tacs = cur.fetchall()

    deck = call("POST", "/decks", tok, body={"name": "parity"})
    call("PUT", f"/decks/{deck['id']}/slots", tok, body={
        "starters": [{"slot_index": i, "card_instance_id": inst(c["card_print_id"])}
                     for i, c in enumerate(starters, 1)],
        "bench": [{"slot_index": i, "card_instance_id": inst(c["card_print_id"])}
                  for i, c in enumerate(bench, 1)],
        "tactics": [{"slot_index": i, "tactic_instance_id": t["id"], "tactic_target_index": i}
                    for i, t in enumerate(tacs, 1)]})
    ch = call("POST", "/challenges", tok, body={"format": "flash"})
    call("POST", f"/challenges/{ch['id']}/bot", tok)
    call("POST", f"/challenges/{ch['id']}/lock", tok, body={"deck_id": deck["id"]})
    recap = call("POST", f"/challenges/{ch['id']}/play", tok)

    # Read both entries back as the page would see them: player ids in slot
    # order, plus each side's tactic codes in slot order.
    sides = []
    cur.execute("""SELECT e.id, u.handle FROM challenge_entry e JOIN app_user u ON u.id=e.user_id
                   WHERE e.challenge_id=%s ORDER BY e.id""", (ch["id"],))
    for e in cur.fetchall():
        cur.execute("""SELECT slot_type, slot_index, player_id FROM entry_slot
                       WHERE entry_id=%s AND slot_type IN ('starter','bench')
                       ORDER BY slot_type DESC, slot_index""", (e["id"],))
        rows = cur.fetchall()
        cur.execute("""SELECT tc.code FROM entry_slot es JOIN tactic_card tc ON tc.id=es.tactic_card_id
                       WHERE es.entry_id=%s AND es.slot_type='tactic' ORDER BY es.slot_index""",
                    (e["id"],))
        cur2 = cur.fetchall()
        cur.execute("""SELECT es.slot_index, sr.raw_game_score, sr.floor_applied,
                              sr.was_replacement, sr.tactic_multiplier, sr.final_score
                       FROM entry_slot es JOIN slot_result sr ON sr.entry_slot_id=es.id
                       WHERE es.entry_id=%s AND es.slot_type='starter' ORDER BY es.slot_index""",
                    (e["id"],))
        sides.append({
            "handle": e["handle"],
            "starters": [r["player_id"] for r in rows if r["slot_type"] == "starter"],
            "bench": [r["player_id"] for r in rows if r["slot_type"] == "bench"],
            "tactics": [t["code"] for t in cur2],
            "expected": [{k: (float(v) if k != "slot_index" and not isinstance(v, bool) else v)
                          for k, v in r.items()} for r in cur.fetchall()],
        })
    cx.close()

    fixture = {"sides": sides,
               "totals": {r["handle"]: float(r["total_score"]) for r in recap["results"]}}
    with open("/tmp/ccg_parity.json", "w") as f:
        json.dump(fixture, f)

    js = r"""
const fs = require('fs');
const h = fs.readFileSync('web/index.html', 'utf8');
const src = h.slice(h.indexOf('<script>') + 8, h.lastIndexOf('</script>'));
const stub = () => ({ addEventListener(){}, setAttribute(){}, style:{},
  querySelectorAll:()=>[], set innerHTML(v){}, set textContent(v){}, set hidden(v){}, value:'x' });
global.document = { getElementById: stub, querySelectorAll: () => [], addEventListener(){} };
global.window = { scrollTo(){} };
eval(src + ';global.__E = { resolveMatch, DATA };');
const E = global.__E;

const fx = JSON.parse(fs.readFileSync('/tmp/ccg_parity.json', 'utf8'));
const card = id => E.DATA.cards.find(c => c.id === id);
const tac  = code => E.DATA.tactics.find(t => t.code === code);
const mk = s => ({ starters: s.starters.map(card), bench: s.bench.map(card),
                   tactics: s.tactics.map(tac) });
// Side order only decides which one is labelled "you"; scoring is symmetric.
const out = E.resolveMatch(mk(fx.sides[0]), mk(fx.sides[1]));

let fails = 0;
const eq = (label, got, want) => {
  const ok = Math.abs(got - want) < 0.005;
  if(!ok){ fails++; console.log(`  FAIL  ${label}: js ${got} vs engine ${want}`); }
  return ok;
};
fx.sides.forEach((side, i) => {
  const mine = out.sides[i];
  console.log(`  ${side.handle}`);
  side.expected.forEach((e, j) => {
    const s = mine.slots[j];
    const ok = [
      eq(`slot ${e.slot_index} raw`, s.raw, e.raw_game_score),
      eq(`slot ${e.slot_index} multiplier`, s.tactic_multiplier, e.tactic_multiplier),
      eq(`slot ${e.slot_index} final`, s.final_score, e.final_score),
      s.floor_applied === e.floor_applied || (fails++, console.log(
        `  FAIL  slot ${e.slot_index} floor_applied: js ${s.floor_applied} vs ${e.floor_applied}`)),
      s.was_replacement === e.was_replacement || (fails++, console.log(
        `  FAIL  slot ${e.slot_index} was_replacement: js ${s.was_replacement} vs ${e.was_replacement}`)),
    ].every(Boolean);
    console.log(`    ${ok ? 'pass' : 'FAIL'}  slot ${e.slot_index}  raw ${s.raw} `
      + `x${s.tactic_multiplier} = ${s.final_score}`
      + (s.was_replacement ? '  [replacement]' : s.floor_applied ? '  [floored]' : ''));
  });
  eq(`${side.handle} total`, mine.total_score, fx.totals[side.handle]);
  console.log(`    total ${mine.total_score} vs engine ${fx.totals[side.handle]}`);
});
console.log(fails ? `\n  ${fails} MISMATCHES` : '\n  the page scores this match exactly like the engine');
process.exit(fails ? 1 : 0);
"""
    print("=== parity: standalone page vs engine/resolution.py ===")
    return subprocess.call(["node", "-e", js])


if __name__ == "__main__":
    sys.exit(main())
