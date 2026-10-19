"""
app/utils/work_hours.py — Расчёт рабочих часов/дней для регламентов ТП ЧЗ.

Регламент ЦРПТ считается только по рабочему времени:
    Пн–Пт, 09:00–18:00, таймзона Europe/Moscow (MSK).
Выходные пропускаются. Производственные праздники РФ пока игнорируются —
список RU_HOLIDAYS оставлен для расширения.

Все функции принимают/возвращают «наивные» datetime в локальном времени сервера
(в проекте MySQL хранит naive-датеты) либо aware-datetime — см. _to_msk().
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Optional

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
    MSK = ZoneInfo("Europe/Moscow")
except Exception:  # pragma: no cover — fallback
    import pytz
    MSK = pytz.timezone("Europe/Moscow")

WORK_START = time(9, 0)   # 09:00 MSK
WORK_END = time(18, 0)     # 18:00 MSK
WORK_HOURS_PER_DAY = 9.0   # 09:00–18:00 без перерыва

# ──────────────────────────────────────────────
# Производственные праздники РФ (для расширения).
# Формат: date(год, месяц, день). Сейчас пуст — праздники не учитываем.
# Пример: date(2026, 1, 1) … date(2026, 1, 7), date(2026, 2, 23), ...
# ──────────────────────────────────────────────
RU_HOLIDAYS: set[date] = set()


def is_holiday(d: date) -> bool:
    """Выходной ли день: суббота/воскресенье или производственный праздник."""
    return d.weekday() >= 5 or d in RU_HOLIDAYS


def _to_msk(dt: datetime) -> datetime:
    """Приводим datetime к MSK. Naive-время считаем уже московским."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=MSK)
    return dt.astimezone(MSK)


def _naive(dt: datetime) -> datetime:
    """Снимаем tzinfo (храним naive MSK — как остальные даты в БД проекта)."""
    return dt.replace(tzinfo=None)


def now_msk() -> datetime:
    """Текущее время в MSK (naive, для хранения в БД)."""
    return _naive(datetime.now(MSK))


def day_work_bounds(d: date) -> Optional[tuple[datetime, datetime]]:
    """Границы рабочего окна [09:00, 18:00] для даты d. None если день нерабочий."""
    if is_holiday(d):
        return None
    start = datetime.combine(d, WORK_START, tzinfo=MSK)
    end = datetime.combine(d, WORK_END, tzinfo=MSK)
    return start, end


def clamp_to_work(dt: datetime) -> datetime:
    """Прижать момент к ближайшему рабочему времени (вперёд, не назад)."""
    dt = _to_msk(dt)
    while True:
        bounds = day_work_bounds(dt.date())
        if bounds is None:                 # выходной — на следующее утро
            dt = datetime.combine(dt.date() + timedelta(days=1), WORK_START, tzinfo=MSK)
            continue
        start, end = bounds
        if dt < start:
            return start
        if dt >= end:                      # после 18:00 — следующее рабочее окно
            nxt = dt.date() + timedelta(days=1)
            dt = datetime.combine(nxt, WORK_START, tzinfo=MSK)
            continue
        return dt


def add_work_hours(start: datetime, hours: float) -> datetime:
    """
    Прибавить N рабочих часов к моменту start (Пн–Пт 09:00–18:00 MSK).

    >>> add_work_hours(datetime(2026, 2, 10, 15, 0), 48)  # вторник 15:00
    datetime(...)                                        # четверг следующей недели ~09:00-11:00
    """
    if hours < 0:
        raise ValueError("hours must be >= 0")

    was_naive = start.tzinfo is None
    current = clamp_to_work(start)
    remaining = float(hours)

    while remaining > 0:
        bounds = day_work_bounds(current.date())
        assert bounds is not None  # clamp_to_work гарантирует рабочий момент
        _, day_end = bounds
        available = (day_end - current).total_seconds() / 3600.0

        if remaining <= available:
            result = current + timedelta(hours=remaining)
            return _naive(result) if was_naive else result

        remaining -= available
        # переходим к следующему рабочему дню 09:00
        nxt = current.date() + timedelta(days=1)
        current = clamp_to_work(datetime.combine(nxt, WORK_START, tzinfo=MSK))

    return _naive(current) if was_naive else current  # hours == 0


