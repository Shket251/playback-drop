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
  token.json   ДАТА последнего продления маркера — и только дата
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

try:                                # сосед по репозиторию-раздаче
    from tg import tell
except Exception:                   # noqa: BLE001 — рассказ о смене не важнее смены
    def tell(text: str, env: dict | None = None) -> bool:
        print(f"! рядом нет tg.py, сообщение не ушло: {text[:80]}", flush=True)
        return False

QUEUE = "queue.json"
POSTED = "posted.json"
TOKEN_NOTE = "token.json"
API = "https://graph.instagram.com/v23.0"
WAIT_TRIES = 20
WAIT_DELAY = 15.0
TRIES = 3           # столько клипов подряд пробуем, если верхние падают
ASK_MINUTES = 45    # столько ждём нажатия: смена идёт по расписанию, человек не ждёт её
SHIFT = "!смена"    # ключ для беды, которая не про клип (именем файла быть не может)

# Беды, в которых клип не виноват: маркер, права, блокировка приложения, лимиты.
# Их надо отличать от «этот файл не скачался», иначе одна блокировка Meta за
# три запуска пометит битыми три хороших клипа и выжжет буфер до дна.
COMMON = ("api access blocked", "access token", "session has been invalidated",
          "rate limit", "application request limit", "permission")


class NotTheClip(RuntimeError):
    """Беда не про клип: сеть раннера, маркер, права, лимиты, поломка на стороне Meta.

    Раньше сюда попадали только текстовые ответы Instagram из `COMMON`, а обрыв связи
    самого раннера (`URLError`, таймаут, DNS, 5xx) до этой проверки не доходил вовсе —
    он летел как обычная ошибка и записывался В ВИНУ КЛИПУ. Очередь отсортирована по
    силе, попыток за смену три: один обрыв связи помечал битыми три лучших клипа
    подряд, а после второго такого эпизода они вычёркивались навсегда.
    """


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
        if exc.code >= 500:                # поломка на их стороне — клип ни при чём
            raise NotTheClip(f"Instagram отвечает {exc.code}: {msg}") from None
        raise RuntimeError(f"Instagram: {msg}") from None
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
        raise NotTheClip(f"сеть раннера: {exc}") from None


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


def already_posted(ig_id: str, token: str, caption: str) -> str:
    """id поста с такой же подписью среди последних, иначе "".

    Между `media_publish` и получением ответа есть окно: обрыв связи ровно в нём
    оставляет пост В ЛЕНТЕ, а нам возвращает ошибку. Клип считается неопубликованным
    и на следующей смене уезжает ВТОРЫМ разом — с той же подписью, в тот же аккаунт.
    Спросить аккаунт дешевле, чем извиняться за дубль.

    Сверяем по подписи: она у каждого клипа своя и уже лежит в записи очереди.
    """
    head = (caption or "").strip()[:80]
    if not head:
        return ""
    try:
        got = _call(f"{API}/{ig_id}/media",
                    {"fields": "id,caption", "limit": "10", "access_token": token})
    except Exception:                                       # noqa: BLE001
        return ""            # не смогли спросить — молчим, дубль дешевле простоя
    for item in got.get("data") or []:
        if str(item.get("caption") or "").strip()[:80] == head:
            return str(item.get("id") or "")
    return ""


def next_up(queue: dict, posted: dict, *, ask: bool = False) -> tuple[dict | None, int]:
    """Первый клип очереди, с которым ещё ничего не случилось.

    Упавший считается отработанным: иначе один битый файл затыкает очередь
    навсегда, и канал молчит, пока никто не заметит.

    Очередей на самом деле две в одной. Клип заказа выходит только с разрешения
    человека, и обычная ночная смена не должна взять его НИКОГДА — а смена по
    кнопке не должна брать ничего, кроме таких. Отсюда разделение по метке.
    """
    for i, row in enumerate(queue.get("posts") or []):
        if bool(row.get("ask")) != ask:
            continue
        if str(row.get("clip") or "") not in posted:
            return row, i
    return None, -1


