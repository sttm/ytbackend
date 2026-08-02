from pathlib import Path
import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import get_settings


settings = get_settings()
database_url = (
    os.environ.get("POSTGRES_DB_URL")
    or os.environ.get("PRODUCERSCENTER_BACKEND_DATABASE_URL")
    or os.environ.get("DATABASE_URL")
    or settings.database_url
)
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql+psycopg://", 1)
elif database_url.startswith("postgresql://"):
    database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)


def with_default_postgres_sslmode(url: str) -> str:
    if not url.startswith("postgresql+psycopg://"):
        return url
    parsed = urlsplit(url)
    hostname = parsed.hostname or ""
    if hostname in {"localhost", "127.0.0.1", "::1"}:
        return url
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if "sslmode" in query:
        return url
    query["sslmode"] = "require"
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


database_url = with_default_postgres_sslmode(database_url)

if database_url.startswith("sqlite:///"):
    db_path = database_url.replace("sqlite:///", "", 1)
    if db_path and db_path != ":memory:":
        Path(db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    connect_args = {"check_same_thread": False}
else:
    connect_args = {}

engine = create_engine(database_url, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    ensure_schema()


def check_db() -> dict:
    with engine.connect() as connection:
        dialect = connection.dialect.name
        result = connection.execute(text("SELECT 1")).scalar_one()
        tables = inspect(connection).get_table_names()
    return {
        "ok": result == 1,
        "dialect": dialect,
        "tables": len(tables),
        "using_postgres": dialect == "postgresql",
    }


def ensure_schema() -> None:
    inspector = inspect(engine)
    if "proxies" not in inspector.get_table_names():
        return

    proxy_columns = {column["name"] for column in inspector.get_columns("proxies")}
    required_columns = {
        "download_ms": "INTEGER DEFAULT 0",
    }
    stream_columns = {column["name"] for column in inspector.get_columns("stream_cache")} if "stream_cache" in inspector.get_table_names() else set()
    stream_required_columns = {
        "artist": "VARCHAR(512) DEFAULT ''",
        "artists_json": "TEXT DEFAULT '[]'",
        "album": "VARCHAR(512) DEFAULT ''",
        "track": "VARCHAR(512) DEFAULT ''",
        "release_year": "INTEGER DEFAULT 0",
    }

    with engine.begin() as connection:
        for column_name, ddl in required_columns.items():
            if column_name not in proxy_columns:
                connection.execute(text(f"ALTER TABLE proxies ADD COLUMN {column_name} {ddl}"))
        for column_name, ddl in stream_required_columns.items():
            if column_name not in stream_columns:
                connection.execute(text(f"ALTER TABLE stream_cache ADD COLUMN {column_name} {ddl}"))
