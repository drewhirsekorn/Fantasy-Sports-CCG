-- ============================================================================
-- 12. Telling a bad game apart from no game at all.
--
-- The rarity floor covers a bad GAME, not an absence: a starter who never
-- appeared takes replacement level and no floor rescues it. That rule is
-- enforced in engine/resolution.apply_floor, but until now nothing recorded
-- WHICH case a slot was, so a recap could not say it. A 15.00 with
-- floor_applied = false looks identical to a Common who simply played badly,
-- and a player who loses a match to an injury is owed the reason.
-- ============================================================================

ALTER TABLE slot_result
  ADD COLUMN was_replacement boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN slot_result.was_replacement IS
  'The starter did not play and no bench card covered, so raw_game_score is '
  'the sport''s replacement level. No rarity floor applies to it.';

-- A replacement score is never floored, by definition of the rule above.
ALTER TABLE slot_result
  ADD CONSTRAINT slot_result_no_floor_on_replacement
  CHECK (NOT (was_replacement AND floor_applied));

-- The post-floor score: what the tactic multiplier is applied to, and what a
-- recap means by "before your reads". It is the floor only where the floor
-- actually fired -- which is already exactly what floor_applied records, so a
-- bench substitute's own floor and a replacement's absence of one both fall
-- out of it without re-deriving either.
CREATE VIEW slot_result_detail AS
SELECT sr.*,
       es.entry_id,
       es.slot_index,
       COALESCE(sub.floor_score, es.floor_score) AS floor_score,
       CASE WHEN sr.floor_applied
            THEN COALESCE(sub.floor_score, es.floor_score)
            ELSE sr.raw_game_score END           AS pre_tactic_score
FROM slot_result sr
JOIN entry_slot es       ON es.id = sr.entry_slot_id
LEFT JOIN entry_slot sub ON sub.id = sr.substitute_slot_id;
