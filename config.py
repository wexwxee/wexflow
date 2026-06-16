"""Конфигурация. Параметры Algolia получены через discover.py/capture_algolia.py."""
import json
import os
from pathlib import Path

import paths

# BASE_DIR — код и ресурсы (в сборке = распакованные данные PyInstaller).
# DATA_DIR — пользовательские данные (в сборке = %AppData%\WexFlow\salling).
# В обычном dev-запуске обе указывают на папку проекта — поведение прежнее.
BASE_DIR = paths.RESOURCE_DIR
DATA_DIR = paths.DATA_DIR
# SHARED_DIR — общие для всех модулей WexFlow данные (в сборке = %AppData%\WexFlow).
# Здесь живут единый профиль кандидата и состояние подписки, чтобы Salling и
# 7-Eleven могли использовать одни и те же данные. В dev совпадает с проектом.
SHARED_DIR = paths.SHARED_DIR
DB_PATH = DATA_DIR / "jobs.db"
PROFILE_PATH = DATA_DIR / "profile.json"          # старое местоположение профиля (для миграции)
SHARED_PROFILE_PATH = SHARED_DIR / "profile.json"  # единый профиль кандидата (общий для модулей)
LICENSE_PATH = SHARED_DIR / "license.json"         # состояние подписки (заготовка)
BROWSER_PROFILE_DIR = DATA_DIR / "browser_profile"  # persistent context (логин SuccessFactors)
SECRETS_PATH = DATA_DIR / "secrets.json"


def _secrets() -> dict:
    if SECRETS_PATH.exists():
        return json.loads(SECRETS_PATH.read_text(encoding="utf-8"))
    return {}

# --- Algolia (публичный search-only ключ, безопасен для клиента) ---
ALGOLIA_APP_ID = "OQYVCGUT3U"
ALGOLIA_API_KEY = "07b09170543ce3fb07fe8044c4c84cfe"
ALGOLIA_INDEX = "prod_JOBS"
ALGOLIA_URL = f"https://{ALGOLIA_APP_ID.lower()}-dsn.algolia.net/1/indexes/{ALGOLIA_INDEX}/query"

# Поля, которые забираем из Algolia
ATTRS = [
    "address", "applicationLink", "brand", "categories", "country", "created",
    "datasourceId", "description", "employmentType", "external", "hours",
    "jobLevel", "jobType", "premium", "published", "region", "requisitionId",
    "start", "title", "trainee", "unsolicited", "id", "modified", "url", "objectID",
]


# --- DeepL перевод описаний вакансий ---
_secret_data = _secrets()
DEEPL_API_KEY = os.getenv("DEEPL_API_KEY") or _secret_data.get("deepl_api_key", "")
DEEPL_API_URL = (
    os.getenv("DEEPL_API_URL")
    or _secret_data.get("deepl_api_url")
    or ("https://api-free.deepl.com/v2/translate" if str(DEEPL_API_KEY).endswith(":fx") else "https://api.deepl.com/v2/translate")
)
