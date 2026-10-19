"""
app/services/crpt_email_service.py — Отправка/приём писем техподдержки ЦРПТ («ТП ЧЗ»).

Бизнес-логика:
  • Исходящие: с ящика markirovka@belor.ru на support@crpt.ru.
    В теле письма формируется шапка (Предприятие / ИНН / Контактный ящик),
    далее — текст обращения, прикладываются файлы (скриншоты, PDF).
    После отправки создаётся CrptSupportCase со статусом WAITING_NUMBER.

  • Входящие: IMAP-обход ящика markirovka@belor.ru (INBOX), фильтр FROM
    support@crpt.ru, парсинг каждые 5 минут (APScheduler, см. scheduler_factory).
    По regex извлекается номер SR, категория, приоритет; по типу письма
    (автоответ / запрос уточнения / решение) обновляется статус кейса и
    регламентные сроки (48 рабочих часов, 3 рабочих дня на автозакрытие).
"""

from __future__ import annotations

import email as email_lib
import email.parser
import email.utils
import imaplib
import json
import re
import smtplib
from datetime import datetime
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders
from pathlib import Path
from typing import Iterable, Optional

from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from app import config
from app.logging_config import get_logger
from app.models import CrptEmail, CrptSupportCase, CrptCaseStatus
from app.utils.work_hours import (
    add_work_days,
    add_work_hours,
    get_remaining_work_hours,
    is_overdue,
    now_msk,
)

logger = get_logger("crpt_email")

# ──────────────────────────────────────────────
# Регулярые выражения для парсинга писем ЦРПТ
# ──────────────────────────────────────────────
RE_SR = re.compile(r"SR\d{7,10}", re.IGNORECASE)
RE_CATEGORY = re.compile(r'категория:\s*([A-ZА-Я0-9"]+)', re.IGNORECASE)
# "приоритет: 4.Стандартный." — берём до конца строки/точки, но не отрезаем часть после точки
RE_PRIORITY = re.compile(r"приоритет:\s*(\d+\.[^.\n]+|[^.\n]+)", re.IGNORECASE)

# Признаки типов писем (в нижнем регистре)
ACK_MARKERS = ("получили ваше обращение", "получили ваше заявл", "ему присвоен номер")
CLARIFICATION_MARKERS = ("уточняющая информация", "уточняющ", "приостановлена")
RESOLVED_MARKERS = ("было разрешено", "разрешено", "обращение решено")


def _detect_event_type(text_lower: str) -> str:
    if any(m in text_lower for m in RESOLVED_MARKERS):
        return "resolved"
    if any(m in text_lower for m in CLARIFICATION_MARKERS):
        return "clarification"
    if any(m in text_lower for m in ACK_MARKERS):
        return "ack"
    return "other"


# ──────────────────────────────────────────────
# Формирование и отправка исходящего письма
# ──────────────────────────────────────────────
def build_email_body(enterprise: str, inn: str, body: str) -> str:
    """Шапка обращения + текст проблемы (как требует ТП ЦРПТ)."""
    return (
        f"Предприятие: {enterprise}\n"
        f"ИНН: {inn}\n"
        f"Контактный ящик: {config.MARKIROVKA_EMAIL}\n"
        "---\n"
        f"{body}\n"
    )


def send_crpt_email(
    subject: str,
    body: str,
    files: Optional[Iterable] = None,
    to_email: Optional[str] = None,
    in_reply_to: Optional[str] = None,
) -> dict:
    """
    Отправить письмо в ТП ЦРПТ с ящика markirovka@belor.ru.

    files — список путей (str/Path) или tuple (filename, bytes).
    Возвращает dict c информацией об отправке (message_id, attachments).
    Бросает исключение при SMTP-ошибке.
    """
    to_email = to_email or config.CRPT_SUPPORT_EMAIL

    msg = MIMEMultipart()
    msg["From"] = config.CRPT_SMTP_USER or config.MARKIROVKA_EMAIL
    msg["To"] = to_email
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.attach(MIMEText(body, "plain", "utf-8"))

    attachments_meta = []
    for item in files or []:
        if isinstance(item, tuple):
            fname, payload = item
        else:
            p = Path(item)
            fname, payload = p.name, p.read_bytes()
        part = MIMEBase("application", "octet-stream")
        part.set_payload(payload)
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f'attachment; filename="{fname}"')
        msg.attach(part)
        attachments_meta.append({"name": fname, "size": len(payload)})

    message_id = msg.get("Message-ID", "")

    if not config.CRPT_SMTP_HOST or not config.CRPT_SMTP_PASSWORD:
        logger.warning("CRPT SMTP не настроен (CRPT_SMTP_HOST/CRPT_SMTP_PASSWORD) — письмо НЕ отправлено")
        return {"sent": False, "message_id": message_id, "attachments": attachments_meta}

    with smtplib.SMTP_SSL(config.CRPT_SMTP_HOST, config.CRPT_SMTP_PORT, timeout=30) \
            if config.CRPT_SMTP_SSL else smtplib.SMTP(config.CRPT_SMTP_HOST, config.CRPT_SMTP_PORT, timeout=30) as smtp:
        if not config.CRPT_SMTP_SSL:
            smtp.starttls()
        smtp.login(config.CRPT_SMTP_USER, config.CRPT_SMTP_PASSWORD)
        smtp.sendmail(msg["From"], [to_email], msg.as_string())

    logger.info("Письмо в ТП ЦРПТ отправлено: %r -> %s (%d вложений)", subject, to_email, len(attachments_meta))
    return {"sent": True, "message_id": message_id, "attachments": attachments_meta}


