"""
app/routers/admin.py — Административные маршруты v2.0.

Справочники: площадки, оборудование, пользователи (доступ только admin).
"""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models import LineEquipment, ProductionSite, Ticket, User, UserRole
from app.security import get_current_user, hash_password, verify_password
from app.logging_config import get_logger

logger = get_logger("admin")

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory="app/templates")


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != UserRole.admin:
        raise HTTPException(status_code=403, detail="Доступ только для администраторов")
    if not current_user.is_active:
        raise HTTPException(status_code=403, detail="Аккаунт деактивирован")
    return current_user


# ══════════════════════════════════════════════
#  ПЛОЩАДКИ (ProductionSite)
# ══════════════════════════════════════════════

@router.get("/sites", response_class=HTMLResponse)
def sites_list(
    request: Request, success: str = "", error: str = "",
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    sites = db.query(ProductionSite).order_by(ProductionSite.name).all()
    return templates.TemplateResponse("admin_sites.html", {
        "request": request, "user": current_user,
        "sites": sites, "success": success, "error": error,
    })


@router.post("/sites/add")
def sites_add(
    name: str = Form(...), support_email: str = Form(""),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    name = name.strip()
    if not name:
        return RedirectResponse(url="/admin/sites?error=Название+пустое", status_code=302)
    if db.query(ProductionSite).filter(ProductionSite.name == name).first():
        return RedirectResponse(url="/admin/sites?error=Площадка+уже+существует", status_code=302)

    db.add(ProductionSite(name=name, support_email=support_email.strip() or None))
    db.commit()
    logger.info('Площадка "%s" создана (%s)', name, current_user.username)
    return RedirectResponse(url="/admin/sites?success=Площадка+добавлена", status_code=302)


@router.get("/sites/edit/{site_id}", response_class=HTMLResponse)
def sites_edit_form(
    request: Request, site_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    site = db.query(ProductionSite).filter(ProductionSite.id == site_id).first()
    if site is None:
        raise HTTPException(status_code=404, detail="Площадка не найдена")
    return templates.TemplateResponse("admin_sites_edit.html", {
        "request": request, "user": current_user, "site": site, "error": None,
    })


@router.post("/sites/edit/{site_id}", response_class=HTMLResponse)
def sites_edit_submit(
    request: Request, site_id: int,
    name: str = Form(...), support_email: str = Form(""),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    site = db.query(ProductionSite).filter(ProductionSite.id == site_id).first()
    if site is None:
        raise HTTPException(status_code=404, detail="Площадка не найдена")
    name = name.strip()
    if not name:
        return templates.TemplateResponse("admin_sites_edit.html", {
            "request": request, "user": current_user, "site": site,
            "error": "Название не может быть пустым",
        }, status_code=400)
    dup = db.query(ProductionSite).filter(ProductionSite.name == name, ProductionSite.id != site_id).first()
    if dup:
        return templates.TemplateResponse("admin_sites_edit.html", {
            "request": request, "user": current_user, "site": site,
            "error": "Площадка с таким названием уже существует",
        }, status_code=400)

    site.name = name
    site.support_email = support_email.strip() or None
    db.commit()
    return RedirectResponse(url="/admin/sites?success=Площадка+обновлена", status_code=302)


@router.post("/sites/delete/{site_id}")
def sites_delete(
    site_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    site = db.query(ProductionSite).filter(ProductionSite.id == site_id).first()
    if site is None:
        raise HTTPException(status_code=404, detail="Площадка не найдена")
    eq_count = db.query(LineEquipment).filter(LineEquipment.site_id == site_id).count()
    if eq_count > 0:
        return RedirectResponse(
            url=f"/admin/sites?error=Нельзя+удалить:+привязано+{eq_count}+оборудование",
            status_code=302)
    db.delete(site)
    db.commit()
    logger.warning('Площадка #%d "%s" УДАЛЕНА (%s)', site_id, site.name, current_user.username)
    return RedirectResponse(url="/admin/sites?success=Площадка+удалена", status_code=302)


# ══════════════════════════════════════════════
#  ОБОРУДОВАНИЕ (LineEquipment)
# ══════════════════════════════════════════════

@router.get("/equipment", response_class=HTMLResponse)
def equipment_list(
    request: Request, success: str = "", error: str = "",
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    equipment_rows = (
        db.query(LineEquipment)
        .options(joinedload(LineEquipment.site))
        .order_by(LineEquipment.name).all()
    )
    equipment_with_counts = []
    for eq in equipment_rows:
        tc = db.query(Ticket).filter(Ticket.equipment_id == eq.id).count()
        equipment_with_counts.append({
            "id": eq.id, "name": eq.name,
            "site_name": eq.site.name if eq.site else "—",
            "ticket_count": tc,
        })

    sites = db.query(ProductionSite).order_by(ProductionSite.name).all()

    return templates.TemplateResponse("admin_equipment.html", {
        "request": request, "user": current_user,
        "equipment_list": equipment_with_counts,
        "sites": sites,
        "success": success, "error": error,
    })


@router.post("/equipment/add")
def equipment_add(
    name: str = Form(...), site_id: int = Form(0),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    name = name.strip()
    if not name:
        return RedirectResponse(url="/admin/equipment?error=Название+пустое", status_code=302)

    db.add(LineEquipment(name=name, site_id=site_id if site_id else None))
    db.commit()
    logger.info('Оборудование "%s" добавлено (site_id=%s, %s)', name, site_id or "—", current_user.username)
    return RedirectResponse(url="/admin/equipment?success=Оборудование+добавлено", status_code=302)


@router.post("/equipment/delete/{equipment_id}")
def equipment_delete(
    equipment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    eq = db.query(LineEquipment).filter(LineEquipment.id == equipment_id).first()
    if eq is None:
        raise HTTPException(status_code=404, detail="Оборудование не найдено")
    tc = db.query(Ticket).filter(Ticket.equipment_id == equipment_id).count()
    if tc > 0:
        return RedirectResponse(
            url=f"/admin/equipment?error=Нельзя+удалить:+привязано+{tc}+тикет(ов)",
            status_code=302)
    db.delete(eq)
    db.commit()
    logger.warning('Оборудование #%d "%s" УДАЛЕНО (%s)', equipment_id, eq.name, current_user.username)
    return RedirectResponse(url="/admin/equipment?success=Оборудование+удалено", status_code=302)


@router.get("/equipment/edit/{equipment_id}", response_class=HTMLResponse)
def equipment_edit_form(
    request: Request, equipment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    eq = db.query(LineEquipment).filter(LineEquipment.id == equipment_id).first()
    if eq is None:
        raise HTTPException(status_code=404, detail="Оборудование не найдено")
    sites = db.query(ProductionSite).order_by(ProductionSite.name).all()
    return templates.TemplateResponse("admin_equipment_edit.html", {
        "request": request, "user": current_user,
        "equipment": eq, "sites": sites, "error": None,
    })


@router.post("/equipment/edit/{equipment_id}", response_class=HTMLResponse)
def equipment_edit_submit(
    request: Request, equipment_id: int,
    name: str = Form(...), site_id: int = Form(0),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    eq = db.query(LineEquipment).filter(LineEquipment.id == equipment_id).first()
    if eq is None:
        raise HTTPException(status_code=404, detail="Оборудование не найдено")
    name = name.strip()
    if not name:
        sites = db.query(ProductionSite).order_by(ProductionSite.name).all()
        return templates.TemplateResponse("admin_equipment_edit.html", {
            "request": request, "user": current_user,
            "equipment": eq, "sites": sites, "error": "Название пустое",
        }, status_code=400)

    eq.name = name
    eq.site_id = site_id if site_id else None
    db.commit()
    return RedirectResponse(url="/admin/equipment?success=Обновлено", status_code=302)


# ══════════════════════════════════════════════
#  ПОЛЬЗОВАТЕЛИ (User Management)
# ══════════════════════════════════════════════

@router.get("/users", response_class=HTMLResponse)
def users_list(
    request: Request, success: str = "", error: str = "",
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    users = db.query(User).order_by(User.username).all()
    return templates.TemplateResponse("admin_users.html", {
        "request": request, "user": current_user,
        "users": users, "success": success, "error": error,
    })


@router.post("/users/add")
def users_add(
    username: str = Form(...), full_name: str = Form(""),
    role: str = Form(...), password: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    username = username.strip()
    if not username:
        return RedirectResponse(url="/admin/users?error=Логин+пустой", status_code=302)
    if db.query(User).filter(User.username == username).first():
        return RedirectResponse(url="/admin/users?error=Пользователь+уже+существует", status_code=302)
    if not password:
        return RedirectResponse(url="/admin/users?error=Пароль+обязателен", status_code=302)
    if role not in ("worker", "engineer", "admin"):
        return RedirectResponse(url="/admin/users?error=Неверная+роль", status_code=302)

    db.add(User(
        username=username,
        password_hash=hash_password(password),
        full_name=full_name.strip() or None,
        role=UserRole(role),
        is_active=1,
    ))
    db.commit()
    logger.info('Пользователь "%s" создан (%s)', username, current_user.username)
    return RedirectResponse(url="/admin/users?success=Пользователь+добавлен", status_code=302)


@router.get("/users/edit/{user_id}", response_class=HTMLResponse)
def users_edit_form(
    request: Request, user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    u = db.query(User).filter(User.id == user_id).first()
    if u is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    return templates.TemplateResponse("admin_users_edit.html", {
        "request": request, "user": current_user, "edit_user": u, "error": None,
    })


@router.post("/users/edit/{user_id}", response_class=HTMLResponse)
def users_edit_submit(
    request: Request, user_id: int,
    full_name: str = Form(""), role: str = Form(...),
    new_password: str = Form(""),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    u = db.query(User).filter(User.id == user_id).first()
    if u is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    if role not in ("worker", "engineer", "admin"):
        return templates.TemplateResponse("admin_users_edit.html", {
            "request": request, "user": current_user, "edit_user": u,
            "error": "Неверная роль",
        }, status_code=400)

    u.full_name = full_name.strip() or None
    u.role = UserRole(role)
    if new_password.strip():
        u.password_hash = hash_password(new_password)
    db.commit()
    return RedirectResponse(url="/admin/users?success=Пользователь+обновлен", status_code=302)


@router.post("/users/toggle/{user_id}")
def users_toggle_active(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    u = db.query(User).filter(User.id == user_id).first()
    if u is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    if u.id == current_user.id:
        return RedirectResponse(url="/admin/users?error=Нельзя+деактивировать+себя", status_code=302)
    u.is_active = 0 if u.is_active else 1
    db.commit()
    status_text = "активирован" if u.is_active else "деактивирован"
    logger.info('Пользователь "%s" %s (%s)', u.username, status_text, current_user.username)
    return RedirectResponse(url="/admin/users?success=Пользователь+" + status_text, status_code=302)
