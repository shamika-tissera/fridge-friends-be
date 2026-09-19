from logging.config import fileConfig

from alembic import context
from sqlmodel import SQLModel

from app.database import DATABASE_URL, engine
import app.models  # noqa: F401  (imported for its side effect: registers the tables)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The URL comes from app.database (which reads .env), never from alembic.ini,
# so there is exactly one place a connection string lives.
config.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))

target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=DATABASE_URL.startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            # SQLite cannot ALTER columns; batch mode rewrites the table instead.
            render_as_batch=DATABASE_URL.startswith("sqlite"),
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
