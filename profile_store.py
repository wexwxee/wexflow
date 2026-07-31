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
ANSWER_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("gender", "Пол (магазины спрашивают в анкете)", "choice:male,female,other"),
    ("start_date", "С какой даты можешь выйти", "date"),
    ("retail_experience", "Есть опыт работы в рознице/магазине", "yesno"),
    ("work_weekends", "Готов(а) работать каждые вторые выходные", "yesno"),
    ("work_evenings", "Готов(а) на вечерние смены (примерно до 22:00)", "yesno"),
    ("work_early", "Готов(а) выходить рано утром (с 06:00)", "yesno"),
    ("work_night", "Готов(а) на ночные смены", "yesno"),
    ("has_drivers_license", "Есть водительские права", "yesno"),
    ("profile_visible", "Разрешаю показывать анкету другим магазинам этой сети", "yesno"),
)

ANSWER_KEYS = tuple(key for key, _human, _kind in ANSWER_FIELDS)

_YESNO = {"yes", "no"}
_GENDERS = {"male", "female", "other"}


def clean_answer(key: str, value) -> str:
    """Привести ответ к хранимому виду. Мусор и «не выбрано» → пустая строка."""
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    kind = dict((k, t) for k, _h, t in ANSWER_FIELDS).get(key, "")
    if kind == "yesno":
        if raw in {"yes", "да", "ja", "1", "true", "on"}:
            return "yes"
        if raw in {"no", "нет", "nej", "0", "false", "off"}:
            return "no"
        return ""
    if kind.startswith("choice:"):
        return raw if raw in _GENDERS else ""
    if kind == "date":
        return str(value).strip()[:10]
    return str(value).strip()[:120]


def answers(profile: dict | None = None) -> dict:
    """Только ответы для анкет — в том виде, в котором их читает заполнитель."""
    data = dict(profile or {})
    return {key: clean_answer(key, data.get(key)) for key in ANSWER_KEYS}


def missing_answers(profile: dict | None = None, keys=None) -> list[str]:
    """Человеческие названия неотвеченных вопросов (для честного стопа подачи)."""
    ready = answers(profile)
    wanted = list(keys or ANSWER_KEYS)
    humans = dict((k, h) for k, h, _t in ANSWER_FIELDS)
    return [humans.get(key, key) for key in wanted if not ready.get(key)]


def clean_profile(data: dict) -> dict:
    data = dict(data or {})
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


def _read_json(path: Path) -> dict | None:
    """Прочитать JSON-словарь или вернуть None, если файла нет либо он битый —
    без падения приложения."""
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (ValueError, OSError):
        return None


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


def load_profile() -> dict:
    _migrate_legacy_profile()
    path = config.SHARED_PROFILE_PATH
    if path.exists():
        data = _read_json(path)
        if data is not None:
            return clean_profile(data)
        # основной файл битый: пробуем последнюю резервную копию, затем откладываем битый
        backup = _read_json(path.with_name(path.name + ".bak"))
        _backup_corrupt(path, "невалидный JSON")
        if backup is not None:
            print("profile.json восстановлен из .bak")
            save_profile(backup)          # вернём хороший профиль на место (атомарно)
            return clean_profile(backup)
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


def save_profile(data: dict):
    data = clean_profile(data)
    path = config.SHARED_PROFILE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    tmp = path.with_name(path.name + ".tmp")
    with _LOCK:
        tmp.write_text(payload, encoding="utf-8")   # атомарно: пишем во временный…
        json_store.replace_file(tmp, path)           # …и подменяем одним движением
        # зеркалим последний УСПЕШНО записанный профиль в .bak — если основной файл
        # позже побьётся, load_profile восстановит из него свежее состояние
        try:
            shutil.copy2(path, path.with_name(path.name + ".bak"))
        except OSError:
            pass


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
