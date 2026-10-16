import os
from functools import lru_cache

from sqlalchemy import create_engine


@lru_cache(maxsize=1)
def get_db_engine():
    """One engine (and connection pool) per process, instead of a new engine per request."""
    db_user = os.getenv("DB_USER")
    db_pass = os.getenv("DB_PASSWORD")
    db_host = os.getenv("DB_HOST")
    db_port = os.getenv("DB_PORT")
    db_name = os.getenv("DB_NAME")

    if db_host:
        db_url = f"postgresql+psycopg://{db_user}:{db_pass}@{db_host}:{db_port}/{db_name}"
    else:
        db_url = os.getenv("POSTGRES_URL", "postgresql+psycopg://postgres:password@localhost:5432/clinical_db")

    return create_engine(db_url, pool_pre_ping=True, pool_size=5, max_overflow=5)
