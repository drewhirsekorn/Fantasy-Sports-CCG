\set ON_ERROR_STOP off
CREATE OR REPLACE FUNCTION expect_fail(sql text, label text) RETURNS text AS $$
BEGIN EXECUTE sql; RETURN 'FAIL  '||label||' (should have been rejected)';
EXCEPTION WHEN others THEN RETURN 'pass  '||label; END $$ LANGUAGE plpgsql;
CREATE OR REPLACE FUNCTION expect_ok(sql text, label text) RETURNS text AS $$
BEGIN EXECUTE sql; RETURN 'pass  '||label;
EXCEPTION WHEN others THEN RETURN 'FAIL  '||label||' -> '||SQLERRM; END $$ LANGUAGE plpgsql;

-- Lock the entry and settle the challenge.
UPDATE challenge SET locked_at = now(), status='locked' WHERE id=1;
UPDATE challenge_entry SET locked_at = now() WHERE id IN (1,2);
INSERT INTO slot_result (entry_slot_id,raw_game_score,floor_applied,tactic_multiplier,final_score)
SELECT s.id, 45.00, false, 1.00, compute_final_score(45.00, s.floor_score, 1.00)
FROM entry_slot s WHERE s.entry_id=1 AND s.slot_type='starter';
INSERT INTO challenge_result (challenge_id,entry_id,total_score,is_winner)
VALUES (1,1,225.00,true),(1,2,210.00,false);
UPDATE challenge SET status='resolved', resolved_at=now() WHERE id=1;

\echo '=== Immutability ==='
SELECT expect_fail($$UPDATE entry_slot SET form_at_lock=99 WHERE entry_id=1 AND slot_index=1 AND slot_type='starter'$$,
                   'locked entry_slot cannot be edited');
SELECT expect_fail($$DELETE FROM entry_slot WHERE entry_id=1 AND slot_type='starter' AND slot_index=1$$,
                   'locked entry_slot cannot be deleted');
SELECT expect_ok($$UPDATE entry_slot SET is_revealed=true WHERE entry_id=1 AND slot_type='starter' AND slot_index=1$$,
                 'is_revealed MAY change after lock');
INSERT INTO dust_ledger (user_id,delta,reason) VALUES (1,50,'dupe');
SELECT expect_fail($$UPDATE dust_ledger SET delta=99999 WHERE user_id=1$$, 'dust_ledger is append-only');
SELECT expect_fail($$UPDATE game_score SET game_score=99 WHERE id=1$$, 'game_score is append-only');
SELECT expect_fail($$DELETE FROM challenge_result WHERE challenge_id=1$$, 'challenge_result is append-only');

\echo ''
\echo '=== Design guardrails ==='
SELECT expect_fail($$INSERT INTO rarity_floor VALUES ('uncommon',46)$$,
                   'rarity floor >38 rejected (pay-to-win cliff)');
SELECT expect_fail($$INSERT INTO tactic_card (code,name,family,condition_key,base_rate,hit_mult,
  miss_mult,delta_p,skill_edge,card_class,legal_sports,rarity)
  VALUES ('CHEAT','Cheat','Outcome','cheat',0.50,1.50,0.90,0.1,0.1,'TUNED',ARRAY['NFL'],'rare')$$,
  'non-mean-neutral tactic rejected (E=1.20)');
SELECT expect_fail($$INSERT INTO tactic_card (code,name,family,condition_key,base_rate,hit_mult,
  miss_mult,delta_p,skill_edge,card_class,legal_sports,rarity)
  VALUES ('BRUTAL','Brutal','Outcome','brutal',0.70,1.15,0.15,0.1,0.1,'TUNED',ARRAY['NFL'],'rare')$$,
  'miss multiplier below 0.35 floor rejected');
SELECT expect_fail($$INSERT INTO card_print (set_id,player_id,rarity,print_run_limit,minted_count)
  VALUES (1,1,'elite',100,101)$$, 'mint beyond print run rejected');

