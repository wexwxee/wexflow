"""Единый шлюз ИИ WexFlow: маршрутизация между провайдерами и один безопасный
результат для всего приложения.

Не размножаем проверки Gemini/Groq по коду — весь выбор провайдера здесь.

Режимы (определяются ЛОКАЛЬНО подключёнными ключами текущего аккаунта):
  * Gemini + Groq (владелец): основной — Gemini, резерв — Groq (только с
    согласием и только при дневном 429 Gemini / таймауте / временном 5xx).
  * Только Gemini: поведение версии 1.3.21.
  * Только Groq: основная Qwen, технический резерв GPT-OSS (внутри провайдера).
  * Нет ключей: обычные функции работают, ИИ-функции показывают мастер
    подключения (error_code=not_connected), а не HTTP 500.
"""
from __future__ import annotations

import time

import ai_secrets
import ai_usage
from ai_providers import base
from ai_providers.anthropic import AnthropicProvider
from ai_providers.base import AIResult
from ai_providers.gemini import GeminiProvider
from ai_providers.groq import GroqProvider

_BACKOFF_BASE = 0.5
_BACKOFF_CAP = 4.0

# Ошибки основного провайдера, при которых разрешён переход на резерв. Минутный
# лимит (RPM/TPM), неправильный запрос, отказ по безопасности — НЕ повод.
_FALLBACK_ON = frozenset({
    base.RATE_LIMIT_RPD, base.RATE_LIMIT_TPD,
    base.PROVIDER_TIMEOUT, base.PROVIDER_UNAVAILABLE, base.OFFLINE,
})


def _account(account_id: str | None) -> str:
    return (account_id or "").strip() or ai_secrets.current_account_id()


def _consent_ok(name: str, account_id: str) -> bool:
    if name != "groq":
        return True
    # Groq (основной или резервный) используем только после согласия пользователя.
    return bool(ai_secrets.info("groq", account_id).get("consent"))


# Реестр провайдеров и порядок выбора. Подключённый платный идёт первым: его
# ключ человек завёл сам и ради качества. Бесплатные Gemini и Groq остаются
# резервом, и переход на них разрешён только по _FALLBACK_ON (дневной лимит,
# таймаут, недоступность), а не «когда захочется».
# Держим ИМЕНА классов, а не сами классы: класс ищется в момент вызова. Так
# подмена ai_gateway.GeminiProvider (тесты, отладка) продолжает работать —
# замороженная в словаре ссылка её бы игнорировала.
_REGISTRY: dict = {
    "anthropic": "AnthropicProvider",
    "gemini": "GeminiProvider",
    "groq": "GroqProvider",
}
ORDER = ("anthropic", "gemini", "groq")


def _provider_class(name: str):
    return globals().get(_REGISTRY.get(str(name or ""), ""))


def _usable_providers(account_id: str) -> list[tuple[str, base.BaseProvider]]:
    """Пригодные провайдеры в порядке приоритета."""
    out: list[tuple[str, base.BaseProvider]] = []
    for name in ORDER:
        factory = _provider_class(name)
        if factory is None:
            continue
        provider = factory(account_id)
        if provider.available() and _consent_ok(name, account_id):
            out.append((name, provider))
    return out


def available(account_id: str | None = None) -> bool:
    return bool(_usable_providers(_account(account_id)))


def active_provider(account_id: str | None = None) -> str:
    order = _usable_providers(_account(account_id))
    return order[0][0] if order else ""


def _not_connected() -> AIResult:
    return AIResult(
        ok=False, provider="", error_code=base.NOT_CONNECTED,
        error_message="ИИ ещё не подключён для этого аккаунта.",
    )


def _with_retry(call, prov, *, retries: int) -> AIResult:
    attempt = 0
    while True:
        res = call(prov)
        if res.ok or res.error_code not in base.TRANSIENT or attempt >= retries:
            return res
        delay = res.retry_after if res.retry_after else _BACKOFF_BASE * (2 ** attempt)
        delay = min(float(delay or 0), _BACKOFF_CAP)
        if delay > 0:
            time.sleep(delay)
        attempt += 1


def _dispatch(account_id: str | None, call, *, retries: int) -> AIResult:
    # Фоновая задача обязана выполняться в том же аккаунте, в котором создана.
    if not ai_secrets.context_valid():
        return AIResult(
            ok=False, provider="", error_code=base.NOT_CONNECTED,
            error_message="Аккаунт сменился — задача остановлена без обращения к ИИ.",
        )
    account_id = _account(account_id)
    order = _usable_providers(account_id)
    if not order:
        return _not_connected()
    primary_name, primary = order[0]
    res = _with_retry(call, primary, retries=retries)
    if res.ok:
        return res
    # Резерв — только для владельца (Gemini основной, Groq резерв) и только при
    # разрешённых причинах. Для «только Groq» резерв модели уже внутри провайдера.
    if len(order) > 1 and res.error_code in _FALLBACK_ON:
        fb_name, fb = order[1]
        res2 = _with_retry(call, fb, retries=retries)
        if res2.ok:
            res2.request_id = res2.request_id or ""
            res2.usage.setdefault("fell_back_from", primary_name)
            return res2
        # Возвращаем более информативную ошибку основного провайдера.
        return res
    return res


# --------------------------------------------------------------------------- #
#  Публичные операции
# --------------------------------------------------------------------------- #
def generate_json(prompt: str, *, account_id: str | None = None, schema: dict | None = None,
                  temperature: float = 0.1, max_tokens: int = 1024, timeout: float = 40.0,
                  retries: int = 1) -> AIResult:
    return _dispatch(
        account_id,
        lambda p: p.generate_json(prompt, schema=schema, temperature=temperature,
                                  max_tokens=max_tokens, timeout=timeout),
        retries=retries,
    )


