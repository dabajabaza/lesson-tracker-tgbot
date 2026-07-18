// Нажатия inline-кнопок — вся навигация бота.
// Схема callback data описана в lib/keyboards.js.
// Бот мультитенантный: все данные читаются/меняются только в рамках
// владельца (query.from.id), поэтому чужой callback до чужих данных не дотянется.

import { api, BotApiError } from 'sdk';
import { setSession, clearSession } from 'lib/session';
import {
  getStudent,
  chargeLesson,
  refundLesson,
  peekLastOperation,
  undoLastOperation,
  listStudents,
  getAllOperations,
} from 'lib/students';
import { mainMenuView, cardView, historyView } from 'lib/views';
import {
  cancelKeyboard,
  undoConfirmKeyboard,
  exportKeyboard,
  sortMenuKeyboard,
} from 'lib/keyboards';
import { describeOperation, formatDateTime } from 'lib/render';
import { formatMoney, toRubles } from 'lib/money';
import { toCsv, chunkText } from 'lib/csv';

export default async function (query) {
  if (!query?.from) return;
  const chatId = query.message?.chat?.id;
  const messageId = query.message?.message_id;
  const uid = query.from.id;

  const answer = (text, alert = false) =>
    api.answerCallbackQuery({
      callback_query_id: query.id,
      ...(text ? { text } : {}),
      ...(alert ? { show_alert: true } : {}),
    });

  if (!chatId || !messageId) {
    await answer();
    return;
  }

  // Telegram отвечает 400 «message is not modified» при повторном нажатии — игнорируем.
  const edit = async (view) => {
    try {
      await api.editMessageText({
        chat_id: chatId,
        message_id: messageId,
        text: view.text,
        reply_markup: view.reply_markup,
      });
    } catch (e) {
      const desc = (e && (e.description || e.message)) || '';
      if (e instanceof BotApiError && /not modified/i.test(desc)) return;
      throw e;
    }
  };

  const [cmd, a1, a2] = (query.data || '').split(':');
  const id = a1 ? parseInt(a1, 10) : null;
  const page = a2 ? parseInt(a2, 10) || 0 : 0;

  switch (cmd) {
    case 'noop':
      await answer();
      return;

    case 'home':
      await clearSession(chatId);
      await edit(await mainMenuView(uid));
      await answer();
      return;

    case 'list':
      await edit(await mainMenuView(uid, a1, page));
      await answer();
      return;

    case 'card':
      await clearSession(chatId);
      await edit(await cardView(uid, id));
      await answer();
      return;

    case 'hist':
      await edit(await historyView(uid, id, page));
      await answer();
      return;

    case 'pay': {
      const s = await getStudent(uid, id);
      if (!s) {
        await answer('Ученик не найден', true);
        return;
      }
      await setSession(chatId, 'await_payment_amount', { studentId: id, msgId: messageId });
      const remainderNote = s.remainder > 0 ? `\nДенежный остаток ${formatMoney(s.remainder)} будет учтён.` : '';
      await edit({
        text: `💵 ${s.name}\nСтоимость занятия: ${formatMoney(s.price)}.${remainderNote}\n\nВведите сумму оплаты:`,
        reply_markup: cancelKeyboard(),
      });
      await answer();
      return;
    }

    case 'charge': {
      const s = await chargeLesson(uid, id);
      if (!s) {
        await answer('Ученик не найден', true);
        return;
      }
      await edit(await cardView(uid, id));
      await answer(
        s.balance < 0
          ? `⚠️ Урок списан. Долг: ${Math.abs(s.balance)}`
          : `➖ Урок списан. Осталось: ${s.balance}`
      );
      return;
    }

    case 'refund': {
      const s = await refundLesson(uid, id);
      if (!s) {
        await answer('Ученик не найден', true);
        return;
      }
      await edit(await cardView(uid, id));
      await answer(`↩️ Урок возвращён. Осталось: ${s.balance}`);
      return;
    }

    case 'price': {
      const s = await getStudent(uid, id);
      if (!s) {
        await answer('Ученик не найден', true);
        return;
      }
      await setSession(chatId, 'await_price_change', { studentId: id, msgId: messageId });
      await edit({
        text: `✏️ ${s.name}\nТекущая стоимость: ${formatMoney(s.price)}.\n\nВведите новую стоимость занятия:`,
        reply_markup: cancelKeyboard(),
      });
      await answer();
      return;
    }

    case 'add':
      await setSession(chatId, 'await_new_name', { msgId: messageId });
      await edit({ text: '➕ Введите имя нового ученика:', reply_markup: cancelKeyboard() });
      await answer();
      return;

    case 'search':
      await setSession(chatId, 'await_search', { msgId: messageId });
      await edit({ text: '🔍 Введите имя или его часть:', reply_markup: cancelKeyboard() });
      await answer();
      return;

    case 'sortmenu':
      await edit({ text: '↕️ Выберите сортировку:', reply_markup: sortMenuKeyboard() });
      await answer();
      return;

    case 'cancel':
      await clearSession(chatId);
      await edit(await mainMenuView(uid));
      await answer('Отменено');
      return;

    case 'undo': {
      const op = await peekLastOperation(uid);
      if (!op) {
        await answer('Отменять нечего — операций ещё не было', true);
        return;
      }
      const s = await getStudent(uid, op.studentId);
      await edit({
        text: `↩️ Отменить последнее действие?\n\n${describeOperation(op, s?.name || '?')}`,
        reply_markup: undoConfirmKeyboard(),
      });
      await answer();
      return;
    }

    case 'undo_yes': {
      const op = await undoLastOperation(uid);
      if (!op) {
        await edit(await mainMenuView(uid));
        await answer('Отменять нечего', true);
        return;
      }
      await edit(await cardView(uid, op.studentId));
      await answer('✅ Действие отменено');
      return;
    }

    case 'undo_no':
      await edit(await mainMenuView(uid));
      await answer();
      return;

    case 'export':
      await edit({ text: '📤 Что экспортировать?', reply_markup: exportKeyboard() });
      await answer();
      return;

    case 'exp_students':
      await sendStudentsCsv(uid, chatId);
      await answer('Готово');
      return;

    case 'exp_history':
      await sendHistoryCsv(uid, chatId);
      await answer('Готово');
      return;

    case 'exp_backup':
      await api.sendMessage({ chat_id: chatId, text: '💾 Полный бэкап: ученики + история операций.' });
      await sendStudentsCsv(uid, chatId);
      await sendHistoryCsv(uid, chatId);
      await answer('Бэкап выгружен');
      return;

    default:
      await answer();
  }
}

