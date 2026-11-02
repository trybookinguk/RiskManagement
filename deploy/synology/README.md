# Running the fraud risk tool on the Synology NAS

The tool runs **inside the existing `s3reporting` container**, set up as in
s3reporting's `deploy/synology/README.md`. Inside that container it gets:

- the shared code at `/app`
- the warehouse and tier snapshot at `/data/.cache/prepared`
- the `.env` credentials

This repo is bind-mounted into the container at `/risk`.

```
/volume1/docker/s3reporting          -> /app    (s3reporting repo)
/volume1/docker/RiskManagement       -> /risk   (this repo)
/volume1/docker/s3reporting-data     -> /data   (warehouse, reports, dispute exports)
```

## One-time setup (SSH into the NAS)

```bash
sudo git clone https://github.com/trybookinguk/RiskManagement.git /volume1/docker/RiskManagement
sudo mkdir -p /volume1/docker/s3reporting-data/disputes
sudo git -C /volume1/docker/s3reporting pull origin main
cd /volume1/docker/s3reporting/deploy/synology && sudo ./deploy_synology.sh
```

`deploy_synology.sh` rebuilds the image, adding `openpyxl`, and recreates the
container with the `/risk` mount. Both come from s3reporting's `docker-compose.yml`
and `Dockerfile`.

RiskManagement is **private**, so the NAS needs a read-only **deploy key** to
clone and pull it. See "Private repo access" below.

## Running it

The container already has `.env` loaded, so there's no need to source it:

```bash
# Re-score with the chargebacks already loaded
sudo docker exec -w /risk s3reporting python3 fraud_risk_report.py

# Load new dispute exports. Copy them to /volume1/docker/s3reporting-data/disputes
# first, NOT into either repo, because they contain customer emails and names.
sudo docker exec -w /risk s3reporting python3 fraud_risk_report.py \
  --chargeback-file /data/disputes/AllDisputeCases_20231102000000_20261031235959_O_01.CSV \
  --chargeback-file /data/disputes/Disputes_Export_Query_2014-07-31_to_2026-11-01.csv

# Safe preview: [TEST] subject
sudo docker exec -e TEST_MODE=true -w /risk s3reporting python3 fraud_risk_report.py
```

The workbook is saved to `/data/reports`, which is
`/volume1/docker/s3reporting-data/reports` on the NAS. It is also uploaded to
SharePoint and emailed as before.

## Scheduling (DSM Task Scheduler)

Go to Control Panel → Task Scheduler → Create → Scheduled Task → User-defined
script. Run it as `root`, and schedule it after the nightly pipeline has
finished, for example 05:00:

```bash
/usr/local/bin/docker exec -w /risk s3reporting python3 fraud_risk_report.py
```

## Updating

```bash
sudo git -C /volume1/docker/RiskManagement pull origin main
```

Code changes are picked up straight away through the bind mount, with no rebuild.

## Where the warehouse lives

This assumes the NAS container runs the s3reporting pipeline (`prepare_data.py`),
so `/data/.cache/prepared/warehouse.db` is the live warehouse.

If the Pi still builds the warehouse, the NAS copy goes stale. Don't point
`WAREHOUSE_DB` at the Pi's file over a network share: the tool writes its
`chargebacks` and `risk_fraud` tables to it, and SQLite writes over SMB or NFS
can corrupt the file. Run the pipeline on one host only.

## Private repo access (deploy key)

A deploy key is an SSH key that can read **only this repo** and nothing else
on GitHub. It belongs to the repo, not to a person, so it doesn't break when
someone leaves or a token expires. Run these steps once, over SSH on the NAS,
as root:

```bash
sudo -i
```

**1. Create the key** (no passphrase, so scheduled tasks can use it):
```bash
mkdir -p /root/.ssh && chmod 700 /root/.ssh
ssh-keygen -t ed25519 -N "" -C "nas-riskmanagement" -f /root/.ssh/riskmanagement_deploy
cat /root/.ssh/riskmanagement_deploy.pub
```

**2. Add it on GitHub.** Go to RiskManagement → Settings → Deploy keys → Add
deploy key. Paste the `.pub` line, give it the title `Synology NAS`, and leave
**Allow write access unticked**.

**3. Point git at the key.** If outbound port 22 is blocked, use
`HostName ssh.github.com` and `Port 443` instead.
```bash
cat >> /root/.ssh/config <<'CFG'
Host github-riskmanagement
    HostName github.com
    User git
    IdentityFile /root/.ssh/riskmanagement_deploy
    IdentitiesOnly yes
CFG
chmod 600 /root/.ssh/config
ssh -T github-riskmanagement     # expect: "Hi trybookinguk/RiskManagement! You've successfully authenticated…"
```

**4. Clone with the alias**, or repoint an existing clone:
```bash
git clone github-riskmanagement:trybookinguk/RiskManagement.git /volume1/docker/RiskManagement
# existing clone:
git -C /volume1/docker/RiskManagement remote set-url origin github-riskmanagement:trybookinguk/RiskManagement.git
git -C /volume1/docker/RiskManagement pull origin main
```

**Notes**
- No `git` on the NAS? Install the **Git Server** package from Package Center.
  It provides the `git` command.
- DSM Task Scheduler tasks that run as `root` use `/root/.ssh` too, so a
  scheduled `git pull` works.
- If s3reporting is also private, give it its **own** deploy key (GitHub
  doesn't allow one key on two repos). Add a second `Host github-s3reporting`
  block pointing at that key. The Pi needs the same setup for s3reporting.
- To revoke the NAS's access, delete the key under Deploy keys.
