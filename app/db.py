"""Database engine and session handling."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine, _SessionFactory
    if _engine is None:
        settings = get_settings()
        _engine = create_engine(settings.database_url, pool_pre_ping=True)
        _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


@contextmanager
def session_scope(staff_id: UUID | None = None) -> Iterator[Session]:
    """Open a session, optionally stamped with the acting staff member.

    The staff id is set as a transaction-local GUC so that the audit triggers on
    candidate_identity can attribute every insert, update and read. Passing it
    is not optional for any code path that touches identity data -- an audit row
    with a null staff_id is evidence that proves nothing.
    """
    get_engine()
    assert _SessionFactory is not None
    session = _SessionFactory()
    try:
        if staff_id is not None:
            session.execute(
                text("SELECT set_config('app.staff_id', :sid, true)"),
                {"sid": str(staff_id)},
            )
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
