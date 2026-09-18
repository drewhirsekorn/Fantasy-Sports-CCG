-- ============================================================================
--  Stage 0 (fantasy point rules) and cold start.
--
--  Measured facts this file encodes:
--   * GameScore is EXACTLY invariant to an affine rescale of fantasy points
--     (max difference 0.0000000000 over 2400 game-lines). Only the RELATIVE
--     weights below matter -- never argue about whether a TD is 4 or 6.
--   * Leaving cameo/garbage game-lines in a peer distribution inflates a true
--     median starter from 50.0 to 54.0. Hence qualification_rule.
--   * Shrinking an unproven player's Form toward the peer prior cuts week-1
--     error from 9.6 to 3.7 (k=6). Without it, a single lucky game yields
--     Form ~90 and a "best 5" deck costs 439 instead of ~280.
--   * A settled league's best possible 5 starters total ~307-322, so the
--     form budget of 340 assumed earlier could NEVER bind. Corrected below.
-- ============================================================================

-- ---------------------------------------------------------------- Stage 0
CREATE TABLE fantasy_rule (
  sport          text NOT NULL REFERENCES sport(code),
  position_scope text NOT NULL DEFAULT 'ALL',   -- 'ALL' or a position_group
  stat_key       text NOT NULL,                 -- key inside player_game_stat.stat_line
  points         numeric(6,3) NOT NULL,
  PRIMARY KEY (sport, position_scope, stat_key)
);

INSERT INTO fantasy_rule (sport, position_scope, stat_key, points) VALUES
 -- NFL, full-PPR relative weights
 ('NFL','ALL','pass_yd',0.04), ('NFL','ALL','pass_td',4),   ('NFL','ALL','pass_int',-2),
 ('NFL','ALL','rush_yd',0.10), ('NFL','ALL','rush_td',6),
 ('NFL','ALL','rec',1.0),      ('NFL','ALL','rec_yd',0.10), ('NFL','ALL','rec_td',6),
 ('NFL','ALL','fumble_lost',-2), ('NFL','ALL','two_pt',2),
 -- NBA
 ('NBA','ALL','pt',1.0),  ('NBA','ALL','reb',1.2), ('NBA','ALL','ast',1.5),
 ('NBA','ALL','stl',3.0), ('NBA','ALL','blk',3.0), ('NBA','ALL','tov',-1.0),
 ('NBA','ALL','dbl_dbl',1.5), ('NBA','ALL','trp_dbl',3.0),
 -- MLB hitters
 ('MLB','BAT','single',3), ('MLB','BAT','double',5), ('MLB','BAT','triple',8),
 ('MLB','BAT','hr',10),    ('MLB','BAT','rbi',2),    ('MLB','BAT','run',2),
 ('MLB','BAT','bb',2),     ('MLB','BAT','hbp',2),    ('MLB','BAT','sb',5),
 ('MLB','BAT','cs',-2),
 -- MLB pitchers (separate peer groups: never comparable to hitters)
 ('MLB','SP','out',0.75), ('MLB','SP','k',2), ('MLB','SP','win',4),
 ('MLB','SP','er',-2), ('MLB','SP','hit_allowed',-0.6), ('MLB','SP','bb_allowed',-0.6),
 ('MLB','SP','complete_game',2.5),
 ('MLB','RP','out',0.75), ('MLB','RP','k',2), ('MLB','RP','win',4),
 ('MLB','RP','er',-2), ('MLB','RP','hit_allowed',-0.6), ('MLB','RP','bb_allowed',-0.6),
 ('MLB','RP','save',5);

-- ------------------------------------------------- peer group granularity
-- Finer groups compare better but estimate worse. Below min_obs a group
-- falls back to parent_group, and a group with no parent falls back to the
-- prior season's table.
CREATE TABLE position_group_config (
  sport          text NOT NULL REFERENCES sport(code),
  position_group text NOT NULL,
  parent_group   text,
  min_obs        int NOT NULL DEFAULT 400,
  PRIMARY KEY (sport, position_group)
);
INSERT INTO position_group_config (sport, position_group, parent_group) VALUES
 ('NFL','QB',NULL), ('NFL','RB',NULL), ('NFL','WR','PASSCATCH'), ('NFL','TE','PASSCATCH'),
 ('NBA','G',NULL),  ('NBA','F',NULL),  ('NBA','C','F'),
 ('MLB','BAT',NULL),('MLB','SP',NULL), ('MLB','RP','SP');

-- ------------------------------------------------------- qualification
-- A game-line joins the peer distribution ONLY if the player actually had a
-- role that day. DNP and cameo lines are handled by replacement_score, not
-- by being dragged into the quantile table.
CREATE TABLE qualification_rule (
  sport     text PRIMARY KEY REFERENCES sport(code),
  stat_key  text NOT NULL,
  min_value numeric(6,2) NOT NULL,
  note      text
);
INSERT INTO qualification_rule VALUES
 ('NFL','snap_share',0.20,'at least 20% of team offensive snaps'),
 ('NBA','minutes',12.0,'at least 12 minutes'),
 ('MLB','appearances',1.0,'started, or faced at least one batter');

-- ---------------------------------------------------------- cold start
ALTER TABLE sport
  ADD COLUMN form_shrink_k        int          NOT NULL DEFAULT 6,
  ADD COLUMN form_prior           numeric(5,2) NOT NULL DEFAULT 50.0,
  ADD COLUMN peer_blend_k         int          NOT NULL DEFAULT 400,
  ADD COLUMN carryover_regression numeric(4,3) NOT NULL DEFAULT 0.35;

COMMENT ON COLUMN sport.form_shrink_k IS
  'Games of peer prior mixed into Form. k=6 measured best; k=0 gives 9.6 error at n=1.';
COMMENT ON COLUMN sport.carryover_regression IS
  'A returning player starts at last season final Form regressed this far toward form_prior.';

-- Form for a player with n games logged. n=0 returns the prior exactly.
CREATE FUNCTION shrunk_form(observed numeric, n int, k int, prior numeric)
RETURNS numeric AS $$
  SELECT round(((n::numeric/(n+k)) * COALESCE(observed, prior))
             + ((k::numeric/(n+k)) * prior), 2);
$$ LANGUAGE sql IMMUTABLE;

-- Weight on the CURRENT season's quantile table; the rest comes from last season's.
CREATE FUNCTION peer_blend_weight(n_obs int, k int)
RETURNS numeric AS $$
  SELECT round(n_obs::numeric / NULLIF(n_obs + k, 0), 4);
$$ LANGUAGE sql IMMUTABLE;

-- A returning player's opening prior.
CREATE FUNCTION carryover_prior(last_form numeric, regression numeric, prior numeric)
RETURNS numeric AS $$
  SELECT round(prior + (1 - regression) * (COALESCE(last_form, prior) - prior), 2);
$$ LANGUAGE sql IMMUTABLE;

-- ------------------------------------------------------- challenge formats
-- form_budget must sit BELOW the best legal 5 (measured 307-322) or it
-- imposes no constraint and deckbuilding has no cost.
CREATE TABLE challenge_format (
  code             text PRIMARY KEY,
  name             text NOT NULL,
  k_games_override int,
  form_budget      int NOT NULL,
  CHECK (form_budget BETWEEN 200 AND 300)
);
INSERT INTO challenge_format VALUES
 ('flash','Flash — one game per card', 1, 275),
 ('series','Series — three games per card', 3, 275),
 ('slate','Slate — fixed weekend window', NULL, 290);
