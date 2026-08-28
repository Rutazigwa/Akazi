"""The configuration checks standing behind the blueprint's architecture
blocker.

"A default cloud Postgres project on US or EU infrastructure holding Rwandan
national ID numbers, home locations and assessment scores is non-compliant
from day one. Do not scaffold onto a managed US/EU database just for now."

The posture was declared in one variable and the database named in another,
and nothing checked that they agreed. A deployment could claim
rwanda_self_hosted while pointing at RDS in Ireland and start without
complaint.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Residency, Settings


def settings(**over):
    base = {
        "data_residency": Residency.RWANDA_SELF_HOSTED,
        "database_url": "postgresql+psycopg://akazi:pw@10.0.0.5/akazi",
        "require_mfa_for_identity": True,
        "debug": False,
    }
    base.update(over)
    return Settings(**base)


# --- where the data actually is -------------------------------------------

@pytest.mark.parametrize("host", [
    "akazi-prod.abc123.eu-west-1.rds.amazonaws.com",
    "db.xyz.supabase.co",
    "akazi.postgres.database.azure.com",
    "ep-cool-name-123.eu-central-1.aws.neon.tech",
    "akazi-db.aivencloud.com",
])
def test_a_foreign_managed_database_contradicts_the_declaration(host):
    """Not a borderline judgement about where a machine sits -- none of these
    has a Rwandan region, so the claim and the connection cannot both be true.
    """
    with pytest.raises(ValidationError, match="no Rwandan region"):
        settings(database_url=f"postgresql+psycopg://u:p@{host}/akazi")


def test_a_plausible_rwandan_host_is_accepted():
    """The check refuses what is certainly wrong. It does not pretend to
    geolocate an address -- that needs a database to keep current, it is wrong
    at the edges, and a check that is wrong at the edges gets switched off."""
    assert settings(
        database_url="postgresql+psycopg://akazi:pw@10.0.0.5/akazi"
    ).data_residency is Residency.RWANDA_SELF_HOSTED


def test_the_split_store_posture_is_checked_the_same_way():
    with pytest.raises(ValidationError, match="no Rwandan region"):
        settings(data_residency=Residency.SPLIT_STORE,
                 database_url="postgresql+psycopg://u:p@x.rds.amazonaws.com/a")


def test_cross_border_still_needs_its_certificate():
    """The one posture where a foreign host is lawful, and only with the
    paperwork that makes it so."""
    with pytest.raises(ValidationError, match="NCSA_CERTIFICATE_REF"):
        settings(data_residency=Residency.CROSS_BORDER_AUTHORISED,
                 database_url="postgresql+psycopg://u:p@x.rds.amazonaws.com/a")

    ok = settings(data_residency=Residency.CROSS_BORDER_AUTHORISED,
                  database_url="postgresql+psycopg://u:p@x.rds.amazonaws.com/a",
                  ncsa_certificate_ref="NCSA-2026-00123")
    assert ok.ncsa_certificate_ref == "NCSA-2026-00123"


def test_local_dev_still_refuses_a_remote_database():
    with pytest.raises(ValidationError, match="local_dev is for throwaway"):
        settings(data_residency=Residency.LOCAL_DEV,
                 database_url="postgresql+psycopg://u:p@somewhere.example/a")


# --- the settings that get changed under pressure -------------------------

def test_the_second_factor_cannot_be_switched_off_in_production():
    """Exactly the setting somebody turns off on a Friday to get a coordinator
    logged in. A password alone would then reach national ID numbers."""
    with pytest.raises(ValidationError, match="only for local_dev"):
        settings(require_mfa_for_identity=False)


def test_debug_cannot_be_left_on_in_production():
    """Error pages would show query fragments and schema to whoever provoked
    them."""
    with pytest.raises(ValidationError, match="only for local_dev"):
        settings(debug=True)


def test_local_development_may_do_both():
    """Throwaway data by definition, and a developer's own business."""
    relaxed = settings(data_residency=Residency.LOCAL_DEV,
                       database_url="postgresql+psycopg://localhost/akazi",
                       require_mfa_for_identity=False, debug=True)
    assert relaxed.debug is True


def test_the_foreign_host_list_is_not_empty():
    """Guards the guard: an empty list would let every check above pass."""
    from app.config import FOREIGN_MANAGED_HOSTS

    assert len(FOREIGN_MANAGED_HOSTS) > 10
    assert all(h.startswith(".") for h in FOREIGN_MANAGED_HOSTS)
