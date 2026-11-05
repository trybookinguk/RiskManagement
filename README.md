# RiskManagement

TryBooking UK's risk data warehouse and fraud and exposure scoring. It runs
only on the Synology NAS.

- **Design:** [docs/architecture.md](docs/architecture.md)
- **Build plan:** [docs/build_plan_phase1.md](docs/build_plan_phase1.md)
- **NAS setup and runbook:** [deploy/synology/README.md](deploy/synology/README.md)

## Status

| Build step | State |
| --- | --- |
| 1. Skeleton: config, warehouse schema, hashing, admin tool, Docker | **done** |
| 2. S3 ingest (bookings lifetime, accounts, balances, recon) | **done** |
| 3. SharePoint "Booking Transactions" ingest | next |
| 4. Stripe nightly + date-range backfill | — |
| 5. Dashboard + JSON API | — |
| 6. Backup, alerts, runbook | — |
| Phase 2: risk models (`config/models.yaml`), scores, report | — |

## Layout

| Path | Contents |
| --- | --- |
| `rm/config.py` | Settings from `.env`. Each job checks only what it needs. |
| `rm/crypto.py` | Keyed hashing of personal data, the card-number guard, and backup encryption |
| `rm/db.py`, `rm/schema.py` | SQLite warehouse: connection, migrations, passphrase check, upsert, import log |
| `rm/s3.py`, `rm/ingest/s3_reports.py` | S3 listing and streaming, report loaders, derived events and lifetime totals |
| `bin/rm_admin.py` | `init-db`, `status`, dashboard users, API tokens |
| `bin/ingest_s3.py` | Nightly S3 load; `--full-refresh` for lifetime history |
| `tests/` | `python3 -m pytest -q` |
| `deploy/synology/` | Dockerfile, docker-compose, `bootstrap.sh` + `rmctl.sh` (no-SSH setup and tasks via DSM Task Scheduler), NAS runbook |
| `legacy/` | The previous fraud report, which still depends on s3reporting. Replaced in phase 2. |

## Data protection

- **Personal data is hashed, never stored in clear.** This covers customer
  emails, phones, names, IP addresses and full postcodes. The hash is keyed
  from `RISK_ENCRYPTION_PASSPHRASE`.
- **Cards** are stored as brand + last 4 (+ Stripe fingerprint). Any card
  field with more than 4 unmasked digits is rejected.
- **Never commit** `.env`, exports or `risk.db`. `.gitignore` blocks the usual
  names.

## Local development

```bash
pip install -r requirements.txt
python3 -m pytest -q
RISK_DATA_DIR=/tmp/rm RISK_ENCRYPTION_PASSPHRASE=dev python3 bin/rm_admin.py init-db
```
