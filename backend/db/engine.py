"""
db/engine.py — SQLAlchemy 2.x engine + session factory.

Security hardening applied:
- Credentials loaded exclusively from environment variables (via config.py)
- Connection string never logged (hide_parameters=True in production)
- pool_pre_ping detects stale connections before use
- pool_recycle prevents MySQL's default 8-hour wait_timeout from killing idle connections
- connect_timeout=10 prevents hung connections from blocking startup
- Principle of Least Privilege: the DB user (from env) should only have
  SELECT, INSERT, UPDATE, DELETE — not DDL — for runtime queries.
  Alembic migrations use the same user but during deployments only.

Production-grade connection pooling:
- Configurable pool size based on environment
- Connection overflow for burst traffic
- Connection timeout and recycling
- Health checks via pool_pre_ping
"""
import logging
from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from config.environment import get_settings

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def _build_engine():
    """Build SQLAlchemy engine with production-grade connection pooling."""
    settings = get_settings()
    db_config = settings.database
    
    # Validate database configuration
    errors = db_config.validate()
    if errors:
        raise ValueError(f"Database configuration errors: {errors}")

    url = db_config.database_url

    # Adjust pool settings based on environment
    if settings.is_production:
        pool_size = db_config.DB_POOL_SIZE
        max_overflow = db_config.DB_MAX_OVERFLOW
        pool_timeout = db_config.DB_POOL_TIMEOUT
        pool_recycle = db_config.DB_POOL_RECYCLE
    else:
        # Development: smaller pool
        pool_size = 5
        max_overflow = 10
        pool_timeout = 30
        pool_recycle = 3600

    eng = create_engine(
        url,
        # ── Connection pool ───────────────────────────────────────────────────
        pool_pre_ping=True,          # discard stale connections before use
        pool_recycle=pool_recycle,   # recycle before MySQL's 8h wait_timeout
        pool_size=pool_size,         # persistent connections kept open
        max_overflow=max_overflow,   # burst headroom above pool_size
        pool_timeout=pool_timeout,   # max wait for a pooled connection
        # ── Security ─────────────────────────────────────────────────────────
        hide_parameters=True,        # never log query parameter values
        # ── Reliability ──────────────────────────────────────────────────────
        connect_args={
            "connect_timeout": 10,   # don't hang forever on network issues
            "charset": "utf8mb4",
        },
        echo=settings.app.DEBUG,     # SQL logging only in debug mode
    )

    # Enforce utf8mb4 for every new connection (guards against server default changes)
    @event.listens_for(eng, "connect")
    def set_charset(dbapi_conn, _connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("SET NAMES utf8mb4")
        cursor.execute("SET CHARACTER SET utf8mb4")
        cursor.execute("SET character_set_connection=utf8mb4")
        cursor.close()

    # Log connection pool configuration
    log.info(
        "MySQL engine ready: %s@%s:%s/%s (pool_size=%d, max_overflow=%d, pool_timeout=%d)",
        db_config.DB_USER, db_config.DB_HOST, db_config.DB_PORT,
        db_config.DB_NAME, pool_size, max_overflow, pool_timeout,
    )
    return eng


engine = _build_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """
    Context-manager that yields a Session and commits on success,
    rolls back on any exception, and always closes the session.

    Usage:
        with get_session() as session:
            session.add(obj)
    """
    session: Session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db():
    """Dependency for FastAPI to get database session."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
