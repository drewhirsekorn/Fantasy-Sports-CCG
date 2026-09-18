-- ============================================================================
--  Replay harness support.
--
--  Replay exists so the pipeline can be validated on a finished season at
--  100x speed: no live feed contract, no licensing on the critical path,
--  deterministic reruns, and known ground truth.
--
--  Design seam: the harness INGESTS and advances the clock. It does not
--  score. Finalised games land in scoring_queue; the scoring engine drains
--  it. That keeps ingest re-runnable without recomputing anything.
-- ============================================================================

-- Whether a stat line may join its peer group's quantile table. Cameo and
-- garbage lines are excluded: including them inflated a true median starter
-- from 50.0 to 54.0.
ALTER TABLE player_game_stat ADD COLUMN peer_eligible boolean;

-- When the schedule expects this game to end. The replay clock finalises a
-- game once sim_now passes it, so replay can resume from a stored cursor
-- rather than from the harness's memory.
ALTER TABLE game ADD COLUMN scheduled_end timestamptz;

-- ------------------------------------------------------------- Stage 0
-- The fantasy-point formula lives in fantasy_rule and is applied here, so no
-- ingest path can invent its own scoring.
CREATE FUNCTION compute_fantasy_points(p_sport text, p_group text, p_stats jsonb)
RETURNS numeric AS $$
  SELECT COALESCE(round(sum(fr.points * COALESCE((p_stats->>fr.stat_key)::numeric, 0)), 2), 0)
  FROM fantasy_rule fr
  WHERE fr.sport = p_sport
    AND (fr.position_scope = 'ALL' OR fr.position_scope = p_group);
$$ LANGUAGE sql STABLE;

CREATE FUNCTION is_peer_eligible(p_sport text, p_stats jsonb)
RETURNS boolean AS $$
  SELECT COALESCE(
    (SELECT COALESCE((p_stats->>q.stat_key)::numeric, 0) >= q.min_value
     FROM qualification_rule q WHERE q.sport = p_sport), true);
$$ LANGUAGE sql STABLE;

-- Applied on the way in, so a hand-written INSERT gets the same treatment
-- as the harness.
CREATE FUNCTION trg_stage0() RETURNS trigger AS $$
DECLARE v_sport text; v_group text;
BEGIN
  SELECT p.sport, p.position_group INTO v_sport, v_group FROM player p WHERE p.id = NEW.player_id;
  IF NEW.fantasy_points IS NULL THEN
    NEW.fantasy_points := compute_fantasy_points(v_sport, v_group, NEW.stat_line);
  END IF;
  IF NEW.peer_eligible IS NULL THEN
    NEW.peer_eligible := NEW.did_play AND is_peer_eligible(v_sport, NEW.stat_line);
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER stage0 BEFORE INSERT ON player_game_stat
  FOR EACH ROW EXECUTE FUNCTION trg_stage0();

-- ------------------------------------------------------------- the clock
CREATE TABLE replay_run (
  id           bigserial PRIMARY KEY,
  label        text NOT NULL,
  season_year  int  NOT NULL,
  sim_now      timestamptz NOT NULL,     -- the virtual clock
  sim_start    timestamptz NOT NULL,
  sim_end      timestamptz NOT NULL,
  games_loaded int NOT NULL DEFAULT 0,
  games_final  int NOT NULL DEFAULT 0,
  started_at   timestamptz NOT NULL DEFAULT now(),
  finished_at  timestamptz,
  CHECK (sim_now BETWEEN sim_start AND sim_end)
);

-- ------------------------------------------------- seam to the scoring engine
CREATE TABLE scoring_queue (
  game_id      bigint PRIMARY KEY REFERENCES game(id),
  enqueued_at  timestamptz NOT NULL DEFAULT now(),
  sim_final_at timestamptz NOT NULL,     -- when it finalised in replay time
  processed_at timestamptz,
  attempts     int NOT NULL DEFAULT 0
);
CREATE INDEX ON scoring_queue (sim_final_at) WHERE processed_at IS NULL;

-- Natural keys so replay is idempotent: re-running a tick inserts nothing new.
CREATE UNIQUE INDEX game_natural_key
  ON game (sport, home_team_id, away_team_id, starts_at);
