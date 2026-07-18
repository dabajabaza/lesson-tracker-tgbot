import { table, integer, text, json, boolean, index, uniqueIndex, sql } from 'sdk/db';

// Деньги (price, remainder, amount) — всегда в копейках (integer).
// Времена — unix-секунды (unixepoch()).
// Мультитенантность: owner_id = Telegram user id; каждый пользователь бота
// видит и меняет только свои строки — все запросы фильтруются по owner_id.

export const students = table(
  'students',
  {
    id: integer('id').primaryKey({ autoIncrement: true }),
    ownerId: integer('owner_id').notNull(),
    name: text('name').notNull(),
    // Имя в нижнем регистре — уникальность без учёта регистра (п.11 ТЗ), в рамках владельца.
    nameLower: text('name_lower').notNull(),
    price: integer('price').notNull(),
    balance: integer('balance').notNull().default(0),
    remainder: integer('remainder').notNull().default(0),
    lastPaymentAt: integer('last_payment_at'),
    lastPaymentAmount: integer('last_payment_amount'),
    lastPaymentLessons: integer('last_payment_lessons'),
    createdAt: integer('created_at').notNull().default(sql`(unixepoch())`),
  },
  (t) => ({
    ownerNameIdx: uniqueIndex('uidx_students_owner_name').on(t.ownerId, t.nameLower),
  })
);

export const operations = table(
  'operations',
  {
    id: integer('id').primaryKey({ autoIncrement: true }),
    ownerId: integer('owner_id').notNull(),
    studentId: integer('student_id').notNull(),
    type: text('type').notNull(), // payment | charge | refund | price_change
    amount: integer('amount'),
    lessonsDelta: integer('lessons_delta').notNull().default(0),
    balanceAfter: integer('balance_after').notNull(),
    remainderAfter: integer('remainder_after').notNull(),
    newPrice: integer('new_price'),
    // Полная копия строки students до операции — для отмены (п.16 ТЗ).
    snapshotBefore: json('snapshot_before').notNull(),
    undone: boolean('undone').notNull().default(false),
    createdAt: integer('created_at').notNull().default(sql`(unixepoch())`),
  },
  (t) => ({
    byStudent: index('idx_operations_student').on(t.studentId),
    byOwner: index('idx_operations_owner').on(t.ownerId, t.undone),
  })
);

// FSM: что бот ждёт от пользователя в данном чате.
export const sessions = table('sessions', {
  chatId: integer('chat_id').primaryKey(),
  state: text('state'),
  payload: json('payload'),
});
