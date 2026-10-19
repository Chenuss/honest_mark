"""
app/config.py — Централизованная конфигурация приложения honest_mark.

Все настройки читаются из переменных окружения (.env), с безопасными
дефолтами для внутренней системы.

Раздел «ТП ЧЗ» (работа с технической поддержкой «Честного Знака»):
  • CRPT_SUPPORT_EMAIL  — адрес поддержки ЦРПТ (получатель наших обращений)
  • MARKIROVKA_EMAIL    — наш ящик, через который ведётся переписка
  • CRPT_SMTP_*         — SMTP для отправки с markirovka@belor.ru
  • CRPT_IMAP_*         — IMAP для чтения входящих support@crpt.ru
  • ENTERPRISE_DEFAULTS — предприятие по умолчанию (ООО «Белоречье»)
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent


def _get_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


# ──────────────────────────────────────────────
# Общие настройки приложения
# ──────────────────────────────────────────────
APP_NAME = "Журнал инцидентов — Честный знак"
SECRET_KEY: str = os.getenv("SECRET_KEY", "change-me-to-random-string")
TEMPLATE_DIR = "app/templates"

# ──────────────────────────────────────────────
# Почта приложения (уведомления по тикетам) — см. app/email_utils.py
# ──────────────────────────────────────────────
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", "noreply@honest-mark.local")

# ──────────────────────────────────────────────
# ТП ЧЗ — работа с техподдержкой ЦРПТ («Честный знак»)
# ──────────────────────────────────────────────
CRPT_SUPPORT_EMAIL = os.getenv("CRPT_SUPPORT_EMAIL", "support@crpt.ru")
MARKIROVKA_EMAIL = os.getenv("MARKIROVKA_EMAIL", "markirovka@belor.ru")

# SMTP для отправки писем в поддержку ЦРПТ (тот же ящик markirovka@belor.ru)
CRPT_SMTP_HOST = os.getenv("CRPT_SMTP_HOST", os.getenv("SMTP_HOST", "smtp.belor.ru"))
CRPT_SMTP_PORT = int(os.getenv("CRPT_SMTP_PORT", "465"))
CRPT_SMTP_USER = os.getenv("CRPT_SMTP_USER", MARKIROVKA_EMAIL)
CRPT_SMTP_PASSWORD = os.getenv("CRPT_SMTP_PASSWORD", os.getenv("SMTP_PASSWORD", ""))
CRPT_SMTP_SSL = _get_bool("CRPT_SMTP_SSL", CRPT_SMTP_PORT == 465)

# IMAP для приёма ответов от support@crpt.ru
CRPT_IMAP_HOST = os.getenv("CRPT_IMAP_HOST", "imap.belor.ru")
CRPT_IMAP_PORT = int(os.getenv("CRPT_IMAP_PORT", "993"))
CRPT_IMAP_USER = os.getenv("CRPT_IMAP_USER", MARKIROVKA_EMAIL)
CRPT_IMAP_PASSWORD = os.getenv("CRPT_IMAP_PASSWORD", os.getenv("CRPT_SMTP_PASSWORD", ""))
CRPT_IMAP_MAILBOX = os.getenv("CRPT_IMAP_MAILBOX", "INBOX")
CRPT_IMAP_SSL = _get_bool("CRPT_IMAP_SSL", CRPT_IMAP_PORT == 993)

# Период парсинга входящих (минут) и включение планировщика
CRPT_EMAIL_PARSE_INTERVAL_MIN = int(os.getenv("CRPT_EMAIL_PARSE_INTERVAL_MIN", "5"))
CRPT_SCHEDULER_ENABLED = _get_bool("CRPT_SCHEDULER_ENABLED", True)

# ──────────────────────────────────────────────
# Предприятие по умолчанию для шапки писем в ТП ЧЗ
# ──────────────────────────────────────────────
ENTERPRISE_DEFAULTS = {
    "enterprise_name": os.getenv("ENTERPRISE_NAME", 'ООО "Белоречье"'),
    "inn": os.getenv("ENTERPRISE_INN", "2330012345"),
    "contact_email": MARKIROVKA_EMAIL,
}

# Регламенты ЦРПТ
CRPT_REGULATORY_WORK_HOURS = 48   # регламентный срок ответа поддержки — 48 рабочих часов
CRPT_AUTO_CLOSE_WORK_DAYS = 3     # автозакрытие без ответа клиента — 3 рабочих дня
