"""Application settings.

The residency check below is deliberate. Law No. 058/2021 requires personal data
to be stored in Rwanda unless a registration certificate authorises otherwise,
and the most likely way to breach that is not a decision -- it is a default. A
developer points DATABASE_URL at a convenient managed Postgres in us-east-1 "just
for now", national ID numbers land on it, and the breach is retroactive and
unfixable. So the app refuses to start unless the residency posture is stated
explicitly.
"""

from __future__ import annotations

from enum import Enum
from urllib.parse import urlparse

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Residency(str, Enum):
    """How this deployment satisfies the data-residency requirement."""

    # Postgres on a Rwandan or regionally-hosted VPS. Recommended for the pilot.
    RWANDA_SELF_HOSTED = "rwanda_self_hosted"
    # Identifying data in Rwanda; a foreign store holds only opaque UUIDs.
    SPLIT_STORE = "split_store"
    # NCSA cross-border authorisation held. Requires a certificate reference.
    CROSS_BORDER_AUTHORISED = "cross_border_authorised"
    # Local development against throwaway data. Never with real personal data.
    LOCAL_DEV = "local_dev"


# Managed database providers with no Rwandan region. Declaring
# rwanda_self_hosted while pointing at one of these is not a borderline
# judgement about where a machine sits -- it is a contradiction, and it is the
# exact scenario the blueprint names: "do not scaffold onto a managed US/EU
# database just for now."
#
# Hostname suffixes only. Nothing here tries to geolocate an IP address: that
# needs a database we would have to keep current, it is wrong at the edges,
# and a check that is wrong at the edges gets switched off. These are
# unambiguous.
FOREIGN_MANAGED_HOSTS = (
    ".rds.amazonaws.com",
    ".neon.tech",
    ".supabase.co",
    ".supabase.com",
    ".database.azure.com",
    ".postgres.database.azure.com",
    ".cloudsql.google.com",
    ".gcp.cloud",
    ".render.com",
    ".herokuapp.com",
    ".amazonaws.com",
    ".planetscale.com",
    ".cockroachlabs.cloud",
    ".timescaledb.io",
    ".elephantsql.com",
    ".digitalocean.com",
    ".ondigitalocean.app",
    ".aivencloud.com",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str = Field(
        default="postgresql+psycopg://localhost/placement_ops"
    )

    # No default. An unset residency posture is a configuration error, not a
    # value to guess.
    data_residency: Residency

    # Required when data_residency is CROSS_BORDER_AUTHORISED: the NCSA
    # registration certificate reference authorising storage abroad.
    ncsa_certificate_ref: str | None = None

    app_name: str = "Placement Operations"
    debug: bool = False

    # Identity data requires a second factor on the session. Defaults on: a
    # single leaked coordinator password otherwise reaches national ID numbers.
    # Turn it off only for local development against throwaway data.
    require_mfa_for_identity: bool = True

    # Shared secret for the messaging provider's inbound webhook. Unset means
    # the endpoint returns 503 rather than accepting unauthenticated posts.
    inbound_webhook_secret: str | None = None

    @model_validator(mode="after")
    def _check_residency(self) -> "Settings":
        host = (urlparse(self.database_url).hostname or "").lower()
        is_local = host in {"", "localhost", "127.0.0.1", "::1", "db", "postgres"}

        if self.data_residency is Residency.CROSS_BORDER_AUTHORISED:
            if not self.ncsa_certificate_ref:
                raise ValueError(
                    "data_residency=cross_border_authorised requires "
                    "NCSA_CERTIFICATE_REF (the certificate authorising storage "
                    "or transfer abroad). Without it this deployment is "
                    "non-compliant with Law No. 058/2021."
                )
            return self

        if self.data_residency is Residency.LOCAL_DEV:
            if not is_local:
                raise ValueError(
                    f"data_residency=local_dev but DATABASE_URL points at "
                    f"a remote host ({host!r}). local_dev is for throwaway "
                    f"data on a local database only."
                )
            # Nothing below applies: local_dev is throwaway data by
            # definition, and MFA and debug are a developer's business.
            return self

        # Postures that assert the data is in Rwanda. The declaration and the
        # connection string were free to disagree, and nothing noticed.
        foreign = next(
            (suffix for suffix in FOREIGN_MANAGED_HOSTS
             if host.endswith(suffix)),
            None,
        )
        if foreign is not None:
            raise ValueError(
                f"data_residency={self.data_residency.value} but DATABASE_URL "
                f"points at {host!r}, a managed provider with no Rwandan "
                f"region. Law No. 058/2021 requires storage in Rwanda unless "
                f"an NCSA certificate authorises otherwise -- in which case "
                f"set data_residency=cross_border_authorised and supply "
                f"NCSA_CERTIFICATE_REF."
            )

        # A deployment holding national ID numbers must not have the second
        # factor switched off. This is exactly the setting that gets changed
        # under pressure, on a Friday, to get somebody logged in.
        if not self.require_mfa_for_identity:
            raise ValueError(
                "require_mfa_for_identity=false is only for local_dev. A "
                "password alone would reach national ID numbers, and one "
                "leaked coordinator login would be enough."
            )

        # Debug tracebacks on a page a coordinator can reach put database
        # structure and query fragments in front of whoever provoked the error.
        if self.debug:
            raise ValueError(
                "debug=true is only for local_dev: error pages would show "
                "query fragments and schema to whoever triggered them."
            )

        return self


def get_settings() -> Settings:
    return Settings()
