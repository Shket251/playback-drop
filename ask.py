"""Показать клип в телеграме и ДОЖДАТЬСЯ ответа. Живёт в облаке — как и tg.py.

Разница с tg.py: там односторонний рассказ («залито туда-то»), а тут разговор.
Надо показать сам клип, поставить под ним кнопки и висеть, пока человек не
нажмёт. Ждать может только тот, кто дотягивается до api.telegram.org, а из
России до него нет связи ни с ПК, ни с VPS — значит ждёт раннер.

Обратно домой ответ едет единственным доступным путём: файлом `asks/<id>.json`
в этом же репозитории. Домашний компьютер снаружи никого не слушает, зато сам
до GitHub достаёт — он этот файл и опрашивает.

Клип уходит ЗАГРУЗКОЙ, а не ссылкой: по ссылке телеграм берёт максимум 20 МБ,
а загрузкой — 50, и наши клипы попадают во второе, но не всегда в первое.

    python3 ask.py --id 3f2a91 --video <url> --text "..." --minutes 30

Нажатия из чужих чатов не считаются: бот открыт всему интернету, и без этой
проверки публикацию мог бы разрешить любой, кто нашёл бота поиском.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

API = "https://api.telegram.org/bot{token}/{method}"
TIMEOUT = 40.0
LONG_POLL = 25              # столько телеграм держит getUpdates, если ничего не происходит
ANSWERS = Path("asks")
YES, NO = "ok", "no"


def _api(method: str, body: dict, token: str, timeout: float = TIMEOUT) -> dict:
    req = urllib.request.Request(
        API.format(token=token, method=method), data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _keyboard(ask_id: str) -> dict:
    return {"inline_keyboard": [[{"text": "✅ Публикуем", "callback_data": f"{YES}:{ask_id}"},
                                 {"text": "✖️ Мимо", "callback_data": f"{NO}:{ask_id}"}]]}


def drain(token: str) -> int:
    """Забыть всё, что бот получил до этого вопроса, и вернуть номер следующего.

    Без этого старое нажатие («мимо» по прошлому клипу) прилетело бы ответом на
    новый вопрос — человек ещё смотрит, а решение уже принято за него.
    """
    off = 0
    try:
        out = _api("getUpdates", {"timeout": 0, "offset": -1}, token, 20.0)
        for upd in out.get("result") or []:
            off = max(off, int(upd.get("update_id") or 0) + 1)
    except Exception as exc:                                # noqa: BLE001
        print(f"! старые сообщения не вычищены ({exc}) — жду с начала очереди", flush=True)
    return off


def show(video_url: str, text: str, ask_id: str, token: str, chat: str) -> bool:
    """Отправить клип с кнопками. Не вышло видео — хотя бы текст с кнопками."""
    body = {"chat_id": chat, "caption": text[:1024], "parse_mode": "HTML",
            "supports_streaming": True, "reply_markup": json.dumps(_keyboard(ask_id))}
    if video_url:
        # Скачиваем и заливаем сами: телеграм по чужой ссылке берёт только 20 МБ,
        # а наши клипы бывают тяжелее — и отказ выглядел бы как «бот молчит».
        tmp = Path("ask.mp4")
        got = subprocess.run(["curl", "-sL", "--max-time", "600", video_url, "-o", str(tmp)])
        size = tmp.stat().st_size if tmp.exists() else 0
        if got.returncode == 0 and size > 100_000:
            print(f"клип на руках: {size/1e6:.1f} МБ", flush=True)
            form: list[str] = []
            for k, v in body.items():
                form += ["-F", f"{k}={v}"]
            r = subprocess.run(["curl", "-sS", "--max-time", "600", *form,
                                "-F", f"video=@{tmp}",
                                API.format(token=token, method="sendVideo")],
                               capture_output=True, text=True)
            ok = r.returncode == 0 and '"ok":true' in (r.stdout or "")
            if ok:
                return True
            print(f"! клип не ушёл ({(r.stdout or r.stderr)[:200]}) — шлю одним текстом",
                  flush=True)
        else:
            print(f"! клип не скачался (curl {got.returncode}, {size} б) — шлю одним текстом",
                  flush=True)
    body.pop("supports_streaming", None)
    body["text"] = body.pop("caption")
    body["reply_markup"] = _keyboard(ask_id)
    out = _api("sendMessage", body, token)
    if not out.get("ok"):
        print(f"! телеграм отказал: {str(out)[:200]}", flush=True)
    return bool(out.get("ok"))


def wait(ask_id: str, token: str, chat: str, minutes: float) -> dict:
    """Висеть, пока не нажмут кнопку. Ответ — что решили и когда."""
    off = drain(token)
    until = time.time() + minutes * 60
    while time.time() < until:
        left = max(1, int(min(LONG_POLL, until - time.time())))
        try:
            out = _api("getUpdates",
                       {"timeout": left, "offset": off,
                        "allowed_updates": ["callback_query"]},
                       token, left + 20.0)
        except Exception as exc:                            # noqa: BLE001
            # Сеть моргнула — это не ответ «нет». Ждём дальше, счётчик идёт.
            print(f"! телеграм не ответил ({type(exc).__name__}) — жду дальше", flush=True)
            time.sleep(3)
            continue
        for upd in out.get("result") or []:
            off = max(off, int(upd.get("update_id") or 0) + 1)
            cq = upd.get("callback_query") or {}
            data = str(cq.get("data") or "")
            if not data.endswith(f":{ask_id}"):
                continue                    # ответ на другой (прошлый) вопрос
            where = str(((cq.get("message") or {}).get("chat") or {}).get("id") or "")
            if where != str(chat):
                print(f"! нажали из чужого чата {where} — не считаю", flush=True)
                continue
            yes = data.startswith(f"{YES}:")
            _close(cq, yes, token)
            return {"ok": yes, "at": int(time.time()),
                    "by": str((cq.get("from") or {}).get("username") or "")}
    return {"ok": False, "why": f"никто не нажал за {minutes:.0f} мин", "at": int(time.time())}


def _close(cq: dict, yes: bool, token: str) -> None:
    """Убрать кнопки и сказать, что решение принято. Молча — дело уже сделано."""
    msg = cq.get("message") or {}
    for method, body in (
            ("answerCallbackQuery", {"callback_query_id": cq.get("id"),
                                     "text": "публикую" if yes else "пропускаю"}),
            ("editMessageReplyMarkup", {"chat_id": (msg.get("chat") or {}).get("id"),
                                        "message_id": msg.get("message_id"),
                                        "reply_markup": {"inline_keyboard": [[{
                                            "text": "✅ публикую" if yes else "✖️ пропущено",
                                            "callback_data": "done"}]]}})):
        try:
            _api(method, body, token, 20.0)
        except Exception as exc:                            # noqa: BLE001
            print(f"! {method}: {exc}", flush=True)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--id", required=True)
    ap.add_argument("--video", default="")
    ap.add_argument("--text", default="Публикуем?")
    ap.add_argument("--minutes", type=float, default=30.0)
    a = ap.parse_args(argv)

    token = str(os.environ.get("TG_BOT_TOKEN") or "")
    chat = str(os.environ.get("TG_CHAT_ID") or "")
    ANSWERS.mkdir(parents=True, exist_ok=True)
    out = ANSWERS / f"{a.id}.json"
    if not token or not chat:
        # Ответ пишем ВСЕГДА, даже когда спросить не вышло: дома ждут файл, и его
        # отсутствие там неотличимо от «раннер ещё не дошёл» — ПК висел бы весь срок.
        out.write_text(json.dumps({"ok": False, "why": "в секретах нет TG_BOT_TOKEN/TG_CHAT_ID"}),
                       encoding="utf-8")
        print("! нечем спрашивать: нет секретов", flush=True)
        return 1
    if not show(a.video, a.text, a.id, token, chat):
        out.write_text(json.dumps({"ok": False, "why": "вопрос не дошёл до телеграма"}),
                       encoding="utf-8")
        return 1
    answer = wait(a.id, token, chat, a.minutes)
    out.write_text(json.dumps(answer, ensure_ascii=False), encoding="utf-8")
    print(f"решение: {answer}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
