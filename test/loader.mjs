// Резолвер bare-имён модулей для тестов на in-memory стабах SDK.
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const PROJ = join(dirname(fileURLToPath(import.meta.url)), '..');
const STUBS = new URL('./stubs/', import.meta.url);

export async function resolve(specifier, context, next) {
  if (specifier === 'sdk') return { url: new URL('sdk.js', STUBS).href, shortCircuit: true };
  if (specifier === 'sdk/db') return { url: new URL('sdk_db.js', STUBS).href, shortCircuit: true };
  if (specifier === 'schema')
    return { url: pathToFileURL(join(PROJ, 'schema.js')).href, shortCircuit: true };
  if (specifier.startsWith('lib/'))
    return { url: pathToFileURL(join(PROJ, `${specifier}.js`)).href, shortCircuit: true };
  return next(specifier, context);
}
