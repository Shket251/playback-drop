"""Смена постинга, которая идёт без нашего компьютера и без нашего сервера.

Зачем это здесь, а не дома. Автопостинг с домашнего ПК держится на том, что ПК
включён — а его выключают. Логичный переезд на свой VPS невозможен: сервер стоит
в России, а graph.instagram.com оттуда не отвечает вообще (соединение по IPv4
висит до таймаута, IPv6 отбивается сразу). Это не наша ошибка настройки, это
взаимная блокировка: тот же токен с того же кода работает из-за пределов РФ.

Поэтому исполнителем стал GitHub Actions: его раннеры за границей, расписание
у него своё, и он бесплатен для публичного репозитория. Клипы туда и так
уезжают ассетами релизов — без этого Instagram не смог бы их скачать.

Файл автономный: никакого clipper, только стандартная библиотека. Он живёт
копией в репозитории-раздаче, куда его кладёт `clipper cloud setup`.

  queue.json   что публиковать (пишет домашний ПК)
  posted.json  что уже опубликовано и что упало (пишет эта смена)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

QUEUE = "queue.json"
POSTED = "posted.json"
API = "https://graph.instagram.com/v23.0"
WAIT_TRIES = 20
WAIT_DELAY = 15.0
TRIES = 3           # столько клипов подряд пробуем, если верхние падают


def _call(url: str, params: dict, *, post: bool = False, timeout: float = 60.0) -> dict:
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url + ("" if post else "?" + data.decode()),
                                 data=data if post else None,
                                 method="POST" if post else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            msg = json.loads(raw)["error"]["message"]
        except Exception:                                  # noqa: BLE001
            msg = raw[:300] or str(exc)
        raise RuntimeError(f"Instagram: {msg}") from None


def publish_reel(url: str, caption: str, ig_id: str, token: str,
                 *, sleep=time.sleep) -> str:
    """Контейнер -> ожидание -> публикация. Возвращает id поста."""
    made = _call(f"{API}/{ig_id}/media",
                 {"media_type": "REELS", "video_url": url, "caption": caption,
                  "access_token": token}, post=True)
    container = str(made.get("id") or "").strip()
    if not container:
        raise RuntimeError(f"не вернулся id контейнера: {made}")

    for _ in range(WAIT_TRIES):
        state = _call(f"{API}/{container}",
                      {"fields": "status_code,status", "access_token": token})
        code = str(state.get("status_code") or "").upper()
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"контейнер {code}: {state.get('status') or 'без пояснения'}")
        sleep(WAIT_DELAY)
    else:
        raise RuntimeError("контейнер так и не дошёл до FINISHED — файл недоступен или тяжёл")

    done = _call(f"{API}/{ig_id}/media_publish",
                 {"creation_id": container, "access_token": token}, post=True)
    mid = str(done.get("id") or "").strip()
    if not mid:
        raise RuntimeError(f"не вернулся id поста: {done}")
    return mid


def next_up(queue: dict, posted: dict) -> tuple[dict | None, int]:
    """Первый клип очереди, с которым ещё ничего не случилось.

    Упавший считается отработанным: иначе один битый файл затыкает очередь
    навсегда, и канал молчит, пока никто не заметит.
    """
    for i, row in enumerate(queue.get("posts") or []):
        if str(row.get("clip") or "") not in posted:
            return row, i
    return None, -1


def run_once(queue: dict, posted: dict, env: dict) -> dict:
    """Опубликовать ОДИН клип. Возвращает только новые записи для posted.json.

    Один за смену — не жадность, а ритм: два поста в один час алгоритм читает
    как спам. Но если верхний клип падает, пробуем следующий: смена не должна
    пропадать впустую из-за одного плохого файла.
    """
    fresh: dict = {}
    for _ in range(TRIES):
        row, _i = next_up(queue, {**posted, **fresh})
        if row is None:
            break
        clip = str(row.get("clip") or "?")
        stamp = time.strftime("%Y-%m-%d %H:%M", time.gmtime())
        token = env.get(str(row.get("token_secret") or "IG_TOKEN") or "IG_TOKEN")
        if not token:
            fresh[clip] = {"error": f"в секретах репозитория нет "
                                    f"{row.get('token_secret') or 'IG_TOKEN'}", "at": stamp}
            break                       # следующий клип упрётся в то же самое
        try:
            mid = publish_reel(str(row.get("url") or ""), str(row.get("caption") or ""),
                               str(row.get("ig_id") or ""), token)
        except Exception as exc:                            # noqa: BLE001
            print(f"× {clip}: {exc}", flush=True)
            fresh[clip] = {"error": str(exc)[:300], "at": stamp}
            continue
        print(f"✓ {clip} -> Reels {mid}", flush=True)
        fresh[clip] = {"id": mid, "at": stamp, "account": str(row.get("account") or ""),
                       "platform": "Reels"}
        break
    return fresh


def refresh(token: str) -> tuple[str, int]:
    """Продлить маркер доступа. Возвращает (новый маркер, сколько дней он ещё жив).

    Маркер Instagram живёт 60 дней и продлевается только пока он ЖИВ. Дома это
    происходило само собой, но смысл переезда в облако как раз в том, что дома
    неделями никого нет: непродлённый маркер тихо умрёт, и канал замолчит.
    """
    resp = _call("https://graph.instagram.com/refresh_access_token",
                 {"grant_type": "ig_refresh_token", "access_token": token})
    fresh = str(resp.get("access_token") or "").strip()
    days = int(resp.get("expires_in") or 0) // 86400
    if not fresh:
        raise RuntimeError(f"маркер не продлился: {resp}")
    return fresh, days


def _refresh_and_store(env: dict) -> int:
    token = env.get("IG_TOKEN") or ""
    if not token:
        print("нечего продлевать: в секретах нет IG_TOKEN")
        return 1
    fresh, days = refresh(token)
    print(f"маркер продлён, жив ещё {days} дней")
    repo = env.get("GITHUB_REPOSITORY") or ""
    res = subprocess.run(["gh", "secret", "set", "IG_TOKEN", "--body", fresh, "--repo", repo],
                         capture_output=True, text=True)
    if res.returncode != 0:
        # Не смертельно: маркер продлён на стороне Instagram, но в секрет не лёг —
        # значит следующая смена пойдёт со старым, и через 60 дней всё встанет.
        print(f"! новый маркер не записался в секреты: {(res.stderr or res.stdout)[:200]}")
        return 1
    print("новый маркер записан в секреты репозитория")
    return 0


def _load(name: str, default):
    try:
        with open(name, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{name} испорчен: {exc}")


def main() -> int:
    if "--refresh" in sys.argv[1:]:
        return _refresh_and_store(dict(os.environ))
    queue = _load(QUEUE, {})
    posted = _load(POSTED, {})
    if not (queue.get("posts") or []):
        print("Очередь пуста — публиковать нечего.")
        return 0
    fresh = run_once(queue, posted, dict(os.environ))
    if not fresh:
        print("Всё из очереди уже отработано.")
        return 0
    posted.update(fresh)
    with open(POSTED, "w", encoding="utf-8") as fh:
        json.dump(posted, fh, ensure_ascii=False, indent=1)
    # Провал — не повод рушить смену: запись уже сделана, дома её увидят.
    return 0


if __name__ == "__main__":
    sys.exit(main())
