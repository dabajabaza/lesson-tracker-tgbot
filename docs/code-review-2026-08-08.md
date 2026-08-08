# Код-ревью всего проекта — 2026-08-08

- **Область**: вся кодовая база на текущем состоянии ветки `feat/unit-of-work-fsm` (68 файлов, diff от пустого дерева, `uv.lock` исключён), а не diff ветки.
- **Метод**: workflow-ревью на уровне **xhigh** — 6 независимых finder-агентов (5 ракурсов корректности + cleanup) → независимая верификация каждого кандидата → контрольный sweep-проход → синтез.
- **Модели**: 66 субагентов на `claude-opus-5`, координатор — `claude-fable-5`.
- **Статистика**: 68 кандидатов → 68 верифицировано → 5 опровергнуто → 63 подтверждённых свёрнуты в **15 уникальных дефектов** (14 CONFIRMED, 1 PLAUSIBLE).

## Итог

Главный кластер проблем — новый конвейер «отправка после коммита»:

- безусловный `BEGIN IMMEDIATE` превращает «читающую» FSM-сессию aiogram (до общего замка) во второго несериализованного писателя SQLite, который молча роняет апдейты;
- незащищённый пост-коммитный `_settle` превращает успешные денежные операции в подсказку «попробуйте ещё раз» — приглашение к двойной оплате;
- строки outbox готовы к отправке фоновым сендером сразу после коммита, поэтому обычные ответы дублируются, а устаревшие `EditMessageText` перерисовывают карточки с неактуальными балансами.

Отдельные одиночные поломки: мёртвый путь deep-link-инвайта (`AccessMiddleware` получает `Update`, а не `Message`), миграция outbox без inspector-гарда (бот не стартует), systemd-юнит без `ADMIN_IDS` (бот никому не отвечает), временные 5xx/429 Telegram на старте считаются фатальными, а `fileConfig` alembic'а навсегда отключает все логгеры приложения в проде.

Maintainability-замечания (дрейф документации по откаченным решениям L4/L5, мёртвый код, дубли тест-хелперов, «вакуумные» тесты middleware, мелкие неэффективности) не вошли в отчёт из-за капа в 15 находок — приоритет отдан корректности.

---

## Находки (по убыванию серьёзности)

### 1. `src/bot/storage.py:86` — CONFIRMED

**FSM-чтение берёт write-lock SQLite вне общего замка приложения.**

`ReadOnlyFsmView`'s "read-only" session actually takes SQLite's RESERVED write lock (verified empirically), and it runs in aiogram's FSM outer middleware — before `ContainerMiddleware`/`DbSessionMiddleware`, i.e. outside the app-wide write lock. *(Same root cause also at: `src/bot/db.py:50`.)*

The class docstring (lines 17–18) claims «Читает короткой собственной сессией (чтение в WAL блокировок записи не берёт)». That is false because `create_db`'s `begin` listener (`db.py:50`) issues `BEGIN IMMEDIATE` unconditionally, including for read-only autobegin. Verified: with a session built by `create_db` holding only `session.get(FsmRecord, ...)`, a separate `sqlite3` connection doing `BEGIN IMMEDIATE` fails, and succeeds again after the session closes. `Dispatcher.__init__` registers `FSMContextMiddleware` as an outer middleware on `dp.update` before `build_dispatcher` adds `ContainerMiddleware` (`__main__.py:170`), and it calls `await context.get_state()` for every update, so every incoming update grabs the SQLite write lock outside `write_lock`.

**Последствие**: two concurrent updates serialize on SQLite instead of on the asyncio lock, and while any write transaction is held longer than `busy_timeout=5000` ms the other update raises `OperationalError: database is locked` inside the FSM outer middleware — before `DbSessionMiddleware` exists, so there is no rollback path, no `ProcessedUpdate` marker, and the user only sees «⚠️ Не получилось выполнить действие».

### 2. `src/bot/middlewares.py:167` — CONFIRMED

**Незащищённый `_settle()`: его сбой провоцирует повторный ввод уже применённой оплаты.**

`_settle()` is called with no exception guard, so a failure of the "harmless" post-send bookkeeping transaction escapes into aiogram's error handler after the money operation was already committed and the reply already delivered.

