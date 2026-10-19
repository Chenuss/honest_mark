"""
app/models.py — ORM-модели для журнала инцидентов «Честный знак» v2.0.

Таблицы:
  • production_sites   — площадки (заводы) холдинга
  • users              — пользователи (worker / engineer / admin)
  • line_equipment     — оборудование, привязано к площадке
  • tickets            — тикеты (инциденты), привязаны к площадке
  • ticket_photos      — фото/документы к тикетам
  • crpt_support_cases — кейсы техподдержки ЦРПТ («ТП ЧЗ»)
  • crpt_emails        — лента писем переписки с support@crpt.ru
"""

import enum
from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import relationship

from app.database import Base


# ──────────────────────────────────────────────
# Перечисления (Enums)
# ──────────────────────────────────────────────
class UserRole(str, enum.Enum):
    worker = "worker"
    engineer = "engineer"
    admin = "admin"


class TicketStatus(str, enum.Enum):
    open = "open"
    in_progress = "in_progress"
    pending_dept = "pending_dept"   # ожидание ответственной службы
    resolved = "resolved"
    closed = "closed"


class CrptCaseStatus(str, enum.Enum):
    """Статусы кейса техподдержки ЦРПТ («ТП ЧЗ»)."""
    NEW = "NEW"                                        # создано локально, письмо ещё не отправлено
    WAITING_NUMBER = "WAITING_NUMBER"                  # письмо отправлено, ждём автоответ с номером SR
    IN_PROGRESS = "IN_PROGRESS"                        # получен номер SR, идёт отсчёт 48 рабочих часов
    WAITING_CLARIFICATION = "WAITING_CLARIFICATION"   # ЦРПТ запросил уточнения, работа приостановлена
    RESOLVED = "RESOLVED"                              # обращение разрешено
    CLOSED = "CLOSED"                                  # закрыто (в т.ч. автоматически)
    OVERDUE = "OVERDUE"                                # просрочен регламентный срок 48 ч


# ──────────────────────────────────────────────
# Модель: Площадка (завод)
# ──────────────────────────────────────────────
class ProductionSite(Base):
    __tablename__ = "production_sites"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(200), unique=True, nullable=False)
    support_email = Column(String(200), nullable=True)

    equipment = relationship("LineEquipment", back_populates="site", lazy="dynamic")
    tickets = relationship("Ticket", back_populates="site", lazy="dynamic")

    def __repr__(self) -> str:
        return f"<ProductionSite id={self.id} name={self.name!r}>"


# ──────────────────────────────────────────────
# Модель: Пользователь
# ──────────────────────────────────────────────
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(100), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    full_name = Column(String(255), nullable=True)
    role = Column(Enum(UserRole), nullable=False, default=UserRole.worker)
    is_active = Column(Integer, nullable=False, default=1)  # 1=active, 0=inactive

    created_tickets = relationship(
        "Ticket", back_populates="author",
        foreign_keys="Ticket.author_id", lazy="dynamic",
    )
    assigned_tickets = relationship(
        "Ticket", back_populates="assignee",
        foreign_keys="Ticket.assignee_id", lazy="dynamic",
    )

    @property
    def display_name(self) -> str:
        """ФИО если заполнено, иначе username."""
        return self.full_name or self.username

    def __repr__(self) -> str:
        return f"<User id={self.id} username={self.username!r} role={self.role.value}>"


# ──────────────────────────────────────────────
# Модель: Оборудование линии
# ──────────────────────────────────────────────
class LineEquipment(Base):
    __tablename__ = "line_equipment"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(200), nullable=False)
    site_id = Column(
        Integer,
        ForeignKey("production_sites.id", ondelete="RESTRICT"),
        nullable=True,
    )

    site = relationship("ProductionSite", back_populates="equipment")
    tickets = relationship("Ticket", back_populates="equipment", lazy="dynamic")

    def __repr__(self) -> str:
        return f"<LineEquipment id={self.id} name={self.name!r}>"


