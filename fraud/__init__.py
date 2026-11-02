"""Fraud risk tool (moved from trybookinguk/s3reporting).

The tool reuses s3reporting's shared code: the SQLite warehouse
(``modules.warehouse``), tier snapshot (``modules.tier_state``), S3 loader,
Graph email and SharePoint helpers, and config. Importing this package puts an
s3reporting checkout on ``sys.path`` so ``from modules ...`` resolves there.

Where s3reporting is looked for, first match wins:
  1. $S3REPORTING_DIR
  2. a sibling checkout: ../s3reporting next to this repo
  3. /root/s3reporting (the Pi)
"""
import os
import sys

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CANDIDATES = [os.environ.get("S3REPORTING_DIR"),
               os.path.join(os.path.dirname(_HERE), "s3reporting"),
               "/root/s3reporting"]


def s3reporting_dir():
    for d in _CANDIDATES:
        if d and os.path.isfile(os.path.join(d, "modules", "warehouse.py")):
            return os.path.abspath(d)
    return None


S3REPORTING_DIR = s3reporting_dir()
if S3REPORTING_DIR and S3REPORTING_DIR not in sys.path:
    sys.path.append(S3REPORTING_DIR)
    # s3reporting's defaults for these are relative to its own folder; pin
    # them there when .env doesn't set them, so running from this repo finds
    # the same warehouse, S3 cache and reports folder.
    os.environ.setdefault("S3_CACHE_DIR", os.path.join(S3REPORTING_DIR, ".cache"))
    os.environ.setdefault("REPORTS_DIR", os.path.join(S3REPORTING_DIR, "reports"))
