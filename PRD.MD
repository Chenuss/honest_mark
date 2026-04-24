# Project Requirements Document (PRD): Журнал инцидентов «Честный знак» v3.0

## 1. Суть проекта

Внутренняя веб-система холдинга с двумя модулями:
- **Журнал инцидентов** — тикет-система для оборудования маркировки
- **Аналитика отклонений ЧЗ** — импорт CSV, справочник продукции, ежемесячные архивы, публичные ссылки

## 2. Технический стек

Backend: Python 3.11+, FastAPI | DB: MySQL + SQLAlchemy | Frontend: Bootstrap 5, Jinja2, Chart.js 4 | Data: Pandas | Auth: JWT httponly-cookie | Email: aiosmtplib | OS: Windows Server

## 3. Архитектура данных

### ProductionSite — id, name, support_email
### User — id, username, password_hash, full_name, role (worker/engineer/admin)
### LineEquipment — id, name, site_id → ProductionSite
### Ticket — id, author_id, assignee_id, equipment_id, site_id, status, description, resolution_note, expert_conclusion, recommendations, responsible_dept, created_at, updated_at, closed_at
### TicketPhoto — id, ticket_id, filename, original_name, is_image, created_at
### Product (справочник GTIN) — id, gtin (unique), name, category
### CzUploadBatch — id, filename, uploaded_by, row_count, created_at
### CzDeviation — id, batch_id, deviation_type, address, address_clean, city, registration_date, gtin, product_name, inn, org_name

## 4. Модуль: Журнал инцидентов

- Роли: worker (создаёт тикеты), engineer (решает), admin (полный доступ + удаление)
- Площадки → оборудование → тикеты
- Статусы: open → in_progress → pending_dept → resolved → closed
- Экспертное заключение + email-маршрутизация
- Загрузка фото/документов, вставка из буфера (Ctrl+V)
- Печатная форма «Акт технического осмотра»

## 5. Модуль: Аналитика отклонений ЧЗ

### 5.1 Импорт CSV
- Парсинг с автоопределением кодировки (UTF-8, CP1251)
- Гибкий маппинг колонок, очистка адресов, парсинг дат
- Автозаполнение справочника продукции (новые GTIN добавляются автоматически)

### 5.2 Архивная структура
- Вход `/deviations/dashboard` показывает навигацию: Год → Месяц → количество
- Каждый месяц — отдельная страница с диаграммами и фильтрами
- Публичная ссылка `/analytics/{год}/{месяц}` — доступна без авторизации

### 5.3 Дашборд (Chart.js)
- Doughnut: структура нарушений (по виду отклонения)
- Doughnut: география проблем (по городам)
- Bar: топ-10 проблемных адресов
- Bar: топ-10 продуктов (GTIN → название из справочника)
- KPI: общее число, самый проблемный адрес, самый частый вид
- Фильтры: вид отклонения, город, GTIN
- Палитра Soft Professional

### 5.4 Справочник продукции (GTIN)
- Таблица с поиском и пагинацией
- Поля: GTIN, название, категория
- GTIN автоматически создаются при импорте CSV
- Редактирование: клик по GTIN → страница редактирования
- На дашборде GTIN заменяются на названия из справочника

### 5.5 Публичный дашборд
- `/analytics/{год}/{месяц}` — полноценный дашборд без навбара
- Только чтение: диаграммы, KPI, фильтры
- Нет кнопок удаления, редактирования, загрузки
- Самодостаточная HTML-страница (все стили inline)

## 6. Инициализация БД (init_db.py)
- Создаёт таблицы (если не существуют)
- Запускает ALTER TABLE миграции (безопасно, с проверками)
- Создаёт admin-аккаунт (только если нет пользователей)
- **НЕ** добавляет демо-оборудование и демо-площадки
- **НЕ** трогает существующие данные

## 7. Маршруты

| Маршрут | Доступ | Описание |
|---------|--------|----------|
| `/login`, `/logout` | Публичный | Авторизация |
| `/` | Auth | Дашборд |
| `/tickets/*` | Auth | Тикеты (CRUD) |
| `/admin/sites/*` | Admin | Площадки |
| `/admin/equipment/*` | Admin | Оборудование |
| `/deviations/upload` | Eng/Admin | Загрузка CSV |
| `/deviations/dashboard` | Eng/Admin | Архив Год→Месяц |
| `/deviations/dashboard?year=&month=` | Eng/Admin | Дашборд за месяц |
| `/deviations/products` | Eng/Admin | Справочник GTIN |
| `/analytics/{year}/{month}` | **Публичный** | Дашборд (только чтение) |

## 8. Out of Scope
- API-интеграция с серверами ЧЗ
- Telegram-бот, управление пользователями через UI
- Временные ряды и прогнозирование
