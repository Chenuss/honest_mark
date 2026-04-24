"""
app/routers/auth.py — Маршруты авторизации с аудитом безопасности.
"""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.logging_config import get_logger
from app.security import (
    COOKIE_NAME,
    create_access_token,
    get_current_user_or_none,
    verify_password,
)
from app.database import get_db
from app.models import User

logger = get_logger("auth")

router = APIRouter(tags=["auth"])
templates = Jinja2Templates(directory="app/templates")


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user_or_none(request, db)
    if user is not None:
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    ip = request.client.host if request.client else "unknown"
    user = db.query(User).filter(User.username == username).first()

    if user is None or not verify_password(password, user.password_hash):
        logger.warning('Неудачная попытка входа: username="%s", IP=%s', username, ip)
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Неверный логин или пароль"},
            status_code=400,
        )

    logger.info('Вход: username="%s", role=%s, IP=%s', user.username, user.role.value, ip)

    token = create_access_token(
        user_id=user.id,
        username=user.username,
        role=user.role.value,
    )

    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=8 * 60 * 60,
    )
    return response


@router.get("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    user = get_current_user_or_none(request, db)
    if user:
        logger.info('Выход: username="%s"', user.username)

    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(key=COOKIE_NAME)
    return response
