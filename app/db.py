import os

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import settings


def _ensure_sqlite_dir(url: str) -> None:
    """Make sure the parent directory exists when DATABASE_URL points at a SQLite file."""
    if not url.startswith('sqlite:'):
        return
    # sqlite:///relative/path  -> 'relative/path'
    # sqlite:////absolute/path -> '/absolute/path'
    path = url.split('sqlite:///', 1)[-1] if url.startswith('sqlite:///') else ''
    if not path or path == ':memory:':
        return
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)


_ensure_sqlite_dir(settings.database_url)

Base = declarative_base()
engine = create_engine(settings.database_url, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
