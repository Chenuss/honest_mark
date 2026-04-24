"""
app/routers/admin.py — Административные маршруты v2.0.

Справочники: площадки, оборудование (доступ только admin).
"""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models import LineEquipment, ProductionSite, Ticket, User, UserRole
from app.security import get_current_user
from app.logging_config import get_logger

logger = get_logger("admin")

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory="app/templates")


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != UserRole.admin:
        raise HTTPException(status_code=403, detail="Доступ только для администраторов")
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
