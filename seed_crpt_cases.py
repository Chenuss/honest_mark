"""
seed_crpt_cases.py — Демо-данные для раздела «ТП ЧЗ».

Создаёт кейс SR8878108 из боевого примера:
  GTIN 04607003503949, ошибка 202 (CONFORMITY_DECLARATION / INTRO_ERROR),
  автоответ ЦРПТ с категорией ЗНО и приоритетом 4.Стандартный,
  рассчитанный дедлайн 48 рабочих часов.

Запуск:  python seed_crpt_cases.py
(таблицы должны существовать — сначала `python init_db.py` или `alembic upgrade head`)
"""

from datetime import timedelta

from app.database import SessionLocal
from app.models import CrptCaseStatus, CrptEmail, CrptSupportCase
from app.utils.work_hours import add_work_hours, get_remaining_work_hours, now_msk


def seed() -> None:
    db = SessionLocal()
    try:
        if db.query(CrptSupportCase).filter_by(sr_number="SR8878108").first():
            print("Кейс SR8878108 уже существует — seed пропущен.")
            return

        first_response = now_msk() - timedelta(hours=10)  # имитация: письмо вчера/сегодня утром

        case = CrptSupportCase(
            sr_number="SR8878108",
            subject="Ошибка 202 при вводе в оборот, GTIN 04607003503949 (молочная продукция)",
            enterprise_name='ООО "Белоречье"',
            inn="2330012345",
            body_text=(
                'Предприятие: ООО "Белоречье"\n'
                "ИНН: 2330012345\n"
                "Контактный ящик: markirovka@belor.ru\n"
                "---\n"
                "При попытке ввода партии в оборот через API Честный Знак получаем "
                "ошибку 202 (INTRO_ERROR) по декларации соответствия CONFORMITY_DECLARATION. "
                "GTIN: 04607003503949. Скриншот ответа API приложен.\n"
            ),
            status=CrptCaseStatus.IN_PROGRESS,
            gtin="04607003503949",
            error_code="202",
            doc_id="CONFORMITY_DECLARATION",
            api_error_object=(
                '{"code": 202, "resultStatus": "INTRO_ERROR", '
                '"reason": {"description": "Не найдена действующая декларация соответствия"}}'
            ),
            created_at=first_response - timedelta(minutes=15),
            first_response_at=first_response,
            deadline_48h=add_work_hours(first_response, 48),
            attachments_json='[{"name": "error_202_screenshot.png", "size": 182340}]',
            email_thread_id="<seed-out-1@belor.ru>",
        )
        case.remaining_work_hours = get_remaining_work_hours(case)
        db.add(case)
        db.flush()

        ack_body = (
            "Мы получили Ваше обращение, ему присвоен номер SR8878108. "
            'Категория: ЗНО и приоритет: 4.Стандартный. '
            "Регламентный срок 48 рабочих часов."
        )
        case.last_crpt_email_body = ack_body

        db.add_all([
            CrptEmail(
                case_id=case.id, direction=CrptEmail.DIRECTION_OUT,
                message_id="<seed-out-1@belor.ru>",
                from_addr="markirovka@belor.ru", to_addr="support@crpt.ru",
                subject=case.subject, body_text=case.body_text,
                event_type="outgoing", received_at=case.created_at,
            ),
            CrptEmail(
                case_id=case.id, direction=CrptEmail.DIRECTION_IN,
                message_id="<seed-in-1@crpt.ru>",
                from_addr="support@crpt.ru", to_addr="markirovka@belor.ru",
                subject="Обращение SR8878108 принято в работу",
                body_text=ack_body,
                parsed_sr_number="SR8878108",
                parsed_category="ЗНО", parsed_priority="4.Стандартный",
                event_type="ack", received_at=first_response,
            ),
        ])
        db.commit()
        print(f"Создан кейс SR8878108 (id={case.id}), deadline_48h={case.deadline_48h}")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
