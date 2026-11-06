"""Settings, read from the environment.

On the NAS the containers get them from /volume1/docker/RiskManagement/.env via
docker-compose ``env_file``. For a manual run elsewhere, pass ``--env-file`` to
a bin/ script (or call ``load_env_file``) — only variables not already set in
the environment are taken from the file.

Each job asks only for what it needs with ``settings.require(...)``, so e.g.
the web container never needs the Stripe key.
"""
import os
from dataclasses import dataclass, field, fields


class ConfigError(RuntimeError):
    """A required setting is missing or invalid."""


def load_env_file(path: str) -> int:
    """Load KEY=VALUE lines from ``path`` into os.environ (existing vars win).

    Handles comments, blank lines, ``export KEY=...`` and single/double quotes.
    Returns the number of variables set.
    """
    n = 0
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value
                n += 1
    return n


def _int(name, default):
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from e


@dataclass(frozen=True)
class Settings:
    # Paths
    data_dir: str = "/data"
    risk_db: str = "/data/risk.db"
    # Secrets / credentials
    risk_passphrase: str = field(default="", repr=False)
    aws_access_key_id: str = field(default="", repr=False)
    aws_secret_access_key: str = field(default="", repr=False)
    s3_bucket: str = "produk-rdsextracts-438255373632"
    azure_tenant_id: str = ""
    azure_client_id: str = ""
    azure_client_secret: str = field(default="", repr=False)
    azure_sender_mailbox: str = ""
    sharepoint_drive_id: str = ""
    stripe_key: str = field(default="", repr=False)
    # Behaviour
    sharepoint_transactions_folder: str = "Platform Data/Booking Transactions"
    payment_retention_days: int = 1100
    alert_email: str = "henry@trybooking.co.uk"
    report_email: str = "henry@trybooking.co.uk"
    timezone: str = "Europe/London"
    web_port: int = 8085
    # Dashboard sign-in with Microsoft (optional; token sign-in works without it)
    web_entra_client_id: str = ""
    web_entra_client_secret: str = field(default="", repr=False)
    web_base_url: str = ""
    web_allowed_domains: str = "trybooking.co.uk"
    web_mfa_check: str = "amr_or_acrs"      # amr_or_acrs | amr | acrs | off
    web_auth_context: str = "c1"            # Conditional Access authentication context requiring MFA
    web_api_audience: str = ""              # extra accepted audience (default api://<client id>)

    _ENV = {
        "data_dir": "RISK_DATA_DIR",
        "risk_db": "RISK_DB",
        "risk_passphrase": "RISK_ENCRYPTION_PASSPHRASE",
        "aws_access_key_id": "AWS_ACCESS_KEY_ID",
        "aws_secret_access_key": "AWS_SECRET_ACCESS_KEY",
        "s3_bucket": "S3_BUCKET",
        "azure_tenant_id": "AZURE_TENANT_ID",
        "azure_client_id": "AZURE_CLIENT_ID",
        "azure_client_secret": "AZURE_CLIENT_SECRET",
        "azure_sender_mailbox": "AZURE_SENDER_MAILBOX",
        "sharepoint_drive_id": "SHAREPOINT_DRIVE_ID",
        "stripe_key": "STRIPE_RESTRICTED_KEY",
        "sharepoint_transactions_folder": "SHAREPOINT_TRANSACTIONS_FOLDER",
        "payment_retention_days": "PAYMENT_RETENTION_DAYS",
        "alert_email": "ALERT_EMAIL",
        "report_email": "REPORT_EMAIL",
        "timezone": "RISK_TIMEZONE",
        "web_port": "WEB_PORT",
        "web_entra_client_id": "WEB_ENTRA_CLIENT_ID",
        "web_entra_client_secret": "WEB_ENTRA_CLIENT_SECRET",
        "web_base_url": "WEB_BASE_URL",
        "web_allowed_domains": "WEB_ALLOWED_DOMAINS",
        "web_mfa_check": "WEB_MFA_CHECK",
        "web_auth_context": "WEB_AUTH_CONTEXT",
        "web_api_audience": "WEB_API_AUDIENCE",
    }

    @classmethod
    def from_env(cls) -> "Settings":
        kw = {}
        for f in fields(cls):
            if f.name.startswith("_"):
                continue
            env = cls._ENV[f.name]
            if f.type is int or f.type == "int":
                kw[f.name] = _int(env, f.default)
            else:
                value = os.environ.get(env)
                if value not in (None, ""):
                    kw[f.name] = value.strip()
        if "risk_db" not in kw and "data_dir" in kw:
            kw["risk_db"] = os.path.join(kw["data_dir"], "risk.db")
        s = cls(**kw)
        if s.payment_retention_days < 30:
            raise ConfigError("PAYMENT_RETENTION_DAYS must be at least 30")
        return s

    def require(self, *names: str) -> "Settings":
        """Raise ConfigError naming every missing setting (by env var name)."""
        missing = [self._ENV[n] for n in names if not getattr(self, n)]
        if missing:
            raise ConfigError("Missing settings in .env: " + ", ".join(missing))
        return self


def get_settings() -> Settings:
    return Settings.from_env()
