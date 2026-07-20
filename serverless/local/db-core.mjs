// Query builder поверх node:sqlite — локальная реализация `db` из 'sdk'.
// Поддерживает подмножество, которым пользуется бот: select/insert/update/delete,
// where/orderBy/limit/offset, returning, onConflictDoUpdate, $count.

import { DatabaseSync } from 'node:sqlite';

const q = (name) => `"${name}"`;

function writeValue(colDef, v) {
  if (v === undefined || v === null) return null;
  const kind = colDef ? colDef.__kind : null;
  const mode = colDef ? colDef.__mode : null;
  if (kind === 'boolean' || mode === 'boolean') return v ? 1 : 0;
  if (kind === 'json' || mode === 'json') return JSON.stringify(v);
  return v;
}

function readRow(t, row) {
  if (!row) return row;
  const out = {};
  for (const [prop, col] of Object.entries(t.__columns)) {
    const raw = row[col.__sqlName];
    if (raw === undefined) continue;
    if (raw === null) out[prop] = null;
    else if (col.__kind === 'boolean' || col.__mode === 'boolean') out[prop] = !!raw;
    else if (col.__kind === 'json' || col.__mode === 'json') out[prop] = JSON.parse(raw);
    else out[prop] = raw;
  }
  return out;
}

function buildWhere(pred, params) {
  if (!pred) return '';
  const walk = (p) => {
    switch (p.__op) {
      case 'and':
        return '(' + p.preds.map(walk).join(' AND ') + ')';
      case 'or':
        return '(' + p.preds.map(walk).join(' OR ') + ')';
      case 'not':
        return 'NOT (' + walk(p.pred) + ')';
      case 'isNull':
        return `${q(p.col.__sqlName)} IS NULL`;
      case 'isNotNull':
        return `${q(p.col.__sqlName)} IS NOT NULL`;
      case 'in':
        return (
          `${q(p.col.__sqlName)} IN (` +
          p.vals.map((v) => (params.push(writeValue(p.col.__def, v)), '?')).join(', ') +
          ')'
        );
      default: {
        const ops = { eq: '=', ne: '<>', gt: '>', gte: '>=', lt: '<', lte: '<=', like: 'LIKE' };
        const op = ops[p.__op];
        if (!op) throw new Error('неизвестный оператор: ' + p.__op);
        params.push(writeValue(p.col.__def, p.val));
        return `${q(p.col.__sqlName)} ${op} ?`;
      }
    }
  };
  return ' WHERE ' + walk(pred);
}

function ddlDefault(col) {
  if (!col.__hasDefault) return '';
  const d = col.__default;
  if (d && d.__sql) return ` DEFAULT ${d.strings.join('')}`;
  if (typeof d === 'boolean') return ` DEFAULT ${d ? 1 : 0}`;
  if (typeof d === 'number') return ` DEFAULT ${d}`;
  return ` DEFAULT '${String(d).replace(/'/g, "''")}'`;
}

const SQL_TYPES = { integer: 'INTEGER', text: 'TEXT', real: 'REAL', boolean: 'INTEGER', json: 'TEXT', blob: 'BLOB' };

