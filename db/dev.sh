#!/usr/bin/env bash
# Local dev database for the fantasy CCG.
#
# Postgres does not survive a container restart and nothing restarts it, so
# every session otherwise repeats the same manual dance. This script is that
# dance, made idempotent.
#
#   ./db/dev.sh up        start the server (initdb on first run)
#   ./db/dev.sh load      create the database and apply every migration in order
#   ./db/dev.sh demo      replay a season, score it, settle a challenge
#   ./db/dev.sh prototype replay + score + a FIXED card pool (no packs, no live data)
#   ./db/dev.sh reset     drop, reload, and re-run the demo from scratch
#   ./db/dev.sh test      migrations + fixtures + assertions on a scratch db
#   ./db/dev.sh reproducible  build the season twice and compare, hash by hash
#   ./db/dev.sh api       start the API (detached, pidfile-tracked)
#   ./db/dev.sh api-stop  stop it
#   ./db/dev.sh status    what is currently in there
#   ./db/dev.sh psql      open a shell on it
#
# A cold container needs:  ./db/dev.sh up && ./db/dev.sh load && ./db/dev.sh demo
set -euo pipefail

# Find the server binaries. pg_ctl and initdb are not on PATH under Debian's
# packaging or Postgres.app, so "postgres is installed" and "this script can
# start it" are different questions and the answer has to be looked up.
find_pgbin() {
  local c
  c=$(command -v pg_ctl 2>/dev/null) && { dirname "$c"; return; }
  for c in /usr/lib/postgresql/*/bin \
           /opt/homebrew/opt/postgresql@*/bin /usr/local/opt/postgresql@*/bin \
           /opt/homebrew/bin /usr/local/bin \
           /Applications/Postgres.app/Contents/Versions/*/bin; do
    [ -x "$c/pg_ctl" ] && { echo "$c"; return; }
  done
  echo "no postgres server binaries found (looked for pg_ctl)." >&2
  echo "  debian/ubuntu: apt install postgresql-16" >&2
  echo "  macos:         brew install postgresql@16" >&2
  exit 1
}
PGBIN=${PGBIN:-$(find_pgbin)}
PGDATA=${PGDATA:-/tmp/pgdata_ccg}
PGPORT=${PGPORT:-5433}
PGHOST=${PGHOST:-/tmp}
PGUSER=${PGUSER:-ccg}
PGDB=${PGDB:-ccg}
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$PGBIN:$PATH"
export CCG_DSN="host=$PGHOST port=$PGPORT user=$PGUSER dbname=$PGDB"

# initdb refuses to run as root, so the server runs as an unprivileged user.
PGRUNAS=$(id -u postgres >/dev/null 2>&1 && echo postgres || echo "$(id -un)")
as_pg() { if [ "$(id -un)" = "$PGRUNAS" ]; then bash -c "$1"; else su "$PGRUNAS" -s /bin/bash -c "$1"; fi; }

running() { pg_isready -h "$PGHOST" -p "$PGPORT" >/dev/null 2>&1; }

cmd_up() {
  if running; then echo "server already up on $PGHOST:$PGPORT"; return; fi
  if [ ! -s "$PGDATA/PG_VERSION" ]; then
    echo "initialising $PGDATA"
    rm -rf "$PGDATA"; mkdir -p "$PGDATA"; chown -R "$PGRUNAS" "$PGDATA"
    as_pg "PATH=$PGBIN:\$PATH initdb -D $PGDATA -U $PGUSER --auth=trust" >/dev/null
  fi
  chown -R "$PGRUNAS" "$PGDATA" 2>/dev/null || true
  as_pg "PATH=$PGBIN:\$PATH pg_ctl -D $PGDATA -l $PGDATA/log \
         -o '-k $PGHOST -p $PGPORT -c listen_addresses=' start" >/dev/null
  for _ in $(seq 1 20); do running && break; sleep 0.5; done
  running && echo "server up on $PGHOST:$PGPORT" || { echo "FAILED - last log lines:"; tail -20 "$PGDATA/log"; exit 1; }
}

cmd_load() {
  running || cmd_up
  createdb -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" "$PGDB" 2>/dev/null || true
  apply_sql "$PGDB" "${MIGRATIONS[@]}"
}

# Explicit, because a glob got this wrong once: 04/05 are the end-to-end
# scenario and its assertions. They seed fixtures the replay demo must not
# see, 05 depends on 04, and 05 sets ON_ERROR_STOP off -- so loading it
# alone reports "ok" while every statement silently fails. Tests are a
# separate command against a scratch database.
MIGRATIONS=(01_schema 02_seed_tactics 03_rules 06_scoring_and_coldstart
            07_replay 08_scoring 09_resolution 10_api 11_demo 12_recap)
TESTS=(04_tests 05_assertions)

apply_sql() {
  local db="$1"; shift
  for name in "$@"; do
    local f="$ROOT/db/${name}.sql"
    [ -e "$f" ] || { echo "  missing $name.sql"; exit 1; }
    printf '  %-34s' "$name.sql"
    if psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$db" -v ON_ERROR_STOP=1 -q -f "$f" >/tmp/ccg_load.log 2>&1
    then echo "ok"; else echo "FAILED"; head -5 /tmp/ccg_load.log; exit 1; fi
  done
}

# Migrations + fixtures + assertions on a throwaway database, so the dev
# database is never polluted with test rows.
cmd_test() {
  running || cmd_up
  local tdb="${PGDB}_test"
  dropdb --force -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" "$tdb" 2>/dev/null || true
  createdb -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" "$tdb"
  apply_sql "$tdb" "${MIGRATIONS[@]}" "${TESTS[0]}"
  echo "  -- assertions --"
  # Run ONCE and reuse the output. Running it twice to count separately makes
  # the second pass error on state the first pass already mutated, which
  # reported failures on a suite where everything actually passed.
  local out fails
  out=$(psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$tdb" -q -f "$ROOT/db/${TESTS[1]}.sql" 2>&1)
  printf '%s\n' "$out" | grep -E "^ *(pass|FAIL)" | sed 's/^ */  /'
  fails=$(printf '%s\n' "$out" | grep -cE "^ *FAIL|^psql.*ERROR" || true)
  dropdb -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" "$tdb" 2>/dev/null || true
  [ "$fails" -eq 0 ] && echo "  all assertions passed" || { echo "  $fails FAILURES"; exit 1; }
}