# ──────────────────────────────────────────────
# Модель: Тикет (инцидент)
# ──────────────────────────────────────────────
class Ticket(Base):
    __tablename__ = "tickets"

    id = Column(Integer, primary_key=True, autoincrement=True)

    author_id = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    assignee_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    equipment_id = Column(Integer, ForeignKey("line_equipment.id", ondelete="RESTRICT"), nullable=False)
    site_id = Column(Integer, ForeignKey("production_sites.id", ondelete="RESTRICT"), nullable=True)

    status = Column(Enum(TicketStatus), nullable=False, default=TicketStatus.open, index=True)
    description = Column(Text, nullable=False)
    resolution_note = Column(Text, nullable=True)

    # Экспертиза ведущего инженера
    expert_conclusion = Column(Text, nullable=True)
    recommendations = Column(Text, nullable=True)
    responsible_dept = Column(String(200), nullable=True)

    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())
    closed_at = Column(DateTime, nullable=True)

    author = relationship("User", back_populates="created_tickets", foreign_keys=[author_id])
    assignee = relationship("User", back_populates="assigned_tickets", foreign_keys=[assignee_id])
    equipment = relationship("LineEquipment", back_populates="tickets")
    site = relationship("ProductionSite", back_populates="tickets")
    photos = relationship(
        "TicketPhoto", back_populates="ticket",
        cascade="all, delete-orphan", order_by="TicketPhoto.id",
    )

    def __repr__(self) -> str:
        return f"<Ticket id={self.id} status={self.status.value} equipment_id={self.equipment_id}>"


# ──────────────────────────────────────────────
# Модель: Файлы к тикету (фото + документы)
# ──────────────────────────────────────────────
class TicketPhoto(Base):
    __tablename__ = "ticket_photos"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticket_id = Column(Integer, ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False, index=True)
    filename = Column(String(300), nullable=False)
    original_name = Column(String(300), nullable=False)
    is_image = Column(Integer, nullable=False, default=1)  # 1=image, 0=document

    created_at = Column(DateTime, nullable=False, server_default=func.now())

    ticket = relationship("Ticket", back_populates="photos")

    def __repr__(self) -> str:
        return f"<TicketPhoto id={self.id} ticket_id={self.ticket_id}>"


# ──────────────────────────────────────────────
# Модель: Справочник продукции (GTIN)
# ──────────────────────────────────────────────
class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, autoincrement=True)
    gtin = Column(String(50), unique=True, nullable=False, index=True)
    name = Column(String(500), nullable=True)
    category = Column(String(200), nullable=True)

    def __repr__(self) -> str:
        return f"<Product gtin={self.gtin!r} name={self.name!r}>"


# ──────────────────────────────────────────────
# Модель: Пакет загрузки отклонений ЧЗ
# ──────────────────────────────────────────────
class CzUploadBatch(Base):
    __tablename__ = "cz_upload_batches"

    id = Column(Integer, primary_key=True, autoincrement=True)
    filename = Column(String(300), nullable=False)
    uploaded_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    row_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    uploader = relationship("User")
    deviations = relationship("CzDeviation", back_populates="batch", cascade="all, delete-orphan")


# ──────────────────────────────────────────────
# Модель: Отклонение «Честного Знака»
# ──────────────────────────────────────────────
class CzDeviation(Base):
    __tablename__ = "cz_deviations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    batch_id = Column(Integer, ForeignKey("cz_upload_batches.id", ondelete="CASCADE"), nullable=False, index=True)

    deviation_type = Column(String(500), nullable=False, index=True)       # Вид отклонения
    address = Column(String(500), nullable=True)                           # Адрес места фиксации
    address_clean = Column(String(300), nullable=True, index=True)         # Очищенный адрес (до номера дома)
    city = Column(String(200), nullable=True, index=True)                  # Муниципальный округ / город
    registration_date = Column(DateTime, nullable=True, index=True)        # Дата и время регистрации
    gtin = Column(String(50), nullable=True, index=True)                   # GTIN товара
    product_name = Column(String(500), nullable=True)                      # Наименование товара
    product_group = Column(String(200), nullable=True, index=True)         # Товарная группа (молочная, табачная и т.п.)
    inn = Column(String(20), nullable=True)                                # ИНН
    org_name = Column(String(500), nullable=True)                          # Наименование организации
    raw_row = Column(Text, nullable=True)                                  # Полная строка CSV для справки

    batch = relationship("CzUploadBatch", back_populates="deviations")

    def __repr__(self) -> str:
        return f"<CzDeviation id={self.id} type={self.deviation_type!r}>"

