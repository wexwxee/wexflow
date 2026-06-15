"""Универсальная подача: «дай ссылку — заполню».

detect(url) распознаёт платформу по адресу, prepare() направляет в нужный
заполнитель (точный для Teamtailor, универсальный для остальных). Работает для
ЛЮБОЙ компании поддерживаемой платформы, а не только из каталога — в этом вся
сила: один автозаполнитель = вся вселенная фирм платформы.

Запуск:  python -m connectors.apply_dispatch <url-вакансии> [--keep-open]
"""
from __future__ import annotations

import re
import sys
import time

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

# (ключ платформы, человекочитаемое имя, регэксп по адресу)
_PLATFORMS = [
    ("teamtailor", "Teamtailor", re.compile(r"\.teamtailor\.com", re.I)),
    ("greenhouse", "Greenhouse", re.compile(r"greenhouse\.io|grnh\.se", re.I)),
    ("ashby", "Ashby", re.compile(r"ashbyhq\.com", re.I)),
    ("lever", "Lever", re.compile(r"jobs\.lever\.co", re.I)),
    ("recruitee", "Recruitee", re.compile(r"\.recruitee\.com", re.I)),
    ("workable", "Workable", re.compile(r"\.workable\.com", re.I)),
]


def detect(url: str) -> str | None:
    """Вернуть ключ платформы или None, если ссылка не поддерживается."""
    for key, _name, rx in _PLATFORMS:
        if rx.search(url or ""):
            return key
    return None


def platform_name(key: str) -> str:
    for k, name, _ in _PLATFORMS:
        if k == key:
            return name
    return key or "неизвестно"


def prepare(page, url: str, profile: dict) -> str:
    """Заполнить форму по ссылке. Возвращает ключ платформы (или '')."""
    key = detect(url)
    if key == "teamtailor":
        from connectors import teamtailor_apply
        teamtailor_apply.prepare(page, url, profile)
    elif key:
        from connectors import generic_apply
        generic_apply.prepare(page, url, profile, platform=platform_name(key))
    else:
        # неизвестная платформа — всё равно пробуем «по подписям», вдруг повезёт
        from connectors import generic_apply
        generic_apply.prepare(page, url, profile, platform="форма")
    return key or ""


def _wait_until_closed(ctx) -> None:
    while True:
        try:
            _ = ctx.pages
            if not ctx.browser or not ctx.browser.is_connected():
                return
        except Exception:
            return
        time.sleep(1.0)


def run(url: str, keep_open: bool = False) -> None:
    from connectors.fill_common import load_profile
    from connectors.browser import launch_browser
    from playwright.sync_api import sync_playwright

    profile = load_profile()
    key = detect(url)
    print(f"платформа: {platform_name(key) if key else 'не распознана (пробую универсально)'}")
    with sync_playwright() as p:
        ctx = launch_browser(p)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            prepare(page, url, profile)
        except Exception as e:
            print("  warning:", e)
        if keep_open:
            _wait_until_closed(ctx)
        else:
            input("\nНажми Enter здесь, когда закончишь, чтобы закрыть браузер...")
        try:
            ctx.close()
        except Exception:
            pass


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit("Использование: python -m connectors.apply_dispatch <url> [--keep-open]")
    run(args[0], keep_open="--keep-open" in sys.argv)
