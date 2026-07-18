// Текстовые сообщения: /start и ввод по FSM-состоянию
// (сумма оплаты, имя ученика, стоимость, поисковый запрос).
// Бот мультитенантный: каждый пользователь (message.from.id) работает
// только со своими учениками.

import { api } from 'sdk';
import { getSession, setSession, clearSession } from 'lib/session';
import { parseMoneyStrict, formatMoney, MAX_MONEY } from 'lib/money';
import {
  applyPayment,
  createStudent,
  changePrice,
  findByNameLower,
  searchStudents,
} from 'lib/students';
import { mainMenuView, searchResultsView } from 'lib/views';
import { cancelKeyboard, cardKeyboard } from 'lib/keyboards';
import { renderCard, lessonsWord } from 'lib/render';

export default async function (message) {
  if (!message?.from || !message?.chat || message.chat.type !== 'private') return;
  const chatId = message.chat.id;
  const uid = message.from.id;

  const text = (message.text || '').trim();

  if (text === '/start' || text === '/menu') {
    await clearSession(chatId);
    await sendView(chatId, await mainMenuView(uid));
    return;
  }

  const session = await getSession(chatId);
  if (!session?.state) {
    await sendView(chatId, await mainMenuView(uid));
    return;
  }

  if (!text) {
    await api.sendMessage({
      chat_id: chatId,
      text: 'Отправьте, пожалуйста, текстовое сообщение.',
      reply_markup: cancelKeyboard(),
    });
    return;
  }

  const payload = session.payload || {};
  switch (session.state) {
    case 'await_payment_amount':
      await onPaymentAmount(uid, chatId, text, payload);
      return;
    case 'await_new_name':
      await onNewName(uid, chatId, text, payload);
      return;
    case 'await_new_price':
      await onNewPrice(uid, chatId, text, payload);
      return;
    case 'await_price_change':
      await onPriceChange(uid, chatId, text, payload);
      return;
    case 'await_search':
      await onSearch(uid, chatId, text, payload);
      return;
    default:
      await clearSession(chatId);
      await sendView(chatId, await mainMenuView(uid));
  }
}

async function sendView(chatId, view) {
  return await api.sendMessage({ chat_id: chatId, text: view.text, reply_markup: view.reply_markup });
}

// Убирает устаревшее приглашение «Введите...» после успешного шага.
async function deleteQuietly(chatId, messageId) {
  if (!messageId) return;
  try {
    await api.deleteMessage({ chat_id: chatId, message_id: messageId });
  } catch (e) {
    // Сообщение могло быть уже удалено — не критично.
  }
}

async function askAgain(chatId, text) {
  await api.sendMessage({ chat_id: chatId, text, reply_markup: cancelKeyboard() });
}

// Понятное объяснение, почему сумма/стоимость не принята.
function moneyErrorText(error, kind, example) {
  const noun = kind === 'price' ? 'Стоимость' : 'Сумма';
  const acc = kind === 'price' ? 'стоимость' : 'сумму';
  if (error === 'range')
    return `⚠️ Слишком большая ${noun.toLowerCase()} — максимум ${formatMoney(MAX_MONEY)}. Введите ${acc} поменьше:`;
  if (error === 'zero') return `⚠️ ${noun} должна быть больше нуля. Введите число, например: ${example}`;
  return `⚠️ Это не похоже на ${acc}. Введите число в рублях, например: ${example}`;
}

// Внесение оплаты (п.6–7 ТЗ).
async function onPaymentAmount(uid, chatId, text, payload) {
  const parsed = parseMoneyStrict(text);
  if (parsed.error) {
    await askAgain(chatId, moneyErrorText(parsed.error, 'amount', '1600'));
    return;
  }
  const amount = parsed.value;
  const res = await applyPayment(uid, payload.studentId, amount);
  await clearSession(chatId);
  if (!res) {
    await sendView(chatId, await mainMenuView(uid));
    return;
  }
  await deleteQuietly(chatId, payload.msgId);
  const s = res.student;
  const lines = [`✅ Оплата ${formatMoney(amount)} внесена.`];
  if (res.prevRemainder > 0) lines.push(`Учтён прежний остаток: ${formatMoney(res.prevRemainder)}.`);
  lines.push(`Добавлено: ${res.lessons} ${lessonsWord(res.lessons)}.`);
  if (res.remainder > 0) lines.push(`Денежный остаток: ${formatMoney(res.remainder)} — будет учтён при следующей оплате.`);
  await api.sendMessage({
    chat_id: chatId,
    text: `${lines.join('\n')}\n\n${renderCard(s)}`,
    reply_markup: cardKeyboard(s.id),
  });
}

// Добавление ученика, шаг 1: имя с проверкой уникальности без учёта регистра (п.11 ТЗ).
async function onNewName(uid, chatId, text, payload) {
  const name = text.replace(/\s+/g, ' ');
  if (name.length > 80) {
    await askAgain(chatId, '⚠️ Слишком длинное имя. Введите короче:');
    return;
  }
  if (await findByNameLower(uid, name)) {
    await askAgain(chatId, `⚠️ Ученик с именем «${name}» уже существует. Введите другое имя:`);
    return;
  }
  await deleteQuietly(chatId, payload.msgId);
  const sent = await api.sendMessage({
    chat_id: chatId,
    text: `Имя: ${name}\n\nТеперь введите стоимость одного занятия, например: 1600`,
    reply_markup: cancelKeyboard(),
  });
  await setSession(chatId, 'await_new_price', { name, msgId: sent?.message_id });
}

// Добавление ученика, шаг 2: стоимость занятия.
async function onNewPrice(uid, chatId, text, payload) {
  const parsed = parseMoneyStrict(text);
  if (parsed.error) {
    await askAgain(chatId, moneyErrorText(parsed.error, 'price', '1600'));
    return;
  }
  const student = await createStudent(uid, payload.name, parsed.value);
  if (!student) {
    await setSession(chatId, 'await_new_name', { msgId: payload.msgId });
    await askAgain(chatId, `⚠️ Имя «${payload.name}» уже занято. Введите другое имя:`);
    return;
  }
  await clearSession(chatId);
  await deleteQuietly(chatId, payload.msgId);
  await api.sendMessage({
    chat_id: chatId,
    text: `✅ Ученик добавлен.\n\n${renderCard(student)}`,
    reply_markup: cardKeyboard(student.id),
  });
}

// Изменение стоимости (п.12 ТЗ): только будущие оплаты, история не пересчитывается.
async function onPriceChange(uid, chatId, text, payload) {
  const parsed = parseMoneyStrict(text);
  if (parsed.error) {
    await askAgain(chatId, moneyErrorText(parsed.error, 'price', '1800'));
    return;
  }
  const price = parsed.value;
  const s = await changePrice(uid, payload.studentId, price);
  await clearSession(chatId);
  if (!s) {
    await sendView(chatId, await mainMenuView(uid));
    return;
  }
  await deleteQuietly(chatId, payload.msgId);
  await api.sendMessage({
    chat_id: chatId,
    text: `✅ Стоимость изменена: ${formatMoney(price)}.\nПрименяется только к будущим оплатам.\n\n${renderCard(s)}`,
    reply_markup: cardKeyboard(s.id),
  });
}

// Поиск по имени (п.14 ТЗ).
async function onSearch(uid, chatId, text, payload) {
  const found = await searchStudents(uid, text);
  await clearSession(chatId);
  await deleteQuietly(chatId, payload.msgId);
  await sendView(chatId, searchResultsView(found, text));
}
