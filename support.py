"""Куда писать человеку, когда WexFlow сам себе помочь не может.

Одно место на всё приложение: канал с новостями, личка поддержки, форма
обратной связи. Пусто = ссылку просто не показываем (текст остаётся честным,
без «нажми сюда», ведущего в никуда).

Заполняется владельцем: канал Ивана появится позже, поэтому здесь заготовки,
а не выдуманные адреса.
"""
from __future__ import annotations

# Телеграм-канал с новостями и обновлениями (например "@wexflow_news").
NEWS_CHANNEL = ""

# Личка поддержки — работает уже сейчас.
SUPPORT_DM = "@wexwxeee"

# Форма/ссылка для обратной связи (когда появится).
FEEDBACK_URL = ""


def _tg_link(handle: str) -> str:
    handle = str(handle or "").strip()
    if not handle:
        return ""
    if handle.startswith("http"):
        return handle
    return "https://t.me/" + handle.lstrip("@")


def channel_link() -> str:
    return _tg_link(NEWS_CHANNEL)


def support_link() -> str:
    return _tg_link(SUPPORT_DM)


def contact_line() -> str:
    """Короткая строка «куда писать» для сообщений об ошибке.

    Возвращает пустую строку, если ничего не настроено, — тогда сообщение
    просто не обещает человеку несуществующую поддержку.
    """
    parts: list[str] = []
    if NEWS_CHANNEL:
        parts.append(f"новости и обновления — {NEWS_CHANNEL}")
    if SUPPORT_DM:
        parts.append(f"поддержка — {SUPPORT_DM}")
    if FEEDBACK_URL:
        parts.append(f"обратная связь — {FEEDBACK_URL}")
    return " · ".join(parts)


def payload() -> dict:
    """Те же контакты для интерфейса и телефона."""
    return {
        "channel": NEWS_CHANNEL,
        "channelUrl": channel_link(),
        "support": SUPPORT_DM,
        "supportUrl": support_link(),
        "feedbackUrl": FEEDBACK_URL,
        "line": contact_line(),
    }
