"""Хранение данных для ассистированной подачи."""
import filecmp
import json
import os
import re
import shutil
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path

import config
import json_store
import candidate_profiles

UPLOAD_DIR = config.DATA_DIR / "uploads"
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
ALLOWED_UPLOAD_EXTENSIONS = {".pdf", ".doc", ".docx"}
_MANAGED_UPLOAD_PREFIX_RE = re.compile(
    r"^(?:bulk_doc|cv|cover)_[0-9a-f]{10}_(?P<original>.+)$",
    re.IGNORECASE,
)


def _clean_upload_filename(filename: str | None) -> str:
    raw_name = Path(filename or "file.pdf").name.strip() or "file.pdf"
    raw_name = raw_name.strip(" .") or "file.pdf"
    suffix = Path(raw_name).suffix.lower()
    stem = raw_name[: -len(suffix)] if suffix else raw_name
    stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    stem = re.sub(r"\s+", "_", stem)
    stem = re.sub(r"\.{2,}", ".", stem).strip(" ._-")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")
    if not stem:
        stem = "file"
    return f"{stem}{suffix}"


def _safe_copy_target(source: Path, prefix: str, safe_name: str) -> Path:
    UPLOAD_DIR.mkdir(exist_ok=True)
    if not safe_name.lower().startswith(f"{prefix.lower()}_"):
        safe_name = f"{prefix}_{safe_name}"
    target = UPLOAD_DIR / safe_name
    try:
        if target.resolve() == source.resolve():
            return target
    except OSError:
        pass
    if target.exists():
        try:
            if filecmp.cmp(source, target, shallow=False):
                return target
        except OSError:
            pass
        target = UPLOAD_DIR / f"{prefix}_{uuid.uuid4().hex[:10]}_{safe_name}"
    return target


def safe_document_upload_path(path: str, prefix: str) -> str:
    """Return an exact document copy with a site-friendly visible basename.

    Bulk import keeps a collision-proof managed name such as
    ``bulk_doc_<id>_Ivan_Cover_Letter_Lidl.pdf``. Browser file controls expose
    that basename to the employer, and narrow controls show only the meaningless
    ``bulk_doc_<id>`` prefix. Keep the managed source intact, but upload an
    byte-identical alias whose visible name starts with ``cv`` or ``cover``.
    """
    clean_path = validate_document_path(path)
    if not clean_path:
        return ""
    source = Path(clean_path)
    if not source.exists() or not source.is_file():
        return clean_path
    safe_name = _clean_upload_filename(source.name)
    managed_match = _MANAGED_UPLOAD_PREFIX_RE.match(safe_name)
    if managed_match:
        safe_name = _clean_upload_filename(managed_match.group("original"))
    if safe_name == source.name:
        return clean_path
    target = _safe_copy_target(source, prefix, safe_name)
    if not target.exists():
        shutil.copy2(source, target)
    return str(target)

CITY_FIXES = {
    "k??benhavn": "København",
    "k?benhavn": "København",
    "kobenhavn": "København",
    "koebenhavn": "København",
    "copenhagen": "København",
}

COUNTRY_FIXES = {
    "denmark": "Danmark",
    "danish": "Danmark",
    "dk": "Danmark",
}

# ── Ответы для анкет ───────────────────────────────────────────────────────
# Магазины спрашивают одно и то же: пол, дата выхода, готов ли работать по
# выходным/вечерам/с раннего утра. Раньше WexFlow оставлял эти вопросы человеку —
# и подача не могла завершиться сама. Теперь ответы хранятся ОДИН раз здесь и
# подставляются как есть. Правило прежнее: чего человек не ответил, то WexFlow
# не выдумывает — вопрос остаётся пустым, а автоподача просто не жмёт кнопку.
#
# ЕДИНСТВЕННОЕ МЕСТО, где объявляется вопрос анкеты. Одна строка ANSWER_FIELDS
# описывает про поле всё: имя, подпись, что в нём можно хранить, наследуется ли
# ответ между компаниями, рисуется ли поле в настройках автоматически и попадает
# ли оно в паспорт кандидата. Отсюда читают настройки (шаблон и автосохранение),
# сохранение профиля, паспорт и ИИ-дозаполнение — руками их дублировать больше
# не нужно. Раньше список был переписан в семи местах, и забытое место ломалось
# молча: поле рисовалось, но не сохранялось.

