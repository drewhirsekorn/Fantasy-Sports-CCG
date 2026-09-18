-- ============================================================================
--  CROSS-SPORT FANTASY CCG - core data model
--  Design invariants this schema is built to protect:
--   1. Scores must be REPRODUCIBLE. A GameScore depends on which stat revision
--      and which peer-group snapshot produced it, so both are recorded.
--   2. Resolved play is IMMUTABLE. Leagues issue stat corrections days later;
--      a settled challenge must never silently change.
--   3. Decks MUTATE, entries do not. An entry freezes its own copy at lock.
--   4. Tactic payoffs must be MEAN-NEUTRAL at base rate - enforced in-schema.
-- ============================================================================
CREATE TYPE rarity      AS ENUM ('common','uncommon','rare','elite','signature');
CREATE TYPE slot_type   AS ENUM ('starter','bench','tactic');
CREATE TYPE chal_status AS ENUM ('open','accepted','locked','live','resolved','void');
CREATE TYPE tactic_class AS ENUM ('TUNED','GAMBLE');

-- ---------------------------------------------------------------- reference
CREATE TABLE sport (
  code              text PRIMARY KEY,                 -- NFL / NBA / MLB
  name              text NOT NULL,
  k_games           int  NOT NULL CHECK (k_games >= 1),   -- games/card to resolve
  form_half_life    int  NOT NULL,                        -- in games
  replacement_score numeric(5,2) NOT NULL DEFAULT 15.0    -- DNP fallback
);
CREATE TABLE team (
  id     bigserial PRIMARY KEY,
  sport  text NOT NULL REFERENCES sport(code),
  code   text NOT NULL, name text NOT NULL,
  UNIQUE (sport, code)
);
CREATE TABLE player (
  id            bigserial PRIMARY KEY,
  sport         text NOT NULL REFERENCES sport(code),
  team_id       bigint REFERENCES team(id),
  full_name     text NOT NULL,
  position_group text NOT NULL,                       -- peer-group axis
  external_ref  text UNIQUE                           -- provider id
);
CREATE TABLE game (
  id          bigserial PRIMARY KEY,
  sport       text NOT NULL REFERENCES sport(code),
  home_team_id bigint NOT NULL REFERENCES team(id),
  away_team_id bigint NOT NULL REFERENCES team(id),
  starts_at   timestamptz NOT NULL,
  final_at    timestamptz,
  CHECK (home_team_id <> away_team_id)
);
-- Stat lines are versioned: a correction inserts a NEW revision, never an update.
CREATE TABLE player_game_stat (
  game_id    bigint NOT NULL REFERENCES game(id),
  player_id  bigint NOT NULL REFERENCES player(id),
  revision   int    NOT NULL DEFAULT 1,
  did_play   boolean NOT NULL,
  stat_line  jsonb  NOT NULL,
  fantasy_points numeric(7,2),
  recorded_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (game_id, player_id, revision)
);

-- ---------------------------------------------------------------- scoring
CREATE TABLE peer_group (
  id    bigserial PRIMARY KEY,
  sport text NOT NULL REFERENCES sport(code),
  position_group text NOT NULL,
  UNIQUE (sport, position_group)
);
-- Quantile tables drift as the season progresses; every snapshot is retained
-- so any historical GameScore can be recomputed exactly.
CREATE TABLE peer_group_snapshot (
  id            bigserial PRIMARY KEY,
  peer_group_id bigint NOT NULL REFERENCES peer_group(id),
  as_of         date NOT NULL,
  n_obs         int  NOT NULL CHECK (n_obs > 0),
  quantiles     numeric(7,2)[] NOT NULL,              -- 101 entries, p0..p100
  q90           numeric(7,2) NOT NULL,
  q99           numeric(7,2) NOT NULL,
  UNIQUE (peer_group_id, as_of),
  CHECK (array_length(quantiles,1) = 101),
  CHECK (q99 > q90)
);
CREATE TABLE game_score (
  id             bigserial PRIMARY KEY,
  game_id        bigint NOT NULL REFERENCES game(id),
  player_id      bigint NOT NULL REFERENCES player(id),
  stat_revision  int    NOT NULL,
  peer_snapshot_id bigint NOT NULL REFERENCES peer_group_snapshot(id),
  fantasy_points numeric(7,2) NOT NULL,
  percentile     numeric(6,5) NOT NULL CHECK (percentile BETWEEN 0 AND 1),
  z_score        numeric(6,3) NOT NULL,
  tail_bonus     numeric(6,3) NOT NULL DEFAULT 0,
  game_score     numeric(5,2) NOT NULL CHECK (game_score BETWEEN 0 AND 100),
  computed_at    timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (game_id, player_id, stat_revision)
    REFERENCES player_game_stat(game_id, player_id, revision),
  UNIQUE (game_id, player_id, stat_revision, peer_snapshot_id)
);
CREATE TABLE player_form (
  player_id bigint NOT NULL REFERENCES player(id),
  as_of     date   NOT NULL,
  form      numeric(5,2) NOT NULL CHECK (form BETWEEN 0 AND 100),
  games_in_window int NOT NULL,
  PRIMARY KEY (player_id, as_of)
);

