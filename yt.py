"""Смена постинга на YouTube, которая идёт без нашего компьютера.

Зачем в облаке. Дома смена ходит из планировщика Windows — то есть работает ровно
тогда, когда включён компьютер, а его выключают. 19.08.2026 из-за этого канал
@playback простоял двое суток с мёртвым ключом, и это выглядело как «нечего постить».
Раннеры GitHub включены всегда и бесплатны для публичного репозитория, а клипы туда
и так уезжают ассетами релизов — ради Reels.

Ритм тот же, что и дома, и он тут ГЛАВНОЕ: шесть постов в сутки на канал и пауза
между ними. Раскладывает его расписание (yt.yml зовёт эту смену шесть раз в день в
разное время), а держит — `posted.json`: смена, пришедшая слишком рано, просто
уходит ни с чем. Пачка подряд читается как спам, а алгоритму нужна регулярность.

Файл автономный: никакого clipper, только стандартная библиотека. Он живёт копией
в репозитории-раздаче, куда его кладёт `clipper cloud setup`.

  queue.json   что публиковать (пишет домашний ПК)
  posted.json  что уже опубликовано и что упало (пишет эта смена)

Ключи каналов лежат в секретах репозитория, по одному на канал:
  YT_CLIENT_ID / YT_CLIENT_SECRET   — общие, из client_secret.json
  YT_REFRESH_<КАНАЛ>                — свой у каждого канала, заглавными,
                                      минусы и точки заменены на подчёркивание
"""
from __future__ import annotations

import json
import os
import re
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
TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = ("https://www.googleapis.com/upload/youtube/v3/videos"
              "?uploadType=resumable&part=snippet,status")

PER_DAY = 6             # постов в сутки на канал: тот же потолок, что и дома
MIN_GAP_MIN = 90.0      # минут между постами одного канала
TRIES = 3               # столько раз клип пробуем, прежде чем отложить его совсем


def _read(name: str, default=None):
    try:
        with open(name, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as exc:        # noqa: BLE001
        # Битый файл НЕ притворяется пустым: пустой posted.json означает «не залито
        # ничего», и весь архив уехал бы в ленту вторым заходом.
        print(f"! {name} не читается ({exc}) — смена не пойдёт", flush=True)
        raise


def _write(name: str, data) -> None:
    with open(name, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def secret_name(channel: str) -> str:
    """Имя секрета под ключ канала. `cardchaos-z6s` -> `YT_REFRESH_CARDCHAOS_Z6S`."""
    return "YT_REFRESH_" + re.sub(r"[^A-Z0-9]", "_", channel.upper())


def _post(url: str, data: bytes, headers: dict, method: str = "POST") -> bytes:
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read()


def access_token(channel: str, env: dict) -> str:
    """Обменять долгий ключ канала на короткий пропуск."""
    refresh = env.get(secret_name(channel), "")
    if not refresh:
        raise RuntimeError(f"нет секрета {secret_name(channel)} — канал не подключён к облаку")
    body = urllib.parse.urlencode({
        "client_id": env.get("YT_CLIENT_ID", ""),
        "client_secret": env.get("YT_CLIENT_SECRET", ""),
        "refresh_token": refresh,
        "grant_type": "refresh_token"}).encode()
    got = json.loads(_post(TOKEN_URL, body,
                           {"Content-Type": "application/x-www-form-urlencoded"}))
    token = got.get("access_token")
    if not token:
        raise RuntimeError(f"Google не отдал пропуск: {str(got)[:200]}")
    return str(token)


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=600) as r:
        return r.read()


def upload(token: str, blob: bytes, title: str, desc: str,
           privacy: str = "public") -> str:
    """Залить ролик и вернуть его id. Заливка двухшаговая, как требует Google."""
    meta = json.dumps({
        "snippet": {"title": title[:100], "description": desc[:5000],
                    "categoryId": "20"},          # 20 = Gaming
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(UPLOAD_URL, data=meta, method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Type": "video/*",
        "X-Upload-Content-Length": str(len(blob))})
    with urllib.request.urlopen(req, timeout=120) as r:
        where = r.headers.get("Location")
    if not where:
        raise RuntimeError("Google не сказал, куда лить файл")
    got = json.loads(_post(where, blob, {"Authorization": f"Bearer {token}",
                                         "Content-Type": "video/*"}, method="PUT"))
    vid = got.get("id")
    if not vid:
        raise RuntimeError(f"залилось, но без id: {str(got)[:200]}")
    return str(vid)


def _times_of(posted: dict, channel: str) -> list[float]:
    out = []
    for rec in posted.values():
        if not isinstance(rec, dict) or rec.get("channel") != channel:
            continue
        try:
            out.append(float(rec.get("at_epoch") or 0.0))
        except (TypeError, ValueError):
            continue
    return [t for t in out if t]


def may_post(posted: dict, channel: str, now: float | None = None) -> str:
    """Пустая строка — можно. Иначе причина, почему эта смена уходит ни с чем."""
    now = now or time.time()
    today = [t for t in _times_of(posted, channel)
             if time.gmtime(t)[:3] == time.gmtime(now)[:3]]
    if len(today) >= PER_DAY:
        return f"сегодня на @{channel} уже {len(today)} — потолок {PER_DAY}"
    if today:
        gap = (now - max(today)) / 60.0
        if gap < MIN_GAP_MIN:
            return f"последний пост на @{channel} {gap:.0f} мин назад — держим паузу"
    return ""


def main() -> int:
    env = dict(os.environ)
    queue = _read(QUEUE, {"posts": []}) or {"posts": []}
    posted = _read(POSTED, {}) or {}
    rows = [p for p in (queue.get("posts") or [])
            if str(p.get("platform") or "") == "youtube"
            and str(p.get("clip") or "") not in posted]
    if not rows:
        print("YouTube: очередь пуста", flush=True)
        return 0

    # По одному ролику на канал за смену — ритм задаёт расписание, а не размер пачки.
    done = 0
    for channel in dict.fromkeys(str(p.get("channel") or "") for p in rows):
        why = may_post(posted, channel)
        if why:
            print(f"YouTube: @{channel} пропускает эту смену — {why}", flush=True)
            continue
        row = next(p for p in rows if str(p.get("channel") or "") == channel)
        clip = str(row.get("clip") or "")
        try:
            token = access_token(channel, env)
            vid = upload(token, fetch(str(row.get("url") or "")),
                         str(row.get("title") or clip), str(row.get("caption") or ""))
        except Exception as exc:                        # noqa: BLE001
            tries = int(row.get("tries") or 0) + 1
            print(f"! {clip} -> @{channel}: {exc}", flush=True)
            posted[clip] = {"channel": channel, "error": str(exc)[:300], "tries": tries,
                            "at_epoch": 0.0} if tries >= TRIES else posted.get(clip, {})
            if tries >= TRIES:
                tell(f"⚠️ YouTube: {clip} не уехал {TRIES} раза подряд — {str(exc)[:120]}")
            continue
        posted[clip] = {"channel": channel, "id": vid, "at_epoch": time.time(),
                        "at": time.strftime("%Y-%m-%d %H:%M", time.gmtime())}
        print(f"✓ {clip} -> @{channel} https://youtube.com/shorts/{vid}", flush=True)
        tell(f"✅ YouTube @{channel}\n{row.get('title') or clip}\n"
             f"https://youtube.com/shorts/{vid}")
        done += 1
    _write(POSTED, posted)
    print(f"YouTube: за эту смену уехало {done}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
