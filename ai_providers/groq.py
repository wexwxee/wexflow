"""Провайдер Groq (OpenAI-совместимый) для WexFlow.

Endpoint:   POST https://api.groq.com/openai/v1/chat/completions
Авторизация: Authorization: Bearer <личный ключ пользователя>
Основная модель:   qwen/qwen3.6-27b
Резервная модель:  openai/gpt-oss-120b  (технический fallback при снятии модели/404/сбое)

Особенности бесплатного тарифа учитываем: короткий max_completion_tokens, JSON
Object Mode (без лишних объяснений), разбор фактических заголовков лимитов
аккаунта. Модель не переключаем ради обхода лимитов — fallback только при
недоступности модели/временном серверном сбое.
"""
from __future__ import annotations

import json
import re
import uuid

import httpx

import ai_secrets
import ai_usage
from ai_providers import base
from ai_providers.base import AIResult

API_URL = "https://api.groq.com/openai/v1/chat/completions"
MODELS_URL = "https://api.groq.com/openai/v1/models"

DEFAULT_MODEL = "qwen/qwen3.6-27b"
FALLBACK_MODEL = "openai/gpt-oss-120b"

# Опубликованный лимит бесплатного тарифа — лишь НАЧАЛЬНОЕ описание. После первого
# ответа API приоритет у фактических заголовков аккаунта (см. extract_rate_limits).
PUBLISHED_RPD_HINT = 1000

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)


