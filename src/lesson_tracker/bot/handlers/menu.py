"""Меню, список учеников, сортировки и catch-all вне диалога."""

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from dishka import FromDishka

from lesson_tracker.bot.keyboards import sort_menu_kb
from lesson_tracker.bot.ui import Responder
from lesson_tracker.bot.views import main_menu_view
from lesson_tracker.services import StudentService, ViewPrefService

from ._common import as_int, menu, owner, parts_of, screen_of, show_menu

router = Router()
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)


# Регистрируется ПЕРВЫМ среди message-обработчиков и подключается первым
# роутером: у /start нет фильтра состояния, и он обязан выигрывать у
# FSM-обработчиков. Иначе «/start» посреди диалога стал бы именем ученика.
@router.message(Command("start", "menu"))
async def on_start(
    message: Message,
    state: FSMContext,
    ui: FromDishka[Responder],
    students: FromDishka[StudentService],
    prefs: FromDishka[ViewPrefService],
) -> None:
    await state.clear()
    text, kb = await menu(students, prefs, owner(message))
    ui.reply(message, text, reply_markup=kb)


@router.callback_query(F.data == "noop")
async def on_noop(cb: CallbackQuery, ui: FromDishka[Responder]) -> None:
    ui.callback(cb)


@router.callback_query(F.data == "home")
async def on_home(
    cb: CallbackQuery,
    students: FromDishka[StudentService],
    prefs: FromDishka[ViewPrefService],
    ui: FromDishka[Responder],
) -> None:
    await show_menu(cb, ui, students, prefs)


@router.callback_query(F.data.startswith("list:"))
async def on_list(
    cb: CallbackQuery,
    students: FromDishka[StudentService],
    prefs: FromDishka[ViewPrefService],
    ui: FromDishka[Responder],
) -> None:
    msg = screen_of(cb, ui)
    if msg is None:
        return
    _cmd, a1, a2 = parts_of(cb)
    sort = a1 if a1 in ("name", "bal", "due") else "name"
    page = as_int(a2) or 0
    await prefs.set(cb.from_user.id, sort, page)
    ui.edit(msg, *await main_menu_view(students, cb.from_user.id, sort, page))
    ui.callback(cb)


@router.callback_query(F.data == "sortmenu")
async def on_sort_menu(cb: CallbackQuery, ui: FromDishka[Responder]) -> None:
    msg = screen_of(cb, ui)
    if msg is None:
        return
    ui.edit(msg, "↕️ Выберите сортировку:", sort_menu_kb())
    ui.callback(cb)


@router.callback_query(F.data == "cancel")
async def on_cancel(
    cb: CallbackQuery,
    students: FromDishka[StudentService],
    prefs: FromDishka[ViewPrefService],
    ui: FromDishka[Responder],
) -> None:
    await show_menu(cb, ui, students, prefs, "Отменено")


# Вне диалога любое сообщение показывает главное меню. Фильтр по состоянию
# делает порядок регистрации неважным: в диалоге сюда ничего не долетит.
@router.message(StateFilter(None))
async def on_any_message(
    message: Message,
    students: FromDishka[StudentService],
    prefs: FromDishka[ViewPrefService],
    ui: FromDishka[Responder],
) -> None:
    text, kb = await menu(students, prefs, owner(message))
    ui.reply(message, text, reply_markup=kb)