# Куда попадает ответ. Это не косметика: от scope зависит, увидит ли ответ
# ДРУГОЙ работодатель.
SHARED = "shared"    # общий факт — переносится в другие компании с разрешения
COMPANY = "company"  # ответ конкретной формы, между компаниями не ходит
CONSENT = "consent"  # согласие работодателю, не наследуется никогда
LEGACY = "legacy"    # старое поле, оставлено только ради миграции данных


@dataclass(frozen=True)
class AnswerField:
    """Один вопрос анкеты со всем, что о нём нужно знать программе."""

    key: str
    human: str          # подпись в настройках и в отчёте «не отвечено»
    kind: str           # yesno | text | date | choice:вариант,вариант
    scope: str = SHARED
    auto_ui: bool = False   # рисуется общим циклом «да/нет» в настройках
    export: str = ""        # раздел паспорта: questionnaire | sensitive | «» — не выгружать
    export_human: str = ""  # подпись в паспорте, если короче настроечной

    @property
    def passport_human(self) -> str:
        return self.export_human or self.human


ANSWER_FIELDS: tuple[AnswerField, ...] = (
    AnswerField(
        "gender",
        "Пол (магазины иногда спрашивают в анкете)",
        "choice:male,female,other,prefer_not_say",
        export="sensitive", export_human="Пол / вариант ответа",
    ),
    AnswerField(
        "start_date", "С какой даты можешь выйти", "date",
        export="questionnaire", export_human="Дата выхода",
    ),
    AnswerField(
        "two_year_goal", "Где видишь себя через два года", "text",
        export="questionnaire", export_human="Цель на два года",
    ),
    AnswerField(
        "retail_experience", "Есть опыт работы в рознице/магазине", "yesno",
        auto_ui=True, export="questionnaire", export_human="Опыт в рознице",
    ),
    AnswerField(
        "warehouse_experience", "Есть опыт складской работы (lager)", "yesno",
        auto_ui=True, export="questionnaire", export_human="Опыт складской работы",
    ),
    AnswerField(
        "english_work", "Можешь общаться на английском по работе", "yesno",
        auto_ui=True, export="questionnaire",
        export_human="Английский для рабочего общения",
    ),
    AnswerField(
        "work_weekends", "Готов(а) работать каждые вторые выходные", "yesno",
        auto_ui=True, export="questionnaire",
        export_human="Готовность работать каждые вторые выходные",
    ),
    AnswerField(
        "work_evenings", "Готов(а) на вечерние смены (примерно до 22:00)", "yesno",
        auto_ui=True, export="questionnaire",
        export_human="Готовность к вечерним сменам",
    ),
    AnswerField(
        "work_early", "Готов(а) выходить рано утром (с 06:00)", "yesno",
        auto_ui=True, export="questionnaire", export_human="Готовность к ранним сменам",
    ),
    AnswerField(
        "work_night", "Готов(а) на ночные смены", "yesno",
        auto_ui=True, export="questionnaire", export_human="Готовность к ночным сменам",
    ),
    AnswerField(
        "has_drivers_license", "Есть водительские права", "yesno",
        auto_ui=True, export="questionnaire", export_human="Водительские права",
    ),
    AnswerField(
        "lidl_referral_name", "Lidl: имя сотрудника, который порекомендовал", "text",
        scope=COMPANY,
    ),
    AnswerField(
        "lidl_current_employee", "Lidl: уже работаешь в Lidl", "yesno", scope=COMPANY,
    ),
    AnswerField(
        "lidl_previous_employment", "Lidl: где и когда раньше работал(а) в Lidl", "text",
        scope=COMPANY,
    ),
    AnswerField(
        "lidl_part_time_availability",
        "Lidl: как тебе подходит указанный неполный график",
        "text", scope=COMPANY,
    ),
    AnswerField("lidl_discovery", "Lidl: как узнал(а) о вакансии", "text", scope=COMPANY),
    AnswerField(
        "citizenship", "Lidl: гражданство", "text",
        export="sensitive", export_human="Гражданство",
    ),
    AnswerField(
        "work_permit", "Lidl: есть действующее разрешение на проживание/работу", "yesno",
        export="sensitive", export_human="Разрешение на проживание или работу",
    ),
    AnswerField(
        "clean_criminal_record",
        "Lidl: можешь предоставить чистую справку о несудимости", "yesno",
        export="sensitive",
        export_human="Возможность предоставить чистую справку о несудимости",
    ),
    AnswerField(
        "relevant_health_condition",
        "Lidl: заболевания, существенно влияющие на работу", "text",
        scope=COMPANY,
    ),
    AnswerField(
        "lidl_newsletter", "Lidl: получать новости о вакансиях", "yesno", scope=CONSENT,
    ),
    AnswerField(
        "lidl_profile_scope",
        "Lidl: для каких вакансий разрешено учитывать профиль",
        "choice:international,country,applied_only", scope=CONSENT,
    ),
    AnswerField(
        "profile_visible",
        "Разрешаю показывать анкету другим магазинам этой сети", "yesno",
        scope=LEGACY,
    ),
)

