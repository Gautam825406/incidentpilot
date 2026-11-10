"""SQLAlchemy setup.

Postgres + pgvector when DATABASE_URL is a Postgres URL (embeddings are
stored as JSON float lists, so the same code runs on Postgres and on the
SQLite demo fallback; a pgvector Vector column is a drop-in migration —
see docs/architecture.md). SQLite is the local-demo default.
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import settings

connect_args = {}
if settings.database_url.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

engine = create_engine(settings.database_url, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    from . import models  # noqa: F401  (registers tables)

    Base.metadata.create_all(bind=engine)
