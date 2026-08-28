"""What a deployer is told to configure, against what the application reads.

INBOUND_WEBHOOK_SECRET was in .env.example and absent from the compose file's
environment block. A deployment that configured it correctly still lost every
worker reply -- harassment reports included -- because the container never saw
it, and the only symptom was a 503 the messaging provider observed.

Three files have to agree and none of them imports the others, so this is the
only place the disagreement can be caught.
"""
from __future__ import annotations

import re
from pathlib import Path

from app.config import Settings


COMPOSE = Path("deploy/docker-compose.prod.yml")
ENV_EXAMPLE = Path("deploy/.env.example")
DOCKERFILE = Path("Dockerfile")


def app_service_environment() -> set[str]:
    """The variables the app container is actually given."""
    text = COMPOSE.read_text()
    # The app service's environment block, up to the next top-level key.
    block = re.search(r"\n  app:\n(.*?)(?=\n  [a-z]|\Z)", text, re.S)
    assert block, "no app service found in the compose file"
    env = re.search(r"environment:\n(.*?)(?=\n    [a-z]|\Z)", block.group(1), re.S)
    assert env, "the app service declares no environment"
    return set(re.findall(r"^\s{6}([A-Z_]+):", env.group(1), re.M))


def env_example_keys() -> set[str]:
    return set(re.findall(r"^([A-Z_]+)=", ENV_EXAMPLE.read_text(), re.M))


def settings_keys() -> set[str]:
    return {name.upper() for name in Settings.model_fields}


# Variables that belong to the database container or the reverse proxy rather
# than the application, so they are in .env.example and never reach the app.
INFRASTRUCTURE = {
    "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB",
    "APP_DB_PASSWORD",   # builds DATABASE_URL in the compose file
    "SITE_ADDRESS",      # Caddy
    "BACKUP_PASSPHRASE",
}


def test_every_app_setting_offered_in_env_reaches_the_container():
    """The bug this file exists for."""
    offered = env_example_keys() - INFRASTRUCTURE
    passed_through = app_service_environment()
    missing = sorted(
        key for key in offered
        if key in settings_keys() and key not in passed_through
    )
    assert missing == [], (
        f"{missing} are in .env.example but never reach the app container. "
        "A deployer would set them and nothing would happen."
    )


def test_every_variable_the_compose_file_passes_is_a_real_setting():
    """A stale name in the compose file is a setting somebody thinks is
    applied."""
    unknown = sorted(
        key for key in app_service_environment()
        if key not in settings_keys() and key not in INFRASTRUCTURE
    )
    assert unknown == [], f"{unknown} are passed to the app but ignored by it"


def test_the_residency_posture_has_no_default_anywhere():
    """It must be stated deliberately. A default is a guess about the law."""
    compose = COMPOSE.read_text()
    assert "DATA_RESIDENCY: ${DATA_RESIDENCY:?" in compose, (
        "DATA_RESIDENCY must fail the deploy when unset, not default"
    )
    assert "data_residency: Residency" in Path("app/config.py").read_text()


def test_the_image_carries_the_stylesheet():
    """`COPY app ./app` takes the whole tree. If that ever became selective,
    every page would render unstyled with no error at all -- the policy
    forbids inline styles, so there is no fallback."""
    dockerfile = DOCKERFILE.read_text()
    assert "COPY app ./app" in dockerfile
    assert Path("app/web/static/akazi.css").exists()


def test_the_image_carries_the_migrations_and_scripts():
    """The crons documented in DEPLOYMENT.md run from inside this container."""
    dockerfile = DOCKERFILE.read_text()
    assert "COPY migrations ./migrations" in dockerfile
    assert "COPY scripts ./scripts" in dockerfile


def test_every_documented_cron_script_exists():
    """A cron line in the deployment guide naming a script that is not there
    fails silently at 4am."""
    guide = Path("docs/DEPLOYMENT.md").read_text()
    referenced = set(re.findall(r"python (scripts/[a-z_]+\.py)", guide))
    referenced |= set(re.findall(r"(scripts/[a-z_]+\.sh)", guide))
    assert referenced, "no cron scripts found in the deployment guide"
    missing = sorted(name for name in referenced if not Path(name).exists())
    assert missing == [], f"the deployment guide runs scripts that do not exist: {missing}"


def test_the_app_does_not_run_as_root():
    """A container escape should not land on a root shell."""
    assert "USER akazi" in DOCKERFILE.read_text()
