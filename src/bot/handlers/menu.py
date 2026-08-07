"""Меню, список учеников, сортировки и catch-all вне диалога."""

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from .. import repo
from ..keyboards import sort_menu_kb
from ..views import main_menu_view
from ._common import as_int, edit, menu, message_of, owner, parts_of

router = Router()
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)


# Регистрируется ПЕРВЫМ среди message-обработчиков и подключается первым
# роутером: у /start нет фильтра состояния, и он обязан выигрывать у
# FSM-обработчиков. Иначе «/start» посреди диалога стал бы именем ученика.
@router.message(Command("start", "menu"))
async def on_start(message: Message, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    text, kb = await menu(session, owner(message))
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "noop")
async def on_noop(cb: CallbackQuery) -> None:
    await cb.answer()


@router.callback_query(F.data == "home")
async def on_home(cb: CallbackQuery, session: AsyncSession) -> None:
    msg = message_of(cb)
    if msg is None:
        await cb.answer()
        return
    await edit(msg, *await menu(session, cb.from_user.id))
    await cb.answer()


@router.callback_query(F.data.startswith("list:"))
async def on_list(cb: CallbackQuery, session: AsyncSession) -> None:
    msg = message_of(cb)
    if msg is None:
        await cb.answer()
        return
    _cmd, a1, a2 = parts_of(cb)
    sort = a1 if a1 in ("name", "bal", "due") else "name"
    page = as_int(a2) or 0
    await repo.set_view_pref(session, cb.from_user.id, sort, page)
    await edit(msg, *await main_menu_view(session, cb.from_user.id, sort, page))
    await cb.answer()


@router.callback_query(F.data == "sortmenu")
async def on_sort_menu(cb: CallbackQuery) -> None:
    msg = message_of(cb)
    if msg is None:
        await cb.answer()
        return
    await edit(msg, "↕️ Выберите сортировку:", sort_menu_kb())
    await cb.answer()


@router.callback_query(F.data == "cancel")
async def on_cancel(cb: CallbackQuery, session: AsyncSession) -> None:
    msg = message_of(cb)
    if msg is None:
        await cb.answer()
        return
    await edit(msg, *await menu(session, cb.from_user.id))
    await cb.answer("Отменено")


# Вне диалога любое сообщение показывает главное меню. Фильтр по состоянию
# делает порядок регистрации неважным: в диалоге сюда ничего не долетит.
@router.message(StateFilter(None))
async def on_any_message(message: Message, session: AsyncSession) -> None:
    text, kb = await menu(session, owner(message))
    await message.answer(text, reply_markup=kb)
