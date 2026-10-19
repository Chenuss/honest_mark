"""
app/routers/crpt_support.py — Раздел «Работа с ТП Честного Знака» (ТП ЧЗ).

Маршруты:
  GET  /crpt-support                    — список кейсов + фильтры
  GET  /crpt-support/<sr>               — детали кейса, лента переписки
  GET  /crpt-support/create             — форма нового обращения
  POST /crpt-support/create             — создание (отправка письма в ЦРПТ)
  POST /crpt-support/<sr>/clarification — отправка уточнения (снимает паузу)
  POST /crpt-support/<sr>/resume        — возобновить отсчёт вручную
  POST /api/crpt/webhook/email          — ручной тест парсинга (.eml в теле)
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.logging_config import get_logger
from app.models import CrptCaseStatus, CrptEmail, CrptSupportCase, User
from app.security import get_current_user
from app.services import crpt_email_service
from app.utils.work_hours import (
    get_auto_close_remaining,
    get_remaining_work_hours,
    is_overdue,
    now_msk,
    progress_percent,
)

logger = get_logger("crpt_support")

router = APIRouter(tags=["crpt-support"])
templates = Jinja2Templates(directory="app/templates")

UPLOAD_DIR = Path("app/static/uploads/crpt")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


# ──────────────────────────────────────────────
# Вспомогательные функции
# ──────────────────────────────────────────────
def _case_viewmodel(case: CrptSupportCase) -> dict:
    """Данные кейса + рассчитанные регламенты для шаблона."""
    remaining = get_remaining_work_hours(case)
    case.remaining_work_hours = remaining
    return {
        "case": case,
        "remaining_hours": remaining,
        "progress": progress_percent(case),
        "auto_close_remaining_h": get_auto_close_remaining(case),
        "overdue": is_overdue(case),
        "attachments": _load_attachments(case),
    }


def _load_attachments(case: CrptSupportCase) -> list[dict]:
    try:
        return json.loads(case.attachments_json or "[]")
    except (TypeError, ValueError):
        return []


def _get_case_by_sr(db: Session, sr_number: str) -> CrptSupportCase:
    case = (
        db.query(CrptSupportCase)
        .options(joinedload(CrptSupportCase.emails), joinedload(CrptSupportCase.created_by))
        .filter(CrptSupportCase.sr_number == sr_number.upper())
        .first()
    )
    if case is None:
        raise HTTPException(status_code=404, detail=f"Кейс {sr_number} не найден")
    return case


async def _save_uploads(files: list[UploadFile]) -> list:
    """Сохранить загрузки на диск, вернуть список путей для вложений письма."""
    saved = []
    for f in files or []:
        if not f.filename:
            continue
        dest = UPLOAD_DIR / f.filename
        dest.write_bytes(await f.read())
        saved.append(dest)
    return saved


# ──────────────────────────────────────────────
# GET /crpt-support — список
# ──────────────────────────────────────────────
@router.get("/crpt-support", response_class=HTMLResponse)
def crpt_case_list(
    request: Request,
    status: str = Query(default="", description="Фильтр по статусу"),
    overdue: str = Query(default="", description="1 — только просроченные"),
    needs_action: str = Query(default="", description="1 — требуют уточнения"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = db.query(CrptSupportCase)
    if status:
        query = query.filter(CrptSupportCase.status == status)

    cases = query.order_by(CrptSupportCase.created_at.desc()).all()

    rows = []
    attention = []  # «Требуют действия»: автозакрытие < 24ч либо просрочка
    for case in cases:
        vm = _case_viewmodel(case)
        ac = vm["auto_close_remaining_h"]
        if vm["overdue"]:
            vm["status_display"] = "OVERDUE"
        if ac is not None and ac < 24:
            attention.append(vm)
        elif vm["overdue"]:
            attention.append(vm)
        if overdue == "1" and not vm["overdue"]:
            continue
        if needs_action == "1" and case.status != CrptCaseStatus.WAITING_CLARIFICATION:
            continue
        rows.append(vm)

    db.commit()  # сохраняем пересчитанный remaining_work_hours

    return templates.TemplateResponse("crpt_support/list.html", {
        "request": request,
        "user": current_user,
        "rows": rows,
        "attention": attention,
        "all_statuses": [s.value for s in CrptCaseStatus],
        "filter_status": status,
        "filter_overdue": overdue,
        "filter_needs_action": needs_action,
    })


# ──────────────────────────────────────────────
# GET /crpt-support/create — форма
# ──────────────────────────────────────────────
@router.get("/crpt-support/create", response_class=HTMLResponse)
def crpt_case_create_form(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    from app import config
    return templates.TemplateResponse("crpt_support/create.html", {
        "request": request,
        "user": current_user,
        "enterprise_defaults": config.ENTERPRISE_DEFAULTS,
    })


# ──────────────────────────────────────────────
# POST /crpt-support/create — создание кейса + отправка письма
# ──────────────────────────────────────────────
@router.post("/crpt-support/create")
async def crpt_case_create(
    subject: str = Form(...),
    body: str = Form(...),
    enterprise: str = Form(default=""),
    inn: str = Form(default=""),
    gtin: str = Form(default=""),
    error_code: str = Form(default=""),
    doc_id: str = Form(default=""),
    api_error_object: str = Form(default=""),
    related_internal_ticket_id: str = Form(default=""),
    files: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    saved_files = await _save_uploads(files)
    rel_id = int(related_internal_ticket_id) if related_internal_ticket_id.strip().isdigit() else None

    try:
        case = crpt_email_service.create_crpt_ticket(
            db=db,
            subject=subject.strip(),
            body=body.strip(),
            enterprise=enterprise.strip() or None,
            inn=inn.strip() or None,
            files=saved_files,
            gtin=gtin.strip() or None,
            error_code=error_code.strip() or None,
            doc_id=doc_id.strip() or None,
            api_error_object=api_error_object.strip() or None,
            related_internal_ticket_id=rel_id,
            created_by_id=current_user.id,
        )
    except Exception as exc:
        logger.exception("Не удалось создать кейс ТП ЧЗ")
        raise HTTPException(status_code=500, detail=f"Ошибка отправки письма в ТП ЦРПТ: {exc}")

    # До присвоения SR номера ссылка ведёт на create-form с уведомлением
    return RedirectResponse(url="/crpt-support?created=1", status_code=303)


# ──────────────────────────────────────────────
# GET /crpt-support/<sr> — детали + лента переписки
# ──────────────────────────────────────────────
@router.get("/crpt-support/{sr_number}", response_class=HTMLResponse)
def crpt_case_detail(
    sr_number: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    case = _get_case_by_sr(db, sr_number)
    vm = _case_viewmodel(case)
    db.commit()

    timeline: list[CrptEmail] = sorted(case.emails, key=lambda e: e.received_at)

    return templates.TemplateResponse("crpt_support/detail.html", {
        "request": request,
        "user": current_user,
        **vm,
        "timeline": timeline,
    })


# ──────────────────────────────────────────────
# POST /crpt-support/<sr>/clarification — отправить уточнение
# ──────────────────────────────────────────────
@router.post("/crpt-support/{sr_number}/clarification")
async def crpt_case_clarification(
    sr_number: str,
    body: str = Form(...),
    url: str = Form(default=""),
    api_request: str = Form(default=""),
    files: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Отправляет уточняющую информацию в ТП ЦРПТ и снимает паузу таймера:
      WAITING_CLARIFICATION -> IN_PROGRESS, resumed_at = now.
    В письме указываем URL запроса, тело API-запроса, прикладываем скриншоты.
    """
    case = _get_case_by_sr(db, sr_number)

    parts = [f"По обращению {case.sr_number} направляю уточняющую информацию:\n"]
    if url.strip():
        parts.append(f"URL запроса: {url.strip()}\n")
    if api_request.strip():
        parts.append(f"Тело API-запроса:\n{api_request.strip()}\n")
    parts.append(body.strip())
    full_body = "".join(parts)

    last_in = (
        db.query(CrptEmail)
        .filter(CrptEmail.case_id == case.id, CrptEmail.direction == CrptEmail.DIRECTION_IN)
        .order_by(CrptEmail.received_at.desc())
        .first()
    )

    saved_files = await _save_uploads(files)
    result = crpt_email_service.send_crpt_email(
        subject=f"{case.sr_number}. Уточняющая информация ({case.enterprise_name})",
        body=full_body,
        files=saved_files,
        in_reply_to=(last_in.message_id or case.email_thread_id) if last_in else case.email_thread_id,
    )

    out_mail = CrptEmail(
        case_id=case.id,
        direction=CrptEmail.DIRECTION_OUT,
        message_id=result.get("message_id") or None,
        from_addr=case_enterprise_email(),
        to_addr="support@crpt.ru",
        subject=f"{case.sr_number}. Уточняющая информация",
        body_text=full_body,
        event_type="clarification_sent",
        received_at=now_msk(),
    )
    db.add(out_mail)

    # Снимаем паузу
    if case.status == CrptCaseStatus.WAITING_CLARIFICATION:
        case.status = CrptCaseStatus.IN_PROGRESS
        case.resumed_at = now_msk()
        case.paused_at = None
        # Продлеваем дедлайн на время паузы (в рабочих часах оно не тикало)
    db.commit()

    logger.info("Кейс %s: отправлено уточнение, пауза снята", case.sr_number)
    return RedirectResponse(url=f"/crpt-support/{case.sr_number}?clarified=1", status_code=303)