def create_crpt_ticket(
    db: Session,
    subject: str,
    body: str,
    enterprise: Optional[str] = None,
    inn: Optional[str] = None,
    files: Optional[Iterable] = None,
    gtin: Optional[str] = None,
    error_code: Optional[str] = None,
    doc_id: Optional[str] = None,
    api_error_object: Optional[str] = None,
    related_internal_ticket_id: Optional[int] = None,
    created_by_id: Optional[int] = None,
) -> CrptSupportCase:
    """
    Создать обращение в ТП ЧЗ: отправить письмо на support@crpt.ru
    и завести кейс со статусом WAITING_NUMBER (ждём автоответ с номером SR).
    """
    enterprise = enterprise or config.ENTERPRISE_DEFAULTS["enterprise_name"]
    inn = inn or config.ENTERPRISE_DEFAULTS["inn"]

    full_body = build_email_body(enterprise, inn, body)
    result = send_crpt_email(subject=subject, body=full_body, files=files)

    case = CrptSupportCase(
        subject=subject,
        enterprise_name=enterprise,
        inn=inn,
        body_text=full_body,
        status=CrptCaseStatus.WAITING_NUMBER if result["sent"] else CrptCaseStatus.NEW,
        gtin=gtin,
        error_code=error_code,
        doc_id=doc_id,
        api_error_object=api_error_object,
        attachments_json=json.dumps(result["attachments"], ensure_ascii=False),
        email_thread_id=result.get("message_id"),
        related_internal_ticket_id=related_internal_ticket_id,
        created_by_id=created_by_id,
        created_at=now_msk(),
    )
    db.add(case)
    db.flush()  # получить case.id

    # Записываем исходящее письмо в ленту переписки
    out_mail = CrptEmail(
        case_id=case.id,
        direction=CrptEmail.DIRECTION_OUT,
        message_id=result.get("message_id") or None,
        from_addr=config.MARKIROVKA_EMAIL,
        to_addr=config.CRPT_SUPPORT_EMAIL,
        subject=subject,
        body_text=full_body,
        event_type="outgoing",
        received_at=now_msk(),
    )
    db.add(out_mail)
    db.commit()
    db.refresh(case)
    logger.info("Создан кейс ТП ЧЗ #%d (subject=%r, sent=%s)", case.id, subject, result["sent"])
    return case


# ──────────────────────────────────────────────
# Парсинг текста письма
# ──────────────────────────────────────────────
def extract_plain_text(msg) -> str:
    """Извлечь текстовое тело письма (text/plain, иначе HTML через BeautifulSoup)."""
    if msg.is_multipart():
        parts_plain, parts_html = [], []
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain" and "attachment" not in str(part.get("Content-Disposition", "")):
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                if payload:
                    parts_plain.append(payload.decode(charset, errors="replace"))
            elif ctype == "text/html":
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                if payload:
                    parts_html.append(payload.decode(charset, errors="replace"))
        if parts_plain:
            return "\n".join(parts_plain)
        if parts_html:
            soup = BeautifulSoup("\n".join(parts_html), "html.parser")
            return soup.get_text("\n", strip=True)
        return ""
    payload = msg.get_payload(decode=True)
    if not payload:
        return ""
    charset = msg.get_content_charset() or "utf-8"
    text = payload.decode(charset, errors="replace")
    if "<html" in text.lower() or "<body" in text.lower():
        text = BeautifulSoup(text, "html.parser").get_text("\n", strip=True)
    return text


def parse_crpt_email_text(text: str) -> dict:
    """
    Распарсить текст письма от support@crpt.ru.
    Возвращает {sr_number, category, priority, event_type}.
    """
    sr_match = RE_SR.search(text)
    cat_match = RE_CATEGORY.search(text)
    pri_match = RE_PRIORITY.search(text)
    lowered = text.lower()
    return {
        "sr_number": sr_match.group(0).upper() if sr_match else None,
        "category": cat_match.group(1) if cat_match else None,
        "priority": pri_match.group(1).strip() if pri_match else None,
        "event_type": _detect_event_type(lowered),
    }


