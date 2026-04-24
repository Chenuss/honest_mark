"""
app/security.py — Хэширование паролей, JWT-токены, dependency для получения текущего пользователя.

Токен хранится в httponly-cookie (не в localStorage) —
это безопаснее для внутризаводской системы, где нет SPA.
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from dotenv import load_dotenv
from fastapi import Depends, HTTPException, Request, status
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User

from app.logging_config import get_logger

logger = get_logger("security")

load_dotenv()

# ──────────────────────────────────────────────
# Настройки
# ──────────────────────────────────────────────
SECRET_KEY: str = os.getenv("SECRET_KEY", "change-me-to-random-string")
ALGORITHM: str = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES: int = 480  # 8 часов — одна рабочая смена

COOKIE_NAME: str = "access_token"

# ──────────────────────────────────────────────
# Хэширование паролей (bcrypt 3.2.0)
# ──────────────────────────────────────────────
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain_password: str) -> str:
    """Возвращает bcrypt-хэш пароля."""
    return pwd_context.hash(plain_password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Проверяет пароль по хэшу. Возвращает True если совпадает."""
    return pwd_context.verify(plain_password, hashed_password)


# ──────────────────────────────────────────────
# JWT-токены
# ──────────────────────────────────────────────
def create_access_token(user_id: int, username: str, role: str) -> str:
    """
    Создаёт JWT-токен с полезной нагрузкой:
      - sub: id пользователя (строка)
      - username: логин
      - role: роль (worker / engineer / admin)
      - exp: время истечения
    """
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "exp": expire,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> Optional[dict]:
    """
    Декодирует JWT-токен. Возвращает payload-словарь или None,
    если токен невалиден / просрочен.
    """
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError as exc:
        logger.warning("Невалидный JWT-токен: %s", type(exc).__name__)
        return None


# ──────────────────────────────────────────────
# FastAPI Dependency: получение текущего пользователя
# ──────────────────────────────────────────────
def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """
    Извлекает JWT из cookie, проверяет его и возвращает объект User.
    Если токен отсутствует или невалиден — кидает 401.
    """
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Не авторизован",
        )

    payload = decode_access_token(token)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Токен недействителен или просрочен",
        )

    user_id = payload.get("sub")
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Некорректный токен",
        )

    user = db.query(User).filter(User.id == int(user_id)).first()
    if user is None:
        logger.warning("Токен с user_id=%s — пользователь не существует", user_id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Пользователь не найден",
        )

    return user


def get_current_user_or_none(request: Request, db: Session = Depends(get_db)) -> Optional[User]:
    """
    Мягкая версия: возвращает User или None (без исключения).
    Полезна для страниц, которые работают и без авторизации.
    """
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None

    payload = decode_access_token(token)
    if payload is None:
        return None

    user_id = payload.get("sub")
    if user_id is None:
        return None

    return db.query(User).filter(User.id == int(user_id)).first()
