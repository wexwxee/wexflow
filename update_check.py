"""Проверка обновлений через GitHub Releases (без сторонних зависимостей).

- В фоне дёргает api.github.com/.../releases/latest, сравнивает тег с текущей
  версией. Если на GitHub версия новее — возвращает информацию о ней.
- Само скачивание/установка — отдельная функция, вызывается по согласию
  пользователя из окна приложения.
- Если репозиторий не задан или нет интернета — тихо возвращает None
  (приложение работает как обычно).
"""
import json
import re
import sys
import urllib.request
from pathlib import Path

import version


def _norm(tag: str) -> tuple:
    """'v1.2.3' / '1.2.3' -> (1, 2, 3) для сравнения."""
    nums = re.findall(r"\d+", tag or "")
    return tuple(int(n) for n in nums[:4]) or (0,)


def check() -> dict | None:
    """Вернуть {'version','url','notes'} если есть версия новее текущей, иначе None."""
    repo = (getattr(version, "GITHUB_REPO", "") or "").strip()
    if not repo:
        return None
    api = f"https://api.github.com/repos/{repo}/releases/latest"
    try:
        req = urllib.request.Request(api, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "WexFlow-Updater",
        })
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 — нет сети/репо/релиза — молча выходим
        return None

    tag = data.get("tag_name") or data.get("name") or ""
    if _norm(tag) <= _norm(version.__version__):
        return None

    # ищем zip-ассет дистрибутива, иначе ссылку на страницу релиза
    download = data.get("html_url", "")
    for asset in data.get("assets", []):
        name = (asset.get("name") or "").lower()
        if name.endswith(".zip"):
            download = asset.get("browser_download_url", download)
            break
    return {"version": tag, "url": download, "notes": (data.get("body") or "")[:600]}


if __name__ == "__main__":
    info = check()
    print(json.dumps(info, ensure_ascii=False) if info else "up-to-date")