-- ---------------------------------------------------------------- catalog
CREATE TABLE card_set (
  id bigserial PRIMARY KEY,
  code text UNIQUE NOT NULL, name text NOT NULL,
  season_year int NOT NULL, released_at date NOT NULL, rotates_at date
);
CREATE TABLE rarity_floor (           -- rarity buys RELIABILITY, not ceiling
  rarity rarity PRIMARY KEY,
  floor_score numeric(5,2) NOT NULL CHECK (floor_score <= 38)  -- >38 = pay-to-win
);
CREATE TABLE trait (
  id bigserial PRIMARY KEY, code text UNIQUE NOT NULL,
  name text NOT NULL, effect jsonb NOT NULL
);
-- A "card" as a design object: player x set x rarity. Player-independent of quality.
CREATE TABLE card_print (
  id bigserial PRIMARY KEY,
  set_id bigint NOT NULL REFERENCES card_set(id),
  player_id bigint NOT NULL REFERENCES player(id),
  rarity rarity NOT NULL,
  print_run_limit int CHECK (print_run_limit IS NULL OR print_run_limit > 0),
  minted_count int NOT NULL DEFAULT 0 CHECK (minted_count >= 0),
  UNIQUE (set_id, player_id, rarity),
  CHECK (print_run_limit IS NULL OR minted_count <= print_run_limit)
);
CREATE TABLE card_print_trait (
  card_print_id bigint NOT NULL REFERENCES card_print(id),
  trait_id bigint NOT NULL REFERENCES trait(id),
  slot_index int NOT NULL CHECK (slot_index BETWEEN 1 AND 3),
  PRIMARY KEY (card_print_id, slot_index)
);
-- The 36 tactics. Mean-neutrality is a CHECK, not a convention.
CREATE TABLE tactic_card (
  id bigserial PRIMARY KEY,
  code text UNIQUE NOT NULL, name text NOT NULL, family text NOT NULL,
  condition_key text NOT NULL,                 -- engine dispatch key
  condition_params jsonb NOT NULL DEFAULT '{}',
  base_rate numeric(4,3) NOT NULL CHECK (base_rate > 0 AND base_rate < 1),
  hit_mult  numeric(4,2) NOT NULL CHECK (hit_mult > 1),
  miss_mult numeric(4,2) NOT NULL CHECK (miss_mult >= 0.35),  -- floor: harsher unplayable
  delta_p   numeric(4,3) NOT NULL,
  skill_edge numeric(5,3) NOT NULL,
  card_class tactic_class NOT NULL,
  legal_sports text[] NOT NULL,
  rarity rarity NOT NULL,
  requires_opponent_reveal boolean NOT NULL DEFAULT false,
  requires_multi_sport boolean NOT NULL DEFAULT false,
  CONSTRAINT mean_neutral CHECK (
    abs(base_rate*hit_mult + (1-base_rate)*miss_mult - 1) < 0.005 )
);