def case_enterprise_email() -> str:
    from app import config
    return config.MARKIROVKA_EMAIL


# ──────────────────────────────────────────────
# POST /crpt-support/<sr>/resume — возобновить вручную
# ──────────────────────────────────────────────
@router.post("/crpt-support/{sr_number}/resume")
def crpt_case_resume(
    sr_number: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    case = _get_case_by_sr(db, sr_number)
    if case.status not in (CrptCaseStatus.WAITING_CLARIFICATION, CrptCaseStatus.OVERDUE):
        raise HTTPException(status_code=400, detail="Кейс не приостановлен")
    case.status = CrptCaseStatus.IN_PROGRESS
    case.resumed_at = now_msk()
    case.paused_at = None
    db.commit()
    return RedirectResponse(url=f"/crpt-support/{case.sr_number}", status_code=303)


# ──────────────────────────────────────────────
# POST /api/crpt/webhook/email — ручной тест парсинга
# ──────────────────────────────────────────────
@router.post("/api/crpt/webhook/email", response_class=JSONResponse)
async def crpt_webhook_email(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Принимает сырое письмо (.eml) как body и прогоняет через парсер ТП ЧЗ.
    Использование: curl -u ... --data-binary @sample.eml localhost:8000/api/crpt/webhook/email
    """
    raw = await request.body()
    if not raw:
        raise HTTPException(status_code=400, detail="Пустое тело запроса (ожидается .eml)")
    try:
        outcome = crpt_email_service.parse_raw_email_for_test(db, raw)
    except Exception as exc:
        logger.exception("Webhook парсинга письма")
        raise HTTPException(status_code=500, detail=str(exc))
    return JSONResponse(content={"ok": True, **outcome})


# ──────────────────────────────────────────────
# POST /api/crpt/parse-inbox — запустить IMAP-обход вручную
# ──────────────────────────────────────────────
@router.post("/api/crpt/parse-inbox", response_class=JSONResponse)
def crpt_parse_inbox_now(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    count = crpt_email_service.fetch_and_process_inbox(db)
    return JSONResponse(content={"ok": True, "processed": count})
