"""
app/email_utils.py — Отправка email-уведомлений с детальным логированием SMTP.

Настройки из .env: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM
"""

import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from dotenv import load_dotenv

from app.logging_config import get_logger

logger = get_logger("email")

load_dotenv()

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", "noreply@honest-mark.local")


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
