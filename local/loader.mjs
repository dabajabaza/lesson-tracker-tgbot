// Резолвер bare-имён модулей платформы Telegram Serverless для локального запуска:
// 'sdk' и 'sdk/db' → локальный адаптер, 'schema' и 'lib/*' → файлы проекта.

import { pathToFileURL } from 'node:url';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const LOCAL = dirname(fileURLToPath(import.meta.url));
const ROOT = join(LOCAL, '..');

export async function resolve(specifier, context, next) {
  if (specifier === 'sdk' || specifier === 'sdk/api' || specifier === 'sdk/fetch')
    return { url: pathToFileURL(join(LOCAL, 'sdk.js')).href, shortCircuit: true };
  if (specifier === 'sdk/db')
    return { url: pathToFileURL(join(LOCAL, 'sdk_db.js')).href, shortCircuit: true };
  if (specifier === 'schema')
    return { url: pathToFileURL(join(ROOT, 'schema.js')).href, shortCircuit: true };
  if (specifier.startsWith('lib/'))
    return { url: pathToFileURL(join(ROOT, `${specifier}.js`)).href, shortCircuit: true };
  return next(specifier, context);
}
