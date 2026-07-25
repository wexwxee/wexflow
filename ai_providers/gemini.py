"""Провайдер Google Gemini для общего gateway.

Использует тот же бесплатный endpoint AI Studio, что и ``ai_filters`` (фасад для
настройки фильтров остаётся Gemini-родным ради обратной совместимости). Здесь —
универсальные ``generate_json``/``generate_text``/``chat`` для gateway. Ключ
берётся строго для конкретного аккаунта через ``ai_secrets``.
"""
from __future__ import annotations

import json
import uuid

import httpx

import ai_secrets
import ai_usage
from ai_providers import base
from ai_providers.base import AIResult

DEFAULT_MODEL = "gemini-2.5-flash"
FALLBACK_MODELS = ("gemini-2.5-flash", "gemini-2.5-flash-lite")
BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


class GeminiProvider(base.BaseProvider):
    provider_name = "gemini"

    def __init__(self, account_id: str, *, model: str | None = None):
        super().__init__(account_id, model=model)
        self._key_override: str | None = None

    @classmethod
    def with_key(cls, key: str, account_id: str = "probe", **kw) -> "GeminiProvider":
        p = cls(account_id, **kw)
        p._key_override = (key or "").strip()
        return p

    def _key(self) -> str:
        if self._key_override is not None:
            return self._key_override
        return ai_secrets.get_api_key("gemini", self.account_id)

    def available(self) -> bool:
        return bool(self._key())

    @property
    def model_name(self) -> str:
        if self._model_override:
            return self._model_override
        try:
            import ai_filters
            return ai_filters.model_name()
        except Exception:  # noqa: BLE001
            return DEFAULT_MODEL

    def _models_to_try(self) -> list[str]:
        out: list[str] = []
        for m in (self.model_name, *FALLBACK_MODELS):
            if m and m not in out:
                out.append(m)
        return out

    # -- нормализация -------------------------------------------------------- #
    def normalize_error(self, status_code, payload, retry_after=None):
        code = int(status_code or 0)
        try:
            detail = str(((payload or {}).get("error") or {}).get("message") or "")
        except Exception:  # noqa: BLE001
            detail = ""
        low = detail.casefold()
        if code in (400,) and "api key" in low:
            return base.INVALID_KEY, "Ключ Gemini не принят.", retry_after
        if code in (401, 403):
            return base.PERMISSION_DENIED, "Нет доступа к модели Gemini.", retry_after
        if code == 404:
            return base.MODEL_NOT_FOUND, "Модель Gemini недоступна.", retry_after
        if code == 429:
            daily = any(m in low for m in ("per day", "perday", "requests per day", "rpd"))
            return (base.RATE_LIMIT_RPD if daily else base.RATE_LIMIT_RPM,
                    "Лимит Gemini исчерпан.", retry_after)
        if code in (500, 502, 503, 504):
            return base.PROVIDER_UNAVAILABLE, "Gemini временно недоступен.", retry_after
        if code == 400:
            return base.INVALID_REQUEST, "Некорректный запрос к Gemini.", retry_after
        if code == 200:
            return base.OK, "", None
        return base.UNKNOWN, f"Ошибка Gemini {code}.", retry_after

    def extract_usage(self, payload):
        u = (payload or {}).get("usageMetadata") or {}
        try:
            return {
                "prompt_tokens": max(0, int(u.get("promptTokenCount") or 0)),
                "output_tokens": max(0, int(u.get("candidatesTokenCount") or 0)),
                "total_tokens": max(0, int(u.get("totalTokenCount") or 0)),
            }
        except (TypeError, ValueError):
            return {}

    # -- проверка ключа ------------------------------------------------------ #
    def validate_key(self, *, use_generation: bool = False) -> AIResult:
        key = self._key()
        rid = uuid.uuid4().hex[:12]
        if not key:
            return AIResult(ok=False, provider="gemini", error_code=base.NOT_CONNECTED,
                            error_message="Для этого аккаунта Gemini не подключён.", request_id=rid)
        try:
            r = httpx.get(f"{BASE_URL}/models", headers={"x-goog-api-key": key}, timeout=15)
        except httpx.TimeoutException:
            return AIResult(ok=False, provider="gemini", error_code=base.PROVIDER_TIMEOUT,
                            error_message="Gemini не ответил вовремя.", request_id=rid)
        except Exception:  # noqa: BLE001
            return AIResult(ok=False, provider="gemini", error_code=base.OFFLINE,
                            error_message="Не удалось связаться с Gemini.", request_id=rid)
        payload = _safe_json(r)
        if r.status_code == 200:
            return AIResult(ok=True, provider="gemini", model=self.model_name,
                            status_code=200, error_code=base.OK, request_id=rid)
        ecode, emsg, _ = self.normalize_error(r.status_code, payload)
        return AIResult(ok=False, provider="gemini", model=self.model_name,
                        status_code=r.status_code, error_code=ecode, error_message=emsg,
                        request_id=rid)

    # -- запросы ------------------------------------------------------------- #
    def _request(self, body: dict, model: str, *, timeout: float) -> tuple[int, dict, str]:
        key = self._key()
        rid = uuid.uuid4().hex[:12]
        url = f"{BASE_URL}/models/{model}:generateContent"
        r = httpx.post(url, headers={"x-goog-api-key": key}, json=body, timeout=timeout)
        payload = _safe_json(r)
        usage = self.extract_usage(payload)
        # Легаси-счётчик (хаб-индикатор 1.3.21) + новый мультипровайдерный учёт.
        try:
            ai_usage.record_response(model, r.status_code, payload)
        except Exception:  # noqa: BLE001
            pass
        ecode, _, _ = self.normalize_error(r.status_code, payload)
        try:
            ai_usage.record_provider(
                "gemini", model, r.status_code,
                account_id=self.account_id,
                fingerprint=ai_secrets.fingerprint("gemini", self.account_id),
                usage=usage, error_code=ecode,
            )
        except Exception:  # noqa: BLE001
            pass
        return r.status_code, payload, rid

    def _run(self, body: dict, *, timeout: float, want: str) -> AIResult:
        key = self._key()
        if not key:
            return AIResult(ok=False, provider="gemini", error_code=base.NOT_CONNECTED,
                            error_message="Для этого аккаунта Gemini не подключён.")
        last = AIResult(ok=False, provider="gemini", error_code=base.UNKNOWN,
                        error_message="Не удалось получить ответ Gemini.")
        for model in self._models_to_try():
            try:
                code, payload, rid = self._request(body, model, timeout=timeout)
            except httpx.TimeoutException:
                last = AIResult(ok=False, provider="gemini", model=model,
                                error_code=base.PROVIDER_TIMEOUT,
                                error_message="Gemini не ответил вовремя.")
                continue
            except Exception:  # noqa: BLE001
                last = AIResult(ok=False, provider="gemini", model=model,
                                error_code=base.OFFLINE,
                                error_message="Не удалось связаться с Gemini.")
                continue
            usage = self.extract_usage(payload)
            if code == 200:
                text = _text(payload)
                res = AIResult(ok=True, provider="gemini", model=model, status_code=200,
                               error_code=base.OK, reply=text, usage=usage, request_id=rid)
                if want == "json":
                    data = _parse_json(text)
                    if isinstance(data, dict):
                        res.data = data
                    else:
                        res.ok = False
                        res.error_code = base.PARSE_ERROR
                        res.error_message = "Gemini вернул не-JSON."
                return res
            ecode, emsg, retry = self.normalize_error(code, payload)
            last = AIResult(ok=False, provider="gemini", model=model, status_code=code,
                            error_code=ecode, error_message=emsg, retry_after=retry,
                            usage=usage, request_id=rid)
            # Между Gemini-моделями перебираем только на 429/503 (как в 1.3.21).
            if code not in (429, 503):
                break
        return last

    def generate_json(self, prompt, *, schema=None, temperature=0.1,
                      max_tokens=1024, timeout=40.0) -> AIResult:
        body = {
            "contents": [{"parts": [{"text": str(prompt or "")[:50000]}]}],
            "generationConfig": {
                "temperature": max(0.0, min(float(temperature), 1.0)),
                "responseMimeType": "application/json",
                "maxOutputTokens": max(1, int(max_tokens)),
            },
        }
        return self._run(body, timeout=timeout, want="json")

    def generate_text(self, prompt, *, temperature=0.2, max_tokens=1024, timeout=40.0) -> AIResult:
        body = {
            "contents": [{"parts": [{"text": str(prompt or "")[:50000]}]}],
            "generationConfig": {
                "temperature": max(0.0, min(float(temperature), 1.0)),
                "maxOutputTokens": max(1, int(max_tokens)),
            },
        }
        return self._run(body, timeout=timeout, want="text")

    def chat(self, messages, *, system=None, json_mode=False, temperature=0.3,
            max_tokens=1024, timeout=40.0) -> AIResult:
        contents = []
        for m in (messages or [])[-16:]:
            role = "model" if m.get("role") in ("assistant", "model") else "user"
            text = str(m.get("text") or m.get("content") or "").strip()[:4000]
            if text:
                contents.append({"role": role, "parts": [{"text": text}]})
        gen: dict = {
            "temperature": max(0.0, min(float(temperature), 1.0)),
            "maxOutputTokens": max(1, int(max_tokens)),
        }
        if json_mode:
            gen["responseMimeType"] = "application/json"
        body: dict = {"contents": contents, "generationConfig": gen}
        if system:
            body["systemInstruction"] = {"parts": [{"text": str(system)[:8000]}]}
        return self._run(body, timeout=timeout, want=("json" if json_mode else "text"))


# --------------------------------------------------------------------------- #
def _safe_json(response) -> dict:
    try:
        payload = response.json()
        return payload if isinstance(payload, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _text(payload: dict) -> str:
    try:
        return str(payload["candidates"][0]["content"]["parts"][0]["text"] or "")
    except Exception:  # noqa: BLE001
        return ""


def _parse_json(text: str):
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(text[start:end + 1])
            except Exception:  # noqa: BLE001
                return None
    return None
