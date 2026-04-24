"""
app/database.py — Конфигурация подключения к MySQL через SQLAlchemy.

Драйвер: PyMySQL (чистый Python, без проблем с компиляцией на Windows).
Настройки читаются из переменных окружения (.env-файл).
"""

import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

# ──────────────────────────────────────────────
# Загружаем переменные из .env (если файл есть)
# ──────────────────────────────────────────────
load_dotenv()

DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = os.getenv("DB_PORT", "3306")
DB_NAME = os.getenv("DB_NAME", "honest_mark")

DATABASE_URL = (
    f"mysql+pymysql://{DB_USER}:{DB_PASSWORD}"
    f"@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    "?charset=utf8mb4"
)

# ──────────────────────────────────────────────
# Engine + фабрика сессий
# ──────────────────────────────────────────────
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    echo=False,
)

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
)


# ──────────────────────────────────────────────
# Базовый класс для моделей (декларативный стиль)
# ──────────────────────────────────────────────
class Base(DeclarativeBase):
    """Базовый класс, от которого наследуются все ORM-модели."""
    pass


# ──────────────────────────────────────────────
# Dependency для FastAPI — получение сессии БД
# ──────────────────────────────────────────────
def get_db():
    """
    Генератор сессии для Depends().

    Использование в роутерах:
        @router.get("/")
        def index(db: Session = Depends(get_db)):
            ...
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