ANSWER_BY_KEY: dict[str, AnswerField] = {field.key: field for field in ANSWER_FIELDS}
ANSWER_KEYS = tuple(ANSWER_BY_KEY)
ANSWER_HUMANS = {field.key: field.human for field in ANSWER_FIELDS}


def answer_keys(*scopes: str) -> tuple[str, ...]:
    """Ключи ответов с нужной областью видимости, в порядке объявления."""
    return tuple(field.key for field in ANSWER_FIELDS if field.scope in scopes)


def export_fields(section: str) -> tuple[tuple[str, str], ...]:
    """Пары (ключ, подпись) для раздела паспорта кандидата."""
    return tuple(
        (field.key, field.passport_human)
        for field in ANSWER_FIELDS
        if field.export == section
    )


# Поля, которые настройки рисуют одинаковым циклом «Да / Нет / не отвечать».
# Новый такой вопрос достаточно объявить выше с auto_ui=True.
AUTO_UI_ANSWERS = tuple(
    (field.key, field.human) for field in ANSWER_FIELDS if field.auto_ui
)

YESNO_ANSWER_KEYS = tuple(field.key for field in ANSWER_FIELDS if field.kind == "yesno")

_YESNO = {"yes", "no"}

# Exact values currently offered by Lidl EasyApply.  The Russian label belongs
# only to WexFlow; the Danish value is what the employer's form receives.
LIDL_DISCOVERY_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Anbefalet stillingen af en nuværende Lidl-medarbejder", "Порекомендовал действующий сотрудник Lidl"),
    ("Anbefalet stillingen gennem ven/familie/etc.", "Посоветовали друзья или родственники"),
    ("Andet", "Другое"),
    ("Elevplads.dk", "Elevplads.dk"),
    ("Elevportalen", "Elevportalen"),
    ("Facebook", "Facebook"),
    ("Graduateland", "Graduateland"),
    ("Instagram", "Instagram"),
    ("Jobindex", "Jobindex"),
    ("Jobopslag i butikken", "Объявление в магазине"),
    ("Lidls karriereside", "Карьерный сайт Lidl"),
    ("LinkedIn", "LinkedIn"),
    ("Messe", "Ярмарка вакансий"),
    ("TikTok", "TikTok"),
    ("Ungarbejder.dk", "Ungarbejder.dk"),
)

# Ниже — та же таблица ANSWER_FIELDS, разложенная по областям видимости.
# Списки вычисляются, а не переписываются руками: разъехаться они уже не могут.

# Факты, которые человек может один раз разрешить использовать во всех анкетах.
# Контактные данные живут отдельно в основном профиле, документы — в правилах
# документов. Здесь только ответы на вопросы работодателя.
REUSABLE_ANSWER_KEYS: tuple[str, ...] = answer_keys(SHARED)

# Эти значения — не общие факты, а отдельное согласие конкретному работодателю.
# Они никогда не наследуются между компаниями, даже если повторное использование
# общих ответов включено.
COMPANY_CONSENT_KEYS: tuple[str, ...] = answer_keys(CONSENT)

# Значение является вариантом конкретной формы Lidl, а не свободным общим
# ответом для других работодателей.
COMPANY_LOCAL_KEYS: tuple[str, ...] = answer_keys(COMPANY, CONSENT)

COMPANY_OVERRIDE_KEYS = REUSABLE_ANSWER_KEYS + COMPANY_LOCAL_KEYS

