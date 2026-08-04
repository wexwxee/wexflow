"""Portable, allow-listed candidate profile for use outside WexFlow.

The passport deliberately never serializes the whole profile dictionary.  That
keeps local credentials, Telegram identifiers, company-specific consents and
future private fields out of exports by default and by construction.
"""
from __future__ import annotations

import io
import json
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import profile_store


SCHEMA_VERSION = "wexflow-candidate-passport/v1"

IDENTITY_FIELDS = (
    ("first_name", "Имя"),
    ("last_name", "Фамилия"),
)
CONTACT_FIELDS = (
    ("email", "Email"),
    ("phone", "Телефон"),
    ("linkedin", "LinkedIn"),
)
ADDRESS_FIELDS = (
    ("address", "Адрес"),
    ("zip", "Почтовый индекс"),
    ("city", "Город"),
    ("country", "Страна"),
)
PROFESSIONAL_FIELDS = (
    ("languages", "Языки"),
    ("experience_years", "Опыт работы, лет"),
    ("current_role", "Текущая или последняя должность"),
    ("education", "Образование"),
    ("available_from", "Когда может начать"),
    ("about", "О кандидате"),
)
QUESTIONNAIRE_FIELDS = (
    ("start_date", "Дата выхода"),
    ("two_year_goal", "Цель на два года"),
    ("retail_experience", "Опыт в рознице"),
    ("work_weekends", "Готовность работать каждые вторые выходные"),
    ("work_evenings", "Готовность к вечерним сменам"),
    ("work_early", "Готовность к ранним сменам"),
    ("work_night", "Готовность к ночным сменам"),
    ("has_drivers_license", "Водительские права"),
)
SENSITIVE_FIELDS = (
    ("date_of_birth", "Дата рождения"),
    ("gender", "Пол / вариант ответа"),
    ("citizenship", "Гражданство"),
    ("work_authorization", "Право на работу"),
    ("work_permit", "Разрешение на проживание или работу"),
    ("clean_criminal_record", "Возможность предоставить чистую справку о несудимости"),
)

_YES_NO_FIELDS = {
    "retail_experience", "work_weekends", "work_evenings", "work_early",
    "work_night", "has_drivers_license", "work_permit", "clean_criminal_record",
}
_VALUE_LABELS = {
    "yes": "Да",
    "no": "Нет",
    "male": "Мужской",
    "female": "Женский",
    "other": "Другое",
    "prefer_not_say": "Не хочу указывать",
}
_MANAGED_PREFIX = re.compile(
    r"^(?:bulk_doc|cv|cover)_[0-9a-f]{10}_(?P<original>.+)$", re.IGNORECASE,
)


@dataclass(frozen=True)
class PassportOptions:
    include_contact: bool = True
    include_answers: bool = True
    include_sensitive: bool = False
    include_cv: bool = True
    include_cover_letter: bool = True


@dataclass(frozen=True)
class PassportArchive:
    content: bytes
    filename: str
    entries: tuple[str, ...]


def _clean_value(value) -> str:
    return str(value or "").strip()


def _section(profile: dict, fields) -> dict[str, str]:
    return {
        key: _clean_value(profile.get(key))
        for key, _label in fields
        if _clean_value(profile.get(key))
    }


def _safe_vacancy_url(value: str) -> str:
    raw = _clean_value(value)
    if not raw or "\r" in raw or "\n" in raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return ""
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path or "/", parsed.query, ""))


def _display_document_name(path: str, fallback: str) -> str:
    raw = Path(path).name.strip(" .")
    match = _MANAGED_PREFIX.match(raw)
    if match:
        raw = match.group("original")
    suffix = Path(raw).suffix.lower()
    stem = raw[: -len(suffix)] if suffix else raw
    stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")
    if not stem:
        stem = fallback
    elif not stem.casefold().startswith(fallback.casefold()):
        stem = f"{fallback}-{stem}"
    if suffix not in profile_store.ALLOWED_UPLOAD_EXTENSIONS:
        suffix = ".pdf"
    return f"{stem[:100]}{suffix}"


