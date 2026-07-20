// Готовые экраны: текст + inline-клавиатура. Используются обоими обработчиками.

import { listStudents, getStudent, getHistory, countHistory } from 'lib/students';
import {
  mainMenuKeyboard,
  cardKeyboard,
  historyKeyboard,
  homeKeyboard,
  PAGE_SIZE,
} from 'lib/keyboards';
import { renderCard, renderOperation, listButtonLabel } from 'lib/render';

const SORT_LABELS = { name: 'по имени', bal: 'по остатку занятий', due: 'скоро оплата' };

export async function mainMenuView(ownerId, sort = 'name', page = 0) {
  const all = await listStudents(ownerId, sort);
  if (!all.length) {
    return {
      text: 'Учеников пока нет.\n\nНажмите «➕ Добавить», чтобы создать первую карточку.',
      reply_markup: mainMenuKeyboard([], sort, 0, 1),
    };
  }
  const totalPages = Math.max(1, Math.ceil(all.length / PAGE_SIZE));
  const safePage = Math.min(Math.max(page, 0), totalPages - 1);
  const slice = all.slice(safePage * PAGE_SIZE, (safePage + 1) * PAGE_SIZE);
  return {
    text: `👩‍🏫 Ученики: ${all.length}\nСортировка: ${SORT_LABELS[sort] || SORT_LABELS.name}`,
    reply_markup: mainMenuKeyboard(slice, sort, safePage, totalPages),
  };
}

export async function cardView(ownerId, id) {
  const s = await getStudent(ownerId, id);
  if (!s) return { text: 'Ученик не найден.', reply_markup: homeKeyboard() };
  return { text: renderCard(s), reply_markup: cardKeyboard(s.id) };
}

const HISTORY_PAGE_SIZE = 10;

export async function historyView(ownerId, id, page = 0) {
  const s = await getStudent(ownerId, id);
  if (!s) return { text: 'Ученик не найден.', reply_markup: homeKeyboard() };
  const total = await countHistory(ownerId, id);
  if (!total) {
    return { text: `📜 ${s.name}: операций ещё не было.`, reply_markup: historyKeyboard(id, 0, 1) };
  }
  const totalPages = Math.max(1, Math.ceil(total / HISTORY_PAGE_SIZE));
  const safePage = Math.min(Math.max(page, 0), totalPages - 1);
  const ops = await getHistory(ownerId, id, safePage, HISTORY_PAGE_SIZE);
  const lines = ops.map(renderOperation).join('\n\n');
  return {
    text: `📜 История: ${s.name}\n\n${lines}`,
    reply_markup: historyKeyboard(id, safePage, totalPages),
  };
}

// Результаты поиска — те же кнопки-карточки, что и в главном меню.
const MAX_SEARCH_RESULTS = 20;

export function searchResultsView(found, query) {
  const shown = found.slice(0, MAX_SEARCH_RESULTS);
  const rows = shown.map((s) => [{ text: listButtonLabel(s), callback_data: `card:${s.id}` }]);
  rows.push([{ text: '⬅️ К списку', callback_data: 'home' }]);
  let text;
  if (!found.length) text = `🔍 По запросу «${query}» никого не нашлось.`;
  else if (found.length > MAX_SEARCH_RESULTS)
    text = `🔍 Найдено по «${query}»: ${found.length}, показаны первые ${MAX_SEARCH_RESULTS}. Уточните запрос.`;
  else text = `🔍 Найдено по «${query}»: ${found.length}`;
  return { text, reply_markup: { inline_keyboard: rows } };
}
