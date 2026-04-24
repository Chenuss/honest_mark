"""
app/logging_config.py — Централизованная конфигурация логирования.

Формат: Время | Уровень | Модуль | Сообщение
Пример: 2026-03-15 14:30:05 | INFO    | auth             | Вход: username="admin", IP=127.0.0.1
"""

import logging
import sys


LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-16s | %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: int = logging.INFO) -> None:
    """
    Инициализирует логирование для всего приложения.
    Вызывать один раз в main.py до создания FastAPI().
    """
    root = logging.getLogger()
    root.setLevel(level)

    # Удаляем дефолтные хендлеры (uvicorn может добавить свои)
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT))
    root.addHandler(handler)

    # Подавляем шум от библиотек
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("watchfiles").setLevel(logging.WARNING)
    logging.getLogger("passlib").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Фабрика логгеров. Используй: logger = get_logger('auth')"""
    return logging.getLogger(name)
