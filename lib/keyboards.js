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

// Главное меню: кнопка на каждого ученика + служебный ряд.
export function mainMenuKeyboard(students, sort, page, totalPages) {
  const rows = students.map((s) => [btn(listButtonLabel(s), `card:${s.id}`)]);
  if (totalPages > 1) {
    rows.push([
      btn(page > 0 ? '⬅️' : ' ', page > 0 ? `list:${sort}:${page - 1}` : 'noop'),
      btn(`${page + 1}/${totalPages}`, 'noop'),
      btn(page < totalPages - 1 ? '➡️' : ' ', page < totalPages - 1 ? `list:${sort}:${page + 1}` : 'noop'),
    ]);
  }
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
  if (totalPages > 1) {
    rows.push([
      btn(page > 0 ? '⬅️' : ' ', page > 0 ? `hist:${id}:${page - 1}` : 'noop'),
      btn(`${page + 1}/${totalPages}`, 'noop'),
      btn(page < totalPages - 1 ? '➡️' : ' ', page < totalPages - 1 ? `hist:${id}:${page + 1}` : 'noop'),
    ]);
  }
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

export function undoConfirmKeyboard() {
  return {
    inline_keyboard: [[btn('✅ Да, отменить', 'undo_yes'), btn('❌ Нет', 'undo_no')]],
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

export function backToCardKeyboard(id) {
  return { inline_keyboard: [[btn('⬅️ К карточке', `card:${id}`)]] };
}

export function homeKeyboard() {
  return { inline_keyboard: [[btn('⬅️ К списку', 'home')]] };
}
