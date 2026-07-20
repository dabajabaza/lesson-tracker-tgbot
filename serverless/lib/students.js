import { db } from 'sdk';
import { students, operations } from 'schema';
import { eq, and, asc, desc } from 'sdk/db';

const nowTs = () => Math.floor(Date.now() / 1000);

// Все функции принимают ownerId (Telegram user id) первым аргументом —
// пользователь бота видит и меняет только свои данные.

const own = (ownerId, id) => and(eq(students.ownerId, ownerId), eq(students.id, id));

export async function getStudent(ownerId, id) {
  return await db.select().from(students).where(own(ownerId, id)).get();
}

// Ученики владельца с сортировкой (п.15 ТЗ). Их немного — сортируем в JS.
export async function listStudents(ownerId, sort = 'name') {
  const all = await db.select().from(students).where(eq(students.ownerId, ownerId)).all();
  const byName = (a, b) => a.name.localeCompare(b.name, 'ru');
  if (sort === 'bal') all.sort((a, b) => b.balance - a.balance || byName(a, b));
  else if (sort === 'due') all.sort((a, b) => a.balance - b.balance || byName(a, b));
  else all.sort(byName);
  return all;
}

// Поиск по подстроке имени без учёта регистра (п.14 ТЗ).
export async function searchStudents(ownerId, query) {
  const q = query.trim().toLowerCase();
  const all = await listStudents(ownerId, 'name');
  return all.filter((s) => s.nameLower.includes(q));
}

export async function findByNameLower(ownerId, name) {
  return await db
    .select()
    .from(students)
    .where(and(eq(students.ownerId, ownerId), eq(students.nameLower, name.trim().toLowerCase())))
    .get();
}

// Создаёт ученика; null, если имя у этого владельца занято (без учёта регистра).
export async function createStudent(ownerId, name, price) {
  const clean = name.trim();
  if (await findByNameLower(ownerId, clean)) return null;
  try {
    const rows = await db
      .insert(students)
      .values({ ownerId, name: clean, nameLower: clean.toLowerCase(), price })
      .returning()
      .run();
    return rows[0] ?? (await findByNameLower(ownerId, clean));
  } catch (e) {
    // Гонка на UNIQUE(owner_id, name_lower) — имя заняли параллельно.
    return null;
  }
}

// Записывает операцию со снимком состояния ДО — основа отмены (п.16 ТЗ).
async function recordOperation(before, fields) {
  await db
    .insert(operations)
    .values({ ownerId: before.ownerId, studentId: before.id, snapshotBefore: before, ...fields })
    .run();
}

// Оплата (п.6–7 ТЗ): к сумме добавляется денежный остаток, считаются целые занятия,
// новый остаток сохраняется.
export async function applyPayment(ownerId, id, amount) {
  const s = await getStudent(ownerId, id);
  if (!s) return null;
  const total = amount + s.remainder;
  const lessons = Math.floor(total / s.price);
  const remainder = total % s.price;
  const changes = {
    balance: s.balance + lessons,
    remainder,
    lastPaymentAt: nowTs(),
    lastPaymentAmount: amount,
    lastPaymentLessons: lessons,
  };
  await db.update(students).set(changes).where(own(ownerId, id)).run();
  await recordOperation(s, {
    type: 'payment',
    amount,
    lessonsDelta: lessons,
    balanceAfter: changes.balance,
    remainderAfter: remainder,
  });
  return { student: { ...s, ...changes }, lessons, remainder, prevRemainder: s.remainder };
}

// Списание урока (п.8). Баланс может уйти в минус — это долг, отображается в карточке.
export async function chargeLesson(ownerId, id) {
  return await shiftBalance(ownerId, id, 'charge', -1);
}

// Возврат урока (п.9).
export async function refundLesson(ownerId, id) {
  return await shiftBalance(ownerId, id, 'refund', +1);
}

async function shiftBalance(ownerId, id, type, delta) {
  const s = await getStudent(ownerId, id);
  if (!s) return null;
  await db.update(students).set({ balance: s.balance + delta }).where(own(ownerId, id)).run();
  await recordOperation(s, {
    type,
    lessonsDelta: delta,
    balanceAfter: s.balance + delta,
    remainderAfter: s.remainder,
  });
  return { ...s, balance: s.balance + delta };
}

// Изменение стоимости (п.12): действует только на будущие оплаты, история не пересчитывается.
export async function changePrice(ownerId, id, newPrice) {
  const s = await getStudent(ownerId, id);
  if (!s) return null;
  await db.update(students).set({ price: newPrice }).where(own(ownerId, id)).run();
  await recordOperation(s, {
    type: 'price_change',
    lessonsDelta: 0,
    balanceAfter: s.balance,
    remainderAfter: s.remainder,
    newPrice,
  });
  return { ...s, price: newPrice };
}

// Самая свежая неотменённая операция владельца — кандидат на отмену.
export async function peekLastOperation(ownerId) {
  return await db
    .select()
    .from(operations)
    .where(and(eq(operations.ownerId, ownerId), eq(operations.undone, false)))
    .orderBy(desc(operations.id))
    .limit(1)
    .get();
}

// Отмена: восстановить ученика из снимка, пометить операцию отменённой.
// Разрешена только для самой свежей операции владельца, поэтому снимок безопасен.
// expectedOpId — id операции, показанной в диалоге подтверждения: если с тех пор
// появились новые операции, отмена не выполняется (status: 'stale').
export async function undoLastOperation(ownerId, expectedOpId) {
  const op = await peekLastOperation(ownerId);
  if (!op) return { status: 'empty' };
  if (!Number.isFinite(expectedOpId) || op.id !== expectedOpId) return { status: 'stale' };
  const snap = op.snapshotBefore;
  await db
    .update(students)
    .set({
      name: snap.name,
      nameLower: snap.nameLower,
      price: snap.price,
      balance: snap.balance,
      remainder: snap.remainder,
      lastPaymentAt: snap.lastPaymentAt,
      lastPaymentAmount: snap.lastPaymentAmount,
      lastPaymentLessons: snap.lastPaymentLessons,
    })
    .where(own(ownerId, op.studentId))
    .run();
  await db.update(operations).set({ undone: true }).where(eq(operations.id, op.id)).run();
  return { status: 'done', op };
}

export async function countHistory(ownerId, studentId) {
  return await db.$count(
    operations,
    and(eq(operations.ownerId, ownerId), eq(operations.studentId, studentId))
  );
}

// Страница истории, новые сверху (п.10).
export async function getHistory(ownerId, studentId, page, pageSize) {
  return await db
    .select()
    .from(operations)
    .where(and(eq(operations.ownerId, ownerId), eq(operations.studentId, studentId)))
    .orderBy(desc(operations.id))
    .limit(pageSize)
    .offset(page * pageSize)
    .all();
}

// Вся история владельца для экспорта, старые сверху.
export async function getAllOperations(ownerId) {
  return await db
    .select()
    .from(operations)
    .where(eq(operations.ownerId, ownerId))
    .orderBy(asc(operations.id))
    .all();
}