def generate_text(prompt: str, *, account_id: str | None = None, temperature: float = 0.2,
                 max_tokens: int = 1024, timeout: float = 40.0, retries: int = 1) -> AIResult:
    return _dispatch(
        account_id,
        lambda p: p.generate_text(prompt, temperature=temperature, max_tokens=max_tokens,
                                  timeout=timeout),
        retries=retries,
    )


def chat(messages: list[dict], *, account_id: str | None = None, system: str | None = None,
        json_mode: bool = False, temperature: float = 0.3, max_tokens: int = 1024,
        timeout: float = 40.0, retries: int = 1) -> AIResult:
    return _dispatch(
        account_id,
        lambda p: p.chat(messages, system=system, json_mode=json_mode,
                         temperature=temperature, max_tokens=max_tokens, timeout=timeout),
        retries=retries,
    )


def validate_key(provider: str, *, account_id: str | None = None,
                use_generation: bool = False) -> AIResult:
    account_id = _account(account_id)
    factory = _provider_class(provider) or GeminiProvider
    res = factory(account_id).validate_key(use_generation=use_generation)
    try:
        ai_secrets.set_last_check(provider, res.ok if res.error_code != base.NOT_CONNECTED else None, account_id)
    except Exception:  # noqa: BLE001
        pass
    return res


def validate_supplied_key(provider: str, key: str, *, use_generation: bool = False) -> AIResult:
    """Проверить ПЕРЕДАННЫЙ ключ, ничего не сохраняя (мастер подключения)."""
    cls = _provider_class(provider) or GeminiProvider
    return cls.with_key(key).validate_key(use_generation=use_generation)


def connect(provider: str, key: str, *, account_id: str | None = None,
           consent: bool | None = None, model: str | None = None,
           use_generation: bool = False) -> AIResult:
    """Подключить провайдера: сперва проверка ключа, СОХРАНЕНИЕ только при успехе."""
    account_id = _account(account_id)
    key = (key or "").strip()
    if provider not in ai_secrets.PROVIDERS:
        return AIResult(ok=False, provider=provider, error_code=base.INVALID_REQUEST,
                        error_message="Неизвестный провайдер.")
    if not key:
        return AIResult(ok=False, provider=provider, error_code=base.INVALID_KEY,
                        error_message="Пустой ключ.")
    res = validate_supplied_key(provider, key, use_generation=use_generation)
    if not res.ok:
        return res  # ключ НЕ сохраняем
    primary = True if provider == "gemini" else None
    ai_secrets.set_api_key(provider, key, account_id, consent=consent,
                           model=model, primary=primary)
    ai_secrets.set_last_check(provider, True, account_id)
    ai_secrets.on_account_switch(account_id)  # сбросить кэш, чтобы подхватить новый ключ
    return res


def disconnect(provider: str, *, account_id: str | None = None) -> bool:
    account_id = _account(account_id)
    return ai_secrets.delete_api_key(provider, account_id)


# --------------------------------------------------------------------------- #
#  Статус для UI (только текущий аккаунт; ничего идентифицирующего наружу)
# --------------------------------------------------------------------------- #
def _model_for(name: str, account_id: str) -> str:
    factory = _provider_class(name) or GeminiProvider
    return factory(account_id).model_name


def _provider_card(account_id: str, name: str, active: str) -> dict:
    info = ai_secrets.info(name, account_id)
    connected = bool(info.get("connected"))
    model = _model_for(name, account_id) if connected else ""
    role = "primary" if active == name else ("secondary" if connected else "")
    card = {
        "provider": name,
        "connected": connected,
        "source": info.get("source", ""),
        "mask": info.get("mask", ""),
        "fingerprint": info.get("fingerprint", ""),
        "consent": bool(info.get("consent")),
        "model": model,
        "role": role,
        "added_at": info.get("added_at", 0.0),
        "last_checked_at": info.get("last_checked_at", 0.0),
        "last_check_ok": info.get("last_check_ok"),
    }
    if connected:
        card["usage"] = ai_usage.provider_status(
            name, account_id, info.get("fingerprint", ""), model=model)
    else:
        card["usage"] = None
    return card


def usage_payload(account_id: str | None = None) -> dict:
    """Данные для sidebar-индикатора и раздела «ИИ и лимиты» (текущий аккаунт)."""
    account_id = _account(account_id)
    active = active_provider(account_id)
    # Карточки строим циклом по реестру: новый провайдер появляется в интерфейсе
    # сам, без правки этой функции.
    cards = {name: _provider_card(account_id, name, active) for name in ai_secrets.PROVIDERS}
    connected = any(card["connected"] for card in cards.values())

    compact = None
    if active:
        card = cards.get(active) or {}
        usage = card.get("usage") or {}
        compact = {
            "provider": active,
            "model": card.get("model", ""),
            "percent_remaining": usage.get("percent_remaining", 0),
            "color": usage.get("color", "green"),
            "limiting": usage.get("limiting", "requests"),
            "estimate": usage.get("estimate", True),
            # у платного ключа дневного лимита нет — индикатор не должен
            # показывать «осталось N%», которых не существует
            "no_daily_cap": bool(usage.get("no_daily_cap")),
            "role": "primary",
        }

    return {
        "connected": connected,
        "primary": active,
        "active": ({"provider": active, "model": compact["model"]} if compact else None),
        "compact": compact,
        "providers": cards,
        "legacy": None,  # заполняется в app.py для обратной совместимости хаба
    }