cmd_demo() {
  running || cmd_up
  python3 -c "import psycopg2" 2>/dev/null || pip install -q psycopg2-binary
  cd "$ROOT"
  python3 -m replay.harness load --sports NFL,NBA --weeks 18 --teams 32 | sed 's/^/  /'
  python3 -m replay.harness run --step-hours 24                        | sed 's/^/  /'
  python3 -m engine.scoring   run                                      | sed 's/^/  /'
  python3 -m engine.resolution seed                                    | sed 's/^/  /'
  python3 -m engine.resolution arm                                     | sed 's/^/  /'
  python3 -m engine.resolution resolve                                 | sed 's/^/  /'
}

# The demo build: replay + score, then a FIXED card pool. Deliberately skips
# engine.resolution's seed, whose random collections would pollute the fixed
# set (and whose settled challenge pins cards that then cannot be pruned).
cmd_prototype() {
  running || cmd_up
  dropdb --force -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" "$PGDB" 2>/dev/null || true
  cmd_load
  python3 -c "import psycopg2" 2>/dev/null || pip install -q psycopg2-binary
  cd "$ROOT"
  python3 -m replay.harness load --sports NFL,NBA --weeks 18 --teams 32 | sed 's/^/  /'
  python3 -m replay.harness run --step-hours 24                        | sed 's/^/  /'
  python3 -m engine.scoring   run                                      | sed 's/^/  /'
  python3 -m demo.setup                                                | sed 's/^/  /'
}

cmd_reset() {
  running || cmd_up
  dropdb --force -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" "$PGDB" 2>/dev/null || true
  cmd_load; cmd_demo
}