def count_work_hours_between(a: datetime, b: datetime) -> float:
    """
    Количество рабочих часов (Пн–Пт 09:00–18:00 MSK) между a и b.
    Если b <= a — 0. Результат неотрицательный float.
    """
    a = _to_msk(a)
    b = _to_msk(b)
    if b <= a:
        return 0.0

    total = 0.0
    cur = a
    while cur < b:
        bounds = day_work_bounds(cur.date())
        if bounds is not None:
            day_start, day_end = bounds
            seg_start = max(cur, day_start)
            seg_end = min(b, day_end)
            if seg_end > seg_start:
                total += (seg_end - seg_start).total_seconds() / 3600.0
        cur = datetime.combine(cur.date() + timedelta(days=1), time(0, 0), tzinfo=MSK)
    return round(total, 4)


def add_work_days(start: datetime, days: int) -> datetime:
    """Прибавить N рабочих дней (Пн–Пт) к дате, сохраняя время дня."""
    was_naive = start.tzinfo is None
    cur = _to_msk(start)
    added = 0
    while added < days:
        cur = cur + timedelta(days=1)
        if not is_holiday(cur.date()):
            added += 1
    return _naive(cur) if was_naive else cur


# ──────────────────────────────────────────────
# Высокоуровневые функции для кейса CrptSupportCase
# (импортируется лениво, чтобы модуль можно было тестировать без SQLAlchemy)
# ──────────────────────────────────────────────
PAUSED_STATUSES = ("WAITING_CLARIFICATION", "RESOLVED", "CLOSED")


def get_remaining_work_hours(case) -> Optional[float]:
    """
    Остаток регламентных 48 рабочих часов для кейса.

    • WAITING_CLARIFICATION — таймер паузится: остаток фиксируется на paused_at.
    • RESOLVED / CLOSED — 0 (регламент выполнен/исчерпан).
    • IN_PROGRESS — считаем now() -> deadline_48h в рабочих часах.
    Возвращает None, если отсчёт ещё не начат (нет first_response_at/deadline).
    """
    status = getattr(case.status, "value", str(case.status))

    if status in ("RESOLVED", "CLOSED"):
        return 0.0

    if case.deadline_48h is None:
        return None

    if status == "WAITING_CLARIFICATION":
        # Заморозить остаток на моменте паузы
        anchor = case.paused_at or now_msk()
        return count_work_hours_between(anchor, _to_msk(case.deadline_48h))

    # IN_PROGRESS / OVERDUE / прочие — живой отсчёт
    return count_work_hours_between(now_msk(), _to_msk(case.deadline_48h))


def is_overdue(case) -> bool:
    """Просрочен ли регламент 48 раб. часов (для активных статусов)."""
    status = getattr(case.status, "value", str(case.status))
    if status in ("RESOLVED", "CLOSED", "NEW", "WAITING_NUMBER", "WAITING_CLARIFICATION"):
        return False
    rem = get_remaining_work_hours(case)
    if rem is None:
        return False
    return rem <= 0.0


def get_auto_close_remaining(case) -> Optional[float]:
    """
    Сколько КАЛЕНДАРНЫХ часов осталось до автозакрытия (3 рабочих дня без ответа).
    None — если кейс не в состоянии ожидания уточнения.
    """
    status = getattr(case.status, "value", str(case.status))
    if status != "WAITING_CLARIFICATION" or case.auto_close_deadline_3d is None:
        return None
    delta = _to_msk(case.auto_close_deadline_3d) - datetime.now(MSK)
    return round(delta.total_seconds() / 3600.0, 2)


def progress_percent(case, total_hours: float = 48.0) -> Optional[float]:
    """Процент ИСТЕКШЕГО регламента 0..100 для прогресс-бара."""
    rem = get_remaining_work_hours(case)
    if rem is None:
        return None
    done = max(0.0, total_hours - rem)
    return round(min(100.0, done / total_hours * 100.0), 1)
