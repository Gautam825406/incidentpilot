"""SQLAlchemy setup.

Postgres is the PRIMARY database path (Phase 4): set DATABASE_URL to a
Postgres URL (Neon / Supabase free tier, or the ``postgres`` service in
docker-compose.yml) and the app runs against it with no code change —
``psycopg2-binary`` is in requirements.txt. SQLite is the LOCAL/DEMO
FALLBACK ONLY: it keeps local dev and the test suite (scratch SQLite
files) working with zero setup, and is what the default DATABASE_URL
points at. Do not run a shared/production deployment on SQLite.

Embeddings are stored as JSON float lists, so the same code runs on
Postgres and on the SQLite demo fallback; a pgvector Vector column is a
drop-in migration — see docs/architecture.md.
"""
import logging

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import settings

logger = logging.getLogger(__name__)


def normalize_database_url(url: str) -> str:
    """Normalise a Postgres URL for SQLAlchemy 2.x.

    Some providers (notably older Neon/Supabase connection strings and
    Heroku-style URLs) emit ``postgres://``; SQLAlchemy 2 only accepts
    ``postgresql://``. SQLite URLs pass through unchanged.
    """
    url = (url or "").strip()
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://"):]
    return url


effective_database_url = normalize_database_url(settings.database_url)
if settings.is_sqlite:
    logger.info("DATABASE_URL is SQLite — local/demo fallback only; "
                "use a Postgres DATABASE_URL for shared/production deployments.")

connect_args = {}
if effective_database_url.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

engine = create_engine(effective_database_url, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def add_missing_columns(bind_engine):
    """Idempotently add columns the models define but the DB file lacks.

    ``create_all`` only creates missing *tables*; it never alters an
    existing table. So a SQLite/Postgres file created before a model gained
    a column (e.g. the three Phase 6 ``Hypothesis`` JSON columns:
    ``related_previous_incidents``, ``alternatives``, ``next_steps``) would
    otherwise fail inserts with "no such column". This inspects every
    mapped table and issues ``ALTER TABLE ... ADD COLUMN`` for each missing
    column. Safe to run on every start: columns that already exist are
    skipped, and a column that cannot be added (e.g. NOT NULL with no
    default on a table that already has rows) is left alone instead of
    breaking startup — that case needs a manual migration.
    """
    from . import models  # noqa: F401  (registers tables)

    try:
        inspector = inspect(bind_engine)
        table_names = set(inspector.get_table_names())
    except Exception:
        return
    for table in Base.metadata.tables.values():
        if table.name not in table_names:
            continue
        try:
            existing = {c["name"] for c in inspector.get_columns(table.name)}
        except Exception:
            continue
        for column in table.columns:
            if column.name in existing:
                continue
            try:
                coltype = column.type.compile(dialect=bind_engine.dialect)
            except Exception:
                continue
            ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {coltype}'
            try:
                with bind_engine.begin() as conn:
                    conn.execute(text(ddl))
            except Exception:
                # e.g. NOT NULL without a usable default on a populated
                # table — do not fail startup; surface via a manual migration.
                continue


def init_db():
    from . import models  # noqa: F401  (registers tables)

    Base.metadata.create_all(bind=engine)
    add_missing_columns(engine)
