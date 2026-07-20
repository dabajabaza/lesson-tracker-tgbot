import { listButtonLabel } from 'lib/render';

// Схема callback data:
//   home | list:<sort>:<page> | card:<id> | pay:<id> | charge:<id> | refund:<id>
//   price:<id> | hist:<id>:<page> | add | search | sortmenu | undo | undo_yes | undo_no
//   export | exp_students | exp_history | exp_backup | cancel | noop

export const PAGE_SIZE = 10;
export const SORTS = { name: 'name', bal: 'bal', due: 'due' };

function btn(text, data) {
  return { text, callback_data: data };
}

// Ряд пагинации: рисуем только реальные стрелки — Telegram отклоняет кнопки
// с пустым текстом (BUTTON_TEXT_EMPTY).
function paginationRow(prefix, page, totalPages) {
  if (totalPages <= 1) return null;
  const row = [];
  if (page > 0) row.push(btn('⬅️', `${prefix}:${page - 1}`));
  row.push(btn(`${page + 1}/${totalPages}`, 'noop'));
  if (page < totalPages - 1) row.push(btn('➡️', `${prefix}:${page + 1}`));
  return row;
}

// Главное меню: кнопка на каждого ученика + служебный ряд.
export function mainMenuKeyboard(students, sort, page, totalPages) {
  const rows = students.map((s) => [btn(listButtonLabel(s), `card:${s.id}`)]);
  const pagination = paginationRow(`list:${sort}`, page, totalPages);
  if (pagination) rows.push(pagination);
  rows.push([btn('➕ Добавить', 'add'), btn('🔍 Поиск', 'search'), btn('↕️ Сортировка', 'sortmenu')]);
  rows.push([btn('↩️ Отменить действие', 'undo'), btn('📤 Экспорт', 'export')]);
  return { inline_keyboard: rows };
}

export function cardKeyboard(id) {
  return {
    inline_keyboard: [
      [btn('➕ Внести оплату', `pay:${id}`)],
      [btn('➖ Списать урок', `charge:${id}`), btn('↩️ Вернуть урок', `refund:${id}`)],
      [btn('✏️ Изменить стоимость', `price:${id}`), btn('📜 История', `hist:${id}:0`)],
      [btn('⬅️ К списку', 'home')],
    ],
  };
}

export function historyKeyboard(id, page, totalPages) {
  const rows = [];
  const pagination = paginationRow(`hist:${id}`, page, totalPages);
  if (pagination) rows.push(pagination);
  rows.push([btn('⬅️ К карточке', `card:${id}`)]);
  return { inline_keyboard: rows };
}

export function sortMenuKeyboard() {
  return {
    inline_keyboard: [
      [btn('🔤 По имени', `list:${SORTS.name}:0`)],
      [btn('📚 По остатку занятий', `list:${SORTS.bal}:0`)],
      [btn('⏳ Скоро потребуется оплата', `list:${SORTS.due}:0`)],
      [btn('⬅️ Назад', 'home')],
    ],
  };
}

// В data зашит id операции, чтобы отменить именно то, что показано в диалоге,
// а не «последнее на момент клика».
export function undoConfirmKeyboard(opId) {
  return {
    inline_keyboard: [[btn('✅ Да, отменить', `undo_yes:${opId}`), btn('❌ Нет', 'undo_no')]],
  };
}

export function exportKeyboard() {
  return {
    inline_keyboard: [
      [btn('👥 Ученики (CSV)', 'exp_students')],
      [btn('📜 История операций (CSV)', 'exp_history')],
      [btn('💾 Полный бэкап', 'exp_backup')],
      [btn('⬅️ Назад', 'home')],
    ],
  };
}

// Кнопка отмены текущего текстового ввода (FSM).
export function cancelKeyboard() {
  return { inline_keyboard: [[btn('❌ Отмена', 'cancel')]] };
}

export function homeKeyboard() {
  return { inline_keyboard: [[btn('⬅️ К списку', 'home')]] };
}
