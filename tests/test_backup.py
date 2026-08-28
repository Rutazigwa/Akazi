"""Backup and restore.

docs/DEPLOYMENT.md says to test a restore before you need one. An instruction
in a document is a hope; this makes it a guarantee that runs on every push.

A real backup is taken from a real database and restored into a fresh one, and
the audit hash chain is verified on the far side -- if a restore silently
corrupted the chain, the evidence trail would be broken exactly when it is most
likely to be asked for.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

BACKUP = Path(__file__).parent.parent / "scripts" / "backup.sh"
PASSPHRASE = "a-test-passphrase-not-a-real-one"


def psql_dsn(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://")


@pytest.fixture
def backup_dir(tmp_path) -> Path:
    target = tmp_path / "backups"
    target.mkdir()
    return target


@pytest.fixture(scope="module")
def populated_db(scratch_database_module) -> str:
    """A migrated database with one committed candidate in it.

    Its own database, not the shared one: pg_dump connects separately and
    cannot see anything held in a rolled-back transaction, and committing into
    the shared database would leak an audit row that no later test can remove.
    """
    # One psql invocation rather than twenty-one: each migration file wraps
    # itself in BEGIN/COMMIT, so they run sequentially in a single session.
    combined = "\n".join(
        path.read_text()
        for path in sorted(
            (Path(__file__).parent.parent / "migrations").glob("*.sql")
        )
    )
    result = subprocess.run(
        ["psql", psql_dsn(scratch_database_module), "-q", "-v", "ON_ERROR_STOP=1"],
        input=combined, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr

    engine = create_engine(scratch_database_module, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO candidate_identity
                        (candidate_id, legal_first_name, legal_last_name,
                         date_of_birth, phone_primary)
                    VALUES (:cid, 'Backup', 'Test',
                            kigali_today() - INTERVAL '22 years', :phone)
                    """
                ),
                {
                    "cid": str(uuid.uuid4()),
                    "phone": f"+25078{uuid.uuid4().int % 10**7:07d}",
                },
            )
    finally:
        engine.dispose()
    return scratch_database_module


def take_backup(source_dsn: str, backup_dir: Path) -> tuple[Path, str]:
    result = subprocess.run(
        [str(BACKUP)],
        env={
            **os.environ,
            "BACKUP_PASSPHRASE": PASSPHRASE,
            "DATABASE_DSN": psql_dsn(source_dsn),
            "BACKUP_DIR": str(backup_dir),
        },
        capture_output=True,
        text=True,
    )
    files = list(backup_dir.glob("*.sql.gz.enc"))
    return (files[0] if files else None), result.stdout + result.stderr


def test_a_backup_verifies_and_is_encrypted(populated_db, backup_dir):
    path, output = take_backup(populated_db, backup_dir)
    assert path is not None, output
    assert "verified" in output

    # Encrypted at rest: the dump's own header must not be readable.
    assert b"PostgreSQL database dump" not in path.read_bytes()


def test_verification_is_not_flaky(populated_db, backup_dir):
    """The check once failed at random on good backups.

    It piped into `head`, which exits early and SIGPIPEs the upstream; under
    `set -o pipefail` the pipeline then reported failure. A verification that
    cries wolf trains whoever reads the cron mail to ignore it.
    """
    for _ in range(3):
        _, output = take_backup(populated_db, backup_dir)
        assert "VERIFICATION FAILED" not in output
        assert "verified" in output


def test_a_truncated_backup_fails_verification(populated_db, backup_dir):
    """A partial dump decrypts and unpacks happily; only the completion marker
    at the end distinguishes it from a whole one."""
    path, _ = take_backup(populated_db, backup_dir)

    truncated = backup_dir / "truncated.enc"
    truncated.write_bytes(path.read_bytes()[:2000])

    found = subprocess.run(
        f'openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 '
        f'-pass env:BACKUP_PASSPHRASE -in "{truncated}" 2>/dev/null '
        f'| gunzip 2>/dev/null '
        f'| grep -c "PostgreSQL database dump complete" || true',
        shell=True, capture_output=True, text=True,
        env={**os.environ, "BACKUP_PASSPHRASE": PASSPHRASE},
    )
    assert found.stdout.strip() == "0"


def test_it_refuses_to_write_an_unencrypted_dump(populated_db, backup_dir):
    """The dump holds national ID numbers. An unencrypted one is the same
    personal data with none of the access control."""
    env = {k: v for k, v in os.environ.items() if k != "BACKUP_PASSPHRASE"}
    result = subprocess.run(
        [str(BACKUP)],
        env={**env, "DATABASE_DSN": psql_dsn(populated_db),
             "BACKUP_DIR": str(backup_dir)},
        capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert "refusing to write an unencrypted dump" in result.stderr
    assert list(backup_dir.glob("*")) == []


def test_a_restore_reproduces_the_data_and_the_audit_chain(
    populated_db, backup_dir
):
    """The whole point. Restored counts must match, and the hash chain must
    still verify -- a restore that quietly broke it would leave the evidence
    trail unusable exactly when it is most likely to be asked for.

    Runs against its own database: pg_dump connects separately and would see
    nothing of the shared session's rolled-back transaction.
    """
    def counts(url: str) -> tuple[int, int, int]:
        engine = create_engine(url)
        try:
            with engine.connect() as conn:
                return (
                    conn.execute(
                        text("SELECT count(*) FROM candidate_identity")
                    ).scalar_one(),
                    conn.execute(text("SELECT count(*) FROM placements")).scalar_one(),
                    conn.execute(text("SELECT count(*) FROM audit_log")).scalar_one(),
                )
        finally:
            engine.dispose()

    before = counts(populated_db)
    assert before[0] >= 1, "nothing committed -- the test would prove nothing"
    assert before[2] >= 1, "no audit rows to verify the chain against"

    path, output = take_backup(populated_db, backup_dir)
    assert path is not None, output

    restored_name = f"akazi_restore_{uuid.uuid4().hex[:8]}"
    admin = create_engine(populated_db, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{restored_name}"'))

    base, _, query = populated_db.partition("?")
    restored_url = (
        base.rsplit("/", 1)[0] + f"/{restored_name}" + (f"?{query}" if query else "")
    )

    try:
        restore = subprocess.run(
            f'openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 '
            f'-pass env:BACKUP_PASSPHRASE -in "{path}" | gunzip | '
            f'psql "{psql_dsn(restored_url)}" -q -v ON_ERROR_STOP=1',
            shell=True, capture_output=True, text=True,
            env={**os.environ, "BACKUP_PASSPHRASE": PASSPHRASE},
        )
        assert restore.returncode == 0, restore.stderr

        assert counts(restored_url) == before

        engine = create_engine(restored_url)
        try:
            with engine.connect() as conn:
                broken = conn.execute(
                    text("SELECT * FROM verify_audit_chain()")
                ).first()
        finally:
            engine.dispose()
        assert broken is None, f"audit chain broken after restore: {broken}"
    finally:
        with admin.connect() as conn:
            conn.execute(
                text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                     "WHERE datname = :db"),
                {"db": restored_name},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{restored_name}"'))
        admin.dispose()
