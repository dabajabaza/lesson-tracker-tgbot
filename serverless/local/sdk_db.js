// Локальная реализация 'sdk/db' — DSL схемы и операторы запросов.
// Повторяет подмножество API Telegram Serverless, которое использует бот,
// чтобы schema.js/lib/handlers работали без изменений.

function makeColumn(kind, sqlName, opts = {}) {
  const col = {
    __kind: kind, // 'integer' | 'text' | 'real' | 'boolean' | 'json' | 'blob'
    __sqlName: sqlName,
    __mode: opts.mode || null,
    __notNull: false,
    __unique: false,
    __pk: false,
    __autoIncrement: false,
    __default: undefined,
    __hasDefault: false,
    primaryKey(o) {
      col.__pk = true;
      if (o && o.autoIncrement) col.__autoIncrement = true;
      return col;
    },
    notNull() {
      col.__notNull = true;
      return col;
    },
    unique() {
      col.__unique = true;
      return col;
    },
    default(v) {
      col.__default = v;
      col.__hasDefault = true;
      return col;
    },
    deprecated() {
      return col;
    },
    constraint() {
      return col;
    },
  };
  return col;
}

export const integer = (n, o) => makeColumn('integer', n, o);
export const text = (n, o) => makeColumn('text', n, o);
export const real = (n) => makeColumn('real', n);
export const float = (n) => makeColumn('real', n);
export const numeric = (n) => makeColumn('real', n);
export const boolean = (n) => makeColumn('boolean', n);
export const json = (n) => makeColumn('json', n);
export const blob = (n) => makeColumn('blob', n);

export function table(name, columns, extraCb) {
  const t = { __table: name, __columns: {}, __indexes: [] };
  for (const [prop, col] of Object.entries(columns)) {
    if (!col.__sqlName) col.__sqlName = prop;
    col.__prop = prop;
    t.__columns[prop] = col;
    t[prop] = { __colRef: true, __prop: prop, __sqlName: col.__sqlName, __table: name, __def: col };
  }
  if (extraCb) {
    const extras = extraCb(t) || {};
    for (const e of Object.values(extras)) {
      if (e && e.__index) t.__indexes.push(e);
    }
  }
  return t;
}

export const index = (name) => ({
  on: (...cols) => ({ __index: name, cols }),
});
export const uniqueIndex = (name) => ({
  on: (...cols) => ({ __index: name, cols, unique: true }),
});
export const check = () => ({});
export const unique = () => ({ on: () => ({}) });
export const primaryKey = () => ({});

// sql`(unixepoch())` в DDL-контексте — сырой фрагмент.
export function sql(strings, ...values) {
  return { __sql: true, strings, values };
}
sql.raw = (s) => ({ __sql: true, strings: [s], values: [] });

// Операторы
export const eq = (col, val) => ({ __op: 'eq', col, val });
export const ne = (col, val) => ({ __op: 'ne', col, val });
export const gt = (col, val) => ({ __op: 'gt', col, val });
export const gte = (col, val) => ({ __op: 'gte', col, val });
export const lt = (col, val) => ({ __op: 'lt', col, val });
export const lte = (col, val) => ({ __op: 'lte', col, val });
export const like = (col, val) => ({ __op: 'like', col, val });
export const isNull = (col) => ({ __op: 'isNull', col });
export const isNotNull = (col) => ({ __op: 'isNotNull', col });
export const and = (...preds) => ({ __op: 'and', preds: preds.filter(Boolean) });
export const or = (...preds) => ({ __op: 'or', preds: preds.filter(Boolean) });
export const not = (pred) => ({ __op: 'not', pred });
export const inArray = (col, vals) => ({ __op: 'in', col, vals });
export const asc = (col) => ({ __order: 'ASC', col });
export const desc = (col) => ({ __order: 'DESC', col });