# Значения сохраняются в том виде, в котором их обычно ожидают датские формы.
# Русская подпись живёт только в интерфейсе и не попадает работодателю.
CITIZENSHIP_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Danmark", "Дания"),
    ("Ukraine", "Украина"),
    ("Polen", "Польша"),
    ("Sverige", "Швеция"),
    ("Norge", "Норвегия"),
    ("Finland", "Финляндия"),
    ("Island", "Исландия"),
    ("Tyskland", "Германия"),
    ("Storbritannien", "Великобритания"),
    ("Irland", "Ирландия"),
    ("Frankrig", "Франция"),
    ("Spanien", "Испания"),
    ("Italien", "Италия"),
    ("Portugal", "Португалия"),
    ("Nederlandene", "Нидерланды"),
    ("Belgien", "Бельгия"),
    ("Luxembourg", "Люксембург"),
    ("Østrig", "Австрия"),
    ("Schweiz", "Швейцария"),
    ("Tjekkiet", "Чехия"),
    ("Slovakiet", "Словакия"),
    ("Ungarn", "Венгрия"),
    ("Rumænien", "Румыния"),
    ("Bulgarien", "Болгария"),
    ("Litauen", "Литва"),
    ("Letland", "Латвия"),
    ("Estland", "Эстония"),
    ("Kroatien", "Хорватия"),
    ("Slovenien", "Словения"),
    ("Grækenland", "Греция"),
    ("Cypern", "Кипр"),
    ("Malta", "Мальта"),
    ("Albanien", "Албания"),
    ("Bosnien-Hercegovina", "Босния и Герцеговина"),
    ("Kosovo", "Косово"),
    ("Montenegro", "Черногория"),
    ("Nordmakedonien", "Северная Македония"),
    ("Serbien", "Сербия"),
    ("Moldova", "Молдова"),
    ("Georgien", "Грузия"),
    ("Armenien", "Армения"),
    ("Aserbajdsjan", "Азербайджан"),
    ("Belarus", "Беларусь"),
    ("Rusland", "Россия"),
    ("Tyrkiet", "Турция"),
    ("USA", "США"),
    ("Canada", "Канада"),
    ("Australien", "Австралия"),
    ("New Zealand", "Новая Зеландия"),
    ("Indien", "Индия"),
    ("Kina", "Китай"),
    ("Japan", "Япония"),
    ("Sydkorea", "Южная Корея"),
    ("Syrien", "Сирия"),
    ("Afghanistan", "Афганистан"),
    ("Irak", "Ирак"),
    ("Iran", "Иран"),
    ("Israel", "Израиль"),
    ("Egypten", "Египет"),
    ("Marokko", "Марокко"),
    ("Tunesien", "Тунис"),
    ("Sydafrika", "ЮАР"),
    ("Brasilien", "Бразилия"),
    ("Argentina", "Аргентина"),
    ("Chile", "Чили"),
    ("Mexico", "Мексика"),
    ("Andet", "Другое / страны нет в списке"),
)


def clean_answer(key: str, value) -> str:
    """Привести ответ к хранимому виду. Мусор и «не выбрано» → пустая строка."""
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    field = ANSWER_BY_KEY.get(key)
    kind = field.kind if field else ""
    if kind == "yesno":
        if raw in {"yes", "да", "ja", "1", "true", "on"}:
            return "yes"
        if raw in {"no", "нет", "nej", "0", "false", "off"}:
            return "no"
        return ""
    if kind.startswith("choice:"):
        allowed = {item.strip() for item in kind.removeprefix("choice:").split(",")}
        return raw if raw in allowed else ""
    if kind == "date":
        return str(value).strip()[:10]
    # Free-text application answers can legitimately be several sentences.
    # The old 120-character cap silently cut the candidate's two-year goal
    # after saving it, even though the account UI still showed the full draft
    # until the next reload.
    return str(value).strip()[:2000]


def answers(profile: dict | None = None) -> dict:
    """Только ответы для анкет — в том виде, в котором их читает заполнитель."""
    data = dict(profile or {})
    return {key: clean_answer(key, data.get(key)) for key in ANSWER_KEYS}


def normalize_company_key(value: str) -> str:
    """Stable local key for a brand/company label or connector slug."""
    raw = str(value or "").strip().casefold()
    aliases = {
        "lidl easyapply": "lidl",
        "lidl_easy_apply": "lidl",
        "lidl danmark": "lidl",
        "lidl dk": "lidl",
        "salling group": "sallinggroup",
        "føtex": "foetex",
        "f\u00f8tex": "foetex",
    }
    raw = aliases.get(raw, raw)
    if raw.startswith("lidl "):
        raw = "lidl"
    clean = re.sub(r"[^a-z0-9æøå]+", "-", raw, flags=re.I).strip("-")
    return clean[:64]