Tutor sends "1600" in `Flow.payment_amount`. `_run` commits the payment and the outbox row; `ui.flush` delivers «✅ Оплата 1 600 ₽ внесена». Then `_settle` opens its second session and its `session.commit()` (line 145) raises — e.g. `OperationalError: database is locked` because an external writer held the write lock past `busy_timeout`. Nothing catches it: `__call__` has no try around lines 166–167, so the exception reaches aiogram's `ErrorsMiddleware`, and `__main__.on_error` posts «⚠️ Не получилось выполнить действие. Попробуйте ещё раз.» into the same chat. The tutor sees a success message immediately followed by a retry prompt for an operation that succeeded, re-enters 1600, and the payment is applied twice. The docstring at lines 129–133 asserts losing this transaction is «безобидна в обе стороны», which the code does not implement.

### 3. `src/bot/outbox.py:82` — CONFIRMED

**Строки outbox готовы к отправке мгновенно — тик поллера дублирует обычные ответы.**

Outbox rows are eligible for the background sender the instant they are committed (`next_attempt_at` defaults to insert time), so a poll tick landing inside the normal post-commit send window delivers the same reply twice in steady-state operation, not just after a crash. *(Same root cause also at: `src/bot/ui.py:162`.)*

`OutboxMessage.next_attempt_at` (`models.py:196`) defaults to `now_ts()`, i.e. the row is due immediately. Middleware commits the row inside the lock, releases the lock, then spends the Telegram round trip (~200–400 ms) in `ui.flush` outside the lock. `run_sender` wakes every `POLL_INTERVAL=5 s` and `deliver_batch` selects `next_attempt_at <= now`, so if a tick lands in that window it reads the still-present row and `await bot(method)` sends it too. Tutor enters an amount and receives «✅ Оплата 1 600 ₽ внесена» plus the student card twice (~1 in 15 replies at a 300 ms send window / 5 s poll) — for money flows a duplicated confirmation is precisely the ambiguous signal the design says must not happen. No grace offset is applied on insert.

### 4. `src/bot/middlewares.py:226` — CONFIRMED

**Deep-link-инвайт мёртв: `AccessMiddleware` получает `Update`, а не `Message`.**

`_invite_code_from_start` expects a `Message`, but `AccessMiddleware` is registered on `dp.update`, so it always receives an `Update` and never finds the deep-link code — the one-time invite path is dead. *(Same root cause also at: `tests/test_access.py:111`.)*

Verified by driving the real dispatcher (`build_dispatcher` + fed `Update`): an admin issues `/invite`, the invitee opens `https://t.me/bot?start=<code>` with a valid, unexpired code, and the bot silently ignores them forever — the log prints `Отказано в доступе: user_id=999 тип=Update инвайт=False`, no `allowed_users` row appears and the invite is never redeemed. Since the deep link is the only self-service entry for non-admins, every invited teacher is locked out and the admin must fall back to `/allow`. The regression is invisible to tests because `tests/test_access.py::_pass_through` calls the middleware with a bare `Message` — a vacuum test.

### 5. `src/bot/outbox.py:105` — CONFIRMED

**Фоновый сендер воспроизводит устаревший `EditMessageText` поверх нового экрана.**

The background sender replays a stale `EditMessageText` against a message the user has since navigated away from, reverting the visible screen to an old state. *(Same root cause also at: `src/bot/ui.py:120`.)*

Tutor taps «➖ Списать урок»; the charge commits and the durable `EditMessageText` row is written, but the immediate send fails on a network blip so the row survives with `next_attempt_at = now+60 s`. The tutor taps «⬅️ К списку»; that update edits the SAME `message_id` to the main menu and succeeds. 60 s later `deliver_batch` revives the queued card edit and re-applies it, so the message the tutor is looking at flips from the student list back to the old student card (with the pre-navigation keyboard). Nothing in `_revive`/`deliver_batch` checks whether the target message still holds the content the edit was computed for, and `EditMessageText` is unconditionally marked durable in `Responder.edit` (`ui.py:111–122`).

### 6. `migrations/versions/2026-08-08_03-47-52_5b3c10d2ced8_outbox.py:24` — CONFIRMED

**Миграция outbox — единственная без inspector-гарда; падает на БД с уже существующей таблицей.**

The outbox migration has no inspector guard, unlike the two migrations before it, so it is not safe against a database whose tables already exist.

