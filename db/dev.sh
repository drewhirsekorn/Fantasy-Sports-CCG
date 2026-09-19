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
#   ./db/dev.sh reset     drop, reload, and re-run the demo from scratch
#   ./db/dev.sh test      migrations + fixtures + assertions on a scratch db
#   ./db/dev.sh status    what is currently in there
#   ./db/dev.sh psql      open a shell on it
#
# A cold container needs:  ./db/dev.sh up && ./db/dev.sh load && ./db/dev.sh demo
set -euo pipefail

PGBIN=/usr/lib/postgresql/16/bin
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
            07_replay 08_scoring 09_resolution 10_api)
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

cmd_reset() {
  running || cmd_up
  dropdb --force -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" "$PGDB" 2>/dev/null || true
  cmd_load; cmd_demo
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

cmd_psql() { exec psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDB"; }

case "${1:-status}" in
  up) cmd_up ;; load) cmd_load ;; demo) cmd_demo ;; reset) cmd_reset ;;
  test) cmd_test ;; verify) cmd_verify ;; status) cmd_status ;; psql) cmd_psql ;;
  *) sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
