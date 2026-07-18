// Все денежные суммы хранятся в копейках (integer), чтобы избежать ошибок float.

// Разбирает пользовательский ввод: «1600», «1 600», «1600.50», «1600,50».
// Возвращает сумму в копейках или null, если ввод некорректен.
export function parseMoney(input) {
  const s = String(input).trim().replace(/\s+/g, '').replace(',', '.');
  if (!/^\d+(\.\d{1,2})?$/.test(s)) return null;
  const [rub, kop = ''] = s.split('.');
  const value = parseInt(rub, 10) * 100 + (kop ? parseInt(kop.padEnd(2, '0'), 10) : 0);
  if (!Number.isSafeInteger(value)) return null;
  return value;
}

// Для CSV: 160050 → «1600,50» (десятичная запятая — под русский Excel).
export function toRubles(kopecks) {
  return (kopecks / 100).toFixed(2).replace('.', ',');
}

// 160000 → «1 600 ₽», 160050 → «1 600,50 ₽»
export function formatMoney(kopecks) {
  const sign = kopecks < 0 ? '-' : '';
  const abs = Math.abs(kopecks);
  const rub = Math.floor(abs / 100);
  const kop = abs % 100;
  const rubStr = String(rub).replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
  return `${sign}${rubStr}${kop ? ',' + String(kop).padStart(2, '0') : ''} ₽`;
}
