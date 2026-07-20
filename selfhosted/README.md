# Lesson Tracker Telegram Bot — self-hosted (aiogram)

Порт бота учёта оплат на **Python + aiogram 3** для self-hosted-деплоя. Функционально
повторяет `../serverless/` (тот же бот, те же сценарии ТЗ), но работает как обычный
long-polling-процесс с собственной БД и без ограничений платформы Telegram Serverless —
в частности, **экспорт отдаётся настоящими файлами** `.xlsx` и `.csv` (serverless слал
CSV текстом).

## Стек

- **aiogram 3** — Telegram Bot API, FSM, inline-кнопки.
- **SQLAlchemy 2 (async)** + **aiosqlite** — SQLite сейчас, с готовым путём на
  **PostgreSQL** (asyncpg) через `DATABASE_URL` (п.19 ТЗ).
- **openpyxl** — экспорт в Excel.

## Возможности (как в ТЗ)

Мультитенантность (данные каждого преподавателя изолированы по `owner_id`), список
учеников со статусами 🟢🟡🟠🔴, карточка, оплаты с денежным остатком, списание/возврат,
изменение стоимости (только на будущее), история, отмена последнего действия со снимком
состояния, поиск, три сортировки (с запоминанием выбора), экспорт в Excel и CSV.
Суммы — в копейках, с лимитом 10 млн ₽ и защитой CSV от формул.

Надёжность: каждая операция коммитится в БД **до** отправки ответа в Telegram (сбой
сети не теряет запись); состояние диалога (FSM) хранится в БД и переживает рестарт
сервиса; апдейты одного пользователя сериализуются (нет гонок на балансе при
двойном тапе); на SQLite включены WAL и `busy_timeout`.

## Структура

```
bot/
├─ __main__.py     — точка входа (long polling, обработка ошибок)
├─ config.py       — токен/БД/прокси из окружения
├─ db.py           — движок и сессии SQLAlchemy
├─ models.py       — Student, Operation (owner_id, снимок для отмены)
├─ repo.py         — бизнес-логика (оплата, списание, отмена…)
├─ views.py        — экраны (меню, карточка, история, поиск)
├─ keyboards.py    — inline-клавиатуры и схема callback data
├─ render.py       — тексты, статусы, форматирование (МСК)
├─ money.py        — деньги в копейках: разбор/формат/лимит
├─ csv_export.py, export_data.py — выгрузка в CSV/Excel
├─ middlewares.py  — сессия БД на апдейт (commit/rollback)
├─ states.py       — FSM-состояния
└─ handlers.py     — команды, ввод, вся навигация по кнопкам
tests/             — pytest на бизнес-логику (in-memory SQLite)
```

## Запуск

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# токен — в BOT_TOKEN или в файл selfhosted/.bot-token
python -m bot
```

В этой сети прямой доступ к `api.telegram.org` режется провайдером — запускать через
локальный прокси (для прокси aiogram требует пакет `aiohttp-socks`, он в requirements):

```bash
TELEGRAM_PROXY=http://127.0.0.1:1080 python -m bot
```

Переменные окружения: `BOT_TOKEN`, `DATABASE_URL`
(по умолчанию `sqlite+aiosqlite:///selfhosted/data.sqlite`), `TELEGRAM_PROXY`.

Вторую копию запустить нельзя: при старте бот берёт эксклюзивный лок (абстрактный
unix-сокет) и вторая копия сразу завершается с ошибкой — защита от конфликта
getUpdates (Telegram 409) и параллельной записи в БД.

### Перенос данных из serverless

Разовый скрипт `migrate_from_serverless.py` копирует учеников и историю из
`../serverless/local/data.sqlite` (выполнен при переезде 2026-07-21):

```bash
python migrate_from_serverless.py
```

> ⚠️ Одновременно может работать только **один** экземпляр бота (serverless **или**
> self-hosted) — иначе Telegram отдаёт 409 на getUpdates. Перед запуском этой версии
> остановите serverless: `systemctl --user stop lesson-tracker-bot`.

## Тесты

```bash
pip install -r requirements-dev.txt
pytest
```

## Деплой (systemd, после переезда)

Юнит — `lesson-tracker-selfhosted.service`. Устанавливать **вместо** serverless-сервиса:

```bash
systemctl --user disable --now lesson-tracker-bot          # остановить serverless
cp lesson-tracker-selfhosted.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now lesson-tracker-selfhosted
```
