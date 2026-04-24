"""
init_db.py — Инициализация и миграция БД v3.

Что делает:
  1. Создаёт все таблицы (если нет)
  2. Запускает безопасные ALTER TABLE миграции
  3. Создаёт аккаунт admin (если нет пользователей)

Что НЕ делает:
  - Не добавляет демо-оборудование
  - Не трогает существующие данные в LineEquipment / ProductionSite
  - Не перезаписывает пользователей

Запуск:  python init_db.py
"""

from passlib.context import CryptContext
from sqlalchemy import text

from app.database import Base, SessionLocal, engine
from app.models import User, UserRole

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def create_tables() -> None:
    """Создаёт все таблицы из ORM-моделей (включая products, cz_*)."""
    print("Создаю таблицы (если не существуют)...")
    Base.metadata.create_all(bind=engine)
    print("OK.")


def _col_exists(conn, table: str, column: str) -> bool:
    r = conn.execute(text(
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_schema = DATABASE() "
        f"AND table_name = :tbl AND column_name = :col"
    ), {"tbl": table, "col": column})
    return r.scalar() > 0


def _table_exists(conn, table: str) -> bool:
    r = conn.execute(text(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema = DATABASE() AND table_name = :tbl"
    ), {"tbl": table})
    return r.scalar() > 0


def migrate() -> None:
    """Безопасные ALTER TABLE — добавляет недостающие столбцы и таблицы."""
    print("Проверяю миграции...")
    with engine.connect() as conn:

        # users.full_name
        if _table_exists(conn, "users") and not _col_exists(conn, "users", "full_name"):
            print("  + users.full_name")
            conn.execute(text("ALTER TABLE users ADD COLUMN full_name VARCHAR(255) NULL"))

        # tickets — expert fields
        if _table_exists(conn, "tickets"):
            for col in ("expert_conclusion", "recommendations", "responsible_dept"):
                if not _col_exists(conn, "tickets", col):
                    dtype = "TEXT" if col != "responsible_dept" else "VARCHAR(200)"
                    print(f"  + tickets.{col}")
                    conn.execute(text(f"ALTER TABLE tickets ADD COLUMN {col} {dtype} NULL"))

            if not _col_exists(conn, "tickets", "site_id"):
                print("  + tickets.site_id")
                conn.execute(text(
                    "ALTER TABLE tickets ADD COLUMN site_id INT NULL"
                ))
                # FK добавляем только если таблица production_sites существует
                if _table_exists(conn, "production_sites"):
                    try:
                        conn.execute(text(
                            "ALTER TABLE tickets ADD CONSTRAINT fk_tickets_site "
                            "FOREIGN KEY (site_id) REFERENCES production_sites(id)"
                        ))
                    except Exception:
                        pass  # FK уже может существовать

        # line_equipment.site_id
        if _table_exists(conn, "line_equipment") and not _col_exists(conn, "line_equipment", "site_id"):
            print("  + line_equipment.site_id")
            conn.execute(text("ALTER TABLE line_equipment ADD COLUMN site_id INT NULL"))
            if _table_exists(conn, "production_sites"):
                try:
                    conn.execute(text(
                        "ALTER TABLE line_equipment ADD CONSTRAINT fk_equipment_site "
                        "FOREIGN KEY (site_id) REFERENCES production_sites(id)"
                    ))
                except Exception:
                    pass

        # ticket_photos.is_image
        if _table_exists(conn, "ticket_photos") and not _col_exists(conn, "ticket_photos", "is_image"):
            print("  + ticket_photos.is_image")
            conn.execute(text("ALTER TABLE ticket_photos ADD COLUMN is_image INT NOT NULL DEFAULT 1"))

        # TicketStatus — pending_dept
        if _table_exists(conn, "tickets"):
            r = conn.execute(text(
                "SELECT COLUMN_TYPE FROM information_schema.columns "
                "WHERE table_schema = DATABASE() "
                "AND table_name = 'tickets' AND column_name = 'status'"
            ))
            col_type = r.scalar() or ""
            if "pending_dept" not in col_type:
                print("  + tickets.status: добавляю pending_dept")
                conn.execute(text(
                    "ALTER TABLE tickets MODIFY COLUMN status "
                    "ENUM('open','in_progress','pending_dept','resolved','closed') "
                    "NOT NULL DEFAULT 'open'"
                ))

        conn.commit()
    print("Миграции завершены.")


def seed_users() -> None:
    """Создаёт аккаунт admin (если нет ни одного пользователя)."""
    db = SessionLocal()
    try:
        user_count = db.query(User).count()
        if user_count == 0:
            db.add(User(
                username="admin",
                password_hash=pwd_context.hash("admin"),
                role=UserRole.admin,
                full_name="Администратор Системы",
            ))
            db.commit()
            print("  + Создан пользователь: admin / admin (роль: admin)")
            print("    Измените пароль после первого входа!")
        else:
            print(f"  Пользователей в системе: {user_count}. Seed пропущен.")
    finally:
        db.close()


if __name__ == "__main__":
    create_tables()
    migrate()
    seed_users()
    print("\nГотово.")
