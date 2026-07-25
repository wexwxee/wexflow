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

import ai_secrets
import ai_usage
import config
import paths

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
    """Ключ Gemini ТЕКУЩЕГО аккаунта (переходный фасад).

    Порядок: зашифрованное хранилище аккаунта -> (dev) переменная окружения ->
    (только dev, не в собранном приложении) legacy secrets.json. В собранном
    приложении legacy-ключ не становится общим для всех аккаунтов.
    """
    key = ai_secrets.get_api_key("gemini")
    if key:
        return key
    if not paths.is_frozen():
        return (_secrets().get("gemini_api_key", "") or "").strip()
    return ""


def model_name() -> str:
    override = ai_secrets.info("gemini").get("model")
    return (os.getenv("GEMINI_MODEL") or override or _secrets().get("gemini_model") or _DEFAULT_MODEL).strip()


def available() -> bool:
    """Доступен ли ИИ для текущего аккаунта (любой провайдер: Gemini или Groq)."""
    try:
        import ai_gateway
        return ai_gateway.available()
    except Exception:  # noqa: BLE001
        return bool(api_key())


def gemini_available() -> bool:
    """Именно Gemini подключён (для роутинга/совместимости)."""
    return bool(api_key())


def _legacy_from_result(res, *, want_fields=False, catalogs=None):
    """Преобразовать AIResult шлюза в старый формат ответа ai_filters."""
    if res.ok and isinstance(res.data, dict):
        if want_fields and catalogs is not None:
            fields, explanation = _sanitize(res.data, *catalogs)
            return {"ok": True, "fields": fields, "explanation": explanation}
        return {"ok": True, "data": res.data, "model": res.model}
    if res.error_code == "not_connected":
        return {"ok": False, "error": "ИИ не подключён: подключи Gemini или Groq в разделе «ИИ и лимиты»."}
    return {"ok": False, "error": res.error_message or "Не удалось получить ответ ИИ."}


# Запасная стабильная 2.5-модель на случай 429/503. Gemini 2.0 удалён из
# перебора: Google закрыл его в июне 2026, лишняя попытка только тратила время
# и путала локальный счётчик запросов.
_FALLBACK_MODELS = ("gemini-2.5-flash", "gemini-2.5-flash-lite")


def _models_to_try() -> list[str]:
    out: list[str] = []
    for m in (model_name(), *_FALLBACK_MODELS):
        if m and m not in out:
            out.append(m)
    return out


