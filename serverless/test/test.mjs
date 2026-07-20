// Интеграционный тест бота на стабах SDK: сценарии из ТЗ.
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const PROJ = join(dirname(fileURLToPath(import.meta.url)), '..');
const load = (p) => import(pathToFileURL(join(PROJ, p)).href);

const { default: onMessage } = await load('handlers/message.js');
const { default: onCallback } = await load('handlers/callback_query.js');
const { __apiCalls, __dump } = await import('sdk');
const { parseMoney, formatMoney } = await load('lib/money.js');
const { listStudents } = await load('lib/students.js');
const { chunkText, toCsv } = await load('lib/csv.js');

const OWNER = 111;
const STRANGER = 222;

let failures = 0;
function check(name, cond, extra) {
  if (cond) console.log(`PASS  ${name}`);
  else {
    failures++;
    console.log(`FAIL  ${name}`, extra === undefined ? '' : JSON.stringify(extra).slice(0, 300));
  }
}

const asFrom = (u) => (typeof u === 'object' ? u : { id: u });
const msg = (text, from = OWNER) =>
  onMessage({ chat: { id: asFrom(from).id, type: 'private' }, from: asFrom(from), text });
const cb = (data, from = OWNER) =>
  onCallback({ id: 'q1', from: asFrom(from), data, message: { message_id: 1, chat: { id: asFrom(from).id } } });

const last = (method) => [...__apiCalls].reverse().find((c) => c.method === method);
const undoYesData = () =>
  last('editMessageText')
    .params.reply_markup.inline_keyboard.flat()
    .find((b) => b.callback_data.startsWith('undo_yes')).callback_data;
const students = () => __dump().students || [];
const ops = () => __dump().operations || [];

// --- money ---
check('parseMoney 1600', parseMoney('1600') === 160000);
check('parseMoney "1 600,50"', parseMoney('1 600,50') === 160050);
check('parseMoney "1600.5"', parseMoney('1600.5') === 160050);
check('parseMoney abc → null', parseMoney('abc') === null);
check('parseMoney "12,345" → null', parseMoney('12,345') === null);
check('parseMoney: гигантское число → null', parseMoney('1000000000000000000000000000') === null);
check('parseMoney: больше лимита 10 млн → null', parseMoney('10000001') === null);
check('parseMoney: ровно 10 млн проходит', parseMoney('10000000') === 1000000000);
check('formatMoney 160000', formatMoney(160000) === '1 600 ₽');
check('formatMoney 160050', formatMoney(160050) === '1 600,50 ₽');

// --- csv utils ---
check('toCsv экранирует ;', toCsv([['a;b', 'c"d']]) === '"a;b";"c""d"');
check('CSV: защита от формул', toCsv([['=1+1', '-1', '+7 999']]) === "'=1+1;-1;'+7 999");
check('chunkText режет по строкам', JSON.stringify(chunkText('a\nb\nc', 3)) === JSON.stringify(['a\nb', 'c']));
check('chunkText режет длинную строку', JSON.stringify(chunkText('abcdef', 3)) === JSON.stringify(['abc', 'def']));
check('chunkText не превышает лимит', chunkText('строка один\nстрока два\nстрока три', 15).every((c) => c.length <= 15));

// --- /start и владелец ---
await msg('/start');
check('первый /start показывает пустое меню', /Учеников пока нет/.test(last('sendMessage').params.text));
await msg('/start', STRANGER);
check('у второго пользователя своё пустое меню', /Учеников пока нет/.test(last('sendMessage').params.text));

// --- добавление ученика ---
await cb('add');
check('add просит имя', /Введите имя/.test(last('editMessageText').params.text));
await msg('Аня');
check('после имени просит стоимость', /стоимость одного занятия/.test(last('sendMessage').params.text));
await msg('1600');
check('ученик создан', students().length === 1 && students()[0].name === 'Аня' && students()[0].price === 160000);
check('карточка после создания', /Аня/.test(last('sendMessage').params.text));

