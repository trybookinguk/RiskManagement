# RiskManagement on the Synology NAS

RiskManagement runs on its own on the NAS. It doesn't need s3reporting or the
Pi. It has one Docker image and two containers:

| Container | Does | Starts |
| --- | --- | --- |
| `riskmanagement` | Worker. The nightly import jobs run inside it with `docker exec`. | always |
| `riskmanagement-web` | Dashboard + JSON API on port 8085 | `rmctl.sh web-start` (section 7) |

| On the NAS | In the containers |
| --- | --- |
| `/volume1/docker/RiskManagement` (this repo) | `/app` (read-only) |
| `/volume1/docker/RiskManagement/.env` (secrets) | environment |
| `/volume1/docker/riskmanagement-data` | `/data`: `risk.db`, logs |

It needs an **x86-64** Synology model (Container Manager). Plan on about 2 GB
of RAM free for the one-off BookingDataAll load.

Everything below is done from the **DSM web interface**: Package Center,
Task Scheduler, File Station and Text Editor. **No SSH is needed.** Two scripts
in this folder do the work:

| Script | Use |
| --- | --- |
| `bootstrap.sh` | One-off first-time setup. You upload it with File Station, point a Task Scheduler task at it, and click Run a few times. |
| `rmctl.sh` | Every task after that: nightly jobs, updates, status, users. |

## 1. Before you start (Package Center)

Install these from **Package Center**:
- **Container Manager.** It runs Docker and creates the `docker` shared
  folder.
- **Git Server.** It provides the `git` command the scripts use.
- **Text Editor.** You use it to edit `.env` and read logs in File Station.

Optional, recommended: create `riskmanagement-data` yourself first as an
**encrypted shared folder** inside `docker`. Go to Control Panel → Shared
Folder → Create, and tick *Encrypt this shared folder*. That way `risk.db` is
encrypted at rest. If it doesn't exist, the bootstrap creates a plain folder.

## 2. First-time setup: the bootstrap task

**Upload the script.** Don't paste it into Task Scheduler: DSM won't save
long scripts that contain `<` or `>`, and shows *"Connection failed. Please
check your network settings"*.
1. On GitHub open `deploy/synology/bootstrap.sh` and click **Download raw
   file**.
2. In **File Station** open the **docker** shared folder → Upload → choose
   `bootstrap.sh`. It is now at `/volume1/docker/bootstrap.sh`.

**Create the task once.**
1. Control Panel → **Task Scheduler** → Create → Scheduled Task →
   **User-defined script**.
2. On the **General** tab:
   - Task: `RiskManagement bootstrap`
   - User: **root**
   - **Untick Enabled**, because it only runs when you click Run.
3. On the **Task Settings** tab, tick *Send run details by email*. Set *Run
   command* to this one line:
   ```
   sed -i 's/\r$//' /volume1/docker/bootstrap.sh; bash /volume1/docker/bootstrap.sh
   ```
   The `sed` part fixes Windows line endings if the file passed through a PC.
4. Click OK.

**Then select the task and click Run.** Each run stops at the next thing it
needs from you and tells you what to do. The message is in the task email,
and in File Station at **docker → riskmanagement-data → setup →
NEXT_STEP.txt**.

| Run | What happens | What you do |
| --- | --- | --- |
| 1 | Creates the folders and a read-only deploy key | Open `setup/deploy_key.pub` with Text Editor and copy the line. On GitHub go to RiskManagement → Settings → Deploy keys → Add deploy key. Title it *Synology NAS*, paste the line, leave **Allow write access unticked**, and save. Run again. |
| 2 | Clones the repo and creates **`setup/env-edit.txt`**, an editable copy of the settings. `.env` itself is root-only, so DSM Text Editor can't open it. | In File Station open **docker → riskmanagement-data → setup → env-edit.txt** with Text Editor and fill in every line. Choose `RISK_ENCRYPTION_PASSPHRASE` **once** and save it in the password manager first, because it must never change. Save, then Run again. The next run moves the settings into the protected `.env` and **deletes** `env-edit.txt`. |
| 3 | Builds the image, starts the worker, creates the warehouse and runs the tests | Nothing. `NEXT_STEP.txt` says **DONE**. The first build takes several minutes. |

