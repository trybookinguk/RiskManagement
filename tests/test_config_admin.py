import pytest

from rm import config


def test_defaults(env):
    s = config.get_settings()
    assert s.risk_db == str(env / "risk.db")
    assert s.payment_retention_days == 1100
    assert s.sharepoint_transactions_folder == "Platform Data/Booking Transactions"


def test_require_names_env_vars(env):
    s = config.get_settings()
    with pytest.raises(config.ConfigError) as e:
        s.require("stripe_key", "aws_access_key_id")
    assert "STRIPE_RESTRICTED_KEY" in str(e.value) and "AWS_ACCESS_KEY_ID" in str(e.value)


def test_secrets_not_in_repr(env, monkeypatch):
    monkeypatch.setenv("STRIPE_RESTRICTED_KEY", "rk_live_secret")
    assert "rk_live_secret" not in repr(config.get_settings())


def test_bad_int(env, monkeypatch):
    monkeypatch.setenv("PAYMENT_RETENTION_DAYS", "lots")
    with pytest.raises(config.ConfigError):
        config.get_settings()


def test_env_file(env, tmp_path, monkeypatch):
    monkeypatch.delenv("S3_BUCKET", raising=False)
    f = tmp_path / ".env"
    f.write_text("# comment\nexport S3_BUCKET='my-bucket'\nRISK_ENCRYPTION_PASSPHRASE=from-file\n\nJUNK\n")
    n = config.load_env_file(str(f))
    s = config.get_settings()
    assert s.s3_bucket == "my-bucket"
    assert s.risk_passphrase != "from-file"  # already set in env: env wins
    assert n == 1


def test_admin_cli_flow(env, capsys):
    import importlib.util, pathlib
    spec = importlib.util.spec_from_file_location(
        "rm_admin", pathlib.Path(__file__).resolve().parents[1] / "bin" / "rm_admin.py")
    adm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adm)
    assert adm.main(["status"]) == 1                          # no warehouse yet
    assert adm.main(["init-db"]) == 0
    assert "Hashing key created" in capsys.readouterr().out
    assert adm.main(["init-db"]) == 0
    assert "Passphrase check: OK" in capsys.readouterr().out
    assert adm.main(["add-user", "Henry@TryBooking.co.uk", "--role", "admin"]) == 0
    assert adm.main(["add-user", "not-an-email"]) == 1
    assert adm.main(["list-users"]) == 0
    assert "henry@trybooking.co.uk" in capsys.readouterr().out
    assert adm.main(["create-token", "power-bi"]) == 0
    out = capsys.readouterr().out
    token = [l.strip() for l in out.splitlines() if l.strip().startswith("tok_")][0]
    assert adm.main(["list-tokens"]) == 0
    listed = capsys.readouterr().out
    assert token.split(".")[1] not in listed                  # secret never listed
    assert adm.main(["revoke-token", token.split(".")[0]]) == 0
    assert adm.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "Passphrase check: OK" in out and "dashboard_users" in out


def test_admin_wrong_passphrase(env, monkeypatch, capsys):
    import importlib.util, pathlib
    spec = importlib.util.spec_from_file_location(
        "rm_admin", pathlib.Path(__file__).resolve().parents[1] / "bin" / "rm_admin.py")
    adm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adm)
    assert adm.main(["init-db"]) == 0
    monkeypatch.setenv("RISK_ENCRYPTION_PASSPHRASE", "changed")
    assert adm.main(["init-db"]) == 1
    assert "does not match" in capsys.readouterr().out


def test_create_token_to_file_keeps_it_out_of_output(env, capsys):
    import importlib.util, os, pathlib, stat
    spec = importlib.util.spec_from_file_location(
        "rm_admin", pathlib.Path(__file__).resolve().parents[1] / "bin" / "rm_admin.py")
    adm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adm)
    assert adm.main(["init-db"]) == 0
    out = env / "setup" / "token.txt"
    assert adm.main(["create-token", "Henry", "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    token = [l for l in out.read_text().splitlines() if l.startswith("tok_")][0]
    assert "." in token and token not in printed and token.split(".")[0] in printed
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
