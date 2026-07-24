# Lesson Tracker Telegram Bot

Telegram-бот учёта оплат учеников преподавателя английского языка (учёт по
абонементам: оплаты, денежный остаток, списание/возврат занятий, история,
отмена, поиск, сортировки, экспорт). ТЗ — `selfhosted/docs/technical-specification.txt`.

Реализация — [`selfhosted/`](selfhosted/): Python, aiogram 3 + SQLAlchemy, long
polling, работает как systemd-сервис `lesson-tracker-selfhosted` (@LessonTracker42Bot).
Мультитенантный (данные каждого преподавателя изолированы). Подробности и запуск —
в [`selfhosted/README.md`](selfhosted/README.md).

> Ранее в репозитории была вторая, serverless-реализация на JavaScript (под
> Telegram Serverless). С переездом на self-hosted 2026-07-21 она выведена из
> эксплуатации и удалена; при необходимости её можно поднять из истории git.