If GitHub can't be reached on port 22, the deploy key is set up to use port
443 instead.

## 3. The tasks to create

Each task is a Task Scheduler **User-defined script**:
- User: **root**
- *Send run details by email*: ticked
- *Run command*: the line shown in the table

| Task name | Schedule | Run command |
| --- | --- | --- |
| RiskManagement full refresh | **disabled**, Run by hand once after setup (25–35 min, 2.5–3.5 GB); avoid 02:00–04:00 | `bash /volume1/docker/RiskManagement/deploy/synology/rmctl.sh full-refresh` |
| RiskManagement S3 nightly | daily 04:10 | `bash /volume1/docker/RiskManagement/deploy/synology/rmctl.sh ingest-s3` |
| RiskManagement SharePoint nightly | daily 04:20 | `bash /volume1/docker/RiskManagement/deploy/synology/rmctl.sh ingest-sharepoint` |
| RiskManagement risk report | daily 05:00 (after the imports) | `bash /volume1/docker/RiskManagement/deploy/synology/rmctl.sh report` |
| RiskManagement update | weekly, e.g. Sunday 03:00, or Run by hand after a merge | `bash /volume1/docker/RiskManagement/deploy/synology/rmctl.sh update` |
| RiskManagement status | **disabled**, Run by hand | `bash /volume1/docker/RiskManagement/deploy/synology/rmctl.sh status` |
| RiskManagement command | **disabled**, edit the line, then Run | e.g. `bash /volume1/docker/RiskManagement/deploy/synology/rmctl.sh report --no-email` |

The Stripe (04:30) and backup (04:50) tasks follow the same pattern and are
added in build steps 4 and 6.

**SharePoint import:** drop Booking Transactions CSVs into SharePoint
**Platform Data → Booking Transactions**.
- **Imported:** a file that imports is deleted, but only after its rows are
  saved.
- **Not imported:** a file that can't be imported moves to **Booking
  Transactions → Failed** with a `.error.txt` note saying why, and an alert
  email goes to `ALERT_EMAIL`.
- **Checking first:** Run the *command* task with `rmctl.sh ingest-sharepoint
  --dry-run` to check files without changing anything.

## 4. Seeing the results

- **Email:** each task's run details arrive by email when *Send run details
  by email* is ticked. A failed run has a non-zero exit code.
- **Logs:** File Station → **docker → riskmanagement-data → logs**. There's
  one file per run, e.g. `ingest-s3-20261106-041000-1234.log`; open it with
  Text Editor. The newest 60 per command are kept.
- **Quick check:** Run the *RiskManagement status* task. It shows the
  warehouse size, row counts and the last import per source.

## 5. `rmctl.sh` commands

| Command | Does |
| --- | --- |
| `setup` | Build the image, start the worker, create or upgrade the warehouse, run the tests |
| `update` | `git pull`, rebuild only if `requirements.txt` or the Dockerfile changed, upgrade the warehouse |
| `status` | Warehouse size, row counts, last import per source |
| `test` | Run the test suite inside the container |
| `ingest-sharepoint [options]` | Import Booking Transactions CSVs from SharePoint. `--dry-run` checks them without storing, moving or deleting anything. |
| `report [--no-email]` | Nightly risk report, emailed to `REPORT_EMAIL`: what changed (balances, priorities, new accounts and events), the top 10 risk accounts and events, import health, and the Booking Transactions check. It's also saved in **riskmanagement-data → reports**. |
| `check-transactions` | Booking Transactions import check only: files, match rate to S3 bookings, how each Gateway Message Description is classified, and the Status, CVV and card type values |
| `ingest-s3 [options]` | Nightly S3 load. Options pass through, e.g. `ingest-s3 --dry-run` or `ingest-s3 --only RiskReport` |
| `full-refresh` | One-off lifetime load from BookingDataAll |
| `web-start`, `web-stop`, `web-status` | Start (or restart), stop, or check the dashboard (section 7). Who can sign in is managed in Entra (app roles), not here. |

