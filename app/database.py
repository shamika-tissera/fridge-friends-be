import os
from collections.abc import Generator

from dotenv import load_dotenv
from sqlalchemy import event
from sqlmodel import Session, create_engine

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./grocery.db")


def normalize_url(url: str) -> str:
    """Route bare postgres URLs (what Supabase hands you) through psycopg 3."""
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


DATABASE_URL = normalize_url(DATABASE_URL)
is_sqlite = DATABASE_URL.startswith("sqlite")

if is_sqlite:
    engine = create_engine(
        DATABASE_URL, echo=False, connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def _enable_sqlite_fks(dbapi_connection, _record):  # pragma: no cover - driver glue
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

else:
    engine = create_engine(
        DATABASE_URL,
        echo=False,
        # Supabase's pooler drops idle connections; recycle and check before use.
        pool_pre_ping=True,
        pool_recycle=300,
        pool_size=5,
        max_overflow=5,
        connect_args={
            "sslmode": os.getenv("DATABASE_SSLMODE", "require"),
            "connect_timeout": 10,
        },
    )


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session
