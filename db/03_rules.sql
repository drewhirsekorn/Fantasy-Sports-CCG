-- ============================================================================
--  Invariants enforced as behavior, not convention.
-- ============================================================================
INSERT INTO sport (code,name,k_games,form_half_life,replacement_score) VALUES
 ('NFL','Football',1,4,15.0), ('NBA','Basketball',1,10,15.0), ('MLB','Baseball',2,15,15.0);
INSERT INTO rarity_floor VALUES
 ('common',0),('uncommon',20),('rare',28),('elite',34),('signature',38);
INSERT INTO craft_cost VALUES
 ('common',40,5),('uncommon',100,15),('rare',400,50),('elite',1600,200),('signature',6400,800);

-- 1. A locked entry is frozen. Stat corrections must not rewrite settled play.
CREATE FUNCTION trg_entry_slot_immutable() RETURNS trigger AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM challenge_entry e
             WHERE e.id = COALESCE(OLD.entry_id, NEW.entry_id)
               AND e.locked_at IS NOT NULL) THEN
    RAISE EXCEPTION 'entry_slot % belongs to a locked entry and is immutable', OLD.id;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER entry_slot_immutable BEFORE UPDATE OR DELETE ON entry_slot
  FOR EACH ROW EXECUTE FUNCTION trg_entry_slot_immutable();

-- is_revealed is the one field that legitimately changes after lock.
CREATE FUNCTION reveal_slot(p_slot_id bigint) RETURNS void AS $$
BEGIN
  ALTER TABLE entry_slot DISABLE TRIGGER entry_slot_immutable;
  UPDATE entry_slot SET is_revealed = true WHERE id = p_slot_id;
  ALTER TABLE entry_slot ENABLE TRIGGER entry_slot_immutable;
END $$ LANGUAGE plpgsql;

-- 2. Ledgers and results are append-only.
CREATE FUNCTION trg_append_only() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION '% is append-only', TG_TABLE_NAME; END $$ LANGUAGE plpgsql;
CREATE TRIGGER dust_append_only BEFORE UPDATE OR DELETE ON dust_ledger
  FOR EACH ROW EXECUTE FUNCTION trg_append_only();
CREATE TRIGGER result_append_only BEFORE UPDATE OR DELETE ON challenge_result
  FOR EACH ROW EXECUTE FUNCTION trg_append_only();
CREATE TRIGGER gamescore_append_only BEFORE UPDATE OR DELETE ON game_score
  FOR EACH ROW EXECUTE FUNCTION trg_append_only();

-- 3. Deck legality, checked at lock. Returns violations; empty = legal.
CREATE FUNCTION validate_entry(p_entry_id bigint) RETURNS TABLE(violation text) AS $$
DECLARE v_budget int;
BEGIN
  SELECT c.form_budget INTO v_budget
    FROM challenge c JOIN challenge_entry e ON e.challenge_id=c.id WHERE e.id=p_entry_id;

  RETURN QUERY SELECT 'starters must be exactly 5, found '||count(*)::text
    FROM entry_slot WHERE entry_id=p_entry_id AND slot_type='starter' HAVING count(*)<>5;
  RETURN QUERY SELECT 'bench must be exactly 3, found '||count(*)::text
    FROM entry_slot WHERE entry_id=p_entry_id AND slot_type='bench' HAVING count(*)<>3;
  RETURN QUERY SELECT 'tactics must be exactly 2, found '||count(*)::text
    FROM entry_slot WHERE entry_id=p_entry_id AND slot_type='tactic' HAVING count(*)<>2;

  -- Counterplay needs opponent reveals to resolve; more than one is degenerate.
  RETURN QUERY SELECT 'at most 1 counterplay tactic, found '||count(*)::text
    FROM entry_slot s JOIN tactic_card t ON t.id=s.tactic_card_id
    WHERE s.entry_id=p_entry_id AND t.requires_opponent_reveal HAVING count(*)>1;

  -- Cross-sport tactics require an actually multi-sport lineup.
  RETURN QUERY SELECT 'cross-sport tactic '||t.name||' requires 2+ sports in lineup'
    FROM entry_slot s JOIN tactic_card t ON t.id=s.tactic_card_id
    WHERE s.entry_id=p_entry_id AND t.requires_multi_sport
      AND (SELECT count(DISTINCT sport) FROM entry_slot
           WHERE entry_id=p_entry_id AND slot_type='starter') < 2;

  -- Same player cannot occupy two lineup slots.
  RETURN QUERY SELECT 'duplicate player in lineup: '||player_id::text
    FROM entry_slot WHERE entry_id=p_entry_id AND slot_type IN ('starter','bench')
    GROUP BY player_id HAVING count(*)>1;

  -- Form budget is snapshotted at lock so legality cannot break mid-challenge.
  RETURN QUERY SELECT 'form budget exceeded: '||sum(form_at_lock)::text||' > '||v_budget::text
    FROM entry_slot WHERE entry_id=p_entry_id AND slot_type='starter'
    HAVING sum(form_at_lock) > v_budget;

  -- A tactic must attach to a starter that exists.
  RETURN QUERY SELECT 'tactic targets missing starter index '||s.tactic_target_index::text
    FROM entry_slot s WHERE s.entry_id=p_entry_id AND s.slot_type='tactic'
      AND s.tactic_target_index IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM entry_slot g WHERE g.entry_id=p_entry_id
                      AND g.slot_type='starter' AND g.slot_index=s.tactic_target_index);
END $$ LANGUAGE plpgsql;

-- 4. Final score derivation, mirroring the validated engine exactly.
CREATE FUNCTION compute_final_score(
  p_raw numeric, p_floor numeric, p_mult numeric) RETURNS numeric AS $$
  SELECT round(greatest(p_raw, p_floor) * p_mult, 2);
$$ LANGUAGE sql IMMUTABLE;

-- 5. A result may only exist for a challenge that actually locked.
CREATE FUNCTION trg_result_requires_lock() RETURNS trigger AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM challenge c WHERE c.id=NEW.challenge_id AND c.locked_at IS NOT NULL)
  THEN RAISE EXCEPTION 'cannot record a result for challenge % before it locks', NEW.challenge_id; END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER result_requires_lock BEFORE INSERT ON challenge_result
  FOR EACH ROW EXECUTE FUNCTION trg_result_requires_lock();
