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
#   ingest-sharepoint [options]  import Booking Transactions CSVs from SharePoint (e.g. --dry-run)
#   report [--no-email]   nightly risk report: changes, top 10 accounts/events, import health (emailed)
#   check-transactions    Booking Transactions import check (values, matching, declines)
#   web-start             start (or restart) the dashboard on port 8085 (WEB_PORT)
#   web-stop              stop the dashboard
#   web-status            is the dashboard running, and does /api/health answer
#   tunnel-start          TEMPORARY: publish the dashboard through ngrok (NGROK_* in .env)
#   tunnel-stop           stop the ngrok tunnel
#   tunnel-status         is the tunnel up, and its address
#   restart               restart the containers
#   env-export            copy .env to riskmanagement-data/setup/env-edit.txt so you
#                         can change a setting with Text Editor (.env itself is root-only)
#   env-apply             move setup/env-edit.txt back into .env, delete it, restart
#   run SCRIPT [args]     any bin/ script, e.g.  run rm_admin.py list-tokens
#
# Only one rmctl command runs at a time; a second one waits up to 2 hours.

set -u

REPO_DIR="${REPO_DIR:-/volume1/docker/RiskManagement}"
DATA_DIR="${DATA_DIR:-/volume1/docker/riskmanagement-data}"
COMPOSE_DIR="$REPO_DIR/deploy/synology"
CONTAINER="riskmanagement"
WEB="riskmanagement-web"
LOG_DIR="$DATA_DIR/logs"
KEEP_LOGS=60
export PATH="$PATH:/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin"
export HOME="${HOME:-/root}"   # Task Scheduler leaves HOME unset; docker warns about .dockercfg without it

CMD="${1:-help}"
[ $# -gt 0 ] && shift

usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; }

if [ "$CMD" = "help" ] || [ "$CMD" = "-h" ] || [ "$CMD" = "--help" ]; then
  usage
  exit 0
fi

case "$CMD" in
  setup|update|status|test|ingest-s3|ingest-sharepoint|report|check-transactions|full-refresh|restart|run|env-export|env-apply|web-start|web-stop|web-status|tunnel-start|tunnel-stop|tunnel-status) ;;
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

TUNNEL="riskmanagement-tunnel"

# A value from .env without printing it (quotes and Windows line endings removed).
env_value() {
  grep -E "^$1=" "$REPO_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2- | sed -e 's/\r$//' -e 's/^["'\'']//' -e 's/["'\'']$//'
}

web_running() { [ "$(docker inspect -f '{{.State.Running}}' "$WEB" 2>/dev/null)" = "true" ]; }

