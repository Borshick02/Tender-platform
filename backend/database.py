"""Подключение к БД и сессии."""
import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

try:
    from dotenv import load_dotenv
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
except ImportError:
    pass

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/tenders"
)
# Для локальной разработки без PostgreSQL — SQLite
USE_SQLITE = os.getenv("USE_SQLITE", "").lower() in ("1", "true", "yes")
if USE_SQLITE:
    DATABASE_URL = f"sqlite:///{Path(__file__).with_name('tenders.db')}"

connect_args: dict[str, object] = {}
if "sqlite" in DATABASE_URL:
    connect_args = {"check_same_thread": False}

engine_kwargs = {
    "connect_args": connect_args,
    "echo": False,
}
if "sqlite" in DATABASE_URL:
    engine_kwargs["poolclass"] = StaticPool
else:
    engine_kwargs["pool_pre_ping"] = True

engine = create_engine(DATABASE_URL, **engine_kwargs)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_db_session():
    """Для фоновых задач — генератор сессий."""
    yield from get_db()