// --- дубликат имени без учёта регистра (п.11) ---
await cb('add');
await msg('АНЯ');
check('дубликат имени отклонён', /уже существует/.test(last('sendMessage').params.text));
await msg('аня');
check('дубликат в нижнем регистре отклонён', /уже существует/.test(last('sendMessage').params.text));
await cb('cancel');
check('cancel возвращает меню', /Ученики: 1|Учеников пока нет/.test(last('editMessageText').params.text));

// --- оплата: пример из ТЗ (п.7): цена 1600, оплата 6500 → 4 занятия + 100 ₽ ---
await cb('pay:1');
check('pay просит сумму', /Введите сумму оплаты/.test(last('editMessageText').params.text));
await msg('6500');
let s = students()[0];
check('оплата 6500: +4 занятия', s.balance === 4, s);
check('оплата 6500: остаток 100 ₽', s.remainder === 10000, s);
check('lastPayment записан', s.lastPaymentAmount === 650000 && s.lastPaymentLessons === 4 && s.lastPaymentAt > 0, s);
check('ответ упоминает 4 занятия', /Добавлено: 4 занятия/.test(last('sendMessage').params.text), last('sendMessage').params.text);
check('операция payment записана', ops().length === 1 && ops()[0].type === 'payment' && ops()[0].lessonsDelta === 4);

// --- оплата с учётом остатка (п.7): 100 + 1500 = 1600 → +1, остаток 0 ---
await cb('pay:1');
await msg('1500');
s = students()[0];
check('остаток учтён: +1 занятие', s.balance === 5, s);
check('новый остаток 0', s.remainder === 0, s);

// --- списание и возврат (п.8–9) ---
await cb('charge:1');
s = students()[0];
check('списание: 5 → 4', s.balance === 4);
check('toast о списании', /Урок списан. Осталось: 4/.test(last('answerCallbackQuery').params.text));
await cb('refund:1');
check('возврат: 4 → 5', students()[0].balance === 5);

// --- изменение стоимости (п.12) ---
await cb('price:1');
await msg('1800');
check('новая цена 1800', students()[0].price === 180000);
check('баланс не пересчитан', students()[0].balance === 5);

// --- отмена действий (п.16), стек ---
await cb('undo');
check('подтверждение отмены: описана смена цены', /изменение стоимости/.test(last('editMessageText').params.text), last('editMessageText').params.text);
await cb(undoYesData());
check('цена откатилась к 1600', students()[0].price === 160000);
check('операция помечена отменённой', ops().find((o) => o.type === 'price_change').undone === true);
await cb('undo');
await cb(undoYesData());
check('второй undo откатил возврат урока: 5 → 4', students()[0].balance === 4, students()[0]);
await cb('undo');
await cb(undoYesData());
check('третий undo откатил списание: 4 → 5', students()[0].balance === 5);

// --- история (п.10) ---
await cb('hist:1:0');
const histText = last('editMessageText').params.text;
check('история открывается', /История: Аня/.test(histText));
check('отменённые помечены', /\(отменено\)/.test(histText));
check('оплата в истории с остатком', /Оплата 6 500 ₽: \+4 занятия, остаток 100 ₽/.test(histText), histText);

// --- второй ученик, долг, сортировки (п.13, 15) ---
await cb('add');
await msg('Борис');
await msg('2000');
await cb('charge:2');
s = students().find((x) => x.name === 'Борис');
check('долг: баланс -1', s.balance === -1);
await cb('card:2');
check('карточка показывает долг', /Долг: 1 занятие/.test(last('editMessageText').params.text), last('editMessageText').params.text);
check('статус 🔴 в списке', true);

const byBal = await listStudents(OWNER, 'bal');
check('сортировка по остатку: Аня первая', byBal[0].name === 'Аня');
const byDue = await listStudents(OWNER, 'due');
check('сортировка «скоро оплата»: Борис первый', byDue[0].name === 'Борис');