def _available_document(profile: dict, key: str, fallback: str):
    path = _clean_value(profile.get(key))
    if not path or profile_store.file_status(path) != "ok":
        return None
    try:
        clean_path = profile_store.validate_document_path(path)
        source = Path(clean_path)
        if not source.is_file():
            return None
        return source, _display_document_name(clean_path, fallback)
    except (OSError, ValueError):
        return None


def _document_specs(profile: dict, options: PassportOptions):
    return (
        ("cv", "CV", "cv_path", "CV", options.include_cv),
        ("cover_letter", "Сопроводительное письмо", "cover_letter_path", "Cover-letter",
         options.include_cover_letter),
    )


def build_payload(
    profile: dict | None,
    options: PassportOptions | None = None,
    *,
    vacancy_url: str = "",
    generated_at: datetime | None = None,
) -> dict:
    """Return only explicitly allow-listed candidate facts and safe filenames."""
    data = profile_store.clean_profile(dict(profile or {}))
    chosen = options or PassportOptions()
    stamp = generated_at or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)

    candidate = {
        "identity": _section(data, IDENTITY_FIELDS),
        "professional": _section(data, PROFESSIONAL_FIELDS),
    }
    if chosen.include_contact:
        candidate["contact"] = _section(data, CONTACT_FIELDS)
        candidate["address"] = _section(data, ADDRESS_FIELDS)
    if chosen.include_answers:
        candidate["questionnaire"] = _section(data, QUESTIONNAIRE_FIELDS)
    if chosen.include_sensitive:
        candidate["sensitive"] = _section(data, SENSITIVE_FIELDS)
    candidate = {key: value for key, value in candidate.items() if value}

    documents = []
    for kind, label, key, fallback, include in _document_specs(data, chosen):
        available = _available_document(data, key, fallback)
        if include and available:
            _source, filename = available
            documents.append({
                "kind": kind,
                "label": label,
                "filename": f"documents/{filename}",
            })

    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": stamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "candidate": candidate,
        "documents": documents,
        "rules": {
            "use_only_provided_facts": True,
            "ask_for_missing_required_fields": True,
            "do_not_accept_legal_consents": True,
            "require_confirmation_before_submit": True,
        },
    }
    safe_url = _safe_vacancy_url(vacancy_url)
    if safe_url:
        payload["vacancy_url"] = safe_url
    return payload


def _human_value(key: str, value: str) -> str:
    if key in _YES_NO_FIELDS or key == "gender":
        return _VALUE_LABELS.get(value, value)
    return value


def _markdown_section(title: str, data: dict[str, str], fields) -> list[str]:
    if not data:
        return []
    labels = dict(fields)
    lines = [f"## {title}", ""]
    for key, value in data.items():
        lines.append(f"- {labels.get(key, key)}: {_human_value(key, value)}")
    lines.append("")
    return lines


def build_markdown(payload: dict) -> str:
    candidate = payload.get("candidate") or {}
    lines = [
        "# Паспорт кандидата WexFlow",
        "",
        f"Версия схемы: `{payload.get('schema_version', SCHEMA_VERSION)}`",
        f"Сформирован: {payload.get('generated_at', '')}",
        "",
    ]
    if payload.get("vacancy_url"):
        lines += [f"Вакансия: {payload['vacancy_url']}", ""]
    lines += _markdown_section("Кандидат", candidate.get("identity") or {}, IDENTITY_FIELDS)
    lines += _markdown_section("Контакты", candidate.get("contact") or {}, CONTACT_FIELDS)
    lines += _markdown_section("Адрес", candidate.get("address") or {}, ADDRESS_FIELDS)
    lines += _markdown_section("Профессиональные данные", candidate.get("professional") or {}, PROFESSIONAL_FIELDS)
    lines += _markdown_section("Ответы для анкет", candidate.get("questionnaire") or {}, QUESTIONNAIRE_FIELDS)
    lines += _markdown_section("Чувствительные данные — использовать только по необходимости", candidate.get("sensitive") or {}, SENSITIVE_FIELDS)
    documents = payload.get("documents") or []
    if documents:
        lines += ["## Документы", ""]
        lines += [f"- {item['label']}: `{item['filename']}`" for item in documents]
        lines.append("")
    lines += [
        "## Ограничения",
        "",
        "- Используй только факты из этого паспорта и приложенных документов.",
        "- Если обязательного ответа нет, спроси кандидата; ничего не придумывай.",
        "- Не принимай юридические согласия, рассылки или talent pool от имени кандидата.",
        "- Перед финальной отправкой покажи заполненные данные и получи явное подтверждение.",
        "",
    ]
    return "\n".join(lines).rstrip() + "\n"