def company_overrides(profile: dict | None = None) -> dict[str, dict]:
    """Return validated per-company answer overrides from the candidate profile."""
    raw_items = (profile or {}).get("company_answer_overrides")
    if not isinstance(raw_items, dict):
        return {}
    clean_items: dict[str, dict] = {}
    for raw_key, raw_rule in raw_items.items():
        if not isinstance(raw_rule, dict):
            continue
        key = normalize_company_key(raw_key)
        if not key:
            continue
        raw_answers = raw_rule.get("answers")
        if not isinstance(raw_answers, dict):
            raw_answers = {}
        rule_answers = {
            answer_key: clean_answer(answer_key, raw_answers.get(answer_key))
            for answer_key in COMPANY_OVERRIDE_KEYS
            if clean_answer(answer_key, raw_answers.get(answer_key))
        }
        clean_items[key] = {
            "label": str(raw_rule.get("label") or raw_key).strip()[:80] or key,
            "inherit_defaults": (
                "no" if str(raw_rule.get("inherit_defaults") or "").lower() == "no"
                else "yes"
            ),
            "answers": rule_answers,
        }
    return clean_items


def resolve_company_answers(profile: dict, company: str) -> dict:
    """Apply reuse consent and one company's sparse overrides to a profile.

    Common questionnaire answers are exposed only after the candidate has
    explicitly allowed reuse. A company rule can inherit those defaults or
    start empty, then replace only selected values. Employer-specific legal
    consents are always empty unless that company's rule contains them.
    """
    data = clean_profile(profile)
    resolved = dict(data)
    company_key = normalize_company_key(company)
    reuse_allowed = str(data.get("answer_reuse_consent") or "").lower() == "yes"
    common = answers(data)

    for key in REUSABLE_ANSWER_KEYS:
        resolved[key] = common.get(key, "") if reuse_allowed else ""
    for key in COMPANY_LOCAL_KEYS:
        resolved[key] = ""

    rule = company_overrides(data).get(company_key)
    if rule:
        if rule["inherit_defaults"] == "no":
            for key in REUSABLE_ANSWER_KEYS:
                resolved[key] = ""
        for key, value in rule["answers"].items():
            resolved[key] = value

    resolved["_answers_company_key"] = company_key
    resolved["_allow_shared_answers"] = reuse_allowed
    resolved["_company_override_active"] = bool(rule)
    return resolved


def missing_answers(profile: dict | None = None, keys=None) -> list[str]:
    """Человеческие названия неотвеченных вопросов (для честного стопа подачи)."""
    ready = answers(profile)
    wanted = list(keys or ANSWER_KEYS)
    return [ANSWER_HUMANS.get(key, key) for key in wanted if not ready.get(key)]


def clean_profile(data: dict) -> dict:
    data = dict(data or {})
    # Старое общее «показывать профиль» было только да/нет. У Lidl теперь три
    # точных варианта. При первом чтении сохраняем прежний смысл осторожно:
    # «да» — только страна проживания, «нет» — лишь лично выбранные вакансии.
    # Международный talent pool никогда не включаем без явного выбора человека.
    if "lidl_profile_scope" not in data:
        legacy_visible = clean_answer("profile_visible", data.get("profile_visible"))
        if legacy_visible == "yes":
            data["lidl_profile_scope"] = "country"
        elif legacy_visible == "no":
            data["lidl_profile_scope"] = "applied_only"
    consent = str(data.get("answer_reuse_consent") or "").strip().lower()
    data["answer_reuse_consent"] = consent if consent in {"yes", "no"} else ""

    # Versioned one-shot repair of the 1.3.63 flat Lidl fields.  The old
    # migration accidentally snapshotted reusable answers (weekends, English,
    # etc.) into a permanent Lidl override, so later edits in the main profile
    # were silently ignored.  Shared answers must stay inherited; only fields
    # whose meaning is company-specific move into the Lidl rule.
    overrides = company_overrides(data)
    try:
        migration_version = int(data.get("company_answers_migration") or 0)
    except (TypeError, ValueError):
        migration_version = 0
    if migration_version < 2:
        rule = overrides.get("lidl") or {
            "label": "Lidl", "inherit_defaults": "yes", "answers": {},
        }
        current_answers = dict(rule.get("answers") or {})
        # Values equal to the old flat shared value are migration snapshots,
        # not intentional overrides.  Remove them so future edits propagate.
        for key in REUSABLE_ANSWER_KEYS:
            if (key in current_answers
                    and current_answers[key] == clean_answer(key, data.get(key))):
                current_answers.pop(key, None)
        for key in COMPANY_LOCAL_KEYS:
            value = clean_answer(key, data.get(key))
            if value and key not in current_answers:
                current_answers[key] = value
        if current_answers:
            rule["answers"] = current_answers
            overrides["lidl"] = rule
        elif "lidl" in overrides:
            overrides["lidl"] = {**rule, "answers": {}}
        data["company_answers_migration"] = 2
    data["company_answer_overrides"] = overrides
    city_key = str(data.get("city") or "").strip().lower()
    country_key = str(data.get("country") or "").strip().lower()
    if city_key in CITY_FIXES:
        data["city"] = CITY_FIXES[city_key]
    if country_key in COUNTRY_FIXES:
        data["country"] = COUNTRY_FIXES[country_key]
    return data