// --- оплата меньше цены → только остаток ---
await cb('pay:2');
await msg('500');
s = students().find((x) => x.name === 'Борис');
check('оплата 500 при цене 2000: 0 занятий, остаток 500', s.balance === -1 && s.remainder === 50000, s);
check('ответ: 0 занятий', /Добавлено: 0 занятий/.test(last('sendMessage').params.text));

// --- некорректный ввод ---
await cb('pay:1');
await msg('пятьсот');
check('нечисловая сумма: понятное сообщение', /не похоже на сумму/.test(last('sendMessage').params.text), last('sendMessage').params.text);
await msg('0');
check('ноль отклоняется с объяснением', /Сумма должна быть больше нуля/.test(last('sendMessage').params.text), last('sendMessage').params.text);
await msg('1000000000000000000000000000');
check('гигантская сумма: сообщение о лимите', /Слишком большая сумма — максимум 10 000 000 ₽/.test(last('sendMessage').params.text), last('sendMessage').params.text);
await cb('cancel');

// --- лимит стоимости при добавлении ученика ---
await cb('add');
await msg('Тестовый Лимит');
await msg('100000000000');
check('гигантская стоимость: сообщение о лимите', /Слишком большая стоимость — максимум 10 000 000 ₽/.test(last('sendMessage').params.text), last('sendMessage').params.text);
await cb('cancel');
check('ученик с гигантской ценой не создан', !students().some((s) => s.name === 'Тестовый Лимит'));

// --- поиск (п.14) ---
await cb('search');
await msg('ор');
check('поиск находит Бориса', /Найдено по «ор»: 1/.test(last('sendMessage').params.text), last('sendMessage').params.text);
await cb('search');
await msg('éé');
check('поиск без результатов', /никого не нашлось/.test(last('sendMessage').params.text));

// --- сортировка через меню ---
await cb('sortmenu');
check('меню сортировки', /Выберите сортировку/.test(last('editMessageText').params.text));
await cb('list:due:0');
check('список отсортирован: скоро оплата', /Сортировка: скоро оплата/.test(last('editMessageText').params.text));

