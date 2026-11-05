#!/bin/bash
# bootstrap.sh - first-time RiskManagement setup on the Synology NAS, no SSH.
#
# Don't paste this file into Task Scheduler: DSM refuses to save long scripts
# containing < or > ("Connection failed. Please check your network settings").
# Instead:
#   1. Download it from GitHub (deploy/synology/bootstrap.sh > Download raw file)
#      and upload it with File Station to the "docker" shared folder.
#   2. Control Panel > Task Scheduler > Create > Scheduled Task > User-defined script
#      General: name "RiskManagement bootstrap", user "root", untick Enabled
#      Task Settings: tick "Send run details by email"; Run command:
#          sed -i 's/\r$//' /volume1/docker/bootstrap.sh; bash /volume1/docker/bootstrap.sh
#      (the sed fixes Windows line endings if the file went through a PC)
#   3. Select the task and click Run. Run it again after each step it asks for;
#      it carries on from where it stopped.
# It needs Container Manager and Git Server (both from Package Center).
#
# Each run:
#   1. creates the folders                 docker/RiskManagement, docker/riskmanagement-data
#   2. creates a read-only deploy key      -> add it to GitHub (it tells you how), then Run again
#   3. clones the private repo
#   4. creates .env from .env.example      -> fill it in with Text Editor, then Run again
#   5. builds and starts the containers, creates the warehouse, runs the tests (rmctl.sh setup)
#
# Output: the task's run details (email) and
#   File Station > docker > riskmanagement-data > logs > bootstrap-<date-time>.log
# Messages for you are also written to
#   File Station > docker > riskmanagement-data > setup > NEXT_STEP.txt

set -u
export PATH="$PATH:/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin"

DOCKER_SHARE="${DOCKER_SHARE:-/volume1/docker}"
REPO_DIR="$DOCKER_SHARE/RiskManagement"
DATA_DIR="$DOCKER_SHARE/riskmanagement-data"
SETUP_DIR="$DATA_DIR/setup"
LOG_DIR="$DATA_DIR/logs"
REPO="trybookinguk/RiskManagement"
SSH_DIR="${SSH_DIR:-/root/.ssh}"
KEY="$SSH_DIR/riskmanagement_deploy"
HOST_ALIAS="github-riskmanagement"

say()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
next_step() {      # message for the person, then stop (exit 0: waiting is not a failure)
  { echo "RiskManagement setup - $(date '+%Y-%m-%d %H:%M')"; echo; printf '%s\n' "$@"; } > "$SETUP_DIR/NEXT_STEP.txt"
  echo; printf '%s\n' "$@"; echo
  say "Waiting for you. This message is also in File Station: docker > riskmanagement-data > setup > NEXT_STEP.txt"
  exit 0
}
die() { say "ERROR: $*"; exit 1; }

# 1. Folders ----------------------------------------------------------------
[ "$(id -u)" = "0" ] || die "run the task as user root (task > Edit > General > User)"
[ -d "$DOCKER_SHARE" ] || die "$DOCKER_SHARE not found - install Container Manager (it creates the 'docker' shared folder)"
mkdir -p "$DATA_DIR" "$SETUP_DIR" "$LOG_DIR" || die "cannot create $DATA_DIR"
exec > >(tee -a "$LOG_DIR/bootstrap-$(date +%Y%m%d-%H%M%S).log") 2>&1
say "RiskManagement bootstrap"
command -v docker >/dev/null 2>&1 || die "docker not found - install Container Manager from Package Center"
command -v git >/dev/null 2>&1 || die "git not found - install Git Server from Package Center, then Run again"

# 2. Deploy key ---------------------------------------------------------------
mkdir -p "$SSH_DIR" && chmod 700 "$SSH_DIR"
if [ ! -f "$KEY" ]; then
  say "Creating a read-only deploy key"
  ssh-keygen -q -t ed25519 -N "" -C "synology-riskmanagement" -f "$KEY" || die "ssh-keygen failed"
