"""Конфигурация из окружения / файлов.

- BOT_TOKEN или файл .bot-token (как в serverless-версии).
- DATABASE_URL — по умолчанию локальная SQLite рядом с проектом.
- TELEGRAM_PROXY — http-прокси для доступа к api.telegram.org (в этой сети
  прямой доступ режется провайдером; ставить http://127.0.0.1:1080).
- ADMIN_IDS — id админов через запятую. Они допущены всегда и могут выдавать
  инвайты; остальных пускает только allowed_users (см. access.py).
"""

import os
from dataclasses import dataclass
from pathlib import Path

# Корень репозитория: src/bot/config.py -> parents[2]. От него отсчитываются
# alembic.ini, .bot-token и data.sqlite по умолчанию. Держится на editable-
# инсталле (пакет живёт в дереве исходников) — у нас он всюду: uv sync локально,
# pip install -e . на сервере.
ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Config:
    token: str
    db_url: str
    proxy: str | None
    admin_ids: frozenset[int]


def _read_token() -> str:
    env = os.environ.get("BOT_TOKEN")
    if env:
        return env.strip()
    token_file = ROOT / ".bot-token"
    if token_file.exists():
        return token_file.read_text(encoding="utf-8").strip()
    raise RuntimeError(
        "Нет токена: задайте BOT_TOKEN или положите его в .bot-token в корне репозитория"
    )


def _read_admin_ids() -> frozenset[int]:
    """ADMIN_IDS — id админов через запятую.

    Пустой список означает, что бот не ответит вообще никому (кроме уже
    впущенных в allowed_users), поэтому кривое значение лучше уронить на
    старте, чем молча оставить бота немым.
    """
    raw = os.environ.get("ADMIN_IDS", "").strip()
    if not raw:
        return frozenset()
    try:
        # Разбор без предварительной проверки формы: здесь она не нужна.
        # Мусор вроде "³" ловит сам int(), и except ниже превращает его во
        # внятный отказ старта — в отличие от /allow (admin.py), где ответить
        # надо подсказкой, а не падением, и потому проверка стоит заранее.
        return frozenset(int(chunk) for chunk in (c.strip() for c in raw.split(",")) if chunk)
    except ValueError as exc:
        raise RuntimeError(
            f"ADMIN_IDS должен быть списком чисел через запятую, получено: {raw!r}"
        ) from exc


def load_config() -> Config:
    default_db = f"sqlite+aiosqlite:///{ROOT / 'data.sqlite'}"
    return Config(
        token=_read_token(),
        db_url=os.environ.get("DATABASE_URL", default_db),
        proxy=os.environ.get("TELEGRAM_PROXY") or None,
        admin_ids=_read_admin_ids(),
    )