def generate_json(
    prompt: str,
    *,
    temperature: float = 0.1,
    timeout: float = 40.0,
) -> dict:
    """Small shared JSON gateway for explicit, user-triggered AI tasks.

    Gemini подключён -> родной путь (поведение 1.3.21). Иначе, если подключён
    другой провайдер (Groq) -> маршрут через общий gateway.
    """
    key = api_key()
    if not key:
        try:
            import ai_gateway
            if ai_gateway.available():
                res = ai_gateway.generate_json(str(prompt or "").strip(),
                                               temperature=temperature, timeout=timeout)
                return _legacy_from_result(res)
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "error": "ИИ не подключён: подключи Gemini или Groq в настройках."}
    prompt = str(prompt or "").strip()
    if not prompt:
        return {"ok": False, "error": "Пустой запрос к ИИ."}
    body = {
        "contents": [{"parts": [{"text": prompt[:50000]}]}],
        "generationConfig": {
            "temperature": max(0.0, min(float(temperature), 1.0)),
            "responseMimeType": "application/json",
        },
    }
    last_error = "Не удалось получить ответ Gemini."
    for mdl in _models_to_try():
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{mdl}:generateContent"
        try:
            response = httpx.post(
                url,
                headers={"x-goog-api-key": key},
                json=body,
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001
            last_error = f"Не вышло связаться с Gemini: {exc}"
            continue
        try:
            response_payload = response.json()
        except Exception:  # noqa: BLE001
            response_payload = {}
        ai_usage.record_response(mdl, response.status_code, response_payload)
        if response.status_code == 200:
            try:
                raw = response_payload["candidates"][0]["content"]["parts"][0]["text"]
                data = json.loads(raw)
            except Exception:  # noqa: BLE001
                last_error = "Не удалось разобрать ответ Gemini."
                continue
            if isinstance(data, dict):
                return {"ok": True, "data": data, "model": mdl}
            last_error = "Gemini вернул неожиданный формат."
            continue
        try:
            detail = (response_payload.get("error", {}) or {}).get("message", "")
        except Exception:  # noqa: BLE001
            detail = response.text[:200]
        last_error = f"Gemini вернул ошибку {response.status_code}: {detail}"
        if response.status_code not in (429, 503):
            break
    return {"ok": False, "error": last_error}


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
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "Опиши словами, что ищешь."}
    if len(text) > 8000:
        text = text[:8000]

    key = api_key()
    if not key:
        try:
            import ai_gateway
            if ai_gateway.available():
                res = ai_gateway.generate_json(
                    _prompt(text, categories, brands, employments, regions),
                    temperature=0.2, timeout=30)
                return _legacy_from_result(
                    res, want_fields=True,
                    catalogs=(categories, brands, employments, regions))
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "error": "ИИ не подключён: подключи Gemini или Groq в разделе «ИИ и лимиты»."}

    body = {
        "contents": [{"parts": [{"text": _prompt(text, categories, brands, employments, regions)}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }

    # Перебор моделей: основная + запасные на случай 429 (нет квоты) / 503 (перегрузка).
    last_error = "Не удалось получить ответ Gemini."
    for mdl in _models_to_try():
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{mdl}:generateContent"
        try:
            # Ключ — в заголовке, а не в URL (?key=…), чтобы он не утекал в логи
            # и в строки исключений при сетевых ошибках.
            r = httpx.post(url, headers={"x-goog-api-key": key}, json=body, timeout=30)
        except Exception as e:  # noqa: BLE001
            last_error = f"Не вышло связаться с Gemini: {e}"
            continue
        try:
            response_payload = r.json()
        except Exception:  # noqa: BLE001
            response_payload = {}
        ai_usage.record_response(mdl, r.status_code, response_payload)
        if r.status_code == 200:
            try:
                raw = response_payload["candidates"][0]["content"]["parts"][0]["text"]
                data = json.loads(raw)
            except Exception:  # noqa: BLE001
                last_error = "Не удалось разобрать ответ Gemini."
                continue
            fields, explanation = _sanitize(data, categories, brands, employments, regions)
            return {"ok": True, "fields": fields, "explanation": explanation}
        # ошибка: 429/503 — пробуем следующую модель; иные — отдаём сразу
        try:
            detail = (response_payload.get("error", {}) or {}).get("message", "")
        except Exception:  # noqa: BLE001
            detail = r.text[:200]
        last_error = f"Gemini вернул ошибку {r.status_code}: {detail}"
        if r.status_code not in (429, 503):
            return {"ok": False, "error": last_error}
    return {"ok": False, "error": last_error}


def _chat_system(categories, brands, employments, regions) -> str:
    return (
        "Ты дружелюбный помощник по настройке поиска вакансий в сети Salling Group "
        "в Дании (Netto, Føtex, Bilka и др.). Веди короткий живой диалог ПО-РУССКИ: "
        "задавай по ОДНОМУ простому вопросу за раз (где искать и как далеко от дома, "
        "кем хочет работать, сколько часов в неделю, возраст, важны ли бренды). "
        "Можешь отвечать и на обычные вопросы пользователя своими словами. Как только "
        "данных хватает для поиска — заполни фильтры и поставь done=true.\n\n"
        "Коды справочника (в fields используй ТОЛЬКО эти коды):\n"
        f"{_catalog(categories, brands, employments, regions)}\n\n"
        "Отвечай СТРОГО JSON-объектом без текста вокруг: "
        '{"reply": "строка для пользователя (вопрос или комментарий)", '
        '"done": true если фильтры готовы иначе false, '
        '"fields": объект фильтров или null}. '
        "Поля fields: max_km/min_hours/max_hours/max_age_days — числа или null; "
        "category/brand/employment_type/regions — массивы КОДОВ из справочника; "
        'age — "under18"/"adult"/null; cities/keywords/exclude_keywords — строки '
        "(датские города, напр. København). Не выдумывай ограничений, которых нет в диалоге."
    )


def chat(messages, categories, brands, employments, regions) -> dict:
    """Многоходовый диалог настройки фильтров. messages: [{"role":"user"|"model","text":str}].
    Возвращает {"ok":True,"reply":str,"done":bool,"fields":dict|None} или {"ok":False,"error":str}."""
    msgs = [m for m in (messages or [])
            if isinstance(m, dict) and str(m.get("text") or "").strip()][-16:]
    if not msgs:
        return {"ok": False, "error": "Напиши, что ищешь."}

    key = api_key()
    if not key:
        try:
            import ai_gateway
            if ai_gateway.available():
                res = ai_gateway.chat(
                    msgs,
                    system=_chat_system(categories, brands, employments, regions),
                    json_mode=True, temperature=0.3, timeout=30)
                if res.ok and isinstance(res.data, dict):
                    data = res.data
                    reply = str(data.get("reply") or "").strip()[:1200]
                    done = bool(data.get("done"))
                    fields = None
                    if done and isinstance(data.get("fields"), dict):
                        fields, _ = _sanitize(data["fields"], categories, brands, employments, regions)
                    return {"ok": True, "reply": reply or "Хорошо.", "done": done, "fields": fields}
                return _legacy_from_result(res)
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "error": "ИИ не подключён: подключи Gemini или Groq в разделе «ИИ и лимиты»."}

    contents = [{"role": ("user" if m.get("role") == "user" else "model"),
                 "parts": [{"text": str(m.get("text"))[:2000]}]} for m in msgs]
    body = {
        "systemInstruction": {"parts": [{"text": _chat_system(categories, brands, employments, regions)}]},
        "contents": contents,
        "generationConfig": {"temperature": 0.3, "responseMimeType": "application/json"},
    }
    last_error = "Не удалось получить ответ Gemini."
    for mdl in _models_to_try():
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{mdl}:generateContent"
        try:
            r = httpx.post(url, headers={"x-goog-api-key": key}, json=body, timeout=30)
        except Exception as e:  # noqa: BLE001
            last_error = f"Не вышло связаться с Gemini: {e}"
            continue
        try:
            response_payload = r.json()
        except Exception:  # noqa: BLE001
            response_payload = {}
        ai_usage.record_response(mdl, r.status_code, response_payload)
        if r.status_code == 200:
            try:
                raw = response_payload["candidates"][0]["content"]["parts"][0]["text"]
                data = json.loads(raw)
            except Exception:  # noqa: BLE001
                last_error = "Не удалось разобрать ответ Gemini."
                continue
            reply = str(data.get("reply") or "").strip()[:1200]
            done = bool(data.get("done"))
            fields = None
            if done and isinstance(data.get("fields"), dict):
                fields, _ = _sanitize(data["fields"], categories, brands, employments, regions)
            return {"ok": True, "reply": reply or "Хорошо.", "done": done, "fields": fields}
        try:
            detail = (response_payload.get("error", {}) or {}).get("message", "")
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
