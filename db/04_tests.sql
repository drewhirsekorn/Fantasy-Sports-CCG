-- Cleaner immutability: permit the one field that legitimately changes post-lock.
DROP FUNCTION IF EXISTS reveal_slot(bigint);
CREATE OR REPLACE FUNCTION trg_entry_slot_immutable() RETURNS trigger AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM challenge_entry e
                 WHERE e.id = COALESCE(OLD.entry_id, NEW.entry_id)
                   AND e.locked_at IS NOT NULL) THEN RETURN NEW; END IF;
  IF TG_OP = 'UPDATE' AND NEW IS NOT DISTINCT FROM (OLD #= hstore('is_revealed', NEW.is_revealed::text))
  THEN RETURN NEW; END IF;
  RAISE EXCEPTION 'locked entry_slot % is immutable (only is_revealed may change)', OLD.id;
END $$ LANGUAGE plpgsql;
CREATE EXTENSION IF NOT EXISTS hstore;

-- ----------------------------------------------------------------- seed
INSERT INTO app_user (handle) VALUES ('alice'),('bob');
INSERT INTO team (sport,code,name) VALUES ('NFL','KC','Chiefs'),('NFL','BUF','Bills'),
 ('NBA','BOS','Celtics'),('NBA','DEN','Nuggets');
INSERT INTO player (sport,team_id,full_name,position_group,external_ref)
SELECT CASE WHEN i<=10 THEN 'NFL' ELSE 'NBA' END,
       CASE WHEN i<=10 THEN 1+(i%2) ELSE 3+(i%2) END,
       'Player '||i, CASE WHEN i<=10 THEN 'RB' ELSE 'G' END, 'ext'||i
FROM generate_series(1,20) i;
INSERT INTO peer_group (sport,position_group) VALUES ('NFL','RB'),('NBA','G');
INSERT INTO peer_group_snapshot (peer_group_id,as_of,n_obs,quantiles,q90,q99)
SELECT pg.id,'2026-09-01',5000,
       (SELECT array_agg(round((q*0.4)::numeric,2)) FROM generate_series(0,100) q),
       36.00, 62.00 FROM peer_group pg;
INSERT INTO game (sport,home_team_id,away_team_id,starts_at,final_at)
VALUES ('NFL',1,2,'2026-09-14 17:00Z','2026-09-14 20:15Z'),
       ('NBA',3,4,'2026-09-15 23:00Z','2026-09-16 01:30Z');
INSERT INTO player_game_stat (game_id,player_id,revision,did_play,stat_line,fantasy_points)
SELECT CASE WHEN p.sport='NFL' THEN 1 ELSE 2 END, p.id, 1, true,
       '{"src":"seed"}'::jsonb, 10 + (p.id % 17) FROM player p;
INSERT INTO game_score (game_id,player_id,stat_revision,peer_snapshot_id,fantasy_points,
                        percentile,z_score,game_score)
SELECT s.game_id,s.player_id,1,
       (SELECT ps.id FROM peer_group_snapshot ps JOIN peer_group pg ON pg.id=ps.peer_group_id
        WHERE pg.sport=p.sport LIMIT 1),
       s.fantasy_points, 0.50000, 0.000, 40 + (p.id % 25)
FROM player_game_stat s JOIN player p ON p.id=s.player_id WHERE s.revision=1;

INSERT INTO card_set (code,name,season_year,released_at) VALUES ('S1','Series One',2026,'2026-08-01');
INSERT INTO card_print (set_id,player_id,rarity,print_run_limit,minted_count)
SELECT 1,id,'rare',5000,0 FROM player;
INSERT INTO card_instance (card_print_id,serial_no,owner_id,acquired_via)
SELECT cp.id, 1, CASE WHEN cp.player_id<=10 THEN 1 ELSE 2 END, 'pack' FROM card_print cp;
INSERT INTO tactic_instance (tactic_card_id,owner_id)
SELECT t.id, u.id FROM tactic_card t, app_user u WHERE t.code IN ('WORKHORSE','MISMATCH','COLD_SNAP','LOCKDOWN');

INSERT INTO deck (user_id,name) VALUES (1,'Alice Main');
INSERT INTO challenge (format,status,created_by,opponent_id,form_budget)
VALUES ('flash','locked',1,2,340);
INSERT INTO challenge_entry (challenge_id,user_id,deck_id,form_budget_used)
SELECT 1,1,1,300 UNION ALL SELECT 1,2,1,300;

-- Alice: 5 NFL starters (single-sport), 3 bench, 2 tactics
INSERT INTO entry_slot (entry_id,slot_type,slot_index,card_print_id,player_id,sport,rarity,floor_score,form_at_lock)
SELECT 1,'starter',i,i,i,'NFL','rare',28,60 FROM generate_series(1,5) i;
INSERT INTO entry_slot (entry_id,slot_type,slot_index,card_print_id,player_id,sport,rarity,floor_score,form_at_lock)
SELECT 1,'bench',i-5,i,i,'NFL','rare',28,50 FROM generate_series(6,8) i;
INSERT INTO entry_slot (entry_id,slot_type,slot_index,tactic_card_id,tactic_target_index)
SELECT 1,'tactic',1,(SELECT id FROM tactic_card WHERE code='WORKHORSE'),1;
INSERT INTO entry_slot (entry_id,slot_type,slot_index,tactic_card_id,tactic_target_index)
SELECT 1,'tactic',2,(SELECT id FROM tactic_card WHERE code='MISMATCH'),2;