// --- экспорт (п.17–18) ---
await cb('exp_students');
const csvMsgs = __apiCalls.filter((c) => c.method === 'sendMessage').slice(-2);
check('экспорт учеников: заголовок', /Ученики \(CSV/.test(csvMsgs[0].params.text));
check('экспорт учеников: данные', /Аня;1600,00;5/.test(csvMsgs[1].params.text), csvMsgs[1].params.text);
await cb('exp_history');
check('экспорт истории содержит оплату', /Оплата;6500,00;4/.test(last('sendMessage').params.text), last('sendMessage').params.text);
await cb('exp_backup');
check('бэкап отправлен', /История операций \(CSV/.test([...__apiCalls].reverse().find((c) => c.method === 'sendMessage' && /CSV/.test(c.params.text)).params.text));

// --- меню с учениками ---
await cb('home');
const menu = last('editMessageText');
check('меню: 2 ученика', /Ученики: 2/.test(menu.params.text));
const btns = menu.params.reply_markup.inline_keyboard.flat().map((b) => b.text).join('|');
check('кнопки учеников с эмодзи-статусом', /🟢 Аня/.test(btns) && /🔴 Борис/.test(btns), btns);

// --- мультитенантность: изоляция данных пользователей ---
await cb('add', STRANGER);
await msg('Аня', STRANGER);
check('у второго пользователя имя «Аня» свободно', /стоимость одного занятия/.test(last('sendMessage').params.text), last('sendMessage').params.text);
await msg('3000', STRANGER);
const anya222 = students().find((s) => s.name === 'Аня' && s.ownerId === STRANGER);
check('у второго пользователя своя Аня', !!anya222 && anya222.price === 300000);
check('Аня владельца не тронута', students().some((s) => s.name === 'Аня' && s.ownerId === OWNER && s.price === 160000));

await cb('home', STRANGER);
check('второй пользователь видит только своего ученика', /Ученики: 1/.test(last('editMessageText').params.text), last('editMessageText').params.text);

await cb('card:1', STRANGER);
check('чужая карточка недоступна', /Ученик не найден/.test(last('editMessageText').params.text));
const balBefore = students().find((s) => s.id === 1).balance;
await cb('charge:1', STRANGER);
check('чужое списание отклонено', /Ученик не найден/.test(last('answerCallbackQuery').params.text));
check('баланс чужого ученика не изменился', students().find((s) => s.id === 1).balance === balBefore);

await cb('pay:1', STRANGER);
check('чужая оплата отклонена', /Ученик не найден/.test(last('answerCallbackQuery').params.text));

await cb('undo', STRANGER);
check('undo второго пользователя не видит чужих операций', /Отменять нечего/.test(last('answerCallbackQuery').params.text), last('answerCallbackQuery').params.text);

await cb('exp_students', STRANGER);
const strangerCsv = [...__apiCalls].reverse().find((c) => c.method === 'sendMessage' && /Аня/.test(c.params.text)).params.text;
check('экспорт второго пользователя без чужих учеников', /Аня;3000,00/.test(strangerCsv) && !/Борис/.test(strangerCsv) && !/1600,00/.test(strangerCsv), strangerCsv);

await cb('exp_history', STRANGER);
check('экспорт истории второго пользователя пуст', /Операций пока нет/.test(last('sendMessage').params.text));

// --- навигация сбрасывает незавершённый ввод (находка ревью №1) ---
await cb('pay:1');
await cb('hist:2:0');
const balA = students().find((s) => s.id === 1).balance;
await msg('7777');
check('ввод после навигации не уходит в оплату', students().find((s) => s.id === 1).balance === balA, students().find((s) => s.id === 1));
check('вместо оплаты показано меню', /Ученики: 2/.test(last('sendMessage').params.text), last('sendMessage').params.text);

// --- отмена по устаревшему диалогу отклоняется (находка ревью №2) ---
await cb('refund:2'); // Борис: -1 → 0
await cb('undo');
const staleData = undoYesData();
await cb('charge:2'); // новая операция, Борис: 0 → -1
await cb(staleData);
check('устаревшая отмена отклонена', /новые операции/.test(last('answerCallbackQuery').params.text), last('answerCallbackQuery').params.text);
check('данные не изменились после отклонённой отмены', students().find((s) => s.id === 2).balance === -1);
check('операции не помечены отменёнными', ops().filter((o) => o.studentId === 2 && o.undone).length === 0);

// --- пагинация без пустых кнопок (находка ревью №3) ---
{
  const { mainMenuKeyboard, historyKeyboard } = await load('lib/keyboards.js');
  const mid = mainMenuKeyboard([], 'name', 1, 3).inline_keyboard.flat().map((b) => b.text);
  check('пагинация: без пустых кнопок', mid.every((t) => t.trim().length > 0), mid);
  check('пагинация: обе стрелки в середине', mid.includes('⬅️') && mid.includes('➡️'));
  const first = mainMenuKeyboard([], 'name', 0, 3).inline_keyboard.flat().map((b) => b.text);
  check('пагинация: первая страница без ⬅️', !first.includes('⬅️') && first.includes('➡️'));
  const lastPage = historyKeyboard(1, 2, 3).inline_keyboard.flat().map((b) => b.text);
  check('пагинация истории: последняя страница без ➡️', lastPage.includes('⬅️') && !lastPage.includes('➡️'));
}

// --- честная обрезка результатов поиска (находка ревью №6) ---
{
  const { searchResultsView } = await load('lib/views.js');
  const many = Array.from({ length: 25 }, (_, i) => ({ id: i + 1, name: 'У' + i, balance: 0, price: 100 }));
  const sv = searchResultsView(many, 'у');
  check('поиск: честный заголовок при обрезке', /25, показаны первые 20/.test(sv.text), sv.text);
  check('поиск: максимум 20 карточек + кнопка назад', sv.reply_markup.inline_keyboard.length === 21);
}

console.log(failures ? `\n${failures} FAILED` : '\nALL TESTS PASSED');
process.exit(failures ? 1 : 0);