-- ---------------------------------------------------------------- collection
CREATE TABLE app_user (
  id bigserial PRIMARY KEY, handle text UNIQUE NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE card_instance (
  id bigserial PRIMARY KEY,
  card_print_id bigint NOT NULL REFERENCES card_print(id),
  serial_no int NOT NULL CHECK (serial_no > 0),
  owner_id bigint REFERENCES app_user(id),
  acquired_via text NOT NULL,                   -- pack | craft | trade
  acquired_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (card_print_id, serial_no)
);
CREATE TABLE tactic_instance (
  id bigserial PRIMARY KEY,
  tactic_card_id bigint NOT NULL REFERENCES tactic_card(id),
  owner_id bigint NOT NULL REFERENCES app_user(id),
  acquired_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE pack_product (
  id bigserial PRIMARY KEY, code text UNIQUE NOT NULL, name text NOT NULL,
  set_id bigint NOT NULL REFERENCES card_set(id),
  pool_scope text NOT NULL CHECK (pool_scope IN ('set','sport','team')),
  pool_ref text,                                -- narrows the 560-pack problem
  cards_per_pack int NOT NULL DEFAULT 5,
  pity_elite_every int NOT NULL DEFAULT 10,
  pity_signature_every int NOT NULL DEFAULT 60
);
CREATE TABLE pack_slot_odds (
  pack_product_id bigint NOT NULL REFERENCES pack_product(id),
  slot_index int NOT NULL, rarity rarity NOT NULL,
  weight numeric(6,4) NOT NULL CHECK (weight >= 0),
  PRIMARY KEY (pack_product_id, slot_index, rarity)
);
CREATE TABLE user_pity (
  user_id bigint NOT NULL REFERENCES app_user(id),
  pack_product_id bigint NOT NULL REFERENCES pack_product(id),
  since_elite int NOT NULL DEFAULT 0, since_signature int NOT NULL DEFAULT 0,
  PRIMARY KEY (user_id, pack_product_id)
);
CREATE TABLE pack_opening (             -- auditable: counters + seed retained
  id bigserial PRIMARY KEY,
  user_id bigint NOT NULL REFERENCES app_user(id),
  pack_product_id bigint NOT NULL REFERENCES pack_product(id),
  opened_at timestamptz NOT NULL DEFAULT now(),
  pity_elite_before int NOT NULL, pity_signature_before int NOT NULL,
  rng_seed bigint NOT NULL
);
CREATE TABLE pack_opening_card (
  pack_opening_id bigint NOT NULL REFERENCES pack_opening(id),
  slot_index int NOT NULL,
  card_instance_id bigint NOT NULL REFERENCES card_instance(id),
  was_pity boolean NOT NULL DEFAULT false,
  PRIMARY KEY (pack_opening_id, slot_index)
);
CREATE TABLE bounty (                   -- "I want MY guy" targeting
  user_id bigint NOT NULL REFERENCES app_user(id),
  player_id bigint NOT NULL REFERENCES player(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, player_id)
);
CREATE TABLE dust_ledger (              -- append-only
  id bigserial PRIMARY KEY,
  user_id bigint NOT NULL REFERENCES app_user(id),
  delta int NOT NULL CHECK (delta <> 0),
  reason text NOT NULL, ref_type text, ref_id bigint,
  created_at timestamptz NOT NULL DEFAULT now()
);
-- Craft cost keyed to RARITY ONLY. Pricing by Form would rebuild pay-to-win.
CREATE TABLE craft_cost (
  rarity rarity PRIMARY KEY,
  dust_cost int NOT NULL CHECK (dust_cost > 0),
  dust_refund int NOT NULL CHECK (dust_refund > 0)
);

-- ---------------------------------------------------------------- play
CREATE TABLE deck (
  id bigserial PRIMARY KEY,
  user_id bigint NOT NULL REFERENCES app_user(id),
  name text NOT NULL, updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE deck_slot (
  deck_id bigint NOT NULL REFERENCES deck(id) ON DELETE CASCADE,
  slot_type slot_type NOT NULL, slot_index int NOT NULL,
  card_instance_id bigint REFERENCES card_instance(id),
  tactic_instance_id bigint REFERENCES tactic_instance(id),
  PRIMARY KEY (deck_id, slot_type, slot_index),
  CHECK ( (slot_type = 'tactic') = (tactic_instance_id IS NOT NULL) ),
  CHECK ( (slot_type <> 'tactic') = (card_instance_id IS NOT NULL) )
);
CREATE TABLE challenge (
  id bigserial PRIMARY KEY,
  format text NOT NULL CHECK (format IN ('flash','series','slate')),
  status chal_status NOT NULL DEFAULT 'open',
  created_by bigint NOT NULL REFERENCES app_user(id),
  opponent_id bigint REFERENCES app_user(id),
  form_budget int NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  locked_at timestamptz, resolved_at timestamptz,
  CHECK (resolved_at IS NULL OR locked_at IS NOT NULL)
);
CREATE TABLE challenge_entry (
  id bigserial PRIMARY KEY,
  challenge_id bigint NOT NULL REFERENCES challenge(id),
  user_id bigint NOT NULL REFERENCES app_user(id),
  deck_id bigint NOT NULL REFERENCES deck(id),   -- provenance only
  form_budget_used numeric(6,2) NOT NULL,
  locked_at timestamptz,
  UNIQUE (challenge_id, user_id)
);
-- The frozen copy. Decks change after lock; this must not.
CREATE TABLE entry_slot (
  id bigserial PRIMARY KEY,
  entry_id bigint NOT NULL REFERENCES challenge_entry(id),
  slot_type slot_type NOT NULL, slot_index int NOT NULL,
  card_print_id bigint REFERENCES card_print(id),
  player_id bigint REFERENCES player(id),
  sport text REFERENCES sport(code),
  rarity rarity, floor_score numeric(5,2),
  form_at_lock numeric(5,2),
  is_revealed boolean NOT NULL DEFAULT false,
  tactic_card_id bigint REFERENCES tactic_card(id),
  tactic_target_index int,                        -- which starter it attaches to
  UNIQUE (entry_id, slot_type, slot_index),
  CHECK ( (slot_type = 'tactic') = (tactic_card_id IS NOT NULL) ),
  CHECK ( (slot_type <> 'tactic') = (card_print_id IS NOT NULL) )
);
CREATE TABLE entry_slot_game (          -- which real games counted for this card
  entry_slot_id bigint NOT NULL REFERENCES entry_slot(id),
  sequence int NOT NULL,
  game_id bigint NOT NULL REFERENCES game(id),
  game_score_id bigint REFERENCES game_score(id),
  PRIMARY KEY (entry_slot_id, sequence)
);
CREATE TABLE slot_result (
  entry_slot_id bigint PRIMARY KEY REFERENCES entry_slot(id),
  raw_game_score numeric(5,2) NOT NULL,
  floor_applied boolean NOT NULL DEFAULT false,
  substituted_from_bench boolean NOT NULL DEFAULT false,
  tactic_multiplier numeric(4,2) NOT NULL DEFAULT 1.0,
  final_score numeric(6,2) NOT NULL,
  resolved_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE tactic_resolution (        -- audit trail: what fired, and why
  id bigserial PRIMARY KEY,
  entry_slot_id bigint NOT NULL REFERENCES entry_slot(id),
  tactic_card_id bigint NOT NULL REFERENCES tactic_card(id),
  target_slot_id bigint REFERENCES entry_slot(id),
  condition_key text NOT NULL,
  did_hit boolean NOT NULL,
  multiplier numeric(4,2) NOT NULL,
  evidence jsonb NOT NULL,
  resolved_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE challenge_result (
  challenge_id bigint NOT NULL REFERENCES challenge(id),
  entry_id bigint NOT NULL REFERENCES challenge_entry(id),
  total_score numeric(7,2) NOT NULL,
  is_winner boolean NOT NULL,
  tiebreak_top_card numeric(6,2),
  resolved_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (challenge_id, entry_id)
);
CREATE INDEX ON game_score (player_id, computed_at DESC);
CREATE INDEX ON card_instance (owner_id) WHERE owner_id IS NOT NULL;
CREATE INDEX ON entry_slot (entry_id);
CREATE INDEX ON challenge (status) WHERE status IN ('open','live');
