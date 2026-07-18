import { formatMoney } from 'lib/money';

// Смещение отображаемого времени от UTC, в часах (Москва = +3).
export const TZ_OFFSET_HOURS = 3;

export function statusEmoji(balance) {
  if (balance >= 3) return '🟢';
  if (balance === 2) return '🟡';
  if (balance === 1) return '🟠';
  return '🔴';
}

// 1 занятие, 2 занятия, 5 занятий
export function lessonsWord(n) {
  const abs = Math.abs(n) % 100;
  const d = abs % 10;
  if (abs > 10 && abs < 20) return 'занятий';
  if (d === 1) return 'занятие';
  if (d >= 2 && d <= 4) return 'занятия';
  return 'занятий';
}

// ts — unix-время в секундах (unixepoch из SQLite).
export function formatDateTime(ts) {
  if (!ts) return '—';
  const d = new Date((ts + TZ_OFFSET_HOURS * 3600) * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getUTCDate())}.${p(d.getUTCMonth() + 1)}.${d.getUTCFullYear()} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
}

export function formatDate(ts) {
  return formatDateTime(ts).split(' ')[0];
}

// Подпись кнопки в списке учеников.
export function listButtonLabel(s) {
  return `${statusEmoji(s.balance)} ${s.name} · ${s.balance} зан. · ${formatMoney(s.price)}`;
}

// Текст карточки ученика (п.5 ТЗ).
export function renderCard(s) {
  const lines = [
    `👤 ${s.name}`,
    '',
    `${statusEmoji(s.balance)} Осталось занятий: ${s.balance}`,
    `💵 Стоимость занятия: ${formatMoney(s.price)}`,
    `💰 Денежный остаток: ${formatMoney(s.remainder)}`,
  ];
  if (s.balance < 0) lines.push(`⚠️ Долг: ${Math.abs(s.balance)} ${lessonsWord(s.balance)}`);
  lines.push('');
  if (s.lastPaymentAt) {
    lines.push(
      `📅 Последняя оплата: ${formatDate(s.lastPaymentAt)}`,
      `   Сумма: ${formatMoney(s.lastPaymentAmount)}`,
      `   Добавлено занятий: ${s.lastPaymentLessons}`
    );
  } else {
    lines.push('📅 Оплат ещё не было');
  }
  return lines.join('\n');
}

const OP_LABELS = {
  payment: 'Оплата',
  charge: 'Списан урок',
  refund: 'Возврат урока',
  price_change: 'Изменение стоимости',
};

// Строка истории операций (п.10 ТЗ).
export function renderOperation(op) {
  let body;
  switch (op.type) {
    case 'payment':
      body = `${OP_LABELS.payment} ${formatMoney(op.amount)}: +${op.lessonsDelta} ${lessonsWord(op.lessonsDelta)}, остаток ${formatMoney(op.remainderAfter)}`;
      break;
    case 'charge':
      body = `${OP_LABELS.charge}: −1 занятие (осталось ${op.balanceAfter})`;
      break;
    case 'refund':
      body = `${OP_LABELS.refund}: +1 занятие (осталось ${op.balanceAfter})`;
      break;
    case 'price_change':
      body = `${OP_LABELS.price_change}: ${formatMoney(op.snapshotBefore.price)} → ${formatMoney(op.newPrice)}`;
      break;
    default:
      body = op.type;
  }
  const line = `${formatDateTime(op.createdAt)} — ${body}`;
  return op.undone ? `❌ ${line} (отменено)` : line;
}

// Короткое описание операции для подтверждения отмены.
export function describeOperation(op, studentName) {
  const what = {
    payment: `оплату ${formatMoney(op.amount)} (+${op.lessonsDelta} ${lessonsWord(op.lessonsDelta)})`,
    charge: 'списание урока',
    refund: 'возврат урока',
    price_change: `изменение стоимости на ${formatMoney(op.newPrice)}`,
  }[op.type] || op.type;
  return `${what} — ${studentName}, ${formatDateTime(op.createdAt)}`;
}
