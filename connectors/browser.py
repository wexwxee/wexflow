"""Запуск видимого браузера для ассистированной подачи коннекторов.

Самостоятельный (не импортирует код Salling), но повторяет проверенный подход
apply.py: сперва системный Chrome/Edge (у встроенного в Playwright Chromium на
этой машине не хватает Visual C++ Redistributable), затем встроенный как
последний шанс. Persistent-контекст хранится отдельно от Salling
(`%AppData%\\WexFlow\\salling\\connector_browser` в сборке, или рядом в dev),
чтобы коннекторы и старый Salling не мешали друг другу.

Этот же запуск переиспользуют будущие филлеры (Greenhouse, Ashby и т.д.).
"""
from __future__ import annotations

import paths

PROFILE_DIR = paths.DATA_DIR / "connector_browser"

_NO_AUTOFILL = [
    "--disable-features=AutofillServerCommunication,AutofillEnableAccountWalletStorage,PasswordManagerOnboarding",
    "--disable-save-password-bubble",
]


def launch_browser(p):
    """Открыть видимый браузер с сохранённым контекстом. Бросает, если ни один
    браузер не стартовал (тогда филлер сообщает пользователю что установить)."""
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    last_err = None
    for opts in ({"channel": "chrome"}, {"channel": "msedge"}, {}):
        try:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=False,
                locale="da-DK",
                args=_NO_AUTOFILL,
                **opts,
            )
            print(f"  браузер: {opts.get('channel', 'встроенный chromium')}")
            return ctx
        except Exception as e:
            last_err = e
    raise RuntimeError(
        "Не удалось открыть браузер. Установи Google Chrome или Microsoft "
        f"Visual C++ Redistributable (x64). Последняя ошибка: {last_err}"
    )
