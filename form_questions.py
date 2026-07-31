"""Банк вопросов анкет: что спросил магазин и что ответил человек.

Зачем. Смысл WexFlow — подавать самому. Мешают этому вопросы вакансии
(«готов работать каждые вторые выходные?», «можешь выходить в 06:00?»):
придумывать ответ за человека нельзя, поэтому подача останавливалась.

Как. Коннектор записывает СЮДА каждый встреченный вопрос. На что ответа нет —
человек отвечает один раз в приложении, и дальше WexFlow ставит этот ответ сам,
в этой и во всех следующих анкетах, где вопрос звучит так же.

Хранение: один JSON в папке данных. Ключ — отпечаток нормализованного текста
вопроса, поэтому лишние пробелы, регистр и знаки препинания не плодят дубли.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time

import config
import json_store

_LOCK = threading.RLock()
_MAX_QUESTIONS = 400          # банк не должен расти бесконечно
_MAX_TEXT = 400


def path():
    return config.DATA_DIR / "form_questions.json"


def normalize(text: str) -> str:
    """Один и тот же вопрос в разных вакансиях должен давать один ключ."""
    clean = str(text or "").strip().lower()
    clean = clean.replace("ё", "е")
    clean = re.sub(r"\s+", " ", clean)
    return re.sub(r"[^0-9a-zA-Zа-яА-ЯæøåÆØÅ ]+", "", clean).strip()


def key_for(text: str) -> str:
    return hashlib.sha1(normalize(text).encode("utf-8")).hexdigest()[:12]


def _load() -> dict:
    data = json_store.read_json(path(), default={}, expected_type=dict) or {}
    items = data.get("items")
    return {"items": items if isinstance(items, dict) else {}}


def _save(state: dict) -> None:
    items = state.get("items") or {}
    if len(items) > _MAX_QUESTIONS:
        # оставляем свежие: старые вопросы всё равно уже не встречаются
        ordered = sorted(items.items(), key=lambda kv: kv[1].get("seen_at", 0), reverse=True)
        items = dict(ordered[:_MAX_QUESTIONS])
        state = {"items": items}
    json_store.atomic_write_json(path(), state, indent=1)


def record(questions, source: str = "", job_title: str = "") -> int:
    """Запомнить встреченные вопросы. Возвращает, сколько из них новых.

    questions: [{"text": ..., "options": [...]}] — как их увидел коннектор.
    Ответы уже отвеченных вопросов не трогаем.
    """
    new = 0
    with _LOCK:
        state = _load()
        items = state["items"]
        for raw in questions or []:
            text = str((raw or {}).get("text") or "").strip()[:_MAX_TEXT]
            if not text:
                continue
            options = [str(o)[:80] for o in (raw or {}).get("options") or []][:12]
            key = key_for(text)
            item = items.get(key)
            if item is None:
                items[key] = {
                    "text": text,
                    "options": options,
                    "answer": "",
                    "source": str(source or "")[:40],
                    "job_title": str(job_title or "")[:160],
                    "seen_at": time.time(),
                    "seen_count": 1,
                }
                new += 1
            else:
                item["seen_at"] = time.time()
                item["seen_count"] = int(item.get("seen_count") or 0) + 1
                if options and not item.get("options"):
                    item["options"] = options
                if job_title and not item.get("job_title"):
                    item["job_title"] = str(job_title)[:160]
        _save(state)
    return new


def set_answer(key: str, value: str) -> bool:
    """Сохранить ответ человека. Пустое значение = «пока не отвечаю»."""
    key = str(key or "").strip()
    with _LOCK:
        state = _load()
        item = state["items"].get(key)
        if item is None:
            return False
        item["answer"] = str(value or "").strip()[:120]
        item["answered_at"] = time.time() if item["answer"] else 0
        _save(state)
    return True


def answer_for(text: str) -> str:
    """Ответ человека на этот вопрос — или пустая строка."""
    item = _load()["items"].get(key_for(text))
    return str((item or {}).get("answer") or "")


def all_items() -> list[dict]:
    items = _load()["items"]
    rows = [dict(value, key=key) for key, value in items.items()]
    rows.sort(key=lambda r: (bool(r.get("answer")), -float(r.get("seen_at") or 0)))
    return rows


def pending() -> list[dict]:
    """Вопросы без ответа — именно их приложение просит закрыть."""
    return [row for row in all_items() if not row.get("answer")]


def pending_count() -> int:
    return len(pending())
