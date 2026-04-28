"""
app/email_utils.py — Отправка email-уведомлений с детальным логированием SMTP.

Настройки из .env: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM
                      MANAGEMENT_EMAILS (список email руководства через запятую)
"""

import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path

from dotenv import load_dotenv

from app.logging_config import get_logger

logger = get_logger("email")

load_dotenv()

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", "noreply@honest-mark.local")
# Список email руководства через запятую, например: "boss@example.com,manager@example.com"
MANAGEMENT_EMAILS_RAW = os.getenv("MANAGEMENT_EMAILS", "")
MANAGEMENT_EMAILS = [
    email.strip() for email in MANAGEMENT_EMAILS_RAW.split(",") if email.strip()
] if MANAGEMENT_EMAILS_RAW else []


def _get_management_emails() -> list[str]:
    """Возвращает список email-адресов руководства."""
    return MANAGEMENT_EMAILS.copy()


async def send_expert_conclusion_email(
    to_email: str,
    ticket_id: int,
    equipment_name: str,
    site_name: str,
    expert_name: str,
    conclusion: str,
    recommendations: str,
    responsible_dept: str,
) -> bool:
    """
    Отправляет заключение эксперта на email ответственной службы.
    Возвращает True при успехе, False при ошибке.
    """
    # ── Проверки до отправки ──
    if not SMTP_HOST:
        logger.warning("SMTP_HOST не задан в .env — отправка email отключена")
        return False

    if not to_email:
        logger.warning(
            "Тикет #%d: email получателя пустой (площадка: %s), отправка пропущена",
            ticket_id, site_name,
        )
        return False

    logger.info(
        "Отправка на %s (тикет #%d, площадка \"%s\", эксперт: %s)",
        to_email, ticket_id, site_name, expert_name,
    )

    try:
        import aiosmtplib

        msg = MIMEMultipart("alternative")
        msg["From"] = SMTP_FROM
        msg["To"] = to_email
        msg["Subject"] = f"Заключение эксперта — Тикет #{ticket_id} ({equipment_name})"

        body = f"""Заключение эксперта по инциденту

Тикет: #{ticket_id}
Площадка: {site_name}
Оборудование: {equipment_name}
Эксперт: {expert_name}
Ответственная служба: {responsible_dept}

─── ЗАКЛЮЧЕНИЕ ───
{conclusion}

─── РЕКОМЕНДАЦИИ ───
{recommendations}

---
Документ сформирован автоматически системой «Журнал инцидентов — Честный знак».
"""

        msg.attach(MIMEText(body, "plain", "utf-8"))

        await aiosmtplib.send(
            msg,
            hostname=SMTP_HOST,
            port=SMTP_PORT,
            username=SMTP_USER or None,
            password=SMTP_PASSWORD or None,
            start_tls=True,
        )

        logger.info("Доставлено: %s (тикет #%d)", to_email, ticket_id)
        return True

    except Exception as exc:
        exc_type = type(exc).__name__

        # Категоризация ошибок SMTP
        try:
            import aiosmtplib
            if isinstance(exc, (aiosmtplib.SMTPConnectError, aiosmtplib.SMTPConnectTimeoutError, OSError)):
                logger.error(
                    "Connection timeout к %s:%d — сервер недоступен (%s)",
                    SMTP_HOST, SMTP_PORT, exc,
                )
            elif isinstance(exc, aiosmtplib.SMTPAuthenticationError):
                logger.error(
                    "Auth refused: проверьте SMTP_USER/SMTP_PASSWORD в .env (%s)",
                    exc,
                )
            elif isinstance(exc, aiosmtplib.SMTPResponseException):
                logger.error(
                    "SMTP ответил ошибкой при отправке на %s: [%s] %s",
                    to_email, exc_type, exc,
                )
            else:
                logger.error(
                    "Ошибка отправки на %s: [%s] %s",
                    to_email, exc_type, exc, exc_info=True,
                )
        except ImportError:
            logger.error(
                "Ошибка отправки на %s: [%s] %s (aiosmtplib не установлен?)",
                to_email, exc_type, exc,
            )

        return False


async def send_management_notification(
    ticket_id: int,
    equipment_name: str,
    site_name: str,
    author_name: str,
    description: str,
    status: str,
    photo_paths: list[str] | None = None,
    is_creation: bool = True,
) -> bool:
    """
    Отправляет короткое уведомление руководству о создании или изменении тикета.
    Если есть графические вложения (фото), они прикрепляются к письму.
    
    :param ticket_id: ID тикета
    :param equipment_name: Название оборудования
    :param site_name: Название площадки
    :param author_name: ФИО автора тикета
    :param description: Описание проблемы (кратко)
    :param status: Статус тикета
    :param photo_paths: Список путей к файлам изображений для вложения
    :param is_creation: True — создание, False — изменение
    :return: True если хотя бы одно письмо отправлено успешно
    """
    management_emails = _get_management_emails()
    
    if not management_emails:
        logger.info("MANAGEMENT_EMAILS не задан — уведомление руководства отключено")
        return False
    
    if not SMTP_HOST:
        logger.warning("SMTP_HOST не задан в .env — отправка email отключена")
        return False
    
    subject_prefix = "Новый тикет" if is_creation else "Изменение тикета"
    subject = f"{subject_prefix} #{ticket_id} — {equipment_name} ({site_name})"
    
    # Краткое описание (первые 200 символов)
    short_desc = description[:200] + "..." if len(description) > 200 else description
    
    logger.info(
        "Отправка уведомления руководству (%d получателей) о тикете #%d",
        len(management_emails), ticket_id,
    )
    
    success_count = 0
    
    for to_email in management_emails:
        try:
            import aiosmtplib
            
            msg = MIMEMultipart("mixed")
            msg["From"] = SMTP_FROM
            msg["To"] = to_email
            msg["Subject"] = subject
            
            body = f"""Уведомление системы инцидентов «Честный знак»

{subject_prefix}: #{ticket_id}
Площадка: {site_name}
Оборудование: {equipment_name}
Автор: {author_name}
Статус: {status}

Описание:
{short_desc}

---
Документ сформирован автоматически.
"""
            
            msg.attach(MIMEText(body, "plain", "utf-8"))
            
            # Прикрепляем изображения если есть
            if photo_paths:
                for photo_path in photo_paths:
                    path = Path(photo_path)
                    if path.exists() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}:
                        try:
                            with open(path, "rb") as f:
                                img_data = f.read()
                            
                            part = MIMEImage(img_data, name=path.name)
                            part.add_header(
                                "Content-Disposition",
                                "attachment",
                                filename=path.name,
                            )
                            msg.attach(part)
                        except Exception as img_exc:
                            logger.warning(
                                "Не удалось прикрепить изображение %s: %s",
                                photo_path, img_exc,
                            )
            
            await aiosmtplib.send(
                msg,
                hostname=SMTP_HOST,
                port=SMTP_PORT,
                username=SMTP_USER or None,
                password=SMTP_PASSWORD or None,
                start_tls=True,
            )
            
            logger.info("Уведомление доставлено: %s (тикет #%d)", to_email, ticket_id)
            success_count += 1
            
        except Exception as exc:
            exc_type = type(exc).__name__
            logger.error(
                "Ошибка отправки уведомления на %s: [%s] %s",
                to_email, exc_type, exc,
            )
    
    return success_count > 0
