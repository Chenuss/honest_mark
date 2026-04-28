"""
app/routers/tickets.py — Маршруты для работы с тикетами v2.0.
"""

import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.email_utils import send_expert_conclusion_email, send_management_notification
from app.models import (
    LineEquipment, ProductionSite, Ticket, TicketPhoto,
    TicketStatus, User, UserRole,
)
from app.security import get_current_user
from app.logging_config import get_logger

logger = get_logger("tickets")

router = APIRouter(prefix="/tickets", tags=["tickets"])
templates = Jinja2Templates(directory="app/templates")

UPLOAD_DIR = Path("app/static/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
DOC_EXTENSIONS = {".pdf", ".doc", ".docx", ".xls", ".xlsx"}
ALLOWED_EXTENSIONS = IMAGE_EXTENSIONS | DOC_EXTENSIONS
MAX_FILES_PER_TICKET = 10


def _load_ticket(db: Session, ticket_id: int) -> Ticket:
    ticket = (
        db.query(Ticket)
        .options(
            joinedload(Ticket.author),
            joinedload(Ticket.assignee),
            joinedload(Ticket.equipment).joinedload(LineEquipment.site),
            joinedload(Ticket.site),
            joinedload(Ticket.photos),
        )
        .filter(Ticket.id == ticket_id)
        .first()
    )
    if ticket is None:
        raise HTTPException(status_code=404, detail="Тикет не найден")
    return ticket


# ── GET /tickets ──
@router.get("", response_class=HTMLResponse)
def ticket_list(
    request: Request,
    status: str = Query(default="", alias="status"),
    equipment_id: int = Query(default=0, alias="equipment"),
    site_id: int = Query(default=0, alias="site"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = db.query(Ticket).options(
        joinedload(Ticket.author),
        joinedload(Ticket.assignee),
        joinedload(Ticket.equipment),
        joinedload(Ticket.site),
    )

    if current_user.role == UserRole.worker:
        query = query.filter(Ticket.author_id == current_user.id)

    if status:
        query = query.filter(Ticket.status == status)
    if equipment_id:
        query = query.filter(Ticket.equipment_id == equipment_id)
    if site_id:
        query = query.filter(Ticket.site_id == site_id)

    tickets = query.order_by(Ticket.created_at.desc()).all()
    all_equipment = db.query(LineEquipment).order_by(LineEquipment.name).all()
    all_statuses = [s.value for s in TicketStatus]
    all_sites = db.query(ProductionSite).order_by(ProductionSite.name).all()

    return templates.TemplateResponse("ticket_list.html", {
        "request": request, "user": current_user,
        "tickets": tickets, "all_equipment": all_equipment,
        "all_statuses": all_statuses, "all_sites": all_sites,
        "filter_status": status, "filter_equipment": equipment_id,
        "filter_site": site_id,
    })


# ── API: оборудование по площадке (для JS-фильтрации) ──
@router.get("/api/equipment", response_class=JSONResponse)
def api_equipment_by_site(
    site_id: int = Query(default=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Возвращает JSON-список оборудования, отфильтрованный по площадке."""
    q = db.query(LineEquipment).order_by(LineEquipment.name)
    if site_id:
        q = q.filter(LineEquipment.site_id == site_id)
    items = [{"id": e.id, "name": e.name} for e in q.all()]
    return JSONResponse(content=items)


# ── GET /tickets/create ──
@router.get("/create", response_class=HTMLResponse)
def ticket_create_form(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role == UserRole.engineer:
        raise HTTPException(status_code=403, detail="Инженеры не создают тикеты")

    sites = db.query(ProductionSite).order_by(ProductionSite.name).all()
    equipment_list = db.query(LineEquipment).order_by(LineEquipment.name).all()
    return templates.TemplateResponse("ticket_create.html", {
        "request": request, "user": current_user,
        "sites": sites, "equipment_list": equipment_list, "error": None,
    })


# ── POST /tickets/create ──
@router.post("/create", response_class=HTMLResponse)
async def ticket_create_submit(
    request: Request,
    equipment_id: int = Form(...),
    description: str = Form(...),
    site_id: int = Form(0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role == UserRole.engineer:
        raise HTTPException(status_code=403, detail="Инженеры не создают тикеты")

    description = description.strip()
    if not description:
        sites = db.query(ProductionSite).order_by(ProductionSite.name).all()
        equipment_list = db.query(LineEquipment).order_by(LineEquipment.name).all()
        return templates.TemplateResponse("ticket_create.html", {
            "request": request, "user": current_user,
            "sites": sites, "equipment_list": equipment_list,
            "error": "Описание проблемы не может быть пустым",
        }, status_code=400)

    equipment = db.query(LineEquipment).filter(LineEquipment.id == equipment_id).first()
    if equipment is None:
        raise HTTPException(status_code=404, detail="Оборудование не найдено")

    # site_id: либо из формы, либо из оборудования
    actual_site_id = site_id if site_id else (equipment.site_id if equipment.site_id else None)

    ticket = Ticket(
        author_id=current_user.id,
        equipment_id=equipment_id,
        site_id=actual_site_id,
        description=description,
        status=TicketStatus.open,
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)

    logger.info(
        "Тикет #%d создан пользователем %s (equipment_id=%d)",
        ticket.id, current_user.username, equipment_id,
    )

    # Отправка уведомления руководству
    site_name = ticket.site.name if ticket.site else "не указана"
    await send_management_notification(
        ticket_id=ticket.id,
        equipment_name=equipment.name,
        site_name=site_name,
        author_name=current_user.display_name,
        description=description,
        status=TicketStatus.open.value,
        photo_paths=None,  # При создании фото ещё нет
        is_creation=True,
    )

    return RedirectResponse(url=f"/tickets/{ticket.id}", status_code=302)


# ── GET /tickets/{id} ──
@router.get("/{ticket_id}", response_class=HTMLResponse)
def ticket_detail(
    request: Request,
    ticket_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ticket = _load_ticket(db, ticket_id)
    if current_user.role == UserRole.worker and ticket.author_id != current_user.id:
        raise HTTPException(status_code=403, detail="Нет доступа к этому тикету")

    return templates.TemplateResponse("ticket_detail.html", {
        "request": request, "user": current_user,
        "ticket": ticket, "error": None,
    })


# ── POST /tickets/{id}/take ──
@router.post("/{ticket_id}/take")
def ticket_take(
    ticket_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in (UserRole.engineer, UserRole.admin):
        raise HTTPException(status_code=403, detail="Только инженеры могут брать тикеты")

    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if ticket is None:
        raise HTTPException(status_code=404, detail="Тикет не найден")
    if ticket.status != TicketStatus.open:
        raise HTTPException(status_code=400, detail="Можно взять только открытый тикет")

    ticket.status = TicketStatus.in_progress
    ticket.assignee_id = current_user.id
    db.commit()
    return RedirectResponse(url=f"/tickets/{ticket_id}", status_code=302)


# ── POST /tickets/{id}/resolve ──
@router.post("/{ticket_id}/resolve", response_class=HTMLResponse)
def ticket_resolve(
    request: Request, ticket_id: int,
    resolution_note: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in (UserRole.engineer, UserRole.admin):
        raise HTTPException(status_code=403, detail="Только инженеры могут решать тикеты")

    ticket = _load_ticket(db, ticket_id)
    if ticket.status not in (TicketStatus.in_progress, TicketStatus.pending_dept):
        raise HTTPException(status_code=400, detail="Можно решить только тикет в работе или ожидании")

    resolution_note = resolution_note.strip()
    if not resolution_note:
        return templates.TemplateResponse("ticket_detail.html", {
            "request": request, "user": current_user,
            "ticket": ticket,
            "error": "Опишите, что было сделано для решения проблемы",
        }, status_code=400)

    ticket.status = TicketStatus.resolved
    ticket.resolution_note = resolution_note
    ticket.closed_at = datetime.now(timezone.utc)
    db.commit()
    return RedirectResponse(url=f"/tickets/{ticket_id}", status_code=302)


# ── POST /tickets/{id}/expert — заключение + email + pending_dept ──
@router.post("/{ticket_id}/expert", response_class=HTMLResponse)
async def ticket_expert(
    request: Request, ticket_id: int,
    expert_conclusion: str = Form(""),
    recommendations: str = Form(""),
    responsible_dept: str = Form(""),
    send_to_dept: int = Form(0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in (UserRole.engineer, UserRole.admin):
        raise HTTPException(status_code=403, detail="Недостаточно прав")

    ticket = _load_ticket(db, ticket_id)
    ticket.expert_conclusion = expert_conclusion.strip() or None
    ticket.recommendations = recommendations.strip() or None
    ticket.responsible_dept = responsible_dept.strip() or None

    # Если чекбокс «Отправить службе» отмечен — меняем статус и шлём email
    if send_to_dept and ticket.expert_conclusion:
        ticket.status = TicketStatus.pending_dept

        # Определяем email: из площадки
        support_email = ""
        if ticket.site and ticket.site.support_email:
            support_email = ticket.site.support_email

        if support_email:
            logger.info(
                "Заключение по тикету #%d отправляется на %s (пользователь: %s)",
                ticket.id, support_email, current_user.username,
            )
            await send_expert_conclusion_email(
                to_email=support_email,
                ticket_id=ticket.id,
                equipment_name=ticket.equipment.name if ticket.equipment else "—",
                site_name=ticket.site.name if ticket.site else "—",
                expert_name=current_user.display_name,
                conclusion=ticket.expert_conclusion or "",
                recommendations=ticket.recommendations or "",
                responsible_dept=ticket.responsible_dept or "",
            )
        else:
            site_name = ticket.site.name if ticket.site else "не указана"
            logger.warning(
                "Тикет #%d: support_email не заполнен (площадка: %s), отправка пропущена",
                ticket.id, site_name,
            )

    db.commit()
    return RedirectResponse(url=f"/tickets/{ticket_id}", status_code=302)


# ── POST /tickets/{id}/upload — загрузка файлов (фото + документы) ──
@router.post("/{ticket_id}/upload")
async def ticket_upload_files(
    ticket_id: int,
    photos: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in (UserRole.engineer, UserRole.admin):
        raise HTTPException(status_code=403, detail="Недостаточно прав")

    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if ticket is None:
        raise HTTPException(status_code=404, detail="Тикет не найден")

    existing_count = db.query(TicketPhoto).filter(TicketPhoto.ticket_id == ticket_id).count()
    if existing_count + len(photos) > MAX_FILES_PER_TICKET:
        return RedirectResponse(url=f"/tickets/{ticket_id}", status_code=302)

    uploaded_photo_paths = []
    
    for photo in photos:
        ext = Path(photo.filename or "file.bin").suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            continue

        safe_name = f"{ticket_id}_{uuid.uuid4().hex[:8]}{ext}"
        file_path = UPLOAD_DIR / safe_name

        content = await photo.read()
        with open(file_path, "wb") as f:
            f.write(content)

        is_image = 1 if ext in IMAGE_EXTENSIONS else 0
        db.add(TicketPhoto(
            ticket_id=ticket_id,
            filename=safe_name,
            original_name=photo.filename or "file",
            is_image=is_image,
        ))
        
        # Сохраняем путь к изображению для уведомления
        if is_image:
            uploaded_photo_paths.append(str(file_path))

        size_kb = len(content) / 1024
        logger.info(
            "Файл загружен к тикету #%d: %s (%.0f KB) пользователем %s",
            ticket_id, photo.filename or "file", size_kb, current_user.username,
        )

    db.commit()
    
    # Если загружены изображения — отправляем уведомление руководству
    if uploaded_photo_paths:
        site_name = ticket.site.name if ticket.site else "не указана"
        equipment_name = ticket.equipment.name if ticket.equipment else "—"
        await send_management_notification(
            ticket_id=ticket.id,
            equipment_name=equipment_name,
            site_name=site_name,
            author_name=current_user.display_name,
            description=ticket.description,
            status=ticket.status.value,
            photo_paths=uploaded_photo_paths,
            is_creation=False,
        )
    
    return RedirectResponse(url=f"/tickets/{ticket_id}", status_code=302)


# ── POST /tickets/{id}/upload-clipboard — вставка из буфера (Ctrl+V) ──
@router.post("/{ticket_id}/upload-clipboard")
async def ticket_upload_clipboard(
    ticket_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Принимает одно изображение из буфера обмена (JS fetch)."""
    if current_user.role not in (UserRole.engineer, UserRole.admin):
        return JSONResponse({"ok": False, "error": "Нет прав"}, status_code=403)

    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if ticket is None:
        return JSONResponse({"ok": False, "error": "Тикет не найден"}, status_code=404)

    existing_count = db.query(TicketPhoto).filter(TicketPhoto.ticket_id == ticket_id).count()
    if existing_count >= MAX_FILES_PER_TICKET:
        return JSONResponse({"ok": False, "error": "Лимит файлов"}, status_code=400)

    # Имя в формате ЧЧ.ММ.СС_ДД.ММ.ГГ.png
    now = datetime.now()
    time_name = now.strftime("%H.%M.%S_%d.%m.%y")
    ext = ".png"
    safe_name = f"{ticket_id}_{time_name}{ext}"
    original_name = f"{time_name}{ext}"

    content = await file.read()
    file_path = UPLOAD_DIR / safe_name
    with open(file_path, "wb") as f:
        f.write(content)

    db.add(TicketPhoto(
        ticket_id=ticket_id,
        filename=safe_name,
        original_name=original_name,
        is_image=1,
    ))
    db.commit()

    size_kb = len(content) / 1024
    logger.info(
        "Clipboard-вставка к тикету #%d: %s (%.0f KB) пользователем %s",
        ticket_id, original_name, size_kb, current_user.username,
    )

    # Отправка уведомления руководству о новом изображении
    site_name = ticket.site.name if ticket.site else "не указана"
    equipment_name = ticket.equipment.name if ticket.equipment else "—"
    await send_management_notification(
        ticket_id=ticket.id,
        equipment_name=equipment_name,
        site_name=site_name,
        author_name=current_user.display_name,
        description=ticket.description,
        status=ticket.status.value,
        photo_paths=[str(file_path)],
        is_creation=False,
    )

    return JSONResponse({"ok": True, "filename": safe_name, "original_name": original_name})


# ── POST /tickets/{id}/photo/{photo_id}/delete ──
@router.post("/{ticket_id}/photo/{photo_id}/delete")
def ticket_delete_photo(
    ticket_id: int, photo_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in (UserRole.engineer, UserRole.admin):
        raise HTTPException(status_code=403, detail="Недостаточно прав")

    photo = db.query(TicketPhoto).filter(
        TicketPhoto.id == photo_id, TicketPhoto.ticket_id == ticket_id
    ).first()
    if photo is None:
        raise HTTPException(status_code=404, detail="Файл не найден")

    file_path = UPLOAD_DIR / photo.filename
    if file_path.exists():
        file_path.unlink()

    db.delete(photo)
    db.commit()
    return RedirectResponse(url=f"/tickets/{ticket_id}", status_code=302)


# ── POST /tickets/{id}/delete — удаление тикета (только admin) ──
@router.post("/{ticket_id}/delete")
def ticket_delete(
    ticket_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != UserRole.admin:
        raise HTTPException(status_code=403, detail="Удалять тикеты может только администратор")

    ticket = _load_ticket(db, ticket_id)

    # Удаляем файлы с диска
    files_count = len(ticket.photos)
    for photo in ticket.photos:
        file_path = UPLOAD_DIR / photo.filename
        if file_path.exists():
            file_path.unlink()

    db.delete(ticket)
    db.commit()

    logger.warning(
        "Тикет #%d УДАЛЁН администратором %s (файлов удалено: %d)",
        ticket_id, current_user.username, files_count,
    )

    return RedirectResponse(url="/tickets", status_code=302)


# ── GET /tickets/{id}/print ──
@router.get("/{ticket_id}/print", response_class=HTMLResponse)
def ticket_print(
    request: Request, ticket_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in (UserRole.engineer, UserRole.admin):
        raise HTTPException(status_code=403, detail="Недостаточно прав")

    ticket = _load_ticket(db, ticket_id)
    return templates.TemplateResponse("ticket_print.html", {
        "request": request, "ticket": ticket, "user": current_user,
    })