export function createDb(path) {
  const raw = new DatabaseSync(path);
  raw.exec('PRAGMA journal_mode = WAL;');

  function migrate(tables) {
    for (const t of tables) {
      const cols = Object.values(t.__columns).map((c) => {
        let s = `${q(c.__sqlName)} ${SQL_TYPES[c.__kind] || 'TEXT'}`;
        if (c.__pk) s += ' PRIMARY KEY' + (c.__autoIncrement ? ' AUTOINCREMENT' : '');
        if (c.__notNull) s += ' NOT NULL';
        if (c.__unique) s += ' UNIQUE';
        s += ddlDefault(c);
        return s;
      });
      raw.exec(`CREATE TABLE IF NOT EXISTS ${q(t.__table)} (${cols.join(', ')});`);
      for (const idx of t.__indexes) {
        const cols = idx.cols.map((c) => q(c.__sqlName)).join(', ');
        raw.exec(
          `CREATE ${idx.unique ? 'UNIQUE ' : ''}INDEX IF NOT EXISTS ${q(idx.__index)} ON ${q(t.__table)} (${cols});`
        );
      }
    }
  }

  class SelectQuery {
    from(t) {
      this._t = t;
      return this;
    }
    where(...preds) {
      this._pred = preds.length > 1 ? { __op: 'and', preds } : preds[0];
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
    _sql() {
      const params = [];
      let s = `SELECT * FROM ${q(this._t.__table)}` + buildWhere(this._pred, params);
      if (this._orders) {
        const parts = this._orders.map((o) =>
          o.__order ? `${q(o.col.__sqlName)} ${o.__order}` : `${q(o.__sqlName)} ASC`
        );
        s += ' ORDER BY ' + parts.join(', ');
      }
      if (this._limit !== undefined) s += ` LIMIT ${Number(this._limit)}`;
      if (this._offset) s += ` OFFSET ${Number(this._offset)}`;
      return { s, params };
    }
    async all() {
      const { s, params } = this._sql();
      return raw.prepare(s).all(...params).map((r) => readRow(this._t, r));
    }
    async get() {
      const { s, params } = this._sql();
      const row = raw.prepare(s).get(...params);
      return row ? readRow(this._t, row) : undefined;
    }
    async values() {
      return (await this.all()).map((r) => Object.values(r));
    }
    then(res, rej) {
      return this.all().then(res, rej);
    }
  }

  function insertSql(t, vals, conflict) {
    const cols = [];
    const params = [];
    for (const [prop, v] of Object.entries(vals)) {
      const col = t.__columns[prop];
      if (!col) throw new Error(`нет колонки ${prop} в ${t.__table}`);
      cols.push(q(col.__sqlName));
      params.push(writeValue(col, v));
    }
    let s = `INSERT INTO ${q(t.__table)} (${cols.join(', ')}) VALUES (${cols.map(() => '?').join(', ')})`;
    if (conflict) {
      if (conflict.kind === 'nothing') {
        s += ` ON CONFLICT(${q(conflict.target.__sqlName)}) DO NOTHING`;
      } else {
        const sets = [];
        for (const [prop, v] of Object.entries(conflict.set)) {
          const col = t.__columns[prop];
          sets.push(`${q(col.__sqlName)} = ?`);
          params.push(writeValue(col, v));
        }
        s += ` ON CONFLICT(${q(conflict.target.__sqlName)}) DO UPDATE SET ${sets.join(', ')}`;
      }
    }
    return { s, params };
  }

  const db = {
    select() {
      return new SelectQuery();
    },
    insert(t) {
      return {
        values(vals) {
          const list = Array.isArray(vals) ? vals : [vals];
          let conflict = null;
          const exec = (returning) => {
            const rows = [];
            for (const v of list) {
              const { s, params } = insertSql(t, v, conflict);
              if (returning) {
                const got = raw.prepare(s + ' RETURNING *').all(...params);
                rows.push(...got.map((r) => readRow(t, r)));
              } else {
                raw.prepare(s).run(...params);
              }
            }
            return returning ? rows : [];
          };
          const self = {
            onConflictDoNothing({ target }) {
              conflict = { kind: 'nothing', target };
              return self;
            },
            onConflictDoUpdate({ target, set }) {
              conflict = { kind: 'update', target, set };
              return self;
            },
            returning() {
              return {
                run: async () => exec(true),
                then: (res, rej) => Promise.resolve().then(() => exec(true)).then(res, rej),
              };
            },
            run: async () => exec(false),
            then: (res, rej) => Promise.resolve().then(() => exec(false)).then(res, rej),
          };
          return self;
        },
      };
    },
    update(t) {
      return {
        set(vals) {
          return {
            where(...preds) {
              const pred = preds.length > 1 ? { __op: 'and', preds } : preds[0];
              const run = async () => {
                const sets = [];
                const params = [];
                for (const [prop, v] of Object.entries(vals)) {
                  const col = t.__columns[prop];
                  if (!col) throw new Error(`нет колонки ${prop} в ${t.__table}`);
                  sets.push(`${q(col.__sqlName)} = ?`);
                  params.push(writeValue(col, v));
                }
                const s = `UPDATE ${q(t.__table)} SET ${sets.join(', ')}` + buildWhere(pred, params);
                raw.prepare(s).run(...params);
                return [];
              };
              return { run, then: (res, rej) => run().then(res, rej) };
            },
          };
        },
      };
    },
    delete(t) {
      return {
        where(...preds) {
          const pred = preds.length > 1 ? { __op: 'and', preds } : preds[0];
          const run = async () => {
            const params = [];
            const s = `DELETE FROM ${q(t.__table)}` + buildWhere(pred, params);
            raw.prepare(s).run(...params);
            return [];
          };
          return { run, then: (res, rej) => run().then(res, rej) };
        },
      };
    },
    async $count(t, pred) {
      const params = [];
      const s = `SELECT COUNT(*) AS c FROM ${q(t.__table)}` + buildWhere(pred, params);
      return Number(raw.prepare(s).get(...params).c);
    },
    async run(sqlText, params = {}) {
      raw.prepare(String(sqlText)).run(params);
    },
    async all(sqlText, params = {}) {
      return raw.prepare(String(sqlText)).all(params);
    },
    async get(sqlText, params = {}) {
      return raw.prepare(String(sqlText)).get(params);
    },
  };

  return { db, migrate, raw };
}