def _migrate_legacy_profile() -> None:
    """Один раз переносит старый модульный профиль в общий файл WexFlow.

    Раньше профиль кандидата жил в DATA_DIR/profile.json (только Salling). Теперь
    он общий (SHARED_PROFILE_PATH), чтобы оба модуля использовали одни данные.
    Если общего файла ещё нет, а старый есть — копируем его содержимое.
    """
    shared = config.SHARED_PROFILE_PATH
    legacy = getattr(config, "LEGACY_SHARED_PROFILE_PATH", config.PROFILE_PATH)
    if not candidate_profiles.is_primary():
        return
    if shared.exists() or not legacy.exists() or shared == legacy:
        return
    try:
        shared.parent.mkdir(parents=True, exist_ok=True)
        shared.write_text(legacy.read_text(encoding="utf-8"), encoding="utf-8")
    except OSError:
        pass


# ── Надёжность profile.json (F37; тот же приём, что в settings_store/F33) ──
_LOCK = threading.RLock()


class _InvalidProfileError(ValueError):
    """The file was read successfully, but is not a JSON object."""


def _read_json(path: Path) -> dict | None:
    """Read a JSON object.

    A missing file is ``None``. Invalid JSON/non-object content is a proven
    corrupt profile and gets its own exception. An ``OSError`` is deliberately
    allowed through: a temporarily locked/unavailable file must never be
    renamed as corrupt or replaced with a default profile.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except UnicodeDecodeError as exc:
        raise _InvalidProfileError("profile is not UTF-8") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _InvalidProfileError("invalid JSON") from exc
    if not isinstance(data, dict):
        raise _InvalidProfileError("JSON root is not an object")
    return data


def _backup_corrupt(path: Path, err) -> None:
    """Отложить повреждённый profile.json в копию .corrupt-<ts>, чтобы данные
    можно было восстановить вручную, а приложение продолжило работу."""
    try:
        if path.exists():
            bad = path.parent / f"{path.stem}.corrupt-{int(time.time())}.json"
            path.replace(bad)
            print(f"profile.json повреждён ({err}); отложил копию: {bad.name}")
    except OSError:
        pass


def _load_profile_unlocked() -> dict:
    _migrate_legacy_profile()
    path = config.SHARED_PROFILE_PATH
    try:
        data = _read_json(path)
    except _InvalidProfileError as exc:
        # Основной файл точно прочитан, но битый: только в этом случае
        # его можно отложить как .corrupt. Ошибка ввода-вывода сюда не попадает.
        try:
            backup = _read_json(path.with_name(path.name + ".bak"))
        except (OSError, _InvalidProfileError):
            backup = None
        _backup_corrupt(path, exc)
        if backup is not None:
            print("profile.json восстановлен из .bak")
            _save_profile_unlocked(backup)  # вернём хорошую копию атомарно
            return clean_profile(backup)
    else:
        if data is not None:
            return clean_profile(data)
    if (config.BASE_DIR / "profile.example.json").exists():
        data = json.loads((config.BASE_DIR / "profile.example.json").read_text(encoding="utf-8"))
        data["first_name"] = ""
        data["last_name"] = ""
        data["email"] = ""
        data["phone"] = ""
        data["address"] = ""
        data["zip"] = ""
        data["city"] = ""
        data["country"] = ""
        data["cv_path"] = ""
        data["cover_letter_path"] = ""
        return data
    return {"cv_path": "", "cover_letter_path": ""}


def load_profile() -> dict:
    """Return one consistent profile snapshot.

    Transient read errors propagate so a caller cannot unknowingly turn an I/O
    outage into an empty-profile save. The original file remains untouched.
    """
    with _LOCK:
        return _load_profile_unlocked()


def _save_profile_unlocked(data: dict) -> dict:
    data = clean_profile(data)
    path = config.SHARED_PROFILE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8")   # атомарно: пишем во временный…
    json_store.replace_file(tmp, path)           # …и подменяем одним движением
    # зеркалим последний УСПЕШНО записанный профиль в .bak — если основной файл
    # позже побьётся, load_profile восстановит из него свежее состояние
    try:
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    except OSError:
        pass
    return data


def save_profile(data: dict) -> None:
    with _LOCK:
        _save_profile_unlocked(data)


def mutate_profile(updater) -> dict:
    """Atomically read, update and save the candidate profile.

    ``updater`` may mutate its dictionary in place and return ``None``, or
    return a replacement dictionary. Holding one RLock across the complete
    cycle prevents concurrent partial forms from dropping each other's fields.
    """
    with _LOCK:
        profile = _load_profile_unlocked()
        replacement = updater(profile)
        if replacement is not None:
            if not isinstance(replacement, dict):
                raise TypeError("profile updater must return dict or None")
            profile = replacement
        return _save_profile_unlocked(profile)


def validate_document_path(path: str) -> str:
    """Validate a manually entered CV/cover-letter path and return a cleaned path."""
    value = (path or "").strip().strip('"')
    if not value:
        return ""
    candidate = Path(value).expanduser()
    if candidate.suffix.lower() not in ALLOWED_UPLOAD_EXTENSIONS:
        raise ValueError("Можно выбрать только PDF, DOC или DOCX.")
    try:
        if candidate.exists():
            if not candidate.is_file():
                raise ValueError("Выбранный путь не является файлом.")
            if candidate.stat().st_size > MAX_UPLOAD_BYTES:
                raise ValueError("Файл слишком большой. Максимум 25 МБ.")
    except OSError as exc:
        raise ValueError("Не удалось проверить файл. Попробуй выбрать его заново.") from exc
    return str(candidate)


def save_upload(upload_file, prefix: str) -> str:
    """Сохраняет UploadFile в uploads/ и возвращает абсолютный путь."""
    UPLOAD_DIR.mkdir(exist_ok=True)
    safe_name = _clean_upload_filename(upload_file.filename)
    suffix = Path(safe_name).suffix.lower()
    if suffix not in ALLOWED_UPLOAD_EXTENSIONS:
        raise ValueError("Можно загрузить только PDF, DOC или DOCX.")
    target = UPLOAD_DIR / f"{prefix}_{uuid.uuid4().hex[:10]}_{safe_name}"
    written = 0
    with target.open("wb") as f:
        while True:
            chunk = upload_file.file.read(1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > MAX_UPLOAD_BYTES:
                try:
                    target.unlink()
                except OSError:
                    pass
                raise ValueError("Файл слишком большой. Максимум 25 МБ.")
            f.write(chunk)
    return str(target)


def remove_managed_document(path: str) -> bool:
    """Delete only WexFlow's private upload copy, never a user's source file."""
    if not path:
        return False
    try:
        candidate = Path(path).resolve()
        uploads = UPLOAD_DIR.resolve()
        if candidate.parent != uploads or not candidate.is_file():
            return False
        candidate.unlink()
        return True
    except OSError:
        return False


def file_label(path: str) -> str:
    if not path:
        return "Файл не выбран"
    p = Path(path)
    return p.name if p.name else path


def file_status(path: str) -> str:
    if not path:
        return "empty"
    try:
        candidate = Path(validate_document_path(path))
        if not candidate.exists() or not candidate.is_file():
            return "missing"
        if candidate.stat().st_size > MAX_UPLOAD_BYTES:
            return "missing"
    except (OSError, ValueError):
        return "missing"
    return "ok"