\echo ''
\echo '=== Stat correction after settlement (the headline invariant) ==='
INSERT INTO player_game_stat (game_id,player_id,revision,did_play,stat_line,fantasy_points)
VALUES (1,1,2,true,'{"src":"league correction"}'::jsonb, 99.00);
INSERT INTO game_score (game_id,player_id,stat_revision,peer_snapshot_id,fantasy_points,percentile,z_score,game_score)
VALUES (1,1,2,1,99.00,0.99000,2.330,92.00);
SELECT 'revisions on file: '||count(*)::text FROM game_score WHERE game_id=1 AND player_id=1;
SELECT 'settled total still: '||total_score::text FROM challenge_result WHERE entry_id=1;
SELECT CASE WHEN total_score=225.00 THEN 'pass  settled result unaffected by correction'
            ELSE 'FAIL  result mutated' END FROM challenge_result WHERE entry_id=1;

\echo ''
\echo '=== Deck legality ==='
SELECT 'legal entry violations: '||coalesce(count(*),0)::text FROM validate_entry(1);
SELECT expect_fail($$INSERT INTO challenge_result (challenge_id,entry_id,total_score,is_winner) VALUES (2,9,1,true)$$, 'result before lock rejected');
INSERT INTO challenge (id,format,status,created_by,opponent_id,form_budget) VALUES (2,'flash','open',1,2,340);
INSERT INTO challenge_entry (id,challenge_id,user_id,deck_id,form_budget_used) VALUES (9,2,1,1,999);
INSERT INTO entry_slot (entry_id,slot_type,slot_index,card_print_id,player_id,sport,rarity,floor_score,form_at_lock)
SELECT 9,'starter',i,i,1,'NFL','rare',28,90 FROM generate_series(1,4) i;   -- 4 starters, dup player, over budget
INSERT INTO entry_slot (entry_id,slot_type,slot_index,tactic_card_id)
SELECT 9,'tactic',1,(SELECT id FROM tactic_card WHERE code='COLD_SNAP');
INSERT INTO entry_slot (entry_id,slot_type,slot_index,tactic_card_id)
SELECT 9,'tactic',2,(SELECT id FROM tactic_card WHERE code='LOCKDOWN');
INSERT INTO entry_slot (entry_id,slot_type,slot_index,tactic_card_id)
SELECT 9,'tactic',3,(SELECT id FROM tactic_card WHERE code='TWO_SPORT');
SELECT '  - '||violation FROM validate_entry(9);

\echo ''
\echo '=== Scoring order (floor applied BEFORE tactic multiplier) ==='
SELECT 'raw 22 vs floor 28, x1.33 => '||compute_final_score(22,28,1.33)::text||'  (expect 37.24)';
SELECT 'raw 60 vs floor 28, x0.67 => '||compute_final_score(60,28,0.67)::text||'  (expect 40.20)';

\echo ''
\echo '=== The floor covers a bad game, not an absence ==='
-- A starter who never played takes replacement level and keeps it. Recording
-- a floored replacement would mean rarity insured against an injury, so the
-- database refuses to record one at all.
SELECT expect_fail($$INSERT INTO slot_result
                       (entry_slot_id,raw_game_score,floor_applied,was_replacement,
                        tactic_multiplier,final_score)
                     VALUES ((SELECT id FROM entry_slot WHERE entry_id=9 AND slot_index=1
                              AND slot_type='starter'), 15.00, true, true, 1.00, 28.00)$$,
                   'a replacement score cannot be floored');
SELECT expect_ok($$INSERT INTO slot_result
                     (entry_slot_id,raw_game_score,floor_applied,was_replacement,
                      tactic_multiplier,final_score)
                   VALUES ((SELECT id FROM entry_slot WHERE entry_id=9 AND slot_index=1
                            AND slot_type='starter'), 15.00, false, true, 1.00, 15.00)$$,
                 'a replacement score keeps replacement level');
SELECT CASE WHEN pre_tactic_score = 15.00 THEN 'pass  pre-tactic score of a DNP is not lifted to the floor'
            ELSE 'FAIL  pre-tactic score lifted to '||pre_tactic_score::text END
FROM slot_result_detail
WHERE entry_slot_id = (SELECT id FROM entry_slot WHERE entry_id=9 AND slot_index=1
                       AND slot_type='starter');