def allowed(row: dict, env: dict, clip: str) -> tuple[bool, str]:
    """Спросить человека кнопкой. Возвращает (разрешено, почему нет).

    Пустая причина при отказе — это «нажали „мимо“»: решение принято, клип надо
    вычеркнуть. Непустая — «спросить не вышло или не ответили»: очередь целая,
    клип дождётся следующей смены.
    """
    from ask import show, wait                              # сосед по репозиторию-раздаче

    token = str(env.get("TG_BOT_TOKEN") or "")
    chat = str(env.get("TG_CHAT_ID") or "")
    if not token or not chat:
        return False, "в секретах репозитория нет TG_BOT_TOKEN/TG_CHAT_ID — спросить нечем"
    key = hashlib.sha1(clip.encode("utf-8")).hexdigest()[:10]
    text = (f"Публикуем в Reels на @{row.get('account')}?\n\n"
            f"<b>{clip}</b>\n\n{str(row.get('caption') or '')[:700]}")
    if not show(str(row.get("url") or ""), text, key, token, chat):
        return False, "вопрос не дошёл до телеграма"
    got = wait(key, token, chat, float(env.get("ASK_MINUTES") or ASK_MINUTES))
    if got.get("ok"):
        return True, ""
    return False, str(got.get("why") or "")


def run_once(queue: dict, posted: dict, env: dict, *, ask: bool = False) -> dict:
    """Опубликовать ОДИН клип. Возвращает только новые записи для posted.json.

    Один за смену — не жадность, а ритм: два поста в один час алгоритм читает
    как спам. Но если верхний клип падает, пробуем следующий: смена не должна
    пропадать впустую из-за одного плохого файла.

    С `ask` смена сначала показывает клип в телеграме и ждёт нажатия. Это и есть
    способ выпустить клип заказа при выключенном компьютере: решает по-прежнему
    человек, просто спрашивает его тот, кто дотягивается до телеграма.
    """
    fresh: dict = {}
    for _ in range(TRIES):
        row, _i = next_up(queue, {**posted, **fresh}, ask=ask)
        if row is None:
            break
        clip = str(row.get("clip") or "?")
        stamp = time.strftime("%Y-%m-%d %H:%M", time.gmtime())
        if ask:
            ok, why = allowed(row, env, clip)
            if not ok and why:
                # Не ответили или спрашивать нечем. Очередь не трогаем: клип живой,
                # просто сегодня его никто не выпустил.
                print(f"— {clip}: {why}", flush=True)
                fresh[SHIFT] = {"error": why, "at": stamp, "clip": clip}
                break
            if not ok:
                print(f"× {clip}: «мимо» — вычёркиваю", flush=True)
                fresh[clip] = {"skipped": True, "at": stamp}
                _tidy(row, env)
                break
        token = env.get(str(row.get("token_secret") or "IG_TOKEN") or "IG_TOKEN")
        if not token:
            # Тоже беда не про клип: следующий упрётся в ровно то же самое.
            fresh[SHIFT] = {"error": f"в секретах репозитория нет "
                                     f"{row.get('token_secret') or 'IG_TOKEN'}", "at": stamp}
            break
        try:
            mid = publish_reel(str(row.get("url") or ""), str(row.get("caption") or ""),
                               str(row.get("ig_id") or ""), token)
        except Exception as exc:                            # noqa: BLE001
            print(f"× {clip}: {exc}", flush=True)
            # Пост мог УСПЕТЬ уйти в ленту, а ответ потеряться. Прежде чем винить
            # клип или смену, спрашиваем сам аккаунт.
            landed = already_posted(str(row.get("ig_id") or ""), token,
                                    str(row.get("caption") or ""))
            if landed:
                print(f"  …но пост уже в ленте: {landed} — засчитываю", flush=True)
                fresh[clip] = {"id": landed, "at": stamp,
                               "account": str(row.get("account") or ""), "platform": "Reels"}
                _tidy(row, env)
                break
            if isinstance(exc, NotTheClip) or any(mark in str(exc).lower() for mark in COMMON):
                # Клип ни при чём — дальше пойдёт то же самое. Очередь не трогаем.
                fresh[SHIFT] = {"error": str(exc)[:300], "at": stamp, "clip": clip}
                break
            fresh[clip] = {"error": str(exc)[:300], "at": stamp}
            continue
        print(f"✓ {clip} -> Reels {mid}", flush=True)
        who = str(row.get("account") or "")
        # Пост ушёл — об этом надо сказать сразу. Дома об этом узнают только после
        # `clipper cloud pull`, то есть когда включат компьютер и вспомнят.
        tell(f"✅ Опубликовано в Reels\n<b>{clip}</b>\nканал: @{who}\n"
             f"https://instagram.com/{who}", env)
        fresh[clip] = {"id": mid, "at": stamp, "account": who, "platform": "Reels"}
        _tidy(row, env)
        break
    return fresh