class GroqProvider(base.BaseProvider):
    provider_name = "groq"

    def __init__(self, account_id: str, *, model: str | None = None,
                 fallback_model: str | None = None):
        super().__init__(account_id, model=model)
        self._fallback = (fallback_model or FALLBACK_MODEL).strip() or FALLBACK_MODEL
        self._key_override: str | None = None

    @classmethod
    def with_key(cls, key: str, account_id: str = "probe", **kw) -> "GroqProvider":
        """Экземпляр для проверки ПЕРЕДАННОГО ключа (до сохранения)."""
        p = cls(account_id, **kw)
        p._key_override = (key or "").strip()
        return p

    # -- статус -------------------------------------------------------------- #
    def _key(self) -> str:
        if self._key_override is not None:
            return self._key_override
        return ai_secrets.get_api_key("groq", self.account_id)

    def available(self) -> bool:
        return bool(self._key())

    @property
    def model_name(self) -> str:
        return self._model_override or DEFAULT_MODEL

    def _models_to_try(self, model: str | None = None) -> list[str]:
        out: list[str] = []
        for m in (model or self.model_name, self._fallback):
            if m and m not in out:
                out.append(m)
        return out

    # -- нормализация ошибок ------------------------------------------------- #
    def normalize_error(self, status_code, payload, retry_after=None):
        code = int(status_code or 0)
        try:
            err = (payload or {}).get("error") or {}
            msg = str(err.get("message") or "")
            etype = str(err.get("type") or err.get("code") or "")
        except Exception:  # noqa: BLE001
            msg, etype = "", ""
        low = (msg + " " + etype).casefold()

        if code == 401:
            return base.INVALID_KEY, "Ключ не принят.", retry_after
        if code == 403:
            return base.PERMISSION_DENIED, "У аккаунта нет доступа к выбранной модели.", retry_after
        if code == 404 or "model_not_found" in low or "does not exist" in low or "decommission" in low:
            return base.MODEL_NOT_FOUND, "Модель недоступна.", retry_after
        if code == 429:
            # Различаем дневные запросы / минутные токены / RPM.
            is_token = "token" in low
            is_day = ("per day" in low or "requests per day" in low or "rpd" in low
                      or "tpd" in low or "tokens per day" in low)
            if is_token and is_day:
                return base.RATE_LIMIT_TPD, "Дневной лимит токенов исчерпан.", retry_after
            if is_token:
                return base.RATE_LIMIT_TPM, "Минутный лимит токенов исчерпан.", retry_after
            if is_day:
                return base.RATE_LIMIT_RPD, "Дневной лимит запросов исчерпан.", retry_after
            return base.RATE_LIMIT_RPM, "Минутный лимит запросов исчерпан.", retry_after
        if code in (500, 502, 503, 504) or "unavailable" in low or "overloaded" in low:
            return base.PROVIDER_UNAVAILABLE, "Groq временно недоступен.", retry_after
        if code == 400:
            return base.INVALID_REQUEST, "Некорректный запрос к модели.", retry_after
        if code == 200:
            return base.OK, "", None
        return base.UNKNOWN, f"Ошибка Groq {code}.", retry_after

    def extract_usage(self, payload):
        u = (payload or {}).get("usage") or {}
        try:
            return {
                "prompt_tokens": max(0, int(u.get("prompt_tokens") or 0)),
                "output_tokens": max(0, int(u.get("completion_tokens") or 0)),
                "total_tokens": max(0, int(u.get("total_tokens") or 0)),
            }
        except (TypeError, ValueError):
            return {}

    def extract_rate_limits(self, headers) -> dict:
        """Разобрать заголовки лимитов (case-insensitive).

        requests_* — ДНЕВНЫЕ запросы; tokens_* — текущее МИНУТНОЕ TPM-окно.
        Явно не смешиваем их в UI.
        """
        if headers is None:
            return {}
        get = self._header_getter(headers)
        out: dict = {}
        _put_num(out, "requests_limit", get("x-ratelimit-limit-requests"))
        _put_num(out, "requests_remaining", get("x-ratelimit-remaining-requests"))
        _put_str(out, "requests_reset", get("x-ratelimit-reset-requests"))
        _put_num(out, "tokens_limit", get("x-ratelimit-limit-tokens"))
        _put_num(out, "tokens_remaining", get("x-ratelimit-remaining-tokens"))
        _put_str(out, "tokens_reset", get("x-ratelimit-reset-tokens"))
        retry = get("retry-after")
        if retry:
            _put_num(out, "retry_after", retry)
        return out

    @staticmethod
    def _header_getter(headers):
        # httpx.Headers уже case-insensitive; dict — приведём вручную.
        if hasattr(headers, "get") and not isinstance(headers, dict):
            return lambda name: headers.get(name)
        lower = {str(k).lower(): v for k, v in dict(headers or {}).items()}
        return lambda name: lower.get(name.lower())

    # -- запросы ------------------------------------------------------------- #
    def validate_key(self, *, use_generation: bool = False) -> AIResult:
        """Безопасная проверка авторизации через список моделей (без генерации).

        При use_generation=True дополнительно делает один минимальный запрос —
        вызывающий предупреждает пользователя, что это тратит один запрос.
        """
        key = self._key()
        if not key:
            return AIResult(ok=False, provider="groq", error_code=base.NOT_CONNECTED,
                            error_message="Для этого аккаунта Groq ещё не подключён.")
        rid = uuid.uuid4().hex[:12]
        try:
            r = httpx.get(MODELS_URL, headers={"Authorization": f"Bearer {key}"}, timeout=15)
        except httpx.TimeoutException:
            return AIResult(ok=False, provider="groq", error_code=base.PROVIDER_TIMEOUT,
                            error_message="Не удалось связаться с Groq (таймаут).", request_id=rid)
        except Exception:  # noqa: BLE001 — любые сетевые = офлайн, без утечки деталей
            return AIResult(ok=False, provider="groq", error_code=base.OFFLINE,
                            error_message="Не удалось связаться с Groq.", request_id=rid)
        payload = _safe_json(r)
        if r.status_code == 200:
            models = {str(m.get("id")) for m in (payload.get("data") or []) if isinstance(m, dict)}
            has_primary = self.model_name in models if models else True
            result = AIResult(
                ok=True, provider="groq", model=self.model_name, status_code=200,
                error_code=base.OK, request_id=rid,
                rate_limits=self.extract_rate_limits(getattr(r, "headers", None)),
                data={"models_available": sorted(models)[:50], "has_primary": has_primary},
            )
            if use_generation:
                gen = self.generate_json('Ответь строго JSON: {"ok": true}', max_tokens=16)
                result.usage = gen.usage
                result.model = gen.model or result.model
                if not gen.ok and gen.error_code == base.MODEL_NOT_FOUND:
                    result.data["has_primary"] = False
            return result
        ecode, emsg, retry = self.normalize_error(r.status_code, payload,
                                                   _retry_after(getattr(r, "headers", None)))
        return AIResult(ok=False, provider="groq", model=self.model_name,
                        status_code=r.status_code, error_code=ecode, error_message=emsg,
                        retry_after=retry, request_id=rid,
                        rate_limits=self.extract_rate_limits(getattr(r, "headers", None)))

    def _request(self, messages: list[dict], *, model: str, json_mode: bool,
                 temperature: float, max_tokens: int, timeout: float) -> AIResult:
        key = self._key()
        rid = uuid.uuid4().hex[:12]
        if not key:
            return AIResult(ok=False, provider="groq", model=model,
                            error_code=base.NOT_CONNECTED,
                            error_message="Для этого аккаунта Groq ещё не подключён.",
                            request_id=rid)
        body: dict = {
            "model": model,
            "messages": messages,
            "temperature": max(0.0, min(float(temperature), 1.0)),
            "max_completion_tokens": max(1, int(max_tokens)),
            "stream": False,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        try:
            r = httpx.post(API_URL, headers={"Authorization": f"Bearer {key}"},
                           json=body, timeout=timeout)
        except httpx.TimeoutException:
            return AIResult(ok=False, provider="groq", model=model,
                            error_code=base.PROVIDER_TIMEOUT,
                            error_message="Groq не ответил вовремя.", request_id=rid)
        except Exception:  # noqa: BLE001
            return AIResult(ok=False, provider="groq", model=model,
                            error_code=base.OFFLINE,
                            error_message="Не удалось связаться с Groq.", request_id=rid)

        payload = _safe_json(r)
        headers = getattr(r, "headers", None)
        rate_limits = self.extract_rate_limits(headers)
        usage = self.extract_usage(payload)
        retry = _retry_after(headers)
        ecode, emsg, retry = self.normalize_error(r.status_code, payload, retry)
        # Каждый реально отправленный запрос учитываем в статистике (в т.ч. ошибки).
        _record(self, model, r.status_code, usage, rate_limits, retry, ecode)

        if r.status_code == 200:
            text = _content(payload)
            return AIResult(ok=True, provider="groq", model=model, status_code=200,
                            error_code=base.OK, reply=text, usage=usage,
                            rate_limits=rate_limits,
                            request_id=_provider_request_id(payload, rid))
        return AIResult(ok=False, provider="groq", model=model, status_code=r.status_code,
                        error_code=ecode, error_message=emsg, retry_after=retry,
                        usage=usage, rate_limits=rate_limits, request_id=rid)

    def _run(self, messages, *, json_mode, temperature, max_tokens, timeout,
             allow_fallback=True) -> AIResult:
        last = AIResult(ok=False, provider="groq", error_code=base.UNKNOWN,
                        error_message="Не удалось получить ответ Groq.")
        for model in self._models_to_try():
            res = self._request(messages, model=model, json_mode=json_mode,
                                 temperature=temperature, max_tokens=max_tokens,
                                 timeout=timeout)
            if res.ok:
                return res
            last = res
            # Fallback qwen -> gpt-oss ТОЛЬКО при недоступности модели/серверном
            # сбое. Лимиты и неправильный запрос — не повод менять модель.
            if not allow_fallback:
                break
            if res.error_code not in (base.MODEL_NOT_FOUND, base.PROVIDER_UNAVAILABLE):
                break
        return last

    def generate_json(self, prompt, *, schema=None, temperature=0.1,
                      max_tokens=1024, timeout=40.0) -> AIResult:
        messages = [
            {"role": "system", "content": "Отвечай СТРОГО одним JSON-объектом без пояснений."},
            {"role": "user", "content": str(prompt or "")[:50000]},
        ]
        res = self._run(messages, json_mode=True, temperature=temperature,
                        max_tokens=max_tokens, timeout=timeout)
        if res.ok:
            data = _parse_json(res.reply)
            if isinstance(data, dict):
                res.data = data
            else:
                res.ok = False
                res.error_code = base.PARSE_ERROR
                res.error_message = "Groq вернул не-JSON."
        return res

    def generate_text(self, prompt, *, temperature=0.2, max_tokens=1024, timeout=40.0) -> AIResult:
        messages = [{"role": "user", "content": str(prompt or "")[:50000]}]
        return self._run(messages, json_mode=False, temperature=temperature,
                         max_tokens=max_tokens, timeout=timeout)

    def chat(self, messages, *, system=None, json_mode=False, temperature=0.3,
            max_tokens=1024, timeout=40.0) -> AIResult:
        msgs: list[dict] = []
        if system:
            msgs.append({"role": "system", "content": str(system)[:8000]})
        for m in (messages or [])[-16:]:
            role = "assistant" if m.get("role") in ("assistant", "model") else "user"
            text = str(m.get("text") or m.get("content") or "").strip()[:4000]
            if text:
                msgs.append({"role": role, "content": text})
        res = self._run(msgs, json_mode=json_mode, temperature=temperature,
                        max_tokens=max_tokens, timeout=timeout)
        if res.ok and json_mode:
            data = _parse_json(res.reply)
            if isinstance(data, dict):
                res.data = data
        return res


# --------------------------------------------------------------------------- #
#  helpers
# --------------------------------------------------------------------------- #
def _safe_json(response) -> dict:
    try:
        payload = response.json()
        return payload if isinstance(payload, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _content(payload: dict) -> str:
    try:
        return str(payload["choices"][0]["message"]["content"] or "")
    except Exception:  # noqa: BLE001
        return ""


def _provider_request_id(payload: dict, fallback: str) -> str:
    rid = str((payload or {}).get("id") or "").strip()
    return rid or fallback


def _parse_json(text: str):
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        pass
    m = _JSON_FENCE.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:  # noqa: BLE001
            pass
    # последний шанс — вырезать первый {...}
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(text[start:end + 1])
        except Exception:  # noqa: BLE001
            return None
    return None


def _retry_after(headers):
    if headers is None:
        return None
    try:
        val = headers.get("retry-after") if hasattr(headers, "get") else None
        if val is None:
            lower = {str(k).lower(): v for k, v in dict(headers or {}).items()}
            val = lower.get("retry-after")
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _put_num(out: dict, key: str, value) -> None:
    if value is None:
        return
    try:
        out[key] = float(value)
    except (TypeError, ValueError):
        pass


def _put_str(out: dict, key: str, value) -> None:
    if value is None:
        return
    s = str(value).strip()
    if s:
        out[key] = s


def _record(provider: "GroqProvider", model, code, usage, rate_limits, retry, ecode) -> None:
    try:
        ai_usage.record_provider(
            "groq", model, code,
            account_id=provider.account_id,
            fingerprint=ai_secrets.fingerprint("groq", provider.account_id),
            usage=usage, rate_limits=rate_limits, retry_after=retry, error_code=ecode,
        )
    except Exception:  # noqa: BLE001 — учёт статистики не должен ронять запрос
        pass
