-- ============================================================================
--  Demo mode: a fixed card pool, no packs, no live data.
--
--  The season is already replayed, so every game a challenge needs is final
--  before the match starts. That lets a match resolve immediately instead of
--  over days -- a tester plays a whole match in one sitting.
--
--  The catch: lock time. arm() picks each starter's next K games AFTER the
--  lock, and real now() is long past the replayed season, so a challenge
--  locked at now() would find no games. demo_lock_at pins locks to a date
--  inside the season with games still ahead of it.
-- ============================================================================
CREATE TABLE IF NOT EXISTS app_config (
  key   text PRIMARY KEY,
  value text NOT NULL
);

COMMENT ON TABLE app_config IS
  'Demo-mode settings. demo_lock_at = the in-season timestamp challenges lock at.';
