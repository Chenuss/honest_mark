"""
app/routers/deviations.py — Аналитика отклонений ЧЗ v3.

Маршруты (авторизованные):
  - GET  /deviations/upload              — загрузка CSV
  - POST /deviations/upload              — парсинг CSV
  - GET  /deviations/dashboard           — архив: Год → Месяц
  - GET  /deviations/dashboard?year=&month= — дашборд за конкретный месяц
  - POST /deviations/batches/{id}/delete — удаление пакета
  - GET  /deviations/products            — справочник продукции
  - GET  /deviations/products/{id}/edit  — редактирование продукта
  - POST /deviations/products/{id}/edit  — сохранение

Публичные (без авторизации):
  - GET  /analytics/{year}/{month}       — публичный дашборд за месяц
"""

import io
import json
import re
from datetime import datetime
from typing import Optional

import pandas as pd
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, extract, func as sa_func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import CzDeviation, CzUploadBatch, Product, User, UserRole
from app.security import get_current_user
from app.logging_config import get_logger

logger = get_logger("deviations")

router = APIRouter(tags=["deviations"])
templates = Jinja2Templates(directory="app/templates")

MONTH_NAMES = {
    1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
    5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
    9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь",
}

PALETTE = [
    "#6C9BCF", "#E8A87C", "#85CDCA", "#D5A6BD",
    "#C9CBA3", "#FFE1A8", "#A2D2FF", "#FF9F9F",
    "#B5E48C", "#DDA0DD", "#F0E68C", "#87CEEB",
]

COLUMN_MAP = {
    "вид отклонения": "deviation_type", "тип отклонения": "deviation_type",
    "адрес места фиксации отклонения": "address", "адрес": "address",
    "адрес места фиксации": "address",
    "дата и время регистрации отклонения": "registration_date",
    "дата регистрации": "registration_date", "дата и время регистрации": "registration_date",
    "gtin": "gtin",
    "муниципальный округ": "city", "муниципальное образование": "city", "город": "city",
    "наименование товара": "product_name", "товар": "product_name",
    "инн": "inn", "инн участника": "inn",
    "наименование участника": "org_name", "наименование организации": "org_name",
}


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    rename = {}
    for col in df.columns:
        key = col.strip().lower()
        if key in COLUMN_MAP:
            rename[col] = COLUMN_MAP[key]
    return df.rename(columns=rename)


def _clean_address(address: str | None) -> str:
    if not address or not isinstance(address, str):
        return ""
    addr = address.strip()
    addr = re.split(r',?\s*(?:кв\.|пом\.|оф\.|этаж|лит\.|комн\.)', addr, flags=re.IGNORECASE)[0]
    addr = re.sub(r'^\d{6}\s*,?\s*', '', addr)
    return addr.strip().rstrip(',').strip()


def _parse_date(val) -> datetime | None:
    if pd.isna(val) or val is None:
        return None
    s = str(val).strip()
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _gtin_display_map(db: Session, gtins: list[str]) -> dict[str, str]:
    """Строит маппинг GTIN → название продукта из справочника."""
    if not gtins:
        return {}
    products = db.query(Product).filter(Product.gtin.in_(gtins)).all()
    return {p.gtin: p.name or p.gtin for p in products}


