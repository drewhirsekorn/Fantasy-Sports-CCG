-- ============================================================================
--  API support: authentication, and pre-lock deck validation.
--
--  validate_entry() checks a LOCKED entry, which only exists once a challenge
--  starts. The deck builder needs the same answers while you are still
--  editing, so validate_deck() mirrors it against deck_slot. The two live
--  side by side deliberately -- if a rule changes, both are in view.
-- ============================================================================

-- Dev-grade bearer tokens. Real auth is a prototype-stage decision; this is
-- enough to attach requests to a user and no more. Do not ship it.
CREATE TABLE user_token (
  token        text PRIMARY KEY,
  user_id      bigint NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  created_at   timestamptz NOT NULL DEFAULT now(),
  last_used_at timestamptz
);
CREATE INDEX ON user_token (user_id);

-- Which starter a tactic attaches to, chosen in the builder and carried
-- through to entry_slot.tactic_target_index at lock.
ALTER TABLE deck_slot ADD COLUMN tactic_target_index int;

-- The player behind a deck slot, with the Form that would be snapshotted.
CREATE VIEW deck_slot_detail AS
SELECT ds.deck_id, ds.slot_type, ds.slot_index,
       ds.card_instance_id, ds.tactic_instance_id,
       cp.id            AS card_print_id,
       p.id             AS player_id,
       p.full_name      AS player_name,
       p.sport, p.position_group,
       cp.rarity,
       rf.floor_score,
       latest_form(p.id, current_date) AS form,
       ci.serial_no,
       tc.id   AS tactic_card_id,
       tc.name AS tactic_name,
       tc.requires_opponent_reveal,
       tc.requires_multi_sport,
       ds.tactic_target_index
FROM deck_slot ds
LEFT JOIN card_instance ci   ON ci.id = ds.card_instance_id
LEFT JOIN card_print    cp   ON cp.id = ci.card_print_id
LEFT JOIN player        p    ON p.id  = cp.player_id
LEFT JOIN rarity_floor  rf   ON rf.rarity = cp.rarity
LEFT JOIN tactic_instance ti ON ti.id = ds.tactic_instance_id
LEFT JOIN tactic_card   tc   ON tc.id = ti.tactic_card_id;

-- Same seven rules as validate_entry, checked before lock. Empty = legal.
CREATE FUNCTION validate_deck(p_deck_id bigint, p_budget int)
RETURNS TABLE(violation text) AS $$
BEGIN
  RETURN QUERY SELECT 'starters must be exactly 5, found '||count(*)::text
    FROM deck_slot WHERE deck_id=p_deck_id AND slot_type='starter' HAVING count(*)<>5;
  RETURN QUERY SELECT 'bench must be exactly 3, found '||count(*)::text
    FROM deck_slot WHERE deck_id=p_deck_id AND slot_type='bench' HAVING count(*)<>3;
  RETURN QUERY SELECT 'tactics must be exactly 2, found '||count(*)::text
    FROM deck_slot WHERE deck_id=p_deck_id AND slot_type='tactic' HAVING count(*)<>2;

  RETURN QUERY SELECT 'at most 1 counterplay tactic, found '||count(*)::text
    FROM deck_slot_detail WHERE deck_id=p_deck_id AND slot_type='tactic'
      AND requires_opponent_reveal HAVING count(*)>1;

  RETURN QUERY SELECT 'cross-sport tactic '||tactic_name||' requires 2+ sports in lineup'
    FROM deck_slot_detail WHERE deck_id=p_deck_id AND slot_type='tactic' AND requires_multi_sport
      AND (SELECT count(DISTINCT sport) FROM deck_slot_detail
           WHERE deck_id=p_deck_id AND slot_type='starter') < 2;

  RETURN QUERY SELECT 'duplicate player in lineup: '||player_name
    FROM deck_slot_detail WHERE deck_id=p_deck_id AND slot_type IN ('starter','bench')
    GROUP BY player_name HAVING count(*)>1;

  RETURN QUERY SELECT 'form budget exceeded: '||round(sum(form))::text||' > '||p_budget::text
    FROM deck_slot_detail WHERE deck_id=p_deck_id AND slot_type='starter'
    HAVING sum(form) > p_budget;

  RETURN QUERY SELECT 'a starter has no Form yet: '||player_name
    FROM deck_slot_detail WHERE deck_id=p_deck_id AND slot_type='starter' AND form IS NULL;

  RETURN QUERY SELECT 'tactic '||d.tactic_name||' targets missing starter slot '||d.tactic_target_index::text
    FROM deck_slot_detail d WHERE d.deck_id=p_deck_id AND d.slot_type='tactic'
      AND d.tactic_target_index IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM deck_slot_detail g WHERE g.deck_id=p_deck_id
                      AND g.slot_type='starter' AND g.slot_index=d.tactic_target_index);
END $$ LANGUAGE plpgsql;

CREATE FUNCTION deck_form_total(p_deck_id bigint) RETURNS numeric AS $$
  SELECT COALESCE(round(sum(form), 2), 0) FROM deck_slot_detail
  WHERE deck_id = p_deck_id AND slot_type = 'starter';
$$ LANGUAGE sql STABLE;
