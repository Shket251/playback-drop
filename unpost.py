"""Снять пост из ленты. Работает только из облака — как и всё остальное.

Публикация умеет ошибаться не только «плохим клипом»: 18.08.2026 на игровой канал
уехали три распаковки карточек, потому что клипы заказа легли не в свою папку и
сторож их не увидел. Убрать их из очереди мало — они уже в ленте.

Руками это делается в приложении, но вход в Instagram с российского адреса
циклится, и до graph.instagram.com оттуда связи нет вовсе. Значит снимать должен
тот же исполнитель, что и публиковал.

Опасность у файла ровно одна: он удаляет НАСОВСЕМ, восстановить пост нельзя.
Поэтому id постов приходят только руками, через кнопку на вкладке Actions, —
ни расписания, ни чтения очереди здесь нет и быть не должно.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://graph.instagram.com/v23.0"


def unpost(media_id: str, token: str) -> None:
    """Снять один пост. Молчание тут недопустимо: ошибку печатаем словами."""
    url = f"{API}/{media_id}?" + urllib.parse.urlencode({"access_token": token})
    req = urllib.request.Request(url, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            msg = json.loads(raw)["error"]["message"]
        except Exception:                                  # noqa: BLE001
            msg = raw[:300] or str(exc)
        raise RuntimeError(msg) from None
    # `success: true` — обычный ответ Meta на удаление; пустой ответ тоже считается
    # успехом только если код был 200, иначе мы бы уже улетели в HTTPError.
    if body and body.get("success") is False:
        raise RuntimeError(f"отказ без объяснения: {body}")


def permalink(media_id: str, token: str) -> str:
    """Ссылка на пост. Нужна ровно тогда, когда снять не вышло.

    Маркер, выданный по входу через Instagram, удалять посты не умеет: Meta даёт
    `instagram_manage_contents` только токенам с Facebook-логином, и на DELETE
    отвечает «Unsupported delete request». Значит человек пойдёт снимать руками,
    и единственное, чем ему тут можно помочь, — дать прямую ссылку.
    """
    url = f"{API}/{media_id}?" + urllib.parse.urlencode(
        {"fields": "permalink,caption", "access_token": token})
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            return str(json.loads(resp.read().decode("utf-8") or "{}").get("permalink") or "")
    except (urllib.error.URLError, OSError):
        return ""


def main() -> int:
    token = os.environ.get("IG_TOKEN", "").strip()
    if not token:
        print("нет секрета IG_TOKEN — снимать нечем")
        return 1
    ids = [x.strip() for x in " ".join(sys.argv[1:]).replace(",", " ").split() if x.strip()]
    if not ids:
        print("не переданы id постов — снимать нечего")
        return 1
    bad = 0
    for media_id in ids:
        try:
            unpost(media_id, token)
        except RuntimeError as exc:
            bad += 1
            link = permalink(media_id, token)
            print(f"✗ {media_id}: {exc}")
            if link:
                print(f"   снять руками: {link}")
        else:
            print(f"✓ {media_id}: снят")
    print(f"снято: {len(ids) - bad} из {len(ids)}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
