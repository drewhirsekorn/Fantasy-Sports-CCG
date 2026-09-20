"""HTTP API for the cross-sport fantasy CCG.

Backs the designed screens: collection, card detail, deck builder, the live
challenge board, and the post-match recap.

Writes are deliberately narrow. The scoring and resolution services own
game_score, slot_result and challenge_result; the API never writes them. It
creates decks and challenges, and snapshots a deck into entry_slots at lock.

    uvicorn api.main:app --port 8000
    open http://localhost:8000/docs
"""
from __future__ import annotations
import secrets

import os

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from api.db import DSN, cursor, one, rows

app = FastAPI(
    title="Fantasy Sports CCG",
    version="0.1.0",
    description="Cross-sport CCG. Cards are real athletes; GameScore is rank-normalised "
                "so any sport can be played against any other.",
)


# --------------------------------------------------------------------- auth
def auth(authorization: str = Header(None)) -> dict:
    """Bearer token -> user. Prototype-grade: no expiry, no refresh."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    token = authorization.split(None, 1)[1].strip()
    with cursor(commit=True) as cur:
        cur.execute("""UPDATE user_token SET last_used_at = now() WHERE token = %s
                       RETURNING user_id""", (token,))
        row = one(cur)
        if not row:
            raise HTTPException(401, "unknown token")
        cur.execute("SELECT id, handle FROM app_user WHERE id = %s", (row["user_id"],))
        return one(cur)


class TokenRequest(BaseModel):
    handle: str = Field(min_length=1, max_length=40)


@app.post("/auth/token", tags=["auth"])
def issue_token(body: TokenRequest):
    """DEV ONLY. Creates the user if absent and hands back a token."""
    token = secrets.token_urlsafe(24)
    with cursor(commit=True) as cur:
        cur.execute("""INSERT INTO app_user (handle) VALUES (%s)
                       ON CONFLICT (handle) DO NOTHING""", (body.handle,))
        cur.execute("SELECT id, handle FROM app_user WHERE handle = %s", (body.handle,))
        user = one(cur)
        cur.execute("INSERT INTO user_token (token, user_id) VALUES (%s, %s)",
                    (token, user["id"]))
    return {"token": token, **user}


@app.get("/me", tags=["auth"])
def me(user: dict = Depends(auth)):
    return user


# --------------------------------------------------------------- collection
@app.get("/collection", tags=["collection"])
def collection(user: dict = Depends(auth),
               sport: str | None = None,
               rarity: str | None = None,
               duplicates_only: bool = False,
               sort: str = Query("form", pattern="^(form|trend|rarity|name)$"),
               limit: int = Query(100, le=500)):
    """Owned cards, grouped by print. Trend is the 7-day change in Form."""
    order = {"form": "form DESC NULLS LAST", "trend": "trend DESC NULLS LAST",
             "rarity": "cp.rarity DESC, form DESC NULLS LAST",
             "name": "p.full_name"}[sort]
    with cursor() as cur:
        cur.execute(f"""
            SELECT cp.id AS card_print_id, p.id AS player_id, p.full_name AS name,
                   p.sport, p.position_group, t.code AS team,
                   cp.rarity, rf.floor_score,
                   latest_form(p.id, current_date) AS form,
                   latest_form(p.id, current_date)
                     - latest_form(p.id, (current_date - 7)) AS trend,
                   count(*) AS copies, min(ci.serial_no) AS best_serial,
                   min(ci.id) AS instance_id, cp.print_run_limit
            FROM card_instance ci
            JOIN card_print cp ON cp.id = ci.card_print_id
            JOIN player     p  ON p.id  = cp.player_id
            LEFT JOIN team  t  ON t.id  = p.team_id
            JOIN rarity_floor rf ON rf.rarity = cp.rarity
            WHERE ci.owner_id = %s
              AND (%s IS NULL OR p.sport = %s)
              AND (%s IS NULL OR cp.rarity::text = %s)
            GROUP BY cp.id, p.id, p.full_name, p.sport, p.position_group,
                     t.code, cp.rarity, rf.floor_score, cp.print_run_limit
            HAVING (NOT %s OR count(*) > 1)
            ORDER BY {order}
            LIMIT %s""",
            (user["id"], sport, sport, rarity, rarity, duplicates_only, limit))
        items = rows(cur)
        cur.execute("""SELECT count(*) AS cards, count(DISTINCT card_print_id) AS unique_prints
                       FROM card_instance WHERE owner_id = %s""", (user["id"],))
        totals = one(cur)
    return {"totals": totals, "items": items}


@app.get("/cards/{card_print_id}", tags=["collection"])
def card_detail(card_print_id: int, user: dict = Depends(auth)):
    """One card: its live Form, the history behind it, and recent games."""
    with cursor() as cur:
        cur.execute("""
            SELECT cp.id AS card_print_id, p.id AS player_id, p.full_name AS name,
                   p.sport, p.position_group, t.code AS team, cp.rarity,
                   rf.floor_score, cp.print_run_limit,
                   latest_form(p.id, current_date) AS form,
                   latest_form(p.id, current_date)
                     - latest_form(p.id, (current_date - 7)) AS trend
            FROM card_print cp
            JOIN player p ON p.id = cp.player_id
            LEFT JOIN team t ON t.id = p.team_id
            JOIN rarity_floor rf ON rf.rarity = cp.rarity
            WHERE cp.id = %s""", (card_print_id,))
        card = one(cur)
        if not card:
            raise HTTPException(404, "no such card")

        cur.execute("""SELECT as_of, form, games_in_window FROM player_form
                       WHERE player_id = %s ORDER BY as_of DESC LIMIT 30""",
                    (card["player_id"],))
        history = list(reversed(rows(cur)))

        cur.execute("""
            SELECT g.final_at::date AS date, gs.game_score, gs.fantasy_points,
                   CASE WHEN g.home_team_id = p.team_id
                        THEN 'vs ' || away.code ELSE 'at ' || home.code END AS opponent
            FROM game_score gs
            JOIN game   g    ON g.id = gs.game_id
            JOIN player p    ON p.id = gs.player_id
            JOIN team   home ON home.id = g.home_team_id
            JOIN team   away ON away.id = g.away_team_id
            WHERE gs.player_id = %s
            ORDER BY g.final_at DESC LIMIT 10""", (card["player_id"],))
        games = rows(cur)

        cur.execute("""SELECT count(*) AS copies, min(serial_no) AS best_serial
                       FROM card_instance WHERE card_print_id = %s AND owner_id = %s""",
                    (card_print_id, user["id"]))
        owned = one(cur)
    return {"card": card, "owned": owned, "form_history": history, "recent_games": games}


@app.get("/my/tactics", tags=["collection"])
def my_tactics(user: dict = Depends(auth)):
    """Owned tactic copies, with the instance ids a deck slot needs."""
    with cursor() as cur:
        cur.execute("""SELECT ti.id AS instance_id, tc.code, tc.name, tc.family,
                              tc.condition_key, tc.base_rate, tc.hit_mult, tc.miss_mult,
                              tc.card_class, tc.rarity, tc.requires_opponent_reveal,
                              tc.requires_multi_sport
                       FROM tactic_instance ti
                       JOIN tactic_card tc ON tc.id = ti.tactic_card_id
                       WHERE ti.owner_id = %s
                       ORDER BY tc.family, tc.name""", (user["id"],))
        return {"items": rows(cur)}


@app.get("/tactics", tags=["collection"])
def tactics():
    """The printed tactic set. hit/miss average to x1.00 at the base rate."""
    with cursor() as cur:
        cur.execute("""SELECT code, name, family, condition_key, base_rate, hit_mult,
                              miss_mult, card_class, rarity, legal_sports,
                              requires_opponent_reveal, requires_multi_sport
                       FROM tactic_card ORDER BY family, name""")
        return {"items": rows(cur)}


# -------------------------------------------------------------------- decks
class DeckCreate(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class Slot(BaseModel):
    slot_index: int = Field(ge=1, le=5)
    card_instance_id: int | None = None
    tactic_instance_id: int | None = None
    tactic_target_index: int | None = None


class DeckSlots(BaseModel):
    starters: list[Slot] = []
    bench: list[Slot] = []
    tactics: list[Slot] = []


def _owned_deck(cur, deck_id: int, user_id: int) -> dict:
    cur.execute("SELECT * FROM deck WHERE id = %s", (deck_id,))
    deck = one(cur)
    if not deck:
        raise HTTPException(404, "no such deck")
    if deck["user_id"] != user_id:
        raise HTTPException(403, "not your deck")
    return deck


def _default_budget(cur) -> int:
    cur.execute("SELECT form_budget FROM challenge_format WHERE code = 'flash'")
    return one(cur)["form_budget"]


@app.get("/decks", tags=["decks"])
def list_decks(user: dict = Depends(auth)):
    with cursor() as cur:
        cur.execute("""SELECT d.id, d.name, d.updated_at, deck_form_total(d.id) AS form_total
                       FROM deck d WHERE d.user_id = %s ORDER BY d.updated_at DESC""",
                    (user["id"],))
        return {"items": rows(cur)}


@app.post("/decks", tags=["decks"], status_code=201)
def create_deck(body: DeckCreate, user: dict = Depends(auth)):
    with cursor(commit=True) as cur:
        cur.execute("INSERT INTO deck (user_id, name) VALUES (%s, %s) RETURNING id, name",
                    (user["id"], body.name))
        return one(cur)


@app.get("/decks/{deck_id}", tags=["decks"])
def get_deck(deck_id: int, user: dict = Depends(auth)):
    """Slots, the Form total, and the same seven legality rules the lock enforces."""
    with cursor() as cur:
        deck = _owned_deck(cur, deck_id, user["id"])
        budget = _default_budget(cur)
        cur.execute("""SELECT slot_type, slot_index, card_instance_id, tactic_instance_id,
                              card_print_id, player_id, player_name, sport, position_group,
                              rarity, floor_score, form, serial_no, tactic_card_id,
                              tactic_name, tactic_target_index
                       FROM deck_slot_detail WHERE deck_id = %s
                       ORDER BY slot_type, slot_index""", (deck_id,))
        slots = rows(cur)
        cur.execute("SELECT deck_form_total(%s) AS total", (deck_id,))
        total = one(cur)["total"]
        cur.execute("SELECT violation FROM validate_deck(%s, %s)", (deck_id, budget))
        violations = [r["violation"] for r in cur.fetchall()]
    return {
        "deck": {"id": deck["id"], "name": deck["name"]},
        "budget": {"limit": budget, "used": total, "remaining": budget - (total or 0)},
        "slots": slots,
        "legal": not violations,
        "violations": violations,
    }


@app.put("/decks/{deck_id}/slots", tags=["decks"])
def set_slots(deck_id: int, body: DeckSlots, user: dict = Depends(auth)):
    """Replace the whole lineup. Ownership of every card is checked."""
    with cursor(commit=True) as cur:
        _owned_deck(cur, deck_id, user["id"])
        groups = (("starter", body.starters), ("bench", body.bench), ("tactic", body.tactics))

        for kind, items in groups:
            for s in items:
                if kind == "tactic":
                    if not s.tactic_instance_id:
                        raise HTTPException(422, f"{kind} slot {s.slot_index} needs a tactic_instance_id")
                    cur.execute("SELECT owner_id FROM tactic_instance WHERE id = %s",
                                (s.tactic_instance_id,))
                else:
                    if not s.card_instance_id:
                        raise HTTPException(422, f"{kind} slot {s.slot_index} needs a card_instance_id")
                    cur.execute("SELECT owner_id FROM card_instance WHERE id = %s",
                                (s.card_instance_id,))
                owner = one(cur)
                if not owner or owner["owner_id"] != user["id"]:
                    raise HTTPException(403, f"{kind} slot {s.slot_index}: card is not yours")

        cur.execute("DELETE FROM deck_slot WHERE deck_id = %s", (deck_id,))
        for kind, items in groups:
            for s in items:
                cur.execute("""INSERT INTO deck_slot
                        (deck_id, slot_type, slot_index, card_instance_id,
                         tactic_instance_id, tactic_target_index)
                        VALUES (%s, %s, %s, %s, %s, %s)""",
                            (deck_id, kind, s.slot_index, s.card_instance_id,
                             s.tactic_instance_id, s.tactic_target_index))
        cur.execute("UPDATE deck SET updated_at = now() WHERE id = %s", (deck_id,))
    return get_deck(deck_id, user)


# --------------------------------------------------------------- challenges
class ChallengeCreate(BaseModel):
    format: str = Field(default="flash", pattern="^(flash|series|slate)$")


class LockRequest(BaseModel):
    deck_id: int


@app.get("/challenges", tags=["challenges"])
def list_challenges(user: dict = Depends(auth)):
    with cursor() as cur:
        cur.execute("""
            SELECT c.id, c.format, c.status, c.form_budget, c.created_at,
                   c.locked_at, c.resolved_at,
                   cu.handle AS created_by, ou.handle AS opponent,
                   EXISTS (SELECT 1 FROM challenge_entry e
                           WHERE e.challenge_id = c.id AND e.user_id = %s
                             AND e.locked_at IS NOT NULL) AS you_locked
            FROM challenge c
            JOIN app_user cu ON cu.id = c.created_by
            LEFT JOIN app_user ou ON ou.id = c.opponent_id
            WHERE c.created_by = %s OR c.opponent_id = %s
            ORDER BY c.created_at DESC""", (user["id"], user["id"], user["id"]))
        mine = rows(cur)
        cur.execute("""SELECT c.id, c.format, c.form_budget, c.created_at, u.handle AS created_by
                       FROM challenge c JOIN app_user u ON u.id = c.created_by
                       WHERE c.status = 'open' AND c.opponent_id IS NULL
                         AND c.created_by <> %s
                       ORDER BY c.created_at DESC LIMIT 25""", (user["id"],))
        return {"mine": mine, "open_board": rows(cur)}


@app.post("/challenges", tags=["challenges"], status_code=201)
def create_challenge(body: ChallengeCreate, user: dict = Depends(auth)):
    with cursor(commit=True) as cur:
        cur.execute("SELECT form_budget FROM challenge_format WHERE code = %s", (body.format,))
        fmt = one(cur)
        if not fmt:
            raise HTTPException(422, "unknown format")
        cur.execute("""INSERT INTO challenge (format, status, created_by, form_budget)
                       VALUES (%s, 'open', %s, %s)
                       RETURNING id, format, status, form_budget""",
                    (body.format, user["id"], fmt["form_budget"]))
        return one(cur)


@app.post("/challenges/{challenge_id}/accept", tags=["challenges"])
def accept(challenge_id: int, user: dict = Depends(auth)):
    with cursor(commit=True) as cur:
        cur.execute("SELECT * FROM challenge WHERE id = %s", (challenge_id,))
        ch = one(cur)
        if not ch:
            raise HTTPException(404, "no such challenge")
        if ch["opponent_id"] is not None:
            raise HTTPException(409, "already accepted")
        if ch["created_by"] == user["id"]:
            raise HTTPException(409, "cannot accept your own challenge")
        cur.execute("""UPDATE challenge SET opponent_id = %s, status = 'accepted'
                       WHERE id = %s RETURNING id, status""", (user["id"], challenge_id))
        return one(cur)


@app.post("/challenges/{challenge_id}/lock", tags=["challenges"])
def lock(challenge_id: int, body: LockRequest, user: dict = Depends(auth)):
    """Freeze a deck into the challenge.

    The entry keeps its OWN copy of every slot with the Form snapshotted now,
    so editing the deck afterwards cannot change a match in flight.
    """
    with cursor(commit=True) as cur:
        cur.execute("SELECT * FROM challenge WHERE id = %s", (challenge_id,))
        ch = one(cur)
        if not ch:
            raise HTTPException(404, "no such challenge")
        if user["id"] not in (ch["created_by"], ch["opponent_id"]):
            raise HTTPException(403, "not your challenge")
        if ch["status"] not in ("open", "accepted"):
            raise HTTPException(409, f"challenge is {ch['status']}")
        _owned_deck(cur, body.deck_id, user["id"])

        cur.execute("SELECT violation FROM validate_deck(%s, %s)",
                    (body.deck_id, ch["form_budget"]))
        violations = [r["violation"] for r in cur.fetchall()]
        if violations:
            raise HTTPException(422, {"error": "deck is not legal", "violations": violations})

        cur.execute("SELECT value FROM app_config WHERE key = 'demo_lock_at'")
        cfg = one(cur)
        lock_ts = cfg["value"] if cfg else None

        cur.execute("""INSERT INTO challenge_entry
                         (challenge_id, user_id, deck_id, form_budget_used, locked_at)
                       VALUES (%s, %s, %s, deck_form_total(%s),
                               COALESCE(%s::timestamptz, now()))
                       ON CONFLICT (challenge_id, user_id) DO NOTHING
                       RETURNING id""",
                    (challenge_id, user["id"], body.deck_id, body.deck_id, lock_ts))
        entry = one(cur)
        if not entry:
            raise HTTPException(409, "you have already locked this challenge")
        entry_id = entry["id"]

        # Two of five starters are revealed at lock; the rest stay hidden
        # until they resolve.
        cur.execute("""INSERT INTO entry_slot
                (entry_id, slot_type, slot_index, card_print_id, player_id, sport,
                 rarity, floor_score, form_at_lock, is_revealed)
            SELECT %s, slot_type, slot_index, card_print_id, player_id, sport,
                   rarity, floor_score, form,
                   (slot_type = 'starter' AND slot_index <= 2)
            FROM deck_slot_detail
            WHERE deck_id = %s AND slot_type IN ('starter', 'bench')""",
                    (entry_id, body.deck_id))
        cur.execute("""INSERT INTO entry_slot
                (entry_id, slot_type, slot_index, tactic_card_id, tactic_target_index)
            SELECT %s, 'tactic', slot_index, tactic_card_id,
                   COALESCE(tactic_target_index, slot_index)
            FROM deck_slot_detail WHERE deck_id = %s AND slot_type = 'tactic'""",
                    (entry_id, body.deck_id))

        # The challenge locks once both sides have.
        cur.execute("""SELECT count(*) AS n FROM challenge_entry
                       WHERE challenge_id = %s AND locked_at IS NOT NULL""", (challenge_id,))
        both = one(cur)["n"] >= 2
        if both:
            cur.execute("""UPDATE challenge
                           SET status = 'locked',
                               locked_at = COALESCE(%s::timestamptz, now())
                           WHERE id = %s AND locked_at IS NULL""", (lock_ts, challenge_id))
    return {"challenge_id": challenge_id, "entry_id": entry_id,
            "both_locked": both,
            "status": "locked" if both else "waiting for opponent"}


def _board(cur, challenge_id: int, viewer_id: int, reveal_all: bool) -> dict:
    cur.execute("""
        SELECT e.id AS entry_id, e.user_id, u.handle,
               es.slot_index, es.is_revealed, es.sport, es.rarity,
               es.floor_score, es.form_at_lock, p.full_name AS name,
               sr.raw_game_score, sr.floor_applied, sr.substituted_from_bench,
               sr.was_replacement, sr.pre_tactic_score,
               sr.tactic_multiplier, sr.final_score
        FROM challenge_entry e
        JOIN app_user u ON u.id = e.user_id
        JOIN entry_slot es ON es.entry_id = e.id AND es.slot_type = 'starter'
        LEFT JOIN player p ON p.id = es.player_id
        LEFT JOIN slot_result_detail sr ON sr.entry_slot_id = es.id
        WHERE e.challenge_id = %s
        ORDER BY e.id, es.slot_index""", (challenge_id,))
    sides: dict[int, dict] = {}
    for r in rows(cur):
        side = sides.setdefault(r["entry_id"], {
            "entry_id": r["entry_id"], "handle": r["handle"],
            "is_you": r["user_id"] == viewer_id, "total": 0.0, "landed": 0, "slots": [],
        })
        resolved = r["final_score"] is not None
        # Hide an opponent's unrevealed, unresolved cards.
        visible = side["is_you"] or reveal_all or r["is_revealed"] or resolved
        side["slots"].append({
            "slot_index": r["slot_index"],
            "name": r["name"] if visible else None,
            "sport": r["sport"] if visible else None,
            "rarity": r["rarity"] if visible else None,
            "hidden": not visible,
            "was_revealed_at_lock": r["is_revealed"],
            "form_at_lock": r["form_at_lock"] if visible else None,
            "raw_game_score": r["raw_game_score"],
            "floor_applied": r["floor_applied"],
            "substituted_from_bench": r["substituted_from_bench"],
            # A starter who never appeared and had no bench cover. The score
            # is replacement level and NO rarity floor applied to it, which a
            # recap has to be able to say out loud -- otherwise it reads as
            # an ordinary bad game.
            "was_replacement": r["was_replacement"],
            "pre_tactic_score": r["pre_tactic_score"],
            "tactic_multiplier": r["tactic_multiplier"],
            "final_score": r["final_score"],
        })
        if resolved:
            side["total"] += float(r["final_score"])
            side["landed"] += 1
    for s in sides.values():
        s["total"] = round(s["total"], 2)
    return {"sides": list(sides.values())}


def _tactic_log(cur, challenge_id: int) -> list[dict]:
    cur.execute("""
        SELECT u.handle, tc.name, tr.condition_key, tr.did_hit, tr.multiplier,
               tr.evidence, target.slot_index AS target_slot_index,
               tp.full_name AS target_name
        FROM tactic_resolution tr
        JOIN entry_slot es ON es.id = tr.entry_slot_id
        JOIN challenge_entry e ON e.id = es.entry_id
        JOIN app_user u ON u.id = e.user_id
        JOIN tactic_card tc ON tc.id = tr.tactic_card_id
        LEFT JOIN entry_slot target ON target.id = tr.target_slot_id
        LEFT JOIN player tp ON tp.id = target.player_id
        WHERE e.challenge_id = %s
        ORDER BY u.handle, tr.id""", (challenge_id,))
    return rows(cur)


@app.get("/challenges/{challenge_id}", tags=["challenges"])
def challenge_board(challenge_id: int, user: dict = Depends(auth)):
    """The live board. Opponent cards stay masked until revealed or resolved."""
    with cursor() as cur:
        cur.execute("""SELECT c.*, cu.handle AS created_by_handle, ou.handle AS opponent_handle
                       FROM challenge c
                       JOIN app_user cu ON cu.id = c.created_by
                       LEFT JOIN app_user ou ON ou.id = c.opponent_id
                       WHERE c.id = %s""", (challenge_id,))
        ch = one(cur)
        if not ch:
            raise HTTPException(404, "no such challenge")
        if user["id"] not in (ch["created_by"], ch["opponent_id"]):
            raise HTTPException(403, "not your challenge")
        resolved = ch["status"] == "resolved"
        board = _board(cur, challenge_id, user["id"], reveal_all=resolved)
        log = _tactic_log(cur, challenge_id) if resolved else []
        total_slots = sum(len(s["slots"]) for s in board["sides"])
        landed = sum(s["landed"] for s in board["sides"])
    return {
        "challenge": {"id": ch["id"], "format": ch["format"], "status": ch["status"],
                      "form_budget": ch["form_budget"], "locked_at": ch["locked_at"],
                      "resolved_at": ch["resolved_at"]},
        "progress": {"cards_landed": landed, "cards_total": total_slots},
        **board,
        "tactic_log": log,
    }


@app.get("/challenges/{challenge_id}/recap", tags=["challenges"])
def recap(challenge_id: int, user: dict = Depends(auth)):
    """A settled match, with the counterfactual that explains it.

    pre_tactic_total is the post-floor score before any multiplier, so a
    client can say 'without your reads you lose by 5.7' rather than just
    showing the final number. Post-floor, not max(raw, floor): a starter who
    never played took replacement level and no floor applied to it, so
    re-flooring here would credit a side for points the match never scored.
    """
    with cursor() as cur:
        cur.execute("SELECT * FROM challenge WHERE id = %s", (challenge_id,))
        ch = one(cur)
        if not ch:
            raise HTTPException(404, "no such challenge")
        if user["id"] not in (ch["created_by"], ch["opponent_id"]):
            raise HTTPException(403, "not your challenge")
        if ch["status"] != "resolved":
            raise HTTPException(409, "challenge has not settled yet")

        cur.execute("""
            SELECT e.id AS entry_id, u.handle, e.user_id,
                   cr.total_score, cr.is_winner,
                   round(sum(sr.pre_tactic_score), 2) AS pre_tactic_total,
                   round(sum(sr.final_score - sr.pre_tactic_score), 2) AS tactic_swing
            FROM challenge_result cr
            JOIN challenge_entry e ON e.id = cr.entry_id
            JOIN app_user u ON u.id = e.user_id
            JOIN entry_slot es ON es.entry_id = e.id AND es.slot_type = 'starter'
            JOIN slot_result_detail sr ON sr.entry_slot_id = es.id
            WHERE cr.challenge_id = %s
            GROUP BY e.id, u.handle, e.user_id, cr.total_score, cr.is_winner
            ORDER BY cr.total_score DESC""", (challenge_id,))
        results = rows(cur)
        board = _board(cur, challenge_id, user["id"], reveal_all=True)
        log = _tactic_log(cur, challenge_id)

    counterfactual = None
    if len(results) == 2:
        a, b = results
        final_margin = float(a["total_score"]) - float(b["total_score"])
        pre_margin = float(a["pre_tactic_total"]) - float(b["pre_tactic_total"])
        counterfactual = {
            "winner": a["handle"],
            "final_margin": round(final_margin, 2),
            "pre_tactic_margin": round(pre_margin, 2),
            # The interesting case: tactics reversed the result.
            "tactics_decided_it": (final_margin > 0) != (pre_margin > 0),
        }
    return {"challenge_id": challenge_id, "results": results,
            "counterfactual": counterfactual, **board, "tactic_log": log}


def _autobuild_deck(cur, user_id: int, budget: int, name: str) -> int:
    """A legal lineup for the demo opponent.

    Spans two sports (so cross-sport tactics are legal), fits the budget, and
    takes at most one counterplay tactic -- the same rules a human faces.
    """
    import random as _rnd
    cur.execute("""
        SELECT ci.id AS instance_id, cp.player_id, p.sport,
               latest_form(p.id, current_date) AS form
        FROM card_instance ci
        JOIN card_print cp ON cp.id = ci.card_print_id
        JOIN player p ON p.id = cp.player_id
        WHERE ci.owner_id = %s AND latest_form(p.id, current_date) IS NOT NULL""",
                (user_id,))
    pool = rows(cur)
    _rnd.shuffle(pool)

    starters, bench, used, spent = [], [], set(), 0.0

    # Reserve room for the slots still to fill, priced on the cheapest cards
    # STILL AVAILABLE -- reserving against the pool minimum overstates what is
    # left once that card is taken, and the last slot misses by a fraction.
    def affordable(c):
        need = 4 - len(starters)
        rest = sorted(float(x["form"]) for x in pool
                      if x["player_id"] not in used and x["player_id"] != c["player_id"])
        return spent + float(c["form"]) + sum(rest[:need]) <= budget

    for sport in sorted({c["sport"] for c in pool}):        # one of each sport first
        for c in pool:
            if c["sport"] == sport and c["player_id"] not in used and affordable(c):
                used.add(c["player_id"]); spent += float(c["form"]); starters.append(c)
                break
    for c in sorted(pool, key=lambda c: -float(c["form"])):  # then best that still fits
        if len(starters) == 5:
            break
        if c["player_id"] in used or not affordable(c):
            continue
        used.add(c["player_id"]); spent += float(c["form"]); starters.append(c)
    for c in pool:
        if len(bench) == 3:
            break
        if c["player_id"] in used:
            continue
        used.add(c["player_id"]); bench.append(c)
    if len(starters) < 5 or len(bench) < 3:
        raise HTTPException(500, "opponent could not field a legal lineup")

    cur.execute("""SELECT ti.id, tc.requires_opponent_reveal
                   FROM tactic_instance ti JOIN tactic_card tc ON tc.id = ti.tactic_card_id
                   WHERE ti.owner_id = %s""", (user_id,))
    tac = rows(cur)
    _rnd.shuffle(tac)
    picked, counterplay = [], 0
    for t in tac:
        if len(picked) == 2:
            break
        if t["requires_opponent_reveal"]:
            if counterplay:                                  # max one per deck
                continue
            counterplay += 1
        picked.append(t)

    cur.execute("INSERT INTO deck (user_id, name) VALUES (%s, %s) RETURNING id",
                (user_id, name))
    deck_id = one(cur)["id"]
    for kind, group in (("starter", starters), ("bench", bench)):
        for i, c in enumerate(group, 1):
            cur.execute("""INSERT INTO deck_slot (deck_id, slot_type, slot_index, card_instance_id)
                           VALUES (%s, %s, %s, %s)""", (deck_id, kind, i, c["instance_id"]))
    for i, t in enumerate(picked, 1):
        cur.execute("""INSERT INTO deck_slot
                         (deck_id, slot_type, slot_index, tactic_instance_id, tactic_target_index)
                       VALUES (%s, 'tactic', %s, %s, %s)""", (deck_id, i, t["id"], i))
    return deck_id


@app.post("/challenges/{challenge_id}/bot", tags=["challenges"])
def bot_opponent(challenge_id: int, user: dict = Depends(auth)):
    """Demo mode: have the house accept and lock a legal lineup."""
    with cursor(commit=True) as cur:
        cur.execute("SELECT * FROM challenge WHERE id = %s", (challenge_id,))
        ch = one(cur)
        if not ch:
            raise HTTPException(404, "no such challenge")
        if ch["created_by"] != user["id"]:
            raise HTTPException(403, "not your challenge")

        cur.execute("""SELECT id, handle FROM app_user WHERE id <> %s
                       ORDER BY id LIMIT 1""", (user["id"],))
        opp = one(cur)
        if not opp:
            raise HTTPException(409, "no opponent account exists")

        if ch["opponent_id"] is None:
            cur.execute("""UPDATE challenge SET opponent_id = %s, status = 'accepted'
                           WHERE id = %s""", (opp["id"], challenge_id))

        cur.execute("""SELECT 1 FROM challenge_entry
                       WHERE challenge_id = %s AND user_id = %s""", (challenge_id, opp["id"]))
        if one(cur):
            return {"opponent": opp["handle"], "already_locked": True}

        deck_id = _autobuild_deck(cur, opp["id"], ch["form_budget"],
                                  f"house deck #{challenge_id}")
        cur.execute("SELECT violation FROM validate_deck(%s, %s)", (deck_id, ch["form_budget"]))
        bad = [r["violation"] for r in cur.fetchall()]
        if bad:
            raise HTTPException(500, {"error": "opponent built an illegal deck", "violations": bad})

        cur.execute("SELECT value FROM app_config WHERE key = 'demo_lock_at'")
        cfg = one(cur)
        lock_ts = cfg["value"] if cfg else None
        cur.execute("""INSERT INTO challenge_entry
                         (challenge_id, user_id, deck_id, form_budget_used, locked_at)
                       VALUES (%s, %s, %s, deck_form_total(%s),
                               COALESCE(%s::timestamptz, now()))
                       RETURNING id""",
                    (challenge_id, opp["id"], deck_id, deck_id, lock_ts))
        entry_id = one(cur)["id"]
        cur.execute("""INSERT INTO entry_slot
                (entry_id, slot_type, slot_index, card_print_id, player_id, sport,
                 rarity, floor_score, form_at_lock, is_revealed)
            SELECT %s, slot_type, slot_index, card_print_id, player_id, sport,
                   rarity, floor_score, form, (slot_type = 'starter' AND slot_index <= 2)
            FROM deck_slot_detail WHERE deck_id = %s AND slot_type IN ('starter','bench')""",
                    (entry_id, deck_id))
        cur.execute("""INSERT INTO entry_slot
                (entry_id, slot_type, slot_index, tactic_card_id, tactic_target_index)
            SELECT %s, 'tactic', slot_index, tactic_card_id,
                   COALESCE(tactic_target_index, slot_index)
            FROM deck_slot_detail WHERE deck_id = %s AND slot_type = 'tactic'""",
                    (entry_id, deck_id))
        cur.execute("""SELECT count(*) AS n FROM challenge_entry
                       WHERE challenge_id = %s AND locked_at IS NOT NULL""", (challenge_id,))
        if one(cur)["n"] >= 2:
            cur.execute("""UPDATE challenge SET status = 'locked',
                             locked_at = COALESCE(%s::timestamptz, now())
                           WHERE id = %s AND locked_at IS NULL""", (lock_ts, challenge_id))
    return {"opponent": opp["handle"], "locked": True}


@app.post("/challenges/{challenge_id}/play", tags=["challenges"])
def play(challenge_id: int, user: dict = Depends(auth)):
    """Demo mode: settle the match now.

    With no live data the whole season is already final, so every game a
    challenge needs has been played. This arms and resolves in one call
    instead of waiting days, then hands back the recap.
    """
    import psycopg2
    from engine.resolution import Resolver

    with cursor() as cur:
        cur.execute("SELECT * FROM challenge WHERE id = %s", (challenge_id,))
        ch = one(cur)
        if not ch:
            raise HTTPException(404, "no such challenge")
        if user["id"] not in (ch["created_by"], ch["opponent_id"]):
            raise HTTPException(403, "not your challenge")
        if ch["status"] == "resolved":
            return recap(challenge_id, user)
        cur.execute("""SELECT count(*) AS n FROM challenge_entry
                       WHERE challenge_id = %s AND locked_at IS NOT NULL""", (challenge_id,))
        if one(cur)["n"] < 2:
            raise HTTPException(409, "both sides must lock first")

    # Resolver unpacks tuple rows, so it needs a plain cursor rather than the
    # dict cursor the rest of the API uses.
    cx = psycopg2.connect(DSN)
    try:
        with cx.cursor() as rc:
            resolver = Resolver(rc)
            resolver.arm(challenge_id)
            _totals, err = resolver.resolve(challenge_id)
        cx.commit()
    finally:
        cx.close()
    if err:
        raise HTTPException(409, err)
    return recap(challenge_id, user)


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"))


@app.get("/trivia", include_in_schema=False)
def trivia_page():
    """The daily trivia game. A built file, not a rendered one.

    It carries its own day inlined and needs nothing from this database, so
    serving it here is a convenience rather than a dependency: the same file
    works opened off disk or from a static host.
    """
    page = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web", "trivia.html")
    if not os.path.exists(page):
        raise HTTPException(404, "not built yet -- run python3 -m trivia.build")
    # One day at a time, and the day turns at 07:00 ET, so a long cache would
    # serve an expired set to anyone who loaded the page before the rollover.
    return FileResponse(page, headers={"Cache-Control": "no-cache"})


@app.get("/health", tags=["ops"])
def health():
    with cursor() as cur:
        cur.execute("SELECT count(*) AS tactics FROM tactic_card")
        return {"ok": True, **one(cur)}
