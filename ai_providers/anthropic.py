"""Провайдер Anthropic (Claude) для WexFlow.

Endpoint:    POST https://api.anthropic.com/v1/messages
Авторизация: заголовок ``x-api-key`` + обязательный ``anthropic-version``
Модель:      claude-sonnet-5 (резерв — claude-haiku-4-5-20251001)

Три отличия от Groq/Gemini, из-за которых это отдельный класс, а не копия:
  1. ``system`` — **отдельное поле запроса**, а не сообщение в списке;
  2. ``max_tokens`` обязателен: без него запрос не примут;
  3. режима «строго JSON» у API нет. Просим строгий JSON инструкцией и
     разбираем ответ тем же снисходительным разбором, что и у Groq —
     ```-заборы и текст вокруг объекта не должны ломать ответ.

**Про деньги честно.** У Claude нет бесплатной дневной квоты: человек платит
за токены своим ключом. Поэтому здесь НЕ рисуется «процент дневного лимита» —
показываем минутный запас из заголовков аккаунта и локальную оценку токенов.
Gemini и Groq остаются бесплатным резервом (см. ai_gateway._FALLBACK_ON).
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

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

DEFAULT_MODEL = "claude-sonnet-5"
FALLBACK_MODEL = "claude-haiku-4-5-20251001"

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)
_JSON_RULE = "Отвечай СТРОГО одним JSON-объектом, без пояснений и без markdown."


def _accepts_temperature(model: str) -> bool:
    """Whether the model accepts non-default sampling parameters.

    Sonnet 5 returns HTTP 400 when ``temperature``/``top_p``/``top_k`` are set
    to a non-default value.  Omitting the field is the forward-compatible API
    shape recommended by Anthropic; Haiku 4.5 still accepts it.
    """
    return not str(model or "").casefold().startswith("claude-sonnet-5")


class AnthropicProvider(base.BaseProvider):
    provider_name = "anthropic"

    def __init__(self, account_id: str, *, model: str | None = None,
                 fallback_model: str | None = None):
        super().__init__(account_id, model=model)
        self._fallback = (fallback_model or FALLBACK_MODEL).strip() or FALLBACK_MODEL
        self._key_override: str | None = None

    @classmethod
    def with_key(cls, key: str, account_id: str = "probe", **kw) -> "AnthropicProvider":
        provider = cls(account_id, **kw)
        provider._key_override = (key or "").strip()
        return provider

    # -- статус -------------------------------------------------------------- #
    def _key(self) -> str:
        if self._key_override is not None:
            return self._key_override
        return ai_secrets.get_api_key("anthropic", self.account_id)

    def available(self) -> bool:
        return bool(self._key())

    @property
    def model_name(self) -> str:
        return self._model_override or DEFAULT_MODEL

    def _models_to_try(self, model: str | None = None) -> list[str]:
        out: list[str] = []
        for name in (model or self.model_name, self._fallback):
            if name and name not in out:
                out.append(name)
        return out

    def _headers(self, key: str) -> dict:
        return {
            "x-api-key": key,                 # ключ только в заголовке, никогда в URL
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }

    # -- нормализация ошибок ------------------------------------------------- #
    def normalize_error(self, status_code, payload, retry_after=None):
        code = int(status_code or 0)
        try:
            err = (payload or {}).get("error") or {}
            msg = str(err.get("message") or "")
            etype = str(err.get("type") or "")
        except Exception:  # noqa: BLE001
            msg, etype = "", ""
        low = (msg + " " + etype).casefold()

        if code == 401 or "authentication_error" in low:
            return base.INVALID_KEY, "Ключ Claude не принят.", retry_after
        if code == 402 or "billing_error" in low:
            return base.BILLING_ERROR, "У Claude нет доступного баланса или способа оплаты.", retry_after
        if code == 403 or "permission_error" in low:
            return base.PERMISSION_DENIED, "У ключа нет доступа к этой модели.", retry_after
        if code == 404 or "not_found_error" in low:
            return base.MODEL_NOT_FOUND, "Модель Claude недоступна.", retry_after
        if code == 429 or "rate_limit_error" in low:
            # У Anthropic лимиты минутные. Дневного лимита запросов нет, поэтому
            # RATE_LIMIT_RPD не ставим: иначе шлюз решит, что «на сегодня всё».
            if "token" in low:
                return base.RATE_LIMIT_TPM, "Минутный лимит токенов Claude исчерпан.", retry_after
            return base.RATE_LIMIT_RPM, "Минутный лимит запросов Claude исчерпан.", retry_after
        if code == 400 or "invalid_request_error" in low:
            return base.INVALID_REQUEST, "Некорректный запрос к Claude.", retry_after
        if code in (500, 502, 503, 504, 529) or "overloaded" in low or "api_error" in low:
            return base.PROVIDER_UNAVAILABLE, "Claude временно недоступен.", retry_after
        if code == 200:
            return base.OK, "", None
        return base.UNKNOWN, f"Ошибка Claude {code}.", retry_after

    def extract_usage(self, payload):
        usage = (payload or {}).get("usage") or {}
        try:
            prompt = max(0, int(usage.get("input_tokens") or 0))
            output = max(0, int(usage.get("output_tokens") or 0))
        except (TypeError, ValueError):
            return {}
        if not (prompt or output):
            return {}
        return {"prompt_tokens": prompt, "output_tokens": output,
                "total_tokens": prompt + output}

    def extract_rate_limits(self, headers) -> dict:
        """Минутные окна аккаунта. Дневных лимитов у Anthropic нет."""
        if headers is None:
            return {}
        get = self._header_getter(headers)
        out: dict = {}
        _put_num(out, "requests_limit", get("anthropic-ratelimit-requests-limit"))
        _put_num(out, "requests_remaining", get("anthropic-ratelimit-requests-remaining"))
        _put_str(out, "requests_reset", get("anthropic-ratelimit-requests-reset"))
        _put_num(out, "tokens_limit", get("anthropic-ratelimit-tokens-limit"))
        _put_num(out, "tokens_remaining", get("anthropic-ratelimit-tokens-remaining"))
        _put_str(out, "tokens_reset", get("anthropic-ratelimit-tokens-reset"))
        # Лимиты минутные — помечаем прямо в данных, чтобы интерфейс не выдал
        # их за «остаток на день».
        if out:
            out["window"] = "minute"
        retry = get("retry-after")
        if retry:
            _put_num(out, "retry_after", retry)
        return out

    @staticmethod
    def _header_getter(headers):
        if hasattr(headers, "get") and not isinstance(headers, dict):
            return lambda name: headers.get(name)
        lower = {str(k).lower(): v for k, v in dict(headers or {}).items()}
        return lambda name: lower.get(name.lower())

    # -- запросы ------------------------------------------------------------- #
    def validate_key(self, *, use_generation: bool = False) -> AIResult:
        """Проверка ключа. У Anthropic нет бесплатного списка моделей, поэтому
        проверяем самым дешёвым из возможных запросов — один токен ответа.
        Об этом честно сказано в интерфейсе мастера подключения."""
        key = self._key()
        rid = uuid.uuid4().hex[:12]
        if not key:
            return AIResult(ok=False, provider="anthropic", error_code=base.NOT_CONNECTED,
                            error_message="Для этого аккаунта Claude ещё не подключён.",
                            request_id=rid)
        res = self._request([{"role": "user", "content": "ok"}], model=self.model_name,
                            system=None, temperature=0.0, max_tokens=1, timeout=20.0)
        if res.ok:
            return AIResult(ok=True, provider="anthropic", model=res.model, status_code=200,
                            error_code=base.OK, request_id=res.request_id or rid,
                            usage=res.usage, rate_limits=res.rate_limits,
                            data={"has_primary": True})
        return res

    def _request(self, messages: list[dict], *, model: str, system, temperature: float,
                 max_tokens: int, timeout: float) -> AIResult:
        key = self._key()
        rid = uuid.uuid4().hex[:12]
        if not key:
            return AIResult(ok=False, provider="anthropic", model=model,
                            error_code=base.NOT_CONNECTED,
                            error_message="Для этого аккаунта Claude ещё не подключён.",
                            request_id=rid)
        body: dict = {
            "model": model,
            "messages": messages,
            "max_tokens": max(1, int(max_tokens)),   # обязательное поле
        }
        if _accepts_temperature(model):
            body["temperature"] = max(0.0, min(float(temperature), 1.0))
        if system:
            body["system"] = str(system)[:8000]      # отдельным полем, не сообщением
        try:
            r = httpx.post(API_URL, headers=self._headers(key), json=body, timeout=timeout)
        except httpx.TimeoutException:
            return AIResult(ok=False, provider="anthropic", model=model,
                            error_code=base.PROVIDER_TIMEOUT,
                            error_message="Claude не ответил вовремя.", request_id=rid)
        except Exception:  # noqa: BLE001 — сеть: без утечки деталей наружу
            return AIResult(ok=False, provider="anthropic", model=model,
                            error_code=base.OFFLINE,
                            error_message="Не удалось связаться с Claude.", request_id=rid)

        payload = _safe_json(r)
        headers = getattr(r, "headers", None)
        rate_limits = self.extract_rate_limits(headers)
        usage = self.extract_usage(payload)
        retry = _retry_after(headers)
        ecode, emsg, retry = self.normalize_error(r.status_code, payload, retry)
        _record(self, model, r.status_code, usage, rate_limits, retry, ecode)

        if r.status_code == 200:
            return AIResult(ok=True, provider="anthropic", model=model, status_code=200,
                            error_code=base.OK, reply=_content(payload), usage=usage,
                            rate_limits=rate_limits,
                            request_id=str(payload.get("id") or "") or rid)
        return AIResult(ok=False, provider="anthropic", model=model,
                        status_code=r.status_code, error_code=ecode, error_message=emsg,
                        retry_after=retry, usage=usage, rate_limits=rate_limits,
                        request_id=rid)

    def _run(self, messages, *, system=None, temperature, max_tokens, timeout) -> AIResult:
        last = AIResult(ok=False, provider="anthropic", error_code=base.UNKNOWN,
                        error_message="Не удалось получить ответ Claude.")
        for model in self._models_to_try():
            res = self._request(messages, model=model, system=system,
                                temperature=temperature, max_tokens=max_tokens,
                                timeout=timeout)
            if res.ok:
                return res
            last = res
            # Резервная модель — только когда основная недоступна. Лимиты
            # моделью не обходятся: это был бы обход воли провайдера.
            if res.error_code not in (base.MODEL_NOT_FOUND, base.PROVIDER_UNAVAILABLE):
                break
        return last

    def generate_json(self, prompt, *, schema=None, temperature=0.1,
                      max_tokens=1024, timeout=40.0) -> AIResult:
        res = self._run([{"role": "user", "content": str(prompt or "")[:50000]}],
                        system=_JSON_RULE, temperature=temperature,
                        max_tokens=max_tokens, timeout=timeout)
        if res.ok:
            data = _parse_json(res.reply)
            if isinstance(data, dict):
                res.data = data
            else:
                res.ok = False
                res.error_code = base.PARSE_ERROR
                res.error_message = "Claude вернул не-JSON."
        return res

    def generate_text(self, prompt, *, temperature=0.2, max_tokens=1024,
                      timeout=40.0) -> AIResult:
        return self._run([{"role": "user", "content": str(prompt or "")[:50000]}],
                         temperature=temperature, max_tokens=max_tokens, timeout=timeout)

    def chat(self, messages, *, system=None, json_mode=False, temperature=0.3,
             max_tokens=1024, timeout=40.0) -> AIResult:
        msgs: list[dict] = []
        for item in (messages or [])[-16:]:
            role = "assistant" if item.get("role") in ("assistant", "model") else "user"
            text = str(item.get("text") or item.get("content") or "").strip()[:4000]
            if text:
                msgs.append({"role": role, "content": text})
        if not msgs:
            msgs = [{"role": "user", "content": "."}]
        prompt_system = system
        if json_mode:
            prompt_system = f"{system}\n{_JSON_RULE}" if system else _JSON_RULE
        res = self._run(msgs, system=prompt_system, temperature=temperature,
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
    """Ответ Claude — список блоков; берём текстовые и склеиваем."""
    try:
        blocks = payload.get("content") or []
        parts = [str(b.get("text") or "") for b in blocks
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "".join(parts)
    except Exception:  # noqa: BLE001
        return ""


def _parse_json(text: str):
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        pass
    match = _JSON_FENCE.search(text)
    if match:
        try:
            return json.loads(match.group(1))
        except Exception:  # noqa: BLE001
            pass
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
        value = headers.get("retry-after") if hasattr(headers, "get") else None
        if value is None:
            lower = {str(k).lower(): v for k, v in dict(headers or {}).items()}
            value = lower.get("retry-after")
        return float(value) if value is not None else None
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
    text = str(value).strip()
    if text:
        out[key] = text


def _record(provider: "AnthropicProvider", model, code, usage, rate_limits,
            retry, ecode) -> None:
    try:
        ai_usage.record_provider(
            "anthropic", model, code,
            account_id=provider.account_id,
            fingerprint=ai_secrets.fingerprint("anthropic", provider.account_id),
            usage=usage, rate_limits=rate_limits, retry_after=retry, error_code=ecode,
        )
    except Exception:  # noqa: BLE001 — учёт не должен ронять запрос
        pass
