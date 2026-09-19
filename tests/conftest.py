import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app import database
from app.database import get_session
from app.main import app


@pytest.fixture(name="session")
def session_fixture(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
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
    def _make(email: str, name: str = "Test User") -> int:
        resp = client.post("/users", json={"email": email, "name": name})
        assert resp.status_code == 201, resp.text
        return resp.json()["id"]

    return _make
