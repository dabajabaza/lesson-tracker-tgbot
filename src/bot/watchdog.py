"""Автономное восстановление после сетевых сбоев (смена wifi/кабель, провал прокси).

Два независимых механизма, оба без внешних зависимостей:

1. sd_notify(READY/WATCHDOG) — общение с systemd watchdog. Бот пингует systemd
   (`WATCHDOG=1`) только пока реально достукивается до Telegram через текущую
   сессию/прокси. Если бот «завис» (мёртвый long-poll сокет, дедлок коннектора,
   недоступный прокси) и перестал пинговать — systemd по WatchdogSec убивает
   процесс и поднимает заново (Restart=always). Это жёсткий бэкстоп.

2. run_watchdog — периодическая лёгкая проба getMe. Живёт рядом с long polling
   в том же event loop и той же сессии, поэтому честно отражает здоровье связки
   «процесс → прокси → Telegram».

Если NOTIFY_SOCKET не задан (запуск вручную, не под systemd notify) — механизм
тихо отключается, бот работает как обычно.
"""

import asyncio
import logging
import os
import socket

log = logging.getLogger("watchdog")


def sd_notify(state: str) -> bool:
    """Отправляет строку состояния systemd через NOTIFY_SOCKET. Возвращает False,
    если сокет не задан или недоступен (тогда вызов — безопасный no-op)."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    # Абстрактный сокет systemd начинается с '@' → в API ядра это ведущий \0.
    if addr[0] == "@":
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(state.encode())
        return True
    except OSError as e:
        log.warning("sd_notify(%r) не отправлено: %s", state, e)
        return False


async def run_watchdog(bot, *, interval: float, probe_timeout: float) -> None:
    """Раз в `interval` секунд проверяет доступность Telegram лёгким getMe.
    Успех → пингуем systemd (WATCHDOG=1). Провал → НЕ пингуем: если так
    продлится дольше WatchdogSec, systemd перезапустит сервис."""
    if not os.environ.get("NOTIFY_SOCKET"):
        log.info("NOTIFY_SOCKET не задан — watchdog отключён (бот не под systemd notify).")
        return
    log.info("watchdog активен: проба каждые %gс, таймаут пробы %gс.", interval, probe_timeout)
    while True:
        await asyncio.sleep(interval)
        try:
            # Внешний asyncio.timeout — страховка на случай, если request_timeout
            # по какой-то причине не сработает (дедлок вне HTTP-запроса).
            async with asyncio.timeout(probe_timeout + 5):
                await bot.get_me(request_timeout=int(probe_timeout))
        except Exception as e:  # noqa: BLE001 — любой сбой пробы = «нездоров»
            log.warning(
                "проба Telegram не прошла (%s) — systemd не пингуем, "
                "ждём авто-рестарт по WatchdogSec.",
                type(e).__name__,
            )
        else:
            sd_notify("WATCHDOG=1")
