"""Tests for the residency guard.

These protect a compliance control, not a convenience. If they start failing,
the app can be started in a configuration that breaches Law No. 058/2021.
"""

import pytest
from pydantic import ValidationError

from app.config import Residency, Settings


def test_local_dev_rejects_a_remote_database():
    with pytest.raises(ValueError, match="local_dev is for throwaway data"):
        Settings(
            database_url="postgresql+psycopg://user:pw@db.us-east-1.example.com/ops",
            data_residency=Residency.LOCAL_DEV,
        )


def test_local_dev_accepts_localhost():
    s = Settings(
        database_url="postgresql+psycopg://localhost/placement_ops",
        data_residency=Residency.LOCAL_DEV,
    )
    assert s.data_residency is Residency.LOCAL_DEV


def test_cross_border_requires_a_certificate_reference():
    with pytest.raises(ValueError, match="NCSA_CERTIFICATE_REF"):
        Settings(
            database_url="postgresql+psycopg://user:pw@eu.example.com/ops",
            data_residency=Residency.CROSS_BORDER_AUTHORISED,
        )


def test_cross_border_is_allowed_with_a_certificate_reference():
    s = Settings(
        database_url="postgresql+psycopg://user:pw@eu.example.com/ops",
        data_residency=Residency.CROSS_BORDER_AUTHORISED,
        ncsa_certificate_ref="NCSA-2026-001",
    )
    assert s.ncsa_certificate_ref == "NCSA-2026-001"


def test_residency_must_be_stated(monkeypatch, tmp_path):
    """There is no default. An unset posture is a configuration error.

    The ambient environment and any local .env are cleared first: this test
    asserts something about the code, and it would otherwise pass or fail
    depending on the shell it was run from.
    """
    monkeypatch.delenv("DATA_RESIDENCY", raising=False)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValidationError):
        Settings(database_url="postgresql+psycopg://localhost/ops")
