#!/bin/bash
# rmctl.sh - run RiskManagement tasks on the Synology NAS without SSH.
#
# Made for DSM Task Scheduler: Control Panel > Task Scheduler > Create >
# Scheduled Task > User-defined script, user "root", with a line such as
#
#     bash /volume1/docker/RiskManagement/deploy/synology/rmctl.sh status
#
# Every run writes a log you can open in File Station:
#     docker > riskmanagement-data > logs > <command>-<date-time>.log
# and the same text goes to the task's output (tick "Send run details by
# email" to get it by email). The exit code is non-zero on failure.
#
# Commands
#   setup                 build the image, start the worker, create/upgrade the warehouse, run the tests
#   update                git pull, rebuild only if the image changed, upgrade the warehouse
#   status                warehouse size, row counts, last import per source
#   test                  run the test suite inside the container
#   ingest-s3 [options]   nightly S3 load (options passed through, e.g. --dry-run)
#   full-refresh          one-off lifetime load from BookingDataAll (25-35 min)
#   add-user EMAIL [viewer|admin]
#   remove-user EMAIL
#   list-users
#   restart               restart the containers
#   run SCRIPT [args]     any bin/ script, e.g.  run rm_admin.py list-tokens
#
# Only one rmctl command runs at a time; a second one waits up to 2 hours.

set -u

REPO_DIR="${REPO_DIR:-/volume1/docker/RiskManagement}"
DATA_DIR="${DATA_DIR:-/volume1/docker/riskmanagement-data}"
COMPOSE_DIR="$REPO_DIR/deploy/synology"
CONTAINER="riskmanagement"
LOG_DIR="$DATA_DIR/logs"
KEEP_LOGS=60
export PATH="$PATH:/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin"

CMD="${1:-help}"
[ $# -gt 0 ] && shift

usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; }

if [ "$CMD" = "help" ] || [ "$CMD" = "-h" ] || [ "$CMD" = "--help" ]; then
  usage
  exit 0
fi

case "$CMD" in
  setup|update|status|test|ingest-s3|full-refresh|add-user|remove-user|list-users|restart|run) ;;
  *) usage; echo; echo "ERROR: unknown command: $CMD"; exit 1 ;;
esac

# ---------------------------------------------------------------- logging --
mkdir -p "$LOG_DIR" 2>/dev/null
LOG="$LOG_DIR/${CMD}-$(date +%Y%m%d-%H%M%S)-$$.log"
if [ -w "$LOG_DIR" ]; then
  exec > >(tee -a "$LOG") 2>&1
  ls -1t "$LOG_DIR/${CMD}-"*.log 2>/dev/null | tail -n +$((KEEP_LOGS + 1)) | xargs -r rm -f
else
  LOG="(not written: $LOG_DIR missing)"
fi

say()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
die()  { say "ERROR: $*"; say "Log: $LOG"; exit 1; }
step() { echo; say "== $*"; }

say "rmctl $CMD $*"

# ------------------------------------------------------------------ checks --
[ "$(id -u)" = "0" ] || die "run this task as root (Task Scheduler > task > General > User: root)"
command -v docker >/dev/null 2>&1 || die "docker not found - install Container Manager from Package Center"
[ -d "$REPO_DIR/.git" ] || die "$REPO_DIR is not a git clone - run bootstrap.sh first (see deploy/synology/README.md)"

if docker compose version >/dev/null 2>&1; then
  COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE="docker-compose"
else
  die "neither 'docker compose' nor 'docker-compose' is available"
fi

# One command at a time (a full refresh and the nightly job must not overlap).
LOCK="$DATA_DIR/.rmctl.lock"
if command -v flock >/dev/null 2>&1; then
  exec 9>"$LOCK"
  if ! flock -w 7200 9; then die "another rmctl command is still running (lock: $LOCK)"; fi
else
  waited=0
  until mkdir "$LOCK.d" 2>/dev/null; do
    [ $waited -ge 7200 ] && die "another rmctl command is still running (lock: $LOCK.d)"
    sleep 10; waited=$((waited + 10))
  done
  trap 'rmdir "$LOCK.d" 2>/dev/null' EXIT
fi

