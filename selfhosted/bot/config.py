"""Конфигурация из окружения / файлов.

- BOT_TOKEN или файл .bot-token (как в serverless-версии).
- DATABASE_URL — по умолчанию локальная SQLite рядом с проектом.
- TELEGRAM_PROXY — http-прокси для доступа к api.telegram.org (в этой сети
  прямой доступ режется провайдером; ставить http://127.0.0.1:1080).
"""

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # каталог selfhosted/


@dataclass
class Config:
    token: str
    db_url: str
    proxy: str | None


def _read_token() -> str:
    env = os.environ.get("BOT_TOKEN")
    if env:
        return env.strip()
    token_file = ROOT / ".bot-token"
    if token_file.exists():
        return token_file.read_text(encoding="utf-8").strip()
    raise RuntimeError(
        "Нет токена: задайте BOT_TOKEN или положите его в selfhosted/.bot-token"
    )


def load_config() -> Config:
    default_db = f"sqlite+aiosqlite:///{ROOT / 'data.sqlite'}"
    return Config(
        token=_read_token(),
        db_url=os.environ.get("DATABASE_URL", default_db),
        proxy=os.environ.get("TELEGRAM_PROXY") or None,
    )
