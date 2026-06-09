"""Конфигурация. Параметры Algolia получены через discover.py/capture_algolia.py."""
import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "jobs.db"
PROFILE_PATH = BASE_DIR / "profile.json"          # данные кандидата для предзаполнения
BROWSER_PROFILE_DIR = BASE_DIR / "browser_profile"  # persistent context (логин SuccessFactors)
SECRETS_PATH = BASE_DIR / "secrets.json"


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