# ──────────────────────────────────────────────
# Применение распарсенного письма к кейсу
# ──────────────────────────────────────────────
def apply_parsed_email(db: Session, case: CrptSupportCase, parsed: dict, body: str) -> str:
    """
    Обновить кейс по входящему письму ЦРПТ. Возвращает применённый event_type.

    • ack ("Мы получили Ваше обращение… SR…") -> IN_PROGRESS, first_response_at=now,
      deadline_48h = now + 48 рабочих часов.
    • clarification ("необходима уточняющая информация… приостановлена")
      -> WAITING_CLARIFICATION, paused_at=now, auto_close_deadline_3d = now + 3 раб. дня.
    • resolved ("обращение … было разрешено") -> RESOLVED, result_text=body.
    """
    event = parsed.get("event_type", "other")
    now = now_msk()

    if event == "ack":
        if case.first_response_at is None:
            case.first_response_at = now
            case.deadline_48h = add_work_hours(now, config.CRPT_REGULATORY_WORK_HOURS)
        # Ответ на запрос уточнения не сбрасываем, если уже RESOLVED
        if case.status not in (CrptCaseStatus.RESOLVED, CrptCaseStatus.CLOSED):
            case.status = CrptCaseStatus.IN_PROGRESS
        logger.info("Кейс %s: получен автоответ, deadline_48h=%s", case.sr_number, case.deadline_48h)

    elif event == "clarification":
        if case.status not in (CrptCaseStatus.RESOLVED, CrptCaseStatus.CLOSED):
            case.status = CrptCaseStatus.WAITING_CLARIFICATION
            case.paused_at = now
            case.auto_close_deadline_3d = add_work_days(
                now, config.CRPT_AUTO_CLOSE_WORK_DAYS
            )
            logger.info(
                "Кейс %s: запрошено уточнение, автозакрытие к %s",
                case.sr_number, case.auto_close_deadline_3d,
            )

    elif event == "resolved":
        case.status = CrptCaseStatus.RESOLVED
        case.result_text = body.strip()[:6000]
        case.paused_at = None
        logger.info("Кейс %s: обращение разрешено", case.sr_number)

    case.last_crpt_email_body = body.strip()[:8000]
    remaining = get_remaining_work_hours(case)
    if remaining is not None:
        case.remaining_work_hours = remaining
    if event != "resolved" and is_overdue(case):
        case.status = CrptCaseStatus.OVERDUE
    return event


def process_incoming_message(db: Session, raw_message, received_at: Optional[datetime] = None) -> Optional[str]:
    """
    Обработать одно входящее письмо (email.message.Message).
    Найти кейс по SR, обновить статус, записать письмо в ленту.
    Возвращает event_type либо None (письмо не от support@crpt.ru).
    """
    from_addr = (email.utils.parseaddr(raw_message.get("From", ""))[1] or "").lower()
    if config.CRPT_SUPPORT_EMAIL.lower() not in from_addr:
        return None

    body = extract_plain_text(raw_message)
    subject = str(raw_message.get("Subject", "") or "")
    parsed = parse_crpt_email_text(subject + "\n" + body)
    message_id = raw_message.get("Message-ID")

    # Дедупликация по Message-ID
    if message_id:
        exists = db.query(CrptEmail).filter(CrptEmail.message_id == message_id).first()
        if exists:
            return None

    case: Optional[CrptSupportCase] = None
    if parsed["sr_number"]:
        case = db.query(CrptSupportCase).filter(
            CrptSupportCase.sr_number == parsed["sr_number"]
        ).first()
        if case is None and parsed["event_type"] == "ack":
            # Автоответ пришёл раньше, чем мы сохранили SR (например ручная отправка) —
            # привязка невозможна без additional context; заводиться «пустой» кейс не будем.
            logger.warning("Автоответ по %s: кейс не найден", parsed["sr_number"])

    if case is None:
        # Ищем по In-Reply-To/References в thread
        refs = (raw_message.get("In-Reply-To") or raw_message.get("References") or "").strip()
        if refs:
            case = db.query(CrptSupportCase).filter(
                CrptSupportCase.email_thread_id == refs
            ).first()

    if case is None:
        logger.info("Входящее от %s не привязано к кейсу (SR=%s) — сохранено без привязки", from_addr, parsed["sr_number"])

    if case is None:
        # Лента CrptEmail требует привязанный кейс — непривязанные письма только логируем.
        logger.warning("Письмо не привязано ни к одному кейсу ТП ЧЗ, в ленту не сохранено: %s", subject)
        return parsed["event_type"]

    mail = CrptEmail(
        case_id=case.id,
        direction=CrptEmail.DIRECTION_IN,
        message_id=message_id,
        from_addr=from_addr,
        to_addr=config.MARKIROVKA_EMAIL,
        subject=subject,
        body_text=body[:8000],
        parsed_sr_number=parsed["sr_number"],
        parsed_category=parsed["category"],
        parsed_priority=parsed["priority"],
        event_type=parsed["event_type"],
        received_at=received_at or now_msk(),
    )
    db.add(mail)

    # Присвоить SR, если ещё нет
    if parsed["sr_number"] and not case.sr_number:
        case.sr_number = parsed["sr_number"]
    apply_parsed_email(db, case, parsed, body)

    db.commit()
    return parsed["event_type"]


