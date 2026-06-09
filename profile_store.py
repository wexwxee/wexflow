"""Хранение данных для ассистированной подачи."""
import json
from pathlib import Path

import config

UPLOAD_DIR = config.BASE_DIR / "uploads"


def load_profile() -> dict:
    if config.PROFILE_PATH.exists():
        return json.loads(config.PROFILE_PATH.read_text(encoding="utf-8"))
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
    config.PROFILE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def save_upload(upload_file, prefix: str) -> str:
    """Сохраняет UploadFile в uploads/ и возвращает абсолютный путь."""
    UPLOAD_DIR.mkdir(exist_ok=True)
    safe_name = Path(upload_file.filename or "file.pdf").name
    target = UPLOAD_DIR / f"{prefix}_{safe_name}"
    with target.open("wb") as f:
        f.write(upload_file.file.read())
    return str(target)


def file_label(path: str) -> str:
    if not path:
        return "Файл не выбран"
    p = Path(path)
    return p.name if p.name else path


def file_status(path: str) -> str:
    if not path:
        return "empty"
    return "ok" if Path(path).exists() else "missing"
