import os

os.environ["BCRYPT_ROUNDS"] = "4"  # must be set before the app is imported
os.environ["AI_AUTO_TRIAGE"] = "false"  # Phase 1/2 tests expect untouched tickets; AI tests turn it on

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.core.database import Base, get_db, get_session_factory
from app.core.security import hash_password
from app.main import app
from app.services.chat_hub import hub
from app.models import User, UserRole

PASSWORD = "password123"


@pytest.fixture()
def session_factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture()
def db_session(session_factory):
    session = session_factory()
    yield session
    session.close()


@pytest.fixture()
def client(db_session, session_factory):
    def override():
        yield db_session

    app.dependency_overrides[get_db] = override
    # Background tasks open their own session; point them at the same in-memory database.
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    # Not using `with` so the startup hook (which targets the real DB) is skipped.
    yield TestClient(app)
    app.dependency_overrides.clear()


def make_user(db, email, role, name=None):
    user = User(
        email=email,
        full_name=name or email.split("@")[0],
        hashed_password=hash_password(PASSWORD),
        role=role,
    )
    db.add(user)
    db.commit()
    return user


def login(client, email) -> dict:
    res = client.post("/api/auth/login", data={"username": email, "password": PASSWORD})
    assert res.status_code == 200, res.text
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


@pytest.fixture()
def users(db_session):
    return {
        "alice": make_user(db_session, "alice@example.com", UserRole.customer),
        "bob": make_user(db_session, "bob@example.com", UserRole.customer),
        "agent": make_user(db_session, "agent@example.com", UserRole.agent),
        "admin": make_user(db_session, "admin@example.com", UserRole.admin),
    }


@pytest.fixture()
def auth(client, users):
    """Ready-made auth headers per user: auth['alice'], auth['agent'], ..."""
    return {name: login(client, u.email) for name, u in users.items()}


@pytest.fixture(autouse=True)
def _clean_hub():
    """The chat hub is a process-wide singleton; make sure no test sees another test's sockets."""
    hub.reset()
    yield
    hub.reset()


@pytest.fixture()
def ai_on(monkeypatch):
    """Run the AI pipeline when tickets are created (off by default in tests)."""
    monkeypatch.setattr(settings, "ai_auto_triage", True)


@pytest.fixture()
def seeded_kb(db_session):
    from app.services import kb
    from scripts.kb_articles import ARTICLES

    for title, content in ARTICLES:
        kb.create_article(db_session, title, content, "seed", None)
