// Локальная реализация 'sdk': api → Bot API по HTTPS, db → node:sqlite.
// Позволяет запускать бота на своей машине, пока Telegram Serverless в закрытой бете.

import { readFileSync, mkdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createDb } from './db-core.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');

// --- база ---
const DB_PATH = process.env.TGLOCAL_DB || join(ROOT, 'local', 'data.sqlite');
if (DB_PATH !== ':memory:') mkdirSync(dirname(DB_PATH), { recursive: true });
const core = createDb(DB_PATH);
export const db = core.db;
export const __migrate = core.migrate;

// --- Bot API ---
export class BotApiError extends Error {
  constructor(code, description, method, parameters) {
    super(`${method}: ${code} ${description}`);
    this.code = code;
    this.description = description;
    this.method = method;
    this.parameters = parameters;
  }
}

function readToken() {
  if (process.env.BOT_TOKEN) return process.env.BOT_TOKEN.trim();
  try {
    return readFileSync(join(ROOT, '.bot-token'), 'utf8').trim();
  } catch {
    throw new Error('Нет токена: задайте BOT_TOKEN или положите его в файл .bot-token в корне проекта.');
  }
}

let token = null;

export const api = new Proxy(
  {},
  {
    get(_, method) {
      return async (params = {}) => {
        token ??= readToken();
        const res = await globalThis.fetch(`https://api.telegram.org/bot${token}/${method}`, {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify(params),
        });
        const data = await res.json();
        if (!data.ok) throw new BotApiError(data.error_code, data.description, method, data.parameters);
        return data.result;
      };
    },
  }
);

export const fetch = globalThis.fetch;
