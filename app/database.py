from pathlib import Path
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import get_settings


settings = get_settings()

if settings.database_url.startswith("sqlite:///"):
    db_path = settings.database_url.replace("sqlite:///", "", 1)
    if db_path.startswith("./"):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    connect_args = {"check_same_thread": False}
else:
    connect_args = {}

engine = create_engine(settings.database_url, connect_args=connect_args)
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


def ensure_schema() -> None:
    inspector = inspect(engine)
    if "proxies" not in inspector.get_table_names():
        return

    proxy_columns = {column["name"] for column in inspector.get_columns("proxies")}
    required_columns = {
        "download_ms": "INTEGER DEFAULT 0",
    }

    with engine.begin() as connection:
        for column_name, ddl in required_columns.items():
            if column_name not in proxy_columns:
                connection.execute(text(f"ALTER TABLE proxies ADD COLUMN {column_name} {ddl}"))