def _build_chart_data(
    db: Session, q, gtin_map: dict[str, str],
) -> tuple[dict, tuple, tuple, list]:
    """Общая логика агрегации для дашборда (используется и в private и в public)."""

    type_stats = (
        q.with_entities(CzDeviation.deviation_type, sa_func.count())
        .group_by(CzDeviation.deviation_type).order_by(sa_func.count().desc()).all()
    )
    city_stats = (
        q.with_entities(CzDeviation.city, sa_func.count())
        .filter(CzDeviation.city.isnot(None), CzDeviation.city != "")
        .group_by(CzDeviation.city).order_by(sa_func.count().desc()).limit(12).all()
    )
    address_stats = (
        q.with_entities(CzDeviation.address_clean, sa_func.count())
        .filter(CzDeviation.address_clean.isnot(None), CzDeviation.address_clean != "")
        .group_by(CzDeviation.address_clean).order_by(sa_func.count().desc()).limit(10).all()
    )
    gtin_stats = (
        q.with_entities(CzDeviation.gtin, sa_func.count())
        .filter(CzDeviation.gtin.isnot(None), CzDeviation.gtin != "")
        .group_by(CzDeviation.gtin).order_by(sa_func.count().desc()).limit(10).all()
    )

    # Подменяем GTIN на название
    gtin_labels = []
    gtin_raw = []
    for g, cnt in gtin_stats:
        gtin_raw.append(g)
        gtin_labels.append(gtin_map.get(g, g) if g else "—")

    chart_data = {
        "types": {"labels": [r[0][:50] for r in type_stats], "values": [r[1] for r in type_stats], "colors": PALETTE[:len(type_stats)]},
        "cities": {"labels": [r[0][:40] if r[0] else "Не указан" for r in city_stats], "values": [r[1] for r in city_stats], "colors": PALETTE[:len(city_stats)]},
        "addresses": {"labels": [r[0][:50] if r[0] else "—" for r in address_stats], "values": [r[1] for r in address_stats]},
        "gtins": {"labels": gtin_labels, "values": [r[1] for r in gtin_stats], "raw_gtins": gtin_raw},
    }

    worst_address = address_stats[0] if address_stats else ("—", 0)
    worst_type = type_stats[0] if type_stats else ("—", 0)

    return chart_data, worst_address, worst_type, gtin_stats


# ═══════════════════════════════════════════════
#  ЗАГРУЗКА CSV
# ═══════════════════════════════════════════════

