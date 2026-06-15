"""Хранение данных для ассистированной подачи."""
import json
import uuid
from pathlib import Path

import config

UPLOAD_DIR = config.DATA_DIR / "uploads"
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
ALLOWED_UPLOAD_EXTENSIONS = {".pdf", ".doc", ".docx"}

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


def clean_profile(data: dict) -> dict:
    data = dict(data or {})
    city_key = str(data.get("city") or "").strip().lower()
    country_key = str(data.get("country") or "").strip().lower()
    if city_key in CITY_FIXES:
        data["city"] = CITY_FIXES[city_key]
    if country_key in COUNTRY_FIXES:
        data["country"] = COUNTRY_FIXES[country_key]
    return data


def load_profile() -> dict:
    if config.PROFILE_PATH.exists():
        return clean_profile(json.loads(config.PROFILE_PATH.read_text(encoding="utf-8")))
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
    config.PROFILE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


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
        raise ValueError(f"Не удалось проверить файл: {exc}") from exc
    return str(candidate)


def save_upload(upload_file, prefix: str) -> str:
    """Сохраняет UploadFile в uploads/ и возвращает абсолютный путь."""
    UPLOAD_DIR.mkdir(exist_ok=True)
    safe_name = Path(upload_file.filename or "file.pdf").name
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
