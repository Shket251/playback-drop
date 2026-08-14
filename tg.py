"""Сообщение в телеграм. Живёт в облаке, потому что из дома его не отправить.

api.telegram.org не открывается ни с домашнего интернета, ни с нашего VPS: оба в
России, соединение просто виснет (замер 14.08.2026 — 000 за 12 секунд с обоих, при
живом DNS). Заграничный раннер GitHub — единственное место, откуда бот дотягивается
до телеграма, и он же публикует Reels. Поэтому облако говорит само, а ПК просит
облако сказать за него (см. clipper/tg.py и notify.yml).

Сообщение — не работа смены, а рассказ о ней. Что бы тут ни сломалось, публикация
уже случилась: отсюда наружу не летит ни одно исключение, но и молчать нельзя —
неотправленное сообщение видно только в логе прогона.

Запускается и руками: `python3 tg.py "текст"`, а `python3 tg.py --whoami` показывает
номера чатов, которые написали боту (иначе взять TG_CHAT_ID неоткуда).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

API = "https://api.telegram.org/bot{token}/{method}"
TIMEOUT = 20.0


def _post(url: str, body: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def tell(text: str, env: dict | None = None) -> bool:
    """Сказать в телеграм. Возвращает, дошло ли; наружу не бросает никогда."""
    env = os.environ if env is None else env
    token = str(env.get("TG_BOT_TOKEN") or "")
    chat = str(env.get("TG_CHAT_ID") or "")
    if not token or not chat:
        missing = " и ".join(n for n, v in (("TG_BOT_TOKEN", token), ("TG_CHAT_ID", chat))
                             if not v)
        print(f"! телеграм молчит: в секретах репозитория нет {missing}", flush=True)
        return False
    try:
        out = _post(API.format(token=token, method="sendMessage"),
                    {"chat_id": chat, "text": text, "parse_mode": "HTML",
                     "disable_web_page_preview": True}, TIMEOUT)
    except Exception as exc:                                # noqa: BLE001
        print(f"! телеграм не ответил ({type(exc).__name__}: {exc}) — "
              f"сообщение не ушло, публикация ни при чём", flush=True)
        return False
    if not out.get("ok"):
        print(f"! телеграм отказал: {str(out)[:200]}", flush=True)
        return False
    return True


def whoami(env: dict | None = None) -> list[str]:
    """Кто написал боту. Единственный способ узнать номер своего чата."""
    env = os.environ if env is None else env
    token = str(env.get("TG_BOT_TOKEN") or "")
    if not token:
        print("! в секретах репозитория нет TG_BOT_TOKEN", flush=True)
        return []
    try:
        me = _post(API.format(token=token, method="getMe"), {}, TIMEOUT)
        out = _post(API.format(token=token, method="getUpdates"), {}, TIMEOUT)
    except Exception as exc:                                # noqa: BLE001
        print(f"! телеграм не ответил: {type(exc).__name__}: {exc}", flush=True)
        return []
    # Кто мы такие. Без этого совет «напиши боту /start» некому выполнить: имя бота
    # знает только сам телеграм, из токена его не достать.
    print(f"Бот: @{(me.get('result') or {}).get('username') or '?'}", flush=True)
    seen: list[str] = []
    for upd in out.get("result") or []:
        msg = upd.get("message") or upd.get("channel_post") or {}
        chat = msg.get("chat") or {}
        line = (f"chat_id={chat.get('id')}  {chat.get('type')}  "
                f"{chat.get('title') or chat.get('username') or chat.get('first_name')}")
        if line not in seen:
            seen.append(line)
    print("Кто писал боту:\n  " + ("\n  ".join(seen) if seen
                                   else "никто — напиши боту /start и повтори"), flush=True)
    return seen


if __name__ == "__main__":
    if "--whoami" in sys.argv:
        whoami()
    else:
        text = " ".join(a for a in sys.argv[1:] if not a.startswith("--"))
        sys.exit(0 if tell(text or "проверка связи") else 1)
