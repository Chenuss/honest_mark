"""
app/main.py — Точка входа приложения «Журнал инцидентов — Честный знак».

Запуск:
    uvicorn app.main:app --reload
"""

import time

from fastapi import Depends, Request
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware

# ── Логирование: инициализируем ДО всего остального ──
from app.logging_config import setup_logging, get_logger

setup_logging()
logger = get_logger("server")

from app.security import get_current_user_or_none, COOKIE_NAME
from app.database import get_db
from app.models import User
from app.routers import auth as auth_router
from app.routers import tickets as tickets_router
from app.routers import admin as admin_router
from app.routers import deviations as deviations_router
from app.routers import crpt_support as crpt_support_router


# ──────────────────────────────────────────────
# Request Logging Middleware
# ──────────────────────────────────────────────
class LogRequestMiddleware(BaseHTTPMiddleware):
    """Логирует все HTTP-запросы (кроме статики)."""

    SKIP_PREFIXES = ("/static/", "/favicon.ico")

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Пропускаем статику — иначе лог забьётся
        if any(path.startswith(p) for p in self.SKIP_PREFIXES):
            return await call_next(request)

        ip = request.client.host if request.client else "unknown"
        method = request.method
        start = time.perf_counter()

        response = await call_next(request)

        elapsed_ms = (time.perf_counter() - start) * 1000
        status_code = response.status_code

        msg = f"{method} {path} → {status_code} ({elapsed_ms:.0f}ms) [IP: {ip}]"

        if status_code >= 500:
            logger.error(msg)
        elif status_code >= 400:
            logger.warning(msg)
        else:
            logger.info(msg)

        return response


# ──────────────────────────────────────────────
# Создание приложения
# ──────────────────────────────────────────────
app = FastAPI(
    title="Честный знак — Журнал инцидентов",
    version="3.0.0",
    docs_url="/docs",
    redoc_url=None,
)

app.add_middleware(LogRequestMiddleware)

# ──────────────────────────────────────────────
# Статика и шаблоны
# ──────────────────────────────────────────────
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

# ──────────────────────────────────────────────
# Подключение роутеров
# ──────────────────────────────────────────────
app.include_router(auth_router.router)
app.include_router(tickets_router.router)
app.include_router(admin_router.router)
app.include_router(deviations_router.router)
app.include_router(crpt_support_router.router)

logger.info("Приложение запущено")


# ──────────────────────────────────────────────
# Фоновые задачи ТП ЧЗ: парсинг почты support@crpt.ru каждые 5 минут
# ──────────────────────────────────────────────
@app.on_event("startup")
def start_crpt_scheduler():
    from app import config as app_config

    if not app_config.CRPT_SCHEDULER_ENABLED:
        logger.info("ТП ЧЗ: планировщик отключён (CRPT_SCHEDULER_ENABLED=false)")
        return
    try:
        from app.services.crpt_email_service import ensure_crpt_tables, scheduler_factory

        # Гарантируем наличие таблиц ТП ЧЗ даже если миграция alembic
        # (a1crpt001 / b2crpt002) ещё не применялась к этой базе.
        ensure_crpt_tables()

        scheduler = scheduler_factory()
        scheduler.start()
        app.state.crpt_scheduler = scheduler
        logger.info(
            "ТП ЧЗ: планировщик запущен (парсинг IMAP каждые %d мин)",
            app_config.CRPT_EMAIL_PARSE_INTERVAL_MIN,
        )
    except Exception:
        logger.exception("ТП ЧЗ: не удалось запустить планировщик — раздел работает без автопарсинга")


@app.on_event("shutdown")
def stop_crpt_scheduler():
    scheduler = getattr(app.state, "crpt_scheduler", None)
    if scheduler is not None:
        scheduler.shutdown(wait=False)
        logger.info("ТП ЧЗ: планировщик остановлен")


# ──────────────────────────────────────────────
# Главная страница
# ──────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db)):
    user = get_current_user_or_none(request, db)
    if user is None:
        return RedirectResponse(url="/login", status_code=302)

    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "user": user},
    )
