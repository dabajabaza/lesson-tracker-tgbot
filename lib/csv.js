// Генерация CSV и разбивка длинного текста на сообщения Telegram (лимит 4096 символов).

function escapeCell(value) {
  const s = value === null || value === undefined ? '' : String(value);
  return /[";\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

// rows — массив массивов; разделитель «;» (так CSV открывается в русском Excel без настройки).
export function toCsv(rows) {
  return rows.map((row) => row.map(escapeCell).join(';')).join('\n');
}

// Режет текст по строкам на куски не длиннее limit.
export function chunkText(text, limit = 4000) {
  const chunks = [];
  let current = '';
  for (const line of text.split('\n')) {
    if (current && current.length + line.length + 1 > limit) {
      chunks.push(current);
      current = '';
    }
    // Строка длиннее лимита сама по себе — режем жёстко.
    if (line.length > limit) {
      for (let i = 0; i < line.length; i += limit) chunks.push(line.slice(i, i + limit));
      continue;
    }
    current = current ? current + '\n' + line : line;
  }
  if (current) chunks.push(current);
  return chunks;
}