# ──────────────────────────────────────────────
# Модель: Кейс техподдержки ЦРПТ («ТП ЧЗ»)
# ──────────────────────────────────────────────
class CrptSupportCase(Base):
    """
    Обращение в техническую поддержку «Честного Знака» (ЦРПТ).

    Жизненный цикл:
      NEW -> WAITING_NUMBER (письмо отправлено на support@crpt.ru)
          -> IN_PROGRESS (пришёл автоответ с номером SR, пошёл отсчёт 48 раб. часов)
          -> WAITING_CLARIFICATION (ЦРПТ запросил уточнения — таймер паузится,
              без ответа обращение автозакроется через 3 рабочих дня)
          -> RESOLVED / CLOSED / OVERDUE
    """
    __tablename__ = "crpt_support_cases"

    id = Column(Integer, primary_key=True, autoincrement=True)

    sr_number = Column(String(50), unique=True, nullable=True, index=True)  # SR8878108
    subject = Column(String(500), nullable=False)
    enterprise_name = Column(String(300), nullable=False, default='ООО "Белоречье"')
    inn = Column(String(20), nullable=False, default="2330012345")
    body_text = Column(Text, nullable=True)

    status = Column(
        Enum(CrptCaseStatus), nullable=False,
        default=CrptCaseStatus.NEW, index=True,
    )

    # Контекст проблемы маркировки
    gtin = Column(String(50), nullable=True, index=True)
    error_code = Column(String(50), nullable=True)          # например 202 / INTRO_ERROR
    doc_id = Column(String(100), nullable=True)             # id документа/партии в ЧЗ
    api_error_object = Column(Text, nullable=True)          # JSON-ответ API ЧЗ

    # Временные метки и регламенты
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    first_response_at = Column(DateTime, nullable=True)     # когда пришёл номер SR
    deadline_48h = Column(DateTime, nullable=True)          # 48 рабочих часов от first_response_at
    paused_at = Column(DateTime, nullable=True)             # запрос уточнения (пауза таймера)
    resumed_at = Column(DateTime, nullable=True)            # ответили ЦРПТ — таймер возобновлён
    auto_close_deadline_3d = Column(DateTime, nullable=True)  # автозакрытие через 3 рабочих дня
    remaining_work_hours = Column(Float, nullable=True)     # кэш остатка раб. часов (обновляется парсером)

    attachments_json = Column(Text, nullable=True)          # список приложенных файлов (JSON)
    email_thread_id = Column(String(255), nullable=True, index=True)
    last_crpt_email_body = Column(Text, nullable=True)      # последнее письмо от support@crpt.ru
    result_text = Column(Text, nullable=True)               # текст решения от ЦРПТ

    related_internal_ticket_id = Column(
        Integer, ForeignKey("tickets.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    created_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    emails = relationship(
        "CrptEmail", back_populates="case",
        cascade="all, delete-orphan", order_by="CrptEmail.received_at",
    )
    related_internal_ticket = relationship("Ticket", foreign_keys=[related_internal_ticket_id])
    created_by = relationship("User", foreign_keys=[created_by_id])

    def __repr__(self) -> str:
        return f"<CrptSupportCase id={self.id} sr={self.sr_number!r} status={self.status.value}>"


# ──────────────────────────────────────────────
# Модель: Письмо переписки с ТП ЦРПТ (лента/timeline)
# ──────────────────────────────────────────────
class CrptEmail(Base):
    """Отдельное письмо (входящее или исходящее) в переписке по кейсу ТП ЧЗ."""
    __tablename__ = "crpt_emails"

    DIRECTION_IN = "in"
    DIRECTION_OUT = "out"

    id = Column(Integer, primary_key=True, autoincrement=True)
    case_id = Column(Integer, ForeignKey("crpt_support_cases.id", ondelete="CASCADE"), nullable=False, index=True)

    direction = Column(String(3), nullable=False, default=DIRECTION_IN)  # in — от ЦРПТ, out — от нас
    message_id = Column(String(500), nullable=True, unique=True)         # RFC Message-ID (дедупликация IMAP)
    from_addr = Column(String(300), nullable=True)
    to_addr = Column(String(300), nullable=True)
    subject = Column(String(500), nullable=True)
    body_text = Column(Text, nullable=True)

    parsed_sr_number = Column(String(50), nullable=True)
    parsed_category = Column(String(200), nullable=True)     # Категория: ЗНО…
    parsed_priority = Column(String(200), nullable=True)     # приоритет: 4.Стандартный
    event_type = Column(String(50), nullable=True)           # ack | clarification | resolved | other

    received_at = Column(DateTime, nullable=False, server_default=func.now(), index=True)

    case = relationship("CrptSupportCase", back_populates="emails")

    def __repr__(self) -> str:
        return f"<CrptEmail id={self.id} case_id={self.case_id} direction={self.direction!r}>"
