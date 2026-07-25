"""Существующие ИИ-функции продолжают работать после смены провайдера.

Проверяем и главное: защита от выдуманных фактов НЕ ослаблена ни для одного
провайдера, а решение об отправке анкеты по-прежнему за человеком.
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ai_filters
import ai_gateway
from ai_providers.base import AIResult, OK
from connectors import ai_fill

CATS = {"sales": "Продажи"}
BRANDS = {"netto": "Netto", "foetex": "Føtex"}
EMPL = {"part": "Подработка"}
REGIONS = {"hovedstaden": "Столичный"}


def _ok(data, provider="groq", model="qwen/qwen3.6-27b"):
    return AIResult(ok=True, provider=provider, model=model, data=data, error_code=OK)


# ── 1. ИИ-создание фильтров автопилота через Groq ─────────────────────────── #
def test_suggest_filters_works_through_gateway_when_only_groq():
    data = {"max_km": 10, "max_hours": 20, "brand": ["netto"],
            "category": [], "employment_type": [], "regions": [],
            "age": None, "cities": "", "keywords": "", "exclude_keywords": "",
            "explanation": "Подработка в Netto рядом"}
    with mock.patch.object(ai_filters, "api_key", return_value=""), \
         mock.patch.object(ai_gateway, "available", return_value=True), \
         mock.patch.object(ai_gateway, "generate_json", return_value=_ok(data)) as gen:
        result = ai_filters.suggest_filters("подработка в нетто рядом", CATS, BRANDS, EMPL, REGIONS)

    assert result["ok"] is True
    assert result["fields"]["brand"] == "netto"
    assert result["fields"]["max_km"] == "10"
    gen.assert_called_once()


def test_suggest_filters_still_sanitizes_unknown_codes_from_any_provider():
    """Корректный JSON ≠ корректный ответ: чужие коды отбрасываются локально."""
    data = {"brand": ["netto", "MADE_UP_BRAND"], "category": ["hacked"],
            "employment_type": [], "regions": [], "max_km": -5,
            "age": "ancient", "cities": "", "keywords": "", "exclude_keywords": ""}
    with mock.patch.object(ai_filters, "api_key", return_value=""), \
         mock.patch.object(ai_gateway, "available", return_value=True), \
         mock.patch.object(ai_gateway, "generate_json", return_value=_ok(data)):
        result = ai_filters.suggest_filters("что угодно", CATS, BRANDS, EMPL, REGIONS)

    assert result["fields"]["brand"] == "netto"      # выдуманный бренд отброшен
    assert result["fields"]["category"] == ""        # выдуманная категория отброшена
    assert result["fields"]["max_km"] == ""          # отрицательное число отброшено
    assert result["fields"]["age"] == ""             # неизвестный возраст отброшен


# ── 2. Telegram ai_chat ───────────────────────────────────────────────────── #
def test_telegram_chat_works_through_gateway_and_sanitizes():
    payload = {"reply": "В каком городе ищешь?", "done": True,
               "fields": {"brand": ["foetex", "nope"], "category": [],
                          "employment_type": [], "regions": [],
                          "cities": "Aarhus", "keywords": "", "exclude_keywords": ""}}
    with mock.patch.object(ai_filters, "api_key", return_value=""), \
         mock.patch.object(ai_gateway, "available", return_value=True), \
         mock.patch.object(ai_gateway, "chat", return_value=_ok(payload)) as chat:
        result = ai_filters.chat([{"role": "user", "text": "ищу работу"}],
                                 CATS, BRANDS, EMPL, REGIONS)

    assert result["ok"] is True
    assert result["reply"] == "В каком городе ищешь?"
    assert result["done"] is True
    assert result["fields"]["brand"] == "foetex"     # мусорный код отброшен
    assert chat.call_args.kwargs["json_mode"] is True


# ── 3. Массовый импорт документов (generate_json фасад) ───────────────────── #
def test_document_import_gateway_path_returns_data():
    with mock.patch.object(ai_filters, "api_key", return_value=""), \
         mock.patch.object(ai_gateway, "available", return_value=True), \
         mock.patch.object(ai_gateway, "generate_json",
                           return_value=_ok({"items": [{"type": "cv"}]})):
        result = ai_filters.generate_json("разбери документы")

    assert result["ok"] is True
    assert result["data"] == {"items": [{"type": "cv"}]}
    assert result["model"] == "qwen/qwen3.6-27b"


def test_generate_json_without_any_provider_points_to_setup():
    with mock.patch.object(ai_filters, "api_key", return_value=""), \
         mock.patch.object(ai_gateway, "available", return_value=False):
        result = ai_filters.generate_json("что-нибудь")

    assert result["ok"] is False
    assert "подключ" in result["error"].casefold()


# ── 4. Умное заполнение анкет: провайдер сменился, защиты — нет ───────────── #
def test_ai_fill_uses_gateway_when_gemini_absent():
    with mock.patch.object(ai_fill.ai_filters, "api_key", return_value=""), \
         mock.patch.object(ai_gateway, "available", return_value=True), \
         mock.patch.object(ai_gateway, "generate_json",
                           return_value=_ok({"answers": {"f0": {"source": "city"}}})) as gen:
        data = ai_fill._ask_gemini("prompt")

    assert data == {"answers": {"f0": {"source": "city"}}}
    assert gen.call_args.kwargs["max_tokens"] == 512      # бережём бесплатный TPM


def test_ai_fill_validation_is_identical_for_groq_answers():
    """Ответ Groq проходит ту же локальную семантическую валидацию."""
    fields = [{"key": "f0", "label": "Municipality", "tag": "input"},
              {"key": "f1", "label": "Preferred language", "tag": "select",
               "options": ["English", "Danish"]}]
    profile = {"city": "København", "languages": "English — fluent"}

    # выдуманный текст игнорируется: берётся точное значение из профиля
    assert ai_fill._validate(
        {"f0": {"source": "city", "value": "HALLUCINATED"}}, fields, profile
    ) == {"f0": "København"}
    # вариант не из списка — отбрасывается
    assert ai_fill._validate(
        {"f1": {"source": "languages", "option": "Spanish"}}, fields, profile
    ) == {}
    # несуществующий источник — отбрасывается
    assert ai_fill._validate(
        {"f0": {"source": "experience_years", "value": "5"}}, fields, profile
    ) == {}


def test_no_invented_experience_dates_salary_education_or_work_rights():
    """Ключевые «придумки» невозможны: их нет в профиле -> нет и значения."""
    fields = [{"key": f"k{i}", "label": label, "tag": "input"} for i, label in enumerate(
        ["Years of experience", "Available from", "Expected salary",
         "Highest education", "Right to work"])]
    profile = {"city": "København"}          # ни одного из этих фактов нет

    invented = ai_fill._validate({
        "k0": {"source": "experience_years", "value": "5"},
        "k1": {"source": "available_from", "value": "2026-08-01"},
        "k2": {"source": "salary_expectation", "value": "30000"},
        "k3": {"source": "education", "value": "University"},
        "k4": {"source": "work_authorization", "value": "Yes"},
    }, fields, profile)

    assert invented == {}


def test_profile_minimization_unchanged():
    profile = {"city": "København", "email": "p@example.com",
               "date_of_birth": "1990-01-01", "about": "narrative"}

    language = ai_fill._profile_for_fields(profile, [{"label": "Preferred working language"}])
    email = ai_fill._profile_for_fields(profile, [{"label": "E-mail address"}])

    assert language == {"city": "København"}          # ни DOB, ни нарратив
    assert "date_of_birth" not in email and "about" not in email


# ── 5. Мотивационный черновик остаётся под отдельным тумблером ────────────── #
def test_motivation_still_requires_its_own_toggle():
    with mock.patch.object(ai_fill, "enabled", return_value=True), \
         mock.patch.dict(os.environ, {"WEXFLOW_AI_FILL_MOTIVATION": "0"}):
        assert ai_fill.motivation_enabled() is False
    with mock.patch.object(ai_fill, "enabled", return_value=False), \
         mock.patch.dict(os.environ, {"WEXFLOW_AI_FILL_MOTIVATION": "1"}):
        assert ai_fill.motivation_enabled() is False   # выключенный ИИ-fill перекрывает


# ── 6. Отправку по-прежнему не жмёт ИИ ────────────────────────────────────── #
def test_ai_layer_never_submits_the_form():
    """В ИИ-модуле нет ни одного действия отправки — только заполнение."""
    source = (ai_fill.__file__)
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    for forbidden in ("click_submit", 'type="submit"', "form.submit(", ".press(\"Enter\")"):
        assert forbidden not in text
    # решение об отправке принимает существующий workflow, не провайдер
    assert "submit" not in text.split("def fill(")[1][:2000].casefold()
