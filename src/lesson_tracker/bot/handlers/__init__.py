"""Сборка обработчиков. Порядок подключения роутеров имеет значение.

`menu` идёт первым: у /start нет фильтра состояния, и он обязан выигрывать у
FSM-обработчиков. Подключи его после `students` — и «/start» посреди диалога
добавления был бы принят за имя ученика.

`fallback` идёт последним: он отвечает на любой нераспознанный колбэк, чтобы
кнопка не «крутилась» вечно. Раньше эту роль играла ветка `else` в общем
if/elif, теперь — отдельный роутер в хвосте.

Между ними порядок безразличен: обработчики различаются по префиксу
callback_data и по состоянию FSM, пересечений нет.

`admin` лежит в этом каталоге, но в список НЕ входит намеренно: он
подключается отдельным роутером и ПЕРВЫМ в build_dispatcher, потому что его
команды обязаны прерывать начатый диалог. Дописав его сюда «для полноты», вы
поставите его вторым — и /invite съест catch-all главного роутера.
"""

from aiogram import Router

from lesson_tracker.bot.middlewares import ClearStateOnCallbackMiddleware

from . import export, fallback, history, menu, payments, students

router = Router()

# Любое нажатие (кроме noop) прерывает незавершённый текстовый ввод — иначе
# «Введите сумму» для ученика A пережило бы переход к ученику B. Раньше это
# делала одна строка в начале общего обработчика; теперь — middleware, чтобы
# правило не забыли в новом обработчике.
router.callback_query.outer_middleware(ClearStateOnCallbackMiddleware())

router.include_router(menu.router)
router.include_router(students.router)
router.include_router(payments.router)
router.include_router(history.router)
router.include_router(export.router)
router.include_router(fallback.router)

__all__ = ["router"]
