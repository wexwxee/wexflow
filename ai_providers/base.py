"""Базовый интерфейс провайдера ИИ и единый безопасный результат.

Все провайдеры (Gemini, Groq, …) возвращают ``AIResult`` одинаковой формы. В нём
НИКОГДА нет ключа, заголовка Authorization, полного URL с секретом, исходного
HTTP-запроса или потенциально чувствительного traceback — только нормализованный
статус, данные ответа и локально посчитанные лимиты/использование.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# --- Нормализованные коды ошибок (единые для всех провайдеров) --------------- #
OK = "ok"
NOT_CONNECTED = "not_connected"
INVALID_KEY = "invalid_key"
PERMISSION_DENIED = "permission_denied"
RATE_LIMIT_RPM = "rate_limit_rpm"
RATE_LIMIT_RPD = "rate_limit_rpd"
RATE_LIMIT_TPM = "rate_limit_tpm"
RATE_LIMIT_TPD = "rate_limit_tpd"
PROVIDER_TIMEOUT = "provider_timeout"
PROVIDER_UNAVAILABLE = "provider_unavailable"
MODEL_NOT_FOUND = "model_not_found"
INVALID_REQUEST = "invalid_request"
SAFETY_REFUSAL = "safety_refusal"
OFFLINE = "offline"
PARSE_ERROR = "parse_error"
UNKNOWN = "unknown_error"

# Ошибки, при которых допустим повтор/переключение на резерв (временные).
TRANSIENT = frozenset({PROVIDER_TIMEOUT, PROVIDER_UNAVAILABLE, OFFLINE})
# Дневные лимиты — повод для fallback на другого провайдера, но НЕ для повтора.
DAILY_LIMITS = frozenset({RATE_LIMIT_RPD, RATE_LIMIT_TPD})


@dataclass
class AIResult:
    ok: bool = False
    data: dict | None = None
    reply: str = ""
    provider: str = ""
    model: str = ""
    status_code: int = 0
    error_code: str = ""
    error_message: str = ""
    retry_after: float | None = None
    usage: dict = field(default_factory=dict)
    rate_limits: dict = field(default_factory=dict)
    request_id: str = ""

    def to_dict(self) -> dict:
        """Безопасный словарь для API/логов (без секретов)."""
        return {
            "ok": self.ok,
            "data": self.data,
            "reply": self.reply,
            "provider": self.provider,
            "model": self.model,
            "status_code": self.status_code,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "retry_after": self.retry_after,
            "usage": self.usage or {},
            "rate_limits": self.rate_limits or {},
            "request_id": self.request_id,
        }

    @property
    def transient(self) -> bool:
        return self.error_code in TRANSIENT

    @property
    def daily_limited(self) -> bool:
        return self.error_code in DAILY_LIMITS


class BaseProvider:
    """Контракт провайдера. Все методы обязаны возвращать ``AIResult`` (кроме
    ``available``/``validate_key`` где указано) и никогда не поднимать наружу
    исключение с секретом."""

    provider_name = "base"

    def __init__(self, account_id: str, *, model: str | None = None):
        self.account_id = account_id
        self._model_override = (model or "").strip() or None

    # -- статус -------------------------------------------------------------- #
    def available(self) -> bool:
        raise NotImplementedError

    @property
    def model_name(self) -> str:
        raise NotImplementedError

    # -- запросы ------------------------------------------------------------- #
    def validate_key(self, *, use_generation: bool = False) -> AIResult:
        raise NotImplementedError

    def generate_json(self, prompt: str, *, schema: dict | None = None,
                      temperature: float = 0.1, max_tokens: int = 1024,
                      timeout: float = 40.0) -> AIResult:
        raise NotImplementedError

    def generate_text(self, prompt: str, *, temperature: float = 0.2,
                     max_tokens: int = 1024, timeout: float = 40.0) -> AIResult:
        raise NotImplementedError

    def chat(self, messages: list[dict], *, system: str | None = None,
            json_mode: bool = False, temperature: float = 0.3,
            max_tokens: int = 1024, timeout: float = 40.0) -> AIResult:
        raise NotImplementedError

    # -- разбор ответа (переопределяется провайдером) ------------------------ #
    def normalize_error(self, status_code: int, payload: dict | None,
                       retry_after: float | None = None) -> tuple[str, str, float | None]:
        raise NotImplementedError

    def extract_usage(self, payload: dict | None) -> dict:
        return {}

    def extract_rate_limits(self, headers) -> dict:
        return {}
