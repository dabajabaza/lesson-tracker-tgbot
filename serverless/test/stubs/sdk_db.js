// Стаб sdk/db: table-DSL + предикаты + in-memory защёлки для стаба db в sdk.js

export function table(name, columns, extra) {
  const t = { __table: name, __columns: columns };
  for (const [prop, col] of Object.entries(columns)) {
    t[prop] = { __col: prop, __table: name, __def: col };
  }
  return t;
}

function makeColumn(kind, name, opts) {
  const col = {
    __kind: kind,
    __name: name,
    __opts: opts,
    __default: undefined,
    __hasDefault: false,
    __autoIncrement: false,
    primaryKey(o) {
      col.__pk = true;
      if (o && o.autoIncrement) col.__autoIncrement = true;
      return col;
    },
    notNull() {
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
  };
  return col;
}

export const integer = (n, o) => makeColumn('integer', n, o);
export const text = (n) => makeColumn('text', n);
export const real = (n) => makeColumn('real', n);
export const float = (n) => makeColumn('real', n);
export const boolean = (n) => makeColumn('boolean', n);
export const json = (n) => makeColumn('json', n);
export const blob = (n) => makeColumn('blob', n);

export const index = (name) => ({ on: (...cols) => ({ __index: name, cols }) });
export const uniqueIndex = (name) => ({ on: (...cols) => ({ __index: name, cols, unique: true }) });

export function sql(strings, ...values) {
  return { __sql: strings.join('?'), values };
}

// Предикаты
export const eq = (col, val) => ({ __pred: 'eq', col, val });
export const and = (...preds) => ({ __pred: 'and', preds });
export const or = (...preds) => ({ __pred: 'or', preds });
export const desc = (col) => ({ __order: 'desc', col });
export const asc = (col) => ({ __order: 'asc', col });
export const count = (col) => ({ __agg: 'count', col });

export function matches(row, pred) {
  if (!pred) return true;
  switch (pred.__pred) {
    case 'eq':
      return row[pred.col.__col] === pred.val;
    case 'and':
      return pred.preds.every((p) => matches(row, p));
    case 'or':
      return pred.preds.some((p) => matches(row, p));
    default:
      throw new Error('unknown predicate');
  }
}
