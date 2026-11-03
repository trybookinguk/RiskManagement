"""RiskManagement: risk data warehouse for TryBooking UK.

Package layout (see docs/build_plan_phase1.md):
    config   settings from the environment (.env on the NAS)
    crypto   keyed hashing of personal data, card-number guard, backup encryption
    db       SQLite warehouse: connection, schema migrations, upsert helpers
"""

__version__ = "0.1.0"