# ──────────────────────────────────────────────
# IMAP-обход входящих
# ──────────────────────────────────────────────
def fetch_and_process_inbox(db: Session) -> int:
    """
    Подключиться к IMAP (markirovka@belor.ru, INBOX), выбрать письма
    FROM support@crpt.ru и обработать каждое. Возвращает число обработанных.
    """
    if not (config.CRPT_IMAP_HOST and config.CRPT_IMAP_USER and config.CRPT_IMAP_PASSWORD):
        logger.warning("CRPT IMAP не настроен — парсинг входящих пропущен")
        return 0

    processed = 0
    try:
        conn_cls = imaplib.IMAP4_SSL if config.CRPT_IMAP_SSL else imaplib.IMAP4
        conn = conn_cls(config.CRPT_IMAP_HOST, config.CRPT_IMAP_PORT)
        conn.login(config.CRPT_IMAP_USER, config.CRPT_IMAP_PASSWORD)
        conn.select(config.CRPT_IMAP_MAILBOX)
        # Не читаем прочитанное повторно: UNSEEN + фильтр отправителя
        typ, data = conn.search(
            None, '(UNSEEN FROM "%s")' % config.CRPT_SUPPORT_EMAIL
        )
        ids = data[0].split() if typ == "OK" and data and data[0] else []
        parser = email.parser.BytesParser().parsebytes
        for num in ids:
            typ, msg_data = conn.fetch(num, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            message = parser(raw)
            try:
                process_incoming_message(db, message)
                processed += 1
            except Exception:
                db.rollback()
                logger.exception("Ошибка обработки письма %s", num)
        conn.logout()
    except Exception:
        logger.exception("IMAP-обход %s failed", config.CRPT_IMAP_HOST)
    if processed:
        logger.info("Обработано входящих писем ТП ЧЗ: %d", processed)
    return processed


def parse_raw_email_for_test(db: Session, raw_eml: bytes) -> dict:
    """
    Ручной тест парсинга (webhook POST /api/crpt/webhook/email):
    принять .eml как bytes, распарсить и применить к существующему кейсу.
    """
    message = email_lib.message_from_bytes(raw_eml)
    event = process_incoming_message(db, message)
    body = extract_plain_text(message)
    parsed = parse_crpt_email_text(body)
    return {"event_type": event or parsed["event_type"], "parsed": parsed}


# ──────────────────────────────────────────────
# Планировщик (APScheduler) — парсинг каждые 5 минут
# ──────────────────────────────────────────────
def scheduler_factory():
    """
    Фабрика фоновых задач ТП ЧЗ для APScheduler BackgroundScheduler.
    Используется в app/main.py при старте приложения.
    """
    from apscheduler.schedulers.background import BackgroundScheduler

    from app.database import SessionLocal

    def job_parse_inbox():
        db = SessionLocal()
        try:
            fetch_and_process_inbox(db)
        finally:
            db.close()

    def job_refresh_deadlines():
        """Пересчёт remaining_work_hours и проставление OVERDUE."""
        db = SessionLocal()
        try:
            cases = db.query(CrptSupportCase).filter(
                CrptSupportCase.status.in_(
                    [CrptCaseStatus.IN_PROGRESS, CrptCaseStatus.OVERDUE]
                )
            ).all()
            changed = False
            for case in cases:
                rem = get_remaining_work_hours(case)
                if rem is not None:
                    case.remaining_work_hours = rem
                if rem is not None and rem <= 0 and case.status == CrptCaseStatus.IN_PROGRESS:
                    case.status = CrptCaseStatus.OVERDUE
                changed = True
            if changed:
                db.commit()
        except Exception:
            db.rollback()
            logger.exception("job_refresh_deadlines failed")
        finally:
            db.close()

    scheduler = BackgroundScheduler(timezone="Europe/Moscow")
    scheduler.add_job(
        job_parse_inbox, "interval",
        minutes=config.CRPT_EMAIL_PARSE_INTERVAL_MIN,
        id="crpt_parse_inbox", replace_existing=True,
    )
    scheduler.add_job(
        job_refresh_deadlines, "interval",
        minutes=15, id="crpt_refresh_deadlines", replace_existing=True,
    )
    return scheduler
