"""Банк вопросов анкет: что спросил магазин и что ответил человек.

Зачем. Смысл WexFlow — подавать самому. Мешают этому вопросы вакансии
(«готов работать каждые вторые выходные?», «можешь выходить в 06:00?»):
придумывать ответ за человека нельзя, поэтому подача останавливалась.

Как. Коннектор записывает СЮДА каждый встреченный вопрос — вместе с магазином
и типом роли (рядовая или руководящая: у руководящих вопросы свои). На что
ответа нет — человек отвечает один раз в приложении или в телефоне, и дальше
WexFlow ставит этот ответ сам, в этой и во всех следующих анкетах, где вопрос
звучит так же.

Ответ хранится по тексту вопроса, а не по магазину: «готов работать по
выходным» — это факт о человеке, он одинаков для Lidl и для Netto. Магазины и
роли копятся списком — по ним раздел группируется.

Хранение: один JSON в папке данных. Ключ — отпечаток нормализованного текста,
поэтому лишние пробелы, регистр и знаки препинания не плодят дубли.
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

# Перевод датских вопросов на русский. Сначала точные фразы, которые реально
# встречаются в анкетах Salling/Lidl, потом общие правила по ключевым словам.
# Перевод нужен для понимания — отвечает всё равно человек.
_RU_EXACT: tuple[tuple[str, str], ...] = (
    (r"har du erfaring med.*(detail|butik|retail)",
     "Есть ли у тебя опыт работы в рознице (в магазине)?"),
    (r"er du villig til at arbejde hver 2\.? weekend",
     "Готов(а) работать каждые вторые выходные?"),
    (r"er du villig til at arbejde hver weekend",
     "Готов(а) работать каждые выходные?"),
    (r"arbejde om aftenen",
     "Готов(а) работать по вечерам? (смена примерно до 22:00)"),
    (r"kan du m(ø|o)de kl\.? ?0?6[.:]00",
     "Можешь выходить к 06:00 утра?"),
    (r"kan du m(ø|o)de kl\.? ?0?5[.:]00",
     "Можешь выходить к 05:00 утра?"),
    (r"beg(å|a) dig ubesv(æ|a)ret p(å|a) dansk",
     "Свободно ли ты общаешься на датском?"),
    (r"taler du dansk", "Говоришь ли ты по-датски?"),
    (r"har du k(ø|o)rekort", "Есть ли у тебя водительские права?"),
    (r"er du fyldt 18", "Тебе уже исполнилось 18 лет?"),
    (r"er du under 18", "Тебе меньше 18 лет?"),
    (r"kan du arbejde om natten|nattevagt|natarbejde",
     "Готов(а) работать в ночные смены?"),
    (r"er du studerende", "Ты учишься (студент)?"),
    (r"har du erfaring med ledelse|ledelseserfaring",
     "Есть ли у тебя опыт руководства (управления людьми)?"),
    (r"har du erfaring med personaleansvar",
     "Есть ли у тебя опыт работы с ответственностью за персонал?"),
    (r"er du medlem af en fagforening",
     "Состоишь ли ты в профсоюзе?"),
    (r"har du mulighed for at arbejde p(å|a) helligdage",
     "Можешь ли работать в праздничные дни?"),
    (r"kan du l(ø|o)fte", "Можешь ли поднимать тяжести?"),
)

# Общие подсказки, когда точная фраза не совпала: переводим смысл по словам.
_RU_HINTS: tuple[tuple[str, str], ...] = (
    (r"weekend", "Вопрос про работу по выходным"),
    (r"aften", "Вопрос про вечерние смены"),
    (r"\bnat\b|nattevagt", "Вопрос про ночные смены"),
    (r"morgen|tidlig", "Вопрос про ранние утренние смены"),
    (r"erfaring", "Вопрос про твой опыт"),
    (r"dansk", "Вопрос про датский язык"),
    (r"k(ø|o)rekort", "Вопрос про водительские права"),
    (r"ledelse|leder", "Вопрос про опыт руководства"),
    (r"18\s*(å|a)r", "Вопрос про возраст (18 лет)"),
    (r"studerende|studie", "Вопрос про учёбу"),
)


def path():
    return config.DATA_DIR / "form_questions.json"


def translate_ru(text: str) -> str:
    """Русское пояснение к вопросу анкеты. Пусто — если сказать нечего.

    Ничего не выдумываем: не узнали вопрос — лучше без перевода, чем неверный.
    """
    clean = str(text or "").strip().lower()
    if not clean:
        return ""
    for pattern, ru in _RU_EXACT:
        if re.search(pattern, clean, re.I):
            return ru
    for pattern, ru in _RU_HINTS:
        if re.search(pattern, clean, re.I):
            return ru
    return ""


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


def _merge(values, addition) -> list:
    out = [v for v in (values or []) if v]
    if addition and addition not in out:
        out.append(addition)
    return out[:12]


def record(questions, source: str = "", job_title: str = "",
           store_label: str = "", role_kind: str = "") -> int:
    """Запомнить встреченные вопросы. Возвращает, сколько из них новых.

    questions: [{"text": ..., "options": [...]}] — как их увидел коннектор.
    source/store_label — магазин («lidl» / «Lidl»), role_kind — «lead» или
    «regular»: у руководящих ролей анкета спрашивает своё.
    Ответы уже отвеченных вопросов не трогаем.
    """
    new = 0
    role_kind = role_kind if role_kind in {"lead", "regular"} else ""
    store_key = str(source or "").strip()[:40]
    label = str(store_label or "").strip()[:60] or store_key.title()
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
                    "text_ru": translate_ru(text),
                    "options": options,
                    "answer": "",
                    "stores": _merge([], store_key),
                    "store_labels": _merge([], label),
                    "roles": _merge([], role_kind),
                    "source": store_key,
                    "job_title": str(job_title or "")[:160],
                    "seen_at": time.time(),
                    "seen_count": 1,
                }
                new += 1
            else:
                item["seen_at"] = time.time()
                item["seen_count"] = int(item.get("seen_count") or 0) + 1
                item["stores"] = _merge(item.get("stores"), store_key)
                item["store_labels"] = _merge(item.get("store_labels"), label)
                item["roles"] = _merge(item.get("roles"), role_kind)
                if not item.get("text_ru"):
                    item["text_ru"] = translate_ru(text)
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
    rows = []
    for key, value in items.items():
        row = dict(value, key=key)
        row.setdefault("stores", [row.get("source") or ""])
        row.setdefault("store_labels", [(row.get("source") or "").title()])
        row.setdefault("roles", [])
        if not row.get("text_ru"):
            row["text_ru"] = translate_ru(row.get("text") or "")
        rows.append(row)
    rows.sort(key=lambda r: (bool(r.get("answer")), -float(r.get("seen_at") or 0)))
    return rows


def by_store() -> list[dict]:
    """Вопросы, сгруппированные по магазинам — как их показывает интерфейс.

    Один и тот же вопрос может встречаться в нескольких сетях: он попадёт в
    каждую, но ответ у него общий (это факт о человеке, а не о магазине).
    Внутри магазина вопросы руководящих ролей идут отдельной группой.
    """
    stores: dict[str, dict] = {}
    for row in all_items():
        keys = [k for k in (row.get("stores") or []) if k] or ["другое"]
        labels = [x for x in (row.get("store_labels") or []) if x]
        for index, store_key in enumerate(keys):
            label = labels[index] if index < len(labels) else store_key.title()
            group = stores.setdefault(store_key, {
                "key": store_key, "label": label, "items": [], "lead_items": [],
            })
            if "lead" in (row.get("roles") or []) and "regular" not in (row.get("roles") or []):
                group["lead_items"].append(row)
            else:
                group["items"].append(row)
    out = []
    for group in stores.values():
        rows = group["items"] + group["lead_items"]
        group["pending"] = len([r for r in rows if not r.get("answer")])
        group["total"] = len(rows)
        out.append(group)
    out.sort(key=lambda g: (-g["pending"], g["label"].lower()))
    return out


def pending() -> list[dict]:
    """Вопросы без ответа — именно их приложение просит закрыть."""
    return [row for row in all_items() if not row.get("answer")]


def pending_count() -> int:
    return len(pending())


def cloud_payload(limit: int = 60) -> list[dict]:
    """Компактный список для телефона: там на эти же вопросы можно ответить."""
    rows = []
    for row in all_items()[:limit]:
        rows.append({
            "key": row.get("key", ""),
            "text": row.get("text", ""),
            "textRu": row.get("text_ru", ""),
            "answer": row.get("answer", ""),
            "store": (row.get("store_labels") or [""])[0],
            "lead": "lead" in (row.get("roles") or []) and "regular" not in (row.get("roles") or []),
        })
    return rows
