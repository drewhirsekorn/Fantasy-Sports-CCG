-- ============================================================================
--  Resolution service support.
--
--  The resolver walks a locked challenge as its games land: it picks each
--  starter's K games, applies the rarity floor, substitutes from the bench on
--  a DNP, fires tactics, and settles.
--
--  Order is load-bearing: FLOOR FIRST, THEN the tactic multiplier. Reversed,
--  a bad game would be amplified before the floor caught it and rarity would
--  stop meaning reliability.
-- ============================================================================

-- Which bench card covered a scratched starter. slot_result already records
-- THAT a substitution happened; this records which card did it, so the UI can
-- show it and support can audit it.
ALTER TABLE slot_result
  ADD COLUMN substitute_slot_id bigint REFERENCES entry_slot(id);

-- A slot is ready to resolve once every game linked to it has a score.
CREATE VIEW slot_readiness AS
SELECT es.id                                   AS entry_slot_id,
       es.entry_id,
       count(esg.game_id)                      AS games_linked,
       count(gs.id)                            AS games_scored,
       count(esg.game_id) > 0
         AND count(esg.game_id) = count(gs.id) AS ready
FROM entry_slot es
LEFT JOIN entry_slot_game esg ON esg.entry_slot_id = es.id
LEFT JOIN game g              ON g.id = esg.game_id
LEFT JOIN game_score gs       ON gs.game_id = esg.game_id
                             AND gs.player_id = es.player_id
WHERE es.slot_type = 'starter'
GROUP BY es.id, es.entry_id;

CREATE INDEX ON entry_slot_game (game_id);
CREATE INDEX ON slot_result (entry_slot_id);
