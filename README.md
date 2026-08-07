# Lesson Tracker Telegram Bot

Telegram-бот учёта оплат учеников преподавателя английского языка: учёт по
абонементам — оплаты, денежный остаток, списание и возврат занятий, история,
отмена, поиск, сортировки, экспорт. ТЗ — [`docs/technical-specification.txt`](docs/technical-specification.txt).

Python, **aiogram 3** + SQLAlchemy, long polling. Мультитенантный: данные каждого
преподавателя изолированы. Работает как [@LessonTracker42Bot](https://t.me/LessonTracker42Bot).

> Ранее в репозитории были две реализации: serverless на JavaScript (под Telegram
> Serverless) и эта, self-hosted, в каталоге `selfhosted/`. Serverless выведена из
> эксплуатации 2026-07-21 и удалена, а self-hosted поднята в корень репозитория —
> вложенный каталог перестал нести смысл, когда остался единственным. Обе можно
> поднять из истории git.

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

Автономное восстановление после сетевых сбоев (смена wifi↔кабель, провал прокси) —
два уровня:
- **Быстрое самолечение**: таймауты ужаты (`session_timeout=15` + `polling_timeout=20`),
  так что мёртвый long-poll сокет отваливается за ~35с (а не за дефолтные ~90с) и
  aiogram переподключается сам.
- **Жёсткий бэкстоп — watchdog** (`bot/watchdog.py`): бот шлёт `WATCHDOG=1` только пока
  реально достукивается до Telegram через прокси (проба `getMe` раз в 30с). Завис
  дольше `WatchdogSec` — супервизор убивает процесс и поднимает заново. Работает и с
  systemd, и с `sdnotify-supervise` на FreeBSD-сервере. Без супервизора (запуск
  вручную) watchdog — no-op.

## Структура

```
bot/
├─ __main__.py     — точка входа (long polling, обработка ошибок)
├─ config.py       — токен/БД/прокси из окружения
├─ db.py           — движок и сессии SQLAlchemy
├─ models.py       — Student, Operation (owner_id, снимок для отмены)
├─ repo.py         — бизнес-логика (оплата, списание, отмена…)
├─ access.py       — белый список и одноразовые инвайты
├─ admin.py        — команды /invite, /allow, /access
├─ views.py        — экраны (меню, карточка, история, поиск)
├─ keyboards.py    — inline-клавиатуры и схема callback data
├─ render.py       — тексты, статусы, форматирование (МСК)
├─ money.py        — деньги в копейках: разбор/формат/лимит
├─ csv_export.py, export_data.py — выгрузка в CSV/Excel
├─ middlewares.py  — сессия БД на апдейт (commit/rollback), контроль доступа
├─ states.py       — FSM-состояния
├─ watchdog.py     — heartbeat супервизору
└─ handlers.py     — команды, ввод, вся навигация по кнопкам
docs/              — ТЗ
tests/             — pytest на бизнес-логику (in-memory SQLite)
```

Код лежит в `src/bot/` (src-раскладка); пакет намеренно называется `bot`, а не
`lesson_tracker` — на сервере rc.d запускает `python -m bot`, и переименование
пакета делается только вместе с правкой rc.d.

## Запуск

Зависимости управляются [uv](https://docs.astral.sh/uv/); `requirements.txt`
порождается из `uv.lock` (для сервера, где uv нет) и проверяется в CI на дрейф.

```bash
uv sync

# токен — в переменной BOT_TOKEN или в файле .bot-token в корне репозитория
uv run python -m bot
```

Миграции применяются автоматически при старте (`alembic upgrade head`).

В этой сети прямой доступ к `api.telegram.org` режется провайдером — запускать через
локальный прокси (для прокси aiogram требует пакет `aiohttp-socks`, он в requirements):

```bash
TELEGRAM_PROXY=http://127.0.0.1:1080 python -m bot
```

Переменные окружения: `BOT_TOKEN`, `DATABASE_URL` (по умолчанию `data.sqlite` в корне
репозитория), `TELEGRAM_PROXY`, `ADMIN_IDS`.

Вторую копию запустить нельзя: при старте бот берёт эксклюзивный лок — абстрактный
unix-сокет в Linux, `flock` на остальных системах (на FreeBSD абстрактных сокетов нет).
Это защита от конфликта getUpdates (Telegram 409) и параллельной записи в БД.

## Тесты

```bash
uv run pytest
```

Схему тестам даёт настоящая цепочка миграций (не `create_all`), а обработчики
гоняются через настоящий `Dispatcher` — см. `tests/conftest.py`.

## Деплой

Боевой экземпляр живёт на домашнем FreeBSD-сервере в jail, запускается через rc.d и
`sdnotify-supervise`, выкатывается автоматически по тегу. Юнит
`lesson-tracker-selfhosted.service` оставлен для запуска под systemd на Linux:

```bash
cp lesson-tracker-selfhosted.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now lesson-tracker-selfhosted
```

### Перенос данных из serverless

Разовый скрипт `migrate_from_serverless.py` копировал учеников и историю из БД
serverless-версии; выполнен при переезде 2026-07-21 и оставлен для истории.
