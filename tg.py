"""Telegram Bot API — тонкая обёртка (long polling), ТОЛЬКО стандартная библиотека.

ВСЯ работа с Telegram сосредоточена здесь нарочно: когда приложение вырастет в
облачную версию, переносить в облако нужно будет только этот слой (отправку и
приём), а остальной код менять не придётся.

Важно понимать: сам бот ничего не «хостит» — он живёт на серверах Telegram и
доступен всегда. Это приложение лишь говорит ему «отправь сообщение» и
спрашивает «что нажали» (getUpdates), пока приложение запущено.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

_API = "https://api.telegram.org/bot{token}/{method}"


def _call(token: str, method: str, params: dict | None = None, timeout: int = 20) -> dict:
    """Вызвать метод Bot API. Возвращает разобранный JSON Telegram
    ({"ok": bool, "result"/"description": ...}) либо {"ok": False, "error": ...}."""
    if not token:
        return {"ok": False, "error": "no token"}
    url = _API.format(token=token.strip(), method=method)
    data = None
    if params:
        # вложенные структуры (reply_markup и т.п.) Telegram ждёт строкой-JSON
        flat = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
                for k, v in params.items() if v is not None}
        data = urllib.parse.urlencode(flat).encode("utf-8")
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {"ok": False, "error": f"http {e.code}"}
    except Exception as e:  # noqa: BLE001 — сеть/таймаут не должны ронять вызывающего
        return {"ok": False, "error": str(e)[:200]}


def get_me(token: str) -> dict:
    """Проверка токена. {"ok":True,"username":...,"name":...} или {"ok":False,"error":...}."""
    r = _call(token, "getMe", timeout=10)
    if r.get("ok"):
        res = r.get("result", {})
        return {"ok": True, "username": res.get("username", ""), "name": res.get("first_name", "")}
    return {"ok": False, "error": r.get("description") or r.get("error") or "bad token"}


def _keyboard(buttons):
    """buttons: список рядов [[(text, callback_data), ...], ...] → inline_keyboard."""
    return {"inline_keyboard": [
        [{"text": t, "callback_data": d} for (t, d) in row] for row in buttons
    ]}


def send_message(token: str, chat_id, text: str, buttons=None) -> dict:
    """Отправить сообщение (опц. с кнопками). buttons — ряды [(text,data),...]."""
    params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True}
    if buttons:
        params["reply_markup"] = _keyboard(buttons)
    r = _call(token, "sendMessage", params)
    if r.get("ok"):
        return {"ok": True, "message_id": r["result"]["message_id"]}
    return {"ok": False, "error": r.get("description") or r.get("error")}


def edit_message(token: str, chat_id, message_id, text: str, buttons=None) -> dict:
    """Переписать ранее отправленное сообщение (например, убрать кнопки после выбора)."""
    params = {"chat_id": chat_id, "message_id": message_id, "text": text,
              "parse_mode": "HTML", "disable_web_page_preview": True}
    params["reply_markup"] = _keyboard(buttons) if buttons else {"inline_keyboard": []}
    return _call(token, "editMessageText", params)


def answer_callback(token: str, callback_id: str, text: str = "") -> dict:
    """Закрыть «часики» на нажатой кнопке (опц. показать всплывашку)."""
    return _call(token, "answerCallbackQuery",
                 {"callback_query_id": callback_id, "text": text})


def get_updates(token: str, offset: int = 0, timeout: int = 25) -> dict:
    """Long polling. {"ok":True,"updates":[...]} . timeout — сколько секунд ждать ответ."""
    r = _call(token, "getUpdates",
              {"offset": offset, "timeout": timeout,
               "allowed_updates": ["message", "callback_query"]},
              timeout=timeout + 10)
    if r.get("ok"):
        return {"ok": True, "updates": r.get("result", [])}
    return {"ok": False, "error": r.get("description") or r.get("error")}