def _tidy(row: dict, env: dict) -> None:
    """Убрать раздачу опубликованного клипа.

    Пост уже в ленте. Что бы ни случилось с уборкой дальше, отметка о нём должна
    уцелеть: потеряв её, следующая смена зальёт тот же клип второй раз.
    """
    try:
        drop_release(str(row.get("tag") or ""), env)
    except Exception as exc:                                # noqa: BLE001
        print(f"! уборка раздачи сорвалась: {exc}", flush=True)


def drop_release(tag: str, env: dict) -> bool:
    """Убрать раздачу опубликованного клипа. Возвращает, удалось ли.

    Раньше это делал домашний ПК, когда забирал отметки, — то есть копии
    опубликованных клипов лежали в открытом репозитории ровно столько, сколько
    компьютер был выключен. Смысл переезда в облако в том и был, что он может
    не включаться неделями, так что убирать за собой должна сама смена.
    """
    repo, token = env.get("GITHUB_REPOSITORY") or "", env.get("GITHUB_TOKEN") or ""
    if not (tag and repo and token):
        return False
    head = {"Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json"}

    def _req(url: str, method: str = "GET"):
        req = urllib.request.Request(url, method=method, headers=head)
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body.strip() else {}

    try:
        rel = _req(f"https://api.github.com/repos/{repo}/releases/tags/{tag}")
        _req(f"https://api.github.com/repos/{repo}/releases/{rel['id']}", "DELETE")
        _req(f"https://api.github.com/repos/{repo}/git/refs/tags/{tag}", "DELETE")
    except Exception as exc:                                # noqa: BLE001
        # Не смертельно: клип уже в ленте, а хвост подберёт домашний `cloud pull`.
        print(f"! раздачу {tag} убрать не вышло: {exc}", flush=True)
        return False
    print(f"  раздача {tag} убрана", flush=True)
    return True


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


def _save_note(note: dict) -> None:
    """Оставить домашнему ПК записку о продлении. ТОЛЬКО дата — репозиторий публичный."""
    with open(TOKEN_NOTE, "w", encoding="utf-8") as fh:
        json.dump(note, fh, ensure_ascii=False, indent=1)


RENEW_AFTER_DAYS = 30       # маркер живёт 60 дней; продлеваем на половине пути


def _age_days(issued: str) -> float:
    try:
        return (time.time() - time.mktime(time.strptime(issued, "%Y-%m-%d"))) / 86400
    except (ValueError, TypeError):
        return 999.0            # нет даты — считаем, что пора


def _refresh_and_store(env: dict) -> int:
    token = env.get("IG_TOKEN") or ""
    if not token:
        print("нечего продлевать: в секретах нет IG_TOKEN")
        return 1
    # Продлеваем по надобности, а не каждую смену. Дважды в сутки дёргать Instagram
    # незачем, но главное — иначе дата в записке означала бы «сегодня» всегда, и
    # домашний ПК считал бы себя отставшим каждый день.
    was = _age_days(str((_load(TOKEN_NOTE, {}) or {}).get("issued") or ""))
    if was < RENEW_AFTER_DAYS:
        print(f"маркер продлевали {was:.0f} дней назад — рано")
        return 0
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
    # Секрет читать нельзя — только записать, поэтому вернуть маркер домой отсюда
    # невозможно. Зато можно сказать ДАТУ: по ней домашний ПК поймёт, что его
    # собственный маркер отстал, и продлит свой, пока тот ещё жив. Иначе дома всё
    # тихо ходит со старым, пока однажды не перестанет — а это отдельная команда,
    # и заметят её нескоро.
    _save_note({"issued": time.strftime("%Y-%m-%d"), "by": "cloud", "days_left": days})
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
    ask = "--ask" in sys.argv[1:]
    queue = _load(QUEUE, {})
    posted = _load(POSTED, {})
    if not (queue.get("posts") or []):
        print("Очередь пуста — публиковать нечего.")
        return 0
    fresh = run_once(queue, posted, dict(os.environ), ask=ask)
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
