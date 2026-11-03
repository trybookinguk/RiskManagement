# RiskManagement on the Synology NAS

RiskManagement runs on its own on the NAS. It doesn't need s3reporting or the
Pi. It has one Docker image and two containers:

| Container | Does | Starts |
| --- | --- | --- |
| `riskmanagement` | Worker. The nightly import jobs run inside it with `docker exec`. | always |
| `riskmanagement-web` | Dashboard + JSON API | from build step 5 (`--profile web`) |

| On the NAS | In the containers |
| --- | --- |
| `/volume1/docker/RiskManagement` (this repo) | `/app` (read-only) |
| `/volume1/docker/RiskManagement/.env` (secrets) | environment |
| `/volume1/docker/riskmanagement-data` | `/data`: `risk.db`, logs |

It needs an **x86-64** Synology model (Container Manager). Plan on about 2 GB
of RAM free for the one-off BookingDataAll load.

## One-time setup

Run these over SSH on the NAS, as root (`sudo -i`).

**1. Data folder.** Create it in DSM as an **encrypted shared folder**
(Control Panel → Shared Folder → Create → *Encrypt this shared folder*), so
`risk.db` is encrypted at rest. Name it `riskmanagement-data` and place it under
`docker`, or create it from the shell:
```bash
mkdir -p /volume1/docker/riskmanagement-data
```

**2. Clone the repo** with the read-only deploy key. See *Private repo access*
below, then:
```bash
git clone github-riskmanagement:trybookinguk/RiskManagement.git /volume1/docker/RiskManagement
```

**3. Create `.env`** from the template and fill it in:
```bash
cp /volume1/docker/RiskManagement/.env.example /volume1/docker/RiskManagement/.env
chmod 600 /volume1/docker/RiskManagement/.env
vi /volume1/docker/RiskManagement/.env
```
Choose `RISK_ENCRYPTION_PASSPHRASE` **once**, and store it in the password
manager **before** the next step. It turns every email, phone, name and IP
into a hash. If it ever changes, new hashes stop matching old ones, and the
warehouse refuses to run rather than mix them.

**4. Build and start the worker:**
```bash
cd /volume1/docker/RiskManagement/deploy/synology
docker compose up -d --build
```

**5. Create the warehouse and check it:**
```bash
docker exec riskmanagement python3 bin/rm_admin.py init-db
docker exec riskmanagement python3 bin/rm_admin.py status
docker exec riskmanagement python3 -m pytest -q -p no:cacheprovider     # all tests should pass
```

**6. Allow yourself on the dashboard** (used from step 5):
```bash
docker exec riskmanagement python3 bin/rm_admin.py add-user henry@trybooking.co.uk --role admin
```

## Admin commands

```bash
docker exec riskmanagement python3 bin/rm_admin.py status          # schema, size, rows, last imports
docker exec riskmanagement python3 bin/rm_admin.py list-users
docker exec riskmanagement python3 bin/rm_admin.py add-user someone@trybooking.co.uk
docker exec riskmanagement python3 bin/rm_admin.py remove-user someone@trybooking.co.uk
docker exec riskmanagement python3 bin/rm_admin.py create-token power-bi   # API token, shown once
docker exec riskmanagement python3 bin/rm_admin.py revoke-token tok_…
```

## Scheduling (DSM Task Scheduler)

Go to Control Panel → Task Scheduler → Create → Scheduled Task →
User-defined script. Run each task as `root`, daily, and tick *Send run
details by email* for alerts.

The import jobs arrive in build steps 2–4. Once they're in:

| Time | Script |
| --- | --- |
| 04:10 | `docker exec riskmanagement python3 bin/ingest_s3.py` |
| 04:20 | `docker exec riskmanagement python3 bin/ingest_sharepoint.py` |
| 04:30 | `docker exec riskmanagement python3 bin/ingest_stripe.py` |
| 04:50 | `docker exec riskmanagement python3 bin/backup.py` |

## Updating

```bash
git -C /volume1/docker/RiskManagement pull origin main
docker exec riskmanagement python3 bin/rm_admin.py init-db     # applies any new schema migrations
```

Code changes are picked up straight away. Rebuild with
`docker compose up -d --build` only when `requirements.txt` or the
`Dockerfile` changes; the PR description will say when.

## Legacy report

`legacy/fraud_risk_report.py` is the current report. It still needs
s3reporting and stays on the Pi until phase 2 replaces it. Nothing in this
setup runs it.

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