@router.get("/deviations/upload", response_class=HTMLResponse)
def upload_page(request: Request, success: str = "", error: str = "",
                db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if current_user.role not in (UserRole.engineer, UserRole.admin):
        raise HTTPException(status_code=403)
    batches = db.query(CzUploadBatch).order_by(CzUploadBatch.created_at.desc()).limit(20).all()
    return templates.TemplateResponse("deviations_upload.html", {
        "request": request, "user": current_user, "batches": batches,
        "success": success, "error": error,
    })


@router.post("/deviations/upload")
async def upload_csv(request: Request, file: UploadFile = File(...),
                     db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if current_user.role not in (UserRole.engineer, UserRole.admin):
        raise HTTPException(status_code=403)
    if not file.filename or not file.filename.lower().endswith(".csv"):
        return RedirectResponse(url="/deviations/upload?error=Загрузите+CSV-файл", status_code=302)

    content = await file.read()
    df = None
    for enc in ("utf-8-sig", "utf-8", "cp1251", "latin-1"):
        try:
            df = pd.read_csv(io.BytesIO(content), encoding=enc, sep=None, engine="python")
            if len(df.columns) > 1:
                break
        except Exception:
            continue

    if df is None or df.empty:
        logger.error("Ошибка парсинга CSV \"%s\": файл пустой или нечитаемый", file.filename)
        return RedirectResponse(url="/deviations/upload?error=Не+удалось+прочитать+CSV", status_code=302)

    df = _normalize_columns(df)
    if "deviation_type" not in df.columns:
        logger.error("Ошибка парсинга CSV \"%s\": не найдена колонка «Вид отклонения»", file.filename)
        return RedirectResponse(url="/deviations/upload?error=Не+найдена+колонка+«Вид+отклонения»", status_code=302)

    batch = CzUploadBatch(filename=file.filename, uploaded_by=current_user.id, row_count=len(df))
    db.add(batch)
    db.flush()

    # Собираем уникальные GTIN для авто-создания в справочнике
    new_gtins = set()

    for _, row in df.iterrows():
        address_raw = str(row.get("address", "")) if pd.notna(row.get("address")) else ""
        city_raw = str(row.get("city", "")) if pd.notna(row.get("city")) else ""
        gtin_raw = str(row.get("gtin", "")) if pd.notna(row.get("gtin")) else ""
        gtin_clean = gtin_raw.strip()

        if gtin_clean:
            new_gtins.add(gtin_clean)

        db.add(CzDeviation(
            batch_id=batch.id,
            deviation_type=str(row.get("deviation_type", "")).strip(),
            address=address_raw.strip() or None,
            address_clean=_clean_address(address_raw),
            city=city_raw.strip() or None,
            registration_date=_parse_date(row.get("registration_date")),
            gtin=gtin_clean or None,
            product_name=str(row.get("product_name", "")).strip() if pd.notna(row.get("product_name")) else None,
            inn=str(row.get("inn", "")).strip() if pd.notna(row.get("inn")) else None,
            org_name=str(row.get("org_name", "")).strip() if pd.notna(row.get("org_name")) else None,
        ))

    # Авто-добавляем новые GTIN в справочник (без имени — пользователь заполнит потом)
    existing_gtins = {p.gtin for p in db.query(Product.gtin).filter(Product.gtin.in_(new_gtins)).all()}
    for g in new_gtins - existing_gtins:
        db.add(Product(gtin=g))

    db.commit()

    size_kb = len(content) / 1024
    logger.info(
        "CSV загружен: %s (%.0f KB, %d строк) пользователем %s",
        file.filename, size_kb, len(df), current_user.username,
    )

    return RedirectResponse(url=f"/deviations/upload?success=Загружено+{len(df)}+записей", status_code=302)


@router.post("/deviations/batches/{batch_id}/delete")
def delete_batch(batch_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if current_user.role != UserRole.admin:
        raise HTTPException(status_code=403)
    batch = db.query(CzUploadBatch).filter(CzUploadBatch.id == batch_id).first()
    if not batch:
        raise HTTPException(status_code=404)

    row_count = batch.row_count
    filename = batch.filename
    db.delete(batch)
    db.commit()

    logger.warning(
        "Пакет #%d УДАЛЁН администратором %s (%s, %d записей)",
        batch_id, current_user.username, filename, row_count,
    )

    return RedirectResponse(url="/deviations/upload?success=Удалено", status_code=302)


# ═══════════════════════════════════════════════
#  ДАШБОРД — архивная структура Год/Месяц
# ═══════════════════════════════════════════════

@router.get("/deviations/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    year: int = Query(default=0),
    month: int = Query(default=0),
    gtin_filter: str = Query(default=""),
    city_filter: str = Query(default=""),
    type_filter: str = Query(default=""),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in (UserRole.engineer, UserRole.admin):
        raise HTTPException(status_code=403)

    # Собираем доступные годы/месяцы
    archive = _build_archive(db)

    # Если год/месяц не выбраны — показываем архивную структуру
    if not year or not month:
        return templates.TemplateResponse("deviations_archive.html", {
            "request": request, "user": current_user, "archive": archive,
        })

    # Фильтр по году/месяцу
    q = db.query(CzDeviation).filter(
        extract("year", CzDeviation.registration_date) == year,
        extract("month", CzDeviation.registration_date) == month,
    )
    if gtin_filter:
        q = q.filter(CzDeviation.gtin == gtin_filter)
    if city_filter:
        q = q.filter(CzDeviation.city == city_filter)
    if type_filter:
        q = q.filter(CzDeviation.deviation_type == type_filter)

    total_count = q.count()

    # GTIN → название
    all_gtin_codes = [r[0] for r in q.with_entities(CzDeviation.gtin).distinct().filter(
        CzDeviation.gtin.isnot(None), CzDeviation.gtin != ""
    ).all()]
    gtin_map = _gtin_display_map(db, all_gtin_codes)

    chart_data, worst_address, worst_type, gtin_stats = _build_chart_data(db, q, gtin_map)

    # Фильтр-опции (для этого месяца)
    all_types = [r[0] for r in q.with_entities(CzDeviation.deviation_type).distinct().all() if r[0]]
    all_cities = [r[0] for r in q.with_entities(CzDeviation.city).distinct().filter(
        CzDeviation.city.isnot(None), CzDeviation.city != "").order_by(CzDeviation.city).all()]

    return templates.TemplateResponse("deviations_dashboard.html", {
        "request": request, "user": current_user,
        "year": year, "month": month, "month_name": MONTH_NAMES.get(month, ""),
        "total_count": total_count,
        "worst_address": worst_address, "worst_type": worst_type,
        "chart_data_json": json.dumps(chart_data, ensure_ascii=False),
        "all_types": all_types, "all_cities": all_cities, "all_gtins": all_gtin_codes,
        "gtin_map": gtin_map,
        "filter_gtin": gtin_filter, "filter_city": city_filter, "filter_type": type_filter,
        "is_public": False,
    })


def _build_archive(db: Session) -> dict:
    """Строит структуру {year: [{month, month_name, count}, ...]}."""
    col_y = extract("year", CzDeviation.registration_date).label("y")
    col_m = extract("month", CzDeviation.registration_date).label("m")

    rows = (
        db.query(col_y, col_m, sa_func.count().label("cnt"))
        .filter(CzDeviation.registration_date.isnot(None))
        .group_by(col_y, col_m)
        .order_by(desc(col_y), desc(col_m))
        .all()
    )
    archive = {}
    for y, m, cnt in rows:
        yr = int(y)
        mn = int(m)
        archive.setdefault(yr, []).append({"month": mn, "month_name": MONTH_NAMES.get(mn, ""), "count": cnt})
    return archive


# ═══════════════════════════════════════════════
#  ПУБЛИЧНЫЙ ДАШБОРД (без авторизации)
# ═══════════════════════════════════════════════

@router.get("/analytics/{year}/{month}", response_class=HTMLResponse)
def public_dashboard(
    request: Request, year: int, month: int,
    gtin_filter: str = Query(default=""),
    city_filter: str = Query(default=""),
    type_filter: str = Query(default=""),
    db: Session = Depends(get_db),
):
    if month < 1 or month > 12:
        raise HTTPException(status_code=404, detail="Некорректный месяц")

    q = db.query(CzDeviation).filter(
        extract("year", CzDeviation.registration_date) == year,
        extract("month", CzDeviation.registration_date) == month,
    )
    if gtin_filter:
        q = q.filter(CzDeviation.gtin == gtin_filter)
    if city_filter:
        q = q.filter(CzDeviation.city == city_filter)
    if type_filter:
        q = q.filter(CzDeviation.deviation_type == type_filter)

    total_count = q.count()
    if total_count == 0:
        raise HTTPException(status_code=404, detail="Нет данных за этот период")

    all_gtin_codes = [r[0] for r in q.with_entities(CzDeviation.gtin).distinct().filter(
        CzDeviation.gtin.isnot(None), CzDeviation.gtin != "").all()]
    gtin_map = _gtin_display_map(db, all_gtin_codes)

    chart_data, worst_address, worst_type, _ = _build_chart_data(db, q, gtin_map)

    all_types = [r[0] for r in q.with_entities(CzDeviation.deviation_type).distinct().all() if r[0]]
    all_cities = [r[0] for r in q.with_entities(CzDeviation.city).distinct().filter(
        CzDeviation.city.isnot(None), CzDeviation.city != "").order_by(CzDeviation.city).all()]

    return templates.TemplateResponse("deviations_dashboard.html", {
        "request": request, "user": None,
        "year": year, "month": month, "month_name": MONTH_NAMES.get(month, ""),
        "total_count": total_count,
        "worst_address": worst_address, "worst_type": worst_type,
        "chart_data_json": json.dumps(chart_data, ensure_ascii=False),
        "all_types": all_types, "all_cities": all_cities, "all_gtins": all_gtin_codes,
        "gtin_map": gtin_map,
        "filter_gtin": gtin_filter, "filter_city": city_filter, "filter_type": type_filter,
        "is_public": True,
    })


# ═══════════════════════════════════════════════
#  СПРАВОЧНИК ПРОДУКЦИИ (GTIN)
# ═══════════════════════════════════════════════

@router.get("/deviations/products", response_class=HTMLResponse)
def product_list(
    request: Request,
    search: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in (UserRole.engineer, UserRole.admin):
        raise HTTPException(status_code=403)

    per_page = 30
    q = db.query(Product)
    if search:
        q = q.filter(
            (Product.gtin.contains(search)) | (Product.name.contains(search)) | (Product.category.contains(search))
        )

    total = q.count()
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)

    products = q.order_by(Product.gtin).offset((page - 1) * per_page).limit(per_page).all()

    return templates.TemplateResponse("deviations_products.html", {
        "request": request, "user": current_user,
        "products": products, "search": search,
        "page": page, "total_pages": total_pages, "total": total,
    })


@router.get("/deviations/products/{product_id}/edit", response_class=HTMLResponse)
def product_edit_form(request: Request, product_id: int,
                      db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if current_user.role not in (UserRole.engineer, UserRole.admin):
        raise HTTPException(status_code=403)
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse("deviations_product_edit.html", {
        "request": request, "user": current_user, "product": product, "error": None,
    })


@router.post("/deviations/products/{product_id}/edit", response_class=HTMLResponse)
def product_edit_submit(request: Request, product_id: int,
                        name: str = Form(""), category: str = Form(""),
                        db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if current_user.role not in (UserRole.engineer, UserRole.admin):
        raise HTTPException(status_code=403)
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404)
    product.name = name.strip() or None
    product.category = category.strip() or None
    db.commit()
    return RedirectResponse(url="/deviations/products", status_code=302)
