// Локальный запуск бота: long polling через Bot API.
// Использование: npm run local  (токен — в BOT_TOKEN или файле .bot-token)

import { register } from 'node:module';
import { pathToFileURL } from 'node:url';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import dns from 'node:dns';

// В этой сети IPv6-маршрут до api.telegram.org периодически умирает,
// а fetch не откатывается на IPv4 сам (в отличие от curl) — закрепляем IPv4.
dns.setDefaultResultOrder('ipv4first');

register('./loader.mjs', import.meta.url);

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  const { api, __migrate, BotApiError } = await import('sdk');
  const schema = await import('schema');
  const { default: onMessage } = await import(pathToFileURL(join(ROOT, 'handlers/message.js')).href);
  const { default: onCallback } = await import(pathToFileURL(join(ROOT, 'handlers/callback_query.js')).href);

  __migrate(Object.values(schema).filter((t) => t && t.__table));

  const me = await api.getMe();
  console.log(`Бот @${me.username} запущен (long polling). Ctrl+C — остановить.`);
  await api.deleteWebhook({ drop_pending_updates: false });

  let offset = 0;
  let errors = 0;
  for (;;) {
    let updates;
    try {
      updates = await api.getUpdates({
        offset,
        timeout: 30,
        allowed_updates: ['message', 'callback_query'],
      });
      errors = 0;
    } catch (e) {
      errors++;
      const cause = e.cause?.code || e.cause?.message || '';
      // После ~5 минут сплошных ошибок выходим: под systemd (Restart=always)
      // свежий процесс переподнимет DNS/сокеты — лечит зависания после сна.
      if (errors >= 10) {
        console.error(`getUpdates: сеть не восстанавливается (${errors} ошибок подряд, последняя: ${e.message} ${cause}) — перезапуск процесса.`);
        process.exit(1);
      }
      const wait = Math.min(30000, 1000 * 2 ** errors);
      console.error(`getUpdates: ${e.message}${cause ? ` (${cause})` : ''}; повтор через ${wait / 1000}с`);
      await sleep(wait);
      continue;
    }
    for (const u of updates) {
      offset = u.update_id + 1;
      try {
        if (u.message) await onMessage(u.message, { update: u });
        else if (u.callback_query) await onCallback(u.callback_query, { update: u });
      } catch (e) {
        if (e instanceof BotApiError) console.error(`Bot API ${e.code}: ${e.description}`);
        else console.error('Ошибка обработчика:', e);
      }
    }
  }
}

main().catch((e) => {
  console.error(e.message || e);
  process.exit(1);
});