Reproduced: create the DB the way `migrate_from_serverless.py` still does (`bot.db.init_models` / `create_all` against the bot's default `data.sqlite`), then start the bot — baseline and processed_updates recognise the existing tables and just stamp, but this revision runs `CREATE TABLE outbox` and alembic aborts with `table outbox already exists`. `main()` raises before `asyncio.run`, systemd (`Restart=always`) restarts it forever, and the bot never comes up; the same holds for any DB copy where `outbox` already exists.

### 7. `lesson-tracker-selfhosted.service:31` — CONFIRMED

**systemd-юнит без `ADMIN_IDS` — бот никому не отвечает.**

The unit was never updated after access control became mandatory: it sets only `TELEGRAM_PROXY`, so `ADMIN_IDS` is empty on this deployment path.

Following the README (`cp lesson-tracker-selfhosted.service ~/.config/systemd/user/ ; systemctl --user enable --now`) starts a bot with `admin_ids == frozenset()`: `AccessMiddleware` silently drops every update, including the owner's, and because `/allow` and `/invite` are admin-only there is no in-bot way to let anyone in. The user sees a bot that never answers anything; the only trace is one WARNING in the journal.

### 8. `src/bot/__main__.py:73` — CONFIRMED

**Временные 5xx/429 Telegram на старте фатальны — ретраев нет.**

`_establish_connection` re-raises anything that is a `TelegramAPIError` but not a `TelegramNetworkError`, yet `TelegramServerError` and `TelegramRetryAfter` are direct `TelegramAPIError` subclasses — so transient Telegram 5xx/429 at startup kill the process instead of being retried.

Telegram returns 502 (`TelegramServerError`) or 429 (`TelegramRetryAfter`) on the startup `getMe` — routine right after a deploy restart. The condition treats both as fatal, the exception escapes `asyncio.run`, and the process exits with a traceback instead of backing off. With `Restart=always`/`RestartSec=15`/`StartLimitBurst=60`/`StartLimitIntervalSec=900`, a Telegram outage of ~15 minutes burns the whole restart budget and systemd leaves the unit in `failed` — the bot stays down until someone runs `systemctl reset-failed`, which is precisely the outcome the retry loop claims to prevent.

### 9. `migrations/env.py:61` — CONFIRMED

**`fileConfig` alembic'а отключает все логгеры приложения в проде.**

`fileConfig(config.config_file_name)` runs with Python's default `disable_existing_loggers=True` during `main()`'s startup migration, which permanently disables every already-created `bot.*`, `watchdog` and `aiogram.*` logger and resets the root logger to alembic's WARNING/format for the rest of the process — and `_run_bot()`'s second `logging.basicConfig(...)` cannot undo it. *(Same root cause also at: `src/bot/__main__.py:199`.)*

Verified by replaying `main()`'s exact sequence (`logging.basicConfig(INFO)` → `_run_migrations(db_url)`): after the call, `bot.middlewares`, `bot.outbox`, `bot.admin`, `watchdog`, `aiogram.dispatcher`, `aiogram.event` all have `disabled=True` and root level 30. In production this silences exactly the operator signals the design relies on: access-denial warnings, «Апдейт уже применён — повтор отброшен», outbox delivery failures, the watchdog's failed-probe warning, `/invite`-`/allow` audit lines, and aiogram's «Failed to fetch updates». The test suite cannot catch it because `tests/schema.py::apply_migrations` injects a connection and takes the branch that skips `fileConfig` — fixed for pytest, never for the production path.

### 10. `src/bot/admin.py:39` — CONFIRMED

**`/invite`, `/allow`, `/access` не очищают живое FSM-состояние диалога.**

The admin router's `/invite`, `/allow` and `/access` handlers have no `StateFilter` and do not clear FSM state, and because the admin router is included before the main one they swallow the update entirely — so a command typed mid-dialog leaves `Flow.payment_amount` and `student_id` live, unlike `/start` and `/menu` which clear deliberately.

An admin taps «➕ Внести оплату» for Ани (state `Flow.payment_amount`), then types `/invite` instead of the amount. `admin_router` matches first, `cmd_invite` returns a link, and propagation stops, so `state.clear()` never runs. The admin's next plain message — e.g. `500` pasted for some unrelated reason — is routed to `on_payment_amount`, parsed as 500 ₽ and committed as a real payment against the student, with the confirmation card as the only hint.

### 11. `src/bot/middlewares.py:195` — CONFIRMED

**`StateFilter` матчится по устаревшему снимку `raw_state`.**

`FsmSessionMiddleware` forwards `data["state"]` onto the request-scoped storage but leaves `data["raw_state"]` (and `data["fsm_storage"]`) as produced by `ReadOnlyFsmView`'s separate pre-lock session, so aiogram's `StateFilter` matches on a stale snapshot.

Verified through the real Dispatcher: in `Flow.new_price`, two quick messages ("1600", "1700") whose `raw_state` reads both land before either commits. The loser still matches `@router.message(Flow.new_price)` on the stale snapshot, finds `data["name"]` gone (the winner already ran `state.clear()`), and takes the recovery branch: it sets state back to `Flow.new_name` and replies «Введите имя ученика:». Observed end state: student created, FSM row = `('Flow:new_name', {})`. The teacher has just successfully added a student yet is asked for a name again, and the bot is parked in the add-student dialog — their next unrelated message is silently swallowed as a new student's name.

### 12. `src/bot/ui.py:163` — CONFIRMED

**Outbox-строка теряет `on_error`: «message is not modified» ретраится все 24 часа TTL.**

The outbox row drops the intent's `on_error` policy, so a replayed `EditMessageText` that Telegram answers "message is not modified" is treated as a hard failure and retried with backoff for the whole 24 h TTL instead of being counted as delivered. *(Same root cause also at: `src/bot/outbox.py:109`.)*

`persist` stores only `method` name and payload; `_Pending.on_error == "not_modified"` (the tolerance `Responder._send` applies at `ui.py:227`) is lost. Scenario: a card edit fails to send once, so the row stays queued; the tutor presses the same button again and the card gets edited to byte-identical text. From then on every `deliver_batch` attempt gets `TelegramBadRequest: message is not modified`, which falls into the generic `except Exception` at `outbox.py:109`, so the row is never marked done — it keeps being retried and WARNING-logged at 60 s, 2 m, 4 m … up to the hourly cap for the full TTL=24 h, filling the operator log with false delivery failures for a reply the user already has.

### 13. `src/bot/handlers/export.py:42` — CONFIRMED

**Провалившийся экспорт всё равно отвечает пользователю «Готово».**

Non-durable intents (documents, callback answers) whose send fails are only logged by `Responder.flush`, while later intents in the buffer still succeed — so a failed export reports «Готово» to the user with no file and no error. *(Same root cause also at: `src/bot/ui.py:209`.)*

User taps «📊 Excel (.xlsx)». `_send_export` enqueues two `SendDocument` intents (`durable=False`, and `SendDocument` is absent from `outbox._METHODS`), then line 42 enqueues `AnswerCallbackQuery("Готово")`. `Responder.flush` (`ui.py:206–216`) sends in order and swallows every exception with `log.warning(...); continue`, so a failed upload (timeout, proxy blip, 413 on a large history) is dropped silently and the next intent still fires: the button shows «Готово» and the tutor is told the export succeeded while no file ever arrives and nothing is retried.

### 14. `src/bot/__main__.py:256` — CONFIRMED

**Миграции выполняются до захвата single-instance-замка.**

`main()` runs `alembic upgrade head` before `_acquire_single_instance_lock()`, so the single-instance guard no longer covers the migration step it is documented to cover (L2 claims L9 excludes a second copy racing on `upgrade head`).

Before the alembic switch, the lock was the first thing `main()` did; now a second copy (manual `python -m bot` next to the systemd unit, or a deploy restarting while the old process lingers) opens the live database and applies migrations while the first copy is serving, and only afterwards dies with «Бот уже запущен». With a pending migration this is a concurrent writer against the running bot: SQLite write-lock contention (`database is locked` at startup) or, for a `render_as_batch` revision that rebuilds a table, rows written by the live process during the rebuild are dropped.

### 15. `src/bot/middlewares.py:113` — PLAUSIBLE

**Часовой prune-DELETE делит транзакцию с денежной операцией.**

The hourly `processed_updates` cleanup DELETE now shares the money transaction; the first implementation ran it in its own session precisely so it could never mix with the business change.

Once an hour `_prune`'s DELETE is executed inside the same unit of work as the user's payment. If that statement or the following commit fails (write-lock contention with an external writer, I/O error), the payment is rolled back with it: the teacher sees «Не получилось выполнить действие» for an operation whose only real problem was a maintenance DELETE, and the payment is silently not applied (also, `_pruned_at` was already advanced, so the cleanup is skipped for another hour).

---

## Опровергнутые кандидаты (5)

Эти находки finder'ы предложили, но верификация их отклонила:

1. `src/bot/db.py:24` — «`PRAGMA journal_mode=WAL` выполняется до `busy_timeout` и падает при чужом write-lock».
2. `src/bot/ui.py:176` — «`Responder.delivered()` отдаёт наружу внутренний список, протокол слива размазан по двум модулям».
3. `tests/conftest.py:78` — «Фикстура `sessionmaker` пересобирает `async_sessionmaker` вручную, выбрасывая тот, что вернул `create_db`».
4. `src/bot/access.py:96` — «`redeem_invite` делает лишний SELECT инвайта только ради `created_by`».
5. `src/bot/__main__.py:113` — «В не-Linux ветке single-instance `open(lock_path, "w")` вне `try` + truncation до захвата замка».
