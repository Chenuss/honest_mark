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

import base64
import email as email_lib
import email.parser
import email.policy
import email.utils
import imaplib
import json
import logging
import re
import smtplib
from datetime import datetime
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders
from email.header import decode_header, make_header
from pathlib import Path
from typing import Iterable, Optional

from bs4 import BeautifulSoup
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import config
from app.logging_config import get_logger
from app.models import CrptEmail, CrptSupportCase, CrptCaseStatus, CrptUnmatchedEmail
from app.utils.work_hours import (
    add_work_days,
    add_work_hours,
    get_remaining_work_hours,
    is_overdue,
    now_msk,
)

logger = get_logger("crpt_email")


def display_body(text: Optional[str], limit: int = 4000) -> str:
    """Безопасный вывод тела письма в ленте: очищает случайно сохранённый HTML/base64.

    Используется роутами для «исцеления» старых записей, созданных до фикса
    парсинга (в БД мог лежать сырой HTML или незадекодированная base64-часть).
    """
    cleaned, _ = normalize_body(text or "")
    return cleaned[:limit]

# ──────────────────────────────────────────────
# Регулярые выражения для парсинга писем ЦРПТ
# ──────────────────────────────────────────────
RE_SR = re.compile(r"\bSR\d{7,10}\b", re.IGNORECASE)
RE_CATEGORY = re.compile(r'категория:\s*"?([A-ZА-ЯЁ0-9]+)"?', re.IGNORECASE)
# "приоритет: 4.Стандартный." — берём до конца строки/точки, но не отрезаем часть после точки
RE_PRIORITY = re.compile(r"приоритет:\s*(\d+\.[^.\n\"]+|[^.\n\"]+)", re.IGNORECASE)

# Признаки типов писем (в нижнем регистре)
ACK_MARKERS = ("получили ваше обращение", "получили ваше заявл", "ему присвоен номер")
CLARIFICATION_MARKERS = (
    "уточняющая информация",
    "необходима уточняющ",
    "требуется уточнени",
    "прошу уточнить",
    "приостановлена",
    "работа по обращению приостановлена",
    "автоматически закроется",
)
RESOLVED_MARKERS = ("было разрешено", "обращение решено")
# «разрешено» в автоответе ЦРПТ встречается и в негативном контексте:
#   «По Вашему обращению была обновлена категория… Регламентный срок…»
#   «Ваше обращение ещё не разрешено» — поэтому проверям только без отрицания.
RESOLVED_NEGATIONS = ("не было разрешено", "ещё не разрешено", "не разрешено", "не может быть разрешено")


