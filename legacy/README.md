# Legacy fraud report (pre-warehouse)

These files are the fraud risk report as it ran in trybookinguk/s3reporting.
They still import s3reporting's warehouse, email and SharePoint code (see
`fraud/__init__.py`), so they only run next to an s3reporting checkout:

```bash
cd legacy && python3 fraud_risk_report.py --help
```

Phase 2 rebuilds the scoring on the RiskManagement warehouse (`rm/`) and
`config/models.yaml`, then this folder is removed. Until then the live copy
on the Pi (s3reporting `main`) is the one in use.