# Two independent builds, compared hash by hash. This existed as a claim in
# the README long before it was true: the harness read due games back with a
# non-unique ORDER BY and the source drew every stat line from one shared
# generator, so the order games happened to come back in decided the season.
# Two consecutive builds shared 5 of 48 pool cards. Nothing downstream can be
# compared across runs unless this passes, so it is a command, not a comment.
cmd_reproducible() {
  running || cmd_up
  cd "$ROOT"
  # The third build is stopped half way and finished by a SECOND process --
  # what a crash and restart actually looks like. The harness promises a tick
  # can be replayed and insert nothing new; before the source drew from a
  # per-fixture generator, resuming silently produced a different season from
  # there on, and nothing checked.
  local out=() db
  for db in ccg_rep_a ccg_rep_b ccg_rep_c; do
    dropdb --force -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" "$db" 2>/dev/null || true
    createdb -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" "$db"
    apply_sql "$db" "${MIGRATIONS[@]}" >/dev/null
    CCG_DSN="host=$PGHOST port=$PGPORT user=$PGUSER dbname=$db" \
      python3 -m replay.harness load --sports NFL,NBA --weeks 18 --teams 32 >/dev/null
    if [ "$db" = ccg_rep_c ]; then
      CCG_DSN="host=$PGHOST port=$PGPORT user=$PGUSER dbname=$db" \
        python3 -m replay.harness run --step-hours 24 --max-ticks 60 >/dev/null
    fi
    CCG_DSN="host=$PGHOST port=$PGPORT user=$PGUSER dbname=$db" \
      python3 -m replay.harness run --step-hours 24 >/dev/null
    CCG_DSN="host=$PGHOST port=$PGPORT user=$PGUSER dbname=$db" \
      python3 -m engine.scoring run >/dev/null
    out+=("$(psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$db" -tAc "
      select 'stats  '||md5(string_agg(game_id||'|'||player_id||'|'||fantasy_points::text,
                                       ',' order by game_id, player_id)) from player_game_stat;
      select 'scores '||md5(string_agg(game_id||'|'||player_id||'|'||game_score::text,
                                       ',' order by game_id, player_id)) from game_score;
      select 'forms  '||md5(string_agg(player_id||'|'||as_of::text||'|'||form::text,
                                       ',' order by player_id, as_of)) from player_form;")")
    dropdb -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" "$db" 2>/dev/null || true
  done
  printf '%s\n' "${out[0]}" | sed 's/^/  /'
  local bad=0
  if [ "${out[0]}" = "${out[1]}" ]; then
    echo "  pass  two independent builds are bit-identical"
  else
    echo "  FAIL  a rebuild does not reproduce the season:"
    printf '%s\n' "${out[1]}" | sed 's/^/    /'; bad=1
  fi
  if [ "${out[0]}" = "${out[2]}" ]; then
    echo "  pass  a replay resumed in a second process matches one that ran straight through"
  else
    echo "  FAIL  resuming a replay produces a different season:"
    printf '%s\n' "${out[2]}" | sed 's/^/    /'; bad=1
  fi
  [ "$bad" -eq 0 ] || exit 1
}

cmd_verify() {
  running || cmd_up
  cd "$ROOT"
  python3 -m engine.scoring verify
}

cmd_status() {
  if ! running; then echo "server DOWN - run: ./db/dev.sh up"; return; fi
  psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDB" -tAc "
    select '  tables            '||count(*) from information_schema.tables where table_schema='public';
    select '  games             '||count(*) from game;
    select '  stat lines        '||count(*) from player_game_stat;
    select '  game scores       '||count(*) from game_score;
    select '  peer snapshots    '||count(*) from peer_group_snapshot;
    select '  player form rows  '||count(*) from player_form;
    select '  tactic cards      '||count(*) from tactic_card;
    select '  settled results   '||count(*) from challenge_result;" 2>/dev/null \
  || echo "  database '$PGDB' not loaded - run: ./db/dev.sh load"
}

# The API runs detached, tracked by a pidfile. Matching it with pgrep -f is a
# trap: any command that also mentions the uvicorn invocation matches its own
# shell and kills it.
APIPID=/tmp/ccg_api.pid
APIPORT=${APIPORT:-8000}

cmd_api() {
  running || cmd_up
  cmd_api_stop
  cd "$ROOT"
  python3 -c "import fastapi, uvicorn" 2>/dev/null || pip install -q fastapi "uvicorn[standard]"
  nohup python3 -m uvicorn api.main:app --port "$APIPORT" --log-level warning \
        > /tmp/ccg_api.log 2>&1 &
  echo $! > "$APIPID"
  for _ in $(seq 1 30); do
    curl -sf "http://localhost:$APIPORT/health" >/dev/null 2>&1 && break; sleep 0.5
  done
  if curl -sf "http://localhost:$APIPORT/health" >/dev/null 2>&1; then
    echo "api up on http://localhost:$APIPORT  (docs at /docs)"
  else
    echo "api FAILED to start:"; tail -15 /tmp/ccg_api.log; exit 1
  fi
}

cmd_api_stop() {
  [ -f "$APIPID" ] && kill "$(cat "$APIPID")" 2>/dev/null && sleep 1
  rm -f "$APIPID"
  return 0
}

cmd_psql() { exec psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDB"; }

case "${1:-status}" in
  up) cmd_up ;; load) cmd_load ;; demo) cmd_demo ;; reset) cmd_reset ;;
  prototype) cmd_prototype ;;
  test) cmd_test ;; verify) cmd_verify ;; status) cmd_status ;; psql) cmd_psql ;;
  reproducible) cmd_reproducible ;;
  api) cmd_api ;; api-stop) cmd_api_stop ;;
  *) sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