def _detect_event_type(text_lower: str) -> str:
    # Порядок проверок важен: автоответ ЦРПТ содержит фразу «потребуется уточнить
    # дополнительные детали» — маркеры уточнения не должны срабатывать раньше ack,
    # а письмо-уточнение всегда содержит «приостановлена/закроется», поэтому его
    # определяем до resolved («без ответа обращение будет закрыто/разрешено…»).
    if any(m in text_lower for m in ACK_MARKERS):
        return "ack"
    if any(m in text_lower for m in CLARIFICATION_MARKERS):
        return "clarification"
    if any(m in text_lower for m in RESOLVED_MARKERS):
        return "resolved"
    if ("разрешено" in text_lower
            and not any(neg in text_lower for neg in RESOLVED_NEGATIONS)
            and "срок" not in text_lower):
        return "resolved"
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

    # Извлекаем SR из темы (ЦРПТ отвечает «Re: <тема>» или «Изменение SR… <тема>») —
    # используется как запасной ключ привязки входящих писем.
    sr_in_subject = RE_SR.search(subject or "")

    case = CrptSupportCase(
        subject=subject,
        enterprise_name=enterprise,
        inn=inn,
        body_text=full_body,
        status=CrptCaseStatus.WAITING_NUMBER if result["sent"] else CrptCaseStatus.NEW,
        sr_number=sr_in_subject.group(0).upper() if sr_in_subject else None,
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
_HTML_HINT_RE = re.compile(r"<\s*(html|body|div|p|table|br|span|td)\b", re.IGNORECASE)


def looks_like_html(text: str) -> bool:
    """Признак того, что текст является HTML-разметкой (а не plain text)."""
    if not text:
        return False
    return bool(_HTML_HINT_RE.search(text))


def html_to_text(html: str) -> str:
    """Очистка HTML в читаемый текст: убираем скрипты/стили, теги — в переводы строк."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "head", "title", "meta", "link"]):
        tag.decompose()
    # Блочные элементы и <br> -> перевод строки, чтобы текст не сливался
    for tag in soup.find_all(["br", "p", "div", "tr", "li", "table", "h1", "h2", "h3", "h4"]):
        tag.append("\n")
    text = soup.get_text(" ", strip=False)
    # Схлопываем пробелы внутри строк и пустые строки
    lines = [re.sub(r"[ \t\xa0]+", " ", ln).strip() for ln in text.splitlines()]
    out, prev_empty = [], True
    for ln in lines:
        if ln:
            out.append(ln)
            prev_empty = False
        elif not prev_empty:
            out.append("")
            prev_empty = True
    return "\n".join(out).strip()


def normalize_body(text: str) -> tuple[str, bool]:
    """Вернуть (чистый текст, is_html). Если внутри HTML — распарсить через BeautifulSoup.

    Используется и при сохранении писем, и для «исцеления» старых записей ленты,
    где когда-то сохранился сырой HTML (в т.ч. base64-части multipart).
    """
    if not text:
        return "", False
    # base64-«простыня» без пробелов — явный признак незадекодированного MIME
    if len(text) > 500 and re.fullmatch(r"[A-Za-z0-9+/=\s]+", text[:2000]) and " " not in text[:200]:
        try:
            decoded = base64.b64decode(text[:20000] + "===", validate=False)
            decoded_txt = decoded.decode("utf-8", errors="ignore")
            if looks_like_html(decoded_txt):
                return html_to_text(decoded_txt), True
        except Exception:
            pass
    if looks_like_html(text):
        return html_to_text(text), True
    return text, False


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
            text = "\n".join(parts_plain)
            # Some mailers send quoted-printable/base64 inside text/plain too
            cleaned, _ = normalize_body(text)
            return cleaned or text
        if parts_html:
            return html_to_text("\n".join(parts_html))
        return ""
    payload = msg.get_payload(decode=True)
    if not payload:
        return ""
    charset = msg.get_content_charset() or "utf-8"
    text = payload.decode(charset, errors="replace")
    cleaned, _ = normalize_body(text)
    return cleaned or text


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


def decode_mime_header(value: Optional[str]) -> str:
    """Декодировать RFC2047-заголовок (=?UTF-8?q?=D0=98=D0=B7=D0=BC=D0=B5=D0=BD=D0=B5=D0=BD=D0=B8=D0=B5_=SR…?=)."""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _normalize_subject(subject: str) -> str:
    """Нормализовать тему: снять префиксы Re:/Fwd:/«Изменение SR…», кавычки, опечатки."""
    s = (subject or "").strip()
    # «Изменение SR9009471 "тема"» -> снять префикс и номер
    s = re.sub(r"^изменение\s*(SR\d{7,10})?\s*[:\-]?\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^(re|fw?d)\s*:\s*", "", s.strip(), flags=re.IGNORECASE).strip()
    return s.strip().strip('"').strip("'").strip()


def _subject_core(subject: str, maxlen: int = 45) -> str:
    """Ядро темы для нестрогого сравнения: только буквы/цифры, первые maxlen символов.

    Снимает искажения вложений/переносов («контуре .» vs «контуре.»), регистр.
    """
    core = re.sub(r"[^0-9a-zA-Zа-яА-ЯёЁ]+", "", _normalize_subject(subject)).lower()
    return core[:maxlen]


def _find_case_by_subject(db: Session, subject: str) -> Optional[CrptSupportCase]:
    """Привязка по теме письма: ЦРПТ отвечает «Re: <тема>» / «Изменение SR… "<тема>"»,
    поэтому сравниваем нормализованные ядра тем кейсов с ядром темы входящего."""
    norm = _normalize_subject(subject)
    if len(norm) < 5:
        return None
    core = _subject_core(norm)
    if not core:
        return None
    candidates = []
    for c in db.query(CrptSupportCase).all():
        ccore = _subject_core(c.subject or "")
        if not ccore:
            continue
        # совпадение: одно ядро является началом/подстрокой другого
        if core.startswith(ccore) or ccore.startswith(core) or core in ccore or ccore in core:
            candidates.append(c)
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        # среди нескольких выбираем ожидающий автоответ (без SR) — самый свежий
        waiting = sorted(
            (c for c in candidates if not c.sr_number and c.status == CrptCaseStatus.WAITING_NUMBER),
            key=lambda c: c.created_at or datetime.min, reverse=True,
        )
        if len(waiting) >= 1:
            return waiting[0]
        logger.warning("Тема %r соответствует нескольким кейсам (%s) — привязка по теме отменена",
                       subject, [c.id for c in candidates])
    return None


def process_incoming_message(db: Session, raw_message, received_at: Optional[datetime] = None) -> Optional[str]:
    """
    Обработать одно входящее письмо (email.message.Message).
    Найти кейс (SR -> In-Reply-To/Message-ID -> исходный Message-ID -> тема),
    обновить статус, записать письмо в ленту.
    Непривязанные письма сохраняются в crpt_unmatched_emails и перепривязываются
    следующим проходом (см. rematch_unmatched_emails).
    Возвращает event_type либо None (письмо не от support@crpt.ru / дубликат).
    """
    from_addr = (email.utils.parseaddr(raw_message.get("From", ""))[1] or "").lower()
    if config.CRPT_SUPPORT_EMAIL.lower() not in from_addr:
        return None

    body = extract_plain_text(raw_message)
    subject = decode_mime_header(raw_message.get("Subject"))
    parsed = parse_crpt_email_text(subject + "\n" + body)
    message_id = raw_message.get("Message-ID")

    # Дедупликация по Message-ID
    if message_id:
        exists = (
            db.query(CrptEmail).filter(CrptEmail.message_id == message_id).first()
            or db.query(CrptUnmatchedEmail).filter(CrptUnmatchedEmail.message_id == message_id).first()
        )
        if exists:
            return None

    case: Optional[CrptSupportCase] = None

    # 1) Прямая привязка по номеру SR из тела/темы письма
    if parsed["sr_number"]:
        case = db.query(CrptSupportCase).filter(
            CrptSupportCase.sr_number == parsed["sr_number"]
        ).first()
        if case is None and parsed["event_type"] == "ack":
            logger.warning("Автоответ по %s: кейс по sr_number не найден — пробуем привязку по тредам/теме",
                           parsed["sr_number"])

    # 2) Привязка по цепочке писем: In-Reply-To/References содержат Message-ID нашего исходящего
    if case is None:
        refs_raw = " ".join(filter(None, [raw_message.get("In-Reply-To"), raw_message.get("References")]))
        ref_ids = [m.strip() for m in re.findall(r"<[^<>]+>", refs_raw)]
        for rid in ref_ids:
            case = db.query(CrptSupportCase).filter(CrptSupportCase.email_thread_id == rid).first()
            if case:
                break
        if case is None and ref_ids:
            out_mail = db.query(CrptEmail).filter(CrptEmail.message_id.in_(ref_ids)).first()
            if out_mail:
                case = db.get(CrptSupportCase, out_mail.case_id)

    # 3) Привязка по теме письма (ЦРПТ часто НЕ ставит In-Reply-To, а SR присваивает
    #    уже после нашего commit — тогда это единственный доступный ключ)
    if case is None:
        case = _find_case_by_subject(db, subject)

    if case is None:
        # Сохраняем письмо, чтобы не потерять: rematch_unmatched_emails привяжет его,
        # когда SR появится в БД (например после ручного заполнения или нового прохода).
        unmatched = CrptUnmatchedEmail(
            message_id=message_id,
            from_addr=from_addr,
            subject=subject[:500],
            body_text=body[:8000],
            body_is_html=looks_like_html(body),
            parsed_sr_number=parsed["sr_number"],
            received_at=received_at or now_msk(),
        )
        db.add(unmatched)
        db.commit()
        logger.info("Входящее от %s не привязано к кейсу (SR=%s) — сохранено в crpt_unmatched_emails: %r",
                    from_addr, parsed["sr_number"], subject)
        return parsed["event_type"]

    mail = CrptEmail(
        case_id=case.id,
        direction=CrptEmail.DIRECTION_IN,
        message_id=message_id,
        from_addr=from_addr,
        to_addr=config.MARKIROVKA_EMAIL,
        subject=subject,
        body_text=body[:8000],
        body_is_html=looks_like_html(body),
        parsed_sr_number=parsed["sr_number"],
        parsed_category=parsed["category"],
        parsed_priority=parsed["priority"],
        event_type=parsed["event_type"],
        received_at=received_at or now_msk(),
    )
    db.add(mail)

    # Присвоить SR, если ещё нет (учитываем unique-конфликт с другим кейсом)
    if parsed["sr_number"] and not case.sr_number:
        clash = db.query(CrptSupportCase).filter(
            CrptSupportCase.sr_number == parsed["sr_number"],
            CrptSupportCase.id != case.id,
        ).first()
        if clash:
            logger.warning("SR %s уже принадлежит кейсу #%d — кейсу #%d SR не присвоен",
                           parsed["sr_number"], clash.id, case.id)
        else:
            case.sr_number = parsed["sr_number"]
    apply_parsed_email(db, case, parsed, body)

    db.commit()
    return parsed["event_type"]


def rematch_unmatched_emails(db: Session) -> int:
    """Перепривязать ранее несопоставленные письма к кейсам (по SR / теме).

    Вызывается из фонового job_parse_inbox после IMAP-обхода: если автоответ
    пришёл раньше, чем кейс получил SR (гонка commit/парсинга), письмо будет
    применено к кейсу на следующем проходе.
    """
    pending = db.query(CrptUnmatchedEmail).filter(
        CrptUnmatchedEmail.matched_case_id.is_(None)
    ).order_by(CrptUnmatchedEmail.received_at).all()
    matched = 0
    for um in pending:
        case: Optional[CrptSupportCase] = None
        if um.parsed_sr_number:
            case = db.query(CrptSupportCase).filter(
                CrptSupportCase.sr_number == um.parsed_sr_number
            ).first()
        if case is None:
            case = _find_case_by_subject(db, um.subject or "")
        if case is None:
            continue

        text = (um.subject or "") + "\n" + (um.body_text or "")
        parsed = parse_crpt_email_text(text)
        # «Исцеление» тела: старые unmatched-письма могли сохраниться сырым HTML/base64
        clean_body, was_html = normalize_body(um.body_text or "")
        if was_html and clean_body:
            um.body_text = clean_body[:8000]
            um.body_is_html = True
        already = db.query(CrptEmail).filter(CrptEmail.message_id == um.message_id).first()
        if not already:
            db.add(CrptEmail(
                case_id=case.id,
                direction=CrptEmail.DIRECTION_IN,
                message_id=um.message_id,
                from_addr=um.from_addr,
                to_addr=config.MARKIROVKA_EMAIL,
                subject=um.subject,
                body_text=(um.body_text or "")[:8000],
                body_is_html=bool(um.body_is_html),
                parsed_sr_number=parsed["sr_number"],
                parsed_category=parsed["category"],
                parsed_priority=parsed["priority"],
                event_type=parsed["event_type"],
                received_at=um.received_at,
            ))
        if parsed["sr_number"] and not case.sr_number:
            clash = db.query(CrptSupportCase).filter(
                CrptSupportCase.sr_number == parsed["sr_number"],
                CrptSupportCase.id != case.id,
            ).first()
            if not clash:
                case.sr_number = parsed["sr_number"]
        apply_parsed_email(db, case, parsed, um.body_text or "")
        um.matched_case_id = case.id
        matched += 1
        logger.info("Перепривязано непривязанное письмо #%d -> кейс %s (SR=%s)",
                    um.id, case.id, case.sr_number)
    if matched:
        db.commit()
    return matched


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
def ensure_crpt_tables() -> None:
    """Гарантия наличия таблиц ТП ЧЗ (критично для SQLite и старых БД).

    Проект исторически инициализируется через init_db.py / alembic; если
    миграция b2crpt002 не была применена (или приложение запущено на новой
    базе), фоновые задачи падали с «Table ... doesn't exist». Создаём
    недостающие таблицы идемпотентно перед первым обращением к ним.
    """
    try:
        from app.database import Base, engine
        import app.models  # noqa: F401 — регистрирует модели в Base.metadata

        tables = [
            CrptSupportCase.__table__,
            CrptEmail.__table__,
            CrptUnmatchedEmail.__table__,
        ]
        # create_all(sort_on_dependent_tables=True) может молча пропустить
        # таблицу из-за FK на несуществующую (в списке) таблицу — создаём
        # каждую отдельно; checkfirst делает операцию идемпотентной.
        for t in tables:
            Base.metadata.create_all(bind=engine, tables=[t], checkfirst=True)
    except Exception:
        logger.exception("Не удалось гарантировать наличие таблиц ТП ЧЗ")


def scheduler_factory():
    """
    Фабрика фоновых задач ТП ЧЗ для APScheduler BackgroundScheduler.
    Используется в app/main.py при старте приложения.
    """
    from apscheduler.schedulers.background import BackgroundScheduler

    from app.database import SessionLocal

    def job_parse_inbox():
        ensure_crpt_tables()
        db = SessionLocal()
        try:
            fetch_and_process_inbox(db)
        finally:
            db.close()
        # Отдельная сессия: если внутри IMAP-обхода был rollback, перепривязку
        # всё равно надо выполнить (письма из crpt_unmatched_emails).
        db2 = SessionLocal()
        try:
            rematch_unmatched_emails(db2)
        except Exception:
            db2.rollback()
            logger.exception("rematch_unmatched_emails failed")
        finally:
            db2.close()

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
