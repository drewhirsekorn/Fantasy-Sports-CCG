-- ============================================================================
--  Scoring engine support.
--
--  The engine drains scoring_queue and writes game_score + player_form.
--  Two properties the schema has to protect:
--
--   1. NO LOOKAHEAD. A game is scored against the peer snapshot that existed
--      when it finalised, built only from games that finished strictly
--      before it. Scoring a week-3 game against an end-of-season table leaks
--      the future and would flatter the engine in replay.
--   2. REPRODUCIBILITY. game_score already records its stat revision and its
--      peer_snapshot_id, so any score can be recomputed exactly.
-- ============================================================================

-- A snapshot built before its peer group reached min_obs is usable but not
-- trustworthy. Flagging it lets validation exclude the burn-in period rather
-- than quietly averaging it in.
ALTER TABLE peer_group_snapshot
  ADD COLUMN is_bootstrap boolean NOT NULL DEFAULT false,
  ADD COLUMN source_groups text[];

COMMENT ON COLUMN peer_group_snapshot.is_bootstrap IS
  'Built from fewer than position_group_config.min_obs eligible lines.';
COMMENT ON COLUMN peer_group_snapshot.source_groups IS
  'Position groups pooled to reach min_obs (fallback widens to the parent family).';

CREATE INDEX ON peer_group_snapshot (peer_group_id, as_of DESC);
CREATE INDEX ON player_game_stat (player_id) WHERE peer_eligible;

-- Form history is rewritten as the season advances, so it is NOT append-only;
-- but a given (player, as_of) is fixed once written.
CREATE OR REPLACE FUNCTION latest_form(p_player bigint, p_as_of date)
RETURNS numeric AS $$
  SELECT form FROM player_form
  WHERE player_id = p_player AND as_of <= p_as_of
  ORDER BY as_of DESC LIMIT 1;
$$ LANGUAGE sql STABLE;
