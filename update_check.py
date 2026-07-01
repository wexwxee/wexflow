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


def _sha256_from_digest(asset: dict | None) -> str:
    """GitHub отдаёт для каждого ассета поле digest вида 'sha256:<хэш>'. Берём хэш."""
    digest = ((asset or {}).get("digest") or "").strip().lower()
    if digest.startswith("sha256:"):
        h = digest.split(":", 1)[1].strip()
        if re.fullmatch(r"[0-9a-f]{64}", h):
            return h
    return ""


def _fetch_sha256_asset(assets: list) -> str:
    """Запасной путь: рядом с zip лежит файл *.sha256 — скачиваем и читаем хэш."""
    for asset in assets or []:
        name = (asset.get("name") or "").lower()
        if not name.endswith(".sha256"):
            continue
        url = asset.get("browser_download_url")
        if not url:
            continue
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "WexFlow-Updater"})
            with urllib.request.urlopen(req, timeout=8) as r:
                text = r.read(4096).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 — нет файла/сети — просто нет суммы
            return ""
        m = re.search(r"[0-9a-fA-F]{64}", text)
        if m:
            return m.group(0).lower()
    return ""


def _resolve_sha256(zip_asset: dict | None, assets: list) -> str:
    """Ожидаемая контрольная сумма zip: сперва из digest GitHub, затем из *.sha256."""
    return _sha256_from_digest(zip_asset) or _fetch_sha256_asset(assets)


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
    assets = data.get("assets", []) or []
    download = data.get("html_url", "")
    zip_asset = None
    for asset in assets:
        name = (asset.get("name") or "").lower()
        if name.endswith(".zip"):
            download = asset.get("browser_download_url", download)
            zip_asset = asset
            break
    return {
        "version": tag,
        "url": download,
        "notes": (data.get("body") or "")[:600],
        "sha256": _resolve_sha256(zip_asset, assets),
    }


if __name__ == "__main__":
    info = check()
    print(json.dumps(info, ensure_ascii=False) if info else "up-to-date")
