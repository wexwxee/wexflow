"""ИИ-помощник автопилота: «опиши словами / резюме» -> черновик фильтров.

Бесплатный движок — Google Gemini Flash (AI Studio). Ключ НЕ зашивается в
сборку: берётся из переменной окружения GEMINI_API_KEY или из secrets.json
(%AppData%\\WexFlow\\salling\\secrets.json -> "gemini_api_key"). Если ключа нет,
функция available() вернёт False, а кнопка в интерфейсе подскажет, что делать.

Безопасность: текст уходит в Gemini только по явному действию пользователя.
Ответ модели мы валидируем по своему справочнику (коды категорий/брендов/
регионов/занятости), а сам черновик всё равно подтверждает человек вручную.
"""
from __future__ import annotations

import json
import os

import httpx

import config

# Модель по умолчанию — бесплатный Flash. Можно переопределить через окружение
# или secrets.json ("gemini_model"), не трогая код.
_DEFAULT_MODEL = "gemini-2.5-flash"


def _secrets() -> dict:
    try:
        if config.SECRETS_PATH.exists():
            return json.loads(config.SECRETS_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    return {}


def api_key() -> str:
    return (os.getenv("GEMINI_API_KEY") or _secrets().get("gemini_api_key", "") or "").strip()


def model_name() -> str:
    return (os.getenv("GEMINI_MODEL") or _secrets().get("gemini_model") or _DEFAULT_MODEL).strip()


def available() -> bool:
    return bool(api_key())


# Запасные модели на случай 429 (нет бесплатной квоты на модель) или 503
# (временная перегрузка). Проверено: на бесплатном тарифе живые — 2.5-flash и
# flash-lite-latest; 2.0-flash у части проектов уже без бесплатной квоты.
_FALLBACK_MODELS = ("gemini-2.5-flash", "gemini-flash-lite-latest", "gemini-2.0-flash-lite")


def _models_to_try() -> list[str]:
    out: list[str] = []
    for m in (model_name(), *_FALLBACK_MODELS):
        if m and m not in out:
            out.append(m)
    return out


# Поля формы, которые ИИ имеет право заполнять. Город/слова — свободный текст;
# остальное — коды из справочника (валидируем ниже). Возраст — under18/adult.
_MULTI_CODE_FIELDS = ("category", "employment_type", "brand", "regions")
_TEXT_FIELDS = ("cities", "keywords", "exclude_keywords")
_NUM_FIELDS = ("max_km", "min_hours", "max_hours", "max_age_days")


def _catalog(categories: dict, brands: dict, employments: dict, regions: dict) -> str:
    def fmt(d: dict) -> str:
        return ", ".join(f"{code}={lbl}" for code, lbl in d.items())
    return (
        f"Категории (category): {fmt(categories)}\n"
        f"Бренды (brand): {fmt(brands)}\n"
        f"Занятость (employment_type): {fmt(employments)}\n"
        f"Регионы (regions): {fmt(regions)}"
    )


def _prompt(text: str, categories: dict, brands: dict, employments: dict, regions: dict) -> str:
    return (
        "Ты помогаешь настроить поиск вакансий в розничной сети Salling Group в Дании "
        "(Netto, Føtex, Bilka и др.). Пользователь описывает словами или резюме, какую "
        "работу ищет. Преврати это в фильтры. Отвечай ТОЛЬКО JSON-объектом без пояснений "
        "вокруг.\n\n"
        "Доступные коды справочника (используй ТОЛЬКО эти коды там, где они нужны):\n"
        f"{_catalog(categories, brands, employments, regions)}\n\n"
        "Ключи JSON и правила:\n"
        "- max_km: число км до работы или null (рядом ~10, по городу ~25, с поездкой ~50).\n"
        "- min_hours, max_hours: часы в неделю (число) или null. 20 — half-time, 37 — датский full-time.\n"
        "- max_age_days: насколько свежей должна быть вакансия (число дней) или null.\n"
        "- category, brand, employment_type, regions: массивы КОДОВ из справочника (пустой массив, если не важно).\n"
        "- age: \"under18\" если школьник/до 18, \"adult\" если 18+, иначе null.\n"
        "- cities: строка с городами через запятую (датское написание, напр. København) или \"\".\n"
        "- keywords: важные слова, которые должны быть в вакансии, через запятую, или \"\".\n"
        "- exclude_keywords: что НЕ предлагать (напр. nat для ночных), через запятую, или \"\".\n"
        "- explanation: одно короткое предложение по-русски, что ты понял.\n\n"
        "Не выдумывай ограничений, которых нет в описании — что не сказано, оставляй null/пустым.\n\n"
        f"Описание пользователя:\n{text.strip()}"
    )


def suggest_filters(
    text: str,
    categories: dict,
    brands: dict,
    employments: dict,
    regions: dict,
) -> dict:
    """Вернёт {"ok": True, "fields": {...}, "explanation": str} или {"ok": False, "error": str}."""
    key = api_key()
    if not key:
        return {"ok": False, "error": "ИИ не подключён: добавь ключ Gemini в secrets.json (gemini_api_key)."}
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "Опиши словами, что ищешь."}
    if len(text) > 8000:
        text = text[:8000]

    body = {
        "contents": [{"parts": [{"text": _prompt(text, categories, brands, employments, regions)}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }

    # Перебор моделей: основная + запасные на случай 429 (нет квоты) / 503 (перегрузка).
    last_error = "Не удалось получить ответ Gemini."
    for mdl in _models_to_try():
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{mdl}:generateContent"
        try:
            r = httpx.post(url, params={"key": key}, json=body, timeout=30)
        except Exception as e:  # noqa: BLE001
            last_error = f"Не вышло связаться с Gemini: {e}"
            continue
        if r.status_code == 200:
            try:
                raw = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                data = json.loads(raw)
            except Exception:  # noqa: BLE001
                last_error = "Не удалось разобрать ответ Gemini."
                continue
            fields, explanation = _sanitize(data, categories, brands, employments, regions)
            return {"ok": True, "fields": fields, "explanation": explanation}
        # ошибка: 429/503 — пробуем следующую модель; иные — отдаём сразу
        try:
            detail = (r.json().get("error", {}) or {}).get("message", "")
        except Exception:  # noqa: BLE001
            detail = r.text[:200]
        last_error = f"Gemini вернул ошибку {r.status_code}: {detail}"
        if r.status_code not in (429, 503):
            return {"ok": False, "error": last_error}
    return {"ok": False, "error": last_error}


def _sanitize(data: dict, categories, brands, employments, regions) -> tuple[dict, str]:
    """Оставляем только валидные значения: коды — из справочника, числа — положительные."""
    valid = {
        "category": set(categories), "brand": set(brands),
        "employment_type": set(employments), "regions": set(regions),
    }
    out: dict = {}

    for f in _NUM_FIELDS:
        v = data.get(f)
        try:
            n = int(float(v)) if v not in (None, "", "null") else None
        except (TypeError, ValueError):
            n = None
        out[f] = str(n) if n and n > 0 else ""

    for f in _MULTI_CODE_FIELDS:
        v = data.get(f) or []
        if isinstance(v, str):
            v = [x.strip() for x in v.split(",")]
        codes = [str(x).strip() for x in v if str(x).strip() in valid[f]]
        out[f] = ",".join(dict.fromkeys(codes))  # без дублей, порядок сохраняем

    age = str(data.get("age") or "").strip().lower()
    out["age"] = age if age in ("under18", "adult") else ""

    for f in _TEXT_FIELDS:
        v = data.get(f)
        out[f] = (v or "").strip() if isinstance(v, str) else ""

    explanation = str(data.get("explanation") or "").strip()[:300]
    return out, explanation