| `restart` | Restart the containers |
| `env-export`, then `env-apply` | Change a setting: `env-export` copies `.env` to `setup/env-edit.txt` for Text Editor. `env-apply` puts it back (root-only), deletes the copy and restarts the worker. It refuses to change `RISK_ENCRYPTION_PASSPHRASE` once the warehouse exists. |
| `run SCRIPT [args]` | Any `bin/` script, e.g. `run rm_admin.py status` |

Only one `rmctl` command runs at a time. A second one waits for the first
(for up to 2 hours), so a by-hand full refresh can't collide with the nightly
job.

**Ad-hoc commands without a task:** Container Manager → Container →
`riskmanagement` → Details → **Terminal** → Create → `bash`. Then run, for
example, `python3 bin/rm_admin.py status`. That's a terminal inside the
container, in the browser.

## 6. Updating

Run the **RiskManagement update** task. It pulls the latest code, rebuilds the
image only when needed, and applies any warehouse upgrade. The weekly
schedule does this automatically.

## 7. Dashboard

The dashboard shows the latest risk report as a list of accounts and events, plus:
- an **account page**: scores and their reasons, timeline, trust (bank transfers, past events), balance and score history, events
- an **event page**: scores, timeline, sales (average ticket, first 24 hours), payment attempts, bookings
- a **booking page**: S3 rows, payment attempts, reconciliation
- **search**: account, event or booking number, booking ID, gateway id, `VISA 1234`, email or phone (hashed, never logged), or a name
- **Imports**

Every page has a JSON twin under `/api/...`.

**Sign-in is Microsoft only:**
- a TryBooking tenant account, @trybooking.co.uk
- the app role Risk Viewer or Risk Admin, assigned in Entra
- MFA, enforced by Conditional Access and double-checked by the app

There are no passwords or static tokens. The API takes a Microsoft access token for `api://<client id>/Risk.Read`.

How it's protected:
- It opens `risk.db` **read-only**.
- No customer personal data appears anywhere.
- Every request is written to **logs → web-audit-YYYYMM.log**. Search terms are stored only as a hash.

**Set-up** is in [docs/entra_sso_setup.md](../../docs/entra_sso_setup.md):
1. the Entra app registration, roles and Conditional Access (Microsoft 365 admin)
2. the Synology DDNS address `https://trybooking.synology.me:8443`
3. a Let's Encrypt certificate for it in DSM
4. the DSM reverse proxy from 8443 to 8085, a router forward of 8443, and DSM firewall rules (8443 from the UK only)
5. the `WEB_*` settings in `.env`
6. `rmctl.sh web-start`

The router forwards **8443 only**, and the DSM firewall allows 8443 only from the UK. Never forward 8085, 5000/5001 or 22.

`update` restarts the dashboard automatically when it's running, so it always runs the latest code.

## Legacy report

`legacy/fraud_risk_report.py` is the current report. It still needs
s3reporting and stays on the Pi until phase 2 replaces it. Nothing in this
setup runs it.

## Appendix A: the same with SSH

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

**6. First S3 load.** This is the lifetime history from BookingDataAll. It
writes about 2.5–3.5 GB and takes 25–35 minutes, so run it outside 02:00–04:00:
```bash
docker exec riskmanagement python3 bin/ingest_s3.py --full-refresh --dry-run   # shows the files it will read
docker exec riskmanagement python3 bin/ingest_s3.py --full-refresh
docker exec riskmanagement python3 bin/rm_admin.py status
```
From then on, the nightly 04:10 task (below) loads only the current and
previous month. That takes about a minute. Rerun `--full-refresh` only if
TryBooking corrects old data.

**7. Dashboard access** is managed in Entra with app roles (docs/entra_sso_setup.md), not on the NAS.

### Admin commands (SSH)

```bash
docker exec riskmanagement python3 bin/rm_admin.py status          # schema, size, rows, last imports
```

### Scheduling with direct docker exec

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

### Updating (SSH)

```bash
git -C /volume1/docker/RiskManagement pull origin main
docker exec riskmanagement python3 bin/rm_admin.py init-db     # applies any new schema migrations
```

Code changes are picked up straight away. Rebuild with
`docker compose up -d --build` only when `requirements.txt` or the
`Dockerfile` changes; the PR description will say when.

## Appendix B: private repo access by hand (deploy key)

`bootstrap.sh` does all of this for you. These are the manual steps, for
reference.

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