fi
cp "$KEY.pub" "$SETUP_DIR/deploy_key.pub"
if ! grep -q "Host $HOST_ALIAS" "$SSH_DIR/config" 2>/dev/null; then
  # Port 443 works even where outbound port 22 is blocked.
  cat >> "$SSH_DIR/config" <<CFG

Host $HOST_ALIAS
    HostName ssh.github.com
    Port 443
    User git
    IdentityFile $KEY
    IdentitiesOnly yes
    StrictHostKeyChecking accept-new
CFG
  chmod 600 "$SSH_DIR/config"
fi
auth=$(ssh -T -o BatchMode=yes -o ConnectTimeout=20 "$HOST_ALIAS" 2>&1)
if ! echo "$auth" | grep -q "successfully authenticated"; then
  next_step \
    "STEP: add the NAS's deploy key to GitHub (one time)." \
    "" \
    "1. In File Station open docker > riskmanagement-data > setup > deploy_key.pub" \
    "   (right-click > Open with Text Editor) and copy the single line. It is:" \
    "" \
    "   $(cat "$KEY.pub")" \
    "" \
    "2. On GitHub: $REPO > Settings > Deploy keys > Add deploy key." \
    "   Title: Synology NAS. Paste the line. Leave 'Allow write access' UNTICKED. Add key." \
    "" \
    "3. Run this task again." \
    "" \
    "(GitHub said: $(echo "$auth" | tail -1))"
fi
say "GitHub deploy key: OK"

# 3. Clone or update --------------------------------------------------------
if [ -d "$REPO_DIR/.git" ]; then
  say "Repo already cloned - pulling"
  git -C "$REPO_DIR" pull --ff-only origin main || die "git pull failed"
else
  [ -e "$REPO_DIR" ] && [ -n "$(ls -A "$REPO_DIR" 2>/dev/null)" ] \
    && die "$REPO_DIR exists and is not empty - move it aside, then Run again"
  say "Cloning $REPO"
  git clone "$HOST_ALIAS:$REPO.git" "$REPO_DIR" || die "git clone failed"
fi

# 4. .env ---------------------------------------------------------------------
ENV="$REPO_DIR/.env"
if [ ! -f "$ENV" ]; then
  cp "$REPO_DIR/.env.example" "$ENV" && chmod 600 "$ENV"
fi
chmod 600 "$ENV"
if ! grep -Eq '^RISK_ENCRYPTION_PASSPHRASE=.+' "$ENV" || ! grep -Eq '^AWS_ACCESS_KEY_ID=.+' "$ENV"; then
  next_step \
    "STEP: fill in the settings file (one time)." \
    "" \
    "1. In File Station open docker > RiskManagement > .env" \
    "   (right-click > Open with Text Editor; install Text Editor from Package Center if needed)." \
    "   If you can't see it: File Station > Settings > tick 'Show hidden files'." \
    "2. Fill in each line. RISK_ENCRYPTION_PASSPHRASE is the 'encrypt word':" \
    "   choose it once and save it in the password manager FIRST - it must never change." \
    "3. Save, then Run this task again."
fi
say ".env: OK"

# 5. Build, start, create the warehouse, test --------------------------------
rm -f "$SETUP_DIR/NEXT_STEP.txt"
bash "$REPO_DIR/deploy/synology/rmctl.sh" setup || die "setup failed - see the log above"

{ echo "RiskManagement setup - $(date '+%Y-%m-%d %H:%M')"; echo
  echo "DONE. The worker container is running and the warehouse is ready."
  echo "Next: create the Task Scheduler tasks listed in deploy/synology/README.md"
  echo "('No-SSH operation'), starting with the one-off 'RiskManagement full refresh'."
} > "$SETUP_DIR/NEXT_STEP.txt"
say "Bootstrap complete. Next steps: docker > riskmanagement-data > setup > NEXT_STEP.txt"
