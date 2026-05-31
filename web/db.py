"""
Conexión SQLAlchemy para datos transaccionales de la tienda Biobella.

Mismo patrón que core/db_storage.py:
- DATABASE_URL set  → PostgreSQL (Render)
- DATABASE_URL unset → SQLite local (data/biobella.db) para desarrollo en Mac

Las tablas relacionales (products, product_locks, sync_log, app_settings, orders…)
conviven con el kv_store de core/db_storage.py — son tablas independientes en
el mismo Postgres, sin conflicto.
"""
import os
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session, DeclarativeBase


def _resolve_database_url() -> str:
    url = os.environ.get('DATABASE_URL', '').strip()
    if url:
        # SQLAlchemy 2.x exige el scheme postgresql:// en lugar del legacy postgres://
        if url.startswith('postgres://'):
            url = 'postgresql://' + url[len('postgres://'):]
        if 'sslmode' not in url:
            url += ('&' if '?' in url else '?') + 'sslmode=require'
        return url
    # Fallback local
    data_dir = os.path.join(os.path.dirname(__file__), '..', 'data')
    os.makedirs(data_dir, exist_ok=True)
    return 'sqlite:///' + os.path.abspath(os.path.join(data_dir, 'biobella.db'))


DATABASE_URL = _resolve_database_url()
_is_sqlite = DATABASE_URL.startswith('sqlite')

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    future=True,
    connect_args={'check_same_thread': False} if _is_sqlite else {},
)

SessionFactory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Session = scoped_session(SessionFactory)


class Base(DeclarativeBase):
    pass


@contextmanager
def session_scope():
    """Context manager: commit on success, rollback on error, always close."""
    s = SessionFactory()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def init_db():
    """Crea las tablas si no existen. Idempotente."""
    # Import diferido para evitar ciclos — los modelos se registran en Base.metadata
    # al importarse
    from web import models_tienda  # noqa: F401
    Base.metadata.create_all(engine)