async function sendChunks(chatId, header, csv) {
  await api.sendMessage({ chat_id: chatId, text: header });
  for (const chunk of chunkText(csv)) {
    await api.sendMessage({ chat_id: chatId, text: chunk });
  }
}

// Экспорт учеников (п.17 ТЗ). Пока Telegram Serverless не поддерживает отправку
// файлов из обработчиков — выгружаем CSV текстом, его можно вставить в Excel.
async function sendStudentsCsv(uid, chatId) {
  const all = await listStudents(uid, 'name');
  if (!all.length) {
    await api.sendMessage({ chat_id: chatId, text: 'Учеников пока нет — экспортировать нечего.' });
    return;
  }
  const rows = [
    ['ID', 'Имя', 'Стоимость занятия, руб', 'Осталось занятий', 'Денежный остаток, руб', 'Последняя оплата', 'Сумма последней оплаты, руб', 'Занятий в последней оплате'],
  ];
  for (const s of all) {
    rows.push([
      s.id,
      s.name,
      toRubles(s.price),
      s.balance,
      toRubles(s.remainder),
      s.lastPaymentAt ? formatDateTime(s.lastPaymentAt) : '',
      s.lastPaymentAmount != null ? toRubles(s.lastPaymentAmount) : '',
      s.lastPaymentLessons ?? '',
    ]);
  }
  await sendChunks(chatId, '👥 Ученики (CSV, разделитель «;») — скопируйте в файл .csv:', toCsv(rows));
}

const TYPE_LABELS = {
  payment: 'Оплата',
  charge: 'Списание урока',
  refund: 'Возврат урока',
  price_change: 'Изменение стоимости',
};

// Экспорт истории операций (п.17–18 ТЗ).
async function sendHistoryCsv(uid, chatId) {
  const ops = await getAllOperations(uid);
  if (!ops.length) {
    await api.sendMessage({ chat_id: chatId, text: 'Операций пока нет — экспортировать нечего.' });
    return;
  }
  const names = new Map((await listStudents(uid, 'name')).map((s) => [s.id, s.name]));
  const rows = [
    ['Дата и время', 'ID ученика', 'Имя', 'Операция', 'Сумма, руб', 'Изменение занятий', 'Занятий после', 'Денежный остаток после, руб', 'Новая стоимость, руб', 'Отменено'],
  ];
  for (const op of ops) {
    rows.push([
      formatDateTime(op.createdAt),
      op.studentId,
      names.get(op.studentId) || op.snapshotBefore?.name || '',
      TYPE_LABELS[op.type] || op.type,
      op.amount != null ? toRubles(op.amount) : '',
      op.lessonsDelta || 0,
      op.balanceAfter,
      toRubles(op.remainderAfter),
      op.newPrice != null ? toRubles(op.newPrice) : '',
      op.undone ? 'да' : '',
    ]);
  }
  await sendChunks(chatId, '📜 История операций (CSV, разделитель «;»):', toCsv(rows));
}
