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

logger.info("Приложение запущено")


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
