// Стаб sdk: in-memory db + api-рекордер. Повторяет использованное подмножество API платформы.

import { matches } from './sdk_db.js';

// ---------- in-memory база ----------

const store = new Map(); // tableName -> { rows: [], seq: number }

function bucket(t) {
  if (!store.has(t.__table)) store.set(t.__table, { rows: [], seq: 0 });
  return store.get(t.__table);
}

export function __resetDb() {
  store.clear();
}

export function __dump() {
  return Object.fromEntries([...store.entries()].map(([k, v]) => [k, v.rows]));
}

function now() {
  return Math.floor(Date.now() / 1000);
}

function resolveValue(v) {
  if (v && typeof v === 'object' && '__sql' in v) {
    if (v.__sql.includes('unixepoch')) return now();
    throw new Error('неизвестный sql-фрагмент: ' + v.__sql);
  }
  return v;
}

function applyDefaults(t, row) {
  const out = { ...row };
  for (const [prop, col] of Object.entries(t.__columns)) {
    if (out[prop] === undefined) {
      if (col.__autoIncrement) {
        out[prop] = ++bucket(t).seq;
      } else if (col.__hasDefault) {
        out[prop] = resolveValue(col.__default);
      } else {
        out[prop] = null;
      }
    }
  }
  // JSON-колонки: глубокая копия, как это сделала бы сериализация
  for (const [prop, col] of Object.entries(t.__columns)) {
    if (col.__kind === 'json' && out[prop] !== null && out[prop] !== undefined) {
      out[prop] = JSON.parse(JSON.stringify(out[prop]));
    }
  }
  return out;
}

function checkUnique(t, row, ignoreRow) {
  for (const [prop, col] of Object.entries(t.__columns)) {
    if ((col.__unique || col.__pk || col.__autoIncrement) && row[prop] != null) {
      const clash = bucket(t).rows.find((r) => r !== ignoreRow && r[prop] === row[prop]);
      if (clash) {
        const err = new Error(`UNIQUE constraint failed: ${t.__table}.${col.__name}`);
        err.unique = true;
        throw err;
      }
    }
  }
}

class SelectQuery {
  constructor() {
    this._pred = null;
    this._order = null;
    this._limit = Infinity;
    this._offset = 0;
  }
  from(t) {
    this._t = t;
    return this;
  }
  where(pred) {
    this._pred = pred;
    return this;
  }
  orderBy(...orders) {
    this._orders = orders;
    return this;
  }
  limit(n) {
    this._limit = n;
    return this;
  }
  offset(n) {
    this._offset = n;
    return this;
  }
  _exec() {
    let rows = bucket(this._t).rows.filter((r) => matches(r, this._pred));
    if (this._orders) {
      const orders = this._orders.map((o) =>
        o.__order ? o : { __order: 'asc', col: o }
      );
      rows = [...rows].sort((a, b) => {
        for (const o of orders) {
          const k = o.col.__col;
          const cmp = a[k] < b[k] ? -1 : a[k] > b[k] ? 1 : 0;
          if (cmp) return o.__order === 'desc' ? -cmp : cmp;
        }
        return 0;
      });
    }
    return rows
      .slice(this._offset, this._offset + this._limit)
      .map((r) => JSON.parse(JSON.stringify(r)));
  }
  async all() {
    return this._exec();
  }
  async get() {
    return this._exec()[0];
  }
  async values() {
    return this._exec().map((r) => Object.values(r));
  }
}

export const db = {
  select() {
    return new SelectQuery();
  },
  insert(t) {
    return {
      values(vals) {
        const list = Array.isArray(vals) ? vals : [vals];
        const doInsert = () => {
          const inserted = [];
          for (const v of list) {
            const row = applyDefaults(t, Object.fromEntries(Object.entries(v).map(([k, x]) => [k, resolveValue(x)])));
            checkUnique(t, row, null);
            bucket(t).rows.push(row);
            inserted.push(JSON.parse(JSON.stringify(row)));
          }
          return inserted;
        };
        const self = {
          async run() {
            doInsert();
            return [];
          },
          returning() {
            return {
              async run() {
                return doInsert();
              },
            };
          },
          onConflictDoUpdate({ target, set }) {
            return {
              async run() {
                for (const v of list) {
                  const key = target.__col;
                  const existing = bucket(t).rows.find((r) => r[key] === v[key]);
                  if (existing) {
                    for (const [k, x] of Object.entries(set)) existing[k] = resolveValue(x);
                  } else {
                    const row = applyDefaults(t, v);
                    bucket(t).rows.push(row);
                  }
                }
              },
            };
          },
        };
        return self;
      },
    };
  },
  update(t) {
    return {
      set(vals) {
        return {
          where(pred) {
            return {
              async run() {
                for (const r of bucket(t).rows.filter((r) => matches(r, pred))) {
                  for (const [k, x] of Object.entries(vals)) r[k] = resolveValue(x);
                  checkUnique(t, r, r);
                }
              },
            };
          },
        };
      },
    };
  },
  delete(t) {
    return {
      where(pred) {
        return {
          async run() {
            const b = bucket(t);
            b.rows = b.rows.filter((r) => !matches(r, pred));
          },
        };
      },
    };
  },
  async $count(t, pred) {
    return bucket(t).rows.filter((r) => matches(r, pred)).length;
  },
};

// ---------- Bot API рекордер ----------

export class BotApiError extends Error {
  constructor(code, description) {
    super(description);
    this.code = code;
    this.description = description;
  }
}

export const __apiCalls = [];
let msgSeq = 1000;

export function __resetApi() {
  __apiCalls.length = 0;
}

export const api = new Proxy(
  {},
  {
    get(_, method) {
      return async (params) => {
        __apiCalls.push({ method, params });
        if (method === 'sendMessage') return { message_id: ++msgSeq, chat: { id: params.chat_id } };
        if (method === 'editMessageText') return true;
        return true;
      };
    },
  }
);

export const fetch = () => {
  throw new Error('fetch не используется в этом боте');
};
