# RiskManagement

TryBooking UK fraud and risk tooling.

| Script | What it does |
| --- | --- |
| `fraud_risk_report.py` | Fraud risk score per account and event: TryBooking risk tests, PayPal and Stripe chargebacks, and exposure. Writes `risk_fraud` to the warehouse, saves an xlsx to SharePoint and emails it to henry@trybooking.co.uk. See [docs/fraud_risk_report.md](docs/fraud_risk_report.md). |

## Layout

- `fraud_risk_report.py`: the command-line script.
- `fraud/risk.py`: risk tests, scoring and the `risk_fraud` tables.
- `fraud/chargebacks.py`: loads PayPal "All Dispute Cases" and Stripe "Disputes export" files and matches them to bookings.
- `fraud/countries.py`: ISO 3166 country table.

## Depends on s3reporting

The tool doesn't keep its own copy of the data pipeline. It uses these from
[trybookinguk/s3reporting](https://github.com/trybookinguk/s3reporting):

- the SQLite warehouse (`modules/warehouse.py`, which `prepare_data.py` loads nightly)
- the tier snapshot
- the S3 loader
- the Graph email and SharePoint helpers
- `.env`

`fraud/__init__.py` finds the s3reporting checkout. It checks, in order:
1. `$S3REPORTING_DIR`
2. `/app`, the s3reporting container on the NAS
3. `../s3reporting` next to this repo
4. `/root/s3reporting`

S3 tooling also stays in s3reporting:
- `warehouse_backfill.py`: the one-off column refill.
- `s3_inventory.py`: which S3 columns exist.

## On the Synology NAS

It runs inside the s3reporting Docker container, with this repo mounted at
`/risk`. Setup, dispute-file location and scheduling are in
[deploy/synology/README.md](deploy/synology/README.md).

```bash
sudo docker exec -w /risk s3reporting python3 fraud_risk_report.py
```

Outside the container, for example on the Pi, it also works with any
s3reporting checkout found as above. Source `.env` first.

**Never commit dispute exports, signals or login files.** They contain customer
emails and names. `.gitignore` blocks the usual file names.
