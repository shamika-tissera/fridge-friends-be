import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app import database, llm
from app.database import get_session
from app.main import app


@pytest.fixture(autouse=True)
def no_real_llm_calls(monkeypatch):
    """Fail fast if a test reaches the real LLM.

    Without this, an unstubbed test makes a live network call: slow, billed,
    and it hangs the whole suite rather than failing.
    """
    def _blocked(*args, **kwargs):
        raise AssertionError(
            "This test tried to call the real LLM. Stub app.llm.call_model instead."
        )

    monkeypatch.setattr(llm, "get_client", _blocked)


@pytest.fixture(name="session")
def session_fixture(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    # SQLite ignores foreign keys unless asked, so without this the test DB
    # silently skips ON DELETE CASCADE that Postgres would apply — the tests
    # would pass while production behaved differently.
    @event.listens_for(engine, "connect")
    def _enable_fks(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    # Point the app's lifespan at this in-memory engine so running the tests
    # never touches (or creates) the real sqlite file.
    monkeypatch.setattr(database, "engine", engine)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture(name="client")
def client_fixture(session):
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def make_user(client):
    def _make(email: str, name: str = "Test User", **extra) -> int:
        # Tests identify users by email; the User ID is derived from it so a
        # test only has to care about the handle when it is testing sign-up.
        body = {
            # Padded: some fixtures use two-character local parts, and the
            # User ID has a three-character minimum.
            "username": extra.pop("username", None) or f"u-{email.split('@')[0].lower()}",
            "password": extra.pop("password", "shelf-life-8"),
            "email": email,
            "name": name,
            **extra,
        }
        resp = client.post("/users", json=body)
        assert resp.status_code == 201, resp.text
        return resp.json()["id"]

    return _make
