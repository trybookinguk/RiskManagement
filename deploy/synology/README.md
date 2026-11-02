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

If the repos are private, the NAS needs GitHub credentials to clone and pull
them, for example a fine-grained read-only token.

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