# ----------------------------------------------------------------- helpers --
check_env() {
  local env="$REPO_DIR/.env"
  [ -f "$env" ] || die ".env missing: copy .env.example to .env in File Station and fill it in (see README)"
  chmod 600 "$env"
  grep -Eq '^RISK_ENCRYPTION_PASSPHRASE=.+' "$env" \
    || die "RISK_ENCRYPTION_PASSPHRASE is empty in $env - set it (and save it in the password manager)"
}

compose() { (cd "$COMPOSE_DIR" && $COMPOSE "$@"); }

running() { [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" = "true" ]; }

ensure_up() {
  running && return 0
  step "Starting the worker container"
  compose up -d "$CONTAINER" || die "could not start $CONTAINER"
}

in_container() { docker exec "$CONTAINER" "$@"; }

py() { ensure_up; in_container python3 "$@"; }

image_inputs_hash() {
  cat "$REPO_DIR/requirements.txt" "$COMPOSE_DIR/Dockerfile" 2>/dev/null | sha256sum | cut -c1-16
}

build() {
  step "Building the image (a few minutes the first time)"
  compose build || die "image build failed"
  image_inputs_hash > "$DATA_DIR/.image_inputs"
  step "Starting containers"
  compose up -d --force-recreate "$CONTAINER" || die "could not start $CONTAINER"
}

# ---------------------------------------------------------------- commands --
rc=0
case "$CMD" in
  setup)
    check_env
    mkdir -p "$DATA_DIR"
    build
    step "Creating / upgrading the warehouse"
    py bin/rm_admin.py init-db || die "init-db failed"
    step "Running the tests"
    in_container python3 -m pytest -q -p no:cacheprovider || die "tests failed"
    step "Status"
    py bin/rm_admin.py status
    rc=$?
    ;;
  update)
    check_env
    step "Pulling the latest code"
    before=$(git -C "$REPO_DIR" rev-parse HEAD)
    git -C "$REPO_DIR" pull --ff-only origin main || die "git pull failed (check the deploy key; see README)"
    after=$(git -C "$REPO_DIR" rev-parse HEAD)
    if [ "$before" = "$after" ]; then say "Already up to date ($after)"; else
      git -C "$REPO_DIR" log --oneline "$before..$after"; fi
    if [ "$(cat "$DATA_DIR/.image_inputs" 2>/dev/null)" != "$(image_inputs_hash)" ]; then
      build
    else
      say "Image unchanged - no rebuild needed"
      ensure_up
    fi
    step "Upgrading the warehouse"
    py bin/rm_admin.py init-db
    rc=$?
    ;;
  status)      py bin/rm_admin.py status; rc=$? ;;
  test)        ensure_up; in_container python3 -m pytest -q -p no:cacheprovider; rc=$? ;;
  ingest-s3)   py bin/ingest_s3.py "$@"; rc=$? ;;
  full-refresh)
    say "Lifetime load from BookingDataAll: expect 25-35 minutes and 2.5-3.5 GB"
    py bin/ingest_s3.py --full-refresh "$@"; rc=$? ;;
  add-user)
    [ $# -ge 1 ] || die "usage: rmctl.sh add-user EMAIL [viewer|admin]"
    py bin/rm_admin.py add-user "$1" --role "${2:-viewer}"; rc=$? ;;
  remove-user)
    [ $# -ge 1 ] || die "usage: rmctl.sh remove-user EMAIL"
    py bin/rm_admin.py remove-user "$1"; rc=$? ;;
  list-users)  py bin/rm_admin.py list-users; rc=$? ;;
  restart)     compose restart; rc=$? ;;
  run)
    [ $# -ge 1 ] || die "usage: rmctl.sh run SCRIPT [args]   e.g. run rm_admin.py list-tokens"
    script="$1"; shift
    case "$script" in */*|*..*) die "give a script name from bin/, e.g. rm_admin.py" ;; esac
    [ -f "$REPO_DIR/bin/$script" ] || die "no such script: bin/$script"
    py "bin/$script" "$@"; rc=$? ;;
  *)
    usage
    die "unknown command: $CMD"
    ;;
esac

echo
if [ $rc -eq 0 ]; then say "OK"; else say "FAILED (exit $rc)"; fi
say "Log: $LOG"
exit $rc