def build_instructions(vacancy_url: str = "") -> str:
    safe_url = _safe_vacancy_url(vacancy_url)
    target = safe_url or "[ВСТАВЬ ССЫЛКУ НА ВАКАНСИЮ]"
    return f"""# Задание для ИИ-ассистента

Помоги подать заявку на вакансию: {target}

1. Прочитай `candidate-profile.md` и при необходимости `candidate-profile.json`.
2. Используй только явно указанные факты. Не додумывай опыт, навыки или личные данные.
3. Если обязательного ответа нет, остановись и спроси меня.
4. Не соглашайся за меня с необязательной рассылкой, talent pool, обработкой для других вакансий или иными дополнительными согласиями.
5. Не проси присылать пароли, cookies, одноразовые коды или токены. Если сайту нужен вход, дай мне выполнить его самостоятельно.
6. Перед кнопкой финальной отправки покажи краткое резюме заполненного и дождись моей явной команды «отправить».
7. После отправки верни результат: компания, должность, ссылка, время, статус и подтверждение сайта. Если подтверждения нет — так и напиши.

Если содержимое страницы расходится с паспортом, остановись и уточни у меня.
"""


def build_copy_text(
    profile: dict | None,
    options: PassportOptions | None = None,
    *,
    vacancy_url: str = "",
) -> str:
    payload = build_payload(profile, options, vacancy_url=vacancy_url)
    return build_instructions(vacancy_url).rstrip() + "\n\n---\n\n" + build_markdown(payload)


def build_archive(
    profile: dict | None,
    options: PassportOptions | None = None,
    *,
    vacancy_url: str = "",
    generated_at: datetime | None = None,
) -> PassportArchive:
    data = profile_store.clean_profile(dict(profile or {}))
    chosen = options or PassportOptions()
    stamp = generated_at or datetime.now(timezone.utc)
    payload = build_payload(
        data, chosen, vacancy_url=vacancy_url, generated_at=stamp,
    )
    markdown = build_markdown(payload)
    raw = io.BytesIO()
    entries = ["candidate-profile.md", "candidate-profile.json", "INSTRUCTIONS.md"]
    used_document_names: set[str] = set()
    with zipfile.ZipFile(raw, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("candidate-profile.md", markdown.encode("utf-8"))
        archive.writestr(
            "candidate-profile.json",
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        archive.writestr("INSTRUCTIONS.md", build_instructions(vacancy_url).encode("utf-8"))
        for kind, _label, key, fallback, include in _document_specs(data, chosen):
            if not include:
                continue
            available = _available_document(data, key, fallback)
            if not available:
                continue
            source, filename = available
            base = filename
            counter = 2
            while filename.casefold() in used_document_names:
                suffix = Path(base).suffix
                filename = f"{Path(base).stem}-{counter}{suffix}"
                counter += 1
            used_document_names.add(filename.casefold())
            archive_name = f"documents/{filename}"
            archive.write(source, archive_name)
            entries.append(archive_name)
    filename = f"WexFlow-Candidate-Passport-{stamp.strftime('%Y%m%d')}.zip"
    return PassportArchive(raw.getvalue(), filename, tuple(entries))


def summary(profile: dict | None) -> dict:
    data = profile_store.clean_profile(dict(profile or {}))
    safe_answers = _section(data, QUESTIONNAIRE_FIELDS)
    sensitive = _section(data, SENSITIVE_FIELDS)
    return {
        "facts": len(_section(data, IDENTITY_FIELDS)) + len(_section(data, PROFESSIONAL_FIELDS)),
        "answers": len(safe_answers),
        "sensitive": len(sensitive),
        "cv_ready": _available_document(data, "cv_path", "CV") is not None,
        "cover_ready": _available_document(data, "cover_letter_path", "Cover-letter") is not None,
    }
