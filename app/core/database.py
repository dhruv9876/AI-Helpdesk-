from collections.abc import Callable, Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings

# SQLite needs this flag because FastAPI runs sync endpoints in a thread pool.
_connect_args = (
    {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
)

engine = create_engine(settings.database_url, connect_args=_connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_session_factory() -> Callable[[], Session]:
    """Background tasks run after the response is sent, so they cannot reuse the request's
    session. They get a factory instead (tests override this to point at the test database)."""
    return SessionLocal