# The dashboard reads the code at start-up: restart it after an update so it runs the new code.
web_refresh() {
  if web_running; then
    step "Restarting the dashboard with the new code"
    compose --profile web up -d --force-recreate "$WEB" || say "WARNING: could not restart the dashboard"
  fi
}

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
    web_refresh
    ;;
  web-start)
    check_env
    grep -Eq '^WEB_ENTRA_CLIENT_ID=.+' "$REPO_DIR/.env" \
      || say "WARNING: WEB_ENTRA_* not set - nobody can sign in until Microsoft SSO is configured (docs/entra_sso_setup.md)"
    ensure_up
    step "Starting the dashboard"
    compose --profile web up -d --force-recreate "$WEB" || die "could not start the dashboard"
    for i in 1 2 3 4 5 6 7 8 9 10; do
      sleep 2
      if docker exec "$WEB" python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8085/api/health', timeout=3).status == 200 else 1)" 2>/dev/null; then
        say "Dashboard is up. Browse to WEB_BASE_URL (https://trybooking.synology.me:8443) and sign in with Microsoft."
        break
      fi
      [ $i -eq 10 ] && { docker logs --tail 40 "$WEB"; die "dashboard did not answer /api/health"; }
    done
    rc=0 ;;
  tunnel-start)
    check_env
    NGROK_AUTHTOKEN=$(env_value NGROK_AUTHTOKEN)
    NGROK_DOMAIN=$(env_value NGROK_DOMAIN)
    NGROK_DOMAIN=${NGROK_DOMAIN#https://}; NGROK_DOMAIN=${NGROK_DOMAIN%/}
    [ -n "$NGROK_AUTHTOKEN" ] || die "NGROK_AUTHTOKEN is empty in .env (docs/ngrok_temporary.md step 1)"
    [ -n "$NGROK_DOMAIN" ] || die "NGROK_DOMAIN is empty in .env, e.g. something.ngrok-free.app (docs/ngrok_temporary.md step 1)"
    if [ "$(env_value WEB_BASE_URL)" != "https://$NGROK_DOMAIN" ]; then
      say "WARNING: WEB_BASE_URL is '$(env_value WEB_BASE_URL)'. For sign-in through the tunnel it must be https://$NGROK_DOMAIN"
      say "         (env-export, change it, env-apply), and that address + /auth/callback must be a redirect URI in Entra."
    fi
    web_running || die "the dashboard isn't running - run 'rmctl.sh web-start' first"
    step "Starting the ngrok tunnel to the dashboard"
    export NGROK_AUTHTOKEN NGROK_DOMAIN
    compose --profile tunnel up -d --force-recreate "$TUNNEL" || die "could not start the tunnel"
    sleep 8
    if docker logs --tail 50 "$TUNNEL" 2>&1 | grep -q "started tunnel"; then
      say "Tunnel is up: https://$NGROK_DOMAIN  (temporary - stop it with 'rmctl.sh tunnel-stop')"
      rc=0
    else
      docker logs --tail 30 "$TUNNEL" 2>&1 | grep -v -i "authtoken" | tail -20
      die "the tunnel didn't start - see the lines above (wrong token or domain?)"
    fi ;;
  tunnel-stop)
    compose --profile tunnel rm -s -f "$TUNNEL"; rc=$?
    [ $rc -eq 0 ] && say "Tunnel stopped: the dashboard is no longer reachable through ngrok" ;;
  tunnel-status)
    if [ "$(docker inspect -f '{{.State.Running}}' "$TUNNEL" 2>/dev/null)" = "true" ]; then
      say "Tunnel is running: https://$(env_value NGROK_DOMAIN | sed -e 's#^https://##' -e 's#/$##')"
      docker logs --tail 10 "$TUNNEL" 2>&1 | grep -v -i "authtoken"
      rc=0
    else
      say "Tunnel is not running"; rc=1
    fi ;;
  web-stop)
    compose --profile web stop "$WEB"; rc=$? ;;
  web-status)
    if web_running; then
      say "Dashboard container is running"
      docker exec "$WEB" python3 -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8085/api/health', timeout=5).read().decode())"
      rc=$?
      docker logs --tail 15 "$WEB"
    else
      say "Dashboard is not running (start it with: rmctl.sh web-start)"; rc=1
    fi ;;
  status)      py bin/rm_admin.py status; rc=$? ;;
  test)        ensure_up; in_container python3 -m pytest -q -p no:cacheprovider; rc=$? ;;
  ingest-s3)   py bin/ingest_s3.py "$@"; rc=$? ;;
  ingest-sharepoint) py bin/ingest_sharepoint.py "$@"; rc=$? ;;
  report)      py bin/risk_report.py "$@"; rc=$? ;;
  check-transactions) py bin/risk_report.py --check-transactions; rc=$? ;;
  full-refresh)
    say "Lifetime load from BookingDataAll: expect 25-35 minutes and 2.5-3.5 GB"
    py bin/ingest_s3.py --full-refresh "$@"; rc=$? ;;
  restart)     compose restart; rc=$? ;;
  env-export)
    check_env
    EDIT="$DATA_DIR/setup/env-edit.txt"
    mkdir -p "$DATA_DIR/setup"
    cp "$REPO_DIR/.env" "$EDIT" || die "could not copy .env"
    if getent group administrators >/dev/null 2>&1; then
      chown root:administrators "$EDIT" && chmod 660 "$EDIT"
    else
      chmod 666 "$EDIT"
    fi
    say "Copied to File Station: docker > riskmanagement-data > setup > env-edit.txt"
    say "Edit it with Text Editor, save, then run 'rmctl.sh env-apply' (it holds secrets - don't leave it there)"
    rc=0 ;;
  env-apply)
    EDIT="$DATA_DIR/setup/env-edit.txt"
    [ -f "$EDIT" ] || die "no setup/env-edit.txt - run 'rmctl.sh env-export' first"
    sed -i 's/\r$//' "$EDIT"
    grep -Eq '^RISK_ENCRYPTION_PASSPHRASE=.+' "$EDIT" || die "RISK_ENCRYPTION_PASSPHRASE is empty in env-edit.txt - not applied"
    old=$(grep -E '^RISK_ENCRYPTION_PASSPHRASE=' "$REPO_DIR/.env" 2>/dev/null)
    new=$(grep -E '^RISK_ENCRYPTION_PASSPHRASE=' "$EDIT")
    if [ -n "$old" ] && [ "$old" != "RISK_ENCRYPTION_PASSPHRASE=" ] && [ "$old" != "$new" ] && [ -f "$DATA_DIR/risk.db" ]; then
      die "RISK_ENCRYPTION_PASSPHRASE differs from the one the warehouse uses - not applied (it must never change)"
    fi
    cat "$EDIT" > "$REPO_DIR/.env" && chmod 600 "$REPO_DIR/.env" || die "could not write .env"
    rm -f "$EDIT"
    say "Settings saved to .env (root-only); env-edit.txt deleted"
    step "Recreating the containers so they pick up the new settings"
    compose up -d --force-recreate "$CONTAINER"; rc=$?
    web_refresh ;;
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
