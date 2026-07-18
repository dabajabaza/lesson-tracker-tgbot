// Тестовый sdk: НАСТОЯЩИЙ SQLite-адаптер (local/db-core) + фейковый api-рекордер.
import { createDb } from '../../local/db-core.mjs';
import * as schema from '../../schema.js';

const core = createDb(':memory:');
core.migrate(Object.values(schema).filter((t) => t && t.__table));

export const db = core.db;

export function __dump() {
  const out = {};
  for (const t of Object.values(schema).filter((t) => t && t.__table)) {
    out[t.__table] = core.raw
      .prepare(`SELECT * FROM "${t.__table}"`)
      .all()
      .map((row) => {
        const converted = {};
        for (const [prop, col] of Object.entries(t.__columns)) {
          const v = row[col.__sqlName];
          if (v === null || v === undefined) converted[prop] = v;
          else if (col.__kind === 'boolean') converted[prop] = !!v;
          else if (col.__kind === 'json') converted[prop] = JSON.parse(v);
          else converted[prop] = v;
        }
        return converted;
      });
  }
  return out;
}

export class BotApiError extends Error {
  constructor(code, description) {
    super(description);
    this.code = code;
    this.description = description;
  }
}

export const __apiCalls = [];
let msgSeq = 1000;

export const api = new Proxy(
  {},
  {
    get(_, method) {
      return async (params) => {
        __apiCalls.push({ method, params });
        if (method === 'sendMessage') return { message_id: ++msgSeq, chat: { id: params.chat_id } };
        return true;
      };
    },
  }
);

export const fetch = () => {
  throw new Error('fetch не используется');
};
